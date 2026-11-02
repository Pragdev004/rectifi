"""The committed build in web/dist: it exists, matches the source, and stays small.

A stale build is the one risk of committing web/dist. If this fails after you changed web/src, run
`python scripts/dev.py build-web` (needs Node) and commit the result.
"""

import gzip
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "web" / "dist"
JS_BUDGET = 175 * 1024      # was 150, then 165: the Alerts and Leaks pages and the push helper added about 10 KB gzipped;
                            # the Signup, Recover, Settings and Privacy pages added about 6 KB gzipped
CSS_BUDGET = 25 * 1024


def load_dev():
    spec = importlib.util.spec_from_file_location("dev_script", ROOT / "scripts" / "dev.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def gz(path: Path) -> int:
    return len(gzip.compress(path.read_bytes(), compresslevel=9))


def test_the_build_exists():
    assert (DIST / "index.html").is_file(), "web/dist is not built: python scripts/dev.py build-web"
    assert (DIST / "theme-init.js").is_file() and (DIST / "manifest.webmanifest").is_file() and (DIST / "favicon.svg").is_file()
    assert (DIST / "icon-192.png").is_file() and (DIST / "icon-512.png").is_file()
    assert any((DIST / "licenses").glob("OFL-*.txt"))


def test_index_html_only_uses_files_that_exist_and_has_no_inline_code():
    html = (DIST / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="(/[^"]+)"', html)
    assert refs
    for ref in refs:
        assert (DIST / ref.lstrip("/")).is_file(), f"index.html points at {ref}, which is not in web/dist"
    assert re.search(r'<script type="module"[^>]+src="/assets/[^"]+\.js"', html)
    assert re.search(r'<link rel="stylesheet"[^>]+href="/assets/[^"]+\.css"', html)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html), "the page's security policy allows no inline script"
    assert "<style" not in html and ' style="' not in html


def test_the_css_points_only_at_files_that_exist():
    for css in (DIST / "assets").glob("*.css"):
        for ref in re.findall(r"url\(([^)]+)\)", css.read_text(encoding="utf-8")):
            ref = ref.strip("'\"")
            assert not ref.startswith("data:"), "the page's security policy allows no data: fonts"
            target = (DIST / ref.lstrip("/")) if ref.startswith("/") else (css.parent / ref)
            assert target.resolve().is_file(), f"{css.name} points at {ref}"


def test_build_info_matches_the_source():
    info = json.loads((DIST / "build-info.json").read_text(encoding="utf-8"))
    assert info["source_hash"] == load_dev().web_source_hash(), (
        "web/dist is out of date with web/src. Run `python scripts/dev.py build-web` and commit web/dist.")
    assert info["node"]


def test_javascript_and_css_stay_within_budget():
    js = sum(gz(p) for p in (DIST / "assets").glob("*.js"))
    css = sum(gz(p) for p in (DIST / "assets").glob("*.css"))
    assert 0 < js <= JS_BUDGET, f"JavaScript is {js / 1024:.1f} KB gzipped (budget 175 KB)"
    assert 0 < css <= CSS_BUDGET, f"CSS is {css / 1024:.1f} KB gzipped (budget 25 KB)"


def test_fonts_are_woff2_and_the_rupee_sign_is_covered():
    names = [p.name for p in (DIST / "assets").glob("*.woff2")]
    assert any(n.startswith("fraunces") for n in names) and any(n.startswith("ibm-plex-sans") for n in names) and any(n.startswith("ibm-plex-mono") for n in names)
    assert not list((DIST / "assets").glob("*.woff")), "woff2 is enough"


def test_fraunces_ships_the_optical_size_axis():
    pytest.importorskip("fontTools")
    pytest.importorskip("brotli")        # fontTools needs it to open woff2 files
    from fontTools.ttLib import TTFont

    files = sorted((DIST / "assets").glob("fraunces-latin-opsz-*.woff2"))
    assert files, "the Fraunces build with the optical size axis is missing"
    axes = {a.axisTag for a in TTFont(files[0])["fvar"].axes}
    assert {"opsz", "wght"} <= axes


def test_the_rupee_sign_is_in_the_plex_fonts():
    pytest.importorskip("fontTools")
    pytest.importorskip("brotli")        # fontTools needs it to open woff2 files
    from fontTools.ttLib import TTFont

    for pattern in ("ibm-plex-sans-latin-ext-400-*.woff2", "ibm-plex-mono-latin-ext-400-*.woff2"):
        font = TTFont(sorted((DIST / "assets").glob(pattern))[0])
        assert 0x20B9 in font.getBestCmap(), f"{pattern} has no rupee sign"
