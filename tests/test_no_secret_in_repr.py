"""Review round 2, item 7: no secret may show in a repr. `repr()` is what a log line, a traceback, a pytest failure and an f-string
with `!r` print. So every dataclass / NamedTuple / attrs class / pydantic model in `rectifi` that has a field with a secret-sounding
name must hide that field (`field(repr=False)`, `attr.ib(repr=False)`, `Field(repr=False)`), or define its own safe `__repr__`.

The test imports every rectifi module, so a new class is checked the day it is written. If it fails on a value that is really
public, add it to PUBLIC below WITH a reason. Never add a real secret there.
"""

import dataclasses
import importlib
import pkgutil

import pytest

import rectifi

# Field-name words that suggest a secret. A field matches when any "_"-separated word of its name is in this set.
SECRET_WORDS = {"key", "k", "secret", "token", "password", "passwd", "private", "salt", "digest", "endpoint", "cookie",
                "dek", "kek", "verifier", "credential", "credentials", "signature"}

# (module, class, field) that look secret but are public. Every entry says why.
PUBLIC = {
    ("rectifi.userctx", "UserContext", "public_key"): "the X25519 PUBLIC key: anyone may know it (it only seals)",
    ("rectifi.alerts", "Alert", "dedupe_key"): "an identifier of the alert (a keyed hash for a custom merchant, a brand name otherwise); the API returns it",
    ("rectifi.alerts", "Alert", "scope_key"): "an identifier of what a mute applies to; the API returns it",
    ("rectifi.insights", "Insight", "key"): "the insight's own id, a fixed rule name",
    ("rectifi.insights", "Recurring", "key"): "a merchant grouping key, shown to the person",
    ("rectifi.merchant_allowlist", "Brand", "key"): "a brand's id on the built-in allow-list",
    ("rectifi.rag.retrieve", "HybridRetriever", "k"): "top-k: how many chunks to return (an int)",
    ("rectifi.rag.retrieve", "HybridRetriever", "fetch_k"): "how many chunks to fetch before MMR (an int)",
}

SKIP_MODULES = {"_child", "_winjob", "__main__"}          # entry points of subprocesses and `python -m`, not libraries


def looks_secret(name: str) -> bool:
    return bool(set(name.lower().split("_")) & SECRET_WORDS)


def modules():
    for info in pkgutil.walk_packages(rectifi.__path__, "rectifi."):
        if info.name.rsplit(".", 1)[-1] in SKIP_MODULES:
            continue
        try:
            yield importlib.import_module(info.name)
        except ImportError:                                   # an optional dependency is missing on this machine
            continue


def classes():
    for mod in modules():
        for name, cls in vars(mod).items():
            if isinstance(cls, type) and cls.__module__ == mod.__name__:
                yield mod.__name__, name, cls


def own_repr(cls) -> bool:
    return "__repr__" in cls.__dict__


def hidden_state(cls):
    """[(field name, shown in repr?)] for a dataclass, NamedTuple, attrs class or pydantic model; None for any other class."""
    if dataclasses.is_dataclass(cls):
        return [(f.name, f.repr) for f in dataclasses.fields(cls)]
    if issubclass(cls, tuple) and hasattr(cls, "_fields"):                       # NamedTuple: it cannot hide a field
        return [(name, True) for name in cls._fields]
    if hasattr(cls, "__attrs_attrs__"):
        return [(a.name, a.repr) for a in cls.__attrs_attrs__]
    if hasattr(cls, "model_fields"):                                              # pydantic
        return [(name, info.repr) for name, info in cls.model_fields.items()]
    return None


def test_no_secret_field_shows_in_a_repr():
    problems, checked = [], set()
    for module, name, cls in classes():
        state = hidden_state(cls)
        if state is None:
            continue
        for field, shown in state:
            if not looks_secret(field) or (module, name, field) in PUBLIC:
                continue
            checked.add((module, name, field))
            if shown and not own_repr(cls):
                problems.append(f"{module}.{name}.{field} shows in repr: use field(repr=False) or write a safe __repr__")
    assert not problems, "\n".join(problems)
    # the scan must really reach the classes that carry keys, or the test proves nothing
    assert ("rectifi.users", "SignedIn", "k") in checked
    assert ("rectifi.userctx", "UserContext", "key") in checked
    assert ("rectifi.userctx", "UserContext", "private_key") in checked


def test_the_public_list_has_no_stale_entries():
    seen = {(m, n, f) for m, n, cls in classes() for f, _ in (hidden_state(cls) or [])}
    assert set(PUBLIC) <= seen, sorted(set(PUBLIC) - seen)


def test_a_detector_catches_a_leaky_class():
    @dataclasses.dataclass
    class Leaky:
        username: str
        token: bytes

    @dataclasses.dataclass
    class Safe:
        username: str
        token: bytes = dataclasses.field(repr=False)

    assert dict(hidden_state(Leaky))["token"] is True and dict(hidden_state(Safe))["token"] is False


def test_the_sign_in_result_and_the_context_never_print_their_secrets():
    from rectifi import userctx, users

    secret = b"\x01secret-cookie-bytes-0123456789ab"
    who = users.SignedIn(uid=2, username="alice", is_owner=False, sid="sid", k=secret, epoch=1,
                         ctx=userctx.UserContext(uid=2, key=b"datakey" * 5, ingest_salt=b"salt" * 8, private_key=b"priv" * 8))
    text = repr(who) + str(who)
    for needle in (repr(secret), secret.hex(), "datakey", "priv", "salt" * 2):
        assert needle not in text
    assert "alice" in text                                          # it still says who it is
