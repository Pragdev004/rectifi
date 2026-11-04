"""Review round 1, MED: an abandoned owner claim plus a friend called "owner" must never break sign-in for everybody."""

import time

import pytest

from rectifi import db, users

PASSWORD = "correct horse battery staple"
OTHER = "another long password here"


@pytest.fixture(autouse=True)
def multiuser(monkeypatch):
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    monkeypatch.setenv("RECTIFI_SESSION_SECRET", "x" * 40)
    users.reset_limiters()


def abandon_owner_claim():
    users.signup(users.create_invite("owner")["code"], "prag", PASSWORD)           # step one only, never confirmed


def later(monkeypatch, minutes=31):
    moment = time.time() + minutes * 60
    monkeypatch.setattr(users, "_now", lambda: moment)


def test_a_friend_cannot_take_the_name_owner(monkeypatch):
    abandon_owner_claim()                                                          # row 1 is now called "prag": "owner" looks free
    for name in ("owner", "Owner", "OWNER"):
        with pytest.raises(users.AuthError) as err:
            users.signup(users.create_invite("friend")["code"], name, OTHER)
        assert err.value.code == "username_taken"


def test_purge_after_an_abandoned_claim_gives_row_one_its_name_back(monkeypatch):
    abandon_owner_claim()
    later(monkeypatch)
    users.purge_unconfirmed()
    with db.connect_system("tests: read row 1") as conn:
        row = conn.execute("SELECT username, verifier, confirmed_at FROM users WHERE id = 1").fetchone()
    assert row["username"] == "owner" and row["verifier"] is None and row["confirmed_at"] is None


def test_an_old_database_with_a_friend_called_owner_still_signs_people_in(monkeypatch):
    """The review's probe: a friend named "Owner" exists (made before the name was reserved) when the owner's pending claim expires.
    Before the fix every login, sign-up and confirm raised UniqueViolation. Now row 1 takes a placeholder name and all of them work."""
    abandon_owner_claim()
    friend_token, _ = users.signup(users.create_invite("friend")["code"], "Zed", OTHER)
    users.confirm_signup(friend_token)
    with db.connect_system("tests: simulate an old database") as conn:
        conn.execute("UPDATE users SET username = 'Owner' WHERE username = 'Zed'")      # what an unreserved name would have produced
    later(monkeypatch)
    users.purge_unconfirmed()                                                       # does not raise
    attempt = users.login("Owner", OTHER)
    assert attempt.status == "ok"
    with db.connect_system("tests: read row 1") as conn:
        row = conn.execute("SELECT username, verifier FROM users WHERE id = 1").fetchone()
    assert row["verifier"] is None and row["username"].startswith("owner-")
    # and a new owner invite can still be claimed
    users.signup(users.create_invite("owner")["code"], "prag", PASSWORD)


def test_one_bad_row_does_not_stop_the_purge(monkeypatch):
    abandon_owner_claim()
    friend_token, _ = users.signup(users.create_invite("friend")["code"], "Yan", OTHER)       # a second pending sign-up
    later(monkeypatch)
    real = users._reset_owner_claim
    monkeypatch.setattr(users, "_reset_owner_claim", lambda conn: (_ for _ in ()).throw(__import__("psycopg").errors.DataError("boom")))
    users.purge_unconfirmed()                                                       # no exception
    with db.connect_system("tests: count") as conn:
        assert conn.execute("SELECT count(*) AS n FROM users WHERE username = 'Yan'").fetchone()["n"] == 0    # the other row was still cleared
    monkeypatch.setattr(users, "_reset_owner_claim", real)
