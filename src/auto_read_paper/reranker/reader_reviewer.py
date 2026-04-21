"""Multi-agent reranker: per-paper Reader produces structured notes,
then a single batch Reviewer ranks them and picks the top-K.

Pipeline:
    candidates --[keyword pre-filter]--> kept
              --[Reader x N parallel]--> notes
              --[Reviewer x 1 batch]--> ranked top-K
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from loguru import logger
from omegaconf import DictConfig, OmegaConf
from tqdm import tqdm

from ..llm_client import LLMClient
from ..protocol import Paper, CorpusPaper
from .base import BaseReranker, register_reranker
from .keyword_llm import _normalize_keywords, count_keyword_hits


READER_SYSTEM_PROMPT = (
    "You are a fast paper reader. Read the given title, abstract, and a preview of "
    "the main content, then produce CONCISE structured notes AND judge whether the "
    "paper's CORE research belongs to the user's keyword domain(s).\n"
    "\n"
    "DOMAIN RELEVANCE — READ CAREFULLY:\n"
    "  • Judge based on the paper's CORE TASK, METHOD, and CONTRIBUTIONS — NOT on "
    "    whether the keywords merely appear in the intro/related-work as background "
    "    citation or motivation.\n"
    "  • Example: a paper whose core contribution is a new reinforcement-learning "
    "    algorithm for robotic manipulation, which only mentions \"autonomous driving\" "
    "    once as motivation, is NOT in the autonomous-driving domain.\n"
    "  • Example: a paper that uses \"diffusion models\" as an off-the-shelf component "
    "    to study a different problem (e.g. protein folding) is NOT in the "
    "    diffusion-model domain unless the core contribution is a new diffusion technique.\n"
    "\n"
    "Output one of three values for domain_relevant:\n"
    "  • \"yes\"      — the paper's main contribution sits squarely inside at least one keyword.\n"
    "  • \"no\"       — clearly outside; the keyword is only a passing mention.\n"
    "  • \"uncertain\" — you cannot tell from the given content (ambiguous framing, "
    "    partial text, keyword is arguably central but not definitively). A senior "
    "    reviewer will adjudicate these later — DO NOT guess \"yes\" or \"no\" just "
    "    to avoid \"uncertain\".\n"
    "If no keywords are provided, set domain_relevant = \"yes\".\n"
    "\n"
    "Return ONLY a compact JSON object with keys "
    '"task", "method", "contributions", "results", "limitations", '
    '"domain_relevant", "relevance_reason". '
    "Each note value should be a single sentence (<= 30 words). "
    'domain_relevant is one of "yes" / "no" / "uncertain"; relevance_reason is one '
    "short sentence (<= 25 words) explaining which keyword matches the CORE "
    "contribution (or why none does, or why it's ambiguous). "
    "No prose outside the JSON.\n"
    "\n"
    "When in doubt, prefer \"uncertain\" over \"yes\" — a senior Reviewer will "
    "adjudicate. Never default to \"yes\" just because the keyword appears in "
    "the text; it must describe the CORE contribution."
)

REVIEWER_SYSTEM_PROMPT = (
    "You are a senior research reviewer. Given structured notes for several papers, "
    "rank them by overall value to a researcher with the stated keywords. "
    "Evaluate each paper holistically on all of the following dimensions, and "
    "fold them into a single score:\n"
    "  • soundness — is the motivation well-posed, the method rigorous?\n"
    "  • novelty — genuinely new idea vs. incremental tweak?\n"
    "  • effectiveness — do reported results support the claims?\n"
    "  • completeness — baselines, ablations, failure modes covered?\n"
    "  • reproducibility — clear setup, data, hyper-parameters, likely-releasable code?\n"
    "  • trending — topic alignment with active research fronts and the user's keywords?\n"
    "A paper strong on novelty but weak on effectiveness + reproducibility should "
    "NOT score near the top; likewise a thorough but routine paper should not beat a "
    "novel and sound one. Calibrate globally — the best paper in the batch anchors "
    "the top of the scale; the weakest anchors the bottom.\n"
    "\n"
    "SCORING SCALE — READ CAREFULLY:\n"
    "  • The score is on a 0-10 INTEGER scale (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10).\n"
    "  • It is NOT a 0-1 probability. Do NOT return values like 0.8 or 0.95.\n"
    "  • Use the FULL range: a mediocre paper should get ~5, a strong one ~8, an "
    "    outstanding one 9-10, a weak one 2-4. Do NOT collapse every paper to the "
    "    same number — discriminate between them.\n"
    "  • If the batch is homogeneous, still produce at least 2-3 distinct integers "
    "    across papers so the caller can rank them.\n"
    "\n"
    "Return ONLY a compact JSON object: "
    '{"rankings": [{"id": <int>, "score": <integer 0-10>, "reason": "<one sentence covering the strongest and weakest dimensions>"}, ...]} '
    "ordered from best to worst. Include EVERY paper id you were given."
)


ADJUDICATOR_SYSTEM_PROMPT = (
    "You are a senior research reviewer acting as a domain-relevance adjudicator. "
    "A junior Reader could not confidently decide whether each paper's CORE "
    "contribution lies inside the user's keyword domain(s). Your job is the "
    "final yes/no decision, one paper at a time, based on the Reader's notes "
    "(task, method, contributions, results, limitations) plus the Reader's "
    "own hesitation reason.\n"
    "\n"
    "RULES:\n"
    "  • \"yes\" only if the paper's main contribution, problem, or method "
    "    sits squarely inside at least one of the user's keywords. A keyword "
    "    appearing only as motivation, citation, or an off-the-shelf component "
    "    does NOT count.\n"
    "  • \"no\" otherwise. When genuinely ambiguous, prefer \"no\" — the user "
    "    would rather miss a borderline paper than read an off-topic one.\n"
    "  • You MUST decide for every id given. No \"uncertain\" output.\n"
    "\n"
    "Return ONLY a compact JSON object: "
    '{"verdicts": [{"id": <int>, "relevant": true|false, "reason": "<one short sentence>"}, ...]} '
    "including every id you were given."
)


def _normalize_reader_notes(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    out = {}
    for k in ("task", "method", "contributions", "results", "limitations"):
        v = data.get(k)
        out[k] = str(v).strip() if v is not None else ""
    # Tri-state domain relevance: "yes" / "no" / "uncertain".
    # Back-compat: accept legacy bool/numeric/truthy-string forms and map them
    # to yes/no. Default to "uncertain" when the field is missing so older
    # model responses go to the Reviewer adjudication path instead of being
    # silently admitted or silently dropped.
    raw = data.get("domain_relevant", "uncertain")
    if isinstance(raw, bool):
        rel = "yes" if raw else "no"
    elif isinstance(raw, (int, float)):
        rel = "yes" if raw else "no"
    elif isinstance(raw, str):
        s = raw.strip().lower()
        if s in {"yes", "true", "1", "y", "t", "relevant"}:
            rel = "yes"
        elif s in {"no", "false", "0", "n", "f", "irrelevant"}:
            rel = "no"
        elif s in {"uncertain", "maybe", "unsure", "unknown", "ambiguous"}:
            rel = "uncertain"
        else:
            rel = "uncertain"
    else:
        rel = "uncertain"
    out["domain_relevant"] = rel
    out["relevance_reason"] = str(data.get("relevance_reason", "")).strip()[:300]
    return out


def _normalize_reviewer_rankings(data, expected_ids: set[int]) -> list[dict] | None:
    if not isinstance(data, dict):
        return None
    rankings = data.get("rankings")
    if not isinstance(rankings, list):
        return None
    cleaned: list[dict] = []
    seen: set[int] = set()
    for item in rankings:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("id"))
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            continue
        if pid not in expected_ids or pid in seen:
            continue
        seen.add(pid)
        cleaned.append({
            "id": pid,
            "score": max(0.0, min(10.0, score)),
            "reason": str(item.get("reason", ""))[:300],
        })
    if not cleaned:
        return None

    # Scale-rescue: some smaller LLMs silently treat the 0-10 rubric as a
    # 0-1 probability and return scores like 0.7 / 0.9 / 1.0 across the
    # whole batch. That surfaces in the email as every paper showing
    # "Relevance 1.0" (after rounding). If the WHOLE batch sits in [0, 1],
    # multiply back up to the intended 0-10 scale. We require >=2 papers
    # to avoid accidentally rescaling a single low-rated paper.
    if len(cleaned) >= 2 and all(c["score"] <= 1.0 for c in cleaned):
        logger.warning(
            "Reviewer returned every score in [0, 1] — the model likely "
            "misread the 0-10 rubric as 0-1. Rescaling x10 so the ranking "
            "is still usable."
        )
        for c in cleaned:
            c["score"] = round(c["score"] * 10.0, 2)
    return cleaned


@register_reranker("reader_reviewer")
class ReaderReviewerReranker(BaseReranker):
    """Two-agent reranker: Reader (per-paper, parallel) + Reviewer (batch)."""

    def __init__(self, config: DictConfig):
        super().__init__(config)
        rr_cfg = config.reranker.reader_reviewer
        self.threshold: float = float(rr_cfg.get("threshold", 0.0))
        self.concurrency: int = int(rr_cfg.get("concurrency", 4))
        self.reader_max_tokens: int = int(rr_cfg.get("reader_max_input_tokens", 3000))
        self.reviewer_max_papers: int = int(rr_cfg.get("reviewer_max_papers", 60))
        self.keywords = _normalize_keywords(
            OmegaConf.to_container(config.source.arxiv.get("keywords"), resolve=True)
            if config.source.arxiv.get("keywords") is not None
            else None
        )
        self.llm = LLMClient.from_config(config.llm)

    def get_similarity_score(self, s1, s2):  # pragma: no cover - not used
        raise NotImplementedError("reader_reviewer reranker does not use similarity scoring")

    def _read_one(self, paper: Paper) -> dict | None:
        body = ""
        if paper.title:
            body += f"Title: {paper.title}\n\n"
        if paper.abstract:
            body += f"Abstract: {paper.abstract}\n\n"
        if paper.full_text:
            body += f"Main content preview:\n{paper.full_text}\n"
        body = self.llm.truncate_to_tokens(body, self.reader_max_tokens)
        if not body.strip():
            return None
        try:
            from ..protocol import _wrap_untrusted
            data = self.llm.complete_json(
                system=READER_SYSTEM_PROMPT,
                user=_wrap_untrusted(body),
                expect="object",
            )
        except Exception as e:
            logger.warning(f"Reader failed for {paper.title}: {e}")
            return None
        notes = _normalize_reader_notes(data)
        if notes is None:
            logger.warning(f"Unparseable Reader output for {paper.title}")
        return notes

    def _build_reviewer_prompt(self, paper_notes: list[tuple[int, Paper, dict]]) -> str:
        lines = [
            f"User research keywords: {', '.join(self.keywords) if self.keywords else '(not provided)'}",
            f"Number of papers to rank: {len(paper_notes)}",
            "",
            "Papers:",
        ]
        for pid, paper, note in paper_notes:
            lines.append(f"--- id: {pid} ---")
            lines.append(f"Title: {paper.title}")
            lines.append(f"Task: {note.get('task', '')}")
            lines.append(f"Method: {note.get('method', '')}")
            lines.append(f"Contributions: {note.get('contributions', '')}")
            lines.append(f"Results: {note.get('results', '')}")
            lines.append(f"Limitations: {note.get('limitations', '')}")
            lines.append("")
        lines.append(
            "Return JSON only: "
            '{"rankings": [{"id": <int>, "score": <integer 0-10>, "reason": "..."}, ...]} '
            "ordered best-first, including every id above. "
            "Score is an INTEGER from 0 to 10 (NOT a 0-1 probability). "
            "Use the full range and discriminate — do NOT give every paper the same score."
        )
        return "\n".join(lines)

    def _call_reviewer(self, prompt: str, expected_ids: set[int], extra_system: str = "") -> list[dict] | None:
        from ..protocol import _wrap_untrusted
        system = REVIEWER_SYSTEM_PROMPT + (("\n\n" + extra_system) if extra_system else "")
        data = self.llm.complete_json(
            system=system,
            user=_wrap_untrusted(prompt),
            expect="object",
        )
        return _normalize_reviewer_rankings(data, expected_ids)

    @staticmethod
    def _is_collapsed(rankings: list[dict]) -> bool:
        """True iff the Reviewer failed to discriminate.

        Catches two common failure modes that both surface as "Relevance 1.0
        everywhere" in the email: (a) every score identical (no ranking
        signal), and (b) every score in [0, 1] with no rescue possible
        upstream (e.g. [0.0, 0.0, 1.0] — the parser's scale-rescue only
        rescales when the WHOLE batch is <=1, which triggers here too).
        """
        if not rankings or len(rankings) < 2:
            return False
        scores = [r["score"] for r in rankings]
        if max(scores) - min(scores) < 0.5:
            return True
        return False

    def _adjudicate_uncertain(
        self, uncertain: list[tuple[int, Paper, dict]]
    ) -> dict[int, dict] | None:
        """Ask the Reviewer to give a final yes/no on Reader-uncertain papers.

        Returns {paper_id: {"relevant": bool, "reason": str}} or None on
        failure. Caller is conservative on None (drops all uncertain).
        """
        if not uncertain:
            return {}
        expected_ids = {pid for pid, _, _ in uncertain}
        lines = [
            f"User research keywords: {', '.join(self.keywords) if self.keywords else '(not provided)'}",
            f"Number of papers to adjudicate: {len(uncertain)}",
            "",
            "Papers:",
        ]
        for pid, paper, note in uncertain:
            lines.append(f"--- id: {pid} ---")
            lines.append(f"Title: {paper.title}")
            lines.append(f"Task: {note.get('task', '')}")
            lines.append(f"Method: {note.get('method', '')}")
            lines.append(f"Contributions: {note.get('contributions', '')}")
            lines.append(f"Results: {note.get('results', '')}")
            lines.append(f"Limitations: {note.get('limitations', '')}")
            lines.append(f"Reader's hesitation: {note.get('relevance_reason', '')}")
            lines.append("")
        lines.append(
            'Return JSON only: {"verdicts": [{"id": <int>, "relevant": true|false, '
            '"reason": "..."}, ...]} including every id above.'
        )
        prompt = "\n".join(lines)

        from ..protocol import _wrap_untrusted
        try:
            data = self.llm.complete_json(
                system=ADJUDICATOR_SYSTEM_PROMPT,
                user=_wrap_untrusted(prompt),
                expect="object",
            )
        except Exception as e:
            logger.warning(f"Adjudicator call raised: {e}")
            return None
        if not isinstance(data, dict):
            return None
        verdicts = data.get("verdicts")
        if not isinstance(verdicts, list):
            return None
        out: dict[int, dict] = {}
        for item in verdicts:
            if not isinstance(item, dict):
                continue
            try:
                pid = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if pid not in expected_ids or pid in out:
                continue
            rel_raw = item.get("relevant")
            if isinstance(rel_raw, bool):
                rel = rel_raw
            elif isinstance(rel_raw, (int, float)):
                rel = bool(rel_raw)
            elif isinstance(rel_raw, str):
                rel = rel_raw.strip().lower() in {"true", "yes", "1", "y", "t"}
            else:
                # Conservative: treat malformed/missing as not relevant.
                rel = False
            out[pid] = {
                "relevant": rel,
                "reason": str(item.get("reason", ""))[:300],
            }
        return out if out else None

    def _review_batch(self, paper_notes: list[tuple[int, Paper, dict]]) -> list[dict] | None:
        if not paper_notes:
            return None
        expected_ids = {pid for pid, _, _ in paper_notes}
        prompt = self._build_reviewer_prompt(paper_notes)
        try:
            rankings = self._call_reviewer(prompt, expected_ids)
        except Exception as e:
            logger.warning(f"Reviewer batch failed: {e}")
            return None
        if rankings is None:
            logger.warning("Unparseable Reviewer output")
            return None

        # Second-chance retry when the Reviewer returns a collapsed ranking
        # (every paper ~identical). Some smaller models do this on their
        # first pass; a sterner system-message + an explicit reminder to
        # discriminate fixes it ~80% of the time in practice.
        if self._is_collapsed(rankings):
            logger.warning(
                f"Reviewer output is collapsed (scores: "
                f"{[r['score'] for r in rankings]}) — retrying once with a "
                f"stricter reminder to use the full 0-10 range."
            )
            stricter = (
                "CRITICAL: your previous answer gave every paper the same score. "
                "That is not a ranking. This time, you MUST use at least 3 "
                "distinct integer scores across the batch — e.g. a weak paper "
                "gets 3, a routine paper 5, a strong paper 8. Never cluster "
                "all papers around a single value."
            )
            try:
                rankings2 = self._call_reviewer(prompt, expected_ids, extra_system=stricter)
            except Exception as e:
                logger.warning(f"Reviewer retry failed: {e} — keeping first-pass rankings")
                return rankings
            if rankings2 is not None and not self._is_collapsed(rankings2):
                logger.info("Reviewer retry succeeded — using retried rankings.")
                return rankings2
            logger.warning(
                "Reviewer retry still collapsed — keeping first-pass rankings. "
                "Consider switching to a stronger model (gpt-4o-mini, "
                "deepseek-chat, Qwen2.5-72B) if this persists."
            )
        return rankings

    def rerank(
        self,
        candidates: list[Paper],
        corpus: list[CorpusPaper],
        *,
        skip_keyword_filter: bool = False,
    ) -> list[Paper]:
        if not candidates:
            return []

        # Belt & suspenders: keyword pre-filter (retriever may already have done this).
        # Skipped when the executor is explicitly rescuing keyword-filtered-out
        # papers to fill an under-sized pool.
        if self.keywords and not skip_keyword_filter:
            filtered = [p for p in candidates if count_keyword_hits(p, self.keywords) > 0]
            logger.info(
                f"Keyword pre-filter: {len(filtered)}/{len(candidates)} papers kept "
                f"(keywords={self.keywords})"
            )
            candidates = filtered
        if not candidates:
            return []

        # Cap how many go to the Reviewer (token budget)
        if len(candidates) > self.reviewer_max_papers:
            logger.info(
                f"Trimming candidates to first {self.reviewer_max_papers} for the Reviewer "
                f"(was {len(candidates)})"
            )
            candidates = candidates[: self.reviewer_max_papers]

        logger.info(
            f"Reader agent: reading {len(candidates)} papers (concurrency={self.concurrency})..."
        )
        notes_by_idx: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.concurrency)) as ex:
            futures = {ex.submit(self._read_one, p): i for i, p in enumerate(candidates)}
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Reading"):
                i = futures[fut]
                try:
                    note = fut.result()
                except Exception as e:
                    logger.warning(f"Reader worker raised: {e}")
                    note = None
                if note is not None:
                    notes_by_idx[i] = note

        paper_notes = [(i, candidates[i], notes_by_idx[i]) for i in sorted(notes_by_idx)]
        logger.info(f"Reader agent: {len(paper_notes)}/{len(candidates)} papers produced notes")
        if not paper_notes:
            logger.warning("Reader produced no notes; returning unranked candidates.")
            for p in candidates:
                p.score = 0.0
            return candidates

        # Tri-state domain-relevance gate: yes / no / uncertain.
        # - "no"        → dropped immediately.
        # - "uncertain" → adjudicated by the Reviewer (second-opinion call).
        # Runs whenever the user configured keywords — INCLUDING the
        # skip_keyword_filter rescue pass. skip_keyword_filter only opts out
        # of the coarse substring pre-filter (so LLM-expanded / back-catalog
        # searches aren't double-filtered by the narrow keyword list); the
        # semantic domain gate still applies so off-topic papers whose
        # abstracts happen to mention a keyword in passing never reach the
        # email.
        if self.keywords:
            kept, dropped, uncertain = [], [], []
            for triple in paper_notes:
                _i, paper, note = triple
                rel = note.get("domain_relevant", "uncertain")
                if rel == "yes":
                    kept.append(triple)
                elif rel == "no":
                    dropped.append((paper, note.get("relevance_reason", "")))
                else:
                    uncertain.append(triple)

            if uncertain:
                logger.info(
                    f"Domain-relevance gate: {len(uncertain)} paper(s) flagged "
                    f"uncertain by Reader — asking Reviewer to adjudicate."
                )
                verdicts = self._adjudicate_uncertain(uncertain)
                if verdicts is None:
                    # Adjudicator failed. Fall back to the conservative
                    # default (drop) so off-topic papers don't slip through
                    # just because the second-opinion call errored.
                    logger.warning(
                        "Adjudicator call failed or returned unparseable JSON — "
                        "conservatively dropping all uncertain papers."
                    )
                    for _i, paper, note in uncertain:
                        dropped.append(
                            (paper, note.get("relevance_reason", "") + " [adjudicator failed]")
                        )
                else:
                    for triple in uncertain:
                        _i, paper, note = triple
                        v = verdicts.get(_i)
                        if v is None:
                            # Adjudicator silently dropped this id. Be
                            # conservative: drop it.
                            dropped.append(
                                (paper, note.get("relevance_reason", "") + " [no adjudicator verdict]")
                            )
                        elif v.get("relevant"):
                            logger.info(
                                f"  adjudicator→KEEP: {paper.title[:90]} — "
                                f"{v.get('reason', '')}"
                            )
                            kept.append(triple)
                        else:
                            logger.info(
                                f"  adjudicator→DROP: {paper.title[:90]} — "
                                f"{v.get('reason', '')}"
                            )
                            dropped.append((paper, v.get("reason", "")))

            if dropped:
                logger.info(
                    f"Domain-relevance gate: dropped {len(dropped)}/{len(paper_notes)} "
                    f"paper(s) whose core contribution is outside the keyword domain"
                )
                for p, reason in dropped[:10]:
                    logger.info(f"  drop: {p.title[:90]} — {reason or '(no reason)'}")
                if len(dropped) > 10:
                    logger.info(f"  ... and {len(dropped) - 10} more")
            paper_notes = kept
            if not paper_notes:
                logger.warning(
                    "Domain-relevance gate removed every paper — returning empty. "
                    "Executor will fall through to its pool-short rescue logic."
                )
                return []

        logger.info(f"Reviewer agent: ranking {len(paper_notes)} papers in one batch call...")
        rankings = self._review_batch(paper_notes)
        if rankings is None:
            # Loud warning because the visible symptom (every paper showing
            # the same low "Relevance" score in the email) is confusing.
            logger.error(
                "Reviewer LLM call failed or returned unparseable JSON. "
                "Falling back to keyword-hit count, projected onto the 0-10 "
                "scale so the email doesn't show 'Relevance 1.0' for every "
                "paper. The underlying LLM issue still needs fixing — "
                "check the model / base_url / max_tokens / rate-limit."
            )
            hits = [count_keyword_hits(p, self.keywords) for p in candidates]
            max_hits = max(hits) if hits else 0
            for p, h in zip(candidates, hits):
                if max_hits > 0:
                    # Spread 2.0 (baseline) → 8.0 (best) so the UI clearly
                    # signals "these are degraded scores" without collapsing
                    # every paper to the same number.
                    p.score = round(2.0 + (h / max_hits) * 6.0, 1)
                else:
                    p.score = 2.0
            ranked = sorted(candidates, key=lambda p: p.score or 0.0, reverse=True)
            return ranked

        score_by_id = {r["id"]: r for r in rankings}
        for i, paper, _note in paper_notes:
            entry = score_by_id.get(i)
            paper.score = entry["score"] if entry else 0.0
            if entry and entry.get("reason"):
                logger.debug(f"[{paper.score:.2f}] {paper.title[:80]} — {entry['reason']}")

        # Ordered as the Reviewer ranked
        results: list[Paper] = []
        for r in rankings:
            paper = candidates[r["id"]]
            if (paper.score or 0.0) >= self.threshold:
                results.append(paper)

        # Score audit log at INFO level so users can SEE whether the scorer
        # actually discriminated without needing to flip on DEBUG. Catches
        # the "every paper shows Relevance 1.0" symptom immediately.
        if results:
            score_preview = ", ".join(
                f"{p.score:.1f}" for p in results[: min(10, len(results))]
            )
            uniq = len({round(p.score or 0.0, 1) for p in results})
            logger.info(
                f"Reranker scores (top {min(10, len(results))}): [{score_preview}] "
                f"| {uniq} distinct value(s) across {len(results)} paper(s)"
            )
        logger.info(
            f"Reranked: {len(results)} papers passed threshold {self.threshold}"
        )
        return results
