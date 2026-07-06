"""Durable, file-based outbox — the SDK's own write-ahead mirror.

Mirrors the collector's own design (NFR-2: write-ahead to object storage
before ack): every event is durably written to local disk BEFORE the SDK
attempts to send it, so a process crash or a network blip never loses data
between "the customer's code called record_decision()" and "the collector
acknowledged it". One file per pending event (not a single append-only log)
so a delivered event is removed by simply deleting its file — no log
compaction needed.

Idempotency (FR-1.2) is what makes retries safe: every event carries its own
event_id, so re-sending an event the collector already accepted is a no-op
on the server side, not a duplicate.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class Outbox:
    """One JSON file per pending event, named `<event_id>.json`. `attempts`
    and `next_attempt_at` are stored inside the file so retry state survives
    a process restart too."""

    def __init__(self, directory: str | os.PathLike):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def put(self, event_id: str, event: dict) -> None:
        record = {"event": event, "attempts": 0, "next_attempt_at": 0.0}
        self._write(event_id, record)

    def pending(self) -> list[str]:
        """event_ids of every file currently in the outbox, oldest first —
        crash-recovered entries from a previous process included."""
        files = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        return [f.stem for f in files]

    def get(self, event_id: str) -> dict | None:
        path = self._path(event_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            # A half-written file from a crash mid-write (see _write's
            # atomic rename below, which makes this exceedingly unlikely,
            # but corrupt-on-disk data should never crash the caller).
            return None

    def mark_attempt(self, event_id: str, *, next_attempt_at: float) -> None:
        record = self.get(event_id)
        if record is None:
            return
        record["attempts"] += 1
        record["next_attempt_at"] = next_attempt_at
        self._write(event_id, record)

    def remove(self, event_id: str) -> None:
        self._path(event_id).unlink(missing_ok=True)

    def __len__(self) -> int:
        return len(list(self.directory.glob("*.json")))

    def _path(self, event_id: str) -> Path:
        return self.directory / f"{event_id}.json"

    def _write(self, event_id: str, record: dict) -> None:
        # Write to a temp file then rename — rename is atomic on POSIX, so a
        # crash mid-write never leaves a half-written outbox entry behind.
        fd, tmp_path = tempfile.mkstemp(dir=self.directory, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(record, f)
            os.replace(tmp_path, self._path(event_id))
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise


def backoff_seconds(attempts: int, *, base: float = 1.0, cap: float = 300.0) -> float:
    """Exponential backoff, capped at 5 minutes — capped, not abandoned:
    ponytail says most systems give up after N tries, but "high accuracy in
    tracking" means an event that's still undelivered after an hour is worth
    more retries, not a dead letter. The outbox is unbounded in time; only
    the retry *interval* is capped."""
    return min(base * (2 ** attempts), cap)


def now() -> float:
    return time.time()
