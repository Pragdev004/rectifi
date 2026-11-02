"""Who is asking? One place that knows the current person, for every database call and every thread.

Laptop mode (RECTIFI_MULTIUSER unset): nothing bound means user 1, with no keys, as before.
Multi-user mode: nothing bound raises NoUser, so a forgotten binding fails closed instead of reading someone's data.

Only standard library here: db.py, vault.py and the web layer all import it.
"""

from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator

from . import config

OWNER_UID = 1          # the person who runs the server; in laptop mode the only person


class NoUser(RuntimeError):
    """Multi-user mode, and no person is bound to this request or job. Bind one with userctx.bound(...)."""


@dataclass(frozen=True)
class UserContext:
    uid: int
    key: bytes | None = None           # the person's data key, only while they are signed in
    public_key: bytes | None = None    # X25519 public key: lets a signed-out job seal text only the person can open later
    ingest_salt: bytes | None = None   # for keyed hashes (dedupe) that must work while signed out
    strict: bool = True                # True: a sealed value with no key raises VaultLocked. False: it reads as None
    private_key: bytes | None = None   # X25519 private key, opened with the data key at sign-in (reads "rs1." values)


LAPTOP = UserContext(uid=OWNER_UID, strict=False)

_current: contextvars.ContextVar[UserContext | None] = contextvars.ContextVar("rectifi_user", default=None)


def current() -> UserContext:
    ctx = _current.get()
    if ctx is not None:
        return ctx
    if config.multiuser():
        raise NoUser("No person is signed in for this request. Bind one with userctx.bound() before touching the database.")
    return LAPTOP


def is_bound() -> bool:
    return _current.get() is not None


@contextmanager
def bound(ctx: UserContext) -> Iterator[UserContext]:
    """Make `ctx` the current person for this block (and for threads started with spawn). Always resets afterwards."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def spawn(target: Callable, *args, name: str, daemon: bool = True, start: bool = True, **kwargs) -> threading.Thread:
    """Start a thread that keeps the current person. The only way to start a thread under rectifi/: a bare
    threading.Thread would lose the binding (a test checks)."""
    context = contextvars.copy_context()
    thread = threading.Thread(target=context.run, args=(target, *args), kwargs=kwargs, name=name, daemon=daemon)
    if start:
        thread.start()
    return thread
