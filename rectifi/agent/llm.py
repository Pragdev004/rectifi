"""One switch for the model. Nothing else in Rectifi knows which provider is behind the agent.

The agent needs a model that supports tool calling. Providers:
    ollama         free, local
    deepseek       paid cloud, cheap
    openrouter     cloud; models whose id ends in ":free" cost nothing but are rate limited
    huggingface    cloud; free accounts have $0 of credit, so this needs PRO or bought credits
    openai_compat  anything else that speaks the OpenAI chat API (Groq, LM Studio, vLLM ...)
"""

from __future__ import annotations

import json
import re
import threading
import time

import httpx

from .. import config
from ..privacy import mask_sensitive

REQUEST_TIMEOUT = 60      # seconds to wait for one model reply
MAX_RETRIES = 2

HF_CREDITS_NOTE = ("Hugging Face is not free on a free account: free accounts get $0 of Inference Providers credit. "
                   "You need PRO ($2 of credit a month) or purchased credits, or every call fails with HTTP 402.")

_FREE_MODELS_HINT = "Run `python scripts/dev.py free-models` to list free models that support tools, then put one in .env."


def get_llm(role: str = "chat"):
    """The chat model for a job. `role` is one of config.ROLES: chat (the Ask agent), classify, code (chart code), grade
    (graders and the router). A role with no model named in .env uses the chat model. With a non-Ollama provider the role
    variable names a model for THAT provider; empty means the provider's own model setting."""
    provider = config.llm_provider()
    named = config.explicit_role_model(role)
    if provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        # Thinking mode is DeepSeek's default. With tools, thinking mode requires sending every earlier
        # reasoning_content back (else HTTP 400) and it costs extra output tokens — so it's off.
        return ChatDeepSeek(
            model=named or config.deepseek_model(),
            temperature=0,
            max_retries=2,
            extra_body={"thinking": {"type": "disabled"}},
        )
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        model = named or config.ollama_model()
        options: dict = {}
        if model.lower().startswith("qwen3"):
            options["reasoning"] = False          # qwen3 "thinks" by default: slow, and it breaks tool calls on small models
        return ChatOllama(
            model=model, temperature=0, base_url=config.ollama_base_url(),
            num_ctx=config.ollama_num_ctx(), keep_alive=config.ollama_keep_alive(),
            client_kwargs={"timeout": config.ollama_timeout()}, **options,
        )
    if provider in config.OPENAI_STYLE:
        from langchain_openai import ChatOpenAI

        s = config.openai_style_settings(provider)
        return ChatOpenAI(
            model=named or s["model"],
            base_url=s["base_url"],
            api_key=s["api_key"],
            temperature=0,
            timeout=REQUEST_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
    raise ValueError(f"Unknown LLM_PROVIDER {provider!r}. Use one of: {', '.join(config.PROVIDERS)}.")


def get_embeddings():
    """Embeddings for the knowledge library. Ollama only (nomic-embed-text by default, RECTIFI_EMBED_MODEL to change).
    Other providers have no embeddings here: the knowledge library is off and the classifier still works without it."""
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(
        model=config.embed_model(), base_url=config.ollama_base_url(),
        keep_alive=config.ollama_keep_alive_seconds(), client_kwargs={"timeout": config.ollama_timeout()},
    )


def model_name(role: str = "chat") -> str:
    """The model id this role will use with the configured provider ("" when none is set)."""
    provider = config.llm_provider()
    named = config.explicit_role_model(role)
    if provider == "deepseek":
        return named or config.deepseek_model()
    if provider == "ollama":
        return config.role_model(role)
    if provider in config.OPENAI_STYLE:
        return named or config.openai_style_settings(provider)["model"]
    return ""


def describe(role: str = "chat") -> str:
    provider = config.llm_provider()
    if provider == "deepseek" or provider == "ollama":
        model = model_name(role)
    elif provider in config.OPENAI_STYLE:
        model = model_name(role) or "(no model set)"
    else:
        model = "?"
    return f"{provider} · {model}"


def _openai_style_status(provider: str, role: str = "chat") -> tuple[bool, str]:
    s = dict(config.openai_style_settings(provider))
    s["model"] = config.explicit_role_model(role) or s["model"]      # a role variable names the model, so "no model set" no longer applies
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


def installed_match(wanted: str, installed: list[str]) -> str | None:
    """The installed Ollama model that `wanted` means, or None. "qwen2.5:7b" must be installed exactly; a plain "llama3"
    matches "llama3:latest" (the name before the colon)."""
    if wanted in installed:
        return wanted
    if ":" not in wanted:
        return next((n for n in installed if n.split(":")[0] == wanted), None)
    return None


def llm_status(role: str = "chat") -> tuple[bool, str]:
    """Can the assistant work right now? Returns (ok, why-not). Quick: never waits more than about 2 seconds.
    `role` is the model job (chat, classify, code, grade); a role with no model of its own uses the chat model."""
    provider = config.llm_provider()
    if provider == "deepseek":
        if not config.deepseek_api_key():
            return False, ("DEEPSEEK_API_KEY isn't set. Add it to .env (see .env.example) and restart the app, "
                           "or set LLM_PROVIDER=ollama to use a free local model.")
        return True, ""
    if provider in config.OPENAI_STYLE:
        return _openai_style_status(provider, role)
    if provider == "ollama":
        base = config.ollama_base_url()
        try:
            reply = httpx.get(f"{base}/api/tags", timeout=1.5)
            reply.raise_for_status()
            installed = [m.get("name", "") for m in reply.json().get("models", [])]
        except (httpx.HTTPError, ValueError):
            return False, (f"Ollama isn't running at {base}. Start it (open the Ollama app, or run `ollama serve`), "
                           "or set LLM_PROVIDER=openrouter or deepseek in .env.")
        wanted = config.role_model(role)
        if installed_match(wanted, installed) is None:
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
    """Turn a failed model call into one plain sentence of advice. Never includes keys. (Written in the WS-LLM block below.)"""
    return _explain_error(exc)


# ═══ WS-LLM: begin ═══
# Model diagnostics: redact(), the explain_error v2 helpers, capabilities() and probe().
# Nothing here prints or returns an API key: every provider message goes through redact() and mask_sensitive().

SUGGESTED_TOOL_MODELS = ("qwen2.5:7b", "llama3.1:8b", "llama3.2:3b")      # Ollama models that support tool calling
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MESSAGE_LIMIT = 300
PROBE_CACHE_SECONDS = 60
MIN_GOOD_CTX = 8192

_SECRETS = (
    (re.compile(r"Bearer\s+\S+", re.I), "Bearer [hidden]"),
    (re.compile(r"\bAQ\.[\w-]+"), "[key hidden]"),
    (re.compile(r"\bAIza[\w-]{20,}"), "[key hidden]"),
    (re.compile(r"\bsk-[\w-]+"), "[key hidden]"),
    (re.compile(r"\bhf_\w+"), "[key hidden]"),
    (re.compile(r"([?&]key=)[^&\s\"']+", re.I), r"\1[hidden]"),
)


def _configured_secrets() -> list[str]:
    values = [config.deepseek_api_key() or ""]
    values += [config.openai_style_settings(p)["api_key"] for p in config.OPENAI_STYLE]
    return [v for v in values if len(v) >= 6]


def redact(text: object) -> str:
    """Hide anything that looks like an API key (and the keys in .env), then mask account numbers. Safe to show."""
    out = str(text)
    for secret in _configured_secrets():
        out = out.replace(secret, "[key hidden]")
    for pattern, repl in _SECRETS:
        out = pattern.sub(repl, out)
    return mask_sensitive(out)


def _clip(text: object, limit: int = MESSAGE_LIMIT) -> str:
    out = " ".join(redact(text).split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


# ───────────── explain_error v2 ─────────────

_AQ_MARKERS = ("api key not valid", "api_key_invalid", "access_token_type_unsupported")
_NOT_FOUND_WORDS = re.compile(r"not found|does not exist|unknown model|no such model|invalid model", re.I)


def _chain(exc: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    while exc is not None and exc not in found and len(found) < 4:
        found.append(exc)
        exc = exc.__cause__ or exc.__context__
    return found


def _class_names(exc: BaseException) -> set[str]:
    return {c.__name__ for e in _chain(exc) for c in type(e).__mro__}


def _status_of(exc: BaseException) -> int | None:
    for e in _chain(exc):
        code = getattr(e, "status_code", None)
        if code is None:
            code = getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _body_message(data: object) -> str:
    """The human sentence in an error body: Ollama {"error": "..."}, OpenAI {"error": {"message": "..."}}, Google [{...}]."""
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error", data.get("message"))
        if isinstance(err, dict):
            err = err.get("message") or err.get("status") or err.get("code")
        if err:
            return str(err)
    return ""


def _message_of(exc: BaseException) -> str:
    for e in _chain(exc):
        for attr in ("error", "body"):                      # ollama.ResponseError.error, openai.APIStatusError.body
            found = _body_message(getattr(e, attr, None)) if attr == "body" else getattr(e, attr, None)
            if isinstance(found, str) and found:
                return found
        if getattr(e, "message", None) and isinstance(e.message, str):
            return e.message
    return str(exc)


def _configured_key() -> str:
    provider = config.llm_provider()
    if provider == "deepseek":
        return config.deepseek_api_key() or ""
    return config.openai_style_settings(provider)["api_key"] if provider in config.OPENAI_STYLE else ""


def _advice(code: int | None, message: str, provider: str) -> str | None:
    """Plain advice for an HTTP status plus the provider's own words, or None when there is nothing better than the message."""
    low = message.lower()
    if any(m in low for m in _AQ_MARKERS):
        lead = ("Google rejected the API key" + (" (it starts with AQ.)" if _configured_key().startswith("AQ.") else "") +
                (f" (HTTP {code})" if code else "") + ". ")
        return (lead + "Google AI Studio now issues keys that start with AQ., and people report they do not work on the OpenAI-compatible "
                "endpoint. Make a classic key (it starts with AIza) in the Google Cloud console under APIs & Services > Credentials, "
                "with the Generative Language API turned on, or use OpenRouter or Ollama. Then restart the app.")
    if code == 401:
        return "The provider rejected the API key (HTTP 401). Check the key in .env and restart the app."
    if code == 402:
        if provider == "huggingface":
            return "Hugging Face says you are out of credits (HTTP 402). " + HF_CREDITS_NOTE
        return "The provider says you are out of credit (HTTP 402). Add credit, or switch model in .env."
    if code == 403:
        return ("The provider refused this request (HTTP 403). The key may not be allowed to use this model, the model may need "
                "access approval, or your country may be blocked. Check the key's settings at the provider, or pick another model.")
    if code == 404:
        if provider == "ollama":
            named = re.search(r'model\s+"?([\w.:/-]+)"?\s+not found', message)
            wanted = named.group(1) if named else config.role_model("chat")
            return f"Ollama doesn't have the model {wanted!r} (HTTP 404). Run `ollama pull {wanted}`, then try again."
        extra = " " + _FREE_MODELS_HINT if provider == "openrouter" else ""
        if "tool" in low:
            return "This model can't be used with tool calling (HTTP 404), and the Ask page needs that. Pick another model." + extra
        if _NOT_FOUND_WORDS.search(message):
            return "The provider doesn't know this model id (HTTP 404). Check the spelling in .env and restart the app." + extra
        return ("The provider says this model isn't available for tool calling (HTTP 404). Either the id is wrong or the model "
                "can't call tools. Pick another model." + extra)
    if code == 429:
        if provider == "openrouter":
            return ("OpenRouter's free limit was hit (HTTP 429): about 20 requests a minute and 50 a day. "
                    "Wait a minute, try again tomorrow, or use Ollama.")
        return "The provider is rate limiting you (HTTP 429). Wait a minute and try again."
    if code == 400 and "support tools" in low:
        return ("This model does not support tool calling (HTTP 400), and the Ask page needs it. Use a model that does, for example "
                + ", ".join(f"`ollama pull {m}`" for m in SUGGESTED_TOOL_MODELS[:2]) + ", then set RECTIFI_MODEL_CHAT in .env.")
    if code is not None and code >= 500:
        return f"The provider had a problem on its side (HTTP {code}). Wait a minute and try again."
    return None


def _explain_error(exc: BaseException) -> str:
    provider = config.llm_provider()
    names = _class_names(exc)
    if any("Timeout" in n for n in names) or isinstance(exc, TimeoutError):
        if provider == "ollama":
            return (f"The model didn't answer within {config.ollama_timeout()} seconds. The first answer after Ollama starts can take "
                    "a minute or more on a laptop CPU while the model loads. Try again; if it keeps happening, use a smaller model "
                    "or raise OLLAMA_TIMEOUT in .env.")
        return f"The provider didn't answer in time ({REQUEST_TIMEOUT} seconds). Try again in a minute, or pick another model."
    if names & {"ConnectError", "APIConnectionError", "ConnectionError", "ConnectionRefusedError"}:
        if provider == "ollama":
            base = config.ollama_base_url()
            return (f"Can't reach Ollama at {base}. Open the Ollama app (or run `ollama serve`). "
                    + ("On Windows, `localhost` can be slow: try OLLAMA_BASE_URL=http://127.0.0.1:11434 in .env. " if "localhost" in base else "")
                    + "Restart Rectifi after you change .env.")
        return "Can't reach the provider. Check your internet connection and the base URL in .env."
    message = _message_of(exc)
    advice = _advice(_status_of(exc), message + " " + str(exc), provider)
    return advice or _clip(message)


# ───────────── capabilities and probe ─────────────

def capabilities(model: str | None = None, timeout: float = 5) -> list[str] | None:
    """What Ollama says the model can do (for example ["completion", "tools"]), or None when it can't be read (not Ollama,
    Ollama is down, the model isn't installed, or an older Ollama that doesn't list capabilities)."""
    if config.llm_provider() != "ollama":
        return None
    model = model or config.role_model("chat")
    try:
        reply = httpx.post(f"{config.ollama_base_url()}/api/show", json={"model": model}, timeout=timeout)
        reply.raise_for_status()
        caps = reply.json().get("capabilities")
    except (httpx.HTTPError, ValueError):
        return None
    return [str(c) for c in caps] if isinstance(caps, list) else None


_probe_cache: dict[tuple, tuple[float, dict]] = {}
_probe_lock = threading.Lock()
_PING_TOOL = {"type": "function", "function": {
    "name": "ping", "description": "Reply to a ping.",
    "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}}}


def clear_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


def _target(role: str) -> dict:
    provider = config.llm_provider()
    if provider == "ollama":
        return {"provider": provider, "model": config.role_model(role), "base": config.ollama_base_url(), "key": ""}
    if provider == "deepseek":
        return {"provider": provider, "model": model_name(role), "base": DEEPSEEK_BASE_URL, "key": config.deepseek_api_key() or ""}
    s = config.openai_style_settings(provider)
    return {"provider": provider, "model": model_name(role), "base": s["base_url"], "key": s["api_key"]}


def cached_probe(role: str = "chat") -> dict | None:
    """The last probe for this role if it is under a minute old (never makes a call)."""
    with _probe_lock:
        for (provider, base, model, _tools), (at, result) in _probe_cache.items():
            t = _target(role)
            if (provider, base, model) == (t["provider"], t["base"], t["model"]) and time.monotonic() - at < PROBE_CACHE_SECONDS:
                return {**result, "cached": True}
    return None


class _Run:
    """Collects the stages of one probe, then builds the result dict."""

    def __init__(self, target: dict, role: str, timeout: float) -> None:
        self.target, self.role, self.timeout = target, role, timeout
        self.stages: list[dict] = []
        self.hints: list[str] = []
        self.tools_ok: bool | None = None
        self.tools_as_text = False
        self.chat_ms: int | None = None

    def passed(self, name: str, ms: int | None = None, detail: str = "") -> None:
        self.stages.append({"name": name, "ok": True, "ms": ms, "detail": _clip(detail, 160)})

    def result(self, stage: str, ok: bool, http_status: int | None = None, message: str = "") -> dict:
        if not ok:
            self.stages.append({"name": stage, "ok": False, "ms": None, "detail": _clip(message, 160)})
        seen = list(dict.fromkeys(self.hints))
        return {"ok": ok, "stage": stage, "http_status": http_status, "provider_message": _clip(message) if message else "",
                "latency_ms": self.chat_ms, "tools_ok": self.tools_ok, "tools_as_text": self.tools_as_text, "hints": seen,
                "provider": self.target["provider"], "model": self.target["model"], "role": self.role, "stages": self.stages,
                "cached": False}


def _looks_like_tool_text(content: str) -> bool:
    """True when a model wrote a tool call as plain text, for example {"name": "ping", "arguments": {"x": 1}}."""
    text = re.sub(r"</?tool_call>|```(?:json)?", " ", content or "")
    for match in re.finditer(r"[\[{]", text):
        try:
            data, _end = json.JSONDecoder().raw_decode(text[match.start():])
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        if any(isinstance(i, dict) and ("name" in i or "function" in i) for i in items):
            return True
    return False


def _judge_tools(run: _Run, content: str, tool_calls: object, ms: int) -> dict | None:
    """Looks at the answer to the ping-tool request. Returns a failed result, or None when the round trip worked."""
    if tool_calls:
        run.tools_ok = True
        run.passed("tools", ms, "The model called the tool.")
        return None
    run.tools_ok = False
    if _looks_like_tool_text(content):
        run.tools_as_text = True
        run.hints += ["This model wrote the tool call as text instead of making a real tool call, so the assistant can't use it. "
                      "Coder models often do this. Use a general model for the chat, for example " + SUGGESTED_TOOL_MODELS[0] +
                      " (set RECTIFI_MODEL_CHAT), and keep the coder model for RECTIFI_MODEL_CODE."]
        return run.result("tools", False, None, "The model wrote the tool call as text.")
    run.hints += ["The model answered but did not call the tool. Use a model that is good at tool calling, for example "
                  + SUGGESTED_TOOL_MODELS[0] + "."]
    return run.result("tools", False, None, "The model did not call the tool.")


def _fail_http(run: _Run, stage: str, exc: Exception) -> dict:
    """A request that never got an answer: refused, timed out, or broken."""
    names = _class_names(exc)
    if any("Timeout" in n for n in names):
        if run.target["provider"] == "ollama":
            run.hints.append(f"No answer within {run.timeout:g} seconds. The first answer after Ollama starts can take a minute on a "
                             "laptop CPU while the model loads. Run the check again; once the model is loaded it is quick.")
        else:
            run.hints.append(f"No answer within {run.timeout:g} seconds. Check your internet connection, or try another model.")
        return run.result(stage, False, None, f"No answer within {run.timeout:g} seconds.")
    if isinstance(exc, httpx.ConnectError):
        base = run.target["base"]
        if run.target["provider"] == "ollama":
            run.hints.append(f"Ollama isn't answering at {base}. Open the Ollama app, or run `ollama serve`.")
            if "localhost" in base:
                run.hints.append("On Windows, `localhost` may try IPv6 first and be slow. Try OLLAMA_BASE_URL=http://127.0.0.1:11434 in .env.")
        else:
            run.hints.append("Can't reach the provider. Check your internet connection and the base URL in .env.")
        run.hints.append("After you change .env, restart Rectifi: it is read once at start.")
        return run.result(stage, False, None, f"Can't connect to {base}.")
    run.hints.append("The request failed before the provider answered. Check the base URL in .env.")
    return run.result(stage, False, None, f"{type(exc).__name__}: {exc}")


def _fail_status(run: _Run, stage: str, status: int, body: object, text: str = "") -> dict:
    message = _body_message(body) or text or f"HTTP {status}"
    advice = _advice(status, message, run.target["provider"])
    if advice:
        run.hints.append(advice)
    return run.result(stage, False, status, message)


def _json_or_none(reply: httpx.Response) -> object:
    try:
        return reply.json()
    except ValueError:
        return None


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _probe_ollama(run: _Run, check_tools: bool) -> dict:
    base, model, timeout = run.target["base"], run.target["model"], run.timeout
    keep, ctx = config.ollama_keep_alive(), config.ollama_num_ctx()
    with httpx.Client(timeout=timeout) as client:
        started = time.monotonic()
        try:
            reply = client.get(f"{base}/api/version")
        except httpx.HTTPError as exc:
            return _fail_http(run, "reachable", exc)
        if reply.status_code != 200:
            return _fail_status(run, "reachable", reply.status_code, _json_or_none(reply), reply.text)
        version = (_json_or_none(reply) or {}).get("version", "")
        run.passed("reachable", _ms(started), f"Ollama {version}".strip())

        # the model must be installed
        started = time.monotonic()
        try:
            reply = client.get(f"{base}/api/tags")
            installed = [m.get("name", "") for m in (_json_or_none(reply) or {}).get("models", [])] if reply.status_code == 200 else []
        except httpx.HTTPError as exc:
            return _fail_http(run, "model", exc)
        have = installed_match(model, installed)
        if have is None:
            run.hints.append(f"Run `ollama pull {model}`. The name must match exactly, including the tag (for example {SUGGESTED_TOOL_MODELS[0]}).")
            if installed:
                run.hints.append("Installed: " + ", ".join(installed[:8]) + ".")
            return run.result("model", False, 404, f'model "{model}" not found, try pulling it first')
        try:
            reply = client.post(f"{base}/api/show", json={"model": have})
        except httpx.HTTPError as exc:
            return _fail_http(run, "model", exc)
        if reply.status_code != 200:
            return _fail_status(run, "model", reply.status_code, _json_or_none(reply), reply.text)
        caps = (_json_or_none(reply) or {}).get("capabilities")
        run.passed("model", _ms(started), f"{have} is installed.")

        # what Ollama says the model can do
        if isinstance(caps, list):
            if "tools" in caps:
                run.passed("capabilities", None, "Ollama lists tool support: " + ", ".join(caps))
            elif check_tools:
                run.tools_ok = False
                run.hints.append(f"{have} does not support tool calling, and the assistant needs it. Use a model that does: "
                                 + ", ".join(f"`ollama pull {m}`" for m in SUGGESTED_TOOL_MODELS[:2]) +
                                 ", then set RECTIFI_MODEL_CHAT in .env.")
                return run.result("capabilities", False, 400, f"{have} does not support tools.")
            else:
                run.passed("capabilities", None, "No tool support (this job doesn't need it): " + ", ".join(caps))
            if "completion" not in caps and "embedding" in caps:
                run.hints.append(f"{have} is an embedding model and can't write answers. Pick a chat model.")
                return run.result("capabilities", False, 400, f"{have} is an embedding model.")
        if ctx < MIN_GOOD_CTX:
            run.hints.append(f"OLLAMA_NUM_CTX is {ctx}. With the assistant's tool list, {MIN_GOOD_CTX} or more is safer: a short window "
                             "silently drops the tools and the model ignores them.")

        # a tiny chat
        options = {"num_predict": 5, "num_ctx": ctx, "temperature": 0}
        extra = {"think": False} if model.lower().startswith("qwen3") else {}
        started = time.monotonic()
        try:
            reply = client.post(f"{base}/api/chat", json={
                "model": have, "stream": False, "keep_alive": keep, "options": options, **extra,
                "messages": [{"role": "user", "content": "Reply with the word OK."}]})
        except httpx.HTTPError as exc:
            return _fail_http(run, "chat", exc)
        if reply.status_code != 200:
            return _fail_status(run, "chat", reply.status_code, _json_or_none(reply), reply.text)
        run.chat_ms = _ms(started)
        run.passed("chat", run.chat_ms, "The model replied.")
        if not check_tools:
            return run.result("ok", True)

        # a tool round trip
        started = time.monotonic()
        try:
            reply = client.post(f"{base}/api/chat", json={
                "model": have, "stream": False, "keep_alive": keep, "tools": [_PING_TOOL], **extra,
                "options": {**options, "num_predict": 128},
                "messages": [{"role": "user", "content": "Call the ping tool with x=1."}]})
        except httpx.HTTPError as exc:
            return _fail_http(run, "tools", exc)
        if reply.status_code != 200:
            run.tools_ok = False
            return _fail_status(run, "tools", reply.status_code, _json_or_none(reply), reply.text)
        message = ((_json_or_none(reply) or {}).get("message") or {})
        failed = _judge_tools(run, message.get("content") or "", message.get("tool_calls"), _ms(started))
        return failed or run.result("ok", True)


def _probe_openai_style(run: _Run, check_tools: bool) -> dict:
    t, timeout = run.target, run.timeout
    url = f"{t['base']}/chat/completions"
    headers = {"Authorization": f"Bearer {t['key']}", "Content-Type": "application/json"}
    base_body = {"model": t["model"], "temperature": 0}
    if t["provider"] == "deepseek":
        base_body["thinking"] = {"type": "disabled"}
    with httpx.Client(timeout=timeout) as client:
        started = time.monotonic()
        try:
            reply = client.post(url, headers=headers, json={
                **base_body, "max_tokens": 5, "messages": [{"role": "user", "content": "Reply with the word OK."}]})
        except httpx.HTTPError as exc:
            return _fail_http(run, "reachable", exc)
        run.passed("reachable", _ms(started), "The provider answered.")
        if reply.status_code != 200:
            return _fail_status(run, "model" if reply.status_code == 404 else "chat", reply.status_code,
                                _json_or_none(reply), reply.text)
        run.chat_ms = _ms(started)
        run.passed("model", None, f"{t['model']} was accepted.")
        run.passed("chat", run.chat_ms, "The model replied.")
        if not check_tools:
            return run.result("ok", True)

        started = time.monotonic()
        try:
            reply = client.post(url, headers=headers, json={
                **base_body, "max_tokens": 128, "tools": [_PING_TOOL], "tool_choice": "auto",
                "messages": [{"role": "user", "content": "Call the ping tool with x=1."}]})
        except httpx.HTTPError as exc:
            return _fail_http(run, "tools", exc)
        if reply.status_code != 200:
            run.tools_ok = False
            return _fail_status(run, "tools", reply.status_code, _json_or_none(reply), reply.text)
        data = _json_or_none(reply)
        try:
            message = data["choices"][0]["message"] or {}
        except (KeyError, IndexError, TypeError):
            message = {}
        failed = _judge_tools(run, message.get("content") or "", message.get("tool_calls"), _ms(started))
        return failed or run.result("ok", True)


def probe(role: str = "chat", timeout: float = 20, *, check_tools: bool | None = None, fresh: bool = False) -> dict:
    """Is the model for this role usable right now? Makes real calls (so it can load the model into memory) and says which
    stage failed. Returns {ok, stage, http_status, provider_message, latency_ms, tools_ok, hints[]} plus provider, model, role,
    tools_as_text and stages (one entry per step that ran: name, ok, ms, detail). `stage` is "ok" or the step that failed:
    config, reachable, model, capabilities, chat or tools. The tool round trip runs for the chat role only, unless
    `check_tools` says otherwise. The result is kept for 60 seconds; `fresh=True` ignores that."""
    if role not in config.ROLES:
        raise ValueError(f"Unknown model role {role!r}. Use one of: {', '.join(config.ROLES)}.")
    check_tools = (role == "chat") if check_tools is None else check_tools
    target = _target(role)
    key = (target["provider"], target["base"], target["model"], check_tools)
    if not fresh:
        with _probe_lock:
            hit = _probe_cache.get(key)
        if hit and time.monotonic() - hit[0] < PROBE_CACHE_SECONDS:
            return {**hit[1], "cached": True}
    run = _Run(target, role, timeout)
    ok, why = (True, "") if target["provider"] == "ollama" else llm_status(role)       # for Ollama the probe itself finds out what is wrong
    if not ok:
        run.hints.append("Fix this in .env, restart Rectifi, then check again.")
        result = run.result("config", False, None, why)
    else:
        try:
            result = (_probe_ollama if target["provider"] == "ollama" else _probe_openai_style)(run, check_tools)
        except Exception as exc:                  # a probe must report, never crash the page that asked
            run.hints.append("Unexpected error while checking the model.")
            result = run.result("chat", False, None, f"{type(exc).__name__}: {exc}")
    with _probe_lock:
        _probe_cache[key] = (time.monotonic(), result)
    return result

# ═══ WS-LLM: end ═══
