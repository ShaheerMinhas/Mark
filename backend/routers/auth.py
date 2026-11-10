from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth_utils import ADMIN_AUDIENCE, CUSTOMER_AUDIENCE, create_access_token, hash_password, verify_password
from backend.database import get_db
from backend.dependencies import get_current_admin, get_current_user
from backend.models import AccountType, Organization, OrganizationMember, OrgRole, User
from backend.schemas import OrgMembershipOut, TokenResponse, UserCreate, UserLogin, UserOut
from backend.services.login_guard import record_failure, too_many_attempts

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "org"


def _user_out(db: Session, user: User) -> UserOut:
    memberships = (
        db.query(OrganizationMember, Organization)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .filter(OrganizationMember.user_id == user.id)
        .all()
    )
    orgs = [
        OrgMembershipOut(
            organization_id=org.id,
            organization_name=org.name,
            role=m.role,
        )
        for m, org in memberships
    ]
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        account_type=user.account_type,
        is_platform_admin=bool(getattr(user, "is_platform_admin", False)),
        organizations=orgs,
    )


@router.post("/register", response_model=TokenResponse)
def register(body: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == body.email.lower()).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=body.email.lower(),
        full_name=body.full_name.strip(),
        hashed_password=hash_password(body.password),
        account_type=body.account_type,
    )
    db.add(user)
    db.flush()

    if body.account_type == AccountType.ORGANIZATION:
        if not body.organization_name:
            raise HTTPException(status_code=400, detail="organization_name required for org accounts")
        base_slug = _slugify(body.organization_name)
        slug = base_slug
        n = 1
        while db.query(Organization).filter(Organization.slug == slug).first():
            slug = f"{base_slug}-{n}"
            n += 1
        org = Organization(name=body.organization_name.strip(), slug=slug)
        db.add(org)
        db.flush()
        db.add(
            OrganizationMember(
                organization_id=org.id,
                user_id=user.id,
                role=OrgRole.OWNER,
            )
        )

    db.commit()
    db.refresh(user)
    token = create_access_token({"sub": str(user.id)}, audience=CUSTOMER_AUDIENCE)
    return TokenResponse(access_token=token, user=_user_out(db, user))


def _client_host(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


def _login_bucket(scope: str, request: Request, email: str) -> str:
    return f"{scope}:{_client_host(request)}:{email.strip().lower()}"


@router.post("/login", response_model=TokenResponse)
def login(body: UserLogin, request: Request, db: Session = Depends(get_db)):
    bucket = _login_bucket("customer", request, body.email)
    if too_many_attempts(bucket):
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again later.")
    user = db.query(User).filter(User.email == body.email.lower(), User.is_active.is_(True)).first()
    if not user or not verify_password(body.password, user.hashed_password):
        record_failure(bucket)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token({"sub": str(user.id)}, audience=CUSTOMER_AUDIENCE)
    return TokenResponse(access_token=token, user=_user_out(db, user))


class AdminSessionOut(BaseModel):
    id: int
    email: str
    full_name: str
    role: str = "platform_admin"


class AdminLogin(BaseModel):
    email: str
    password: str
    next: str | None = None


class AdminTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    next: str = "/admin"
    operator: AdminSessionOut


def _admin_next(value: str | None) -> str:
    if not value:
        return "/admin"
    if not value.startswith("/admin") or value.startswith("//") or "\\" in value or "://" in value or ".." in value:
        return "/admin"
    allowed = value == "/admin" or value.startswith("/admin/")
    if not allowed:
        return "/admin"
    return value


def _operator_out(user: User) -> AdminSessionOut:
    return AdminSessionOut(id=user.id, email=user.email, full_name=user.full_name)


@router.post("/admin/login", response_model=AdminTokenResponse)
def admin_login(body: AdminLogin, request: Request, db: Session = Depends(get_db)):
    """Operator sign-in. A customer password for a non-operator gets the same failure as an unknown email."""
    bucket = _login_bucket("admin", request, body.email)
    if too_many_attempts(bucket):
        raise HTTPException(status_code=429, detail="Too many sign-in attempts. Try again later.")
    user = db.query(User).filter(User.email == body.email.lower(), User.is_active.is_(True)).first()
    password_ok = bool(user) and verify_password(body.password, user.hashed_password)
    if not password_ok or not user.is_platform_admin:
        record_failure(bucket)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token(
        {"sub": str(user.id), "ver": int(user.admin_token_version or 0)},
        audience=ADMIN_AUDIENCE,
    )
    return AdminTokenResponse(access_token=token, next=_admin_next(body.next), operator=_operator_out(user))


@router.get("/admin/me", response_model=AdminSessionOut)
def admin_me(admin: User = Depends(get_current_admin)):
    return _operator_out(admin)


@router.post("/admin/logout")
def admin_logout(admin: User = Depends(get_current_admin), db: Session = Depends(get_db)):
    """Invalidate outstanding operator tokens. A customer session for the same person stays valid."""
    admin.admin_token_version = int(admin.admin_token_version or 0) + 1
    db.commit()
    return {"ok": True}


class ProfileUpdate(BaseModel):
    full_name: str | None = None
    password: str | None = None
    current_password: str | None = None


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _user_out(db, user)


@router.patch("/me", response_model=UserOut)
def update_me(
    body: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.full_name and body.full_name.strip():
        user.full_name = body.full_name.strip()
    if body.password:
        if len(body.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        if body.current_password and not verify_password(body.current_password, user.hashed_password):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        user.hashed_password = hash_password(body.password)
    db.commit()
    db.refresh(user)
    return _user_out(db, user)

