from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

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
