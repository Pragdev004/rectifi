"""/api/models: is the model usable? `status` never calls a model; `probe` makes the real calls (and so can load the model).

The model code is imported only inside the handlers (importing rectifi.api must not load langchain)."""

from __future__ import annotations

import threading

from starlette.routing import Route

from ... import config
from ..errors import ApiError
from ..inputs import api

_probe_busy = threading.Lock()          # one check at a time: a check can load a model into memory


def _role(value: object) -> str:
    role = "chat" if value in (None, "") else value
    if not isinstance(role, str) or role not in config.ROLES:
        raise ApiError(422, "invalid", f"role must be one of: {', '.join(config.ROLES)}.", "role")
    return role


@api()
def model_status(request):
    """Settings and the quick status (a 1.5 s look at Ollama at most), plus the last check if it is under a minute old."""
    from ...agent import llm

    ok, why = llm.llm_status()
    provider = config.llm_provider()
    return {
        "ok": bool(ok), "why": why or "", "notice": llm.llm_notice() if ok else "", "provider": provider, "model": llm.describe(),
        "roles": [{"role": r, "model": llm.model_name(r), "named": bool(config.explicit_role_model(r))} for r in config.ROLES],
        "embed_model": config.embed_model() if provider == "ollama" else None,
        "probe": llm.cached_probe("chat"),
    }


@api(body=True)
def run_probe(request, data):
    from ...agent import llm

    role = _role(data.get("role"))
    if not _probe_busy.acquire(blocking=False):
        raise ApiError(409, "busy", "A model check is already running. Wait for it to finish.")
    try:
        return llm.probe(role, timeout=min(float(config.ollama_timeout()), 60.0) if config.llm_provider() == "ollama" else 30.0, fresh=True)
    finally:
        _probe_busy.release()


routes = [
    Route("/models/status", model_status, methods=["GET"]),
    Route("/models/probe", run_probe, methods=["POST"]),
]
