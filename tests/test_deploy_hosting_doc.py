"""WS-DEPLOY: docs/HOSTING.md has the multi-user runbook, the gateway and Funnel steps, and the right database login."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "docs" / "HOSTING.md").read_text(encoding="utf-8")


def test_the_invite_runbook_steps_are_there_in_order():
    needles = ["migrate-tenancy", "invite --owner", "VACUUM FULL", "delete every backup older", "dev.py invite`", "dev.py invites", "revoke-invite"]
    positions = [TEXT.index(n) for n in needles]
    assert positions == sorted(positions), "the steps must come in this order"
    section = TEXT[TEXT.index("## Invite people"):]
    assert "you run this yourself" in section and "type `yes`" in section
    assert "There is no password reset" in section


def test_back_up_before_migrating():
    section = TEXT[TEXT.index("## Invite people"):]
    assert section.index("Back up first") < section.index("migrate-tenancy")


def test_gateway_and_funnel_steps_mark_what_is_not_verified():
    assert "tailscale funnel --bg 8501" in TEXT
    assert "tailscale funnel --bg --https=8443 8765" in TEXT
    after = TEXT[TEXT.index("**Not verified:**"):][:900]
    assert "free on the Personal plan" in after and "8443" in after and "Host header" in after and "free tunnel" in after
    assert "--profile gateway" in TEXT


def test_backups_run_as_postgres_and_the_privacy_note_says_seven_days():
    assert "run as `postgres`" in TEXT
    assert "keeps **7 days**" in TEXT and "deletes their account" in TEXT and "up to 7 days" in TEXT
    for line in TEXT.splitlines():
        if re.search(r"\b(pg_dump|pg_restore|psql|createdb|dropdb)\b", line) and "-U" in line and "old-volume" not in line and "rectifi_dev" not in line:
            assert "-U postgres" in line, line


def test_the_old_volume_path_is_explained():
    for needle in ("down -v", "old-volume.dump", "RECTIFI_DB_PASSWORD", "refuses to start"):
        assert needle in TEXT, needle


def test_no_port_is_published_on_every_interface_in_the_guide():
    assert "0.0.0.0:" not in TEXT
    assert "Never publish those ports on `0.0.0.0`" in TEXT
