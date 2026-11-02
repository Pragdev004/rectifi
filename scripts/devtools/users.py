"""dev.py commands for people and invites: migrate-tenancy now; invite, invites and revoke-invite come with sign-up.
Standard library only; dev.py loads this file by path (so it works before anything is installed and never imports rectifi).
The real work runs in the project's Python, as a child process.

    migrate-tenancy              give every row of an older database to the owner (user 1), so it can hold more than one person
    invite [--days 7] [--owner]  not built yet (WS-AUTH)
    invites                      not built yet (WS-AUTH)
    revoke-invite <id>           not built yet (WS-AUTH)

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


def _not_built(name: str):
    def command(args: list[str]) -> None:
        raise SystemExit(f"`{name}` is not built yet. It comes with the sign-up work. Nothing was changed.")
    return command


# dev.py reads this: command name -> (function, one-line help)
COMMANDS = {
    "migrate-tenancy": (migrate_tenancy, "upgrade an older database so it can hold more than one person (asks first)"),
    "invite": (_not_built("invite"), "make a one-time sign-up code (not built yet)"),
    "invites": (_not_built("invites"), "list invites without their codes (not built yet)"),
    "revoke-invite": (_not_built("revoke-invite"), "cancel an invite (not built yet)"),
}


if __name__ == "__main__":
    # `python scripts/devtools/users.py migrate-tenancy` also works, without going through dev.py
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        raise SystemExit("usage: users.py {" + "|".join(COMMANDS) + "} [options]")
    COMMANDS[sys.argv[1]][0](sys.argv[2:])
