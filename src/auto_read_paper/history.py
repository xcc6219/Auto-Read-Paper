"""Rolling score history persisted as a JSON file in the repo.

Every paper we score is stored with its score, scored_at date, and sent_at date
(or null if not yet sent). On each run, unsent papers within the retention
window are merged with today's freshly-scored papers and the Top-N is selected
from the combined pool — so high-scoring papers that got outranked on their
own day eventually make it into an email.

Dedup key is the arXiv id WITHOUT the version suffix (e.g. "2508.14001").
This keeps old scores when a new version of the same paper is retrieved later.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from .protocol import Paper


def arxiv_root_id(paper: Paper) -> Optional[str]:
    """Strip version suffix from arXiv URLs.

    "https://arxiv.org/abs/2508.14001v3" -> "2508.14001"
    """
    if not paper.url:
        return None
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([\w.-]+?)(?:v\d+)?(?:\.pdf)?/?$", paper.url)
    return m.group(1) if m else None


def _paper_id(paper: Paper) -> str:
    return arxiv_root_id(paper) or paper.url or paper.title


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _paper_to_entry(paper: Paper, scored_at: str) -> dict:
    return {
        "id": _paper_id(paper),
        "source": paper.source,
        "title": paper.title,
        "authors": list(paper.authors or []),
        "abstract": paper.abstract,
        "url": paper.url,
        "pdf_url": paper.pdf_url,
        "full_text": paper.full_text,
        "affiliations": paper.affiliations,
        "score": paper.score,
        "scored_at": scored_at,
        "sent_at": None,
    }


def _entry_to_paper(entry: dict) -> Paper:
    return Paper(
        source=entry.get("source") or "arxiv",
        title=entry.get("title") or "",
        authors=list(entry.get("authors") or []),
        abstract=entry.get("abstract") or "",
        url=entry.get("url") or "",
        pdf_url=entry.get("pdf_url"),
        full_text=entry.get("full_text"),
        affiliations=entry.get("affiliations"),
        score=entry.get("score"),
    )


class ScoreHistory:
    def __init__(self, path: str, retention_days: int):
        self.path = Path(path)
        self.retention_days = int(retention_days)
        self.entries: list[dict] = []
        # Guards against duplicate sends: if today's rendered HTML hashes to the
        # same value as the last sent email, we skip the send. Catches cache
        # restore failures, concurrent runs, and mail provider replays.
        self.last_sent_email: dict = {}

    def load(self) -> None:
        if not self.path.exists():
            logger.info(f"No history file at {self.path}; starting fresh")
            self.entries = []
            self.last_sent_email = {}
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self.entries = list(data.get("papers", []))
            self.last_sent_email = dict(data.get("last_sent_email") or {})
            logger.info(
                f"Loaded {len(self.entries)} entries from {self.path}"
            )
        except Exception as e:
            logger.warning(f"Failed to load history {self.path}: {e}; starting fresh")
            self.entries = []
            self.last_sent_email = {}
            return

        # Heal legacy cache pollution: pre-fix runs may have persisted scores
        # on a 0-1 scale (LLM misread the 0-10 rubric) or as raw keyword-hit
        # counts (Reviewer-fallback). Both surface in the email as every
        # paper showing "Relevance 1.0". If EVERY scored entry sits in
        # [0, 1] (and we have >=2 to distinguish from "genuinely low"),
        # rescale x10 in place so the merged candidate pool mixes cleanly
        # with freshly-scored papers under the fixed rerankers.
        scored = [e for e in self.entries if isinstance(e.get("score"), (int, float))]
        if len(scored) >= 2 and all(float(e["score"]) <= 1.0 for e in scored):
            logger.warning(
                f"History heal: every one of {len(scored)} persisted scores "
                f"sits in [0, 1] — this is stale pollution from a pre-fix "
                f"run (you were seeing 'Relevance 1.0' everywhere). "
                f"Rescaling x10 in place."
            )
            for e in scored:
                e["score"] = round(float(e["score"]) * 10.0, 2)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(
                {"papers": self.entries, "last_sent_email": self.last_sent_email},
                f,
                ensure_ascii=False,
                indent=2,
            )
        logger.info(f"Saved {len(self.entries)} entries to {self.path}")

    def is_duplicate_of_last_send(self, content_hash: str) -> bool:
        """True iff the last recorded send has the same content hash (any date)."""
        return bool(self.last_sent_email) and self.last_sent_email.get("hash") == content_hash

    def record_sent_email(self, content_hash: str, today: str) -> None:
        self.last_sent_email = {"date": today, "hash": content_hash}

    def trim(self) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        ).strftime("%Y-%m-%d")
        kept = [e for e in self.entries if (e.get("scored_at") or "") >= cutoff]
        dropped = len(self.entries) - len(kept)
        if dropped:
            logger.info(
                f"Trimmed {dropped} entries older than {self.retention_days}d (cutoff {cutoff})"
            )
        self.entries = kept

    def existing_ids(self) -> set[str]:
        return {e["id"] for e in self.entries if e.get("id")}

    def filter_new_papers(self, papers: list[Paper]) -> list[Paper]:
        """Return only papers whose root-id is NOT already in history."""
        existing = self.existing_ids()
        new: list[Paper] = []
        skipped = 0
        for p in papers:
            if _paper_id(p) in existing:
                skipped += 1
                continue
            new.append(p)
        if skipped:
            logger.info(f"Dedup: {skipped} papers already scored in the last {self.retention_days}d")
        return new

    def unsent_papers(self) -> list[Paper]:
        """Reconstruct Paper objects from entries that were never sent."""
        return [_entry_to_paper(e) for e in self.entries if not e.get("sent_at")]

    def sent_papers(self) -> list[Paper]:
        """Previously-sent entries, usable as a fallback filler when the primary pool is empty."""
        return [_entry_to_paper(e) for e in self.entries if e.get("sent_at")]

    def record_newly_scored(self, papers: list[Paper], today: str) -> None:
        existing = self.existing_ids()
        added = 0
        for p in papers:
            pid = _paper_id(p)
            if pid in existing:
                continue
            self.entries.append(_paper_to_entry(p, scored_at=today))
            existing.add(pid)
            added += 1
        if added:
            logger.info(f"Added {added} newly-scored papers to history")

    def mark_sent(self, papers: list[Paper], today: str) -> None:
        ids_to_mark = {_paper_id(p) for p in papers}
        marked = 0
        for e in self.entries:
            if e.get("id") in ids_to_mark and not e.get("sent_at"):
                e["sent_at"] = today
                marked += 1
        if marked:
            logger.info(f"Marked {marked} papers as sent on {today}")
