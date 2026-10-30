"""Shared helpers for the alert tests: a fake clock, a fake push service and a fake phone."""

import os
from datetime import date, datetime
from decimal import Decimal

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from rectifi import alerts, tools, webpush
from rectifi.db import connect

TODAY = date(2026, 10, 21)          # a Wednesday


class Clock:
    """Replaces alerts._clock. Starts at 12:00 on TODAY; call .at('23:10') or .plus(hours=2) to move it."""

    def __init__(self):
        self.now = datetime(2026, 10, 21, 12, 0).astimezone()

    def __call__(self):
        return self.now

    def at(self, hhmm, day=None):
        hour, minute = (int(x) for x in hhmm.split(":"))
        base = self.now if day is None else self.now.replace(day=day)
        self.now = base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def plus(self, **kw):
        from datetime import timedelta

        self.now = self.now + timedelta(**kw)


class FakePush:
    """Replaces webpush.send. `.answer` is what the 'push service' says; every call is recorded (without the endpoint)."""

    def __init__(self):
        self.answer = "ok"
        self.calls = []

    def __call__(self, sub, payload, ttl=3600, urgency="normal", client=None):
        import json

        self.calls.append({"payload": json.loads(payload), "raw": payload, "urgency": urgency, "ttl": ttl})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def add_phone(label="Pixel", n=1):
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return alerts.add_subscription(f"https://fcm.googleapis.com/fcm/send/device-{n}-{os.urandom(4).hex()}",
                                   webpush.b64u(public), webpush.b64u(os.urandom(16)), label)


def add(amount, when, payee, category=None, direction="debit"):
    return tools.add_transaction(str(amount), direction, when, payee=payee, category=category, account="Main", source="manual")["transaction"]


def rows(sql="SELECT * FROM alerts ORDER BY id"):
    with connect() as conn:
        return conn.execute(sql).fetchall()


def states():
    return {r["dedupe_key"]: r["push_state"] for r in rows()}
