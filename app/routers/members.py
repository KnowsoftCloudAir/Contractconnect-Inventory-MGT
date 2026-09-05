"""Public member registration + member portal after approval."""
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from datetime import datetime, date
import uuid, shutil

from app.database import get_session
from app.models import (
    User, UserRole, ChurchUnit, ChurchLevel, ChurchMember, ChurchLevel as CL
)
from app.auth import (
    get_current_user, require_user, get_password_hash, create_access_token,
    ACCESS_TOKEN_EXPIRE_MINUTES, verify_password
)

router = APIRouter(tags=["members"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
UPLOAD = Path("app/static/uploads")
UPLOAD.mkdir(parents=True, exist_ok=True)

@router.get("/join", response_class=HTMLResponse)
async def join_page(request: Request, session: Session = Depends(get_session)):
    globals_ = session.exec(
        select(ChurchUnit).where(
            ChurchUnit.level == ChurchLevel.global_church,
            ChurchUnit.approval_status == "approved"
        ).order_by(ChurchUnit.name)
    ).all()
    return templates.TemplateResponse("members/join.html", {
        "request": request, "globals": globals_
    })

@router.get("/api/churches")
async def list_child_churches(
    parent_id: int,
    session: Session = Depends(get_session)
):
    children = session.exec(
        select(ChurchUnit).where(
            ChurchUnit.parent_id == parent_id,
            ChurchUnit.approval_status == "approved"
        ).order_by(ChurchUnit.name)
    ).all()
    return [{"id": c.id, "name": c.name, "code": c.code, "level": c.level.value} for c in children]

@router.post("/join")
async def join_submit(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    sex: str = Form(...),
    age_category: str = Form(...),
    confession: str = Form(...),
    member_since: str = Form(""),
    prayer_request: str = Form(""),
    address: str = Form(""),
    whatsapp: str = Form(""),
    phone: str = Form(""),
    global_id: int = Form(...),
    country_id: int = Form(...),
    state_id: int = Form(...),
    group_id: int = Form(...),
    district_id: int = Form(...),
    profile_pic: UploadFile = File(None),
    session: Session = Depends(get_session)
):
    if session.exec(select(User).where(User.email == email)).first():
        globals_ = session.exec(select(ChurchUnit).where(
            ChurchUnit.level == ChurchLevel.global_church, ChurchUnit.approval_status == "approved"
        )).all()
        return templates.TemplateResponse("members/join.html", {
            "request": request, "globals": globals_,
            "error": "Email already registered. Please sign in."
        }, status_code=400)

    district = session.get(ChurchUnit, district_id)
    if not district or district.level != ChurchLevel.district:
        raise HTTPException(400, "Please select a valid District church")

    pic_path = None
    if profile_pic and profile_pic.filename:
        ext = profile_pic.filename.rsplit(".", 1)[-1].lower()
        if ext in ("jpg", "jpeg", "png", "webp", "gif"):
            fname = f"member_{uuid.uuid4().hex[:10]}.{ext}"
            dest = UPLOAD / fname
            with open(dest, "wb") as f:
                shutil.copyfileobj(profile_pic.file, f)
            pic_path = f"/static/uploads/{fname}"

    ms = None
    if member_since:
        try:
            ms = date.fromisoformat(member_since)
        except ValueError:
            ms = None

    member = ChurchMember(
        church_id=district_id,
        global_church_id=global_id,
        country_church_id=country_id,
        state_church_id=state_id,
        group_church_id=group_id,
        full_name=full_name.strip(),
        sex=sex,
        age_category=age_category,
        confession=confession,
        member_since=ms or date.today(),
        prayer_request=prayer_request.strip() or None,
        address=address.strip() or None,
        whatsapp=whatsapp.strip() or None,
        phone=phone.strip() or None,
        email=email.strip(),
        profile_pic=pic_path,
        status="member",
        approval_status="pending",
        is_active=True,
    )
    session.add(member)
    session.commit()
    session.refresh(member)

    user = User(
        email=email.strip(),
        hashed_password=get_password_hash(password),
        full_name=full_name.strip(),
        role=UserRole.member,
        church_id=district_id,
        member_id=member.id,
        is_active=True,  # can login but limited until approved
    )
    session.add(user)
    session.commit()

    return templates.TemplateResponse("members/pending.html", {
        "request": request, "full_name": full_name, "email": email
    })

@router.get("/member/portal", response_class=HTMLResponse)
async def member_portal(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    from app.models import SpecialProgram, ProgramPhoto, WeeklyStat, PhotoLike, PhotoComment
    member = session.get(ChurchMember, user.member_id) if user.member_id else None
    if not member:
        member = session.exec(select(ChurchMember).where(ChurchMember.email == user.email)).first()
    church = session.get(ChurchUnit, user.church_id) if user.church_id else None
    if not church and member:
        church = session.get(ChurchUnit, member.church_id)

    programs = []
    photos = []
    if church:
        # Programs for this district + parents (group/state broadcasts)
        scope = {church.id}
        ch = church
        while ch and ch.parent_id:
            scope.add(ch.parent_id)
            ch = session.get(ChurchUnit, ch.parent_id)
        programs = session.exec(
            select(SpecialProgram).where(
                SpecialProgram.church_id.in_(list(scope)),
                SpecialProgram.is_active == True
            ).order_by(SpecialProgram.created_at.desc()).limit(10)
        ).all()
        prog_ids = [p.id for p in programs]
        if prog_ids:
            raw_photos = session.exec(
                select(ProgramPhoto).where(ProgramPhoto.program_id.in_(prog_ids)).order_by(ProgramPhoto.created_at.desc()).limit(12)
            ).all()
            for ph in raw_photos:
                likes = session.exec(select(PhotoLike).where(PhotoLike.photo_id == ph.id)).all()
                comments = session.exec(select(PhotoComment).where(PhotoComment.photo_id == ph.id)).all()
                photos.append({
                    "id": ph.id, "path": ph.file_path, "caption": ph.caption,
                    "program_id": ph.program_id,
                    "likes": len(likes),
                    "liked": any(l.user_id == user.id for l in likes),
                    "comment_count": len(comments),
                })

    district_member_count = None
    if church and getattr(user, "can_see_member_count", False):
        district_member_count = len(session.exec(
            select(ChurchMember).where(
                ChurchMember.church_id == church.id,
                ChurchMember.approval_status == "approved"
            )
        ).all())

    weekly_note = (church.weekly_activities_note or church.activity_days) if church else None

    sample_warning = None
    try:
        from app.routers.subscriptions import check_sample_member, expire_due_subscriptions
        expire_due_subscriptions(session)
        sample = check_sample_member(session, user)
        if sample.get("expired"):
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/auth/login?sample=expired", status_code=303)
        if sample.get("show_warning"):
            sample_warning = sample.get("message")
    except Exception as e:
        print("sample check:", e)

    return templates.TemplateResponse("members/portal.html", {
        "request": request, "user": user, "member": member, "church": church,
        "programs": programs, "photos": photos,
        "district_member_count": district_member_count,
        "weekly_note": weekly_note,
        "sample_warning": sample_warning,
    })

@router.post("/member/update-profile")
async def update_profile(
    request: Request,
    full_name: str = Form(...),
    whatsapp: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    prayer_request: str = Form(""),
    confession: str = Form(""),
    requested_status: str = Form(""),
    requested_title: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    member = session.get(ChurchMember, user.member_id) if user.member_id else None
    if not member:
        member = session.exec(select(ChurchMember).where(ChurchMember.email == user.email)).first()
    if not member:
        raise HTTPException(404, "Member profile not found")
    if member.approval_status not in ("approved", "pending"):
        raise HTTPException(403, "Account not active")
    member.full_name = full_name.strip()
    member.whatsapp = whatsapp or None
    member.phone = phone or None
    member.address = address or None
    member.prayer_request = prayer_request or None
    if confession:
        member.confession = confession
    # Member may propose status; admin confirms via Approvals / members list
    if requested_status or requested_title:
        note = f"[Status request: {requested_status or member.status}"
        if requested_title:
            note += f" / title: {requested_title}"
        note += "]"
        member.notes = ((member.notes or "") + " " + note).strip()
        # Soft-update display title only if they already have a role; full status stays until admin edits
        if requested_title:
            member.custom_title = requested_title.strip()
    user.full_name = full_name.strip()
    session.add(member)
    session.add(user)
    session.commit()
    return RedirectResponse("/member/portal", status_code=303)

@router.post("/member/request-discontinue")
async def request_discontinue(
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    member = session.get(ChurchMember, user.member_id) if user.member_id else None
    if not member:
        member = session.exec(select(ChurchMember).where(ChurchMember.email == user.email)).first()
    if member:
        member.discontinue_requested = True
        session.add(member)
        session.commit()
    return RedirectResponse("/member/portal", status_code=303)


@router.get("/api/churches-by-level")
async def churches_by_level(level: str = "global", session: Session = Depends(get_session)):
    try:
        lv = ChurchLevel(level if level != "global" else "global")
    except ValueError:
        lv = ChurchLevel.global_church
    if level == "global":
        lv = ChurchLevel.global_church
    rows = session.exec(
        select(ChurchUnit).where(
            ChurchUnit.level == lv,
            ChurchUnit.approval_status == "approved"
        ).order_by(ChurchUnit.name)
    ).all()
    return [{"id": c.id, "name": c.name, "code": c.code, "level": getattr(c.level, "value", str(c.level))} for c in rows]


@router.get("/api/geo/countries")
async def geo_countries():
    import json
    from pathlib import Path as P
    p = P(__file__).resolve().parent.parent / "data" / "countries.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


@router.get("/api/music-links")
async def api_music_links(session: Session = Depends(get_session)):
    """Active YouTube tracks for member portal (managed by General Admin)."""
    from app.models import MusicLink
    links = list(session.exec(
        select(MusicLink).where(MusicLink.is_active == True).order_by(MusicLink.sort_order, MusicLink.id)
    ).all())
    return [{"id": L.youtube_id, "title": L.title} for L in links]


@router.post("/member/{member_id}/toggle-broadcast")
async def toggle_broadcast(
    member_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """District/church admin may grant broadcast privilege to an approved member."""
    from app.auth import role_val
    if role_val(user.role) not in ("church_admin", "general_admin"):
        raise HTTPException(403, "Admin only")
    target = session.exec(select(User).where(User.member_id == member_id)).first()
    if not target:
        raise HTTPException(404, "User not found")
    target.can_broadcast = not bool(getattr(target, "can_broadcast", False))
    session.add(target)
    session.commit()
    return RedirectResponse("/district/members", status_code=303)
