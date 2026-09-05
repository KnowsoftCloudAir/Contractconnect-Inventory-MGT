
"""YouTube links for home showcase + Global church data export/import."""
from pathlib import Path
from datetime import datetime
from typing import Optional, List
import csv
import io
import re

from fastapi import APIRouter, Depends, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import (
    User, UserRole, ChurchUnit, ChurchLevel, ChurchMember, WeeklyStat, YoutubeChannelLink
)
from app.auth import require_user, require_roles, role_val

router = APIRouter(tags=["youtube-data"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def extract_youtube_id(url: str) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"youtube\.com/watch\?.*?v=([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    return None


def _is_global_church_admin(user: User, session: Session) -> bool:
    if role_val(user.role) == "general_admin":
        return True
    if role_val(user.role) != "church_admin" or not user.church_id:
        return False
    ch = session.get(ChurchUnit, user.church_id)
    if not ch:
        return False
    lv = str(getattr(ch.level, "value", ch.level)).lower()
    return lv in ("global", "global_church")


def _descendant_ids(session: Session, root_id: int) -> List[int]:
    ids = [root_id]
    queue = [root_id]
    while queue:
        pid = queue.pop(0)
        for k in session.exec(select(ChurchUnit).where(ChurchUnit.parent_id == pid)).all():
            ids.append(k.id)
            queue.append(k.id)
    return ids


# ---------- YouTube management (Global + GA) ----------
@router.get("/youtube", response_class=HTMLResponse)
async def youtube_manage(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    rv = role_val(user.role)
    if rv == "general_admin":
        links = list(session.exec(select(YoutubeChannelLink).order_by(YoutubeChannelLink.created_at.desc())).all())
        rows = []
        for L in links:
            ch = session.get(ChurchUnit, L.church_id) if L.church_id else None
            rows.append({"L": L, "church": ch})
        return templates.TemplateResponse("youtube/admin.html", {
            "request": request, "user": user, "rows": rows,
        })
    if not _is_global_church_admin(user, session):
        raise HTTPException(403, "Global church admin only")
    links = list(session.exec(
        select(YoutubeChannelLink).where(YoutubeChannelLink.church_id == user.church_id)
        .order_by(YoutubeChannelLink.created_at.desc())
    ).all())
    return templates.TemplateResponse("youtube/global.html", {
        "request": request, "user": user, "links": links,
    })


@router.post("/youtube/submit")
async def youtube_submit(
    title: str = Form("YouTube"),
    youtube_url: str = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    rv = role_val(user.role)
    vid = extract_youtube_id(youtube_url)
    if not vid:
        raise HTTPException(400, "Could not read a YouTube video URL. Use a watch or youtu.be link.")
    if rv == "general_admin":
        link = YoutubeChannelLink(
            church_id=None,
            owner_type="general_admin",
            title=title.strip() or "Knowsoft",
            youtube_url=youtube_url.strip(),
            youtube_video_id=vid,
            is_approved=True,
            is_active=True,
            submitted_by=user.id,
            approved_by=user.id,
            approved_at=datetime.utcnow(),
        )
        session.add(link)
        session.commit()
        return RedirectResponse("/youtube", status_code=303)
    if not _is_global_church_admin(user, session):
        raise HTTPException(403)
    link = YoutubeChannelLink(
        church_id=user.church_id,
        owner_type="global_church",
        title=title.strip() or "Our channel",
        youtube_url=youtube_url.strip(),
        youtube_video_id=vid,
        is_approved=False,
        is_active=True,
        submitted_by=user.id,
    )
    session.add(link)
    session.commit()
    return RedirectResponse("/youtube", status_code=303)


@router.post("/youtube/{link_id}/approve")
async def youtube_approve(
    link_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    L = session.get(YoutubeChannelLink, link_id)
    if not L:
        raise HTTPException(404)
    L.is_approved = True
    L.approved_by = user.id
    L.approved_at = datetime.utcnow()
    session.add(L)
    session.commit()
    return RedirectResponse("/youtube", status_code=303)


@router.post("/youtube/{link_id}/reject")
async def youtube_reject(
    link_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    L = session.get(YoutubeChannelLink, link_id)
    if not L:
        raise HTTPException(404)
    L.is_approved = False
    L.is_active = False
    session.add(L)
    session.commit()
    return RedirectResponse("/youtube", status_code=303)


@router.post("/youtube/{link_id}/delete")
async def youtube_delete(
    link_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    L = session.get(YoutubeChannelLink, link_id)
    if not L:
        raise HTTPException(404)
    rv = role_val(user.role)
    if rv != "general_admin" and L.church_id != user.church_id:
        raise HTTPException(403)
    session.delete(L)
    session.commit()
    return RedirectResponse("/youtube", status_code=303)


# ---------- Data export / import (Global tree) ----------
@router.get("/data-tools", response_class=HTMLResponse)
async def data_tools_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not _is_global_church_admin(user, session) and role_val(user.role) != "general_admin":
        raise HTTPException(403, "Global church admin only")
    return templates.TemplateResponse("data/tools.html", {"request": request, "user": user})


@router.get("/data-tools/export/members.csv")
async def export_members_csv(
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not _is_global_church_admin(user, session) and role_val(user.role) != "general_admin":
        raise HTTPException(403)
    root_id = user.church_id
    if role_val(user.role) == "general_admin":
        # all members
        members = list(session.exec(select(ChurchMember).order_by(ChurchMember.id)).all())
    else:
        ids = _descendant_ids(session, root_id)
        members = list(session.exec(
            select(ChurchMember).where(ChurchMember.church_id.in_(ids))
        ).all())
    # map churches
    churches = {c.id: c for c in session.exec(select(ChurchUnit)).all()}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "full_name", "email", "phone", "whatsapp", "sex", "age_category", "confession",
        "status", "worker_type", "leader_type", "member_since", "address",
        "district", "group", "state", "country", "global", "approval_status", "is_active",
    ])
    for m in members:
        dist = churches.get(m.church_id)
        # walk parents for labels
        labels = {"district": "", "group": "", "state": "", "country": "", "global": ""}
        cur = dist
        while cur:
            lv = str(getattr(cur.level, "value", cur.level)).lower()
            if "district" in lv:
                labels["district"] = cur.name
            elif "group" in lv:
                labels["group"] = cur.name
            elif "state" in lv:
                labels["state"] = cur.name
            elif "country" in lv:
                labels["country"] = cur.name
            elif "global" in lv:
                labels["global"] = cur.name
            cur = churches.get(cur.parent_id) if cur.parent_id else None
        w.writerow([
            m.full_name, m.email or "", m.phone or "", m.whatsapp or "",
            m.sex or "", m.age_category or "", m.confession or "",
            m.status or "", m.worker_type or "", m.leader_type or "",
            str(m.member_since or ""), m.address or "",
            labels["district"], labels["group"], labels["state"], labels["country"], labels["global"],
            m.approval_status or "", "yes" if m.is_active else "no",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=churchgate_members.csv"},
    )


@router.get("/data-tools/export/stats.csv")
async def export_stats_csv(
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Weekly stats including tithe & offerings for the global tree."""
    if not _is_global_church_admin(user, session) and role_val(user.role) != "general_admin":
        raise HTTPException(403)
    if role_val(user.role) == "general_admin":
        stats = list(session.exec(select(WeeklyStat).order_by(WeeklyStat.week_start.desc())).all())
    else:
        ids = _descendant_ids(session, user.church_id)
        stats = list(session.exec(
            select(WeeklyStat).where(WeeklyStat.church_id.in_(ids))
            .order_by(WeeklyStat.week_start.desc())
        ).all())
    churches = {c.id: c for c in session.exec(select(ChurchUnit)).all()}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "church_name", "week_start", "adult_male", "adult_female", "children_boys", "children_girls",
        "youth_male", "youth_female", "offering", "tithe", "donation",
        "newcomers", "converts", "counseling", "members_in_need", "notes",
    ])
    for s in stats:
        ch = churches.get(s.church_id)
        w.writerow([
            ch.name if ch else "", str(s.week_start or ""),
            s.adult_male or 0, s.adult_female or 0, s.children_boys or 0, s.children_girls or 0,
            getattr(s, "youth_male", 0) or 0, getattr(s, "youth_female", 0) or 0,
            getattr(s, "offering", 0) or 0, getattr(s, "tithe", 0) or 0, getattr(s, "donation", 0) or 0,
            getattr(s, "newcomers", 0) or 0, getattr(s, "converts", 0) or 0,
            getattr(s, "counseling", 0) or 0, getattr(s, "members_in_need", 0) or 0,
            getattr(s, "notes", "") or "",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=churchgate_tithe_offerings_stats.csv"},
    )


@router.post("/data-tools/import/members")
async def import_members_csv(
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    if not _is_global_church_admin(user, session):
        raise HTTPException(403, "Global church admin only")
    if not user.church_id:
        raise HTTPException(400)
    # Import into the admin's primary district if possible, else first district under tree
    ids = _descendant_ids(session, user.church_id)
    districts = [
        c for c in session.exec(select(ChurchUnit).where(ChurchUnit.id.in_(ids))).all()
        if "district" in str(getattr(c.level, "value", c.level)).lower()
    ]
    target = districts[0] if districts else session.get(ChurchUnit, user.church_id)
    raw = (await file.read()).decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(raw))
    added = 0
    for row in reader:
        name = (row.get("full_name") or row.get("name") or "").strip()
        if not name:
            continue
        email = (row.get("email") or "").strip() or None
        session.add(ChurchMember(
            church_id=target.id,
            global_church_id=user.church_id,
            full_name=name,
            email=email,
            phone=(row.get("phone") or "").strip() or None,
            whatsapp=(row.get("whatsapp") or "").strip() or None,
            sex=(row.get("sex") or "brother").strip(),
            age_category=(row.get("age_category") or "adult").strip(),
            confession=(row.get("confession") or "saved").strip(),
            status=(row.get("status") or "member").strip(),
            worker_type=(row.get("worker_type") or "").strip() or None,
            leader_type=(row.get("leader_type") or "").strip() or None,
            address=(row.get("address") or "").strip() or None,
            approval_status=(row.get("approval_status") or "approved").strip(),
            is_active=True,
        ))
        added += 1
    session.commit()
    return RedirectResponse(f"/data-tools?imported={added}", status_code=303)
