"""IntegraAI - FastAPI application entry point."""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import ai, approvals, auth, connections, integrations, runtime, workflows
from .core.config import STATIC_DIR, get_settings
from .core.db import init_db, session_scope
from .mock import services as mock_services
from .services.workflows import scheduler_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
_stop = threading.Event()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from . import seed
    init_db()
    with session_scope() as db:
        seed.run(db)
    if get_settings().run_scheduler:
        threading.Thread(target=scheduler_loop, args=(_stop,), name="scheduler", daemon=True).start()
    yield
    _stop.set()


app = FastAPI(title="IntegraAI", version="1.0.0", lifespan=lifespan,
              description="AI-assisted, configuration-driven integration platform with admin-governed activation.")

for r in (auth.router, ai.router, integrations.router, approvals.router, connections.router, runtime.router,
          runtime.mcp_router, workflows.router, workflows.hooks_router, mock_services.router):
    app.include_router(r)

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
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    logging.getLogger("integra").exception("Unhandled error", exc_info=exc)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


@app.get("/api/v1/health")
def health():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
