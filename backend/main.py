"""FastAPI entrypoint.  Run from inside backend/:   python -m uvicorn main:app --reload

Local-first: binds to 127.0.0.1, serves the website AND the API from one origin, every API call
needs the shared token, CORS is pinned to the frontend + extension origins (never "*").
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

import database as db
import security
from config import settings
from routes import admin, analytics, extension, system
from services import activity_storage as storage
from services import bootstrap, catchup
from services.classification_worker import worker

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wdmt")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    storage.get_tz_name()  # detect + persist the timezone on first run
    new_token = security.ensure_token()
    if new_token:
        banner = "=" * 70
        print(f"\n{banner}\n  FIRST RUN - your API token (enter it once in the website and the extension):\n\n"
              f"    {new_token}\n\n  Also saved to: {security.token_file()}\n{banner}\n", flush=True)
    if settings.app_mode == "demo":
        from services import demo_seed
        demo_seed.seed_if_empty()
    else:
        bootstrap.reset_stale_running()
    worker.start()
    catchup.scheduler.start()
    log.info("started in %s mode (db=%s)", settings.app_mode, settings.db_path)
    yield
    worker.stop()
    catchup.scheduler.stop()


app = FastAPI(title="Where Did My Time Go?", version="1.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)  # no public API explorer

app.add_middleware(
    CORSMiddleware,
    allow_origins=security.allowed_origins(),      # explicit list, never "*"
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Token", "Authorization"],
    allow_credentials=False,
)
if settings.app_mode != "demo" or settings.allowed_hosts != ["localhost", "127.0.0.1", "[::1]", "testserver"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    """Reject browser requests from any origin that is not our website or our extension."""
    origin = request.headers.get("origin")
    if not (security.origin_allowed(origin) or security.same_origin_ok(origin, request.headers.get("host"))):
        return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
    return await call_next(request)


app.include_router(system.public)
app.include_router(system.router)
app.include_router(extension.router)
app.include_router(analytics.router)
app.include_router(admin.router)

_frontend = Path(settings.frontend_dir)
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
