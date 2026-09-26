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
