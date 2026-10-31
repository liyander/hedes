"""H.A.D.E.S - FastAPI application entry point."""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import ai, approvals, assistant, auth, connections, healing, oauth, integrations, notifications, runtime, slack, sources, templates, users, whatsapp, workflows
from .core.config import STATIC_DIR, get_settings
from .core.db import init_db, session_scope
from .mock import services as mock_services
from .services import healing as _healing_hooks, notify, slack_bot, vault  # noqa: F401  (notify and healing register their after-commit hooks)
from .services.ingest import scheduler_loop as ingest_scheduler_loop
from .services.workflows import scheduler_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
_stop = threading.Event()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from . import maintenance, seed
    init_db()
    with session_scope() as db:
        seed.run(db)
        maintenance.close_interrupted_runs(db)
    if get_settings().run_scheduler:
        threading.Thread(target=scheduler_loop, args=(_stop,), name="scheduler", daemon=True).start()
        threading.Thread(target=ingest_scheduler_loop, args=(_stop,), name="ingest-scheduler", daemon=True).start()
        threading.Thread(target=slack_bot.supervisor, args=(_stop,), name="slack-supervisor", daemon=True).start()
    yield
    _stop.set()


app = FastAPI(title="H.A.D.E.S", version="1.0.0", lifespan=lifespan,
              description="AI-assisted, configuration-driven integration platform with admin-governed activation.")

for r in (auth.router, users.router, slack.router, oauth.router, healing.router, ai.router, integrations.router, approvals.router, connections.router, runtime.router,
          runtime.mcp_router, notifications.router, assistant.router, sources.router, templates.router, workflows.router, workflows.hooks_router,
          whatsapp.router, whatsapp.fix_router):
    app.include_router(r)
if get_settings().demo_mode:
    app.include_router(mock_services.router)

CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # revalidate (ETag) so updated code is never run from a stale cache
    elif request.url.path.startswith("/static-v/") and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # the URL changes whenever a file does
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(vault.EnvRefNotAllowed)
async def env_ref_not_allowed(_request: Request, exc: vault.EnvRefNotAllowed):
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    logging.getLogger("integra").exception("Unhandled error", exc_info=exc)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


APP_DIR = Path(__file__).resolve().parent


def code_version() -> str:
    """Newest modification time of the server's Python code."""
    return format(max(f.stat().st_mtime_ns for f in APP_DIR.rglob("*.py")) // 1_000_000, "x")


STARTED_CODE = code_version()


@app.get("/api/v1/health")
def health():
    # "stale": the Python code on disk changed after this process started (e.g. after a git pull without a restart).
    # The web pages are always served fresh, but new API routes only exist after a restart.
    return {"ok": True, "code": STARTED_CODE, "stale": code_version() != STARTED_CODE}


_static = StaticFiles(directory=STATIC_DIR)
app.mount("/static", _static, name="static")


def asset_version() -> str:
    """Changes whenever any front-end file changes (checked per page load, so edits apply without a restart)."""
    return format(max(f.stat().st_mtime_ns for f in STATIC_DIR.rglob("*") if f.is_file()) // 1_000_000, "x")


@app.get("/static-v/{version}/{path:path}", include_in_schema=False)
async def versioned_static(version: str, path: str, request: Request):
    # Browsers cache these forever; a new version gives every file a new URL, so stale code is never reused. Module imports
    # are relative, so the whole module graph loads from one version (no mix of old and new files).
    return await _static.get_response(path, request.scope)


@app.get("/", include_in_schema=False)
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace('"/static/', f'"/static-v/{asset_version()}/')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
