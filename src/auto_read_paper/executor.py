from loguru import logger
from omegaconf import DictConfig, OmegaConf
from .retriever import get_retriever_cls
from .reranker import get_reranker_cls
from .reranker.keyword_llm import _normalize_keywords, count_keyword_hits
from .construct_email import render_email
from .utils import send_email
from .history import ScoreHistory, _today_iso, _paper_id
from .llm_client import LLMClient
from tqdm import tqdm


def _expand_keywords(llm: LLMClient, keywords: list[str], n: int = 12) -> list[str]:
    """Ask the LLM for related/alternative keywords covering the same research
    area. Used to refill the candidate pool when too few papers hit the
    user's exact keywords — instead of dropping the filter entirely and
    letting unrelated papers in, we broaden the filter with AI-suggested
    synonyms, abbreviations, and adjacent subtopics."""
    if not keywords:
        return []
    system = (
        "You are a research librarian. Given a user's research keywords, "
        "produce related terms, common synonyms, and abbreviations that "
        "would match papers on the same topics. Stay tight to the user's "
        "research area — do not drift into unrelated fields."
    )
    user = (
        f"User keywords: {', '.join(keywords)}\n\n"
        f"Return ONLY a JSON array of up to {n} additional keywords (strings), "
        f"lowercase, no duplicates of the user's keywords, no prose."
    )
    result = llm.complete_json(system=system, user=user, expect="array")
    if not isinstance(result, list):
        logger.warning("Keyword expansion LLM call returned no usable list")
        return []
    seen = {k.lower() for k in keywords}
    expanded: list[str] = []
    for k in result:
        if isinstance(k, str) and k.strip() and k.strip().lower() not in seen:
            kw = k.strip().lower()
            expanded.append(kw)
            seen.add(kw)
    return expanded


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

        # Segregate today's fresh retrieval by the keyword filter so we can
        # dip into the spillover (keyword-mismatched) half if the primary
        # pool doesn't fill max_n. The rerankers will apply the same filter
        # internally; segregating up-front just lets us re-score the rejects.
        kw_cfg = arxiv_cfg.get("keywords") if arxiv_cfg is not None else None
        keywords = _normalize_keywords(
            OmegaConf.to_container(kw_cfg, resolve=True) if kw_cfg is not None else []
        )
        if keywords:
            primary_today = [p for p in all_papers if count_keyword_hits(p, keywords) > 0]
            spillover_today = [p for p in all_papers if count_keyword_hits(p, keywords) == 0]
        else:
            primary_today = list(all_papers)
            spillover_today = []

        # Score today's new papers.
        scored_today: list = []
        if primary_today:
            logger.info("Reranking papers (keyword filter + LLM scoring)...")
            scored_today = self.reranker.rerank(primary_today, [])

        # Record today's scores, then merge with unsent history into the candidate pool.
        # unsent_papers() never includes papers already sent on ANY day — each push
        # therefore only ever considers papers the user has not yet received. No
        # filler from sent_papers(), no content-hash dedup: multi-push-per-day is
        # a side-effect of the pool naturally shrinking as papers get marked sent.
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

        # Pool-short fallback: today's RSS is retrieved in full (no cap) and
        # filtered by the user's exact keywords. Only when that primary pool
        # plus unsent history still doesn't reach max_n do we broaden: ask
        # the LLM to expand the keyword set, then walk arXiv's submission
        # history in progressively wider time windows until the pool is full
        # or we've exhausted the search horizon.
        #
        # Critically, every rescue candidate still passes through the
        # reranker's semantic domain-relevance gate (skip_keyword_filter only
        # opts out of the coarse substring pre-filter, not the LLM gate), so
        # off-topic papers whose abstracts merely mention a keyword in passing
        # never reach the email. Better to send fewer papers than wrong ones.
        if len(top_papers) < max_n and keywords:
            arxiv_retriever = self.retrievers.get("arxiv")
            has_search = (
                arxiv_retriever is not None
                and hasattr(arxiv_retriever, "search_by_keywords")
            )

            expanded = _expand_keywords(self.llm, keywords) if has_search else []
            if has_search and not expanded:
                logger.warning(
                    "Keyword expansion produced no usable terms — falling back "
                    "to the user's original keywords for the back-catalog search."
                )
            combined = keywords + expanded if has_search else []

            # First: re-check today's keyword-rejected retrieval against the
            # expanded keyword set. Cheap, local, no extra arXiv calls.
            if combined and spillover_today:
                already_scored = (
                    self.history.existing_ids() if self.history is not None else set()
                )
                sent_ids = (
                    {e.get("id") for e in self.history.entries if e.get("sent_at")}
                    if self.history is not None
                    else set()
                )
                matched_spill = [
                    p for p in spillover_today
                    if _paper_id(p) not in already_scored
                    and _paper_id(p) not in sent_ids
                    and count_keyword_hits(p, combined) > 0
                ]
                if matched_spill:
                    logger.info(
                        f"Spillover from today's retrieval: {len(matched_spill)} "
                        f"paper(s) match expanded keywords — running through "
                        f"domain-relevance gate"
                    )
                    scored_spill = self.reranker.rerank(
                        matched_spill, [], skip_keyword_filter=True
                    )
                    if self.history is not None:
                        self.history.record_newly_scored(scored_spill, today)
                        pool = self.history.unsent_papers()
                    else:
                        pool = pool + list(scored_spill)
                    pool.sort(key=lambda p: p.score or 0.0, reverse=True)
                    top_papers = pool[:max_n]

            # Progressive time-window back-catalog search. Widens the window
            # until the pool is full or we've hit the horizon. Every hit
            # still passes through the domain-relevance gate inside rerank().
            if has_search and combined and len(top_papers) < max_n:
                seen_search_ids: set[str] = set()
                for window_days in (7, 14, 30, 60, 120, 240):
                    if len(top_papers) >= max_n:
                        break
                    logger.info(
                        f"Back-catalog window={window_days}d — current pool "
                        f"{len(top_papers)}/{max_n}"
                    )
                    already_scored = (
                        self.history.existing_ids() if self.history is not None else set()
                    )
                    sent_ids = (
                        {e.get("id") for e in self.history.entries if e.get("sent_at")}
                        if self.history is not None
                        else set()
                    )
                    try:
                        searched = arxiv_retriever.search_by_keywords(
                            combined,
                            days=window_days,
                            limit=min(max_n * 10, 200),
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Back-catalog search at {window_days}d failed: {exc}"
                        )
                        continue
                    fresh = []
                    for p in searched:
                        pid = _paper_id(p)
                        if (
                            pid in already_scored
                            or pid in sent_ids
                            or pid in seen_search_ids
                        ):
                            continue
                        seen_search_ids.add(pid)
                        fresh.append(p)
                    if not fresh:
                        logger.info(
                            f"  window={window_days}d yielded 0 new candidates "
                            f"(all already scored/sent/seen)"
                        )
                        continue
                    logger.info(
                        f"  window={window_days}d: {len(fresh)} new candidate(s) "
                        f"— running domain-relevance gate"
                    )
                    scored_fill = self.reranker.rerank(
                        fresh, [], skip_keyword_filter=True
                    )
                    if self.history is not None:
                        self.history.record_newly_scored(scored_fill, today)
                        pool = self.history.unsent_papers()
                    else:
                        pool = pool + list(scored_fill)
                    pool.sort(key=lambda p: p.score or 0.0, reverse=True)
                    top_papers = pool[:max_n]

                if len(top_papers) < max_n:
                    logger.info(
                        f"Back-catalog exhausted: pool still short "
                        f"({len(top_papers)}/{max_n}). Sending what we have "
                        f"rather than diluting with off-topic papers."
                    )

        # Last-resort heartbeat: only when the user did NOT configure
        # keywords and the unsent pool is empty. With keywords configured,
        # the progressive back-catalog above is the correct path and
        # heartbeat filler would just bypass the domain gate we just
        # installed.
        if not top_papers and not keywords:
            arxiv_retriever = self.retrievers.get("arxiv")
            if arxiv_retriever is not None and hasattr(arxiv_retriever, "retrieve_fallback_papers"):
                logger.info("Pool empty — fetching recent arXiv papers as heartbeat fallback")
                try:
                    fb = arxiv_retriever.retrieve_fallback_papers(days=3, limit=max_n)
                except Exception as exc:
                    logger.warning(f"Heartbeat fallback failed: {exc}")
                    fb = []
                if fb and self.history is not None:
                    sent_ids = {
                        e.get("id") for e in self.history.entries if e.get("sent_at")
                    }
                    fb = [p for p in fb if _paper_id(p) not in sent_ids]
                if fb:
                    logger.info(f"Scoring {len(fb)} heartbeat papers")
                    fb = self.reranker.rerank(fb, [], skip_keyword_filter=True)
                    fb.sort(key=lambda p: p.score or 0.0, reverse=True)
                    top_papers = fb[:max_n]
                    if self.history is not None:
                        self.history.record_newly_scored(top_papers, today)

        if not top_papers and not self.config.executor.send_empty:
            logger.info("No unsent papers available — no email will be sent.")
            if self.history is not None:
                self.history.save()
            return

        if top_papers:
            logger.info(f"Generating deep summaries for top {len(top_papers)} papers...")
            lang = str(self.config.llm.get("language", "Chinese"))
            for p in tqdm(top_papers):
                if not p.tldr:
                    p.generate_tldr(self.llm, lang)
                if not p.affiliations:
                    p.generate_affiliations(self.llm)
                if lang.lower() != "english" and not getattr(p, "title_zh", None):
                    p.generate_title_zh(self.llm, lang)

        lang = str(self.config.llm.get("language", "Chinese"))
        email_content = render_email(top_papers, lang)

        if self.history is not None:
            self.history.save()

        logger.info("Sending email...")
        send_email(self.config, email_content)
        logger.info("Email sent successfully")

        if self.history is not None:
            self.history.mark_sent(top_papers, today)
            self.history.save()
