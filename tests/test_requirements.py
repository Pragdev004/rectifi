import re
from importlib.metadata import version
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"


def _pins() -> dict[str, str]:
    pins = {}
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]+\])?==(\S+)", line)
        assert match, f"Every line in requirements.txt must be an exact pin (name==version): {line!r}"
        pins[match[1]] = match[2]
    return pins


def test_pins_match_installed():
    pins = _pins()
    assert len(pins) >= 15
    wrong = {name: (want, version(name)) for name, want in pins.items() if version(name) != want}
    assert not wrong, f"requirements.txt differs from what is installed (wanted, installed): {wrong}"
