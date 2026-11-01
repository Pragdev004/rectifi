"""The service worker (web/public/sw.js, copied to web/dist/sw.js) does one job: show a push notification and open Rectifi on a tap.

It must never intercept a request or keep a page, because a cached page could show old money figures. These are static checks on
the file, so they need no browser. The page registers the worker only after you tap "Turn on phone alerts" (checked in the web tests).
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = [ROOT / "web" / "public" / "sw.js", ROOT / "web" / "dist" / "sw.js"]


def code(path: Path) -> str:
    """The file without comments, so a sentence that explains 'no fetch listener' does not count as one."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


@pytest.fixture(params=FILES, ids=["public", "dist"])
def worker(request) -> str:
    if not request.param.is_file():
        pytest.fail(f"{request.param.relative_to(ROOT)} is missing (python scripts/dev.py build-web copies web/public into web/dist)")
    return code(request.param)


def test_it_handles_only_install_activate_push_and_notification_click(worker):
    events = set(re.findall(r"addEventListener\(\s*[\"'](\w+)[\"']", worker))
    assert events == {"install", "activate", "push", "notificationclick"}


def test_it_never_intercepts_a_request(worker):
    assert not re.search(r"[\"']fetch[\"']", worker)
    assert not re.search(r"\bonfetch\b", worker)
    assert "respondWith" not in worker
    assert not re.search(r"\bfetch\s*\(", worker)
    for name in ("sync", "periodicsync", "backgroundfetch", "message"):
        assert not re.search(rf"addEventListener\(\s*[\"']{name}", worker)


def test_it_never_writes_to_the_cache(worker):
    assert not re.search(r"caches\.open", worker)
    assert not re.search(r"\.(put|add|addAll)\s*\(", worker)
    assert "importScripts" not in worker
    assert not re.search(r"\b(localStorage|indexedDB|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b", worker)


def test_activate_deletes_every_cache_an_older_version_left(worker):
    activate = worker[worker.index('"activate"') if '"activate"' in worker else worker.index("'activate'"):]
    activate = activate[: activate.index("addEventListener")] if "addEventListener" in activate else activate
    assert "caches.keys()" in activate
    assert "caches.delete(" in activate
    assert "clients.claim()" in activate


def test_a_tap_only_ever_opens_a_page_of_rectifi(worker):
    assert "location.origin" in worker             # the address from a push is checked against our own origin
    assert "openWindow" in worker and "matchAll" in worker


def test_the_built_copy_is_the_same_file():
    public, built = FILES
    if not built.is_file():
        pytest.fail("web/dist/sw.js is missing: python scripts/dev.py build-web")
    assert built.read_bytes() == public.read_bytes()
