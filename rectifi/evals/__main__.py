"""`python -m rectifi.evals <classify|charts|rag> [options]`. `dev.py eval` runs this."""
from __future__ import annotations

import importlib
import sys

NAMES = ("classify", "charts", "rag")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] not in NAMES:
        print(f"Usage: python -m rectifi.evals {{{'|'.join(NAMES)}}} [--live] [options]")
        return 2
    module = importlib.import_module(f"rectifi.evals.{args[0]}")
    return int(module.main(args[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
