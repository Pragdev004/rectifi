"""Small in-memory stores for the import flow. One uvicorn worker, so a lock is all the coordination they need.

PendingUploads holds a parsed statement TABLE (never the file bytes, never a PDF password) for up to 15 minutes, so the
person can fix the columns and then import. RecentImports remembers which file contents were just imported, so an
accidental second upload is answered "already imported" without writing anything (the database dedupe is the real guard)."""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict

from .. import userctx
from .errors import ApiError

PENDING_TTL = 15 * 60
PENDING_MAX = 5                 # per person: one person's uploads never push out another's
RECENT_MAX = 100


class PendingUploads:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self._items: OrderedDict[str, tuple[float, int, dict]] = OrderedDict()      # id -> (when, owner uid, payload)
        self._lock = threading.Lock()

    def _purge(self) -> None:
        cutoff = self.clock() - PENDING_TTL
        for key in [k for k, (stamp, _, _) in self._items.items() if stamp <= cutoff]:
            del self._items[key]

    def put(self, payload: dict) -> str:
        uid = userctx.current().uid
        with self._lock:
            self._purge()
            mine = [k for k, (_, owner, _) in self._items.items() if owner == uid]
            for key in mine[: max(0, len(mine) - PENDING_MAX + 1)]:
                del self._items[key]
            upload_id = secrets.token_urlsafe(16)
            self._items[upload_id] = (self.clock(), uid, payload)
            return upload_id

    def get(self, upload_id: str) -> dict:
        with self._lock:
            self._purge()
            found = self._items.get(upload_id)
        if found is None:
            raise ApiError(409, "upload_expired", "That upload has expired. Choose the file again.")
        if found[1] != userctx.current().uid:                      # someone else's upload: the same answer as no such upload
            raise ApiError(404, "not_found", "There is no such upload.")
        return found[2]

    def drop(self, upload_id: str) -> None:
        with self._lock:
            found = self._items.get(upload_id)
            if found is not None and found[1] == userctx.current().uid:
                del self._items[upload_id]

    def __len__(self) -> int:
        uid = userctx.current().uid
        with self._lock:
            self._purge()
            return sum(1 for _, owner, _ in self._items.values() if owner == uid)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class RecentImports:
    def __init__(self) -> None:
        self._items: OrderedDict[tuple[int, str], dict] = OrderedDict()
        self._lock = threading.Lock()

    # Keyed by (person, file digest): the same statement uploaded by two people is two separate things, and one person must
    # never be told about another's import.
    @staticmethod
    def _key(digest: str) -> tuple[int, str]:
        return userctx.current().uid, digest

    def get(self, digest: str) -> dict | None:
        with self._lock:
            return self._items.get(self._key(digest))

    def put(self, digest: str, entry: dict) -> None:
        key = self._key(digest)
        with self._lock:
            self._items[key] = entry
            self._items.move_to_end(key)
            while len(self._items) > RECENT_MAX:
                self._items.popitem(last=False)

    def forget(self, digest: str) -> None:
        with self._lock:
            self._items.pop(self._key(digest), None)

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
