"""One switch for the model. Nothing else in Rectifi knows which provider is behind the agent.

The agent needs a model that supports tool calling. Providers:
    ollama         free, local
    deepseek       paid cloud, cheap
    openrouter     cloud; models whose id ends in ":free" cost nothing but are rate limited
    huggingface    cloud; free accounts have $0 of credit, so this needs PRO or bought credits
    openai_compat  anything else that speaks the OpenAI chat API (Groq, LM Studio, vLLM ...)
"""

from __future__ import annotations

import httpx

from .. import config

REQUEST_TIMEOUT = 60      # seconds to wait for one model reply
MAX_RETRIES = 2

HF_CREDITS_NOTE = ("Hugging Face is not free on a free account: free accounts get $0 of Inference Providers credit. "
                   "You need PRO ($2 of credit a month) or purchased credits, or every call fails with HTTP 402.")

_FREE_MODELS_HINT = "Run `python scripts/dev.py free-models` to list free models that support tools, then put one in .env."


def get_llm():
    provider = config.llm_provider()
    if provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        # Thinking mode is DeepSeek's default. With tools, thinking mode requires sending every earlier
        # reasoning_content back (else HTTP 400) and it costs extra output tokens — so it's off.
        return ChatDeepSeek(
            model=config.deepseek_model(),
            temperature=0,
            max_retries=2,
            extra_body={"thinking": {"type": "disabled"}},
        )
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(model=config.ollama_model(), temperature=0, base_url=config.ollama_base_url())
    if provider in config.OPENAI_STYLE:
        from langchain_openai import ChatOpenAI

        s = config.openai_style_settings(provider)
        return ChatOpenAI(
            model=s["model"],
            base_url=s["base_url"],
            api_key=s["api_key"],
            temperature=0,
            timeout=REQUEST_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
    raise ValueError(f"Unknown LLM_PROVIDER {provider!r}. Use one of: {', '.join(config.PROVIDERS)}.")


def describe() -> str:
    provider = config.llm_provider()
    if provider == "deepseek":
        model = config.deepseek_model()
    elif provider == "ollama":
        model = config.ollama_model()
    elif provider in config.OPENAI_STYLE:
        model = config.openai_style_settings(provider)["model"] or "(no model set)"
    else:
        model = "?"
    return f"{provider} · {model}"


def _openai_style_status(provider: str) -> tuple[bool, str]:
    s = config.openai_style_settings(provider)
    if provider == "openrouter":
        if not s["api_key"]:
            return False, ("OPENROUTER_API_KEY isn't set. Make a free key at openrouter.ai/keys, put it in .env as "
                           "OPENROUTER_API_KEY=..., and restart the app. " + _FREE_MODELS_HINT)
        if not s["model"]:
            return False, ("OPENROUTER_MODEL isn't set. " + _FREE_MODELS_HINT +
                           " Example line in .env: OPENROUTER_MODEL=<an id ending in :free>. Then restart the app.")
        return True, ""
    if provider == "huggingface":
        if not s["api_key"]:
            return False, ("HF_TOKEN isn't set. At huggingface.co/settings/tokens make a fine-grained token with "
                           "\"Make calls to Inference Providers\", put it in .env as HF_TOKEN=..., and restart the app. "
                           + HF_CREDITS_NOTE)
        return True, ""            # the credits note is shown by llm_notice(), not as a blocker
    # openai_compat
    if not s["base_url"]:
        return False, ("OPENAI_COMPAT_BASE_URL isn't set. Put the provider's base URL in .env, for example "
                       "OPENAI_COMPAT_BASE_URL=https://api.groq.com/openai/v1, and restart the app.")
    if not s["api_key"]:
        return False, "OPENAI_COMPAT_API_KEY isn't set. Put the provider's key in .env and restart the app."
    if not s["model"]:
        return False, ("OPENAI_COMPAT_MODEL isn't set. Put a model id that supports tool calling in .env "
                       "(for Groq, for example, llama-3.3-70b-versatile) and restart the app.")
    return True, ""


def llm_status() -> tuple[bool, str]:
    """Can the assistant work right now? Returns (ok, why-not). Quick: never waits more than about 2 seconds."""
    provider = config.llm_provider()
    if provider == "deepseek":
        if not config.deepseek_api_key():
            return False, ("DEEPSEEK_API_KEY isn't set. Add it to .env (see .env.example) and restart the app, "
                           "or set LLM_PROVIDER=ollama to use a free local model.")
        return True, ""
    if provider in config.OPENAI_STYLE:
        return _openai_style_status(provider)
    if provider == "ollama":
        base = config.ollama_base_url()
        try:
            reply = httpx.get(f"{base}/api/tags", timeout=1.5)
            reply.raise_for_status()
            installed = [m.get("name", "") for m in reply.json().get("models", [])]
        except (httpx.HTTPError, ValueError):
            return False, (f"Ollama isn't running at {base}. Start it (open the Ollama app, or run `ollama serve`), "
                           "or set LLM_PROVIDER=openrouter or deepseek in .env.")
        wanted = config.ollama_model()
        # "qwen2.5:7b" must be installed exactly; plain "llama3" matches "llama3:latest" (the name before the colon).
        have = wanted in installed or (":" not in wanted and wanted in {n.split(":")[0] for n in installed})
        if not have:
            return False, f"Ollama is running but the model {wanted!r} isn't downloaded. Run `ollama pull {wanted}`."
        return True, ""
    return False, f"Unknown LLM_PROVIDER {provider!r}. Use one of: {', '.join(config.PROVIDERS)} in .env."


def llm_notice() -> str:
    """A warning to show even when llm_status() is OK (empty if there is nothing to warn about)."""
    provider = config.llm_provider()
    if provider == "huggingface":
        return HF_CREDITS_NOTE
    if provider == "openrouter":
        return ("OpenRouter free models allow about 20 requests a minute and 50 a day. Your messages go to OpenRouter "
                "and to whoever hosts the model.")
    return ""


def explain_error(exc: Exception) -> str:
    """Turn a failed model call into one plain sentence of advice. Never includes keys."""
    code = getattr(exc, "status_code", None)
    provider = config.llm_provider()
    if code == 401:
        return "The provider rejected the API key (HTTP 401). Check the key in .env and restart the app."
    if code == 402:
        if provider == "huggingface":
            return "Hugging Face says you are out of credits (HTTP 402). " + HF_CREDITS_NOTE
        return "The provider says you are out of credit (HTTP 402). Add credit, or switch model in .env."
    if code == 429:
        if provider == "openrouter":
            return ("OpenRouter's free limit was hit (HTTP 429): about 20 requests a minute and 50 a day. "
                    "Wait a minute, try again tomorrow, or use Ollama.")
        return "The provider is rate limiting you (HTTP 429). Wait a minute and try again."
    if code == 404 and provider in config.OPENAI_STYLE:
        return ("The provider says this model isn't available for tool calling (HTTP 404). Pick another model. "
                + (_FREE_MODELS_HINT if provider == "openrouter" else ""))
    return str(exc)
