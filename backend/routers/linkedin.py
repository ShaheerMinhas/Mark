from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.dependencies import get_current_user
from backend.models import LinkedInSession, User
from backend.schemas import LinkedInSessionCreate, LinkedInSessionOut
from backend.services.crypto import encrypt_value
from backend.services.linkedin_session import validate_linkedin_cookie

router = APIRouter(prefix="/api/linkedin", tags=["linkedin"])


@router.get("/sessions", response_model=list[LinkedInSessionOut])
def list_sessions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(LinkedInSession).filter(LinkedInSession.user_id == user.id).all()


@router.post("/sessions", response_model=LinkedInSessionOut)
def connect_session(body: LinkedInSessionCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    valid, name, message = validate_linkedin_cookie(body.session_cookie)
    if not valid:
        raise HTTPException(status_code=400, detail=message)

    session = LinkedInSession(
        user_id=user.id,
        label=body.label.strip() or "Primary",
        encrypted_cookie=encrypt_value(body.session_cookie.strip()),
        linkedin_name=name,
        is_valid=True,
        last_validated_at=datetime.utcnow(),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.post("/sessions/{session_id}/validate", response_model=LinkedInSessionOut)
def revalidate_session(session_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = (
        db.query(LinkedInSession)
        .filter(LinkedInSession.id == session_id, LinkedInSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    from backend.services.crypto import decrypt_value

    valid, name, _ = validate_linkedin_cookie(decrypt_value(session.encrypted_cookie))
    session.is_valid = valid
    session.linkedin_name = name if valid else session.linkedin_name
    session.last_validated_at = datetime.utcnow()
    db.commit()
    db.refresh(session)
    if not valid:
        raise HTTPException(status_code=400, detail="Session cookie expired — reconnect LinkedIn")
    return session


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    session = (
        db.query(LinkedInSession)
        .filter(LinkedInSession.id == session_id, LinkedInSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete(session)
    db.commit()
    return {"ok": True}
