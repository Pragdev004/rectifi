"""The Insights page: the list, applying a suggested budget, and the optional "Explain this" (the only place a model is used)."""

from __future__ import annotations

from starlette.routing import Route

from ... import insights as core
from ... import tools
from ..errors import ApiError
from ..inputs import api, text_field
from .. import wire


def find(key: str):
    """The insight with this key right now. Insights are worked out fresh each time, so a key can disappear."""
    item = next((i for i in core.insights() if i.key == key), None)
    if item is None:
        raise ApiError(404, "not_found", "That suggestion has changed since you opened the page. Refresh to see the current list.")
    return item


@api()
def list_insights(request):
    return {"span": core.data_span(), "items": [wire.insight(i) for i in core.insights()]}


@api(body=True)
def apply_insight(request, data):
    """The browser sends only the key. The category, month and limit come from the insight as Rectifi works it out now."""
    item = find(text_field(data, "key", max_len=120))
    if not item.apply or item.apply.get("tool") != "set_budget":
        raise ApiError(422, "invalid", "That suggestion can't be applied with one click.")
    done = tools.set_budget(item.apply["category"], item.apply["limit"], item.apply["month"])
    return {"category": done["category"], "month": wire.month_text(done["month"]), "limit": done["limit"]}


@api(body=True)
def explain_insight(request, data):
    from ...agent.explain import explain          # loaded only now: the Insights list works without any model code

    item = find(text_field(data, "key", max_len=120))
    return explain(item)


routes = [
    Route("/insights", list_insights, methods=["GET"]),
    Route("/insights/apply", apply_insight, methods=["POST"]),
    Route("/insights/explain", explain_insight, methods=["POST"]),
]
