"""Small in-memory stores for the import flow. One uvicorn worker, so a lock is all the coordination they need.

PendingUploads holds a parsed statement TABLE (never the file bytes, never a PDF password) for up to 15 minutes, so the
person can fix the columns and then import. RecentImports remembers which file contents were just imported, so an
accidental second upload is answered "already imported" without writing anything (the database dedupe is the real guard)."""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict

from .errors import ApiError

PENDING_TTL = 15 * 60
PENDING_MAX = 5
RECENT_MAX = 100


class PendingUploads:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._items: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self) -> None:
        cutoff = self.clock() - PENDING_TTL
        for key in [k for k, (stamp, _) in self._items.items() if stamp <= cutoff]:
            del self._items[key]

    def put(self, payload: dict) -> str:
        with self._lock:
            self._purge()
            while len(self._items) >= PENDING_MAX:
                self._items.popitem(last=False)
            upload_id = secrets.token_urlsafe(16)
            self._items[upload_id] = (self.clock(), payload)
            return upload_id

    def get(self, upload_id: str) -> dict:
        with self._lock:
            self._purge()
            found = self._items.get(upload_id)
        if found is None:
            raise ApiError(409, "upload_expired", "That upload has expired. Choose the file again.")
        return found[1]

    def drop(self, upload_id: str) -> None:
        with self._lock:
            self._items.pop(upload_id, None)

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class RecentImports:
    def __init__(self) -> None:
        self._items: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, digest: str) -> dict | None:
        with self._lock:
            return self._items.get(digest)

    def put(self, digest: str, entry: dict) -> None:
        with self._lock:
            self._items[digest] = entry
            self._items.move_to_end(digest)
            while len(self._items) > RECENT_MAX:
                self._items.popitem(last=False)

    def forget(self, digest: str) -> None:
        with self._lock:
            self._items.pop(digest, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


pending = PendingUploads()
recent = RecentImports()


def reset() -> None:
    """For tests: forget everything and use the real clock again."""
    pending.clear()
    pending.clock = time.monotonic
    recent.clear()
