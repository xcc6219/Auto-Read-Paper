"""Regression tests for the ``UnicodeEncodeError: surrogates not allowed``
crash that brought down a real production run when an LLM emitted a lone
surrogate inside a generated tldr / title_zh / affiliation string.

Also covers the atomic-write path so a crash mid-dump can't truncate
the persisted history file.
"""
from __future__ import annotations

import json

import pytest

from auto_read_paper.history import ScoreHistory, _strip_surrogates


def test_strip_surrogates_removes_lone_surrogates():
    # Half of an emoji surrogate pair, the kind small LLMs occasionally emit.
    poisoned = "before \ud83d after"
    assert _strip_surrogates(poisoned) == "before  after"


def test_strip_surrogates_recurses_into_nested_structures():
    payload = {
        "title": "ok",
        "affs": ["lab \ud83d 1", "lab 2"],
        "meta": {"note": "tldr \udc00 oops"},
    }
    cleaned = _strip_surrogates(payload)
    # Encoding to UTF-8 must not raise — that's the whole point.
    json.dumps(cleaned).encode("utf-8")
    assert "\ud83d" not in cleaned["affs"][0]
    assert "\udc00" not in cleaned["meta"]["note"]


def test_strip_surrogates_passes_through_clean_unicode():
    # Real BMP / emoji code points (paired) survive intact.
    s = "医学图像 😀 analysis"
    assert _strip_surrogates(s) == s


def test_history_save_with_surrogate_does_not_crash(tmp_path):
    """The bug: a paper with a lone surrogate in the tldr made json.dump
    raise UnicodeEncodeError, which bubbled out of save() and killed the
    workflow before the email could be sent."""
    h = ScoreHistory(path=str(tmp_path / "history.json"), retention_days=7)
    h.entries = [
        {
            "id": "2605.04228",
            "source": "arxiv",
            "title": "Some paper",
            "abstract": "abstract",
            "tldr": "[CORE] hello \ud83d world [INNOVATION] x [VALUE] y",
            "title_zh": "好 \udc00 标题",
            "score": 7.5,
            "scored_at": "2026-05-08",
            "sent_at": None,
        }
    ]
    # Must not raise.
    h.save()
    # File must exist and load back as valid UTF-8 JSON.
    with open(h.path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["papers"][0]["id"] == "2605.04228"
    assert "\ud83d" not in data["papers"][0]["tldr"]
    assert "\udc00" not in data["papers"][0]["title_zh"]


def test_history_save_is_atomic(tmp_path):
    """Even if ``json.dump`` crashes mid-write, the existing history file
    must remain intact so the next run's ``load()`` still works."""
    target = tmp_path / "history.json"
    # Pre-populate with a known-good prior save.
    h = ScoreHistory(path=str(target), retention_days=7)
    h.entries = [{"id": "old", "title": "ok", "score": 1.0}]
    h.save()
    prior_bytes = target.read_bytes()

    # Now poison the entries with something that would raise inside
    # json.dump — _strip_surrogates handles real surrogates, so we use a
    # non-serializable object instead. This is the atomicity probe: dump
    # crashes, but the original file should still be readable.
    h.entries = [{"id": "boom", "value": object()}]
    with pytest.raises(TypeError):
        h.save()

    # Original file is intact and parses fine.
    after_bytes = target.read_bytes()
    assert after_bytes == prior_bytes
    with open(target, encoding="utf-8") as f:
        data = json.load(f)
    assert data["papers"][0]["id"] == "old"
