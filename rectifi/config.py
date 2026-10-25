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
