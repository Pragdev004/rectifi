"""Run checked chart code in a limited child process and take back only JSON and PNG bytes. Pure stdlib (no pandas here).

The parent writes data.json, code.py and meta.json into a fresh temp folder, starts `python -I _child.py <folder>` with a
scrubbed environment, sends "go", waits (wall clock) and reads result.json plus two PNGs. It checks every byte it reads:
PNG signature and size, JSON size and shape. Nothing the child wrote is ever unpickled or executed.

What this protects against, and what it does not, is written down in the README of this workstream's handback and in
AI_PLAN B2.4: it stops mistakes, a prompt-injected request, stray file/network/process use, runaway loops and memory, and
reading .env or the database URL. It is NOT a defence against someone who can already run arbitrary Python.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from . import theme as theme_module

CHILD = Path(__file__).resolve().parent / "_child.py"
WINJOB = Path(__file__).resolve().parent / "_winjob.py"
MONEY = Path(__file__).resolve().parent.parent / "money.py"
MAX_PNG = 2 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MIN_SIDE, MAX_SIDE = 50, 6000
AS_OVERHEAD_MB = 1536                  # numpy + pandas + matplotlib reserve this much address space before any data

# Where matplotlib keeps its font list between runs (the first run builds it). Tests point this at a temp folder.
CACHE_DIR = Path(os.getenv("RECTIFI_HOME") or Path.home() / ".rectifi") / "mplcache"


class BadOutput(ValueError):
    """The child produced something the parent refuses to trust."""


@dataclass
class SandboxResult:
    status: str                     # ok | error | timeout | memory | crashed | bad_output
    error: dict | None = None       # {"type", "message", "traceback"}
    png: dict = field(default_factory=dict)          # {"light": bytes, "dark": bytes}
    info: dict | None = None        # what the figure holds (validated shape)
    warnings: list = field(default_factory=list)
    limits: str = "unknown"         # rlimit | job-object | timeout-only
    elapsed: float = 0.0
    log: str = ""                   # the tail of the child's stdout/stderr, for the trace

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ───────────────────────── checking what comes back ─────────────────────────

def check_png(blob: bytes) -> tuple[int, int]:
    """Return (width, height) of a PNG, or raise BadOutput. Looks at the signature, the size cap and the IHDR chunk only."""
    if not isinstance(blob, (bytes, bytearray)) or len(blob) > MAX_PNG:
        raise BadOutput("the image is too large")
    if len(blob) < 33 or not blob.startswith(PNG_SIGNATURE) or blob[12:16] != b"IHDR":
        raise BadOutput("the image is not a PNG")
    width, height = struct.unpack(">II", blob[16:24])
    if not (MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE):
        raise BadOutput("the image has an odd size")
    return width, height


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def _str(v, limit=400) -> str:
    if not isinstance(v, str):
        raise BadOutput("a text field is not text")
    return v[:limit]


def _num_list(v, limit=2000) -> list:
    if not isinstance(v, list) or len(v) > limit or not all(_is_num(x) for x in v):
        raise BadOutput("a number list is malformed")
    return v


ARTIST_TYPES = ("bar", "line", "scatter", "wedge", "collection", "image")


def validate_result(raw) -> dict:
    """Rebuild result.json from only the fields we expect, with their types checked. Raise BadOutput on anything else."""
    if not isinstance(raw, dict):
        raise BadOutput("result.json is not an object")
    status = raw.get("status")
    if status not in ("ok", "error"):
        raise BadOutput("result.json has an unknown status")
    out: dict = {"status": status, "limits": _str(raw.get("limits", "unknown"), 30), "error": None, "info": None, "warnings": []}
    warnings = raw.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 60:
        raise BadOutput("warnings are malformed")
    out["warnings"] = [_str(w, 300) for w in warnings]
    err = raw.get("error")
    if err is not None:
        if not isinstance(err, dict):
            raise BadOutput("error is malformed")
        out["error"] = {"type": _str(err.get("type", ""), 80), "message": _str(err.get("message", ""), 400),
                        "traceback": _str(err.get("traceback", ""), 1200)}
    info = raw.get("info")
    if info is not None:
        if not isinstance(info, dict):
            raise BadOutput("info is malformed")
        axes = info.get("axes")
        if not isinstance(axes, list) or len(axes) > 24:
            raise BadOutput("axes are malformed")
        clean_axes = []
        for ax in axes:
            if not isinstance(ax, dict):
                raise BadOutput("an axis is malformed")
            artists = ax.get("artists")
            if not isinstance(artists, list) or len(artists) > 40:
                raise BadOutput("artists are malformed")
            clean_artists = []
            for a in artists:
                if not isinstance(a, dict) or a.get("type") not in ARTIST_TYPES or not isinstance(a.get("n"), int) or isinstance(a.get("n"), bool):
                    raise BadOutput("an artist is malformed")
                clean_artists.append({"type": a["type"], "n": a["n"], **{k: _num_list(a.get(k, [])) for k in ("values", "values_x", "tops", "rights")}})
            for key in ("texts", "legend", "xticks", "yticks"):
                if not isinstance(ax.get(key), list) or len(ax[key]) > 200:
                    raise BadOutput(f"{key} is malformed")
            clean_axes.append({
                **{k: _str(ax.get(k, "")) for k in ("title", "title_left", "title_right", "xlabel", "ylabel")},
                "artists": clean_artists, **{k: [_str(t, 300) for t in ax[k]] for k in ("texts", "legend", "xticks", "yticks")},
                "is_pie": bool(ax.get("is_pie")), "visible": bool(ax.get("visible", True)),
            })
        size = info.get("figsize")
        if not (isinstance(size, list) and len(size) == 2 and all(_is_num(x) for x in size)):
            raise BadOutput("figsize is malformed")
        out["info"] = {"suptitle": _str(info.get("suptitle", "")), "n_axes": len(clean_axes), "axes": clean_axes, "figsize": size}
    return out


def _read_file(path: Path, limit: int) -> bytes:
    size = path.stat().st_size
    if size > limit:
        raise BadOutput(f"{path.name} is larger than {limit} bytes")
    return path.read_bytes()


# ───────────────────────── launching ─────────────────────────

def child_env(job_dir: str) -> dict:
    """Only these variables reach the child. No DATABASE_URL, no API keys, no proxies."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "TEMP": job_dir, "TMP": job_dir, "TMPDIR": job_dir,
        "MPLBACKEND": "Agg", "MPLCONFIGDIR": str(CACHE_DIR),
        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
    }
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


def _kill(proc: subprocess.Popen, winjob) -> None:
    try:
        if os.name == "nt":
            if winjob is not None:
                winjob.terminate()
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)          # start_new_session made the child its own group leader
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_chart(code: str, frame: dict, *, period: str = "", timeout: float | None = None, memory_mb: int | None = None,
              themes: tuple[str, ...] = theme_module.THEMES) -> SandboxResult:
    """Run `code` against `frame` (the dict from tools.chart_frame) and return what came back.

    timeout and memory_mb default to config.CHART_TIMEOUT and config.CHART_MEMORY_MB."""
    timeout = float(timeout if timeout is not None else config.CHART_TIMEOUT)
    memory_mb = int(memory_mb if memory_mb is not None else config.CHART_MEMORY_MB)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    job_dir = tempfile.mkdtemp(prefix="rectifi-chart-")
    started = time.monotonic()
    winjob = None
    try:
        job = Path(job_dir)
        (job / "code.py").write_text(code, encoding="utf-8")
        (job / "data.json").write_text(json.dumps(frame, ensure_ascii=False), encoding="utf-8")
        meta = {
            "period": str(period)[:80], "memory_bytes": (memory_mb + AS_OVERHEAD_MB) * 1024 * 1024, "cpu_seconds": int(timeout) + 5,
            "money_path": str(MONEY), "winjob_path": str(WINJOB), "cache_dir": str(CACHE_DIR), "font_dir": str(theme_module.FONT_DIR),
            "font_files": [str(theme_module.FONT_DIR / n) for n in theme_module.FONT_FILES],
            "themes": {t: {"rc": theme_module.rc_params(t), "palette": theme_module.palette(t)} for t in themes},
        }
        (job / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        log_path = job / "child.log"
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        with open(log_path, "wb") as log:
            proc = subprocess.Popen([sys.executable, "-I", str(CHILD), job_dir], cwd=job_dir, stdin=subprocess.PIPE,
                                    stdout=log, stderr=subprocess.STDOUT, env=child_env(job_dir), **kwargs)
            if os.name == "nt":
                from . import _winjob
                winjob = _winjob.ParentJob()
                winjob.assign(proc.pid)
            try:
                proc.communicate(input=b"go\n", timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill(proc, winjob)
                proc.wait()
                return SandboxResult("timeout", {"type": "Timeout", "message": f"The chart code ran longer than {timeout:g} seconds and was stopped.",
                                                 "traceback": ""}, elapsed=time.monotonic() - started, log=_tail(log_path))
            finally:
                if proc.poll() is None:
                    _kill(proc, winjob)
        elapsed = time.monotonic() - started
        log_text = _tail(log_path)
        result_path = job / "result.json"
        if not result_path.exists():
            code_ = proc.returncode
            killed = code_ is not None and code_ < 0
            return SandboxResult("timeout" if killed and -code_ in (signal.SIGXCPU, signal.SIGKILL) and elapsed >= timeout - 1 else "crashed",
                                 {"type": "Crashed", "message": f"The chart process stopped without a result (exit code {code_}).",
                                  "traceback": ""}, elapsed=elapsed, log=log_text)
        try:
            result = validate_result(json.loads(_read_file(result_path, MAX_JSON).decode("utf-8")))
            pngs: dict = {}
            if result["status"] == "ok":
                for t in themes:
                    blob = _read_file(job / f"{t}.png", MAX_PNG)
                    check_png(blob)
                    pngs[t] = blob
                if result["info"] is None:
                    raise BadOutput("a finished chart has no description")
        except (BadOutput, OSError, ValueError) as exc:
            return SandboxResult("bad_output", {"type": "BadOutput", "message": f"The chart process returned something unusable ({exc}).",
                                                "traceback": ""}, elapsed=elapsed, log=log_text)
        status = result["status"]
        if status == "error" and result["error"] and result["error"]["type"] == "MemoryError":
            status = "memory"
        return SandboxResult(status, result["error"], pngs, result["info"], result["warnings"], result["limits"], elapsed, log_text)
    finally:
        if winjob is not None:
            winjob.close()
        shutil.rmtree(job_dir, ignore_errors=True)


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        data = path.read_bytes()[-limit:]
        return data.decode("utf-8", "replace")
    except OSError:
        return ""
