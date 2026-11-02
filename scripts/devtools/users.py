"""dev.py commands for people and invites: migrate-tenancy, invite, invites and revoke-invite.
Standard library only; dev.py loads this file by path (so it works before anything is installed and never imports rectifi).
The real work runs in the project's Python, as a child process.

    migrate-tenancy              give every row of an older database to the owner (user 1), so it can hold more than one person
    invite [--days 7] [--owner]  make a one-time sign-up code. The code is printed ONCE; only its hash is stored.
                                 --owner: the code that sets the password on the owner (user 1, existing data stays)
    invites                      list invites and their state. Never a code (only hashes exist).
    revoke-invite <id>           cancel an invite that has not been used

There is no command to reset a password, and there never will be one: a forgotten password plus a lost recovery code
means the encrypted text is gone for good (multi-user plan, D2). Only the person's password or recovery code unwraps their key.

migrate-tenancy is the ONLY way to upgrade a database that is not named *_test or *_demo (multi-user plan, D10). It asks you
to type the word yes, it refuses when there is no terminal to ask on, and it reminds you to back up first. It never runs on its own.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

STATUS_SNIPPET = """
import json, sys
from rectifi import config, db
try:
    with db.connect_system("migrate-tenancy status") as conn:
        old = db._tables_before_tenancy(conn)
        counts = {t: conn.execute('SELECT count(*) AS n FROM "' + t + '"').fetchone()["n"] for t in old}
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__ + ": " + str(exc)})); sys.exit(0)
print(json.dumps({"target": db.describe_target(config.database_url()), "old": counts}))
"""

MIGRATE_SNIPPET = """
from rectifi import db
db.init_db(migrate=True)
print("done")
"""


def project_python() -> str:
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        if (ROOT / rel).exists():
            return str(ROOT / rel)
    return sys.executable


def _child(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run([project_python(), "-c", snippet], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")


def migrate_tenancy(args: list[str]) -> None:
    """Upgrade an older database to per-person rows. Needs a terminal and a typed yes."""
    if args:
        raise SystemExit("migrate-tenancy takes no options. It always asks you to type yes.")
    if not (sys.stdin and sys.stdin.isatty()):
        raise SystemExit("migrate-tenancy needs a terminal, so you can read what it will change and type yes. "
                         "It will not run from a script. Nothing was changed.")
    status = _child(STATUS_SNIPPET)
    try:
        info = json.loads(status.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise SystemExit("Could not check the database. " + (status.stderr.strip().splitlines() or ["Is Postgres running?"])[-1])
    if "error" in info:
        raise SystemExit("Could not reach the database: " + info["error"] + "\nStart it with `python scripts/dev.py db` or check DATABASE_URL in .env.")
    if not info["old"]:
        print(f"{info['target']} is new, or already upgraded. Nothing to do.")
        return
    print(f"Database: {info['target']}")
    print("This upgrade gives every row below to the owner (user 1) and turns on row level security:")
    for table, count in info["old"].items():
        print(f"  {table:<20} {count} rows")
    print("\nIt runs in one transaction and counts every table before and after. If any number differs it undoes everything.")
    print("It cannot be reversed except from a backup. BACK UP FIRST, for example:")
    print("    pg_dump --format=custom --file rectifi-before-upgrade.dump <your database>")
    answer = input("\nBacked up? Type yes to upgrade this database: ")
    if answer.strip() != "yes":
        print("Not upgraded. Nothing was changed.")
        return
    done = _child(MIGRATE_SNIPPET)
    if done.returncode != 0:
        print("The upgrade was stopped and undone. Nothing was changed.\n" + (done.stderr.strip().splitlines() or ["(no detail)"])[-1])
        raise SystemExit(1)
    print("Upgraded. Start the app as usual.")


INVITE_SNIPPET = """
import json, sys
from rectifi import db, users
kind, days = sys.argv[1], int(sys.argv[2])
try:
    db.init_db()
    made = users.create_invite(kind, days)
except users.AuthError as exc:
    print(json.dumps({"error": exc.message})); sys.exit(0)
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__ + ": " + str(exc)})); sys.exit(0)
print(json.dumps(made))
"""

LIST_SNIPPET = """
import json
from rectifi import db, users
try:
    db.init_db()
    rows = users.list_invites()
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__ + ": " + str(exc)})); raise SystemExit(0)
print(json.dumps({"rows": rows}))
"""

REVOKE_SNIPPET = """
import json, sys
from rectifi import db, users
try:
    db.init_db()
    done = users.revoke_invite(int(sys.argv[1]))
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__ + ": " + str(exc)})); raise SystemExit(0)
print(json.dumps({"revoked": done}))
"""


def _child_json(snippet: str, *argv: str) -> dict:
    done = subprocess.run([project_python(), "-c", snippet, *argv], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        data = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise SystemExit("Could not reach the database. " + (done.stderr.strip().splitlines() or ["Is Postgres running?"])[-1])
    if "error" in data:
        raise SystemExit(data["error"])
    return data


def invite(args: list[str]) -> None:
    """Make a one-time sign-up code and print it. This is the only time it is ever shown."""
    days, kind, rest = 7, "friend", list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--owner":
            kind = "owner"
        elif arg == "--days" and rest:
            try:
                days = int(rest.pop(0))
            except ValueError:
                raise SystemExit("--days needs a whole number, for example --days 7.")
            if not 1 <= days <= 90:
                raise SystemExit("--days must be between 1 and 90.")
        else:
            raise SystemExit("usage: invite [--days 7] [--owner]")
    made = _child_json(INVITE_SNIPPET, kind, str(days))
    print(f"Invite #{made['id']} ({kind}, good for {days} day{'s' if days != 1 else ''}):\n")
    print(f"    {made['code']}\n")
    print("Copy it now. Rectifi keeps only a fingerprint of it, so it cannot be shown again. It works once.")
    if kind == "owner":
        print("This one sets the password on the owner account (user 1); your existing data stays.")


def invites(args: list[str]) -> None:
    """List invites without their codes."""
    if args:
        raise SystemExit("invites takes no options.")
    rows = _child_json(LIST_SNIPPET)["rows"]
    if not rows:
        print("No invites yet. Make one with: python scripts/dev.py invite")
        return
    import time

    print(f"{'id':>3}  {'kind':<7} {'state':<9} {'expires':<17} reserved by")
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["expires_at"]))
        print(f"{r['id']:>3}  {r['kind']:<7} {r['state']:<9} {when:<17} {r['reserved_by'] or ''}")


def revoke_invite(args: list[str]) -> None:
    if len(args) != 1 or not args[0].isdigit():
        raise SystemExit("usage: revoke-invite <id>   (ids are in `dev.py invites`)")
    if _child_json(REVOKE_SNIPPET, args[0])["revoked"]:
        print(f"Invite #{args[0]} is cancelled.")
    else:
        print(f"Invite #{args[0]} was not cancelled: it does not exist or was already used.")


# dev.py reads this: command name -> (function, one-line help)
COMMANDS = {
    "migrate-tenancy": (migrate_tenancy, "upgrade an older database so it can hold more than one person (asks first)"),
    "invite": (invite, "make a one-time sign-up code, shown once (--days 7, --owner)"),
    "invites": (invites, "list invites without their codes"),
    "revoke-invite": (revoke_invite, "cancel an invite that has not been used"),
}


if __name__ == "__main__":
    # `python scripts/devtools/users.py migrate-tenancy` also works, without going through dev.py
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit("usage: users.py {" + "|".join(COMMANDS) + "} [options]")
    COMMANDS[sys.argv[1]][0](sys.argv[2:])
