"""Shared helpers for the web API tests (not a test file)."""

from decimal import Decimal

from starlette.testclient import TestClient

from rectifi.api.app import create_app
from rectifi.money import PAISE

HEADERS = {"X-Rectifi-Request": "1"}
BASE = "http://127.0.0.1:8501"


def make_client(dist_dir=None, raise_server_exceptions=False, headers=HEADERS) -> TestClient:
    return TestClient(create_app(dist_dir), base_url=BASE, headers=headers, raise_server_exceptions=raise_server_exceptions)


def m(value) -> str:
    """What the API should say for a money value from tools.*: exactly two decimals."""
    d = Decimal(value).quantize(PAISE)
    return "0.00" if d == 0 else format(d, "f")


def p(value):
    """A percentage as the API says it: one decimal, or None."""
    if value is None:
        return None
    return format(Decimal(value).quantize(Decimal("0.1")), "f")
