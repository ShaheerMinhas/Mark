"""Grant platform-operator access. Registration never does this.

Usage, from a shell that can read the database::

    python -m backend.platform_admin grant operator@example.com

The account must already exist. Owning a workspace is not enough.
There is no environment variable that turns every user into an operator.
"""

from __future__ import annotations

import sys

from backend.database import SessionLocal, init_db
from backend.models import User


def grant(email: str) -> None:
    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email.strip().lower()).one_or_none()
        if user is None:
            raise SystemExit(f"No user named {email}")
        user.is_platform_admin = True
        db.commit()
        print(f"Granted platform admin to {user.email}. Workspace ownership was not changed.")
    finally:
        db.close()


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "grant":
        raise SystemExit("Usage: python -m backend.platform_admin grant operator@example.com")
    grant(sys.argv[2])


if __name__ == "__main__":
    main()
