"""Tests for ArxivRetriever."""

import sys
import time
from types import SimpleNamespace

import feedparser
import pytest

from auto_read_paper.retriever.arxiv_retriever import ArxivRetriever, _run_with_hard_timeout
import auto_read_paper.retriever.arxiv_retriever as arxiv_retriever


# `_run_with_hard_timeout` uses multiprocessing.Process. On Linux the default
# start method is `fork` (near-instant), but Windows only supports `spawn`
# which adds ~1s of Python interpreter startup per subprocess. Tests below
# assume the fork-speed contract, so short-timeout assertions race against
# spawn overhead. CI runs on Linux where this is not an issue.
_skip_on_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="multiprocessing spawn startup exceeds test timeout on Windows; Linux CI covers it",
)


def _sleep_and_return(value: str, delay_seconds: float) -> str:
    time.sleep(delay_seconds)
    return value


def _raise_runtime_error() -> None:
    raise RuntimeError("boom")


def test_arxiv_retriever(config, mock_feedparser, monkeypatch):
    monkeypatch.setattr("auto_read_paper.retriever.base.sleep", lambda _: None)
    # Inter-batch sleep in _retrieve_raw_papers; harmless to skip in tests.
    monkeypatch.setattr(arxiv_retriever.time, "sleep", lambda _s: None)

    # The RSS fixture gives us paper IDs. _retrieve_raw_papers then calls
    # _fetch_papers_and_affiliations per 20-id batch (single Atom request that
    # yields both paper details and affiliations). Stub that method so the
    # test stays offline; the FakeClient stub on arxiv.Client is no longer
    # needed because the main path doesn't go through arxiv-py anymore.
    new_entries = [
        e for e in mock_feedparser.entries
        if e.get("arxiv_announce_type", "new") == "new"
    ]

    fake_results = []
    fake_results_by_id: dict[str, SimpleNamespace] = {}
    for entry in new_entries:
        pid = entry.id.removeprefix("oai:arXiv.org:")
        result = SimpleNamespace(
            title=entry.title,
            authors=[SimpleNamespace(name="Test Author")],
            summary="Test abstract",
            pdf_url=f"https://arxiv.org/pdf/{pid}",
            entry_id=f"https://arxiv.org/abs/{pid}",
            source_url=lambda pid=pid: f"https://arxiv.org/e-print/{pid}",
        )
        fake_results.append(result)
        fake_results_by_id[ArxivRetriever._normalize_paper_id(pid)] = result

    def fake_fetch(self, batch_ids):
        wanted = {ArxivRetriever._normalize_paper_id(b) for b in batch_ids}
        return [fake_results_by_id[p] for p in wanted if p in fake_results_by_id], {}

    monkeypatch.setattr(ArxivRetriever, "_fetch_papers_and_affiliations", fake_fetch)

    # Skip file downloads in convert_to_paper
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_html", lambda paper: None)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_pdf", lambda paper: None)
    monkeypatch.setattr(arxiv_retriever, "extract_text_from_tar", lambda paper: None)

    retriever = ArxivRetriever(config)
    papers = retriever.retrieve_papers()

    assert len(papers) == len(new_entries)
    assert set(p.title for p in papers) == set(e.title for e in new_entries)


def test_rss_fetch_raises_retriever_fetch_error_after_retries(config, monkeypatch):
    """A persistent RSS network failure should retry, then raise
    RetrieverFetchError (so the executor can degrade to the history pool) —
    not a bare Exception, and not after a single attempt."""
    import requests

    from auto_read_paper.retriever.base import RetrieverFetchError

    # No real backoff sleeping between retries.
    monkeypatch.setattr(arxiv_retriever.time, "sleep", lambda _s: None)

    calls = {"n": 0}

    def always_timeout(url, **kwargs):
        calls["n"] += 1
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(requests, "get", always_timeout)

    retriever = ArxivRetriever(config)
    with pytest.raises(RetrieverFetchError) as excinfo:
        retriever._retrieve_raw_papers()

    assert "Failed to fetch arXiv RSS feed" in str(excinfo.value)
    assert calls["n"] == 4  # retried up to max_attempts, not just once


@_skip_on_windows
def test_run_with_hard_timeout_returns_value():
    result = _run_with_hard_timeout(
        _sleep_and_return, ("done", 0.01), timeout=1, operation="test op", paper_title="paper"
    )
    assert result == "done"


@_skip_on_windows
def test_run_with_hard_timeout_returns_none_on_timeout(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(arxiv_retriever, "logger", SimpleNamespace(warning=warnings.append))
    result = _run_with_hard_timeout(
        _sleep_and_return, ("done", 1.0), timeout=0.01, operation="test op", paper_title="paper"
    )
    assert result is None
    assert "timed out" in warnings[0]


@_skip_on_windows
def test_run_with_hard_timeout_returns_none_on_failure(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(arxiv_retriever, "logger", SimpleNamespace(warning=warnings.append))
    result = _run_with_hard_timeout(
        _raise_runtime_error, (), timeout=1, operation="test op", paper_title="paper"
    )
    assert result is None
    assert "boom" in warnings[0]
