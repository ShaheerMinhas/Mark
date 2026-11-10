"""People database (Lead Management) across every search in the active scope.

Discovery leads live in ``lead_records``. This is distinct from the selective
``crm_contacts`` table so discovered leads do not automatically enter the CRM.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session, joinedload

from backend.database import get_db
from backend.dependencies import get_current_user, require_org_access
from backend.models import (
    LEAD_LIFECYCLE_STAGES,
    LeadActivity,
    LeadNote,
    LeadRecord,
    LeadTask,
    SearchJob,
    User,
)
from backend.schemas import LeadOut, PageMeta, PeopleItem, PeoplePageOut, PeopleUpdate

router = APIRouter(prefix="/api/people", tags=["people"])

CRM_STAGES = LEAD_LIFECYCLE_STAGES
MAX_PAGE_SIZE = 200


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    due_at: datetime | None = None
    assigned_user_id: int | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    due_at: datetime | None = None
    completed: bool | None = None
    assigned_user_id: int | None = None


class BulkPeopleRequest(BaseModel):
    lead_ids: list[int] = Field(default_factory=list, max_length=5000)
    action: str  # update | delete | archive | restore | enrich
    lead_status: str | None = None
    crm_stage: str | None = None
    priority: str | None = None
    assigned_user_id: int | None = None
    tags_add: list[str] = Field(default_factory=list)


def _owned_leads(db: Session, user: User, organization_id: int | None = None):
    q = db.query(LeadRecord).join(SearchJob, LeadRecord.search_job_id == SearchJob.id)
    if organization_id is not None:
        require_org_access(db, user, organization_id)
        return q.filter(SearchJob.organization_id == organization_id)
    return q.filter(SearchJob.owner_id == user.id)


def _record_activity(
    db: Session, lead_id: int, user_id: int | None, event_type: str, detail: str = "", meta: dict | None = None
) -> None:
    db.add(
        LeadActivity(
            lead_id=lead_id,
            user_id=user_id,
            event_type=event_type,
            detail=detail[:500] if detail else None,
            meta=meta or {},
        )
    )


def _has_email():
    return and_(LeadRecord.email.isnot(None), LeadRecord.email != "")


def _missing_email():
    return or_(LeadRecord.email.is_(None), LeadRecord.email == "")


def _email_was_looked_up():
    """A lookup wrote a timestamp or a status. An address alone is not a lookup."""
    return or_(
        LeadRecord.email_enriched_at.isnot(None),
        and_(LeadRecord.email_status.isnot(None), LeadRecord.email_status != ""),
    )


def _apply_email_filter(query, email_status: str):
    es = (email_status or "").strip().lower()
    if not es:
        return query
    if es in {"with_email", "has_email"}:
        return query.filter(_has_email())
    if es in {"without_email", "missing"}:
        return query.filter(_missing_email())
    if es == "not_searched":
        return query.filter(_missing_email(), ~_email_was_looked_up())
    if es == "no_email_found":
        return query.filter(_missing_email(), _email_was_looked_up())
    return query.filter(func.lower(func.coalesce(LeadRecord.email_status, "")) == es)


def _email_facets(query) -> dict[str, int]:
    all_count = int(query.count())
    with_email = int(query.filter(_has_email()).count())
    not_searched = int(query.filter(_missing_email(), ~_email_was_looked_up()).count())
    no_email_found = int(query.filter(_missing_email(), _email_was_looked_up()).count())
    return {
        "all": all_count,
        "with_email": with_email,
        "without_email": all_count - with_email,
        "not_searched": not_searched,
        "no_email_found": no_email_found,
    }


def _item(lead: LeadRecord) -> PeopleItem:
    job = lead.search_job
    data = LeadOut.model_validate(lead).model_dump()
    stage = lead.lead_status or lead.crm_stage or "new"
    data["crm_stage"] = lead.crm_stage or stage
    data["lead_status"] = stage
    return PeopleItem(
        **data,
        search_id=lead.search_job_id,
        search_name=job.name if job else "",
    )


@router.get("", response_model=PeoplePageOut)
def list_people(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    q: str = "",
    stage: str = "",
    status: str = "",
    email_status: str = "",
    company: str = "",
    location: str = "",
    has_phone: str = "",
    priority: str = "",
    tag: str = "",
    assigned_user_id: int | None = None,
    include_archived: bool = False,
    archived_only: bool = False,
    sort: str = "recent",
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    base = _owned_leads(db, user, organization_id)
    query = base

    if archived_only:
        query = query.filter(
            LeadRecord.archived_at.isnot(None)
            | (func.coalesce(LeadRecord.lead_status, LeadRecord.crm_stage, "new") == "archived")
        )
    elif not include_archived and stage != "archived" and status != "archived":
        query = query.filter(LeadRecord.archived_at.is_(None))

    needle = q.strip().lower()
    if needle:
        like = f"%{needle}%"
        query = query.filter(
            func.lower(LeadRecord.name).like(like)
            | func.lower(func.coalesce(LeadRecord.email, "")).like(like)
            | func.lower(func.coalesce(LeadRecord.company_name, "")).like(like)
            | func.lower(func.coalesce(LeadRecord.work_designation, "")).like(like)
            | func.lower(func.coalesce(LeadRecord.personal_location, "")).like(like)
        )

    chosen_stage = (status or stage).strip().lower()
    if chosen_stage:
        if chosen_stage not in CRM_STAGES:
            raise HTTPException(status_code=400, detail="Unknown pipeline stage")
        query = query.filter(
            (func.coalesce(LeadRecord.lead_status, "") == chosen_stage)
            | (func.coalesce(LeadRecord.crm_stage, "new") == chosen_stage)
        )

    if company.strip():
        query = query.filter(func.lower(func.coalesce(LeadRecord.company_name, "")).like(f"%{company.strip().lower()}%"))
    if location.strip():
        loc = f"%{location.strip().lower()}%"
        query = query.filter(
            func.lower(func.coalesce(LeadRecord.personal_location, "")).like(loc)
            | func.lower(func.coalesce(LeadRecord.company_location, "")).like(loc)
        )
    phone_choice = has_phone.strip().lower()
    if phone_choice in {"yes", "true", "1"}:
        query = query.filter(and_(LeadRecord.phone.isnot(None), LeadRecord.phone != ""))
    elif phone_choice in {"no", "false", "0"}:
        query = query.filter(or_(LeadRecord.phone.is_(None), LeadRecord.phone == ""))
    if priority.strip():
        query = query.filter(func.coalesce(LeadRecord.priority, "medium") == priority.strip().lower())
    if assigned_user_id is not None:
        query = query.filter(LeadRecord.assigned_user_id == assigned_user_id)

    email_counts = _email_facets(query)
    query = _apply_email_filter(query, email_status)

    has_email_rank = case((_has_email(), 0), else_=1)
    priority_rank = case(
        (func.lower(func.coalesce(LeadRecord.priority, "medium")) == "high", 0),
        (func.lower(func.coalesce(LeadRecord.priority, "medium")) == "low", 2),
        else_=1,
    )
    sorts = {
        "recent": (LeadRecord.id.desc(),),
        "relevance": (LeadRecord.relevance_score.desc(), LeadRecord.id.desc()),
        "name": (func.lower(LeadRecord.name).asc(), LeadRecord.id.asc()),
        "company": (func.lower(func.coalesce(LeadRecord.company_name, "")).asc(), LeadRecord.id.asc()),
        "title": (func.lower(func.coalesce(LeadRecord.work_designation, "")).asc(), LeadRecord.id.asc()),
        "priority": (priority_rank.asc(), LeadRecord.id.desc()),
        "email": (has_email_rank.asc(), func.lower(LeadRecord.name).asc(), LeadRecord.id.asc()),
    }
    order = sorts.get(sort, sorts["recent"])

    total = query.count()
    rows = (
        query.options(joinedload(LeadRecord.search_job))
        .order_by(*order)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    if tag.strip():
        t_low = tag.strip().lower()
        rows = [r for r in rows if any(str(x).lower() == t_low for x in (r.tags or []))]

    counts = {name: 0 for name in CRM_STAGES}
    grouped = (
        base.with_entities(
            func.coalesce(LeadRecord.crm_stage, "new"), func.count(LeadRecord.id)
        )
        .group_by(func.coalesce(LeadRecord.crm_stage, "new"))
        .all()
    )
    for name, count in grouped:
        key = name if name in counts else "new"
        counts[key] = counts.get(key, 0) + int(count)

    archive_clause = LeadRecord.archived_at.is_(None)
    if archived_only:
        library_total = int(
            base.filter(
                LeadRecord.archived_at.isnot(None)
                | (func.coalesce(LeadRecord.lead_status, LeadRecord.crm_stage, "new") == "archived")
            ).count()
        )
    elif include_archived or stage == "archived" or status == "archived":
        library_total = int(base.count())
    else:
        library_total = int(base.filter(archive_clause).count())

    total_pages = max(1, (total + page_size - 1) // page_size)
    return PeoplePageOut(
        items=[_item(row) for row in rows],
        page=PageMeta(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            has_next=page < total_pages,
        ),
        stage_counts=counts,
        email_counts=email_counts,
        library_total=library_total,
    )


@router.get("/{lead_id}")
def get_person_detail(
    lead_id: int,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    lead = (
        _owned_leads(db, user, organization_id)
        .options(joinedload(LeadRecord.search_job))
        .filter(LeadRecord.id == lead_id)
        .first()
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Person not found")
    notes = (
        db.query(LeadNote)
        .filter(LeadNote.lead_id == lead.id)
        .order_by(LeadNote.created_at.desc())
        .all()
    )
    tasks = (
        db.query(LeadTask)
        .filter(LeadTask.lead_id == lead.id)
        .order_by(LeadTask.completed.asc(), LeadTask.created_at.desc())
        .all()
    )
    activities = (
        db.query(LeadActivity)
        .filter(LeadActivity.lead_id == lead.id)
        .order_by(LeadActivity.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "person": _item(lead).model_dump(),
        "notes": [
            {
                "id": n.id,
                "body": n.body,
                "author_name": n.author_name or "Team",
                "created_at": n.created_at,
            }
            for n in notes
        ],
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "due_at": t.due_at,
                "completed": t.completed,
                "assigned_user_id": t.assigned_user_id,
                "created_at": t.created_at,
            }
            for t in tasks
        ],
        "activities": [
            {
                "id": a.id,
                "event_type": a.event_type,
                "detail": a.detail,
                "meta": a.meta or {},
                "created_at": a.created_at,
            }
            for a in activities
        ],
    }


@router.patch("/{lead_id}", response_model=PeopleItem)
def update_person(
    lead_id: int,
    body: PeopleUpdate,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lead = (
        _owned_leads(db, user, organization_id)
        .options(joinedload(LeadRecord.search_job))
        .filter(LeadRecord.id == lead_id)
        .first()
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Person not found")

    next_stage = body.lead_status if body.lead_status is not None else body.crm_stage
    if next_stage is not None:
        stage = next_stage.strip().lower()
        if stage not in CRM_STAGES:
            raise HTTPException(status_code=400, detail="Unknown pipeline stage")
        lead.crm_stage = stage
        lead.lead_status = stage
        if stage == "archived" and not lead.archived_at:
            lead.archived_at = datetime.utcnow()
        _record_activity(db, lead.id, user.id, "stage_changed", f"Stage set to {stage}")

    if body.crm_notes is not None:
        lead.crm_notes = body.crm_notes.strip()[:4000]
        _record_activity(db, lead.id, user.id, "notes_updated", "Updated summary notes")
    if body.priority is not None:
        lead.priority = body.priority.strip().lower()[:16]
    if body.assigned_user_id is not None:
        lead.assigned_user_id = body.assigned_user_id
        _record_activity(db, lead.id, user.id, "assigned", f"Assigned to user {body.assigned_user_id}")
    if body.follow_up_date is not None:
        lead.follow_up_date = body.follow_up_date
    if body.archived is True:
        lead.archived_at = datetime.utcnow()
        lead.lead_status = "archived"
        lead.crm_stage = "archived"
        _record_activity(db, lead.id, user.id, "archived", "Lead archived")
    elif body.archived is False:
        lead.archived_at = None
        if lead.lead_status == "archived":
            lead.lead_status = "new"
            lead.crm_stage = "new"
        _record_activity(db, lead.id, user.id, "restored", "Lead restored from archive")
    if body.tags is not None:
        lead.tags = [str(t).strip() for t in body.tags if str(t).strip()]
    if body.custom_fields is not None:
        merged = dict(lead.custom_fields or {})
        merged.update(body.custom_fields)
        lead.custom_fields = merged
    for attr in ("name", "email", "phone", "work_designation", "company_name", "personal_location"):
        val = getattr(body, attr, None)
        if val is not None:
            setattr(lead, attr, val.strip() if isinstance(val, str) else val)

    lead.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(lead)
    return _item(lead)


@router.delete("/{lead_id}")
def delete_person(
    lead_id: int,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lead = _owned_leads(db, user, organization_id).filter(LeadRecord.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Person not found")
    db.delete(lead)
    db.commit()
    return {"ok": True, "deleted_id": lead_id}


@router.post("/{lead_id}/notes")
def add_person_note(
    lead_id: int,
    body: NoteCreate,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lead = _owned_leads(db, user, organization_id).filter(LeadRecord.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Person not found")
    note = LeadNote(
        lead_id=lead.id,
        user_id=user.id,
        author_name=user.full_name,
        body=body.body.strip(),
    )
    db.add(note)
    _record_activity(db, lead.id, user.id, "note_added", body.body.strip()[:120])
    db.commit()
    db.refresh(note)
    return {
        "id": note.id,
        "body": note.body,
        "author_name": note.author_name,
        "created_at": note.created_at,
    }


@router.post("/{lead_id}/tasks")
def add_person_task(
    lead_id: int,
    body: TaskCreate,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lead = _owned_leads(db, user, organization_id).filter(LeadRecord.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Person not found")
    task = LeadTask(
        lead_id=lead.id,
        assigned_user_id=body.assigned_user_id or user.id,
        title=body.title.strip(),
        due_at=body.due_at,
    )
    db.add(task)
    _record_activity(db, lead.id, user.id, "task_created", task.title)
    db.commit()
    db.refresh(task)
    return {
        "id": task.id,
        "title": task.title,
        "due_at": task.due_at,
        "completed": task.completed,
        "assigned_user_id": task.assigned_user_id,
        "created_at": task.created_at,
    }


@router.patch("/{lead_id}/tasks/{task_id}")
def update_person_task(
    lead_id: int,
    task_id: int,
    body: TaskUpdate,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lead = _owned_leads(db, user, organization_id).filter(LeadRecord.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Person not found")
    task = db.query(LeadTask).filter(LeadTask.id == task_id, LeadTask.lead_id == lead.id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if body.title is not None:
        task.title = body.title.strip()
    if body.due_at is not None:
        task.due_at = body.due_at
    if body.completed is not None:
        task.completed = body.completed
    if body.assigned_user_id is not None:
        task.assigned_user_id = body.assigned_user_id
    db.commit()
    db.refresh(task)
    return {
        "id": task.id,
        "title": task.title,
        "due_at": task.due_at,
        "completed": task.completed,
        "assigned_user_id": task.assigned_user_id,
    }


@router.post("/bulk")
def bulk_people_action(
    body: BulkPeopleRequest,
    organization_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Chunked bulk operations so 200–2,000 leads update cleanly."""
    if not body.lead_ids:
        return {"ok": True, "affected": 0}
    ids = list(dict.fromkeys(int(i) for i in body.lead_ids))
    action = body.action.strip().lower()

    affected = 0
    chunk_size = 250
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start : start + chunk_size]
        rows = _owned_leads(db, user, organization_id).filter(LeadRecord.id.in_(chunk)).all()
        if action == "delete":
            for r in rows:
                db.delete(r)
                affected += 1
        elif action == "archive":
            now = datetime.utcnow()
            for r in rows:
                r.archived_at = now
                r.lead_status = "archived"
                r.crm_stage = "archived"
                r.updated_at = now
                affected += 1
        elif action == "restore":
            now = datetime.utcnow()
            for r in rows:
                r.archived_at = None
                if r.lead_status == "archived":
                    r.lead_status = "new"
                    r.crm_stage = "new"
                r.updated_at = now
                affected += 1
        elif action == "update":
            stage = (body.lead_status or body.crm_stage or "").strip().lower()
            if stage and stage not in CRM_STAGES:
                raise HTTPException(status_code=400, detail="Unknown pipeline stage")
            now = datetime.utcnow()
            for r in rows:
                if stage:
                    r.lead_status = stage
                    r.crm_stage = stage
                if body.priority:
                    r.priority = body.priority.strip().lower()
                if body.assigned_user_id is not None:
                    r.assigned_user_id = body.assigned_user_id
                if body.tags_add:
                    existing = list(r.tags or [])
                    for t in body.tags_add:
                        if t and t not in existing:
                            existing.append(t)
                    r.tags = existing
                r.updated_at = now
                affected += 1
        elif action == "enrich":
            from backend.services.EmailModule.jobs import spawn_email_job, start_email_job

            by_search: dict[int, list[int]] = {}
            for r in rows:
                by_search.setdefault(r.search_job_id, []).append(r.id)
                affected += 1
            for sid, lids in by_search.items():
                ej = start_email_job(db, sid, len(lids))
                spawn_email_job(ej.id, lids, use_providers=True)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown bulk action: {action}")
        db.commit()

    return {"ok": True, "action": action, "affected": affected}
