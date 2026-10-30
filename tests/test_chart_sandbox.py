"""The chart sandbox. These tests send code straight to the sandbox, SKIPPING the AST validator on purpose: the sandbox is the
second lock and must hold on its own. Reaching dangerous modules goes through modules the libraries already imported
(`matplotlib.os`, `matplotlib.sys`), which is how a validator slip would look."""

import os
import sys

import pytest

from rectifi.charts import sandbox
from rectifi.charts.sandbox import BadOutput, check_png, validate_result

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX limits; the Windows job object has its own (skipped) test")

FRAME = {
    "transaction_columns": ["date", "month", "weekday", "amount_paise", "amount", "direction", "category", "category_kind", "payee", "account"],
    "transactions": [["2026-10-01", "2026-10", "Thu", 125050, 1250.5, "debit", "Food", "expense", "Swiggy", "SBI"],
                     ["2026-10-02", "2026-10", "Fri", 50000, 500.0, "debit", "Transport", "expense", "Uber", "SBI"]],
    "budget_columns": ["month", "category", "limit_paise"], "budgets": [["2026-10", "Food", 500000]],
    "categories": [["Food", "expense"], ["Transport", "expense"]],
}
GOOD = '''import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(8, 5))
d = df.groupby("category").amount.sum()
ax.bar(d.index, d.values, color=PALETTE["accent"])
ax.set_title("Spend " + inr(df.amount_paise.sum()))
ax.set_xlabel("Category"); ax.set_ylabel("Rupees")
'''
PRELUDE = "import matplotlib\nimport matplotlib.pyplot as plt\nimport numpy as np\nimport pandas as pd\nfig, ax = plt.subplots()\nax.plot([1, 2], [1, 2])\n"


@pytest.fixture(scope="module", autouse=True)
def cache(tmp_path_factory):
    old = sandbox.CACHE_DIR
    sandbox.CACHE_DIR = tmp_path_factory.mktemp("mplcache")
    yield
    sandbox.CACHE_DIR = old


def run(code, **kw):
    return sandbox.run_chart(code, FRAME, period="2026-10", **kw)


def blocked(result, needle=""):
    assert result.status == "error", (result.status, result.error)
    assert result.error["type"] == "PermissionError", result.error
    assert needle in result.error["message"]


def test_a_good_chart_comes_back_as_two_checked_pngs():
    r = run(GOOD)
    assert r.ok, r.error
    assert r.limits == "rlimit"
    assert set(r.png) == {"light", "dark"}
    for blob in r.png.values():
        assert check_png(blob)[0] > 400
    assert r.png["light"] != r.png["dark"]
    assert r.info["axes"][0]["title"] == "Spend ₹1,750.50"
    assert r.info["axes"][0]["artists"][0]["values"] == [1250.5, 500.0]


def test_the_rupee_sign_renders_without_missing_glyph_warnings():
    r = run(GOOD)
    assert r.ok and not [w for w in r.warnings if "missing" in w.lower() or "glyph" in w.lower()], r.warnings


def test_a_runaway_loop_is_killed_by_the_wall_clock():
    r = run("fig = 1\nwhile True:\n    pass\n", timeout=3)
    assert r.status == "timeout" and r.elapsed < 10


def test_a_memory_hog_is_stopped():
    r = run(PRELUDE + "x = np.ones(3 * 1024 * 1024 * 1024, dtype='u1')\n", memory_mb=256)
    assert r.status == "memory", (r.status, r.error)


def test_the_network_is_blocked():
    blocked(run(PRELUDE + "matplotlib.sys.modules['socket'].create_connection(('127.0.0.1', 9))\n"))
    blocked(run(PRELUDE + "matplotlib.sys.modules['socket'].socket()\n"))


def test_writing_outside_the_job_folder_is_blocked(tmp_path):
    target = tmp_path / "x.npy"
    r = run(PRELUDE + f"np.save({str(target)!r}, np.arange(3))\n")
    blocked(r, "writing outside")
    assert not target.exists()


def test_reading_a_secret_file_outside_the_allowed_folders_is_blocked(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("DATABASE_URL=postgresql://rectifi:topsecret@localhost/x\n")
    for reader in (f"np.loadtxt({str(secret)!r}, dtype=str)", f"pd.read_csv({str(secret)!r})"):
        r = run(PRELUDE + reader + "\n")
        blocked(r, "reading outside")
        assert "topsecret" not in str(r.error)


def test_a_subprocess_is_blocked():
    blocked(run(PRELUDE + "matplotlib.os.system('true')\n"))
    blocked(run(PRELUDE + "matplotlib.subprocess.run(['true'])\n"))


def test_a_second_exec_eval_or_compile_is_blocked():
    for call in ("exec('x = 1')", "eval('1 + 1')", "compile('1', 'x', 'eval')"):
        blocked(run(PRELUDE + f"matplotlib.sys.modules['builtins'].{call}\n"), "chart code")


def test_the_child_environment_has_no_secrets(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy")
    code = PRELUDE + "e = matplotlib.os.environ\nassert not [k for k in e if k in ('DATABASE_URL', 'OPENAI_API_KEY', 'HTTPS_PROXY')], list(e)\n"
    r = run(code)
    assert r.ok, r.error
    assert set(sandbox.child_env("/x")) <= {"PATH", "TEMP", "TMP", "TMPDIR", "MPLBACKEND", "MPLCONFIGDIR", "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                                            "MKL_NUM_THREADS", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8", "PYTHONIOENCODING", "SYSTEMROOT"}


def test_an_import_outside_the_allow_list_fails_inside_the_child_too():
    r = run("import os\nfig = 1\n")
    assert r.status == "error" and r.error["type"] == "ImportError"


def test_an_error_comes_back_with_only_chart_frames_in_the_traceback():
    r = run("import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\nax.bar(df['nope'], 1)\n")
    assert r.status == "error" and r.error["type"] == "KeyError"
    assert "line 3" in r.error["traceback"] and "site-packages" not in r.error["traceback"]


def test_no_figure_is_an_error():
    r = run("fig = 5\n")
    assert r.status == "error" and r.error["type"] == "NoFigure"


def test_a_huge_figure_is_clamped_not_a_memory_bomb():
    r = run("import matplotlib.pyplot as plt\nfig, ax = plt.subplots(figsize=(900, 900))\nax.plot([1, 2], [1, 2])\n")
    assert r.ok, r.error
    assert max(check_png(r.png["light"])) <= 4000


def test_the_chart_data_is_rebuilt_for_each_theme():
    code = "import matplotlib.pyplot as plt\ndf['x'] = 1\nfig, ax = plt.subplots()\nax.plot(df['x'].sum() + [0, 1])\n"
    assert run(code).ok


# ───────────── what the parent refuses to trust ─────────────

GOOD_RESULT = {"status": "ok", "limits": "rlimit", "error": None, "warnings": [],
               "info": {"suptitle": "", "n_axes": 1, "figsize": [8, 5], "axes": [{
                   "title": "t", "title_left": "", "title_right": "", "xlabel": "x", "ylabel": "y", "texts": [], "legend": [], "xticks": [], "yticks": [],
                   "artists": [{"type": "bar", "n": 1, "values": [1.0], "values_x": [], "tops": [], "rights": []}], "is_pie": False, "visible": True}]}}


def test_a_well_formed_result_passes_and_is_rebuilt():
    assert validate_result(GOOD_RESULT)["info"]["axes"][0]["title"] == "t"


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(status="pwned"),
    lambda r: r.update(warnings="x"),
    lambda r: r["info"]["axes"][0]["artists"][0].update(type="__import__"),
    lambda r: r["info"]["axes"][0]["artists"][0].update(values=[float("nan")]),
    lambda r: r["info"]["axes"][0]["artists"][0].update(values=["1"]),
    lambda r: r["info"]["axes"][0]["artists"][0].update(values=[1.0] * 5000),
    lambda r: r["info"]["axes"][0].update(title=5),
    lambda r: r["info"].update(axes=[GOOD_RESULT["info"]["axes"][0]] * 40),
    lambda r: r["info"].update(figsize=[1]),
    lambda r: r.update(error="boom"),
])
def test_a_malicious_or_broken_result_json_is_rejected(mutate):
    import copy
    bad = copy.deepcopy(GOOD_RESULT)
    mutate(bad)
    with pytest.raises(BadOutput):
        validate_result(bad)
    with pytest.raises(BadOutput):
        validate_result([1, 2])


def test_a_png_must_be_a_real_small_png():
    good = run(GOOD).png["light"]
    assert check_png(good)
    for bad in (b"", b"GIF89a" + b"0" * 100, good[:20], b"\x89PNG\r\n\x1a\n" + b"0" * 40, good + b"0" * sandbox.MAX_PNG):
        with pytest.raises(BadOutput):
            check_png(bad)


def test_a_planted_oversized_result_file_is_not_read(tmp_path):
    big = tmp_path / "result.json"
    big.write_bytes(b"x" * (sandbox.MAX_JSON + 1))
    with pytest.raises(BadOutput):
        sandbox._read_file(big, sandbox.MAX_JSON)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_the_windows_job_object_limits_apply():                       # pragma: no cover - runs only on Windows
    r = run(GOOD)
    assert r.ok and r.limits in ("job-object", "timeout-only")


def test_the_sandbox_runs_when_only_tmp_is_writable(tmp_path):
    """The deploy compose file runs the app with a read-only root file system and a tmpfs /tmp (docs/HOSTING.md). In a private
    mount namespace HOME is read-only, so the cache folder the sandbox would normally use cannot be made: it must fall back to /tmp."""
    import json
    import shutil
    import subprocess

    from pathlib import Path

    if shutil.which("unshare") is None or os.geteuid() != 0:
        pytest.skip("needs `unshare` and root to make a read-only mount")
    root = Path(__file__).resolve().parent.parent
    ro_home = tmp_path / "ro_home"
    ro_home.mkdir()
    py = ("import json, sys; from rectifi.charts import sandbox; "
          f"r = sandbox.run_chart({GOOD!r}, json.loads({json.dumps(FRAME)!r})); "
          "print('RESULT', r.ok, sandbox.usable_cache_dir())")
    shell = (f"set -e; mount --bind {ro_home} {ro_home}; mount -o remount,ro,bind {ro_home}; "
             f"export HOME={ro_home} RECTIFI_HOME={ro_home}/.rectifi; cd {root}; {sys.executable} -c \"$PY\"")
    done = subprocess.run(["unshare", "-m", "sh", "-c", shell], capture_output=True, text=True, timeout=120, env={**os.environ, "PY": py})
    if done.returncode != 0 and "mount" in done.stderr:
        pytest.skip("this environment does not allow mounts: " + done.stderr.strip()[:120])
    assert "RESULT True" in done.stdout, done.stdout + done.stderr
    assert "rectifi-mplcache" in done.stdout, done.stdout                      # fell back to the temp dir
