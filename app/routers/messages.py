"""District messaging: members to admin/pastor; broadcast with privilege."""
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import User, UserRole, ChurchMember, DistrictMessage
from app.auth import require_user, role_val

router = APIRouter(prefix="/messages", tags=["messages"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _district_id_for_user(user: User, session: Session) -> Optional[int]:
    if user.church_id:
        return user.church_id
    if user.member_id:
        m = session.get(ChurchMember, user.member_id)
        if m:
            return m.church_id
    return None


def _can_broadcast(user: User) -> bool:
    rv = role_val(user.role)
    if rv in ("church_admin", "general_admin", "data_officer"):
        return True
    return bool(getattr(user, "can_broadcast", False))


@router.get("", response_class=HTMLResponse)
async def messages_inbox(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    cid = _district_id_for_user(user, session)
    if not cid:
        raise HTTPException(400, "No church linked")
    all_msg = list(session.exec(
        select(DistrictMessage).where(DistrictMessage.church_id == cid)
        .order_by(DistrictMessage.created_at.desc()).limit(100)
    ).all())
    visible = []
    for m in all_msg:
        if m.is_broadcast and m.to_role == "members":
            if m.recipient_user_ids:
                ids = {int(x) for x in m.recipient_user_ids.split(",") if x.strip().isdigit()}
                if user.id in ids or role_val(user.role) in ("church_admin", "general_admin"):
                    visible.append(m)
            else:
                visible.append(m)
            continue
        if m.to_role in ("admin", "pastor"):
            if role_val(user.role) in ("church_admin", "general_admin", "data_officer") or m.sender_user_id == user.id:
                visible.append(m)
            continue
        if m.sender_user_id == user.id:
            visible.append(m)
            continue
        if m.recipient_user_ids and str(user.id) in m.recipient_user_ids.split(","):
            visible.append(m)
    rows = []
    for m in visible:
        sender = session.get(User, m.sender_user_id)
        rows.append({"m": m, "sender": sender})
    members = list(session.exec(
        select(ChurchMember).where(
            ChurchMember.church_id == cid,
            ChurchMember.approval_status == "approved",
        )
    ).all())
    member_users = []
    for mem in members:
        u = session.exec(select(User).where(User.member_id == mem.id)).first()
        if u:
            member_users.append({"user": u, "member": mem})
    return templates.TemplateResponse("messages/inbox.html", {
        "request": request, "user": user, "rows": rows,
        "can_broadcast": _can_broadcast(user),
        "member_users": member_users,
        "is_admin": role_val(user.role) in ("church_admin", "general_admin", "data_officer"),
    })


@router.post("/send")
async def send_message(
    body: str = Form(...),
    subject: str = Form(""),
    to_role: str = Form("admin"),
    broadcast: str = Form(""),
    recipient_ids: List[str] = Form(default=[]),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    cid = _district_id_for_user(user, session)
    if not cid:
        raise HTTPException(400, "No church linked")
    body = body.strip()
    if not body:
        raise HTTPException(400, "Message required")
    is_broadcast = broadcast == "yes"
    if is_broadcast and not _can_broadcast(user):
        raise HTTPException(403, "You are not approved to broadcast")
    rv = role_val(user.role)
    if rv == "member" and not is_broadcast:
        if to_role not in ("admin", "pastor"):
            to_role = "admin"
    msg = DistrictMessage(
        church_id=cid,
        sender_user_id=user.id,
        subject=subject.strip() or None,
        body=body,
        is_broadcast=is_broadcast,
        recipient_user_ids=(",".join(recipient_ids) if recipient_ids else None) if is_broadcast else None,
        to_role="members" if is_broadcast else to_role,
    )
    session.add(msg)
    session.commit()
    return RedirectResponse("/messages", status_code=303)
