from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.auth_utils import hash_password
from backend.database import get_db
from backend.dependencies import get_current_user, require_org_access
from backend.models import Organization, OrganizationMember, OrgRole, User
from backend.schemas import InviteMemberRequest, OrganizationOut

router = APIRouter(prefix="/api/organizations", tags=["organizations"])


@router.get("", response_model=list[OrganizationOut])
def my_organizations(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(Organization, OrganizationMember)
        .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
        .filter(OrganizationMember.user_id == user.id)
        .all()
    )
    out = []
    for org, _ in rows:
        count = db.query(OrganizationMember).filter(OrganizationMember.organization_id == org.id).count()
        out.append(OrganizationOut(id=org.id, name=org.name, slug=org.slug, member_count=count))
    return out


@router.post("/{org_id}/members")
def invite_member(
    org_id: int,
    body: InviteMemberRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_org_access(db, user, org_id, admin_only=True)
    member_user = db.query(User).filter(User.email == body.email.lower()).first()
    if not member_user:
        # Create placeholder user — in production send email invite
        member_user = User(
            email=body.email.lower(),
            full_name=body.email.split("@")[0].title(),
            hashed_password=hash_password("changeme123"),
            account_type=user.account_type,
        )
        db.add(member_user)
        db.flush()

    existing = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == member_user.id,
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="User already in organization")

    db.add(
        OrganizationMember(
            organization_id=org_id,
            user_id=member_user.id,
            role=body.role,
        )
    )
    db.commit()
    return {"ok": True, "email": member_user.email, "role": body.role}
