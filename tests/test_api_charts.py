"""The charts API with a scripted model: status, a streamed draw, the review gate over HTTP, retry, gallery, pin, run again, delete, and the guards."""

import json

import pytest
from api_support import make_client
from charts_support import CRASHES, GOOD, INVALID, reply, seed_spending
from support.fakes import ScriptedModel

from rectifi import tools
from rectifi.api.routes import charts as charts_routes
from rectifi.charts import graph as chart_graph
from rectifi.charts import sandbox


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    from rectifi import db

    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(sandbox, "CACHE_DIR", tmp_path / "mpl")
    monkeypatch.setattr(chart_graph, "RETRY_INTERVAL", 0)
    db.init_db()
    seed_spending()
    charts_routes.reset()
    yield
    charts_routes.reset()


@pytest.fixture
def client():
    return make_client()


def model(monkeypatch, *codes):
    scripted = ScriptedModel(script=[reply(c) for c in codes])
    monkeypatch.setattr("rectifi.agent.llm.get_llm", lambda role="chat": scripted)
    return scripted


def lines(response):
    return [json.loads(x) for x in response.text.splitlines() if x.strip()]


def draw(client, **body):
    return client.post("/api/charts/draw", json={"request": "monthly food spend as a bar chart", **body})


def test_status_on_and_off(client, monkeypatch):
    body = client.get("/api/charts/status").json()
    assert body["ok"] is True and body["plotly"] is False
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    body = client.get("/api/charts/status").json()
    assert body["ok"] is False and "DEEPSEEK_API_KEY" in body["why"]


def test_a_draw_streams_progress_then_the_finished_chart(client, monkeypatch):
    model(monkeypatch, INVALID, GOOD)
    r = draw(client)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = lines(r)
    kinds = [(e.get("step"), e.get("verdict")) for e in events if e["type"] == "progress"]
    assert ("write", "working") in kinds and ("validate", "VALIDATION FAILED") in kinds and ("check", "PASSED") in kinds
    done = events[-1]
    assert done["type"] == "done" and done["chart"]["attempts"] == 2
    chart = done["chart"]
    assert chart["image_light"].startswith("iVBOR") and chart["image_dark"] != chart["image_light"]    # base64 of a PNG
    assert "plt.subplots" in chart["code"] and chart["pinned"] is False
    assert {c["name"] for c in chart["checks"]} >= {"has_data", "labelled", "figures_match"} and all(c["ok"] for c in chart["checks"])
    assert chart["entries"] > 0 and "Food & Dining spend by month" in chart["alt"]
    assert [t["step"] for t in chart["trace"]][0] == "write"


def test_giving_up_is_a_failed_event_not_a_server_error(client, monkeypatch):
    model(monkeypatch, INVALID, INVALID, INVALID)
    events = lines(draw(client))
    assert events[-1]["type"] == "failed" and "3 tries" in events[-1]["message"] and events[-1]["trace"]
    assert client.get("/api/charts").json() == {"items": []}


def test_the_review_gate_over_http(client, monkeypatch):
    model(monkeypatch, GOOD)
    events = lines(draw(client, review=True))
    pause = events[-1]
    assert pause["type"] == "interrupt" and "plt.subplots" in pause["code"] and pause["thread_id"].startswith("chart-")
    done = lines(client.post(f"/api/charts/threads/{pause['thread_id']}/resume", json={"run": True}))
    assert done[-1]["type"] == "done"
    assert any(e.get("step") == "review" and e.get("verdict") == "APPROVED" for e in done if e["type"] == "progress")


def test_cancel_at_the_review_gate(client, monkeypatch):
    model(monkeypatch, GOOD)
    pause = lines(draw(client, review=True))[-1]
    out = lines(client.post(f"/api/charts/threads/{pause['thread_id']}/resume", json={"run": False}))
    assert out[-1]["type"] == "failed" and "cancelled" in out[-1]["message"]


def test_try_again_from_an_attempt(client, monkeypatch):
    scripted = model(monkeypatch, INVALID, INVALID, INVALID)
    failed = lines(draw(client))[-1]
    scripted.script.append(reply(GOOD))
    out = lines(client.post(f"/api/charts/threads/{failed['thread_id']}/retry", json={"attempt": 2, "feedback": "use bars"}))
    assert out[-1]["type"] == "done" and out[-1]["chart"]["attempts"] == 2
    assert client.post(f"/api/charts/threads/{failed['thread_id']}/retry", json={"attempt": 9}).status_code == 422


def test_gallery_detail_pin_rerun_delete(client, monkeypatch):
    scripted = model(monkeypatch, GOOD)
    chart_id = lines(draw(client))[-1]["chart"]["id"]
    listing = client.get("/api/charts").json()["items"]
    assert [c["id"] for c in listing] == [chart_id] and listing[0]["thumb"].startswith("iVBOR") and listing[0]["pinned"] is False
    assert client.get("/api/charts?pinned=1").json()["items"] == []
    assert client.post(f"/api/charts/{chart_id}/pin", json={"pinned": True}).json()["pinned"] is True
    assert len(client.get("/api/charts?pinned=1").json()["items"]) == 1
    seen = len(scripted.seen)
    again = client.post(f"/api/charts/{chart_id}/rerun")
    assert again.status_code == 200 and again.json()["refreshed_at"] is not None and len(scripted.seen) == seen      # no model call
    assert client.get(f"/api/charts/{chart_id}").json()["code"].startswith("import matplotlib")
    assert client.delete(f"/api/charts/{chart_id}").json() == {"deleted": True}
    assert client.get(f"/api/charts/{chart_id}").status_code == 422 or client.get(f"/api/charts/{chart_id}").status_code == 404
    assert client.delete(f"/api/charts/{chart_id}").status_code == 404


def test_rerun_works_with_the_model_off(client, monkeypatch):
    model(monkeypatch, GOOD)
    chart_id = lines(draw(client))[-1]["chart"]["id"]
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert client.get("/api/charts/status").json()["ok"] is False
    assert client.post(f"/api/charts/{chart_id}/rerun").status_code == 200
    assert draw(client).status_code == 503


def test_a_second_draw_while_one_runs_is_409(client, monkeypatch):
    model(monkeypatch, GOOD)
    assert charts_routes._busy.acquire(blocking=False)
    assert draw(client).status_code == 409
    charts_routes._busy.release()


@pytest.mark.parametrize("body,field", [({"request": ""}, "request"), ({"request": "x" * 600}, "request"), ({"request": "a", "period": "'; drop"}, "period"),
                                         ({"request": "a", "review": "yes"}, "review"), ({"request": "a", "period": "last-3-fortnights"}, None)])
def test_bad_input_is_422(client, monkeypatch, body, field):
    model(monkeypatch, GOOD)
    r = client.post("/api/charts/draw", json=body)
    assert r.status_code == 422
    if field:
        assert r.json()["error"]["field"] == field


def test_the_guards_still_apply(monkeypatch):
    bare = make_client(headers={})
    assert bare.get("/api/charts").status_code == 403                       # no X-Rectifi-Request header
    assert make_client().options("/api/charts/draw").status_code == 403     # never a CORS preflight answer
    assert "access-control-allow-origin" not in make_client().get("/api/charts").headers


def test_unknown_threads_and_charts(client):
    assert client.post("/api/charts/threads/not-a-thread/resume", json={"run": True}).status_code == 404
    assert client.get("/api/charts/999999").status_code in (404, 422)
    assert client.post("/api/charts/999999/pin", json={"pinned": True}).status_code in (404, 422)
    assert client.post("/api/charts/1/pin", json={"pinned": "yes"}).status_code == 422


def test_a_crash_in_the_chart_is_repaired_over_http(client, monkeypatch):
    model(monkeypatch, CRASHES, GOOD)
    events = lines(draw(client))
    assert any(e.get("step") == "run" and e.get("verdict") == "ERROR" for e in events if e["type"] == "progress")
    assert events[-1]["type"] == "done"
