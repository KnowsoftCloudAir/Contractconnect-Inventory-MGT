import shutil
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
import secrets, string

from app.database import get_session
from app.models import User, UserRole, ChurchUnit, ChurchLevel, ChurchMember, WeeklyStat
from app.auth import require_user, require_roles, get_password_hash

router = APIRouter(tags=["church"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

def gen_code(prefix="CG"):
    return f"{prefix}-" + ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(5))

def _level_val(level) -> str:
    return getattr(level, "value", str(level))

def collect_descendant_ids(session: Session, root_id: int) -> list:
    """All unit ids under root including root."""
    ids = [root_id]
    queue = [root_id]
    while queue:
        pid = queue.pop(0)
        kids = session.exec(select(ChurchUnit).where(ChurchUnit.parent_id == pid)).all()
        for k in kids:
            ids.append(k.id)
            queue.append(k.id)
    return ids




# Approximate country centroids for map when a unit has no lat/lng
_COUNTRY_COORDS = {
    "nigeria": (9.0820, 8.6753),
    "ghana": (7.9465, -1.0232),
    "kenya": (-1.2921, 36.8219),
    "south africa": (-30.5595, 22.9375),
    "united states": (37.0902, -95.7129),
    "united kingdom": (55.3781, -3.4360),
    "india": (20.5937, 78.9629),
    "canada": (56.1304, -106.3468),
    "australia": (-25.2744, 133.7751),
    "cameroon": (7.3697, 12.3547),
    "uganda": (1.3733, 32.2903),
    "tanzania": (-6.3690, 34.8888),
    "zambia": (-13.1339, 27.8493),
    "zimbabwe": (-19.0154, 29.1549),
    "egypt": (26.8206, 30.8025),
    "brazil": (-14.2350, -51.9253),
    "philippines": (12.8797, 121.7740),
}

# Approximate state/province centroids (country|state lowercase)
_STATE_COORDS = {
    "nigeria|lagos": (6.5244, 3.3792),
    "nigeria|abuja": (9.0765, 7.3986),
    "nigeria|federal capital territory": (9.0765, 7.3986),
    "nigeria|rivers": (4.8156, 7.0498),
    "nigeria|kano": (12.0022, 8.5920),
    "nigeria|oyo": (7.3775, 3.9470),
    "nigeria|kaduna": (10.5105, 7.4165),
    "ghana|greater accra": (5.6037, -0.1870),
    "kenya|nairobi": (-1.2921, 36.8219),
    "united states|california": (36.7783, -119.4179),
    "united states|texas": (31.9686, -99.9018),
    "united states|new york": (40.7128, -74.0060),
}


def resolve_unit_coords(u):
    """Return (lat, lng, source) for a church unit."""
    lat = getattr(u, "latitude", None)
    lng = getattr(u, "longitude", None)
    if lat is not None and lng is not None:
        try:
            return float(lat), float(lng), "exact"
        except (TypeError, ValueError):
            pass
    cn = (getattr(u, "country_name", None) or "").strip().lower()
    sn = (getattr(u, "state_name", None) or "").strip().lower()
    if cn and sn:
        key = f"{cn}|{sn}"
        if key in _STATE_COORDS:
            a, b = _STATE_COORDS[key]
            return a, b, "state"
    if cn in _COUNTRY_COORDS:
        a, b = _COUNTRY_COORDS[cn]
        return a, b, "country"
    return None, None, None


def build_map_payload(session, scope_ids):
    """Markers for each unit + country/state counts for the global map."""
    map_markers = []
    state_counts = {}
    country_counts = {}
    for uid in scope_ids:
        u = session.get(ChurchUnit, uid)
        if not u:
            continue
        lv = str(getattr(u.level, "value", u.level)).lower()
        cn = (u.country_name or "").strip() or "Unknown"
        sn = (u.state_name or "").strip() or "Unknown"
        # Prefer counting district churches as "assemblies"
        if lv == "district":
            state_counts[(cn, sn)] = state_counts.get((cn, sn), 0) + 1
            country_counts[cn] = country_counts.get(cn, 0) + 1
        lat, lng, src = resolve_unit_coords(u)
        if lat is None:
            continue
        map_markers.append({
            "name": u.name,
            "code": u.code,
            "level": lv,
            "lat": lat,
            "lng": lng,
            "address": u.address or "",
            "country": cn,
            "state": sn if sn != "Unknown" else "",
            "source": src,
        })
    # If no districts, count every non-global unit so the map still shows numbers
    if not country_counts:
        for uid in scope_ids:
            u = session.get(ChurchUnit, uid)
            if not u:
                continue
            lv = str(getattr(u.level, "value", u.level)).lower()
            if lv in ("global", "global_church"):
                continue
            cn = (u.country_name or "").strip() or "Unknown"
            sn = (u.state_name or "").strip() or "Unknown"
            state_counts[(cn, sn)] = state_counts.get((cn, sn), 0) + 1
            country_counts[cn] = country_counts.get(cn, 0) + 1

    country_summary = []
    for cn, count in sorted(country_counts.items(), key=lambda x: -x[1]):
        lat = lng = None
        key = cn.lower()
        if key in _COUNTRY_COORDS:
            lat, lng = _COUNTRY_COORDS[key]
        country_summary.append({
            "country": cn, "count": count,
            "lat": lat, "lng": lng,
        })
    state_summary = [
        {"country": a, "state": b, "count": n}
        for (a, b), n in sorted(state_counts.items(), key=lambda x: (x[0][0], -x[1]))
    ]
    # If no district counts, count every unit with a country
    if not country_summary:
        for m in map_markers:
            cn = m.get("country") or "Unknown"
            country_counts[cn] = country_counts.get(cn, 0) + 1
        country_summary = []
        for cn, count in sorted(country_counts.items(), key=lambda x: -x[1]):
            lat = lng = None
            if cn.lower() in _COUNTRY_COORDS:
                lat, lng = _COUNTRY_COORDS[cn.lower()]
            country_summary.append({"country": cn, "count": count, "lat": lat, "lng": lng})
    return map_markers, state_summary, country_summary



def build_parent_path(session: Session, unit: ChurchUnit) -> str:
    """Global > Country > State > Group > District style path."""
    parts = []
    cur = unit
    guard = 0
    while cur and guard < 10:
        parts.append(cur.name)
        if not cur.parent_id:
            break
        cur = session.get(ChurchUnit, cur.parent_id)
        guard += 1
    return " → ".join(reversed(parts))


def user_can_manage_tree(session: Session, user: User, target_church_id: int) -> bool:
    """True if target is user church or under it (or general admin)."""
    from app.auth import role_val
    if role_val(user.role) == "general_admin":
        return True
    if not user.church_id:
        return False
    if user.church_id == target_church_id:
        return True
    return target_church_id in collect_descendant_ids(session, user.church_id)


_COUNTRY_COORDS = {
    "nigeria": (9.0820, 8.6753),
    "ghana": (7.9465, -1.0232),
    "kenya": (-1.2921, 36.8219),
    "south africa": (-30.5595, 22.9375),
    "united states": (37.0902, -95.7129),
    "united kingdom": (55.3781, -3.4360),
    "india": (20.5937, 78.9629),
    "canada": (56.1304, -106.3468),
    "australia": (-25.2744, 133.7751),
    "cameroon": (7.3697, 12.3547),
    "uganda": (1.3733, 32.2903),
    "tanzania": (-6.3690, 34.8888),
    "zambia": (-13.1339, 27.8493),
    "zimbabwe": (-19.0154, 29.1549),
    "egypt": (26.8206, 30.8025),
    "brazil": (-14.2350, -51.9253),
    "philippines": (12.8797, 121.7740),
}

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    """Church dashboard — members without grant are redirected away."""
    from app.auth import role_val
    try:
        if role_val(user.role) == "member":
            return RedirectResponse("/member/portal", status_code=303)

        church = session.get(ChurchUnit, user.church_id) if user.church_id else None
        children = []
        stats = []
        members_count = 0
        chart_labels, chart_attendance, chart_offering, chart_tithe, chart_donation = [], [], [], [], []
        total_offering = total_tithe = 0.0
        latest_attendance = 0
        demo = {}
        map_markers = []
        state_summary = []
        country_summary = []
        is_global_view = False
        scope_ids = []

        if role_val(user.role) == "general_admin" and not church:
            churches = list(session.exec(select(ChurchUnit).order_by(ChurchUnit.name)).all())
            try:
                members_count = len(list(session.exec(select(ChurchMember)).all()))
            except Exception:
                members_count = 0
            return templates.TemplateResponse("church/dashboard.html", {
                "request": request, "user": user, "church": None,
                "children": churches, "stats": [], "members_count": members_count,
                "chart_labels": [], "chart_attendance": [], "chart_offering": [],
                "chart_tithe": [], "chart_donation": [],
                "total_offering": 0, "total_tithe": 0, "latest_attendance": 0,
                "is_admin_overview": True, "demo": {},
                "map_markers": [], "is_global_view": False, "state_summary": [], "country_summary": [],
                "admin_viewing": False,
            })

        if church:
            children = list(session.exec(select(ChurchUnit).where(ChurchUnit.parent_id == church.id)).all())
            scope_ids = collect_descendant_ids(session, church.id)
            try:
                members_list = list(session.exec(
                    select(ChurchMember).where(
                        ChurchMember.church_id.in_(scope_ids),
                        ChurchMember.approval_status == "approved",
                    )
                ).all())
            except Exception:
                members_list = []
            members_count = len(members_list)

            def count_sex_age(sex, ages):
                return sum(
                    1 for m in members_list
                    if (str(m.sex or "")).lower() in sex and (str(m.age_category or "")) in ages
                )

            demo = {
                "men": count_sex_age(["brother", "male"], ["adult", "campus"]),
                "women": count_sex_age(["sister", "female"], ["adult", "campus"]),
                "youth_boys": count_sex_age(["brother", "male"], ["youth"]),
                "youth_girls": count_sex_age(["sister", "female"], ["youth"]),
                "ya_boys": count_sex_age(["brother", "male"], ["campus"]),
                "ya_girls": count_sex_age(["sister", "female"], ["campus"]),
                "children_boys": count_sex_age(["brother", "male"], ["child"]),
                "children_girls": count_sex_age(["sister", "female"], ["child"]),
                "newcomers_men": 0, "newcomers_women": 0, "newcomers_children": 0,
                "converts_men": 0, "converts_women": 0, "converts_children": 0,
            }
            try:
                for s in session.exec(select(WeeklyStat).where(WeeklyStat.church_id.in_(scope_ids))).all():
                    n = int(getattr(s, "newcomers", 0) or 0)
                    c = int(getattr(s, "converts", 0) or 0)
                    demo["newcomers_men"] += n // 2
                    demo["newcomers_women"] += n - n // 2
                    demo["converts_men"] += c // 2
                    demo["converts_women"] += c - c // 2
            except Exception:
                pass

            try:
                own_stats = list(session.exec(
                    select(WeeklyStat).where(WeeklyStat.church_id == church.id)
                    .order_by(WeeklyStat.week_start.desc()).limit(12)
                ).all())
            except Exception:
                own_stats = []
            if own_stats:
                stats = list(reversed(own_stats))
            else:
                try:
                    all_stats = list(session.exec(
                        select(WeeklyStat).where(WeeklyStat.church_id.in_(scope_ids))
                    ).all())
                except Exception:
                    all_stats = []
                by_week = {}
                for s in all_stats:
                    key = str(getattr(s, "week_start", ""))
                    if key not in by_week:
                        by_week[key] = {
                            "week_start": s.week_start,
                            "adult_male": 0, "adult_female": 0,
                            "children_boys": 0, "children_girls": 0,
                            "youth_male": 0, "youth_female": 0,
                            "offering": 0.0, "tithe": 0.0, "donation": 0.0,
                        }
                    b = by_week[key]
                    for k in ("adult_male", "adult_female", "children_boys", "children_girls", "youth_male", "youth_female"):
                        b[k] += int(getattr(s, k, 0) or 0)
                    for k in ("offering", "tithe", "donation"):
                        b[k] += float(getattr(s, k, 0) or 0)

                class Agg:
                    def __init__(self, d):
                        self.__dict__.update(d)

                ordered = sorted(by_week.values(), key=lambda x: str(x["week_start"]))[-12:]
                stats = [Agg(d) for d in ordered]

            for s in stats:
                att = sum(int(getattr(s, k, 0) or 0) for k in (
                    "adult_male", "adult_female", "children_boys", "children_girls", "youth_male", "youth_female"
                ))
                chart_labels.append(str(getattr(s, "week_start", "")))
                chart_attendance.append(att)
                chart_offering.append(float(getattr(s, "offering", 0) or 0))
                chart_tithe.append(float(getattr(s, "tithe", 0) or 0))
                chart_donation.append(float(getattr(s, "donation", 0) or 0))
                total_offering += float(getattr(s, "offering", 0) or 0)
                total_tithe += float(getattr(s, "tithe", 0) or 0)
            if chart_attendance:
                latest_attendance = chart_attendance[-1]

            lv = str(getattr(church.level, "value", church.level)).lower()
            is_global_view = lv in ("global", "global_church")
            country_summary = []
            if is_global_view and scope_ids:
                map_markers, state_summary, country_summary = build_map_payload(session, scope_ids)

        return templates.TemplateResponse("church/dashboard.html", {
            "request": request, "user": user, "church": church,
            "children": children, "stats": stats, "members_count": members_count,
            "chart_labels": chart_labels, "chart_attendance": chart_attendance,
            "chart_offering": chart_offering, "chart_tithe": chart_tithe,
            "chart_donation": chart_donation, "total_offering": total_offering,
            "total_tithe": total_tithe, "latest_attendance": latest_attendance,
            "is_admin_overview": False, "demo": demo,
            "map_markers": map_markers, "is_global_view": is_global_view,
            "state_summary": state_summary, "country_summary": country_summary if "country_summary" in dir() else [], "admin_viewing": False,
        })
    except Exception as e:
        return HTMLResponse(
            f"""<!DOCTYPE html><html><head><title>Dashboard</title>
            <script src="https://cdn.tailwindcss.com"></script></head>
            <body class="bg-slate-100 p-8 font-sans">
            <div class="max-w-lg mx-auto bg-white rounded-2xl border p-8">
              <h1 class="text-xl font-bold mb-2">Dashboard</h1>
              <p class="text-sm text-slate-500 mb-4">Could not load the full dashboard.</p>
              <p class="text-xs text-red-600 mb-4">{e}</p>
              <div class="flex flex-wrap gap-3 text-sm">
                <a class="px-3 py-2 rounded-lg bg-slate-900 text-white" href="/district/members">Members</a>
                <a class="px-3 py-2 rounded-lg border" href="/district/stats/enter">Attendance</a>
                <a class="px-3 py-2 rounded-lg border" href="/programs/">Programs</a>
                <a class="px-3 py-2 rounded-lg border" href="/auth/logout">Sign out</a>
              </div>
            </div></body></html>"""
        )



@router.get("/church/create-child", response_class=HTMLResponse)
async def create_child_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    if user.role != UserRole.general_admin and not getattr(user, "can_create_churches", False):
        raise HTTPException(403, "General Admin has not granted you permission to create churches")
    church = session.get(ChurchUnit, user.church_id) if user.church_id else None
    if not church or church.approval_status != "approved":
        raise HTTPException(403, "Only approved churches can create sub-units")
    level_map = {"global": "country", "country": "state", "state": "group", "group": "district"}
    next_level = level_map.get(_level_val(church.level))
    if not next_level:
        return templates.TemplateResponse("church/message.html", {
            "request": request, "user": user,
            "title": "District is the lowest level",
            "message": "District churches cannot create child units."
        })
    return templates.TemplateResponse("church/create_child.html", {
        "request": request, "user": user, "parent": church, "next_level": next_level
    })

@router.post("/church/create-child")
async def create_child(
    request: Request,
    name: str = Form(...),
    admin_email: str = Form(...),
    admin_full_name: str = Form(...),
    admin_password: str = Form(...),
    resident_pastor: str = Form(""),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    if user.role != UserRole.general_admin and not getattr(user, "can_create_churches", False):
        raise HTTPException(403, "General Admin has not granted you permission to create churches")
    parent = session.get(ChurchUnit, user.church_id)
    if not parent or parent.approval_status != "approved":
        raise HTTPException(403, "Not allowed")

    level_map = {
        "global": ChurchLevel.country, "country": ChurchLevel.state,
        "state": ChurchLevel.group, "group": ChurchLevel.district
    }
    next_level = level_map.get(_level_val(parent.level))
    if not next_level:
        raise HTTPException(400, "Cannot create child under district")

    code = gen_code("CG")
    while session.exec(select(ChurchUnit).where(ChurchUnit.code == code)).first():
        code = gen_code("CG")

    child = ChurchUnit(
        code=code, name=name.strip(), level=next_level, parent_id=parent.id,
        global_code=parent.global_code or parent.code,
        country_code=parent.country_code, state_code=parent.state_code,
        group_code=parent.group_code, approval_status="approved",
        resident_pastor=resident_pastor.strip() or None, email=admin_email.strip()
    )
    lv = _level_val(next_level)
    if lv == "country":
        child.country_code = code
    elif lv == "state":
        child.state_code = code
    elif lv == "group":
        child.group_code = code
    elif lv == "district":
        child.district_code = code

    session.add(child)
    session.commit()
    session.refresh(child)

    if session.exec(select(User).where(User.email == admin_email)).first():
        raise HTTPException(400, "Email already used")

    admin = User(
        email=admin_email.strip(),
        hashed_password=get_password_hash(admin_password),
        full_name=admin_full_name.strip(),
        role=UserRole.church_admin,
        church_id=child.id,
        is_active=True,
        can_create_churches=True,
        can_approve_members=True,
    )
    session.add(admin)
    session.commit()
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/church/settings", response_class=HTMLResponse)
async def church_settings_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = session.get(ChurchUnit, user.church_id) if user.church_id else None
    if not church:
        raise HTTPException(400, "No church linked")
    return templates.TemplateResponse("church/settings.html", {"request": request, "user": user, "church": church})

@router.post("/church/settings")
async def church_settings_save(
    address: str = Form(""),
    resident_pastor: str = Form(""),
    pastor_phone: str = Form(""),
    pastor_email: str = Form(""),
    weekly_activities_note: str = Form(""),
    tithe_account_name: str = Form(""),
    tithe_account_number: str = Form(""),
    tithe_bank_name: str = Form(""),
    offering_account_name: str = Form(""),
    offering_account_number: str = Form(""),
    offering_bank_name: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    country_name: str = Form(""),
    state_name: str = Form(""),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = session.get(ChurchUnit, user.church_id) if user.church_id else None
    if not church:
        raise HTTPException(400, "No church linked")
    church.address = address.strip() or None
    church.resident_pastor = resident_pastor.strip() or None
    church.pastor_phone = pastor_phone.strip() or None
    church.pastor_email = pastor_email.strip() or None
    church.weekly_activities_note = weekly_activities_note.strip() or None
    church.tithe_account_name = tithe_account_name.strip() or None
    church.tithe_account_number = tithe_account_number.strip() or None
    church.tithe_bank_name = tithe_bank_name.strip() or None
    church.offering_account_name = offering_account_name.strip() or None
    church.offering_account_number = offering_account_number.strip() or None
    church.offering_bank_name = offering_bank_name.strip() or None
    if country_name.strip():
        church.country_name = country_name.strip()
    if state_name.strip():
        church.state_name = state_name.strip()
    try:
        church.latitude = float(latitude) if latitude.strip() else church.latitude
    except ValueError:
        pass
    try:
        church.longitude = float(longitude) if longitude.strip() else church.longitude
    except ValueError:
        pass
    session.add(church)
    session.commit()
    return RedirectResponse("/church/settings", status_code=303)


@router.get("/church/network", response_class=HTMLResponse)
async def church_network(
    request: Request,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    """List all churches under this unit (esp. districts) with parent tree."""
    from app.auth import role_val
    root = None
    if role_val(user.role) == "general_admin":
        # Prefer linked church; else first global
        root = session.get(ChurchUnit, user.church_id) if user.church_id else None
        if not root:
            from app.models import ChurchLevel
            root = session.exec(
                select(ChurchUnit).where(ChurchUnit.level == ChurchLevel.global_church)
            ).first()
    else:
        root = session.get(ChurchUnit, user.church_id) if user.church_id else None
    if not root:
        return templates.TemplateResponse("church/message.html", {
            "request": request, "user": user,
            "title": "Network", "message": "No church linked to this account.",
        })
    ids = collect_descendant_ids(session, root.id)
    units = []
    for uid in ids:
        u = session.get(ChurchUnit, uid)
        if not u:
            continue
        path = build_parent_path(session, u)
        lv = str(getattr(u.level, "value", u.level)).lower()
        units.append({"unit": u, "path": path, "level": lv})
    # Sort: global, country, state, group, district then name
    order = {"global": 0, "global_church": 0, "country": 1, "state": 2, "group": 3, "district": 4}
    units.sort(key=lambda x: (order.get(x["level"], 9), x["unit"].name or ""))
    districts = [x for x in units if x["level"] == "district"]
    # Sub-admins under this tree
    subadmins = []
    for uid in ids:
        for u in session.exec(select(User).where(User.church_id == uid)).all():
            from app.auth import role_val
            rv = role_val(u.role)
            if rv in ("church_admin", "data_officer"):
                ch = session.get(ChurchUnit, u.church_id)
                subadmins.append({"user": u, "church": ch, "path": build_parent_path(session, ch) if ch else ""})
    return templates.TemplateResponse("church/network.html", {
        "request": request, "user": user, "root": root,
        "units": units, "districts": districts, "subadmins": subadmins,
    })


@router.get("/church/units/{church_id}/dashboard", response_class=HTMLResponse)
async def view_unit_dashboard(
    church_id: int,
    request: Request,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    """Open dashboard for a church in your tree (read-only context of that unit)."""
    if not user_can_manage_tree(session, user, church_id):
        raise HTTPException(403, "That church is outside your network")
    church = session.get(ChurchUnit, church_id)
    if not church:
        raise HTTPException(404, "Church not found")
    ids = collect_descendant_ids(session, church.id)
    children = list(session.exec(select(ChurchUnit).where(ChurchUnit.parent_id == church.id)).all())
    try:
        members_list = list(session.exec(
            select(ChurchMember).where(
                ChurchMember.church_id.in_(ids),
                ChurchMember.approval_status == "approved",
            )
        ).all())
    except Exception:
        members_list = []
    members_count = len(members_list)
    demo = {
        "men": 0, "women": 0, "youth_boys": 0, "youth_girls": 0,
        "ya_boys": 0, "ya_girls": 0, "children_boys": 0, "children_girls": 0,
        "newcomers_men": 0, "newcomers_women": 0, "newcomers_children": 0,
        "converts_men": 0, "converts_women": 0, "converts_children": 0,
    }
    for m in members_list:
        sex = (str(m.sex or "")).lower()
        age = str(m.age_category or "")
        if sex in ("brother", "male"):
            if age in ("adult", "campus"):
                demo["men"] += 1
            elif age == "youth":
                demo["youth_boys"] += 1
            elif age == "child":
                demo["children_boys"] += 1
            if age == "campus":
                demo["ya_boys"] += 1
        elif sex in ("sister", "female"):
            if age in ("adult", "campus"):
                demo["women"] += 1
            elif age == "youth":
                demo["youth_girls"] += 1
            elif age == "child":
                demo["children_girls"] += 1
            if age == "campus":
                demo["ya_girls"] += 1
    map_markers = []
    state_summary = []
    lv = str(getattr(church.level, "value", church.level)).lower()
    is_global_view = lv in ("global", "global_church")
    country_summary = []
    if is_global_view:
        map_markers, state_summary, country_summary = build_map_payload(session, ids)
    return templates.TemplateResponse("church/dashboard.html", {
        "request": request, "user": user, "church": church,
        "children": children, "stats": [], "members_count": members_count,
        "chart_labels": [], "chart_attendance": [], "chart_offering": [],
        "chart_tithe": [], "chart_donation": [],
        "total_offering": 0, "total_tithe": 0, "latest_attendance": 0,
        "is_admin_overview": False, "demo": demo,
        "map_markers": map_markers, "is_global_view": is_global_view,
        "state_summary": state_summary, "country_summary": country_summary if "country_summary" in dir() else [], "admin_viewing": True,
    })


@router.post("/church/subadmins/{user_id}/password")
async def reset_subadmin_password(
    user_id: int,
    new_password: str = Form(...),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    target = session.get(User, user_id)
    if not target or not target.church_id:
        raise HTTPException(404, "User not found")
    if not user_can_manage_tree(session, user, target.church_id):
        raise HTTPException(403, "Outside your network")
    from app.auth import role_val
    if role_val(target.role) == "general_admin":
        raise HTTPException(403, "Cannot change General Admin password here")
    if len(new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    target.hashed_password = get_password_hash(new_password)
    session.add(target)
    session.commit()
    return RedirectResponse("/church/network?msg=password_updated", status_code=303)


@router.post("/church/subadmins/{user_id}/privileges")
async def update_subadmin_privileges(
    user_id: int,
    can_create_churches: str = Form(""),
    can_approve_members: str = Form(""),
    can_enter_stats: str = Form(""),
    can_see_member_count: str = Form(""),
    can_view_church_dashboard: str = Form(""),
    is_active: str = Form("yes"),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    target = session.get(User, user_id)
    if not target or not target.church_id:
        raise HTTPException(404, "User not found")
    if not user_can_manage_tree(session, user, target.church_id):
        raise HTTPException(403, "Outside your network")
    from app.auth import role_val
    if role_val(target.role) == "general_admin":
        raise HTTPException(403, "Cannot edit General Admin")
    # Global/country admins may reduce privileges of lower-level admins
    target.can_create_churches = can_create_churches == "yes"
    target.can_approve_members = can_approve_members == "yes"
    target.can_enter_stats = can_enter_stats == "yes"
    if hasattr(target, "can_see_member_count"):
        target.can_see_member_count = can_see_member_count == "yes"
    if hasattr(target, "can_view_church_dashboard"):
        target.can_view_church_dashboard = can_view_church_dashboard == "yes"
    target.is_active = is_active == "yes"
    session.add(target)
    session.commit()
    return RedirectResponse("/church/network?msg=privileges_updated", status_code=303)


LOGO_DIR = Path("app/static/uploads/logos")
LOGO_DIR.mkdir(parents=True, exist_ok=True)

@router.post("/church/settings/logo")
async def church_logo_upload(
    file: UploadFile = File(...),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    """Global church (or any church admin) can set a logo shown on their pages and home adverts."""
    if not user.church_id:
        raise HTTPException(400, "No church linked")
    church = session.get(ChurchUnit, user.church_id)
    if not church:
        raise HTTPException(404, "Church not found")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Images only")
    ext = (file.filename or "logo.png").rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "gif", "webp", "svg"):
        ext = "png"
    fname = f"church_{church.id}_{uuid.uuid4().hex[:8]}.{ext}"
    dest = LOGO_DIR / fname
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    # remove old file if under uploads/logos
    old = getattr(church, "logo_url", None) or ""
    if "/static/uploads/logos/" in old:
        try:
            Path("app") .joinpath(old.lstrip("/").replace("static/", "static/", 1))
            p = Path("app/static/uploads/logos") / Path(old).name
            if p.exists():
                p.unlink(missing_ok=True)
        except Exception:
            pass
    church.logo_url = f"/static/uploads/logos/{fname}"
    session.add(church)
    session.commit()
    return RedirectResponse("/church/settings", status_code=303)


@router.post("/church/settings/logo/remove")
async def church_logo_remove(
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    if not user.church_id:
        raise HTTPException(400)
    church = session.get(ChurchUnit, user.church_id)
    if not church:
        raise HTTPException(404)
    old = getattr(church, "logo_url", None) or ""
    if old:
        try:
            p = Path("app/static/uploads/logos") / Path(old).name
            if p.exists():
                p.unlink(missing_ok=True)
        except Exception:
            pass
    church.logo_url = None
    session.add(church)
    session.commit()
    return RedirectResponse("/church/settings", status_code=303)
