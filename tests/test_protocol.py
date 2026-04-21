"""Tests for auto_read_paper.protocol: Paper.generate_tldr, Paper.generate_affiliations."""

import pytest

from tests.canned_responses import make_sample_paper, make_stub_llm_client


# ---------------------------------------------------------------------------
# generate_tldr
# ---------------------------------------------------------------------------


def test_tldr_returns_response():
    # When the LLM returns a fully-anchored TLDR, generate_tldr returns it as-is.
    canned = "[CORE] core sentence. [INNOVATION] innovation sentence. [VALUE] value sentence."
    llm = make_stub_llm_client(responses={"senior AI researcher": canned})
    paper = make_sample_paper()
    result = paper.generate_tldr(llm, "English")
    assert result is not None
    assert "[CORE]" in result and "[INNOVATION]" in result and "[VALUE]" in result
    assert paper.tldr == result


def test_tldr_without_abstract_or_fulltext():
    # No content to summarise → return None (caller should drop the paper).
    llm = make_stub_llm_client()
    paper = make_sample_paper(abstract="", full_text=None)
    result = paper.generate_tldr(llm, "English")
    assert result is None
    assert paper.tldr is None


def test_tldr_returns_none_on_persistent_error():
    # Every retry raises → caller sees None and drops the paper from the email.
    paper = make_sample_paper()
    broken = make_stub_llm_client(raises=RuntimeError("API down"))
    result = paper.generate_tldr(broken, "English")
    assert result is None
    assert paper.tldr is None


def test_tldr_truncates_long_prompt():
    canned = "[CORE] c. [INNOVATION] i. [VALUE] v."
    llm = make_stub_llm_client(responses={"senior AI researcher": canned})
    paper = make_sample_paper(full_text="word " * 10000)
    result = paper.generate_tldr(llm, "English")
    assert result is not None
    assert "[CORE]" in result


# ---------------------------------------------------------------------------
# generate_affiliations
# ---------------------------------------------------------------------------


def test_affiliations_returns_parsed_list():
    llm = make_stub_llm_client()
    paper = make_sample_paper()
    result = paper.generate_affiliations(llm)
    assert isinstance(result, list)
    assert "TsingHua University" in result
    assert "Peking University" in result


def test_affiliations_none_without_fulltext():
    llm = make_stub_llm_client()
    paper = make_sample_paper(full_text=None)
    result = paper.generate_affiliations(llm)
    assert result is None


def test_affiliations_deduplicates():
    llm = make_stub_llm_client()
    paper = make_sample_paper()
    result = paper.generate_affiliations(llm)
    assert len(result) == len(set(result))


def test_affiliations_malformed_llm_output():
    """LLM returns affiliations without JSON brackets — tolerant parser returns None."""
    llm = make_stub_llm_client(
        responses={"extracts affiliations": "TsingHua University, Peking University"},
    )
    paper = make_sample_paper()
    result = paper.generate_affiliations(llm)
    assert result is None


def test_affiliations_error_returns_none():
    broken = make_stub_llm_client(raises=RuntimeError("boom"))
    paper = make_sample_paper()
    result = paper.generate_affiliations(broken)
    assert result is None
    assert paper.affiliations is None
