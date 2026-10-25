"""dev.py commands for models: ollama-setup, classify, eval. Standard library only; dev.py loads this file by path.

F1 (the foundation) wrote only the shape. WS-LLM fills in `ollama-setup`, `classify` and `eval` follow their workstreams.
Each command is a function(args: list[str]) -> None. Do not call subprocess with shell=True.
"""

from __future__ import annotations

NOT_BUILT = "{name}: not built yet."


def ollama_setup(args: list[str]) -> None:
    print(NOT_BUILT.format(name="ollama-setup"))


def classify(args: list[str]) -> None:
    print(NOT_BUILT.format(name="classify"))


def run_eval(args: list[str]) -> None:
    print(NOT_BUILT.format(name="eval"))


# dev.py reads this: command name -> (function, one-line help)
COMMANDS = {
    "ollama-setup": (ollama_setup, "pull the models for this laptop and create the Rectifi Modelfile models"),
    "classify": (classify, "sort the payments still waiting for a category"),
    "eval": (run_eval, "measure the classifier, charts and knowledge answers on the files in evals/"),
}
