"""docs/CONCEPTS.md only names tests that exist."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = (ROOT / "docs" / "CONCEPTS.md").read_text(encoding="utf-8")
REFS = sorted(set(re.findall(r"`(test_[a-z0-9_]+)::(test_[a-z0-9_]+)`", DOC)))


def test_the_doc_names_a_reasonable_number_of_tests():
    assert len(REFS) >= 50


def test_every_named_test_exists():
    missing = []
    for file, name in REFS:
        path = ROOT / "tests" / f"{file}.py"
        if not path.exists() or not re.search(rf"^(?:async )?def {name}\b", path.read_text(encoding="utf-8"), re.M):
            missing.append(f"{file}::{name}")
    assert not missing, missing


def test_the_exclusions_are_stated():
    for word in ("MCP", "load_prompt", "RunnableBranch", "AgentExecutor"):
        assert word in DOC
