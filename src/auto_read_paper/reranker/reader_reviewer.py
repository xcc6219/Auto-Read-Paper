"""Multi-agent reranker: per-paper Reader produces structured notes,
then a single batch Reviewer ranks them and picks the top-K.

Pipeline:
    candidates --[keyword pre-filter]--> kept
              --[Reader x N parallel]--> notes
              --[Reviewer x 1 batch]--> ranked top-K
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import tiktoken
from loguru import logger
from omegaconf import DictConfig, OmegaConf
from openai import OpenAI
from tqdm import tqdm

from ..protocol import Paper, CorpusPaper
from .base import BaseReranker, register_reranker
from .keyword_llm import _normalize_keywords, count_keyword_hits


READER_SYSTEM_PROMPT = (
    "You are a fast paper reader. Read the given title, abstract, and a preview of "
    "the main content, then produce CONCISE structured notes. "
    "Return ONLY a compact JSON object with keys "
    '"task", "method", "contributions", "results", "limitations". '
    "Each value should be a single sentence (<= 30 words). No prose outside the JSON."
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


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if not text:
        return ""
    enc = tiktoken.encoding_for_model("gpt-4o")
    toks = enc.encode(text)
    if len(toks) <= max_tokens:
        return text
    return enc.decode(toks[:max_tokens])


def _parse_reader_json(content: str) -> dict | None:
    if not content:
        return None
    m = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    out = {}
    for k in ("task", "method", "contributions", "results", "limitations"):
        v = data.get(k)
        out[k] = str(v).strip() if v is not None else ""
    return out


def _parse_reviewer_json(content: str, expected_ids: set[int]) -> list[dict] | None:
    if not content:
        return None
    m = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
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
        self.client = OpenAI(
            api_key=config.llm.api.key,
            base_url=config.llm.api.base_url,
        )
        self.model_kwargs = OmegaConf.to_container(
            config.llm.generation_kwargs, resolve=True
        ) or {}

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
        body = _truncate_to_tokens(body, self.reader_max_tokens)
        if not body.strip():
            return None
        try:
            resp = self.client.chat.completions.create(
                messages=[
                    {"role": "system", "content": READER_SYSTEM_PROMPT},
                    {"role": "user", "content": body},
                ],
                **self.model_kwargs,
            )
            content = resp.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"Reader failed for {paper.title}: {e}")
            return None
        notes = _parse_reader_json(content)
        if notes is None:
            logger.warning(f"Unparseable Reader output for {paper.title}: {content[:200]}")
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

    def _call_reviewer(self, prompt: str, extra_system: str = "") -> str:
        system = REVIEWER_SYSTEM_PROMPT + (("\n\n" + extra_system) if extra_system else "")
        resp = self.client.chat.completions.create(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            **self.model_kwargs,
        )
        return resp.choices[0].message.content or ""

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

    def _review_batch(self, paper_notes: list[tuple[int, Paper, dict]]) -> list[dict] | None:
        if not paper_notes:
            return None
        expected_ids = {pid for pid, _, _ in paper_notes}
        prompt = self._build_reviewer_prompt(paper_notes)
        try:
            content = self._call_reviewer(prompt)
        except Exception as e:
            logger.warning(f"Reviewer batch failed: {e}")
            return None
        rankings = _parse_reviewer_json(content, expected_ids)
        if rankings is None:
            logger.warning(f"Unparseable Reviewer output: {content[:300]}")
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
                content2 = self._call_reviewer(prompt, extra_system=stricter)
            except Exception as e:
                logger.warning(f"Reviewer retry failed: {e} — keeping first-pass rankings")
                return rankings
            rankings2 = _parse_reviewer_json(content2, expected_ids)
            if rankings2 is not None and not self._is_collapsed(rankings2):
                logger.info("Reviewer retry succeeded — using retried rankings.")
                return rankings2
            logger.warning(
                "Reviewer retry still collapsed — keeping first-pass rankings. "
                "Consider switching to a stronger model (gpt-4o-mini, "
                "deepseek-chat, Qwen2.5-72B) if this persists."
            )
        return rankings

    def rerank(self, candidates: list[Paper], corpus: list[CorpusPaper]) -> list[Paper]:
        if not candidates:
            return []

        # Belt & suspenders: keyword pre-filter (retriever may already have done this)
        if self.keywords:
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
