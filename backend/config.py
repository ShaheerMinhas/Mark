from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and not os.environ.get(key):
            os.environ[key] = value


_load_dotenv()

def _database_url() -> str:
    """Railway injects DATABASE_URL for Postgres; fall back to SQLite locally."""
    url = os.getenv("DATABASE_URL") or os.getenv("MARK_DATABASE_URL")
    if url:
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url
    return f"sqlite:///{DATA_DIR / 'mark.db'}"


DATABASE_URL = _database_url()
SECRET_KEY = os.getenv("MARK_SECRET_KEY", "change-me-in-production-use-long-random-string")
CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "https://markdiscovery.netlify.app,http://localhost:5173,http://127.0.0.1:5173",
)
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("MARK_TOKEN_EXPIRE_MINUTES", "10080"))  # 7 days
SESSION_ENCRYPTION_KEY = os.getenv("MARK_SESSION_KEY", SECRET_KEY[:32].ljust(32, "0"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")
LEADMAGIC_API_KEY = os.getenv("LEADMAGIC_API_KEY", "")
PROSPEO_API_KEY = os.getenv("PROSPEO_API_KEY", "")
FINDYMAIL_API_KEY = os.getenv("FINDYMAIL_API_KEY", "")
TOMBA_API_KEY = os.getenv("TOMBA_API_KEY", "")
TOMBA_SECRET = os.getenv("TOMBA_SECRET", "")
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", os.getenv("EMAIL_PROVIDERS", ""))
EMAIL_CACHE_DAYS = int(os.getenv("EMAIL_CACHE_DAYS", "30"))


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Worker runtime. The API process hosts workers by default so the existing
# single-service Railway deployment keeps working; set MARK_INLINE_WORKER=0 once a
# dedicated `python -m backend.worker` service is running.
INLINE_WORKER = _flag("MARK_INLINE_WORKER", True)
# Three workers so a new search is not stuck behind two enrichment passes.
WORKER_CONCURRENCY = max(1, int(os.getenv("MARK_WORKER_CONCURRENCY", "3")))
WORKER_LEASE_SECONDS = max(30, int(os.getenv("MARK_WORKER_LEASE_SECONDS", "300")))
WORKER_IDLE_SLEEP_SECONDS = max(0.2, float(os.getenv("MARK_WORKER_IDLE_SLEEP", "2.0")))
LOG_LEVEL = os.getenv("MARK_LOG_LEVEL", "INFO")

# Public-query result cache. Set to 0 to disable.
SEARCH_CACHE_HOURS = max(0, int(os.getenv("MARK_SEARCH_CACHE_HOURS", "24")))

# How many search queries run at once. Each slot is one independent client, so
# twelve queries in flight are twelve DuckDuckGo/Google/Yahoo sessions, not one
# session asked twelve times in a row.
QUERY_PARALLELISM = max(1, min(24, int(os.getenv("MARK_QUERY_PARALLELISM", "12"))))

# Extra copies of each backend. Four Google clients can each wait their own
# two seconds instead of sharing one throttle.
ENGINE_REPLICAS = max(1, min(8, int(os.getenv("MARK_ENGINE_REPLICAS", "4"))))

# Public-web email/phone lookups per enrichment batch. Independent of discovery.
ENRICHMENT_PARALLELISM = max(1, min(24, int(os.getenv("MARK_ENRICHMENT_PARALLELISM", "16"))))
