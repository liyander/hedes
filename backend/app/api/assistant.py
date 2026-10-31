"""The platform assistant API, plus the shared help content shown in every page's info panel."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import get_db
from ..core.security import Principal, current_principal, require_human, require_role
from ..models import AssistantAction, AssistantMessage, AssistantThread
from ..services import assistant, help_content
from .common import iso

router = APIRouter(prefix="/api/v1", tags=["assistant"])


@router.get("/help")
def help_all(p: Principal = Depends(current_principal)):
    return {"concepts": help_content.CONCEPTS, "pages": help_content.PAGES}


@router.get("/assistant/status")
def status(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return {"configured": assistant.configured(db, p.org_id),
            "tools": [{"name": n, "needs_approval": t[0], "role": t[1], "description": t[3]} for n, t in assistant.TOOLS.items()]}


@router.get("/assistant/taggables")
def taggables(q: str = "", p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return assistant.search_taggable(db, p, q[:60])


def _thread(db: Session, p: Principal, tid: str) -> AssistantThread:
    t = db.get(AssistantThread, tid)
    if not t or t.organization_id != p.org_id or t.user_id != p.id:
        raise HTTPException(404, "Conversation not found")
    return t


@router.get("/assistant/threads")
def list_threads(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(AssistantThread).where(AssistantThread.user_id == p.id).order_by(AssistantThread.updated_at.desc()).limit(40)).all()
    return [{"id": t.id, "title": t.title, "updated_at": iso(t.updated_at)} for t in rows]


class ThreadIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)


@router.post("/assistant/threads")
def create_thread(body: ThreadIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    t = AssistantThread(organization_id=p.org_id, user_id=p.id, title=body.title or "New conversation")
    db.add(t)
    db.commit()
    return {"id": t.id, "title": t.title}


@router.get("/assistant/threads/{tid}")
def get_thread(tid: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    t = _thread(db, p, tid)
    msgs = db.scalars(select(AssistantMessage).where(AssistantMessage.thread_id == t.id).order_by(AssistantMessage.created_at)).all()
    return {"id": t.id, "title": t.title, "messages": [assistant.message_view(db, m) for m in msgs if m.role != "system"]}


@router.delete("/assistant/threads/{tid}")
def delete_thread(tid: str, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    t = _thread(db, p, tid)
    for model in (AssistantAction, AssistantMessage):
        for row in db.scalars(select(model).where(model.thread_id == t.id)).all():
            db.delete(row)
    db.delete(t)
    db.commit()
    return {"ok": True}


class TagIn(BaseModel):
    type: str = Field(pattern="^(integration|source|workflow|credential)$")
    id: str = Field(max_length=64)
    label: str = Field(default="", max_length=120)


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    tags: list[TagIn] = Field(default_factory=list, max_length=6)
    page: str | None = Field(default=None, max_length=40)


@router.post("/assistant/threads/{tid}/messages")
def send(tid: str, body: MessageIn, p: Principal = Depends(require_role("user")), db: Session = Depends(get_db)):
    t = _thread(db, p, tid)
    msg = assistant.chat(db, p, t, body.content, [x.model_dump() for x in body.tags], body.page)
    return {"message": assistant.message_view(db, msg), "title": t.title}


class DecisionIn(BaseModel):
    page: str | None = Field(default=None, max_length=40)


def _action(db: Session, p: Principal, aid: str) -> AssistantAction:
    a = db.get(AssistantAction, aid)
    if not a or a.organization_id != p.org_id or a.user_id != p.id:
        raise HTTPException(404, "Action not found")
    return a


@router.post("/assistant/actions/{aid}/approve")
def approve(aid: str, body: DecisionIn, p: Principal = Depends(require_human("user")), db: Session = Depends(get_db)):
    """Only an interactive (browser) session can approve; API tokens cannot."""
    act = assistant.decide(db, p, _action(db, p, aid), True)
    follow = assistant.follow_up(db, p, db.get(AssistantThread, act.thread_id), act, body.page)
    return {"action": assistant.action_view(act), "message": assistant.message_view(db, follow) if follow else None}


@router.post("/assistant/actions/{aid}/reject")
def reject(aid: str, body: DecisionIn, p: Principal = Depends(require_human("user")), db: Session = Depends(get_db)):
    act = assistant.decide(db, p, _action(db, p, aid), False)
    return {"action": assistant.action_view(act), "message": None}
