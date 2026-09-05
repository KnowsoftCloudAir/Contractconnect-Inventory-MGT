from pathlib import Path
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import User, UserRole, ChurchUnit, ChurchMember, WeeklyStat, SpecialProgram, ProgramPhoto, ChurchLevel
from app.auth import require_roles, get_password_hash, role_val

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

@router.get("/", response_class=HTMLResponse)
async def admin_home(
    request: Request,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    churches = session.exec(select(ChurchUnit).order_by(ChurchUnit.created_at.desc())).all()
    users = session.exec(select(User).order_by(User.created_at.desc()).limit(100)).all()
    pending = [c for c in churches if c.approval_status == "pending"]
    subadmins = [u for u in users if role_val(u.role) == "church_admin"]
    return templates.TemplateResponse("admin/dashboard.html", {
        "request": request, "user": user,
        "churches": churches, "pending": pending, "users": users, "subadmins": subadmins
    })

@router.get("/churches/{church_id}", response_class=HTMLResponse)
async def view_church(
    church_id: int,
    request: Request,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = session.get(ChurchUnit, church_id)
    if not church:
        raise HTTPException(404, "Church not found")
    members = session.exec(select(ChurchMember).where(ChurchMember.church_id == church_id).limit(100)).all()
    stats = session.exec(select(WeeklyStat).where(WeeklyStat.church_id == church_id).order_by(WeeklyStat.week_start.desc()).limit(12)).all()
    children = session.exec(select(ChurchUnit).where(ChurchUnit.parent_id == church_id)).all()
    admins = session.exec(select(User).where(User.church_id == church_id)).all()
    return templates.TemplateResponse("admin/church_edit.html", {
        "request": request, "user": user, "church": church,
        "members": members, "stats": stats, "children": children, "admins": admins
    })

@router.post("/churches/{church_id}/edit")
async def edit_church(
    church_id: int,
    name: str = Form(...),
    resident_pastor: str = Form(""),
    address: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    doctrine: str = Form(""),
    activity_days: str = Form(""),
    approval_status: str = Form("approved"),
    is_active: str = Form("yes"),
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = session.get(ChurchUnit, church_id)
    if not church:
        raise HTTPException(404, "Church not found")
    church.name = name.strip()
    church.resident_pastor = resident_pastor.strip() or None
    church.address = address.strip() or None
    church.phone = phone.strip() or None
    church.email = email.strip() or None
    church.doctrine = doctrine.strip() or None
    church.activity_days = activity_days.strip() or None
    church.approval_status = approval_status
    church.is_active = is_active == "yes"
    session.add(church)
    session.commit()
    return RedirectResponse(f"/admin/churches/{church_id}", status_code=303)

@router.post("/churches/{church_id}/approve")
async def approve_church(
    church_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = session.get(ChurchUnit, church_id)
    if not church:
        raise HTTPException(404, "Church not found")
    church.approval_status = "approved"
    try:
        if str(getattr(church.level, "value", church.level)) == "global" and not church.global_code:
            church.global_code = church.code
    except Exception:
        pass
    session.add(church)
    admin = session.exec(select(User).where(User.church_id == church_id, User.role == UserRole.church_admin)).first()
    if admin:
        admin.is_active = True
        # Default permissions when GA approves church — GA can tighten later
        admin.can_approve_members = True
        admin.can_create_churches = True
        session.add(admin)
    session.commit()
    return RedirectResponse("/admin/", status_code=303)

@router.post("/churches/{church_id}/reject")
async def reject_church(
    church_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = session.get(ChurchUnit, church_id)
    if not church:
        raise HTTPException(404, "Church not found")
    church.approval_status = "rejected"
    session.add(church)
    session.commit()
    return RedirectResponse("/admin/", status_code=303)

@router.post("/users/{user_id}/permissions")
async def set_subadmin_permissions(
    user_id: int,
    can_create_churches: str = Form(""),
    can_approve_members: str = Form(""),
    can_enter_stats: str = Form(""),
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    """General Admin grants/revokes sub-admin powers."""
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(404, "User not found")
    if target.role == UserRole.general_admin:
        raise HTTPException(400, "Cannot change general admin")
    target.can_create_churches = can_create_churches == "yes"
    target.can_approve_members = can_approve_members == "yes"
    target.can_enter_stats = can_enter_stats == "yes"
    session.add(target)
    session.commit()
    return RedirectResponse("/admin/", status_code=303)

@router.post("/users/{user_id}/deactivate")
async def deactivate(
    user_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    target = session.get(User, user_id)
    if not target or target.role == UserRole.general_admin:
        raise HTTPException(400, "Cannot deactivate")
    target.is_active = False
    session.add(target)
    session.commit()
    return RedirectResponse("/admin/", status_code=303)

@router.post("/users/{user_id}/activate")
async def activate(
    user_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(404)
    target.is_active = True
    session.add(target)
    session.commit()
    return RedirectResponse("/admin/", status_code=303)


@router.post("/churches/{church_id}/disapprove")
async def disapprove_church(
    church_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    """Deactivate a church (esp. Global) and all branches + logins."""
    church = session.get(ChurchUnit, church_id)
    if not church:
        raise HTTPException(404, "Church not found")
    ids = [church.id]
    queue = [church.id]
    while queue:
        pid = queue.pop(0)
        for k in session.exec(select(ChurchUnit).where(ChurchUnit.parent_id == pid)).all():
            ids.append(k.id)
            queue.append(k.id)
    for cid in ids:
        unit = session.get(ChurchUnit, cid)
        if unit:
            unit.approval_status = "rejected"
            unit.is_active = False
            session.add(unit)
        for u in session.exec(select(User).where(User.church_id == cid)).all():
            if u.role != UserRole.general_admin:
                u.is_active = False
                session.add(u)
    session.commit()
    return RedirectResponse("/admin/globals", status_code=303)


@router.get("/churches/{church_id}/dashboard", response_class=HTMLResponse)
async def admin_church_dashboard(
    church_id: int,
    request: Request,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    from app.routers.church import collect_descendant_ids
    church = session.get(ChurchUnit, church_id)
    if not church:
        raise HTTPException(404, "Not found")
    # Reuse church dashboard by temporarily setting context — call same template builder
    from app.models import ChurchMember, WeeklyStat
    ids = collect_descendant_ids(session, church.id)
    children = list(session.exec(select(ChurchUnit).where(ChurchUnit.parent_id == church.id)).all())
    members_list = list(session.exec(select(ChurchMember).where(ChurchMember.church_id.in_(ids), ChurchMember.approval_status=="approved")).all())
    members_count = len(members_list)
    demo = {"men":0,"women":0,"youth_boys":0,"youth_girls":0,"ya_boys":0,"ya_girls":0,"children_boys":0,"children_girls":0,
            "newcomers_men":0,"newcomers_women":0,"newcomers_children":0,"converts_men":0,"converts_women":0,"converts_children":0}
    map_markers = []
    # Country/state aggregation for map
    state_counts = {}
    for uid in ids:
        u = session.get(ChurchUnit, uid)
        if not u:
            continue
        if u.latitude is not None and u.longitude is not None:
            map_markers.append({"name": u.name, "code": u.code, "level": str(getattr(u.level,"value",u.level)),
                "lat": float(u.latitude), "lng": float(u.longitude), "address": u.address or "",
                "country": u.country_name or "", "state": u.state_name or ""})
        key = (u.country_name or "Unknown", u.state_name or "Unknown")
        state_counts[key] = state_counts.get(key, 0) + 1
    state_summary = [{"country": k[0], "state": k[1], "count": v} for k, v in state_counts.items()]
    lv = str(getattr(church.level, "value", church.level)).lower()
    is_global_view = lv in ("global", "global_church")
    return templates.TemplateResponse("church/dashboard.html", {
        "request": request, "user": user, "church": church, "children": children,
        "stats": [], "members_count": members_count,
        "chart_labels": [], "chart_attendance": [], "chart_offering": [], "chart_tithe": [], "chart_donation": [],
        "total_offering": 0, "total_tithe": 0, "latest_attendance": 0,
        "is_admin_overview": False, "demo": demo, "admin_viewing": True,
        "map_markers": map_markers, "is_global_view": is_global_view, "state_summary": state_summary,
    })


@router.get("/globals", response_class=HTMLResponse)
async def list_global_churches(
    request: Request,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    from app.models import ChurchLevel
    try:
        globals_ = list(session.exec(
            select(ChurchUnit).where(ChurchUnit.level == ChurchLevel.global_church)
        ).all())
    except Exception:
        globals_ = [
            c for c in session.exec(select(ChurchUnit)).all()
            if str(getattr(c.level, "value", c.level)).lower() in ("global", "global_church")
        ]
    return templates.TemplateResponse("admin/globals.html", {
        "request": request, "user": user, "globals": globals_,
    })




@router.get("/featured-programs", response_class=HTMLResponse)
async def featured_programs_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    """Pending home-page requests + currently live showcase programs."""
    from datetime import datetime
    all_progs = list(session.exec(
        select(SpecialProgram).where(SpecialProgram.is_active == True)
        .order_by(SpecialProgram.created_at.desc())
    ).all())
    pending, live, other = [], [], []
    now = datetime.utcnow()
    for p in all_progs:
        church = session.get(ChurchUnit, p.church_id)
        if not church:
            continue
        lv = str(getattr(church.level, "value", church.level)).lower()
        if lv not in ("global", "global_church"):
            continue
        photo = session.exec(
            select(ProgramPhoto).where(ProgramPhoto.program_id == p.id)
            .order_by(ProgramPhoto.created_at.desc())
        ).first()
        row = {"p": p, "church": church, "photo": photo}
        ends = getattr(p, "home_display_ends_at", None)
        if p.featured_on_home and ends and ends > now:
            live.append(row)
        elif p.featured_on_home and ends and ends <= now:
            # expired — auto clear
            p.featured_on_home = False
            session.add(p)
            other.append(row)
        elif getattr(p, "request_home_display", False) and not p.featured_on_home:
            pending.append(row)
        else:
            other.append(row)
    session.commit()
    return templates.TemplateResponse("admin/featured_programs.html", {
        "request": request, "user": user,
        "pending": pending, "live": live, "other": other,
    })


@router.post("/programs/{program_id}/feature")
async def feature_program(
    program_id: int,
    display_hours: float = Form(12),
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    """Approve home display for up to 24 hours."""
    from datetime import datetime, timedelta
    p = session.get(SpecialProgram, program_id)
    if not p:
        raise HTTPException(404, "Program not found")
    church = session.get(ChurchUnit, p.church_id)
    lv = str(getattr(church.level, "value", church.level)).lower() if church else ""
    if lv not in ("global", "global_church"):
        raise HTTPException(400, "Only Global church programs can appear on the home page")
    try:
        hours = float(display_hours)
    except Exception:
        hours = 12.0
    hours = max(0.25, min(24.0, hours))  # 15 min to 24 hours
    now = datetime.utcnow()
    p.request_home_display = True
    p.featured_on_home = True
    p.home_display_hours = hours
    p.home_display_starts_at = now
    p.home_display_ends_at = now + timedelta(hours=hours)
    session.add(p)
    session.commit()
    return RedirectResponse("/admin/featured-programs", status_code=303)


@router.post("/programs/{program_id}/unfeature")
async def unfeature_program(
    program_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    p = session.get(SpecialProgram, program_id)
    if not p:
        raise HTTPException(404, "Program not found")
    p.featured_on_home = False
    p.home_display_ends_at = None
    session.add(p)
    session.commit()
    return RedirectResponse("/admin/featured-programs", status_code=303)


# --- Music links (General Admin) ---
from app.models import MusicLink

@router.get("/music", response_class=HTMLResponse)
async def admin_music_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    links = list(session.exec(select(MusicLink).order_by(MusicLink.sort_order, MusicLink.id)).all())
    return templates.TemplateResponse("admin/music.html", {"request": request, "user": user, "links": links})


@router.post("/music/add")
async def admin_music_add(
    title: str = Form(...),
    youtube_id: str = Form(...),
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    yid = youtube_id.strip()
    # Accept full URL or raw id
    if "youtu.be/" in yid:
        yid = yid.split("youtu.be/")[-1].split("?")[0]
    elif "v=" in yid:
        yid = yid.split("v=")[-1].split("&")[0]
    yid = yid.strip()[:20]
    max_order = 0
    for L in session.exec(select(MusicLink)).all():
        max_order = max(max_order, L.sort_order or 0)
    session.add(MusicLink(title=title.strip(), youtube_id=yid, is_active=True, sort_order=max_order + 1))
    session.commit()
    return RedirectResponse("/admin/music", status_code=303)


@router.post("/music/{link_id}/edit")
async def admin_music_edit(
    link_id: int,
    title: str = Form(...),
    youtube_id: str = Form(...),
    is_active: str = Form("yes"),
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    link = session.get(MusicLink, link_id)
    if not link:
        raise HTTPException(404)
    yid = youtube_id.strip()
    if "youtu.be/" in yid:
        yid = yid.split("youtu.be/")[-1].split("?")[0]
    elif "v=" in yid:
        yid = yid.split("v=")[-1].split("&")[0]
    link.title = title.strip()
    link.youtube_id = yid.strip()[:20]
    link.is_active = is_active == "yes"
    session.add(link)
    session.commit()
    return RedirectResponse("/admin/music", status_code=303)


@router.post("/music/{link_id}/delete")
async def admin_music_delete(
    link_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    link = session.get(MusicLink, link_id)
    if link:
        session.delete(link)
        session.commit()
    return RedirectResponse("/admin/music", status_code=303)
