"""All settings come from environment variables (or a .env file next to the project)."""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def database_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://rectifi:rectifi_dev@localhost:5432/rectifi")


# Every provider the Ask page understands. The last three speak the OpenAI chat API ("OpenAI-compatible").
PROVIDERS = ("ollama", "deepseek", "openrouter", "huggingface", "openai_compat")
OPENAI_STYLE = ("openrouter", "huggingface", "openai_compat")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
HUGGINGFACE_BASE_URL = "https://router.huggingface.co/v1"
HUGGINGFACE_DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3.1:cheapest"


def llm_provider() -> str:
    # one of PROVIDERS
    return os.getenv("LLM_PROVIDER", "ollama").strip().lower()


def deepseek_model() -> str:
    return os.getenv("DEEPSEEK_MODEL", "deepseek-flash")


def ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", "qwen2.5:7b")


def ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")


def deepseek_api_key() -> str | None:
    return os.getenv("DEEPSEEK_API_KEY") or None


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def openai_style_settings(provider: str) -> dict[str, str]:
    """base_url, api key and model for an OpenAI-compatible provider. Empty string means "not set"."""
    if provider == "openrouter":
        return {"base_url": OPENROUTER_BASE_URL, "api_key": _env("OPENROUTER_API_KEY"), "model": _env("OPENROUTER_MODEL")}
    if provider == "huggingface":
        return {"base_url": HUGGINGFACE_BASE_URL, "api_key": _env("HF_TOKEN"),
                "model": _env("HF_MODEL") or HUGGINGFACE_DEFAULT_MODEL}
    if provider == "openai_compat":
        return {"base_url": _env("OPENAI_COMPAT_BASE_URL").rstrip("/"), "api_key": _env("OPENAI_COMPAT_API_KEY"),
                "model": _env("OPENAI_COMPAT_MODEL")}
    raise ValueError(f"{provider!r} is not an OpenAI-compatible provider.")


# ───────────── phone SMS gateway (off unless you turn it on; see README, "Auto-capture bank SMS") ─────────────

DEFAULT_SMS_PORT = 8765
DEFAULT_SMS_HOST = "127.0.0.1"


def sms_gateway_enabled() -> bool:
    """RECTIFI_SMS_GATEWAY=1 in .env makes `dev.py up` start the gateway too."""
    return os.getenv("RECTIFI_SMS_GATEWAY", "").strip().lower() in ("1", "true", "yes", "on")


def sms_port() -> int:
    """The port the gateway listens on (RECTIFI_SMS_PORT, default 8765). A value that is not a port number falls back to the default."""
    try:
        port = int(os.getenv("RECTIFI_SMS_PORT", "").strip() or DEFAULT_SMS_PORT)
    except ValueError:
        return DEFAULT_SMS_PORT
    return port if 1 <= port <= 65535 else DEFAULT_SMS_PORT


def sms_host() -> str:
    """The address the gateway listens on (RECTIFI_SMS_HOST). The default, 127.0.0.1, means this laptop only: the phone
    reaches it through `tailscale serve` (HTTPS, only your own devices). 0.0.0.0 opens it to the whole Wi-Fi: an advanced
    option, protected only by the device token. Anything that is not an IP address falls back to the safe default."""
    import ipaddress

    value = os.getenv("RECTIFI_SMS_HOST", "").strip() or DEFAULT_SMS_HOST
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return DEFAULT_SMS_HOST
    return value


def sms_host_is_open() -> bool:
    """True when the gateway listens beyond this laptop (anything but a loopback address)."""
    import ipaddress

    return not ipaddress.ip_address(sms_host()).is_loopback


# ───────────── next phase: model roles, Ollama options, hosting and tunables ─────────────
# Roles name WHICH model does a job. `agent/llm.get_llm(role)` reads them. With a non-Ollama provider a role variable
# names a model for that provider; if it is empty the provider's own model setting is used.

ROLES = ("chat", "classify", "code", "grade")
ROLE_ENV = {
    "chat": "RECTIFI_MODEL_CHAT",          # the Ask agent and the chat (needs tool calling). Default: OLLAMA_MODEL
    "classify": "RECTIFI_MODEL_CLASSIFY",  # sorts payments into categories (small and fast is fine). Default: the chat model
    "code": "RECTIFI_MODEL_CODE",          # writes chart code. Default: the chat model
    "grade": "RECTIFI_MODEL_GRADE",        # graders and the router in the knowledge Q&A. Default: the chat model
}
EMBED_MODEL_ENV = "RECTIFI_EMBED_MODEL"
DEFAULT_EMBED_MODEL = "nomic-embed-text"


def explicit_role_model(role: str) -> str:
    """The model the person named for this role (its own variable, else RECTIFI_MODEL_CHAT), or "" if they named none."""
    if role not in ROLES:
        raise ValueError(f"Unknown model role {role!r}. Use one of: {', '.join(ROLES)}.")
    return _env(ROLE_ENV[role]) or (_env(ROLE_ENV["chat"]) if role != "chat" else "")


def role_model(role: str) -> str:
    """The Ollama model name for a role: the explicit one, else OLLAMA_MODEL."""
    return explicit_role_model(role) or ollama_model()


def embed_model() -> str:
    return _env(EMBED_MODEL_ENV) or DEFAULT_EMBED_MODEL


def _int_env(name: str, default: int, low: int = 1, high: int = 10**9) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if low <= value <= high else default


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def ollama_num_ctx() -> int:
    """Context window sent to Ollama. Its own default is small, and a short window silently drops the tool list."""
    return _int_env("OLLAMA_NUM_CTX", 8192, low=512)


def ollama_timeout() -> int:
    """Seconds to wait for one Ollama reply. A laptop CPU can take a minute to load a model, so the default is generous."""
    return _int_env("OLLAMA_TIMEOUT", 120, low=5)


def ollama_keep_alive() -> str:
    """How long Ollama keeps the model in memory after a reply (for example 30m)."""
    return _env("OLLAMA_KEEP_ALIVE") or "30m"


def ollama_keep_alive_seconds() -> int:
    """OLLAMA_KEEP_ALIVE as whole seconds (OllamaEmbeddings wants a number): 30m, 2h, 90s or 600. Unreadable means 30 minutes."""
    raw = ollama_keep_alive().lower()
    unit = {"s": 1, "m": 60, "h": 3600}.get(raw[-1:], None)
    try:
        return int(float(raw[:-1]) * unit) if unit else int(float(raw))
    except ValueError:
        return 1800


# Hosting. Both default to "this laptop only"; see CLAUDE.md rule 11.
DEFAULT_BIND_HOST = "127.0.0.1"


def bind_host() -> str:
    """The address the web app listens on (RECTIFI_BIND_HOST). Only the Docker image sets 0.0.0.0. Anything that is not an
    IP address falls back to 127.0.0.1."""
    import ipaddress

    value = _env("RECTIFI_BIND_HOST") or DEFAULT_BIND_HOST
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return DEFAULT_BIND_HOST
    return value


def public_mode() -> bool:
    """True when the app is reachable from beyond this laptop: RECTIFI_PUBLIC=1, or a bind host that is not loopback.
    Public mode needs its own login (RECTIFI_PASSWORD_HASH and RECTIFI_SESSION_SECRET)."""
    import ipaddress

    return _flag("RECTIFI_PUBLIC") or not ipaddress.ip_address(bind_host()).is_loopback


def web_search() -> bool:
    """Optional web search for the knowledge Q&A (RECTIFI_WEB_SEARCH=1). Off by default: your questions would leave the laptop."""
    return _flag("RECTIFI_WEB_SEARCH")


def langsmith_optin() -> bool:
    """LangSmith tracing sends run data to the internet, so it runs only when RECTIFI_LANGSMITH=1 is set on purpose."""
    return _flag("RECTIFI_LANGSMITH")


def auto_classify() -> bool:
    """Sort new imports with the model right after they land (RECTIFI_AUTO_CLASSIFY, default on). The fixed layers always run."""
    raw = os.getenv("RECTIFI_AUTO_CLASSIFY", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


# Classifier tunables (percent scores, whole numbers).
CLASSIFY_APPLY_MIN = _int_env("RECTIFI_CLASSIFY_APPLY_MIN", 80, 0, 100)     # at or above: store the category
CLASSIFY_SUGGEST_MIN = _int_env("RECTIFI_CLASSIFY_SUGGEST_MIN", 50, 0, 100) # at or above (but below apply): show as a suggestion
CLASSIFY_BATCH_SIZE = _int_env("RECTIFI_CLASSIFY_BATCH", 12, 1, 100)        # payments per model call

# Chart tunables.
CHART_MAX_ATTEMPTS = _int_env("RECTIFI_CHART_ATTEMPTS", 3, 1, 6)            # write -> validate -> run -> check, at most this many times
CHART_TIMEOUT = _int_env("RECTIFI_CHART_TIMEOUT", 30, 5, 300)              # seconds the sandboxed child may run
CHART_MEMORY_MB = _int_env("RECTIFI_CHART_MEMORY_MB", 512, 64, 8192)

# Knowledge Q&A tunables.
RAG_TOP_K = _int_env("RECTIFI_RAG_TOP_K", 4, 1, 20)
RAG_CHUNK_CHARS = _int_env("RECTIFI_RAG_CHUNK_CHARS", 900, 200, 4000)
RAG_CHUNK_OVERLAP = _int_env("RECTIFI_RAG_CHUNK_OVERLAP", 120, 0, 1000)
RAG_MAX_REGENERATE = _int_env("RECTIFI_RAG_MAX_REGENERATE", 2, 0, 5)        # Self-RAG: rewrite an unsupported answer at most this often
RAG_MAX_RETRIEVE = _int_env("RECTIFI_RAG_MAX_RETRIEVE", 2, 0, 5)            # CRAG: re-retrieve at most this often

# Traces.
TRACE_RETENTION_DAYS = _int_env("RECTIFI_TRACE_DAYS", 14, 1, 365)
TRACE_MAX_ROWS = _int_env("RECTIFI_TRACE_ROWS", 20_000, 100, 10**7)
TRACE_PREVIEW_CHARS = 2048


def enforce_langsmith_privacy() -> bool:
    """LangSmith would send runs (your questions and amounts) to the internet. If LANGSMITH_TRACING is on in the environment
    but RECTIFI_LANGSMITH is not 1, switch it off for this process. Returns True when it had to switch something off."""
    if langsmith_optin():
        return False
    names = [n for n in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING")
             if os.getenv(n, "").strip().lower() in ("1", "true", "yes", "on")]
    for name in names:
        os.environ[name] = "false"
    if names:
        import logging

        logging.getLogger("rectifi").warning(
            "LANGSMITH_TRACING was on, but RECTIFI_LANGSMITH is not 1, so tracing was switched off: it would send your data online.")
    return bool(names)
