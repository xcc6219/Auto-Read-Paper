"""Tests for the tri-state domain-relevance gate in ReaderReviewerReranker.

Covers:
  - _normalize_reader_notes tri-state parsing (yes/no/uncertain + legacy bool)
  - Reader yes/no verdicts gate correctly
  - Reader "uncertain" is adjudicated by Reviewer
  - Adjudicator failure drops uncertain conservatively
  - Adjudicator "keep" moves uncertain papers into kept
  - Adjudicator "drop" removes them
  - skip_keyword_filter rescue bypasses the gate
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from auto_read_paper.reranker.reader_reviewer import (
    ReaderReviewerReranker,
    _normalize_reader_notes,
)
from tests.canned_responses import make_sample_paper


# ---------------------------------------------------------------------------
# _normalize_reader_notes
# ---------------------------------------------------------------------------


def test_normalize_tri_state_strings():
    for s in ("yes", "YES", "true", "1", "relevant"):
        n = _normalize_reader_notes({"task": "t", "domain_relevant": s})
        assert n["domain_relevant"] == "yes", s
    for s in ("no", "false", "0", "irrelevant"):
        n = _normalize_reader_notes({"task": "t", "domain_relevant": s})
        assert n["domain_relevant"] == "no", s
    for s in ("uncertain", "maybe", "unsure", "ambiguous", "gibberish"):
        n = _normalize_reader_notes({"task": "t", "domain_relevant": s})
        assert n["domain_relevant"] == "uncertain", s


def test_normalize_legacy_bool_compat():
    assert _normalize_reader_notes({"domain_relevant": True})["domain_relevant"] == "yes"
    assert _normalize_reader_notes({"domain_relevant": False})["domain_relevant"] == "no"
    assert _normalize_reader_notes({"domain_relevant": 1})["domain_relevant"] == "yes"
    assert _normalize_reader_notes({"domain_relevant": 0})["domain_relevant"] == "no"


def test_normalize_missing_defaults_uncertain():
    n = _normalize_reader_notes({"task": "foo"})
    assert n["domain_relevant"] == "uncertain"


def test_normalize_non_dict_returns_none():
    assert _normalize_reader_notes("not a dict") is None
    assert _normalize_reader_notes(None) is None


# ---------------------------------------------------------------------------
# Integration: rerank() pipeline with routed stub LLM
# ---------------------------------------------------------------------------


class RoutedLLM:
    """Routes complete_json to per-paper Reader verdicts, a Reviewer batch
    ranking, or an Adjudicator batch verdict based on system prompt content.

    - reader_verdicts: dict[paper_title_substring -> domain_relevant str]
      (all get same dummy notes)
    - adjudicator_verdicts: dict[paper_title_substring -> bool]
    - adjudicator_raises: if True, adjudicator call raises
    """

    def __init__(
        self,
        reader_verdicts: dict[str, str],
        adjudicator_verdicts: dict[str, bool] | None = None,
        adjudicator_raises: bool = False,
    ):
        self.reader_verdicts = reader_verdicts
        self.adjudicator_verdicts = adjudicator_verdicts or {}
        self.adjudicator_raises = adjudicator_raises
        self.calls: list[str] = []  # track which agent was called

    def complete_json(self, *, system: str, user: str, expect: str = "object"):
        if "fast paper reader" in system:
            self.calls.append("reader")
            # Pick which title this user prompt describes
            rel = "yes"
            for marker, verdict in self.reader_verdicts.items():
                if marker in user:
                    rel = verdict
                    break
            return {
                "task": "t",
                "method": "m",
                "contributions": "c",
                "results": "r",
                "limitations": "l",
                "domain_relevant": rel,
                "relevance_reason": f"routed: {rel}",
            }
        if "adjudicator" in system:
            self.calls.append("adjudicator")
            if self.adjudicator_raises:
                raise RuntimeError("adjudicator API down")
            # Parse ids present in user prompt
            verdicts = []
            # Heuristic: for each `--- id: N ---` block, find Title: line and
            # match against adjudicator_verdicts markers.
            import re
            blocks = re.split(r"--- id: (\d+) ---", user)
            # blocks = [prefix, id1, block1, id2, block2, ...]
            for i in range(1, len(blocks), 2):
                pid = int(blocks[i])
                body = blocks[i + 1] if i + 1 < len(blocks) else ""
                rel = False
                for marker, decision in self.adjudicator_verdicts.items():
                    if marker in body:
                        rel = decision
                        break
                verdicts.append({"id": pid, "relevant": rel, "reason": "routed"})
            return {"verdicts": verdicts}
        if "senior research reviewer" in system:
            self.calls.append("reviewer")
            # Rank in input order, distinct scores so _is_collapsed is false.
            import re
            ids = [int(m) for m in re.findall(r"--- id: (\d+) ---", user)]
            rankings = [
                {"id": pid, "score": 9 - i, "reason": "ok"}
                for i, pid in enumerate(ids)
            ]
            return {"rankings": rankings}
        raise AssertionError(f"Unrouted prompt: {system[:60]}")

    def complete(self, **kwargs):  # pragma: no cover - not used here
        raise NotImplementedError

    def token_count(self, text: str) -> int:
        return max(1, len(text) // 4)

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        if not text or max_tokens <= 0:
            return ""
        return text[: max_tokens * 4]


def _make_reranker(config, llm) -> ReaderReviewerReranker:
    """Instantiate reranker, swap in routed stub LLM."""
    # Force the reader_reviewer section to exist with sane defaults.
    from omegaconf import OmegaConf
    config.executor.reranker = "reader_reviewer"
    if "reader_reviewer" not in config.reranker:
        config.reranker.reader_reviewer = OmegaConf.create({
            "threshold": 0.0,
            "concurrency": 1,
            "reader_max_input_tokens": 3000,
            "reviewer_max_papers": 60,
        })
    # Keywords are set to ["test"] by conftest; keep it.
    # Patch LLMClient.from_config so __init__ doesn't try to hit a real API.
    from auto_read_paper import llm_client as _llm_mod
    original = _llm_mod.LLMClient.from_config
    _llm_mod.LLMClient.from_config = staticmethod(lambda cfg: llm)
    try:
        rr = ReaderReviewerReranker(config)
    finally:
        _llm_mod.LLMClient.from_config = original
    rr.llm = llm  # belt & suspenders
    return rr


def _paper(title: str, abstract: str = "abstract test keyword") -> object:
    return make_sample_paper(
        title=title,
        abstract=abstract,
        url=f"https://arxiv.org/abs/{abs(hash(title)) % 10**8}",
        full_text="some body",
    )


def test_gate_drops_reader_no_keeps_reader_yes(config):
    llm = RoutedLLM(reader_verdicts={"KEEP-ME": "yes", "DROP-ME": "no"})
    rr = _make_reranker(config, llm)

    papers = [_paper("KEEP-ME paper about test"), _paper("DROP-ME paper about test")]
    out = rr.rerank(papers, [])
    titles = {p.title for p in out}
    assert "KEEP-ME paper about test" in titles
    assert "DROP-ME paper about test" not in titles
    assert "adjudicator" not in llm.calls  # no uncertain => no adjudicator


def test_gate_uncertain_adjudicator_keep(config):
    llm = RoutedLLM(
        reader_verdicts={"MAYBE-A": "uncertain", "MAYBE-B": "uncertain"},
        adjudicator_verdicts={"MAYBE-A": True, "MAYBE-B": False},
    )
    rr = _make_reranker(config, llm)

    papers = [_paper("MAYBE-A paper test"), _paper("MAYBE-B paper test")]
    out = rr.rerank(papers, [])
    titles = {p.title for p in out}
    assert "MAYBE-A paper test" in titles
    assert "MAYBE-B paper test" not in titles
    assert "adjudicator" in llm.calls


def test_gate_uncertain_adjudicator_failure_drops_all(config):
    llm = RoutedLLM(
        reader_verdicts={"UNCERT": "uncertain"},
        adjudicator_raises=True,
    )
    rr = _make_reranker(config, llm)

    papers = [_paper("UNCERT paper test")]
    out = rr.rerank(papers, [])
    # All papers were uncertain, adjudicator failed → gate returns empty early
    assert out == []


def test_gate_mixed_reader_plus_adjudicator(config):
    llm = RoutedLLM(
        reader_verdicts={
            "YES-P": "yes",
            "NO-P": "no",
            "MAYBE-P": "uncertain",
        },
        adjudicator_verdicts={"MAYBE-P": True},
    )
    rr = _make_reranker(config, llm)

    papers = [
        _paper("YES-P test"),
        _paper("NO-P test"),
        _paper("MAYBE-P test"),
    ]
    out = rr.rerank(papers, [])
    titles = {p.title for p in out}
    assert titles == {"YES-P test", "MAYBE-P test"}
    assert "adjudicator" in llm.calls
    assert "reviewer" in llm.calls


def test_skip_keyword_filter_still_runs_domain_gate(config):
    # skip_keyword_filter opts out of the coarse substring pre-filter only.
    # The semantic domain-relevance gate must STILL run on rescue candidates
    # so off-topic papers don't slip in through the back-catalog search.
    llm = RoutedLLM(reader_verdicts={"RESCUE": "no"})
    rr = _make_reranker(config, llm)

    papers = [_paper("RESCUE paper")]
    out = rr.rerank(papers, [], skip_keyword_filter=True)
    assert len(out) == 0
    # "no" is decisive — no adjudicator needed.
    assert "adjudicator" not in llm.calls
