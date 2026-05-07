"""Unit tests for executor-level relevance helpers added by the
keyword-precision fixes:
  - tightened keyword expansion (drops generic ML stopwords)
  - embedding-similarity terminal gate (final relevance check)

These run on stubbed LLM clients so they don't hit any network."""
from __future__ import annotations

from types import SimpleNamespace

from auto_read_paper.executor import (
    _cosine,
    _embedding_relevance_filter,
    _expand_keywords,
    _is_acceptable_expansion,
)
from auto_read_paper.protocol import Paper


# ---------- _is_acceptable_expansion ------------------------------------


def test_acceptable_expansion_accepts_multi_word_phrases():
    user = ["medical image analysis"]
    assert _is_acceptable_expansion("biomedical image analysis", user)
    assert _is_acceptable_expansion("computer aided diagnosis", user)


def test_acceptable_expansion_rejects_generic_single_words():
    user = ["medical image analysis"]
    for kw in ("training", "image", "model", "deep", "neural", "data"):
        assert not _is_acceptable_expansion(kw, user), kw


def test_acceptable_expansion_rejects_very_short_tokens():
    assert not _is_acceptable_expansion("ai", ["medical imaging"])
    assert not _is_acceptable_expansion("ml", ["medical imaging"])
    assert not _is_acceptable_expansion("a", ["medical imaging"])


def test_acceptable_expansion_accepts_acronym_at_threshold():
    # 4+ char acronyms not in the stopword list survive the single-word filter.
    assert _is_acceptable_expansion("fmri", ["medical imaging"])
    assert _is_acceptable_expansion("mri", ["medical imaging"]) is False  # 3 chars


def test_acceptable_expansion_rejects_substring_of_user_keyword():
    user = ["medical image analysis"]
    # "image analysis" is already inside the user keyword — adds no recall.
    assert not _is_acceptable_expansion("image analysis", user)


# ---------- _expand_keywords (with stubbed LLM) -------------------------


def _stub_llm(payload):
    """Build a SimpleNamespace masquerading as LLMClient for _expand_keywords.

    Only ``complete_json`` is actually called by the function under test.
    """
    return SimpleNamespace(
        complete_json=lambda *, system, user, expect: payload,
    )


def test_expand_keywords_filters_generic_words():
    # An LLM that drifts to generic terms — those should be filtered out.
    llm = _stub_llm([
        "medical imaging",       # ok (multi-word)
        "training",              # rejected (stopword)
        "biomedical image analysis",  # ok
        "deep learning",         # rejected (in stopwords as "deep" and the
                                 # whole phrase gets filtered as substring? no
                                 # — it's multi-word so phrase is accepted.
                                 # Adjust expectation below.)
    ])
    out = _expand_keywords(llm, ["medical image analysis"])
    # Multi-word phrases pass; single-word generic terms don't.
    assert "training" not in out
    assert "medical imaging" in out


def test_expand_keywords_caps_at_n():
    payload = [f"phrase number {i}" for i in range(20)]  # 20 candidate phrases
    llm = _stub_llm(payload)
    out = _expand_keywords(llm, ["seed keyword"], n=5)
    assert len(out) == 5


def test_expand_keywords_handles_unparseable_response():
    llm = _stub_llm(None)  # LLM returned no usable list
    assert _expand_keywords(llm, ["whatever"]) == []


def test_expand_keywords_empty_input():
    llm = _stub_llm(["foo"])  # should never be called
    assert _expand_keywords(llm, []) == []


# ---------- _cosine ------------------------------------------------------


def test_cosine_basic():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert abs(_cosine([1.0, 1.0], [1.0, 0.0]) - 1 / (2 ** 0.5)) < 1e-9


def test_cosine_zero_vector_returns_zero():
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert _cosine([], [1.0]) == 0.0


# ---------- _embedding_relevance_filter ---------------------------------


def _paper(title: str, abstract: str = "") -> Paper:
    return Paper(
        source="test",
        title=title,
        authors=["A"],
        abstract=abstract,
        url=f"http://example/{title}",
        pdf_url=None,
        full_text=None,
    )


def test_embedding_gate_skips_when_no_embedding_model():
    # When embedding_model is None the gate is a no-op.
    llm = SimpleNamespace(embedding_model=None, embed=lambda *_a, **_k: None)
    papers = [_paper("Foo"), _paper("Bar")]
    out = _embedding_relevance_filter(llm, papers, ["x"], threshold=0.5)
    assert out == papers


def test_embedding_gate_drops_below_threshold():
    # Stub: keyword vector points at [1, 0]; first paper aligned, second orthogonal.
    embeddings = [
        [1.0, 0.0],   # keywords
        [1.0, 0.0],   # paper 1: cosine 1.0 -> kept
        [0.0, 1.0],   # paper 2: cosine 0.0 -> dropped
    ]
    llm = SimpleNamespace(
        embedding_model="x",
        embed=lambda texts: embeddings,
    )
    papers = [_paper("aligned"), _paper("orthogonal")]
    out = _embedding_relevance_filter(llm, papers, ["k"], threshold=0.30)
    assert [p.title for p in out] == ["aligned"]


def test_embedding_gate_keeps_all_above_threshold():
    embeddings = [
        [1.0, 0.0],
        [1.0, 0.05],
        [0.95, 0.1],
    ]
    llm = SimpleNamespace(
        embedding_model="x",
        embed=lambda texts: embeddings,
    )
    papers = [_paper("a"), _paper("b")]
    out = _embedding_relevance_filter(llm, papers, ["k"], threshold=0.30)
    assert len(out) == 2


def test_embedding_gate_passthrough_when_embed_returns_none():
    # API failure: embed() returns None → gate must not drop anything.
    llm = SimpleNamespace(embedding_model="x", embed=lambda texts: None)
    papers = [_paper("a"), _paper("b")]
    out = _embedding_relevance_filter(llm, papers, ["k"], threshold=0.30)
    assert out == papers


def test_embedding_gate_passthrough_on_partial_response():
    # API returned fewer vectors than expected — be conservative, don't drop.
    embeddings = [[1.0, 0.0], [1.0, 0.0]]  # missing the second paper
    llm = SimpleNamespace(embedding_model="x", embed=lambda texts: embeddings)
    papers = [_paper("a"), _paper("b")]
    out = _embedding_relevance_filter(llm, papers, ["k"], threshold=0.30)
    assert out == papers
