import hashlib
import os

from loguru import logger
from omegaconf import DictConfig
from .retriever import get_retriever_cls
from .reranker import get_reranker_cls
from .construct_email import render_email
from .utils import send_email
from .history import ScoreHistory, _today_iso
from .llm_client import LLMClient
from tqdm import tqdm


class Executor:
    def __init__(self, config: DictConfig):
        self.config = config
        self.retrievers = {
            source: get_retriever_cls(source)(config) for source in config.executor.source
        }
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        self.llm = LLMClient.from_config(config.llm)

        hist_cfg = config.get("history") if hasattr(config, "get") else None
        self.history: ScoreHistory | None = None
        if hist_cfg is not None and bool(hist_cfg.get("enabled", True)):
            self.history = ScoreHistory(
                path=str(hist_cfg.get("path", "state/score_history.json")),
                retention_days=int(hist_cfg.get("retention_days", 7)),
            )

    def run(self):
        today = _today_iso()

        # Surface effective arXiv filter config up-front so users can verify
        # CUSTOM_CONFIG actually took effect (vs silently falling back to the
        # committed config/custom.yaml).
        arxiv_cfg = self.config.get("source", {}).get("arxiv") if hasattr(self.config, "get") else None
        if arxiv_cfg is not None:
            cats = list(arxiv_cfg.get("category") or [])
            kws = list(arxiv_cfg.get("keywords") or [])
            logger.info(f"Effective arXiv categories: {cats}")
            logger.info(f"Effective arXiv keywords: {kws}")

        if self.history is not None:
            self.history.load()
            self.history.trim()

        all_papers = []
        for source, retriever in self.retrievers.items():
            logger.info(f"Retrieving {source} papers...")
            papers = retriever.retrieve_papers()
            if len(papers) == 0:
                logger.info(f"No {source} papers found")
                continue
            logger.info(f"Retrieved {len(papers)} {source} papers")
            all_papers.extend(papers)
        logger.info(f"Total {len(all_papers)} papers retrieved from all sources")

        # Skip papers we've already scored within the retention window.
        if self.history is not None:
            all_papers = self.history.filter_new_papers(all_papers)
            logger.info(f"{len(all_papers)} new papers need scoring today")

        # Score today's new papers.
        scored_today: list = []
        if all_papers:
            logger.info("Reranking papers (keyword filter + LLM scoring)...")
            scored_today = self.reranker.rerank(all_papers, [])

        # Record today's scores, then merge with unsent history into the candidate pool.
        if self.history is not None:
            self.history.record_newly_scored(scored_today, today)
            pool = self.history.unsent_papers()
            logger.info(
                f"Candidate pool for today's email: {len(pool)} papers "
                f"(today={len(scored_today)} + unsent history)"
            )
        else:
            pool = list(scored_today)

        pool.sort(key=lambda p: p.score or 0.0, reverse=True)
        max_n = max(0, int(self.config.executor.max_paper_num))
        top_papers = pool[:max_n]

        # Fallback so the daily email is never empty: if we don't have enough
        # unsent/fresh papers, pad with previously-sent entries from history
        # (highest-scoring first). This guarantees the pipeline produces a
        # visible heartbeat every day even on quiet days.
        #
        # Critical for multi-push-per-day users: we EXCLUDE papers sent today
        # so the 2nd/3rd trigger within the same calendar day rotates to
        # different historical papers. Without this, each same-day trigger
        # would refill with exactly the batch we just sent → identical
        # content hash → dedup short-circuits the SMTP call and the user
        # never receives the second email.
        if len(top_papers) < max_n and self.history is not None:
            already_ids = {getattr(p, "url", None) for p in top_papers}
            filler_pool = [
                p for p in self.history.sent_papers(exclude_sent_on=today)
                if getattr(p, "url", None) not in already_ids
            ]
            filler_pool.sort(key=lambda p: p.score or 0.0, reverse=True)
            needed = max_n - len(top_papers)
            filler = filler_pool[:needed]
            if filler:
                logger.info(
                    f"Padding email with {len(filler)} previously-sent paper(s) "
                    f"as fallback (primary pool had only {len(top_papers)})"
                )
                top_papers.extend(filler)

        # Last-resort fallback: still nothing (e.g. first run with empty history on a
        # quiet day). Pull a few recent arXiv papers so the pipeline proves it's alive.
        if not top_papers:
            arxiv_retriever = self.retrievers.get("arxiv")
            if arxiv_retriever is not None and hasattr(arxiv_retriever, "retrieve_fallback_papers"):
                logger.info("Pool empty — fetching recent arXiv papers as heartbeat fallback")
                try:
                    fb = arxiv_retriever.retrieve_fallback_papers(days=3, limit=max_n)
                except Exception as exc:
                    logger.warning(f"Heartbeat fallback failed: {exc}")
                    fb = []
                if fb:
                    # Score them so the email still shows a ranked number.
                    logger.info(f"Scoring {len(fb)} heartbeat papers")
                    fb = self.reranker.rerank(fb, [])
                    fb.sort(key=lambda p: p.score or 0.0, reverse=True)
                    top_papers = fb[:max_n]
                    if self.history is not None:
                        self.history.record_newly_scored(top_papers, today)

        if not top_papers and not self.config.executor.send_empty:
            logger.info("No papers in pool even after fallback. No email will be sent.")
            if self.history is not None:
                self.history.save()
            return

        if top_papers:
            logger.info(f"Generating deep summaries for top {len(top_papers)} papers...")
            lang = str(self.config.llm.get("language", "Chinese"))
            for p in tqdm(top_papers):
                # Skip re-generating tldr for previously-rendered fillers that
                # already have it from a past run — saves tokens.
                if not p.tldr:
                    p.generate_tldr(self.llm, lang)
                if not p.affiliations:
                    p.generate_affiliations(self.llm)
                if lang.lower() != "english" and not getattr(p, "title_zh", None):
                    p.generate_title_zh(self.llm, lang)

        lang = str(self.config.llm.get("language", "Chinese"))
        email_content = render_email(top_papers, lang)
        content_hash = hashlib.sha256(email_content.encode("utf-8")).hexdigest()

        # Content-hash dedup. If the last successfully-sent email hashes to the
        # same value, this is a duplicate trigger — skip the SMTP send. Catches
        # overlapping workflow runs, stale cache restores, and mail-provider
        # replays. Hash covers every visible paper + summary, so a genuine new
        # candidate pool always differs and goes through.
        #
        # Escape hatch: set SKIP_DEDUP=1 to bypass (useful when re-sending the
        # same content for SMTP / rendering / template testing).
        skip_dedup = os.environ.get("SKIP_DEDUP", "").strip().lower() in ("1", "true", "yes")
        if (
            not skip_dedup
            and self.history is not None
            and self.history.is_duplicate_of_last_send(content_hash)
        ):
            last = self.history.last_sent_email
            logger.warning(
                f"Skipping duplicate send — identical email already sent on "
                f"{last.get('date')} (hash {content_hash[:12]}...). "
                f"This typically means two triggers fired for the same candidate "
                f"pool. Set SKIP_DEDUP=1 to force resend for debugging."
            )
            return
        if skip_dedup and self.history is not None and self.history.is_duplicate_of_last_send(content_hash):
            logger.warning(
                f"SKIP_DEDUP=1 set — sending despite identical-content hash "
                f"({content_hash[:12]}...) as last send."
            )

        # Persist the content hash BEFORE calling SMTP. This is the strongest
        # defense against "same-time duplicate" deliveries: if the runner is
        # killed, SMTP raises after partial delivery, or the cache-save step
        # later skips, the hash is already on disk — the next overlapping run
        # loads it via is_duplicate_of_last_send() and short-circuits.
        # Papers themselves are only marked sent AFTER SMTP succeeds, so a
        # genuine SMTP failure keeps them in the unsent pool for tomorrow.
        # Content-hash dedup still blocks a re-send of the SAME HTML; use
        # SKIP_DEDUP=1 to force one when you know the first send never landed.
        if self.history is not None:
            self.history.record_sent_email(content_hash, today)
            self.history.save()

        logger.info("Sending email...")
        send_email(self.config, email_content, content_hash=content_hash)
        logger.info("Email sent successfully")

        if self.history is not None:
            self.history.mark_sent(top_papers, today)
            self.history.save()
