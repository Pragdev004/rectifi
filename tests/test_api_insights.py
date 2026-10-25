"""The Insights API: the list, applying a suggestion (the server decides the limit), and Explain (the only model use)."""

from datetime import date
from decimal import Decimal

import pytest
from api_support import make_client
from langchain_core.messages import AIMessage

from rectifi import insights as ins
from rectifi import tools
from rectifi.agent import explain as explain_module

KEY = "unbudgeted:Food & Dining"


def first_of_month(back: int) -> date:
    return ins._shift_month(date.today().replace(day=1), back)


def with_history():
    tools.add_account("Main", "1234")
    for back in (1, 2, 3):
        tools.add_transaction("3000", "debit", first_of_month(back).replace(day=10), payee="Swiggy", category="Food & Dining", account="Main")


class FakeModel:
    def __init__(self, reply=None, boom=None):
        self.reply, self.boom, self.seen = reply, boom, []

    def invoke(self, messages):
        self.seen = messages
        if self.boom:
            raise self.boom
        return AIMessage(self.reply)


@pytest.fixture
def client():
    return make_client()


def test_empty_database_gives_an_empty_list(client):
    body = client.get("/api/insights").json()
    assert body["items"] == [] and "span" in body


def test_list_shape_and_money_as_strings(client):
    with_history()
    items = client.get("/api/insights").json()["items"]
    food = next(i for i in items if i["key"] == KEY)
    assert food["can_apply"] is True and food["is_data_quality"] is False
    assert food["numbers"]["suggested_budget"] == "3000.00" and food["numbers"]["prior_months"] == "3"
    assert set(food) >= {"key", "kind", "severity", "title", "detail", "numbers", "action_label", "action_page", "link"}
    assert all(isinstance(v, str) for v in food["numbers"].values())


def test_apply_uses_the_servers_limit_and_ignores_client_fields(client):
    with_history()
    r = client.post("/api/insights/apply", json={"key": KEY, "limit": "1", "category": "Travel", "month": "2020-01"})
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "Food & Dining" and body["limit"] == "3000.00" and body["month"] == date.today().strftime("%Y-%m")
    status = tools.budget_status(date.today())
    assert [(b["category"], b["limit"]) for b in status["categories"]] == [("Food & Dining", Decimal("3000.00"))]
    # now it has a budget, so the suggestion is gone and a second apply is a 404
    again = client.post("/api/insights/apply", json={"key": KEY})
    assert again.status_code == 404 and again.json()["error"]["code"] == "not_found"


def test_apply_a_stale_or_missing_key(client):
    assert client.post("/api/insights/apply", json={"key": "nope:nothing"}).status_code == 404
    assert client.post("/api/insights/apply", json={}).status_code == 422


def test_apply_refuses_an_insight_with_no_one_click_action(client):
    tools.add_account("Main", "1234")
    tools.add_transaction("100", "debit", date.today(), payee="Chai", category="Food & Dining")      # no account -> unlinked
    items = client.get("/api/insights").json()["items"]
    unlinked = next(i for i in items if i["kind"] == "unlinked")
    assert unlinked["can_apply"] is False and unlinked["is_data_quality"] is True
    assert unlinked["link"] == {"page": "ledger", "params": {"account": "none", "period": "all"}}
    r = client.post("/api/insights/apply", json={"key": unlinked["key"]})
    assert r.status_code == 422


def test_explain_returns_the_text_when_every_figure_is_the_insights(client, monkeypatch):
    with_history()
    model = FakeModel("Food averages ₹3,000 a month, so a ₹3,000 budget would hold you to your usual.")
    monkeypatch.setattr(explain_module, "get_llm", lambda: model)
    body = client.post("/api/insights/explain", json={"key": KEY}).json()
    assert body["text"].startswith("Food averages") and body["dropped"] == [] and body["error"] is None
    sent = "\n".join(str(m.content) for m in model.seen)
    assert "suggested_budget: ₹3,000" in sent


def test_explain_drops_a_made_up_figure(client, monkeypatch):
    with_history()
    monkeypatch.setattr(explain_module, "get_llm", lambda: FakeModel("Cut ₹750 a month from Food."))
    body = client.post("/api/insights/explain", json={"key": KEY}).json()
    assert body["text"] is None and body["dropped"] == ["750.00"] and body["error"] is None


def test_explain_error_is_a_200_with_a_plain_message(client, monkeypatch):
    with_history()
    monkeypatch.setattr(explain_module, "get_llm", lambda: FakeModel(boom=RuntimeError("down")))
    r = client.post("/api/insights/explain", json={"key": KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] is None and body["dropped"] == [] and body["error"]


def test_explain_for_a_stale_key_is_404(client):
    assert client.post("/api/insights/explain", json={"key": "gone:away"}).status_code == 404


def test_one_month_of_history_is_not_called_one_months(client):
    tools.add_account("Main", "1234")
    tools.add_transaction("3000", "debit", first_of_month(1).replace(day=10), payee="Swiggy", category="Food & Dining", account="Main")
    items = client.get("/api/insights").json()["items"]
    food = next((i for i in items if i["key"] == KEY), None)
    assert food is not None and "last 1 month with data" in food["detail"]
