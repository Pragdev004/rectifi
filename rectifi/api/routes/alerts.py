"""Leak alerts: the inbox, caps, presets, delivery settings, phones (Web Push), merchant names and the Leaks report.

A thin wrapper over rectifi/alerts.py, rectifi/merchants.py and rectifi/webpush.py. The only model use is the optional
"Explain this" on one alert, and it is imported inside that route. A phone's push address (endpoint) and keys are accepted
here and never sent back."""

from __future__ import annotations

from starlette.routing import Route

from ... import alerts as core
from ... import merchants, tools, webpush
from ..errors import ApiError
from ..inputs import api, int_field, path_int, query, text_field
from .. import wire

PUBLIC_PREFS = ("push_on", "private_notifications", "quiet_start", "quiet_end", "max_per_hour", "max_per_day", "cooldown_hours",
                "digest_on", "digest_time", "monthly_income", "spend_plan", "snooze_until", "muted_kinds")


def _alert(alert_id: int) -> dict:
    try:
        return core.get_alert(alert_id)
    except tools.RectifiError:
        raise ApiError(404, "not_found", "That alert is gone.") from None


# ───────────────────────── inbox ─────────────────────────

@api()
def list_alerts(request):
    status = query(request, "status", "unread")
    raw = query(request, "limit", "100")
    if status not in ("unread", "all"):
        raise ApiError(422, "invalid", "Status must be 'unread' or 'all'.", "status")
    if not raw.isdigit():
        raise ApiError(422, "invalid", "The limit must be a whole number.", "limit")
    return {"items": [wire.alert(r) for r in core.list_alerts(status, int(raw))], "unread": core.unread_count()}


@api()
def read_alert(request):
    _alert(path_int(request, "alert_id"))
    return wire.alert(core.mark_read(path_int(request, "alert_id")))


@api()
def read_all(request):
    return {"marked": core.read_all()}


@api()
def mute_alert(request):
    _alert(path_int(request, "alert_id"))
    return core.mute_alert(path_int(request, "alert_id"))


@api(body=True)
def snooze_alert(request, data):
    _alert(path_int(request, "alert_id"))
    done = core.snooze_alert(path_int(request, "alert_id"), int_field(data, "hours"))
    return {"snoozed": done["snoozed"], "until": done["until"]}


@api()
def explain_alert(request):
    from ...agent.explain import explain          # loaded only now: the inbox works without any model code

    return explain(core.as_insight(_alert(path_int(request, "alert_id"))))


@api()
def today(request):
    ctx = core.build_context()
    return wire.today_view(core.today_view(ctx), core.income(ctx))


# ───────────────────────── caps and presets ─────────────────────────

@api()
def list_rules(request):
    return {"items": [wire.rule(r) for r in core.list_rules()]}


@api(body=True)
def create_rule(request, data):
    cap = text_field(data, "cap", max_len=32)
    return wire.rule(core.create_rule(text_field(data, "scope", max_len=20), text_field(data, "target", required=False, max_len=80) or "all",
                                      text_field(data, "period", max_len=10), cap))


@api(body=True)
def patch_rule(request, data):
    changes = {}
    if "cap" in data:
        changes["cap"] = text_field(data, "cap", max_len=32)
    if "muted" in data:
        changes["muted"] = data["muted"]
    if "period" in data:
        changes["period"] = text_field(data, "period", max_len=10)
    if not changes:
        raise ApiError(422, "invalid", "Nothing to change: send a cap, a period or muted.")
    return wire.rule(core.update_rule(path_int(request, "rule_id"), **changes))


@api()
def delete_rule(request):
    if not core.delete_rule(path_int(request, "rule_id")):
        raise ApiError(404, "not_found", "That cap is gone.")
    return {"deleted": True}


@api()
def presets(request):
    return {"items": core.presets(core.build_context())}


# ───────────────────────── delivery settings ─────────────────────────

def _prefs_out() -> dict:
    prefs = core.get_prefs()
    ctx = core.build_context()
    found = core.income(ctx)
    detected = core.income(ctx, ignore_manual=True)
    return {**{k: prefs[k] for k in PUBLIC_PREFS}, "detected_income": wire.money(detected["amount"]), "income_source": found["source"]}


@api()
def get_prefs(request):
    return _prefs_out()


@api(body=True)
def put_prefs(request, data):
    core.save_prefs(data)
    return _prefs_out()


# ───────────────────────── merchant names ─────────────────────────

@api()
def list_patterns(request):
    return {"items": merchants.list_patterns(), "groups": list(merchants.GROUPS)}


@api(body=True)
def add_pattern(request, data):
    return merchants.add_pattern(text_field(data, "pattern", max_len=80), text_field(data, "merchant", max_len=80), text_field(data, "group", max_len=20))


@api()
def delete_pattern(request):
    if not merchants.delete_pattern(path_int(request, "pattern_id")):
        raise ApiError(404, "not_found", "That merchant name is gone.")
    return {"deleted": True}


# ───────────────────────── phones ─────────────────────────

@api()
def push_key(request):
    try:
        return {"configured": True, "public_key": webpush.public_key_b64(), "fingerprint": webpush.key_fingerprint()}
    except tools.RectifiError:
        return {"configured": False, "public_key": None, "fingerprint": None}


@api()
def list_subscriptions(request):
    return {"items": core.list_subscriptions()}


@api(body=True)
def add_subscription(request, data):
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else data
    return core.add_subscription(text_field(data, "endpoint", max_len=1000), text_field(keys, "p256dh", max_len=200),
                                 text_field(keys, "auth", max_len=100), text_field(data, "label", required=False, max_len=60))


@api()
def delete_subscription(request):
    if not core.delete_subscription(path_int(request, "sub_id")):
        raise ApiError(404, "not_found", "That phone is already gone.")
    return {"deleted": True}


@api()
def push_test(request):
    done = core.send_test()
    if done["devices"] == 0:
        raise ApiError(422, "invalid", "No phone has alerts turned on yet.")
    return done


# ───────────────────────── leaks ─────────────────────────

@api()
def leaks(request):
    month = tools.parse_month(query(request, "month"))
    return wire.leak_report(core.leak_report(core.build_context(since=month), month))


routes = [
    Route("/alerts", list_alerts, methods=["GET"]),
    Route("/alerts/today", today, methods=["GET"]),
    Route("/alerts/read-all", read_all, methods=["POST"]),
    Route("/alerts/presets", presets, methods=["GET"]),
    Route("/alerts/prefs", get_prefs, methods=["GET"]),
    Route("/alerts/prefs", put_prefs, methods=["PUT"]),
    Route("/alerts/rules", list_rules, methods=["GET"]),
    Route("/alerts/rules", create_rule, methods=["POST"]),
    Route("/alerts/rules/{rule_id}", patch_rule, methods=["PATCH"]),
    Route("/alerts/rules/{rule_id}", delete_rule, methods=["DELETE"]),
    Route("/alerts/{alert_id}/read", read_alert, methods=["POST"]),
    Route("/alerts/{alert_id}/mute", mute_alert, methods=["POST"]),
    Route("/alerts/{alert_id}/snooze", snooze_alert, methods=["POST"]),
    Route("/alerts/{alert_id}/explain", explain_alert, methods=["POST"]),
    Route("/leaks", leaks, methods=["GET"]),
    Route("/merchants/patterns", list_patterns, methods=["GET"]),
    Route("/merchants/patterns", add_pattern, methods=["POST"]),
    Route("/merchants/patterns/{pattern_id}", delete_pattern, methods=["DELETE"]),
    Route("/push/key", push_key, methods=["GET"]),
    Route("/push/subscriptions", list_subscriptions, methods=["GET"]),
    Route("/push/subscriptions", add_subscription, methods=["POST"]),
    Route("/push/subscriptions/{sub_id}", delete_subscription, methods=["DELETE"]),
    Route("/push/test", push_test, methods=["POST"]),
]
