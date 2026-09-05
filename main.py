from fastapi import FastAPI, Request, Depends, Form
from typing import Optional
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse
from contextlib import asynccontextmanager
from sqlmodel import Session, select
from pathlib import Path

from app.database import create_db_and_tables, get_session, engine
from app.models import User, UserRole
from app.auth import get_password_hash, get_current_user, verify_password, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES
from app.routers import auth, admin, church, district, members, programs, projects, community, payments, youtube_data, messages, subscriptions

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    try:
        with Session(engine) as session:
            # Always ensure General Admin exists with known password
            admin = session.exec(select(User).where(User.email == "admin@knowsoft.com")).first()
            if not admin:
                admin = User(
                    email="admin@knowsoft.com",
                    hashed_password=get_password_hash("Admin@12345"),
                    full_name="Knowsoft General Admin",
                    role=UserRole.general_admin,
                    is_active=True
                )
                session.add(admin)
            else:
                admin.hashed_password = get_password_hash("Admin@12345")
                admin.is_active = True
                admin.role = UserRole.general_admin
                session.add(admin)
            session.commit()
            print("✅ General Admin ready: admin@knowsoft.com / Admin@12345")

            ensure_all_sample_data(session)
    except Exception as e:
        print(f"⚠️ Seed: {e}")
        import traceback
        traceback.print_exc()
    yield

app = FastAPI(
    title="Knowsoft Churchgate",
    description="Church hierarchy, membership & growth analytics platform",
    version="1.0.0",
    lifespan=lifespan
)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(church.router)
app.include_router(district.router)
app.include_router(members.router)
app.include_router(programs.router)
app.include_router(projects.router)
app.include_router(community.router)
app.include_router(payments.router)
app.include_router(messages.router)
app.include_router(subscriptions.router)
app.include_router(youtube_data.router)

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: Optional[User] = Depends(get_current_user)):
    if user:
        from app.auth import role_val
        rv = role_val(user.role)
        if rv == "general_admin":
            return RedirectResponse("/admin/", status_code=303)
        if rv == "member" and not getattr(user, "can_view_church_dashboard", False):
            return RedirectResponse("/member/portal", status_code=303)
        return RedirectResponse("/dashboard", status_code=303)
    # Public landing: General-Admin-approved Global church programs
    featured = []
    try:
        from sqlmodel import Session, select
        from app.database import engine
        from app.models import SpecialProgram, ProgramPhoto, ProgramVideo, ChurchUnit, ChurchLevel
        with Session(engine) as session:
            from datetime import datetime as _dt
            now = _dt.utcnow()
            progs = list(session.exec(
                select(SpecialProgram).where(
                    SpecialProgram.featured_on_home == True,
                    SpecialProgram.is_active == True,
                ).order_by(SpecialProgram.created_at.desc())
            ).all())
            for p in progs:
                ends = getattr(p, "home_display_ends_at", None)
                if ends and ends <= now:
                    p.featured_on_home = False
                    session.add(p)
                    continue
                if ends is None and getattr(p, "home_display_hours", None):
                    # legacy without ends_at — skip until re-approved
                    continue
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
                video = session.exec(
                    select(ProgramVideo).where(ProgramVideo.program_id == p.id)
                    .order_by(ProgramVideo.created_at.desc())
                ).first()
                try:
                    session.commit()
                except Exception:
                    pass
                logo = getattr(church, "logo_url", None) or ""
                featured.append({
                    "title": p.title,
                    "church": church.name,
                    "church_logo": logo if logo.startswith("/") else (("/" + logo) if logo else ""),
                    "date": str(p.program_date) if p.program_date else "",
                    "venue": p.location or "",
                    "photo": (
                        photo.file_path if photo and photo.file_path.startswith("/")
                        else ("/" + photo.file_path if photo and photo.file_path else "")
                    ),
                    "video": (video.file_path if video and video.file_path.startswith("/") else (("/" + video.file_path) if video and video.file_path else "")),
                    "description": (p.description or "")[:180],
                })
    except Exception as e:
        print(f"featured load: {e}")
    youtube_clips = []
    live_stats = {"churches": 0, "countries": 0, "states": 0, "members": 0}
    try:
        from app.models import YoutubeChannelLink, ChurchMember, ChurchLevel
        with Session(engine) as session:
            for L in session.exec(
                select(YoutubeChannelLink).where(
                    YoutubeChannelLink.is_approved == True,
                    YoutubeChannelLink.is_active == True,
                ).order_by(YoutubeChannelLink.created_at.desc())
            ).all():
                if not L.youtube_video_id:
                    continue
                ch = session.get(ChurchUnit, L.church_id) if L.church_id else None
                youtube_clips.append({
                    "title": L.title or "YouTube",
                    "church": ch.name if ch else "Knowsoft Churchgate",
                    "video_id": L.youtube_video_id,
                    "url": L.youtube_url,
                })
            units = list(session.exec(select(ChurchUnit).where(ChurchUnit.is_active == True)).all())
            countries = set()
            states = set()
            n_churches = 0
            for u in units:
                if (u.approval_status or "approved") != "approved":
                    continue
                n_churches += 1
                lv = str(getattr(u.level, "value", u.level)).lower()
                if u.country_name:
                    countries.add(u.country_name.strip().lower())
                if "country" in lv:
                    countries.add((u.name or "").strip().lower())
                if u.state_name:
                    states.add(f"{(u.country_name or '')}:{(u.state_name or '')}".lower())
                if "state" in lv:
                    states.add((u.name or "").strip().lower())
            n_members = len(session.exec(
                select(ChurchMember).where(ChurchMember.approval_status == "approved", ChurchMember.is_active == True)
            ).all())
            live_stats = {
                "churches": max(n_churches, 1),
                "countries": max(len(countries), 1),
                "states": max(len(states), 1),
                "members": max(n_members, 1),
            }
    except Exception as e:
        print(f"home yt/stats: {e}")
    return templates.TemplateResponse("index.html", {
        "request": request, "user": None,
        "featured_programs": featured,
        "youtube_clips": youtube_clips,
        "live_stats": live_stats,
    })


@app.get("/manifest.json")
async def root_manifest():
    from fastapi.responses import FileResponse
    return FileResponse(str(BASE_DIR / "app" / "static" / "manifest.json"), media_type="application/manifest+json")

@app.get("/sw.js")
async def root_sw():
    from fastapi.responses import FileResponse, Response
    path = BASE_DIR / "app" / "static" / "sw.js"
    content = path.read_text(encoding="utf-8")
    return Response(
        content=content,
        media_type="application/javascript; charset=utf-8",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )

@app.get("/health")
async def health():
    return {"status": "ok", "app": "Knowsoft Churchgate"}

# Hidden admin portal
@app.get("/ks-admin/login", response_class=HTMLResponse)
async def ks_admin_login_page(request: Request):
    return templates.TemplateResponse("admin/login.html", {"request": request})

@app.post("/ks-admin/login")
async def ks_admin_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session)
):
    user = session.exec(select(User).where(User.email == email)).first()
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse("admin/login.html", {
            "request": request, "error": "Invalid credentials"
        }, status_code=400)
    if user.role != UserRole.general_admin:
        return templates.TemplateResponse("admin/login.html", {
            "request": request, "error": "General Admin only"
        }, status_code=403)
    token = create_access_token({"sub": user.email})
    resp = RedirectResponse("/admin/", status_code=303)
    resp.set_cookie("access_token", token, httponly=True, max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60, samesite="lax")
    return resp
