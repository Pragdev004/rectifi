"""WS-DEPLOY: the old Streamlit app refuses to run in multi-user mode (plan D1) and runs as before in laptop mode."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app" / "streamlit_app.py"


def test_streamlit_refuses_in_multiuser_mode(monkeypatch):
    monkeypatch.setenv("RECTIFI_MULTIUSER", "1")
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.run()
    assert not at.exception
    assert any("does not run in multi-user mode" in t.value for t in at.title)
    assert any("RECTIFI_MULTIUSER" in e.value for e in at.error)
    assert not at.sidebar.markdown, "the sidebar and pages must not be drawn"


def test_streamlit_runs_in_laptop_mode(monkeypatch):
    monkeypatch.delenv("RECTIFI_MULTIUSER", raising=False)
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.run()
    assert not at.exception
    assert not any("multi-user mode" in t.value for t in at.title)
    assert at.sidebar.markdown, "the sidebar is drawn in laptop mode"
