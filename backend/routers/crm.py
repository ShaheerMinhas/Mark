"""Selective Internal CRM, nested groups/subgroups, smart groups, and transfer."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user, require_org_access
from backend.models import (
    AuditLog,
    CrmContact,
    CrmExportRecord,
    CrmGroup,
    CrmGroupMembership,
    LEAD_LIFECYCLE_STAGES,
    LeadActivity,
    LeadRecord,
    SearchJob,
    User,
)
from backend.services.export_service import leads_to_csv, leads_to_xml

router = APIRouter(prefix="/api/crm", tags=["crm"])


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: int | None = None
    description: str | None = None
    kind: str = "static"  # static | smart
    smart_rules: dict[str, Any] = Field(default_factory=dict)
    position: int = 0
    permissions: dict[str, Any] = Field(default_factory=dict)
    organization_id: int | None = None


class GroupUpdate(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    description: str | None = None
    kind: str | None = None
    smart_rules: dict[str, Any] | None = None
    position: int | None = None
    permissions: dict[str, Any] | None = None


class TransferPreviewRequest(BaseModel):
    lead_ids: list[int] = Field(default_factory=list, max_length=5000)
    group_id: int | None = None
    subgroup_id: int | None = None
    organization_id: int | None = None


class TransferExecuteRequest(TransferPreviewRequest):
    exclude_without_email: bool = False
    exclude_unverified: bool = False
    skip_duplicates: bool = True
    allow_update_existing: bool = False
    field_mapping: dict[str, str] = Field(default_factory=dict)


class ContactUpdate(BaseModel):
    stage: str | None = None
    priority: str | None = None
    assigned_user_id: int | None = None
    notes: str | None = None
    tags: list[str] | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    title: str | None = None
    company_name: str | None = None
    location: str | None = None


class MembershipModify(BaseModel):
    contact_ids: list[int] = Field(default_factory=list)
    target_group_id: int | None = None


def _scope_groups(db: Session, user: User, org_id: int | None):
    q = db.query(CrmGroup)
    if org_id is not None:
        require_org_access(db, user, org_id)
        return q.filter(CrmGroup.organization_id == org_id)
    return q.filter(CrmGroup.owner_id == user.id, CrmGroup.organization_id.is_(None))


def _scope_contacts(db: Session, user: User, org_id: int | None):
    q = db.query(CrmContact)
    if org_id is not None:
        require_org_access(db, user, org_id)
        return q.filter(CrmContact.organization_id == org_id)
    return q.filter(CrmContact.owner_id == user.id, CrmContact.organization_id.is_(None))


def _scope_leads(db: Session, user: User, org_id: int | None):
    q = db.query(LeadRecord).join(SearchJob, LeadRecord.search_job_id == SearchJob.id)
    if org_id is not None:
        require_org_access(db, user, org_id)
        return q.filter(SearchJob.organization_id == org_id)
    return q.filter(SearchJob.owner_id == user.id)


def _matches_smart_rules(contact: CrmContact, rules: dict[str, Any]) -> bool:
    if not rules:
        return False
    for key, val in rules.items():
        needle = str(val or "").strip().lower()
        if not needle:
            continue
        if key in {"country", "state", "city", "location"}:
            if needle not in (contact.location or "").lower():
                return False
        elif key in {"job_title", "title"}:
            if needle not in (contact.title or "").lower():
                return False
        elif key in {"company", "company_name"}:
            if needle not in (contact.company_name or "").lower():
                return False
        elif key == "has_email" and bool(val) and not contact.email:
            return False
        elif key == "stage" and (contact.stage or "new").lower() != needle:
            return False
    return True


def _contact_dict(c: CrmContact, group_ids: list[int] | None = None) -> dict[str, Any]:
    return {
        "id": c.id,
        "lead_id": c.lead_id,
        "name": c.name,
        "email": c.email,
        "email_status": c.email_status,
        "email_confidence": c.email_confidence,
        "phone": c.phone,
        "title": c.title,
        "work_designation": c.title,
        "company_name": c.company_name,
        "company_domain": c.company_domain,
        "location": c.location,
        "linkedin_url": c.linkedin_url,
        "stage": c.stage or "new",
        "crm_stage": c.stage or "new",
        "priority": c.priority or "medium",
        "assigned_user_id": c.assigned_user_id,
        "notes": c.notes or "",
        "crm_notes": c.notes or "",
        "tags": c.tags or [],
        "group_ids": group_ids if group_ids is not None else [],
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


@router.get("/groups")
def list_groups(
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    groups = (
        _scope_groups(db, user, organization_id)
        .order_by(CrmGroup.position.asc(), CrmGroup.id.asc())
        .all()
    )
    contacts = _scope_contacts(db, user, organization_id).all()
    static_counts = dict(
        db.query(CrmGroupMembership.group_id, func.count(CrmGroupMembership.id))
        .group_by(CrmGroupMembership.group_id)
        .all()
    )
    out = []
    for g in groups:
        if g.kind == "smart":
            cnt = sum(1 for c in contacts if _matches_smart_rules(c, g.smart_rules or {}))
        else:
            cnt = int(static_counts.get(g.id, 0))
        out.append(
            {
                "id": g.id,
                "parent_id": g.parent_id,
                "name": g.name,
                "description": g.description or "",
                "kind": g.kind,
                "smart_rules": g.smart_rules or {},
                "position": g.position,
                "permissions": g.permissions or {},
                "contact_count": cnt,
                "created_at": g.created_at,
            }
        )
    return {"items": out}


@router.post("/groups")
def create_group(
    body: GroupCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.organization_id is not None:
        require_org_access(db, user, body.organization_id, allow_viewer=False)
    if body.parent_id is not None:
        parent = _scope_groups(db, user, body.organization_id).filter(CrmGroup.id == body.parent_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent group not found")
    group = CrmGroup(
        owner_id=user.id,
        organization_id=body.organization_id,
        parent_id=body.parent_id,
        name=body.name.strip(),
        description=(body.description or "").strip() or None,
        kind=body.kind if body.kind in {"static", "smart"} else "static",
        smart_rules=body.smart_rules or {},
        position=body.position,
        permissions=body.permissions or {},
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return {
        "id": group.id,
        "parent_id": group.parent_id,
        "name": group.name,
        "description": group.description or "",
        "kind": group.kind,
        "smart_rules": group.smart_rules or {},
        "position": group.position,
        "contact_count": 0,
    }


@router.patch("/groups/{group_id}")
def update_group(
    group_id: int,
    body: GroupUpdate,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    group = _scope_groups(db, user, organization_id).filter(CrmGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if body.parent_id is not None:
        if body.parent_id == group.id:
            raise HTTPException(status_code=400, detail="Group cannot be its own parent")
        group.parent_id = body.parent_id or None
    if body.name is not None:
        group.name = body.name.strip()
    if body.description is not None:
        group.description = body.description.strip()
    if body.kind in {"static", "smart"}:
        group.kind = body.kind
    if body.smart_rules is not None:
        group.smart_rules = body.smart_rules
    if body.position is not None:
        group.position = body.position
    if body.permissions is not None:
        group.permissions = body.permissions
    db.commit()
    db.refresh(group)
    return {
        "id": group.id,
        "parent_id": group.parent_id,
        "name": group.name,
        "description": group.description or "",
        "kind": group.kind,
        "smart_rules": group.smart_rules or {},
        "position": group.position,
    }


@router.delete("/groups/{group_id}")
def delete_group(
    group_id: int,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    group = _scope_groups(db, user, organization_id).filter(CrmGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    # Re-parent children so nested subgroups are not orphaned.
    db.query(CrmGroup).filter(CrmGroup.parent_id == group.id).update({CrmGroup.parent_id: group.parent_id})
    db.query(CrmGroupMembership).filter(CrmGroupMembership.group_id == group.id).delete()
    db.delete(group)
    db.commit()
    return {"ok": True, "deleted_id": group_id}


def _collect_descendant_group_ids(groups: list[CrmGroup], root_id: int) -> set[int]:
    ids = {root_id}
    added = True
    while added:
        added = False
        for g in groups:
            if g.parent_id in ids and g.id not in ids:
                ids.add(g.id)
                added = True
    return ids


@router.get("/contacts")
def list_crm_contacts(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    q: str = "",
    stage: str = "",
    group_id: int | None = None,
    include_subgroups: bool = True,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    base = _scope_contacts(db, user, organization_id)
    query = base

    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.filter(
            func.lower(CrmContact.name).like(like)
            | func.lower(func.coalesce(CrmContact.email, "")).like(like)
            | func.lower(func.coalesce(CrmContact.company_name, "")).like(like)
            | func.lower(func.coalesce(CrmContact.title, "")).like(like)
        )
    if stage.strip():
        query = query.filter(func.coalesce(CrmContact.stage, "new") == stage.strip().lower())

    smart_group: CrmGroup | None = None
    if group_id is not None:
        g = _scope_groups(db, user, organization_id).filter(CrmGroup.id == group_id).first()
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        if g.kind == "smart":
            smart_group = g
        else:
            all_groups = _scope_groups(db, user, organization_id).all()
            gids = _collect_descendant_group_ids(all_groups, g.id) if include_subgroups else {g.id}
            cids = [
                cid
                for (cid,) in db.query(CrmGroupMembership.contact_id)
                .filter(CrmGroupMembership.group_id.in_(gids))
                .all()
            ]
            query = query.filter(CrmContact.id.in_(cids or [-1]))

    rows = query.order_by(CrmContact.id.desc()).all()
    if smart_group is not None:
        rows = [c for c in rows if _matches_smart_rules(c, smart_group.smart_rules or {})]

    total = len(rows)
    sliced = rows[(page - 1) * page_size : page * page_size]

    memberships = (
        db.query(CrmGroupMembership)
        .filter(CrmGroupMembership.contact_id.in_([c.id for c in sliced] or [-1]))
        .all()
    )
    by_contact: dict[int, list[int]] = {}
    for m in memberships:
        by_contact.setdefault(m.contact_id, []).append(m.group_id)

    stage_counts = {s: 0 for s in LEAD_LIFECYCLE_STAGES}
    for s, cnt in (
        base.with_entities(func.coalesce(CrmContact.stage, "new"), func.count(CrmContact.id))
        .group_by(func.coalesce(CrmContact.stage, "new"))
        .all()
    ):
        stage_counts[s] = int(cnt)

    total_pages = max(1, (total + page_size - 1) // page_size)
    return {
        "items": [_contact_dict(c, by_contact.get(c.id, [])) for c in sliced],
        "page": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
        },
        "stage_counts": stage_counts,
    }


@router.patch("/contacts/{contact_id}")
def update_crm_contact(
    contact_id: int,
    body: ContactUpdate,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = _scope_contacts(db, user, organization_id).filter(CrmContact.id == contact_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="CRM contact not found")
    if body.stage is not None:
        s = body.stage.strip().lower()
        if s not in LEAD_LIFECYCLE_STAGES:
            raise HTTPException(status_code=400, detail="Unknown stage")
        c.stage = s
    if body.priority is not None:
        c.priority = body.priority.strip().lower()
    if body.assigned_user_id is not None:
        c.assigned_user_id = body.assigned_user_id
    if body.notes is not None:
        c.notes = body.notes.strip()[:4000]
    if body.tags is not None:
        c.tags = [str(t).strip() for t in body.tags if str(t).strip()]
    for attr in ("name", "email", "phone", "title", "company_name", "location"):
        val = getattr(body, attr, None)
        if val is not None:
            setattr(c, attr, val.strip() if isinstance(val, str) else val)
    c.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return _contact_dict(c)


@router.delete("/contacts/{contact_id}")
def delete_crm_contact(
    contact_id: int,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = _scope_contacts(db, user, organization_id).filter(CrmContact.id == contact_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="CRM contact not found")
    db.query(CrmGroupMembership).filter(CrmGroupMembership.contact_id == c.id).delete()
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.post("/groups/{group_id}/contacts")
def add_contacts_to_group(
    group_id: int,
    body: MembershipModify,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    g = _scope_groups(db, user, organization_id).filter(CrmGroup.id == group_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    contacts = _scope_contacts(db, user, organization_id).filter(CrmContact.id.in_(body.contact_ids)).all()
    existing = {
        cid
        for (cid,) in db.query(CrmGroupMembership.contact_id)
        .filter(CrmGroupMembership.group_id == g.id)
        .all()
    }
    added = 0
    for c in contacts:
        if c.id not in existing:
            db.add(CrmGroupMembership(group_id=g.id, contact_id=c.id))
            added += 1
    db.commit()
    return {"ok": True, "added": added}


@router.post("/groups/{group_id}/move")
def move_contacts_between_groups(
    group_id: int,
    body: MembershipModify,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Move contacts out of ``group_id`` into ``target_group_id`` (or just remove if None)."""
    src = _scope_groups(db, user, organization_id).filter(CrmGroup.id == group_id).first()
    if not src:
        raise HTTPException(status_code=404, detail="Source group not found")
    db.query(CrmGroupMembership).filter(
        CrmGroupMembership.group_id == src.id,
        CrmGroupMembership.contact_id.in_(body.contact_ids),
    ).delete(synchronize_session=False)
    if body.target_group_id is not None:
        dst = _scope_groups(db, user, organization_id).filter(CrmGroup.id == body.target_group_id).first()
        if not dst:
            raise HTTPException(status_code=404, detail="Target group not found")
        existing = {
            cid
            for (cid,) in db.query(CrmGroupMembership.contact_id)
            .filter(CrmGroupMembership.group_id == dst.id)
            .all()
        }
        for cid in body.contact_ids:
            if cid not in existing:
                db.add(CrmGroupMembership(group_id=dst.id, contact_id=cid))
    db.commit()
    return {"ok": True}


def _evaluate_transfer(
    db: Session,
    user: User,
    lead_ids: list[int],
    org_id: int | None,
) -> tuple[list[LeadRecord], set[int], dict[str, Any]]:
    leads = _scope_leads(db, user, org_id).filter(LeadRecord.id.in_(lead_ids or [-1])).all()
    existing_contacts = _scope_contacts(db, user, org_id).all()
    existing_lead_ids = {c.lead_id for c in existing_contacts if c.lead_id}
    existing_identities = {c.identity_key for c in existing_contacts if c.identity_key}
    existing_emails = {(c.email or "").lower() for c in existing_contacts if c.email}

    dup_lead_ids: set[int] = set()
    with_email = 0
    verified_email = 0
    without_email = 0
    unverified = 0

    for lead in leads:
        em = (lead.email or "").strip().lower()
        if em:
            with_email += 1
            if (lead.email_status or "") in {"verified", "found"}:
                verified_email += 1
            else:
                unverified += 1
        else:
            without_email += 1

        if (
            lead.id in existing_lead_ids
            or (lead.identity_key and lead.identity_key in existing_identities)
            or (em and em in existing_emails)
        ):
            dup_lead_ids.add(lead.id)

    warnings: list[str] = []
    if without_email:
        warnings.append(f"{without_email} selected lead(s) do not have an email address yet.")
    if unverified:
        warnings.append(f"{unverified} lead(s) have inferred or unverified emails.")
    if dup_lead_ids:
        warnings.append(f"{len(dup_lead_ids)} lead(s) already exist in the CRM and will not be duplicated.")

    summary = {
        "selected_count": len(leads),
        "with_confirmed_email": verified_email,
        "with_email": with_email,
        "without_email": without_email,
        "unverified_email": unverified,
        "duplicate_count": len(dup_lead_ids),
        "warnings": warnings,
        "field_mappings": {
            "name": "name",
            "email": "email",
            "phone": "phone",
            "work_designation": "title",
            "company_name": "company_name",
            "personal_location": "location",
            "linkedin_url": "linkedin_url",
        },
    }
    return leads, dup_lead_ids, summary


@router.post("/transfer/preview")
def preview_crm_transfer(
    body: TransferPreviewRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Selective CRM transfer preview with explicit counts and warnings."""
    _, _, summary = _evaluate_transfer(db, user, body.lead_ids, body.organization_id)
    summary["group_id"] = body.group_id
    summary["subgroup_id"] = body.subgroup_id
    return summary


@router.post("/transfer")
def execute_crm_transfer(
    body: TransferExecuteRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Selective transfer of chosen discovery leads into the Internal CRM."""
    if not body.lead_ids:
        raise HTTPException(status_code=400, detail="Select at least one lead to add to the CRM")

    target_group_ids: list[int] = []
    for gid in (body.group_id, body.subgroup_id):
        if gid is not None:
            g = _scope_groups(db, user, body.organization_id).filter(CrmGroup.id == gid).first()
            if not g:
                raise HTTPException(status_code=404, detail=f"CRM group {gid} not found")
            target_group_ids.append(g.id)
    if body.organization_id is not None:
        require_org_access(db, user, body.organization_id, allow_viewer=False)

    leads, dup_ids, summary = _evaluate_transfer(db, user, body.lead_ids, body.organization_id)
    existing_by_key: dict[str, CrmContact] = {}
    for c in _scope_contacts(db, user, body.organization_id).all():
        if c.lead_id:
            existing_by_key[f"lead:{c.lead_id}"] = c
        if c.identity_key:
            existing_by_key[f"id:{c.identity_key}"] = c
        if c.email:
            existing_by_key[f"em:{c.email.lower()}"] = c

    transferred = 0
    updated = 0
    contact_ids_for_groups: list[int] = []

    for lead in leads:
        if body.exclude_without_email and not (lead.email or "").strip():
            continue
        if body.exclude_unverified and (lead.email_status or "") not in {"verified", "found"}:
            continue

        em = (lead.email or "").strip().lower()
        existing = (
            existing_by_key.get(f"lead:{lead.id}")
            or (existing_by_key.get(f"id:{lead.identity_key}") if lead.identity_key else None)
            or (existing_by_key.get(f"em:{em}") if em else None)
        )
        if existing:
            contact_ids_for_groups.append(existing.id)
            if body.allow_update_existing:
                existing.email = existing.email or lead.email
                existing.phone = existing.phone or lead.phone
                existing.title = existing.title or lead.work_designation
                existing.company_name = existing.company_name or lead.company_name
                existing.updated_at = datetime.utcnow()
                updated += 1
            continue

        contact = CrmContact(
            owner_id=user.id,
            organization_id=body.organization_id,
            lead_id=lead.id,
            identity_key=lead.identity_key,
            name=lead.name,
            email=lead.email,
            email_status=lead.email_status,
            email_confidence=lead.email_confidence,
            phone=lead.phone,
            title=lead.work_designation,
            company_name=lead.company_name,
            company_domain=lead.company_domain,
            location=lead.personal_location or lead.company_location,
            linkedin_url=lead.linkedin_url,
            stage=lead.lead_status or lead.crm_stage or "new",
            priority=lead.priority or "medium",
            notes=lead.crm_notes,
            tags=list(lead.tags or []),
        )
        db.add(contact)
        db.flush()
        contact_ids_for_groups.append(contact.id)
        transferred += 1
        db.add(
            LeadActivity(
                lead_id=lead.id,
                user_id=user.id,
                event_type="added_to_crm",
                detail="Added to CRM",
                meta={"contact_id": contact.id, "groups": target_group_ids},
            )
        )

    for gid in target_group_ids:
        existing_members = {
            cid
            for (cid,) in db.query(CrmGroupMembership.contact_id)
            .filter(CrmGroupMembership.group_id == gid)
            .all()
        }
        for cid in contact_ids_for_groups:
            if cid not in existing_members:
                db.add(CrmGroupMembership(group_id=gid, contact_id=cid))
                existing_members.add(cid)

    export_rec = CrmExportRecord(
        owner_id=user.id,
        organization_id=body.organization_id,
        kind="crm_transfer",
        destination="internal_crm",
        group_id=body.subgroup_id or body.group_id,
        selected_count=len(leads),
        transferred_count=transferred,
        with_email_count=summary["with_email"],
        duplicate_count=len(dup_ids),
        field_mapping=body.field_mapping or summary["field_mappings"],
        warnings=summary["warnings"],
    )
    db.add(export_rec)
    db.add(
        AuditLog(
            user_id=user.id,
            organization_id=body.organization_id,
            action="crm.selective_transfer",
            target_type="crm_group",
            target_id=body.subgroup_id or body.group_id,
            detail={"selected": len(leads), "transferred": transferred, "updated": updated},
        )
    )
    db.commit()
    return {
        "ok": True,
        "selected_count": len(leads),
        "transferred_count": transferred,
        "updated_count": updated,
        "duplicate_count": len(dup_ids),
        "warnings": summary["warnings"],
        "export_id": export_rec.id,
    }


@router.get("/exports")
def list_export_history(
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(CrmExportRecord)
    if organization_id is not None:
        require_org_access(db, user, organization_id)
        q = q.filter(CrmExportRecord.organization_id == organization_id)
    else:
        q = q.filter(CrmExportRecord.owner_id == user.id)
    rows = q.order_by(CrmExportRecord.created_at.desc()).limit(50).all()
    return {
        "items": [
            {
                "id": r.id,
                "kind": r.kind,
                "destination": r.destination,
                "group_id": r.group_id,
                "selected_count": r.selected_count,
                "transferred_count": r.transferred_count,
                "with_email_count": r.with_email_count,
                "duplicate_count": r.duplicate_count,
                "warnings": r.warnings or [],
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.get("/export")
def export_crm_contacts(
    format: str = "csv",
    group_id: int | None = None,
    contact_ids: str = "",
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = _scope_contacts(db, user, organization_id)
    if contact_ids.strip():
        ids = [int(x) for x in contact_ids.split(",") if x.strip().isdigit()]
        q = q.filter(CrmContact.id.in_(ids or [-1]))
    elif group_id is not None:
        all_groups = _scope_groups(db, user, organization_id).all()
        gids = _collect_descendant_group_ids(all_groups, group_id)
        cids = [
            cid
            for (cid,) in db.query(CrmGroupMembership.contact_id)
            .filter(CrmGroupMembership.group_id.in_(gids))
            .all()
        ]
        q = q.filter(CrmContact.id.in_(cids or [-1]))
    contacts = q.order_by(CrmContact.id.desc()).all()
    rows = [
        {
            "name": c.name,
            "email": c.email,
            "email_status": c.email_status,
            "email_confidence": c.email_confidence,
            "email_source": "crm",
            "phone": c.phone,
            "personal_location": c.location,
            "ai_description": c.notes,
            "work_field": "",
            "work_designation": c.title,
            "company_name": c.company_name,
            "company_location": c.location,
            "linkedin_url": c.linkedin_url,
            "relevance_score": "",
            "activity_status": c.stage,
            "raw_snippet": "",
        }
        for c in contacts
    ]
    db.add(
        CrmExportRecord(
            owner_id=user.id,
            organization_id=organization_id,
            kind=format.lower(),
            destination="file_download",
            group_id=group_id,
            selected_count=len(rows),
            transferred_count=len(rows),
            with_email_count=sum(1 for c in contacts if c.email),
        )
    )
    db.commit()
    if format.lower() == "xml":
        return Response(
            content=leads_to_xml(rows),
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="mark-crm.xml"'},
        )
    return Response(
        content=leads_to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="mark-crm.csv"'},
    )
