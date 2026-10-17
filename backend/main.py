"""Mark — lead discovery platform."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.config import CORS_ORIGINS, INLINE_WORKER, LOG_LEVEL
from backend.database import init_db
from backend.observability import configure_logging
from backend.routers import auth, organizations, search_jobs, searches

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging(LOG_LEVEL)
    init_db()
    workers = []
    if INLINE_WORKER:
        # Keeps the current single-service Railway deployment working. Set
        # MARK_INLINE_WORKER=0 once a dedicated worker service is running.
        from backend.worker import start_inline_workers, stop_inline_workers

        workers = start_inline_workers()
    try:
        yield
    finally:
        if workers:
            stop_inline_workers()


app = FastAPI(
    title="Mark",
    description="Lead discovery via public web search — no LinkedIn login required",
    version="2.2.0",
    lifespan=lifespan,
)

_origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(searches.router)
app.include_router(search_jobs.router)
app.include_router(organizations.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "product": "Mark", "version": "2.2.0", "linkedin_login_required": False}


# Serve React production build
if FRONTEND_DIST.exists():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIST / "index.html")
