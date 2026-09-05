from pathlib import Path
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from datetime import datetime, date, timedelta
from io import BytesIO

from app.database import get_session
from app.models import User, UserRole, ChurchUnit, ChurchLevel, ChurchMember, WeeklyStat, MemberStatus
from app.auth import require_user, require_roles

router = APIRouter(prefix="/district", tags=["district"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

def get_user_church(user: User, session: Session) -> ChurchUnit:
    if not user.church_id:
        raise HTTPException(400, "No church linked")
    church = session.get(ChurchUnit, user.church_id)
    if not church:
        raise HTTPException(404, "Church not found")
    return church

@router.get("/members", response_class=HTMLResponse)
async def list_members(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    church = get_user_church(user, session)
    # Include members under this unit and descendants (so Group/State see district members)
    ids = [church.id]
    queue = [church.id]
    while queue:
        pid = queue.pop(0)
        for k in session.exec(select(ChurchUnit).where(ChurchUnit.parent_id == pid)).all():
            ids.append(k.id)
            queue.append(k.id)
    members = session.exec(
        select(ChurchMember).where(ChurchMember.church_id.in_(ids)).order_by(ChurchMember.full_name)
    ).all()
    return templates.TemplateResponse("district/members.html", {
        "request": request, "user": user, "church": church, "members": members
    })

@router.get("/members/add", response_class=HTMLResponse)
async def add_member_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.data_officer, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = get_user_church(user, session)
    return templates.TemplateResponse("district/member_form.html", {
        "request": request, "user": user, "church": church
    })

@router.post("/members/add")
async def add_member(
    request: Request,
    full_name: str = Form(...),
    gender: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    status: str = Form("member"),
    worker_type: str = Form(""),
    leader_type: str = Form(""),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.data_officer, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = get_user_church(user, session)
    try:
        st = MemberStatus(status)
    except ValueError:
        st = MemberStatus.member
    m = ChurchMember(
        church_id=church.id,
        full_name=full_name.strip(),
        gender=gender or None,
        phone=phone or None,
        email=email or None,
        address=address or None,
        status=st,
        worker_type=worker_type or None if st == MemberStatus.worker else None,
        leader_type=leader_type or None if st == MemberStatus.leader else None,
        joined_date=date.today()
    )
    session.add(m)
    session.commit()
    return RedirectResponse("/district/members", status_code=303)

@router.get("/stats/enter", response_class=HTMLResponse)
async def enter_stats_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.data_officer, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = get_user_church(user, session)
    # Default to current week's Monday
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    return templates.TemplateResponse("district/stats_form.html", {
        "request": request, "user": user, "church": church, "week_start": week_start.isoformat()
    })

@router.post("/stats/enter")
async def enter_stats(
    request: Request,
    week_start: str = Form(...),
    adult_male: int = Form(0),
    adult_female: int = Form(0),
    children_boys: int = Form(0),
    children_girls: int = Form(0),
    youth_male: int = Form(0),
    youth_female: int = Form(0),
    offering: float = Form(0),
    tithe: float = Form(0),
    donation: float = Form(0),
    special_program_attendance: int = Form(0),
    newcomers: int = Form(0),
    converts: int = Form(0),
    counseling: int = Form(0),
    members_in_need: int = Form(0),
    notes: str = Form(""),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.data_officer, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = get_user_church(user, session)
    ws = date.fromisoformat(week_start)
    # Upsert for the week
    existing = session.exec(
        select(WeeklyStat).where(WeeklyStat.church_id == church.id, WeeklyStat.week_start == ws)
    ).first()
    if existing:
        existing.adult_male = adult_male
        existing.adult_female = adult_female
        existing.children_boys = children_boys
        existing.children_girls = children_girls
        existing.youth_male = youth_male
        existing.youth_female = youth_female
        existing.offering = offering
        existing.tithe = tithe
        existing.donation = donation
        existing.special_program_attendance = special_program_attendance
        existing.newcomers = newcomers
        existing.converts = converts
        existing.counseling = counseling
        existing.members_in_need = members_in_need
        existing.notes = notes or None
        existing.entered_by = user.id
        session.add(existing)
    else:
        stat = WeeklyStat(
            church_id=church.id,
            week_start=ws,
            adult_male=adult_male, adult_female=adult_female,
            children_boys=children_boys, children_girls=children_girls,
            youth_male=youth_male, youth_female=youth_female,
            offering=offering, tithe=tithe, donation=donation,
            special_program_attendance=special_program_attendance,
            newcomers=newcomers, converts=converts,
            counseling=counseling, members_in_need=members_in_need,
            notes=notes or None, entered_by=user.id
        )
        session.add(stat)
    session.commit()
    return RedirectResponse("/dashboard", status_code=303)

@router.get("/reports/pdf")
async def generate_pdf_report(
    request: Request,
    period: str = "monthly",
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    """Downloadable PDF – safe for string/enum fields and rolled-up members."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm
    except ImportError:
        raise HTTPException(500, "PDF library not available. Install reportlab.")

    church = get_user_church(user, session)
    # Scope: this unit + descendants
    ids = [church.id]
    queue = [church.id]
    while queue:
        pid = queue.pop(0)
        for k in session.exec(select(ChurchUnit).where(ChurchUnit.parent_id == pid)).all():
            ids.append(k.id)
            queue.append(k.id)

    members = session.exec(
        select(ChurchMember).where(ChurchMember.church_id.in_(ids)).order_by(ChurchMember.full_name)
    ).all()
    stats = session.exec(
        select(WeeklyStat).where(WeeklyStat.church_id.in_(ids)).order_by(WeeklyStat.week_start.desc()).limit(24)
    ).all()

    def s(val):
        if val is None:
            return ""
        return str(getattr(val, "value", val))

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 25 * mm

    def newline(h=6):
        nonlocal y
        y -= h * mm
        if y < 20 * mm:
            c.showPage()
            y = height - 20 * mm

    c.setFont("Helvetica-Bold", 16)
    c.drawString(20 * mm, y, "Knowsoft Churchgate Report")
    newline(8)
    c.setFont("Helvetica", 10)
    c.drawString(20 * mm, y, f"Church: {church.name} ({church.code})")
    newline(5)
    c.drawString(20 * mm, y, f"Level: {s(church.level)}  |  Period: {period}  |  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    newline(8)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, y, f"Members ({len(members)})")
    newline(6)
    c.setFont("Helvetica", 9)
    for m in members[:80]:
        line = f"{m.full_name} | {s(m.status)}"
        if m.worker_type:
            line += f" ({m.worker_type})"
        if m.leader_type:
            line += f" [{m.leader_type}]"
        if getattr(m, "custom_title", None):
            line += f" – {m.custom_title}"
        line += f" | {s(m.approval_status)}"
        c.drawString(22 * mm, y, line[:95])
        newline(5)

    newline(4)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(20 * mm, y, "Weekly statistics")
    newline(6)
    c.setFont("Helvetica", 9)
    for st in stats:
        total_att = (st.adult_male + st.adult_female + st.children_boys +
                     st.children_girls + st.youth_male + st.youth_female)
        line = (f"Week {st.week_start}: Att {total_att} | Off {st.offering:.0f} | "
                f"Tithe {st.tithe:.0f} | New {st.newcomers} | Conv {st.converts}")
        c.drawString(22 * mm, y, line[:100])
        newline(5)

    c.save()
    buffer.seek(0)
    filename = f"churchgate_{church.code}_{period}.pdf".replace(" ", "_")
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/approvals", response_class=HTMLResponse)
async def pending_members(
    request: Request,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    if user.role != UserRole.general_admin and not getattr(user, "can_approve_members", False):
        raise HTTPException(403, "General Admin has not granted permission to approve members")
    church = get_user_church(user, session)
    pending = session.exec(
        select(ChurchMember).where(
            ChurchMember.church_id == church.id,
            ChurchMember.approval_status == "pending"
        ).order_by(ChurchMember.created_at.desc())
    ).all()
    approved = session.exec(
        select(ChurchMember).where(
            ChurchMember.church_id == church.id,
            ChurchMember.approval_status == "approved"
        ).order_by(ChurchMember.full_name)
    ).all()
    discontinue = session.exec(
        select(ChurchMember).where(
            ChurchMember.church_id == church.id,
            ChurchMember.discontinue_requested == True
        )
    ).all()
    return templates.TemplateResponse("district/approvals.html", {
        "request": request, "user": user, "church": church,
        "pending": pending, "approved": approved, "discontinue": discontinue
    })

@router.post("/approvals/{member_id}/approve")
async def approve_member(
    member_id: int,
    status: str = Form("member"),
    worker_type: str = Form(""),
    leader_type: str = Form(""),
    can_enter_stats: str = Form(""),
    can_view_church_dashboard: str = Form(""),
    custom_title: str = Form(""),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    if user.role != UserRole.general_admin and not getattr(user, "can_approve_members", False):
        raise HTTPException(403, "Not permitted to approve members")
    member = session.get(ChurchMember, member_id)
    if not member:
        raise HTTPException(404, "Not found")
    church = get_user_church(user, session)
    if user.role != UserRole.general_admin and member.church_id != church.id:
        raise HTTPException(403, "Not your member")
    member.approval_status = "approved"
    member.status = status
    member.worker_type = worker_type or None
    member.leader_type = leader_type or None
    member.custom_title = custom_title.strip() or None
    session.add(member)
    # link user
    u = session.exec(select(User).where(User.email == member.email)).first()
    if u:
        u.member_id = member.id
        u.church_id = member.church_id
        if can_enter_stats == "yes":
            u.can_enter_stats = True
        if can_view_church_dashboard == "yes":
            u.can_view_church_dashboard = True
        elif can_view_church_dashboard == "no":
            u.can_view_church_dashboard = False
        session.add(u)
    session.commit()
    return RedirectResponse("/district/approvals", status_code=303)

@router.post("/approvals/{member_id}/reject")
async def reject_member(
    member_id: int,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    member = session.get(ChurchMember, member_id)
    if not member:
        raise HTTPException(404, "Not found")
    member.approval_status = "rejected"
    session.add(member)
    session.commit()
    return RedirectResponse("/district/approvals", status_code=303)

@router.post("/approvals/{member_id}/discontinue")
async def confirm_discontinue(
    member_id: int,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    member = session.get(ChurchMember, member_id)
    if not member:
        raise HTTPException(404, "Not found")
    member.approval_status = "discontinued"
    member.is_active = False
    member.discontinue_requested = False
    session.add(member)
    u = session.exec(select(User).where(User.email == member.email)).first()
    if u:
        u.is_active = False
        session.add(u)
    session.commit()
    return RedirectResponse("/district/approvals", status_code=303)


@router.post("/members/{member_id}/status")
async def edit_member_status(
    member_id: int,
    status: str = Form("member"),
    worker_type: str = Form(""),
    leader_type: str = Form(""),
    custom_title: str = Form(""),
    can_enter_stats: str = Form(""),
    can_view_church_dashboard: str = Form(""),
    can_see_member_count: str = Form(""),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    """Church admin edits status of an already-approved member."""
    if user.role != UserRole.general_admin and not getattr(user, "can_approve_members", False):
        raise HTTPException(403, "Not permitted to edit member status")
    member = session.get(ChurchMember, member_id)
    if not member:
        raise HTTPException(404, "Member not found")
    church = get_user_church(user, session)
    # allow if member in this unit or descendant
    ids = [church.id]
    queue = [church.id]
    while queue:
        pid = queue.pop(0)
        for k in session.exec(select(ChurchUnit).where(ChurchUnit.parent_id == pid)).all():
            ids.append(k.id)
            queue.append(k.id)
    if user.role != UserRole.general_admin and member.church_id not in ids:
        raise HTTPException(403, "Member not in your church tree")
    member.status = status
    member.worker_type = worker_type or None
    member.leader_type = leader_type or None
    member.custom_title = custom_title.strip() or None
    session.add(member)
    u = session.exec(select(User).where(User.email == member.email)).first()
    if u:
        if can_enter_stats == "yes":
            u.can_enter_stats = True
        if can_view_church_dashboard == "yes":
            u.can_view_church_dashboard = True
        elif can_view_church_dashboard == "no":
            u.can_view_church_dashboard = False
        if can_see_member_count == "yes":
            u.can_see_member_count = True
        session.add(u)
    session.commit()
    return RedirectResponse("/district/members", status_code=303)
