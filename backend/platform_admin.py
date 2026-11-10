"""Grant or revoke platform-operator access. Registration never does this.

Usage, from a shell that can read the database::

    python -m backend.platform_admin grant operator@example.com
    python -m backend.platform_admin revoke operator@example.com

The account must already exist. The command does not set or print a password.
The person signs in at /admin with the password already on that account.
Owning a workspace is not enough. There is no environment variable that turns
every user into an operator, and the first registered user is not an operator.
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
            raise SystemExit(f"No user named {email}. Create the account first. This command does not set a password.")
        user.is_platform_admin = True
        db.commit()
        print(f"Granted platform admin to {user.email}. Sign in at /admin. Workspace ownership was not changed.")
    finally:
        db.close()


def revoke(email: str) -> None:
    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email.strip().lower()).one_or_none()
        if user is None:
            raise SystemExit(f"No user named {email}")
        user.is_platform_admin = False
        user.admin_token_version = int(user.admin_token_version or 0) + 1
        db.commit()
        print(f"Revoked platform admin from {user.email}. Existing operator sessions no longer pass.")
    finally:
        db.close()


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"grant", "revoke"}:
        raise SystemExit(
            "Usage: python -m backend.platform_admin grant operator@example.com\n"
            "       python -m backend.platform_admin revoke operator@example.com"
        )
    if sys.argv[1] == "grant":
        grant(sys.argv[2])
    else:
        revoke(sys.argv[2])


if __name__ == "__main__":
    main()
