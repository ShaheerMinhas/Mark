from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.auth_utils import create_access_token, hash_password, verify_password
from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models import AccountType, Organization, OrganizationMember, OrgRole, User
from backend.schemas import OrgMembershipOut, TokenResponse, UserCreate, UserLogin, UserOut

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
    token = create_access_token({"sub": str(user.id)})
    return TokenResponse(access_token=token, user=_user_out(db, user))


@router.post("/login", response_model=TokenResponse)
def login(body: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token({"sub": str(user.id)})
    return TokenResponse(access_token=token, user=_user_out(db, user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _user_out(db, user)
