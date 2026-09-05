"""Short home-page videos from Global churches (max ~30s, live 24 hours)."""
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import uuid
import shutil

from fastapi import APIRouter, Depends, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import User, UserRole, ChurchUnit, HomeVideo
from app.auth import require_user, require_roles, role_val

router = APIRouter(prefix="/videos", tags=["videos"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
UPLOAD = Path("app/static/uploads/videos")
UPLOAD.mkdir(parents=True, exist_ok=True)

MAX_BYTES = 25 * 1024 * 1024  # 25 MB soft for short clip
ALLOWED = {"video/mp4", "video/webm", "video/quicktime", "video/x-msvideo"}


def _is_global_admin(user: User, session: Session) -> bool:
    rv = role_val(user.role)
    if rv == "general_admin":
        return True
    if rv != "church_admin" or not user.church_id:
        return False
    church = session.get(ChurchUnit, user.church_id)
    if not church:
        return False
    lv = str(getattr(church.level, "value", church.level)).lower()
    return lv in ("global", "global_church")


@router.get("", response_class=HTMLResponse)
async def videos_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not _is_global_admin(user, session) and role_val(user.role) != "general_admin":
        raise HTTPException(403, "Only Global church admins can manage home videos")
    cid = user.church_id
    now = datetime.utcnow()
    q = select(HomeVideo).order_by(HomeVideo.created_at.desc())
    if role_val(user.role) != "general_admin" and cid:
        q = select(HomeVideo).where(HomeVideo.church_id == cid).order_by(HomeVideo.created_at.desc())
    rows = list(session.exec(q.limit(30)).all())
    items = []
    for v in rows:
        ch = session.get(ChurchUnit, v.church_id)
        live = v.is_active and v.ends_at and v.ends_at > now
        items.append({"v": v, "church": ch, "live": live})
    return templates.TemplateResponse("videos/list.html", {
        "request": request, "user": user, "items": items,
        "can_upload": _is_global_admin(user, session) or role_val(user.role) == "general_admin",
    })


@router.post("/upload")
async def upload_video(
    title: str = Form(""),
    caption: str = Form(""),
    file: UploadFile = File(...),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    if not _is_global_admin(user, session):
        raise HTTPException(403, "Only Global church admins can upload home videos")
    if not user.church_id:
        raise HTTPException(400, "No church linked")
    if file.content_type and file.content_type not in ALLOWED and not (file.filename or "").lower().endswith((".mp4", ".webm", ".mov")):
        raise HTTPException(400, "Use MP4 or WebM short video (about 30 seconds)")
    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(400, "Video too large (max 25 MB for a short clip)")
    ext = (file.filename or "clip.mp4").rsplit(".", 1)[-1].lower()
    if ext not in ("mp4", "webm", "mov"):
        ext = "mp4"
    fname = f"home_{user.church_id}_{uuid.uuid4().hex[:10]}.{ext}"
    dest = UPLOAD / fname
    dest.write_bytes(data)
    now = datetime.utcnow()
    # Deactivate previous live videos for this church (one live clip at a time per global)
    for old in session.exec(
        select(HomeVideo).where(HomeVideo.church_id == user.church_id, HomeVideo.is_active == True)
    ).all():
        if old.ends_at and old.ends_at > now:
            old.is_active = False
            session.add(old)
    vid = HomeVideo(
        church_id=user.church_id,
        title=(title.strip() or "Message from our Global church"),
        caption=caption.strip() or None,
        file_path=f"/static/uploads/videos/{fname}",
        uploaded_by=user.id,
        duration_seconds=30,
        is_active=True,
        starts_at=now,
        ends_at=now + timedelta(hours=24),
    )
    session.add(vid)
    session.commit()
    return RedirectResponse("/videos", status_code=303)


@router.post("/{video_id}/deactivate")
async def deactivate_video(
    video_id: int,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    v = session.get(HomeVideo, video_id)
    if not v:
        raise HTTPException(404)
    if role_val(user.role) != "general_admin" and v.church_id != user.church_id:
        raise HTTPException(403)
    v.is_active = False
    session.add(v)
    session.commit()
    return RedirectResponse("/videos", status_code=303)
