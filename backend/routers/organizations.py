from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from backend.auth_utils import hash_password
from backend.database import get_db
from backend.dependencies import get_current_user, require_org_access
from backend.models import AuditLog, Organization, OrganizationMember, OrgRole, User
from backend.schemas import InviteMemberRequest, OrganizationOut

router = APIRouter(prefix="/api/organizations", tags=["organizations"])


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class WorkspaceUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class RoleUpdate(BaseModel):
    role: OrgRole


class TransferOwnershipRequest(BaseModel):
    user_id: int


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "workspace"


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


@router.post("", response_model=OrganizationOut)
def create_workspace(
    body: WorkspaceCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    name = body.name.strip()
    if db.query(Organization).filter(Organization.name == name).first():
        raise HTTPException(status_code=400, detail="Workspace name already in use")
    base = _slugify(name)
    slug = base
    n = 1
    while db.query(Organization).filter(Organization.slug == slug).first():
        slug = f"{base}-{n}"
        n += 1
    org = Organization(name=name, slug=slug)
    db.add(org)
    db.flush()
    db.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=OrgRole.OWNER))
    db.add(
        AuditLog(
            user_id=user.id,
            organization_id=org.id,
            action="workspace.created",
            target_type="organization",
            target_id=org.id,
            detail={"name": name},
        )
    )
    db.commit()
    return OrganizationOut(id=org.id, name=org.name, slug=org.slug, member_count=1)


@router.patch("/{org_id}", response_model=OrganizationOut)
def rename_workspace(
    org_id: int,
    body: WorkspaceUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_org_access(db, user, org_id, admin_only=True)
    org = db.get(Organization, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Workspace not found")
    org.name = body.name.strip()
    db.add(
        AuditLog(
            user_id=user.id,
            organization_id=org.id,
            action="workspace.renamed",
            target_type="organization",
            target_id=org.id,
            detail={"name": org.name},
        )
    )
    db.commit()
    count = db.query(OrganizationMember).filter(OrganizationMember.organization_id == org.id).count()
    return OrganizationOut(id=org.id, name=org.name, slug=org.slug, member_count=count)


@router.delete("/{org_id}")
def delete_workspace(
    org_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = require_org_access(db, user, org_id, admin_only=True)
    if membership.role != OrgRole.OWNER:
        raise HTTPException(status_code=403, detail="Only the workspace owner can delete it")
    org = db.get(Organization, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Workspace not found")
    db.query(OrganizationMember).filter(OrganizationMember.organization_id == org_id).delete()
    db.delete(org)
    db.commit()
    return {"ok": True}


@router.get("/{org_id}/members")
def list_workspace_members(
    org_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_org_access(db, user, org_id)
    rows = (
        db.query(OrganizationMember, User)
        .join(User, User.id == OrganizationMember.user_id)
        .filter(OrganizationMember.organization_id == org_id)
        .all()
    )
    return {
        "items": [
            {
                "id": m.id,
                "user_id": u.id,
                "email": u.email,
                "full_name": u.full_name,
                "role": m.role.value if hasattr(m.role, "value") else str(m.role),
                "joined_at": m.joined_at,
            }
            for m, u in rows
        ]
    }


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
    db.add(
        AuditLog(
            user_id=user.id,
            organization_id=org_id,
            action="workspace.member_invited",
            target_type="user",
            target_id=member_user.id,
            detail={"email": member_user.email, "role": str(body.role.value)},
        )
    )
    db.commit()
    return {"ok": True, "user_id": member_user.id, "email": member_user.email, "role": body.role}


@router.patch("/{org_id}/members/{member_user_id}")
def update_member_role(
    org_id: int,
    member_user_id: int,
    body: RoleUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_org_access(db, user, org_id, admin_only=True)
    m = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == member_user_id,
        )
        .first()
    )
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    m.role = body.role
    db.add(
        AuditLog(
            user_id=user.id,
            organization_id=org_id,
            action="workspace.role_updated",
            target_type="user",
            target_id=member_user_id,
            detail={"role": str(body.role.value)},
        )
    )
    db.commit()
    return {"ok": True, "user_id": member_user_id, "role": m.role}


@router.delete("/{org_id}/members/{member_user_id}")
def remove_member(
    org_id: int,
    member_user_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_org_access(db, user, org_id, admin_only=True)
    m = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == member_user_id,
        )
        .first()
    )
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    if m.role == OrgRole.OWNER:
        raise HTTPException(status_code=400, detail="Transfer ownership before removing the owner")
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.post("/{org_id}/transfer-ownership")
def transfer_ownership(
    org_id: int,
    body: TransferOwnershipRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    caller = require_org_access(db, user, org_id, admin_only=True)
    if caller.role != OrgRole.OWNER:
        raise HTTPException(status_code=403, detail="Only the owner can transfer ownership")
    target = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == body.user_id,
        )
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail="Target user is not a workspace member")
    caller.role = OrgRole.ADMIN
    target.role = OrgRole.OWNER
    db.add(
        AuditLog(
            user_id=user.id,
            organization_id=org_id,
            action="workspace.ownership_transferred",
            target_type="user",
            target_id=body.user_id,
            detail={"previous_owner": user.id},
        )
    )
    db.commit()
    return {"ok": True, "new_owner_id": body.user_id}


@router.get("/{org_id}/audit-logs")
def list_audit_logs(
    org_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    require_org_access(db, user, org_id)
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.organization_id == org_id)
        .order_by(AuditLog.created_at.desc())
        .limit(100)
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "detail": r.detail or {},
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }
