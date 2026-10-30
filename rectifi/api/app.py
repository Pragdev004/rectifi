"""The web app's server: /api (JSON) and the built browser app (web/dist), on one port.

    python -m uvicorn --factory rectifi.api.app:create_app --host 127.0.0.1 --port 8501

Run exactly one worker: the pending uploads, the recent-imports list and the Ask locks live in memory.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import config
from ..db import init_db
from .errors import DB_ERRORS, EXCEPTION_HANDLERS
from .routes import (accounts, ask, auth, budgets, charts, chat, classify, imports, insights, knowledge, memory, meta, models,
                     overview, phone, review, traces, transactions)
from .security import ApiGuard, BodyLimit, HostGuard, SecurityHeaders, TailscaleLock, install_session_lock

log = logging.getLogger("rectifi.api")

ROOT = Path(__file__).resolve().parent.parent.parent
NOT_BUILT = ("The web app isn't built. Run `python scripts/dev.py build-web`, "
             "or use the old app with `python scripts/dev.py up --streamlit`.")


def default_dist() -> Path:
    return Path(os.getenv("RECTIFI_WEB_DIST") or ROOT / "web" / "dist")


class ImmutableFiles(StaticFiles):
    """/assets/* files have a content hash in their name, so a browser may keep them for a year."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def api_routes() -> list[Route]:
    found = []
    for module in (meta, overview, review, insights, transactions, imports, budgets, accounts, phone, ask,
                   classify, charts, chat, memory, traces, models, auth, knowledge):
        found += module.routes
    return found


def create_app(dist_dir: str | Path | None = None) -> Starlette:
    dist = Path(dist_dir) if dist_dir else default_dist()
    root = dist.resolve()
    index = root / "index.html"

    async def spa(request: Request) -> Response:
        if not index.is_file():
            return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Rectifi</title><p>{NOT_BUILT}</p>", status_code=503,
                                headers={"Cache-Control": "no-store"})
        rel = unquote(request.path_params.get("path", "")).lstrip("/")
        parts = rel.split("/") if rel else []
        if any(p in ("..", ".") or "\\" in p or "\x00" in p for p in parts):
            return PlainTextResponse("Not found", status_code=404)
        if rel:
            candidate = (root / rel).resolve()
            if candidate.is_relative_to(root) and candidate.is_file() and candidate.name != "index.html":
                return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
            if rel.startswith("assets/"):
                return PlainTextResponse("Not found", status_code=404)
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    def _start_background_work() -> None:
        """Only when the database is up: finish sorting jobs a restart cut short, and refresh the library index (a thread; one worker)."""
        try:
            from ..agent import jobs                            # lazy: rectifi.api must import without langchain

            jobs.requeue_unfinished()
        except Exception:                                       # noqa: BLE001
            log.exception("Could not resume the unfinished sorting jobs.")
        knowledge.auto_reindex()

    @asynccontextmanager
    async def lifespan(app):
        config.enforce_langsmith_privacy()          # traces never leave the laptop unless RECTIFI_LANGSMITH=1
        try:
            await run_in_threadpool(init_db)
        except DB_ERRORS:
            log.warning("The database is not reachable yet. /api answers 503 until it is, then recovers on its own.")
        else:
            await run_in_threadpool(_start_background_work)
        yield

    routes = [
        Mount("/api", routes=api_routes()),
        Mount("/assets", app=ImmutableFiles(directory=root / "assets", check_dir=False)),
        Route("/{path:path}", spa, methods=["GET", "HEAD"]),
    ]
    middleware = [Middleware(SecurityHeaders), Middleware(HostGuard)]
    lock_user = os.getenv("RECTIFI_TAILSCALE_USER", "").strip()
    if lock_user:
        middleware.append(Middleware(TailscaleLock, user=lock_user))
    middleware += [Middleware(ApiGuard), Middleware(BodyLimit)]
    app = Starlette(routes=routes, middleware=middleware, exception_handlers=EXCEPTION_HANDLERS, lifespan=lifespan)
    install_session_lock(app)         # raises StartupRefused in public mode without RECTIFI_PASSWORD_HASH + RECTIFI_SESSION_SECRET
    return app

