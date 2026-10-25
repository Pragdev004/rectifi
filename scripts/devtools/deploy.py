"""dev.py commands for hosting: set-password, deploy-check. Standard library only; dev.py loads this file by path.

F1 (the foundation) wrote only the shape. WS-HOST fills in both commands.
Each command is a function(args: list[str]) -> None. Never print a password. Do not call subprocess with shell=True.
"""

from __future__ import annotations

NOT_BUILT = "{name}: not built yet."


def set_password(args: list[str]) -> None:
    print(NOT_BUILT.format(name="set-password"))


def deploy_check(args: list[str]) -> None:
    print(NOT_BUILT.format(name="deploy-check"))


# dev.py reads this: command name -> (function, one-line help)
COMMANDS = {
    "set-password": (set_password, "choose the password that protects Rectifi when it is online"),
    "deploy-check": (deploy_check, "check the hosting files and settings before you deploy"),
}
