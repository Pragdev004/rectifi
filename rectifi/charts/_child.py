"""The sandboxed half of the chart harness. Run as: python -I _child.py <job_dir>. Never imported by the app.

It puts its own limits on itself, waits for "go" on stdin, loads the data, runs the checked code under an audit hook and
writes PNGs plus result.json into the job directory. Nothing is pickled; the parent reads JSON and PNG bytes only.

Order matters (see AI_PLAN B2.4): limits, "go", imports, data, theme, network patches, compile, audit hook, exec, save.
This is a guard against mistakes and a prompt-injected request. It is NOT a defence against a determined attacker who can
run arbitrary Python (PEP 578: audit hooks are not a security boundary).

Importing this file does nothing; the work happens in main(), so the pure-module import check can load it safely.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

ALLOWED_IMPORTS = frozenset({
    "pandas", "numpy", "matplotlib", "matplotlib.pyplot", "matplotlib.ticker", "matplotlib.dates",
    "seaborn", "math", "datetime", "calendar", "collections", "itertools", "textwrap",
})
FORBIDDEN_BUILTINS = ("eval", "exec", "compile", "open", "input", "__import__", "globals", "locals", "vars", "getattr", "setattr",
                      "delattr", "breakpoint", "exit", "quit", "help", "memoryview")
MAX_SERIES = 2000            # values kept per artist in result.json
MAX_TEXTS = 200
MAX_PIXELS = 4000            # longest side of the PNG
EXTRA_READ_ROOTS = ("/usr/share/zoneinfo", "/usr/lib/python3", "/etc/localtime", "/proc/self", "/proc/cpuinfo", "/proc/meminfo",
                    "/sys/devices/system/cpu", "/dev/urandom", "/dev/null")


def _fail_early(message: str, code: int = 3) -> None:
    sys.stderr.write(message + "\n")
    sys.exit(code)


def _set_limits(meta: dict) -> str:
    """Lower our own limits so user code can't raise them. Returns which kind of limit is in force."""
    if os.name == "nt":
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_winjob", meta["winjob_path"])
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.limit_self(int(meta["memory_bytes"]), int(meta["cpu_seconds"]))
            return "job-object"
        except Exception:                       # noqa: BLE001 - the wall clock in the parent still applies
            return "timeout-only"
    try:
        import resource

        def cap(which, value):
            resource.setrlimit(which, (value, value))      # soft = hard: user code cannot raise it back

        cap(resource.RLIMIT_CPU, int(meta["cpu_seconds"]))
        cap(resource.RLIMIT_AS, int(meta["memory_bytes"]))
        cap(resource.RLIMIT_FSIZE, 20 * 1024 * 1024)
        cap(resource.RLIMIT_NOFILE, 256)
        cap(resource.RLIMIT_CORE, 0)
        return "rlimit"
    except Exception:                           # noqa: BLE001
        return "timeout-only"


def _load_by_path(name: str, path: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _under(path: str, roots: list[str]) -> bool:
    p = os.path.normcase(os.path.realpath(path))
    for r in roots:
        r = os.path.normcase(r)
        if p == r or p.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False


def _install_audit(job_dir: str, read_roots: list[str], code_obj, runs: int) -> None:
    import sys as _sys

    jd = os.path.realpath(job_dir)
    reads = [os.path.realpath(r) for r in read_roots] + [os.path.realpath(r) for r in EXTRA_READ_ROOTS] + [jd]
    allowed_exec = {id(code_obj): runs}
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    blocked_prefixes = ("socket.", "ctypes.", "subprocess.", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.forkpty",
                        "os.system", "os.startfile", "pty.", "msvcrt.", "winreg.", "webbrowser.", "smtplib.", "ftplib.",
                        "http.client.", "urllib.Request", "telnetlib.", "imaplib.", "poplib.", "multiprocessing.")
    path_events = ("os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.truncate", "os.symlink",
                   "os.link", "os.utime", "os.setxattr", "shutil.rmtree", "shutil.copyfile", "shutil.copytree", "shutil.move",
                   "shutil.chown", "shutil.copymode", "shutil.copystat", "os.putenv", "os.unsetenv")

    def nearest_is_user_code() -> bool:
        frame = _sys._getframe(2)
        return frame is not None and frame.f_code.co_filename == "<chart>"

    def hook(event: str, args: tuple) -> None:
        if event.startswith(blocked_prefixes):
            raise PermissionError(f"blocked by the chart sandbox: {event}")
        if event in ("exec", "compile"):
            if event == "exec" and args and id(args[0]) in allowed_exec and allowed_exec[id(args[0])] > 0:
                allowed_exec[id(args[0])] -= 1
                return
            if nearest_is_user_code():
                raise PermissionError(f"blocked by the chart sandbox: {event} from chart code")
            return                               # library imports and namedtuple/dataclass builders run exec themselves
        if event == "open":
            path, mode, flags = (args + (None, None, None))[:3]
            if isinstance(path, int):
                return
            try:
                text = os.fsdecode(path)
            except TypeError:
                return
            writing = False
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                writing = True
            if isinstance(flags, int) and flags & write_flags:
                writing = True
            if writing:
                if not _under(os.path.join(jd, text) if not os.path.isabs(text) else text, [jd, "/dev/null"]):
                    raise PermissionError("blocked by the chart sandbox: writing outside the job folder")
            elif not _under(os.path.join(jd, text) if not os.path.isabs(text) else text, reads):
                raise PermissionError("blocked by the chart sandbox: reading outside the allowed folders")
            return
        if event in path_events:
            for value in args[:2]:
                if isinstance(value, (str, bytes, os.PathLike)):
                    text = os.fsdecode(value)
                    if not _under(os.path.join(jd, text) if not os.path.isabs(text) else text, [jd]):
                        raise PermissionError(f"blocked by the chart sandbox: {event} outside the job folder")
            if event in ("os.putenv", "os.unsetenv"):
                raise PermissionError(f"blocked by the chart sandbox: {event}")

    _sys.addaudithook(hook)


def _safe_import_factory(real_import):
    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0 or name not in ALLOWED_IMPORTS:
            raise ImportError(f"importing '{name}' is not allowed in chart code")
        return real_import(name, globals, locals, fromlist, level)
    return safe_import


def _block_network() -> None:
    import socket

    def blocked(*args, **kwargs):
        raise PermissionError("network access is blocked in the chart sandbox")

    class _Blocked(socket.socket):
        def __init__(self, *a, **k):
            blocked()

    socket.socket = _Blocked
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "create_server",
                 "socketpair", "fromfd"):
        if hasattr(socket, name):
            setattr(socket, name, blocked)


def _inr_factory(money):
    from decimal import Decimal

    def inr(paise, paise_digits: bool = True) -> str:
        """Format an integer number of paise as rupees: inr(125050) -> '₹1,250.50'. The only way to put money in text."""
        value = int(paise)
        return money.fmt_inr(Decimal(value) / 100, paise=bool(paise_digits))
    return inr


def _build_frames(data: dict):
    import pandas as pd

    tx = pd.DataFrame(data["transactions"], columns=data["transaction_columns"])
    if len(tx):
        tx["date"] = pd.to_datetime(tx["date"])
        tx["amount_paise"] = tx["amount_paise"].astype("int64")
        tx["amount"] = tx["amount"].astype("float64")
    else:
        tx = tx.astype({"amount_paise": "int64", "amount": "float64"})
        tx["date"] = pd.to_datetime(tx["date"])
    bud = pd.DataFrame(data["budgets"], columns=data["budget_columns"])
    if len(bud):
        bud["limit_paise"] = bud["limit_paise"].astype("int64")
    cats = pd.DataFrame(data["categories"], columns=["name", "kind"])
    return tx, bud, cats


def _num(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _floats(seq) -> list:
    out = []
    for v in list(seq)[:MAX_SERIES]:
        n = _num(v)
        if n is not None:
            out.append(n)
    return out


def _text(s) -> str:
    return str(s)[:300]


def _introspect(fig) -> dict:
    import numpy as np
    from matplotlib import patches
    from matplotlib.collections import PathCollection
    from matplotlib.lines import Line2D

    axes_info = []
    suptitle = fig._suptitle.get_text() if getattr(fig, "_suptitle", None) is not None else ""
    for ax in fig.get_axes():
        artists = []
        bars_h, bars_w, bars_top, bars_right, wedges = [], [], [], [], 0
        rect_count = 0
        for p in ax.patches:
            if isinstance(p, patches.Wedge):
                wedges += 1
            elif isinstance(p, patches.Rectangle) and (p.get_width() != 0 or p.get_height() != 0):
                if p.get_visible():
                    rect_count += 1
                    bars_h.append(p.get_height()); bars_w.append(p.get_width())
                    bars_top.append(p.get_y() + p.get_height()); bars_right.append(p.get_x() + p.get_width())
        if rect_count:
            artists.append({"type": "bar", "n": rect_count, "values": _floats(bars_h), "values_x": _floats(bars_w),
                            "tops": _floats(bars_top), "rights": _floats(bars_right)})
        if wedges:
            artists.append({"type": "wedge", "n": wedges, "values": [], "values_x": [], "tops": [], "rights": []})
        for line in ax.lines:
            if isinstance(line, Line2D) and line.get_visible() and not str(line.get_label()).startswith("_child"):
                ys = np.asarray(line.get_ydata())
                xs = np.asarray(line.get_xdata())
                if ys.dtype.kind in "iufb" and len(ys):
                    artists.append({"type": "line", "n": int(len(ys)), "values": _floats(ys), "values_x": _floats(xs) if xs.dtype.kind in "iufb" else [],
                                    "tops": [], "rights": []})
        for coll in ax.collections:
            if isinstance(coll, PathCollection):
                off = np.asarray(coll.get_offsets())
                if off.ndim == 2 and off.shape[0]:
                    artists.append({"type": "scatter", "n": int(off.shape[0]), "values": _floats(off[:, 1]), "values_x": _floats(off[:, 0]),
                                    "tops": [], "rights": []})
            elif hasattr(coll, "get_paths") and len(coll.get_paths()):
                artists.append({"type": "collection", "n": int(len(coll.get_paths())), "values": [], "values_x": [], "tops": [], "rights": []})
        if ax.images:
            artists.append({"type": "image", "n": len(ax.images), "values": [], "values_x": [], "tops": [], "rights": []})
        texts = [_text(t.get_text()) for t in ax.texts if t.get_text()][:MAX_TEXTS]
        legend = ax.get_legend()
        legend_texts = [_text(t.get_text()) for t in legend.get_texts()] if legend is not None else []
        # bar_label() adds Text objects to ax.texts in matplotlib >= 3.4, so `texts` already holds them.
        xticks = [_text(t.get_text()) for t in ax.get_xticklabels() if t.get_text()][:60]
        yticks = [_text(t.get_text()) for t in ax.get_yticklabels() if t.get_text()][:60]
        axes_info.append({
            "title": _text(ax.get_title() or ax.get_title(loc="left") or ax.get_title(loc="right")), "title_left": _text(ax.get_title(loc="left")), "title_right": _text(ax.get_title(loc="right")),
            "xlabel": _text(ax.get_xlabel()), "ylabel": _text(ax.get_ylabel()),
            "artists": artists[:40], "texts": texts, "legend": legend_texts[:40], "xticks": xticks, "yticks": yticks,
            "is_pie": wedges > 0, "visible": bool(ax.get_visible()),
        })
    w, h = fig.get_size_inches()
    return {"suptitle": _text(suptitle), "n_axes": len(axes_info), "axes": axes_info, "figsize": [float(w), float(h)]}


def _clamp_size(fig, dpi: int) -> None:
    w, h = fig.get_size_inches()
    longest = max(w, h) * dpi
    if longest > MAX_PIXELS:
        scale = MAX_PIXELS / longest
        fig.set_size_inches(w * scale, h * scale)


def _trim_traceback(exc: BaseException, code_lines: list[str]) -> str:
    out = []
    for frame in traceback.extract_tb(exc.__traceback__):
        if frame.filename == "<chart>":
            line = code_lines[frame.lineno - 1].strip() if 0 < (frame.lineno or 0) <= len(code_lines) else ""
            out.append(f"line {frame.lineno}: {line}")
    return "\n".join(out[-8:])


def _write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    job_dir = os.path.abspath(argv[1])
    os.chdir(job_dir)
    with open(os.path.join(job_dir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    limits = _set_limits(meta)
    if sys.stdin.buffer.readline().strip() != b"go":      # the parent applies its own limits first, then says go
        _fail_early("no go signal")
    result_path = os.path.join(job_dir, "result.json")
    result: dict = {"status": "error", "limits": limits, "error": None, "info": None, "warnings": []}
    try:
        import io
        import logging
        import warnings

        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        import pandas as pd
        import seaborn  # noqa: F401  (imported now so its files are read before the audit hook is on)
        from matplotlib import font_manager

        with open(os.path.join(job_dir, "data.json"), encoding="utf-8") as f:
            data = json.load(f)
        with open(os.path.join(job_dir, "code.py"), encoding="utf-8") as f:
            source = f.read()
        code_lines = source.splitlines()
        money = _load_by_path("rectifi_money", meta["money_path"])
        inr = _inr_factory(money)
        for path in meta.get("font_files", []):
            font_manager.fontManager.addfont(path)
        _block_network()
        code_obj = compile(source, "<chart>", "exec")
        themes = meta["themes"]
        read_roots = [sys.base_prefix, sys.prefix, matplotlib.get_data_path(), meta["cache_dir"], meta["font_dir"], job_dir]
        import site
        read_roots += list(site.getsitepackages())
        try:
            read_roots.append(site.getusersitepackages())
        except Exception:                       # noqa: BLE001
            pass
        read_roots += [p for p in sys.path if p]
        _install_audit(job_dir, read_roots, code_obj, runs=len(themes))
        import builtins as _b
        safe_import = _safe_import_factory(_b.__import__)

        log_records: list[str] = []

        class _Collect(logging.Handler):
            def emit(self, record):
                message = record.getMessage()[:300]
                if not message.startswith("findfont:"):        # the DejaVu fallback has no semibold: harmless noise
                    log_records.append(message)

        handler = _Collect(level=logging.WARNING)
        logging.getLogger("matplotlib").addHandler(handler)

        ok_png: dict[str, bytes] = {}
        info = None
        for theme, spec in themes.items():
            plt.close("all")
            plt.rcdefaults()
            matplotlib.rcParams.update(spec["rc"])
            np.random.seed(0)
            tx, bud, cats = _build_frames(data)
            builtins_dict = {k: v for k, v in vars(_b).items() if k not in FORBIDDEN_BUILTINS}
            builtins_dict["__import__"] = safe_import
            namespace = {"__builtins__": builtins_dict, "__name__": "chart", "df": tx, "budgets": bud, "categories": cats,
                         "inr": inr, "PALETTE": dict(spec["palette"]), "PERIOD": meta["period"]}
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                try:
                    exec(code_obj, namespace)
                except BaseException as exc:           # noqa: BLE001 - SystemExit, MemoryError and so on all end the run the same way
                    result["error"] = {"type": type(exc).__name__, "message": _text(exc), "traceback": _trim_traceback(exc, code_lines)}
                    result["warnings"] = [_text(w.message) for w in caught][:20]
                    break
                fig = namespace.get("fig")
                from matplotlib.figure import Figure
                if not isinstance(fig, Figure):
                    inner = getattr(fig, "figure", None) or getattr(fig, "fig", None)
                    fig = inner if isinstance(inner, Figure) else None
                if fig is None:
                    result["error"] = {"type": "NoFigure", "message": "`fig` is not a matplotlib figure. Set `fig` to the figure (fig, ax = plt.subplots()).", "traceback": ""}
                    break
                _clamp_size(fig, 144)
                if theme == "light":
                    info = _introspect(fig)
                buf = io.BytesIO()
                fig.savefig(buf, format="png", dpi=144, facecolor=fig.get_facecolor())
                ok_png[theme] = buf.getvalue()
                result["warnings"] = [_text(w.message) for w in caught][:20]
        result["warnings"] = (result["warnings"] + log_records)[:30]
        if result["error"] is None and set(ok_png) == set(themes):
            for theme, blob in ok_png.items():
                with open(os.path.join(job_dir, f"{theme}.png"), "wb") as out:
                    out.write(blob)
            result["status"] = "ok"
            result["info"] = info
    except BaseException as exc:                            # noqa: BLE001
        result["error"] = {"type": type(exc).__name__, "message": _text(exc), "traceback": "".join(traceback.format_exception_only(type(exc), exc))[:600]}
    _write_json(result_path, result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
