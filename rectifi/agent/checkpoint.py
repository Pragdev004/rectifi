"""Conversation memory in Postgres for every graph: one connection pool shared by a PostgresSaver (short-term memory,
the messages of each thread) and a PostgresStore (long-term memory, the things you asked Rectifi to remember).

The old agent path (`graph.postgres_checkpointer`) used one connection, which breaks when several threads run graphs.
A pool does not. LangGraph's own tables (checkpoints*, store*) are created by `setup()` here, once per process.

    mem = get_memory()          # builds the pool and the tables on first use
    graph = build_graph(checkpointer=mem.saver)
    mem.store.put(("users", "owner", "memories"), key, {...})
    close_memory()              # at shutdown, and in tests after the tables are dropped
"""

from __future__ import annotations

import threading
from typing import NamedTuple

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from .. import config
from ..db import DatabaseUnavailable, describe_target

POOL_MAX = 6


class Memory(NamedTuple):
    saver: object      # langgraph.checkpoint.postgres.PostgresSaver
    store: object      # langgraph.store.postgres.PostgresStore
    pool: ConnectionPool


_lock = threading.Lock()
_memory: Memory | None = None
_memory_url: str | None = None


def get_memory() -> Memory:
    """The shared saver + store. Rebuilt if DATABASE_URL changed. Raises DatabaseUnavailable if Postgres is not there."""
    global _memory, _memory_url
    url = config.database_url()
    with _lock:
        if _memory is not None and _memory_url == url:
            return _memory
        _close_locked()
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.store.postgres import PostgresStore

        pool = ConnectionPool(
            url, min_size=1, max_size=POOL_MAX, open=False,
            # LangGraph needs autocommit, no prepared statements (a pool reuses sessions) and dict rows.
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row, "connect_timeout": 3},
        )
        try:
            pool.open(wait=True, timeout=5)
        except (PoolTimeout, Exception) as exc:      # psycopg.OperationalError and friends
            pool.close()
            raise DatabaseUnavailable(f"Can't reach the database at {describe_target(url)} for chat memory.") from exc
        saver = PostgresSaver(pool)
        store = PostgresStore(pool)
        saver.setup()
        store.setup()
        _memory, _memory_url = Memory(saver, store, pool), url
        return _memory


def checkpointer():
    return get_memory().saver


def store():
    return get_memory().store


def _close_locked() -> None:
    global _memory, _memory_url
    if _memory is not None:
        try:
            _memory.pool.close()
        except Exception:      # closing must never raise at shutdown
            pass
    _memory, _memory_url = None, None


def close_memory() -> None:
    with _lock:
        _close_locked()
