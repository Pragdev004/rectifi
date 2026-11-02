"""The web app's server: /api (JSON) and the built browser app (web/dist), on one port.

    python -m uvicorn --factory rectifi.api.app:create_app --host 127.0.0.1 --port 8501

Run exactly one worker: the pending uploads, the recent-imports list and the Ask locks live in memory.
"""

from __future__ import annotations

import asyncio
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
from ..db import TenancyMigrationNeeded, UnsafeDatabaseRole, init_db
from .errors import DB_ERRORS, EXCEPTION_HANDLERS
from .routes import (accounts, alerts as alerts_routes, ask, auth, budgets, charts, chat, classify, imports, insights, knowledge, memory, meta, models,
                     overview, phone, review, traces, transactions)
from .security import ApiGuard, BodyLimit, HostGuard, SecurityHeaders, TailscaleLock, install_session_lock

log = logging.getLogger("rectifi.api")

TICK_SECONDS = 60                    # how often the alerts ticker runs
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
                   classify, charts, chat, memory, traces, models, auth, knowledge, alerts_routes):
        found += module.routes
    # ═══ WS-ACCOUNT: begin ═══   (account export and delete, /api/privacy)
    from .routes import account as account_routes

    found += account_routes.routes
    # ═══ WS-ACCOUNT: end ═══
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

    async def sw(request: Request) -> Response:
        """The push-only service worker. Its own route so it is always text/javascript (a Windows registry can say .js is text/plain)
        and never cached, so an update reaches the phone at once. Public, like every static file."""
        path = root / "sw.js"
        if not path.is_file():
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(path, media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    # ═══ WS-ALERTS: begin ═══   (the ticker: a task that calls alerts.tick every minute)
    async def alert_ticker() -> None:
        """Every minute: catch payments that arrived without a hook, release alerts held overnight, the digest, retries."""
        from .. import alerts

        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await run_in_threadpool(alerts.tick)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                                # noqa: BLE001  (log the type only; the loop must go on)
                log.error("alerts tick failed (%s)", type(exc).__name__)
    # ═══ WS-ALERTS: end ═══

    @asynccontextmanager
    async def lifespan(app):
        config.enforce_langsmith_privacy()          # traces never leave the laptop unless RECTIFI_LANGSMITH=1
        ticker = None
        try:
            await run_in_threadpool(init_db)
        except (TenancyMigrationNeeded, UnsafeDatabaseRole) as exc:
            log.error("%s", exc)                    # the server does not start; the message above says what to do
            raise
        except DB_ERRORS:
            log.warning("The database is not reachable yet. /api answers 503 until it is, then recovers on its own.")
        else:
            await run_in_threadpool(_start_background_work)
            # ═══ WS-ALERTS: begin ═══   (start the ticker)
            if not os.getenv("RECTIFI_NO_ALERT_BACKGROUND"):
                from .. import alerts

                try:
                    await run_in_threadpool(alerts.ensure_watermark)       # what is already here is history, not news
                except DB_ERRORS:
                    pass
                ticker = asyncio.create_task(alert_ticker())
            # ═══ WS-ALERTS: end ═══
        try:
            yield
        finally:
            # ═══ WS-ALERTS: begin ═══   (stop the ticker)
            if ticker is not None:
                ticker.cancel()
                try:
                    await ticker
                except asyncio.CancelledError:
                    pass
            # ═══ WS-ALERTS: end ═══

    routes = [
        Mount("/api", routes=api_routes()),
        Route("/sw.js", sw, methods=["GET", "HEAD"]),
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

