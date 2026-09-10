from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.auth_utils import decode_access_token
from backend.database import get_db
from backend.models import OrganizationMember, OrgRole, User

security = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_access_token(creds.credentials)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.query(User).filter(User.id == int(payload["sub"]), User.is_active.is_(True)).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_optional_user(
    creds: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User | None:
    if not creds or not creds.credentials:
        return None
    payload = decode_access_token(creds.credentials)
    if not payload or "sub" not in payload:
        return None
    return db.query(User).filter(User.id == int(payload["sub"]), User.is_active.is_(True)).first()


def require_org_access(db: Session, user: User, org_id: int, admin_only: bool = False) -> OrganizationMember:
    membership = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user.id,
        )
        .first()
    )
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this organization")
    if admin_only and membership.role not in {OrgRole.OWNER, OrgRole.ADMIN}:
        raise HTTPException(status_code=403, detail="Admin access required")
    return membership
