"""dev.py commands for models: llm-check, ollama-setup, classify, eval. Standard library only; dev.py loads this file by path
(so it works before anything is installed and never imports rectifi).

    llm-check [--quick]          is the model usable? A real round trip, the RAM tier, the embed model, loaded models
    ollama-setup [--tier T] [--dry-run]   pull the models for this laptop and `ollama create` the Rectifi Modelfile models
    classify [batch_id]          sort the payments still waiting for a category (runs the WS-CLASSIFY code)
    eval [classify|charts|rag]   measure a feature on the files in evals/

`doctor_checks(setting, quick)` is what `dev.py doctor` calls. It returns (level, what, fix) tuples, level "ok", "warn" or "fail".
Commands run as argument lists, never through a shell. A key is never printed: provider messages go through `redact`.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
ROUND_TRIP_TIMEOUT = 30
DEFAULT_BASE = "http://localhost:11434"
DEFAULT_CHAT = "qwen2.5:7b"
DEFAULT_EMBED = "nomic-embed-text"
OPENAI_STYLE = {                      # provider -> (key setting, model setting, default model, base url or None = a setting)
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", "", "https://openrouter.ai/api/v1"),
    "huggingface": ("HF_TOKEN", "HF_MODEL", "deepseek-ai/DeepSeek-V3.1:cheapest", "https://router.huggingface.co/v1"),
    "openai_compat": ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL", "", None),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "deepseek-flash", "https://api.deepseek.com"),
}
ROLE_ENV = {"chat": "RECTIFI_MODEL_CHAT", "classify": "RECTIFI_MODEL_CLASSIFY", "code": "RECTIFI_MODEL_CODE", "grade": "RECTIFI_MODEL_GRADE"}

# ───────────── the RAM tier table (docs/AI_PLAN.md, B5) ─────────────

TIERS = {
    "small": {"label": "8 GB or less", "chat": "llama3.2:3b", "classify": "qwen2.5:3b", "code": "qwen2.5-coder:3b",
              "grade": "qwen2.5:3b", "embed": DEFAULT_EMBED},
    "laptop": {"label": "12-16 GB (a typical laptop)", "chat": "qwen2.5:7b", "classify": "qwen2.5:3b", "code": "qwen2.5-coder:7b",
               "grade": "qwen2.5:3b", "embed": DEFAULT_EMBED},
    "large": {"label": "24 GB or more, or a GPU", "chat": "qwen3:14b", "classify": "qwen2.5:7b", "code": "qwen2.5-coder:14b",
              "grade": "qwen2.5:7b", "embed": DEFAULT_EMBED},
    "oracle": {"label": "Oracle A1 free server (2 cores, 12 GB)", "chat": "qwen2.5:3b", "classify": "qwen2.5:3b",
               "code": "qwen2.5-coder:3b", "grade": "qwen2.5:3b", "embed": DEFAULT_EMBED},
}
SMALL_MAX_GB = 9          # a machine sold as "8 GB" reports about 7.7 GB
LARGE_MIN_GB = 23         # "24 GB" reports a little less


def read_ram_gb() -> float | None:
    """Installed RAM in GB, or None when it can't be read. Linux /proc/meminfo, Windows GlobalMemoryStatusEx, macOS sysctl."""
    try:
        system = platform.system()
        if system == "Linux":
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 * 1024)
        elif system == "Windows":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("tpage", ctypes.c_ulonglong), ("apage", ctypes.c_ulonglong),
                            ("tvirt", ctypes.c_ulonglong), ("avirt", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]

            status = Status()
            status.length = ctypes.sizeof(Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):   # type: ignore[attr-defined]
                return status.total / (1024 ** 3)
        elif system == "Darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) / (1024 ** 3)
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        pass
    return None


def has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None


def pick_tier(ram_gb: float | None, gpu: bool = False) -> str:
    """small / laptop / large. With the RAM unknown, the laptop tier is the safe middle."""
    if gpu or (ram_gb is not None and ram_gb >= LARGE_MIN_GB):
        return "large"
    if ram_gb is not None and ram_gb <= SMALL_MAX_GB:
        return "small"
    return "laptop"


def env_lines(tier: str) -> list[str]:
    t = TIERS[tier]
    return [f"RECTIFI_MODEL_CHAT={t['chat']}", "RECTIFI_MODEL_CLASSIFY=rectifi-classify", "RECTIFI_MODEL_CODE=rectifi-chartcoder",
            f"RECTIFI_MODEL_GRADE={t['grade']}", f"RECTIFI_EMBED_MODEL={t['embed']}"]


# ───────────── Modelfiles ─────────────

MODELFILES = {"rectifi-classify": ("Modelfile.classify", "classify"), "rectifi-chartcoder": ("Modelfile.chartcoder", "code")}


def parse_modelfile(text: str) -> dict:
    """{"from": str, "system": str, "parameters": {name: value}}. Understands FROM, SYSTEM (one line or triple quotes), PARAMETER
    and # comments. Raises ValueError on a line it doesn't know or when FROM or SYSTEM is missing."""
    result: dict = {"from": "", "system": "", "parameters": {}}
    lines = iter(text.splitlines())
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        keyword, rest = keyword.upper(), rest.strip()
        if keyword == "FROM" and rest:
            result["from"] = rest
        elif keyword == "SYSTEM" and rest:
            if rest.startswith('"""'):
                body = rest[3:]
                while '"""' not in body:
                    body += "\n" + next(lines, '"""')
                result["system"] = body.split('"""')[0].strip()
            else:
                result["system"] = rest.strip('"')
        elif keyword == "PARAMETER":
            name, _, value = rest.partition(" ")
            if not name or not value.strip():
                raise ValueError(f"Bad PARAMETER line: {raw!r}")
            result["parameters"][name] = value.strip()
        else:
            raise ValueError(f"Unknown Modelfile line: {raw!r}")
    if not result["from"] or not result["system"]:
        raise ValueError("A Modelfile needs FROM and SYSTEM.")
    return result


def render_modelfile(text: str, base: str) -> str:
    """The Modelfile text with its FROM line pointing at `base`."""
    return re.sub(r"(?m)^FROM\s+\S+", f"FROM {base}", text, count=1)


# ───────────── settings (the process environment, then .env) ─────────────

def load_setting():
    """A function setting(name, default="") that reads the environment first, then .env next to the project."""
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip().strip('"').strip("'")

    def setting(name: str, default: str = "") -> str:
        return (os.environ.get(name) or values.get(name) or default).strip()

    return setting


# ───────────── HTTP (urllib) ─────────────

_SECRETS = [(re.compile(p, re.I), r) for p, r in (
    (r"Bearer\s+\S+", "Bearer [hidden]"), (r"\bAQ\.[\w-]+", "[key hidden]"), (r"\bAIza[\w-]{20,}", "[key hidden]"),
    (r"\bsk-[\w-]+", "[key hidden]"), (r"\bhf_\w+", "[key hidden]"), (r"([?&]key=)[^&\s\"']+", r"\1[hidden]"))]


def redact(text: object, secrets: tuple[str, ...] = ()) -> str:
    out = str(text)
    for s in secrets:
        if len(s) >= 6:
            out = out.replace(s, "[key hidden]")
    for pattern, repl in _SECRETS:
        out = pattern.sub(repl, out)
    out = " ".join(out.split())
    return out if len(out) <= 300 else out[:299] + "…"


def http_json(url: str, payload: dict | None = None, headers: dict | None = None, timeout: float = ROUND_TRIP_TIMEOUT):
    """(status, parsed body or {}, error text). Never raises for an HTTP error; a connection problem gives status 0."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "rectifi-dev", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return reply.status, json.loads(reply.read() or b"{}"), ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(body, list) and body and isinstance(body[0], dict):
            err = body[0].get("error")
        if isinstance(err, dict):
            err = err.get("message")
        return exc.code, body if isinstance(body, dict) else {}, str(err or raw or exc.reason)
    except TimeoutError:
        return 0, {}, f"no answer within {timeout:g} seconds"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return 0, {}, f"no answer within {timeout:g} seconds"
        return 0, {}, str(reason)


PING_TOOL = {"type": "function", "function": {"name": "ping", "description": "Reply to a ping.",
             "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}}}


def _tool_text(content: str) -> bool:
    text = re.sub(r"</?tool_call>|```(?:json)?", " ", content or "")
    decoder = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            data, _ = decoder.raw_decode(text[m.start():])
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        if any(isinstance(i, dict) and ("name" in i or "function" in i) for i in items):
            return True
    return False


def ollama_round_trip(base: str, model: str, timeout: float = ROUND_TRIP_TIMEOUT) -> tuple[str, str, str]:
    """A real /api/chat call with a tool. Returns (level, what, fix)."""
    status, body, err = http_json(f"{base}/api/chat", {
        "model": model, "stream": False, "tools": [PING_TOOL], "options": {"num_predict": 128},
        "messages": [{"role": "user", "content": "Call the ping tool with x=1."}]}, timeout=timeout)
    if status == 200:
        message = body.get("message") or {}
        if message.get("tool_calls"):
            return "ok", f"LLM: ollama answered a real tool call with {model}", ""
        if _tool_text(message.get("content") or ""):
            return ("warn", f"LLM: {model} wrote the tool call as text, not as a real call (the Ask page would fail)",
                    f"Use a general model for the chat, for example `ollama pull {DEFAULT_CHAT}` and RECTIFI_MODEL_CHAT={DEFAULT_CHAT}.")
        return "warn", f"LLM: {model} answered but did not call the tool", f"Use a model that is good at tool calling, for example {DEFAULT_CHAT}."
    if status == 404:
        return "warn", f"LLM: ollama has no model {model!r} (HTTP 404)", f"Run `ollama pull {model}`."
    if status == 400 and "support tools" in err.lower():
        return ("warn", f"LLM: {model} does not support tool calling (HTTP 400)",
                f"Use one that does: `ollama pull {DEFAULT_CHAT}`, then set RECTIFI_MODEL_CHAT in .env.")
    if status == 0:
        return ("warn", f"LLM: ollama round trip failed: {redact(err)}",
                "The first answer can take a minute while the model loads: run the check again. If Ollama is off, open the Ollama app.")
    return "warn", f"LLM: ollama answered HTTP {status}: {redact(err)}", "Check the Ollama log, and the model name in .env."


def openai_round_trip(provider: str, setting, timeout: float = ROUND_TRIP_TIMEOUT) -> tuple[str, str, str]:
    """A real POST {base}/chat/completions with a tool. Never prints the key."""
    key_name, model_name, model_default, base = OPENAI_STYLE[provider]
    key = setting(key_name)
    model = setting(ROLE_ENV["chat"]) or setting(model_name, model_default)
    base = base or setting("OPENAI_COMPAT_BASE_URL").rstrip("/")
    body = {"model": model, "temperature": 0, "max_tokens": 128, "tools": [PING_TOOL], "tool_choice": "auto",
            "messages": [{"role": "user", "content": "Call the ping tool with x=1."}]}
    if provider == "deepseek":
        body["thinking"] = {"type": "disabled"}
    status, reply, err = http_json(f"{base}/chat/completions", body, {"Authorization": f"Bearer {key}"}, timeout)
    shown = redact(err, (key,))
    if status == 200:
        try:
            message = reply["choices"][0]["message"] or {}
        except (KeyError, IndexError, TypeError):
            message = {}
        if message.get("tool_calls"):
            return "ok", f"LLM: {provider} answered a real tool call with {model}", ""
        if _tool_text(message.get("content") or ""):
            return "warn", f"LLM: {model} wrote the tool call as text (the Ask page would fail)", "Pick a model that supports tool calling."
        return "warn", f"LLM: {provider} answered, but {model} did not call the tool", "Pick a model that supports tool calling."
    low = err.lower()
    if any(m in low for m in ("api key not valid", "api_key_invalid", "access_token_type_unsupported")) or key.startswith("AQ."):
        return ("warn", f"LLM: {provider} rejected the key (HTTP {status}): {shown}",
                "Google AI Studio keys that start with AQ. often fail on the OpenAI-compatible endpoint. Make a classic key (AIza...) "
                "in the Google Cloud console, or use OpenRouter or Ollama.")
    advice = {401: "Check the key in .env.", 402: "Add credit, or pick a free model.", 403: "The key may not be allowed to use this model.",
              404: "The model id may be wrong, or the model can't call tools. For OpenRouter: `python scripts/dev.py free-models`.",
              429: "Rate limited. Wait a minute and run the check again."}.get(status, "Check the base URL, the model id and your internet connection.")
    where = f"HTTP {status}" if status else "no answer"
    return "warn", f"LLM: {provider} round trip failed ({where}): {shown}", advice


# ───────────── doctor ─────────────

def ollama_names(base: str) -> list[str] | None:
    status, body, _ = http_json(f"{base}/api/tags", timeout=3)
    return [m.get("name", "") for m in body.get("models", [])] if status == 200 else None


def have_model(wanted: str, names: list[str]) -> bool:
    return wanted in names or (":" not in wanted and wanted in {n.split(":")[0] for n in names})


def doctor_checks(setting, quick: bool = False, sandbox_check: bool = True) -> list[tuple[str, str, str]]:
    """Everything `dev.py doctor` says about models. `quick` makes no network calls."""
    out: list[tuple[str, str, str]] = []
    provider = setting("LLM_PROVIDER", "ollama").lower()
    chat = setting(ROLE_ENV["chat"]) or (setting("OLLAMA_MODEL", DEFAULT_CHAT) if provider == "ollama" else "")
    if provider == "ollama":
        base = setting("OLLAMA_BASE_URL", DEFAULT_BASE).rstrip("/")
        if quick:
            out.append(("ok", f"LLM: ollama, chat model {chat} (not tested: --quick)", ""))
        else:
            names = ollama_names(base)
            if names is None:
                out.append(("warn", f"LLM: ollama is not running at {base} (the Ask page will be off; the rest of Rectifi works)",
                            "Open the Ollama app, or set LLM_PROVIDER=openrouter or deepseek in .env."
                            + (" If you see long delays, try OLLAMA_BASE_URL=http://127.0.0.1:11434." if "localhost" in base else "")))
            else:
                for role, env in ROLE_ENV.items():
                    wanted = setting(env) or chat
                    if role != "chat" and not setting(env):
                        continue
                    if have_model(wanted, names):
                        out.append(("ok", f"LLM: {role} model {wanted} is downloaded", ""))
                    else:
                        out.append(("warn", f"LLM: {role} model {wanted} is not downloaded", f"Run `ollama pull {wanted}`."))
                if have_model(chat, names):
                    out.append(ollama_round_trip(base, chat))
                embed = setting("RECTIFI_EMBED_MODEL", DEFAULT_EMBED)
                if have_model(embed, names):
                    out.append(("ok", f"Embeddings: {embed} is downloaded", ""))
                else:
                    out.append(("warn", f"Embeddings: {embed} is not downloaded (the knowledge library will be off)", f"Run `ollama pull {embed}`."))
                status, body, _ = http_json(f"{base}/api/ps", timeout=3)
                if status == 200:
                    loaded = [m.get("name", "") for m in body.get("models", [])]
                    out.append(("ok", "Ollama has in memory: " + (", ".join(loaded) if loaded else "nothing (the first answer loads the model)"), ""))
    elif provider in OPENAI_STYLE:
        key_name, model_name, model_default, _base = OPENAI_STYLE[provider]
        model = setting(ROLE_ENV["chat"]) or setting(model_name, model_default)
        missing = [n for n, v in ((key_name, setting(key_name)), (model_name, model)) if not v]
        if provider == "openai_compat" and not setting("OPENAI_COMPAT_BASE_URL"):
            missing.append("OPENAI_COMPAT_BASE_URL")
        if missing:
            out.append(("warn", f"LLM: {provider}, but {', '.join(missing)} is empty (the Ask page will be off)", "Put " + ", ".join(missing) + " in .env."
                        + (" `python scripts/dev.py free-models` lists models you can use." if provider == "openrouter" else "")))
        elif quick:
            say = "API key is set, " if provider == "deepseek" else ""
            out.append(("ok", f"LLM: {provider}, {say}model {model}, key is set (not tested: --quick)" if not say else
                        f"LLM: {provider}, API key is set, model {model} (not tested: --quick)", ""))
        else:
            out.append(openai_round_trip(provider, setting))
        if provider == "huggingface":
            out.append(("warn", "LLM: Hugging Face is not free on a free account ($0 credit)",
                        "You need PRO or purchased credits, or the Ask page fails with HTTP 402. Ollama or OpenRouter :free cost nothing."))
    else:
        out.append(("warn", f"LLM_PROVIDER={provider!r} is not known (the Ask page will be off)",
                    "Use ollama, deepseek, openrouter, huggingface or openai_compat in .env."))

    ram, gpu = read_ram_gb(), has_nvidia()
    tier = pick_tier(ram, gpu)
    t = TIERS[tier]
    out.append(("ok", f"Machine: {f'{ram:.1f} GB RAM' if ram else 'RAM unknown'}{', NVIDIA GPU' if gpu else ''}. Suggested models ({t['label']}): "
                f"chat {t['chat']}, classify {t['classify']}, code {t['code']}, grade {t['grade']}, embed {t['embed']}. "
                "`python scripts/dev.py ollama-setup` downloads them.", ""))
    if setting("LANGSMITH_TRACING", "").lower() in ("1", "true", "yes", "on") or setting("LANGCHAIN_TRACING_V2", "").lower() in ("1", "true", "yes", "on"):
        if setting("RECTIFI_LANGSMITH", "").lower() in ("1", "true", "yes", "on"):
            out.append(("warn", "LangSmith tracing is ON: your questions and amounts are sent to the internet", "Remove RECTIFI_LANGSMITH from .env to turn it off."))
        else:
            out.append(("ok", "LangSmith tracing is set in the environment but RECTIFI_LANGSMITH is not 1, so Rectifi switches it off (nothing leaves the laptop)", ""))
    if sandbox_check:
        if platform.system() == "Windows":
            out.append(("warn", "Chart sandbox: Windows can't limit a program's memory with `resource`, so only the time limit applies",
                        "Fine for your own charts. Run on Linux or Docker for the full memory limit."))
        else:
            try:
                import resource

                resource.getrlimit(resource.RLIMIT_AS)
                out.append(("ok", "Chart sandbox: memory and time limits are supported on this system", ""))
            except (ImportError, ValueError, OSError):
                out.append(("warn", "Chart sandbox: this system can't set memory limits", "Only the time limit will apply."))
    return out


def llm_check(args: list[str]) -> None:
    quick = "--quick" in args
    setting = load_setting()
    marks = {"ok": "  ok   ", "warn": "  WARN ", "fail": "  FAIL "}
    print("Model check\n")
    worst = "ok"
    for level, what, fix in doctor_checks(setting, quick):
        print(f"{marks[level]} {what}" + (f"\n         fix: {fix}" if fix else ""))
        worst = "warn" if level == "warn" and worst == "ok" else worst
    if worst != "ok":
        print("\nThe model is not ready. Fix the WARN lines, restart Rectifi, and run this again.")


# ───────────── ollama-setup ─────────────

def run(cmd: list[str]) -> int:
    """Runs a command with a list of arguments (never a shell) and shows its output."""
    return subprocess.run(cmd, cwd=ROOT).returncode


def ollama_setup(args: list[str]) -> None:
    dry = "--dry-run" in args
    tier = None
    if "--tier" in args:
        i = args.index("--tier")
        tier = args[i + 1] if i + 1 < len(args) else ""
        if tier not in TIERS:
            sys.exit(f"--tier must be one of: {', '.join(TIERS)}.")
    ram = read_ram_gb()
    tier = tier or pick_tier(ram, has_nvidia())
    t = TIERS[tier]
    print(f"Machine: {f'{ram:.1f} GB RAM' if ram else 'RAM unknown'}. Tier: {tier} ({t['label']}).")
    pulls = list(dict.fromkeys([t["chat"], t["classify"], t["code"], t["grade"], t["embed"]]))
    creates = [(name, MODELFILES[name][0], t[MODELFILES[name][1]]) for name in MODELFILES]
    print("Will download:  " + ", ".join(pulls))
    print("Will create:    " + ", ".join(f"{n} (from {b})" for n, _f, b in creates))
    if dry:
        print("\n--dry-run: nothing was done. These .env lines go with it:\n  " + "\n  ".join(env_lines(tier)))
        return
    if shutil.which("ollama") is None:
        sys.exit("The `ollama` command was not found. Install Ollama from ollama.com, open it once, then run this again.")
    for model in pulls:
        if run(["ollama", "pull", model]) != 0:
            sys.exit(f"`ollama pull {model}` failed. Check that Ollama is running and that you have internet, then run this again.")
    for name, filename, base in creates:
        text = render_modelfile((DEPLOY / filename).read_text(encoding="utf-8"), base)
        parse_modelfile(text)                                    # refuse to build from a broken file
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / filename
            target.write_text(text, encoding="utf-8")
            if run(["ollama", "create", name, "-f", str(target)]) != 0:
                sys.exit(f"`ollama create {name}` failed.")
    print("\nDone. Put these lines in .env, then restart Rectifi:\n  " + "\n  ".join(env_lines(tier)))
    print("Then check it with:  python scripts/dev.py llm-check")


# ───────────── classify / eval passthroughs ─────────────

def project_python() -> str:
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        if (ROOT / rel).exists():
            return str(ROOT / rel)
    return sys.executable


CLASSIFY_SNIPPET = """
import sys
from rectifi import tools
from rectifi.agent import jobs
from rectifi.agent.classify import classify_pending
from rectifi.db import init_db

init_db()
raw = sys.argv[1] if len(sys.argv) > 1 else ''
if raw:
    print(classify_pending(int(raw), progress=lambda *a: print('...', *a, flush=True)))
else:
    waiting = tools.queue_miscellaneous_resort()
    print(f'{waiting} entries are waiting for a category.')
    print(classify_pending(None, progress=lambda *a: print('...', *a, flush=True)))
"""


def classify(args: list[str]) -> None:
    """With a batch number: sort that import. Without: mark every undecided Miscellaneous entry and sort them all."""
    batch = next((a for a in args if a.isdigit()), "")
    code = subprocess.run([project_python(), "-c", CLASSIFY_SNIPPET, batch], cwd=ROOT).returncode
    if code:
        sys.exit(code)


def run_eval(args: list[str]) -> None:
    which = next((a for a in args if not a.startswith("-")), "classify")
    if which not in ("classify", "charts", "rag"):
        sys.exit("eval takes one of: classify, charts, rag.")
    code = subprocess.run([project_python(), "-m", "rectifi.evals", which, *[a for a in args if a != which]], cwd=ROOT).returncode
    if code:
        sys.exit(code)


# dev.py reads this: command name -> (function, one-line help)
COMMANDS = {
    "llm-check": (llm_check, "test the model with a real call, and suggest models for this machine (--quick skips the network)"),
    "ollama-setup": (ollama_setup, "pull the models for this laptop and create the Rectifi Modelfile models (--tier, --dry-run)"),
    "classify": (classify, "sort the payments still waiting for a category"),
    "eval": (run_eval, "measure the classifier, charts and knowledge answers on the files in evals/"),
}
