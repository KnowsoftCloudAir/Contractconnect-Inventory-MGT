from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from datetime import datetime
import secrets
import string

from app.database import get_session
from app.models import User, UserRole, ChurchUnit, ChurchLevel, ApprovalStatus, ChurchMember
from app.auth import require_user, role_val, verify_password, get_password_hash, create_access_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

def generate_code(prefix: str = "CG") -> str:
    suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"{prefix}-{suffix}"

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: Optional[User] = Depends(get_current_user)):
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse("auth/login.html", {"request": request})

@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session)
):
    user = session.exec(select(User).where(User.email == email)).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse("auth/login.html", {
            "request": request, "error": "Invalid email or password"
        }, status_code=400)
    if not user.is_active:
        return templates.TemplateResponse("auth/login.html", {
            "request": request, "error": "Account deactivated. Contact Knowsoft Churchgate support."
        }, status_code=400)

    # Church admins must belong to an approved church
    if role_val(user.role) == "church_admin" and user.church_id:
        church = session.get(ChurchUnit, user.church_id)
        status = (church.approval_status or "approved") if church else "approved"
        if church and status not in ("approved",):
            return templates.TemplateResponse("auth/login.html", {
                "request": request, "error": "Your church is still pending approval by Knowsoft Admin."
            }, status_code=400)
    # Members need approved membership for full login
    if role_val(user.role) == "member":
        m = session.get(ChurchMember, user.member_id) if user.member_id else None
        if not m:
            m = session.exec(select(ChurchMember).where(ChurchMember.email == user.email)).first()
        if m and m.approval_status == "pending":
            return templates.TemplateResponse("auth/login.html", {
                "request": request, "error": "Your membership is pending district approval."
            }, status_code=400)
        if m and m.approval_status in ("rejected", "discontinued"):
            return templates.TemplateResponse("auth/login.html", {
                "request": request, "error": "Membership not active."
            }, status_code=400)

    token = create_access_token({"sub": user.email})
    user.last_login = datetime.utcnow()
    session.add(user)
    session.commit()
    # Route by role — members without dashboard grant go to portal only
    rv = role_val(user.role)
    if rv == "general_admin":
        dest = "/admin/"
    elif rv == "member":
        dest = "/member/portal"
    else:
        dest = "/dashboard"
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(
        "access_token", token,
        httponly=True,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
        path="/",
    )
    return resp

@router.get("/register-church", response_class=HTMLResponse)
async def register_church_page(request: Request):
    return templates.TemplateResponse("auth/register_church.html", {"request": request})

@router.post("/register-church")
async def register_church(
    request: Request,
    name: str = Form(...),
    level: str = Form(...),
    parent_global_id: str = Form(""),
    parent_country_id: str = Form(""),
    parent_state_id: str = Form(""),
    parent_group_id: str = Form(""),
    country_name: str = Form(""),
    state_name: str = Form(""),
    doctrine: str = Form(""),
    activity_days: str = Form(""),
    owner_name: str = Form(...),
    resident_pastor: str = Form(...),
    address: str = Form(""),
    phone: str = Form(""),
    email: str = Form(...),
    admin_full_name: str = Form(...),
    admin_password: str = Form(...),
    session: Session = Depends(get_session)
):
    level_raw = (level or "").strip().lower()
    level_map = {"global": "global", "global_church": "global", "country": "country",
                 "state": "state", "group": "group", "district": "district"}
    level_raw = level_map.get(level_raw, level_raw)
    try:
        church_level = ChurchLevel(level_raw)
    except ValueError:
        return templates.TemplateResponse("auth/register_church.html", {
            "request": request, "error": f"Invalid church level: {level}"
        }, status_code=400)

    parent_id = None
    global_code = country_code = state_code = group_code = district_code = None

    def get_unit(raw_id):
        if not raw_id or not str(raw_id).strip().isdigit():
            return None
        return session.get(ChurchUnit, int(raw_id))

    if church_level == ChurchLevel.global_church:
        parent_id = None
    elif church_level == ChurchLevel.country:
        g = get_unit(parent_global_id)
        if not g or g.approval_status != "approved":
            return templates.TemplateResponse("auth/register_church.html", {
                "request": request, "error": "Select an approved Global parent church"
            }, status_code=400)
        parent_id = g.id
        global_code = g.global_code or g.code
    elif church_level == ChurchLevel.state:
        g, c = get_unit(parent_global_id), get_unit(parent_country_id)
        if not g or not c or c.parent_id != g.id:
            return templates.TemplateResponse("auth/register_church.html", {
                "request": request, "error": "Select valid Global and Country parents"
            }, status_code=400)
        parent_id = c.id
        global_code = g.global_code or g.code
        country_code = c.country_code or c.code
    elif church_level == ChurchLevel.group:
        g, c, s = get_unit(parent_global_id), get_unit(parent_country_id), get_unit(parent_state_id)
        if not all([g, c, s]) or s.parent_id != c.id:
            return templates.TemplateResponse("auth/register_church.html", {
                "request": request, "error": "Select valid Global, Country and State parents"
            }, status_code=400)
        parent_id = s.id
        global_code = g.global_code or g.code
        country_code = c.country_code or c.code
        state_code = s.state_code or s.code
    elif church_level == ChurchLevel.district:
        g = get_unit(parent_global_id)
        c = get_unit(parent_country_id)
        s = get_unit(parent_state_id)
        gr = get_unit(parent_group_id)
        if not all([g, c, s, gr]) or gr.parent_id != s.id:
            return templates.TemplateResponse("auth/register_church.html", {
                "request": request, "error": "Select valid Global, Country, State and Group parents"
            }, status_code=400)
        parent_id = gr.id
        global_code = g.global_code or g.code
        country_code = c.country_code or c.code
        state_code = s.state_code or s.code
        group_code = gr.group_code or gr.code

    code = generate_code("CG")
    while session.exec(select(ChurchUnit).where(ChurchUnit.code == code)).first():
        code = generate_code("CG")

    if church_level == ChurchLevel.global_church:
        global_code = code
    elif church_level == ChurchLevel.country:
        country_code = code
    elif church_level == ChurchLevel.state:
        state_code = code
    elif church_level == ChurchLevel.group:
        group_code = code
    elif church_level == ChurchLevel.district:
        district_code = code

    existing_user = session.exec(select(User).where(User.email == email)).first()
    if existing_user:
        return templates.TemplateResponse("auth/register_church.html", {
            "request": request, "error": "Email already registered"
        }, status_code=400)

    church = ChurchUnit(
        code=code,
        name=name.strip(),
        level=church_level,
        parent_id=parent_id,
        global_code=global_code,
        country_code=country_code,
        state_code=state_code,
        group_code=group_code,
        district_code=district_code,
        country_name=country_name.strip() or None,
        state_name=state_name.strip() or None,
        doctrine=doctrine.strip() or None,
        activity_days=activity_days.strip() or None,
        owner_name=owner_name.strip(),
        resident_pastor=resident_pastor.strip(),
        address=address.strip() or None,
        phone=phone.strip() or None,
        email=email.strip(),
        approval_status="pending",
    )
    session.add(church)
    session.commit()
    session.refresh(church)

    admin = User(
        email=email.strip(),
        hashed_password=get_password_hash(admin_password),
        full_name=admin_full_name.strip(),
        role=UserRole.church_admin,
        church_id=church.id,
        is_active=True,
        can_create_churches=False,
        can_approve_members=False,
    )
    session.add(admin)
    session.commit()

    return templates.TemplateResponse("auth/pending.html", {
        "request": request,
        "church_name": name,
        "code": code,
        "email": email
    })

@router.get("/logout")
async def logout():
    resp = RedirectResponse("/auth/login", status_code=303)
    resp.delete_cookie("access_token")
    return resp


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request,
    user: User = Depends(require_user),
):
    return templates.TemplateResponse("auth/change_password.html", {
        "request": request, "user": user, "error": None, "success": None,
    })


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Any logged-in user including General Admin can change their password."""
    if not verify_password(current_password, user.hashed_password):
        return templates.TemplateResponse("auth/change_password.html", {
            "request": request, "user": user,
            "error": "Current password is incorrect", "success": None,
        }, status_code=400)
    if len(new_password) < 8:
        return templates.TemplateResponse("auth/change_password.html", {
            "request": request, "user": user,
            "error": "New password must be at least 8 characters", "success": None,
        }, status_code=400)
    if new_password != confirm_password:
        return templates.TemplateResponse("auth/change_password.html", {
            "request": request, "user": user,
            "error": "New passwords do not match", "success": None,
        }, status_code=400)
    db_user = session.get(User, user.id)
    db_user.hashed_password = get_password_hash(new_password)
    session.add(db_user)
    session.commit()
    return templates.TemplateResponse("auth/change_password.html", {
        "request": request, "user": user,
        "error": None, "success": "Password updated successfully.",
    })
