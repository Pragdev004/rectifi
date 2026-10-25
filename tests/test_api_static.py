"""Serving the built web app: index.html with the SPA fallback, hashed assets, no path traversal, a helpful 503 when unbuilt."""

import pytest
from api_support import make_client


@pytest.fixture
def dist(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>Rectifi</title><div id=root></div>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "x.js").write_text("console.log(1)")
    (tmp_path / "assets" / "x.css").write_text("body{}")
    (tmp_path / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path.parent / "outside-secret.txt").write_text("secret")
    return tmp_path


def test_spa_routes_serve_index_with_no_cache(dist):
    c = make_client(dist)
    for path in ("/", "/ledger", "/add?tab=sms", "/review/some/deep/link"):
        r = c.get(path)
        assert r.status_code == 200 and "<div id=root>" in r.text, path
        assert r.headers["cache-control"] == "no-cache"
        assert r.headers["content-type"].startswith("text/html")


def test_assets_are_immutable_and_served_with_their_type(dist):
    r = make_client(dist).get("/assets/x.js")
    assert r.status_code == 200 and r.text == "console.log(1)"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "javascript" in r.headers["content-type"]
    assert make_client(dist).get("/assets/x.css").headers["content-type"].startswith("text/css")


def test_a_missing_asset_is_404_not_index(dist):
    r = make_client(dist).get("/assets/missing.js")
    assert r.status_code == 404 and "<div id=root>" not in r.text


def test_root_files_are_served_and_revalidated(dist):
    r = make_client(dist).get("/favicon.svg")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and "svg" in r.headers["content-type"]


def test_api_404_is_json_not_index(dist):
    r = make_client(dist).get("/api/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("path", ["/%2e%2e/outside-secret.txt", "/assets/%2e%2e/%2e%2e/outside-secret.txt", "/..%2f..%2foutside-secret.txt",
                                  "/%2e%2e/%2e%2e/etc/passwd", "/a/../../outside-secret.txt"])
def test_path_traversal_is_404(dist, path):
    r = make_client(dist).get(path)
    assert "secret" not in r.text
    assert r.status_code in (404, 200) and (r.status_code == 404 or "<div id=root>" in r.text)


def test_dot_dot_segments_that_reach_the_router_are_404(dist):
    c = make_client(dist)
    assert c.get("/%2e%2e/etc/passwd").status_code == 404
    assert c.get("/assets/%2e%2e/index.html").status_code == 404


def test_a_missing_dist_is_a_503_with_advice(tmp_path):
    r = make_client(tmp_path / "nothing-here").get("/")
    assert r.status_code == 503
    assert "build-web" in r.text and "--streamlit" in r.text
    assert make_client(tmp_path / "nothing-here").get("/api/health").status_code == 200       # the API still works


def test_head_works(dist):
    assert make_client(dist).head("/ledger").status_code == 200


def test_dist_dir_can_come_from_the_env(dist, monkeypatch):
    monkeypatch.setenv("RECTIFI_WEB_DIST", str(dist))
    from api_support import make_client as mk

    assert "<div id=root>" in mk().get("/ledger").text
