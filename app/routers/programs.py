from pathlib import Path
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from datetime import date
import uuid, shutil

from app.database import get_session
from app.models import (
    User, UserRole, ChurchUnit, ChurchLevel, SpecialProgram, ProgramPhoto,
    PhotoLike, PhotoComment, ChurchMember, ProgramVideo,
)
from app.auth import require_user, require_roles

router = APIRouter(prefix="/programs", tags=["programs"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
UPLOAD = Path("app/static/uploads/programs")
UPLOAD.mkdir(parents=True, exist_ok=True)
VIDEO_UPLOAD = Path("app/static/uploads/program_videos")
VIDEO_UPLOAD.mkdir(parents=True, exist_ok=True)

def member_scope_church_ids(user: User, session: Session) -> set:
    """Churches whose program photos this member may see (state, group, district of their tree)."""
    ids = set()
    if user.church_id:
        ids.add(user.church_id)
    member = None
    if user.member_id:
        member = session.get(ChurchMember, user.member_id)
    if not member and user.email:
        member = session.exec(select(ChurchMember).where(ChurchMember.email == user.email)).first()
    if member:
        for cid in (member.district_church_id if hasattr(member, 'district_church_id') else None,
                    member.church_id, member.group_church_id, member.state_church_id,
                    member.country_church_id, member.global_church_id):
            if cid:
                ids.add(cid)
        # also include parent chain of district
        ch = session.get(ChurchUnit, member.church_id)
        while ch:
            ids.add(ch.id)
            if ch.parent_id:
                ch = session.get(ChurchUnit, ch.parent_id)
            else:
                break
    return ids

@router.get("/", response_class=HTMLResponse)
async def list_programs(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    if user.role == UserRole.general_admin:
        programs = session.exec(select(SpecialProgram).order_by(SpecialProgram.created_at.desc()).limit(50)).all()
    elif user.role in (UserRole.church_admin, UserRole.data_officer):
        # own church + children broadcasts
        programs = session.exec(
            select(SpecialProgram).where(SpecialProgram.church_id == user.church_id)
            .order_by(SpecialProgram.created_at.desc())
        ).all()
        # also programs broadcast into this level from parents - simplified: show all in hierarchy
        scope = member_scope_church_ids(user, session)
        more = session.exec(select(SpecialProgram).where(SpecialProgram.church_id.in_(list(scope) or [0]))).all()
        seen = {p.id for p in programs}
        for p in more:
            if p.id not in seen:
                programs.append(p)
    else:
        scope = member_scope_church_ids(user, session)
        programs = session.exec(
            select(SpecialProgram).where(
                SpecialProgram.church_id.in_(list(scope) or [0]),
                SpecialProgram.is_active == True
            ).order_by(SpecialProgram.created_at.desc())
        ).all() if scope else []

    return templates.TemplateResponse("programs/list.html", {
        "request": request, "user": user, "programs": programs
    })

@router.get("/create", response_class=HTMLResponse)
async def create_program_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    church = session.get(ChurchUnit, user.church_id) if user.church_id else None
    return templates.TemplateResponse("programs/create.html", {
        "request": request, "user": user, "church": church
    })

@router.post("/create")
async def create_program(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    program_date: str = Form(""),
    location: str = Form(""),
    broadcast_to: str = Form("district"),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    if not user.church_id:
        raise HTTPException(400, "No church linked")
    pd = None
    if program_date:
        try:
            pd = date.fromisoformat(program_date)
        except ValueError:
            pass
    # Global church may request home display at create time
    form_data = await request.form()
    want_home = form_data.get("request_home_display") == "yes"
    church = session.get(ChurchUnit, user.church_id)
    lv = str(getattr(church.level, "value", church.level)).lower() if church else ""
    is_global = lv in ("global", "global_church")
    prog = SpecialProgram(
        church_id=user.church_id,
        title=title.strip(),
        description=description.strip() or None,
        program_date=pd,
        location=location.strip() or None,
        broadcast_to=broadcast_to,
        created_by=user.id,
        is_active=True,
        request_home_display=bool(want_home and is_global),
        featured_on_home=False,
    )
    session.add(prog)
    session.commit()
    session.refresh(prog)
    return RedirectResponse(f"/programs/{prog.id}", status_code=303)

@router.get("/{program_id}", response_class=HTMLResponse)
async def view_program(
    program_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    prog = session.get(SpecialProgram, program_id)
    if not prog:
        raise HTTPException(404, "Program not found")
    photos = session.exec(select(ProgramPhoto).where(ProgramPhoto.program_id == program_id)).all()
    photo_data = []
    for ph in photos:
        likes = session.exec(select(PhotoLike).where(PhotoLike.photo_id == ph.id)).all()
        comments = session.exec(
            select(PhotoComment).where(PhotoComment.photo_id == ph.id).order_by(PhotoComment.created_at)
        ).all()
        comment_list = []
        for cm in comments:
            u = session.get(User, cm.user_id)
            comment_list.append({"id": cm.id, "body": cm.body, "user": u.full_name if u else "Member", "user_id": cm.user_id, "at": cm.created_at})
        liked = any(l.user_id == user.id for l in likes)
        photo_data.append({
            "id": ph.id, "path": ph.file_path, "caption": ph.caption,
            "likes": len(likes), "liked": liked, "comments": comment_list
        })
    videos = list(session.exec(
        select(ProgramVideo).where(ProgramVideo.program_id == program_id)
        .order_by(ProgramVideo.created_at.desc())
    ).all())
    video_data = [{"id": v.id, "path": v.file_path, "caption": v.caption} for v in videos]
    church = session.get(ChurchUnit, prog.church_id)
    from app.auth import role_val
    can_upload = role_val(user.role) in ("church_admin", "general_admin", "data_officer")
    lv = str(getattr(church.level, "value", church.level)).lower() if church else ""
    is_global_church = lv in ("global", "global_church")
    can_request_home = is_global_church and (
        role_val(user.role) in ("church_admin", "general_admin")
        and (user.church_id == prog.church_id or role_val(user.role) == "general_admin")
    )
    # Safe attribute access if DB columns not yet migrated
    req = bool(getattr(prog, "request_home_display", False))
    feat = bool(getattr(prog, "featured_on_home", False))
    ends = getattr(prog, "home_display_ends_at", None)
    return templates.TemplateResponse("programs/view.html", {
        "request": request, "user": user, "program": prog, "church": church,
        "photos": photo_data, "videos": video_data, "can_upload": can_upload,
        "is_global_church": is_global_church,
        "can_request_home": can_request_home,
        "home_requested": req,
        "home_featured": feat,
        "home_ends_at": ends,
    })

def _can_manage_program(user: User, prog: SpecialProgram) -> bool:
    from app.auth import role_val
    rv = role_val(user.role)
    if rv == "general_admin":
        return True
    if rv in ("church_admin", "data_officer") and user.church_id == prog.church_id:
        return True
    return False


def _delete_video_files_and_rows(session: Session, program_id: int):
    for v in list(session.exec(select(ProgramVideo).where(ProgramVideo.program_id == program_id)).all()):
        try:
            p = Path("app/static/uploads/program_videos") / Path(v.file_path).name
            if p.exists():
                p.unlink(missing_ok=True)
        except Exception:
            pass
        session.delete(v)
    session.commit()


def _delete_photo_files_and_rows(session: Session, program_id: int):
    """Remove all photos for a program (files + likes/comments + rows)."""
    photos = list(session.exec(select(ProgramPhoto).where(ProgramPhoto.program_id == program_id)).all())
    for ph in photos:
        for like in session.exec(select(PhotoLike).where(PhotoLike.photo_id == ph.id)).all():
            session.delete(like)
        for cm in session.exec(select(PhotoComment).where(PhotoComment.photo_id == ph.id)).all():
            session.delete(cm)
        # disk file
        try:
            rel = (ph.file_path or "").lstrip("/")
            for candidate in (Path(rel), Path("app") / rel, UPLOAD / Path(rel).name):
                if candidate.exists() and candidate.is_file():
                    candidate.unlink(missing_ok=True)
                    break
        except Exception:
            pass
        session.delete(ph)
    session.commit()


@router.post("/{program_id}/photo")
async def upload_photo(
    program_id: int,
    caption: str = Form(""),
    file: UploadFile = File(...),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin, UserRole.data_officer)),
    session: Session = Depends(get_session)
):
    """Upload program picture. A new upload replaces any existing picture(s).
    If the program was on the home page (or requested), photo change re-requests General Admin approval.
    """
    prog = session.get(SpecialProgram, program_id)
    if not prog:
        raise HTTPException(404, "Program not found")
    if not _can_manage_program(user, prog):
        raise HTTPException(403, "Not your church program")
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "Images only")

    had_photo = session.exec(select(ProgramPhoto).where(ProgramPhoto.program_id == program_id)).first() is not None
    was_home = bool(getattr(prog, "featured_on_home", False) or getattr(prog, "request_home_display", False))

    # One media only: photo replaces any existing photo AND removes video
    _delete_photo_files_and_rows(session, program_id)
    _delete_video_files_and_rows(session, program_id)

    ext = (file.filename or "img.jpg").rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
        ext = "jpg"
    fname = f"prog_{program_id}_{uuid.uuid4().hex[:8]}.{ext}"
    dest = UPLOAD / fname
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    ph = ProgramPhoto(
        program_id=program_id,
        file_path=f"/static/uploads/programs/{fname}",
        caption=caption.strip() or None,
        uploaded_by=user.id,
    )
    session.add(ph)

    # New picture → new home approval cycle for Global showcase
    if was_home or had_photo:
        from app.models import ProgramVideo, ChurchUnit
        church = session.get(ChurchUnit, prog.church_id)
        lv = str(getattr(church.level, "value", church.level)).lower() if church else ""
        if lv in ("global", "global_church"):
            prog.request_home_display = True
            prog.featured_on_home = False
            prog.home_display_hours = None
            prog.home_display_starts_at = None
            prog.home_display_ends_at = None
            session.add(prog)

    session.commit()
    return RedirectResponse(f"/programs/{program_id}", status_code=303)


@router.post("/photo/{photo_id}/caption")
async def edit_photo_caption(
    photo_id: int,
    caption: str = Form(""),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin, UserRole.data_officer)),
    session: Session = Depends(get_session),
):
    ph = session.get(ProgramPhoto, photo_id)
    if not ph:
        raise HTTPException(404, "Photo not found")
    prog = session.get(SpecialProgram, ph.program_id)
    if not prog or not _can_manage_program(user, prog):
        raise HTTPException(403, "Not allowed")
    ph.caption = caption.strip() or None
    session.add(ph)
    session.commit()
    return RedirectResponse(f"/programs/{ph.program_id}", status_code=303)


@router.post("/photo/{photo_id}/delete")
async def delete_program_photo(
    photo_id: int,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin, UserRole.data_officer)),
    session: Session = Depends(get_session),
):
    ph = session.get(ProgramPhoto, photo_id)
    if not ph:
        raise HTTPException(404, "Photo not found")
    prog = session.get(SpecialProgram, ph.program_id)
    if not prog or not _can_manage_program(user, prog):
        raise HTTPException(403, "Not allowed")
    pid = ph.program_id
    # delete this one only (then use helper pattern)
    for like in session.exec(select(PhotoLike).where(PhotoLike.photo_id == ph.id)).all():
        session.delete(like)
    for cm in session.exec(select(PhotoComment).where(PhotoComment.photo_id == ph.id)).all():
        session.delete(cm)
    try:
        rel = (ph.file_path or "").lstrip("/")
        for candidate in (Path(rel), Path("app") / rel, UPLOAD / Path(rel).name):
            if candidate.exists() and candidate.is_file():
                candidate.unlink(missing_ok=True)
                break
    except Exception:
        pass
    session.delete(ph)
    session.commit()
    return RedirectResponse(f"/programs/{pid}", status_code=303)


@router.post("/{program_id}/delete")
async def delete_program(
    program_id: int,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    """Creator church admin or general admin can delete a program that is no longer required."""
    prog = session.get(SpecialProgram, program_id)
    if not prog:
        raise HTTPException(404, "Program not found")
    from app.auth import role_val
    rv = role_val(user.role)
    if rv != "general_admin" and not (rv == "church_admin" and user.church_id == prog.church_id):
        raise HTTPException(403, "Only the owning church admin can delete this program")
    _delete_photo_files_and_rows(session, program_id)
    for v in session.exec(select(ProgramVideo).where(ProgramVideo.program_id == program_id)).all():
        try:
            vp = Path("app/static/uploads/program_videos") / Path(v.file_path).name
            if vp.exists():
                vp.unlink(missing_ok=True)
        except Exception:
            pass
        session.delete(v)
    session.delete(prog)
    session.commit()
    return RedirectResponse("/programs/", status_code=303)

@router.get("/photo/{photo_id}/download")
async def download_photo(
    photo_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    """Only logged-in approved members / staff can download."""
    if user.role == UserRole.member:
        member = session.get(ChurchMember, user.member_id) if user.member_id else None
        if member and member.approval_status != "approved":
            raise HTTPException(403, "Membership not approved yet")
    ph = session.get(ProgramPhoto, photo_id)
    if not ph:
        raise HTTPException(404, "Photo not found")
    path = Path("app") / ph.file_path.lstrip("/")
    if not path.exists():
        path = Path(ph.file_path.lstrip("/"))
    if not path.exists():
        # try relative from static
        path = Path("app/static/uploads/programs") / Path(ph.file_path).name
    if not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(path, filename=path.name)

@router.post("/photo/{photo_id}/like")
async def like_photo(
    photo_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    existing = session.exec(
        select(PhotoLike).where(PhotoLike.photo_id == photo_id, PhotoLike.user_id == user.id)
    ).first()
    if existing:
        session.delete(existing)
    else:
        session.add(PhotoLike(photo_id=photo_id, user_id=user.id))
    session.commit()
    ph = session.get(ProgramPhoto, photo_id)
    return RedirectResponse(f"/programs/{ph.program_id}" if ph else "/programs/", status_code=303)

@router.post("/photo/{photo_id}/comment")
async def comment_photo(
    photo_id: int,
    body: str = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session)
):
    if not body.strip():
        raise HTTPException(400, "Empty comment")
    session.add(PhotoComment(photo_id=photo_id, user_id=user.id, body=body.strip()))
    session.commit()
    ph = session.get(ProgramPhoto, photo_id)
    return RedirectResponse(f"/programs/{ph.program_id}" if ph else "/programs/", status_code=303)


@router.post("/photo/comment/{comment_id}/edit")
async def edit_photo_comment(
    comment_id: int,
    body: str = Form(...),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    cm = session.get(PhotoComment, comment_id)
    if not cm:
        raise HTTPException(404, "Comment not found")
    # Only the author may edit — never another user (including staff)
    if cm.user_id != user.id:
        raise HTTPException(403, "You can only edit your own comment")
    body = body.strip()
    if not body:
        raise HTTPException(400, "Empty comment")
    cm.body = body
    session.add(cm)
    session.commit()
    ph = session.get(ProgramPhoto, cm.photo_id)
    return RedirectResponse(f"/programs/{ph.program_id}" if ph else "/programs/", status_code=303)


@router.post("/photo/comment/{comment_id}/delete")
async def delete_photo_comment(
    comment_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    cm = session.get(PhotoComment, comment_id)
    if not cm:
        raise HTTPException(404, "Comment not found")
    from app.auth import role_val
    is_admin = role_val(user.role) in ("church_admin", "general_admin")
    if cm.user_id != user.id and not is_admin:
        raise HTTPException(403, "You can only delete your own comment")
    pid = cm.photo_id
    session.delete(cm)
    session.commit()
    ph = session.get(ProgramPhoto, pid)
    return RedirectResponse(f"/programs/{ph.program_id}" if ph else "/programs/", status_code=303)




@router.post("/{program_id}/video")
async def upload_program_video(
    program_id: int,
    caption: str = Form(""),
    file: UploadFile = File(...),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin, UserRole.data_officer)),
    session: Session = Depends(get_session),
):
    """Staff can attach a short video to a program (MP4/WebM)."""
    prog = session.get(SpecialProgram, program_id)
    if not prog:
        raise HTTPException(404, "Program not found")
    if not _can_manage_program(user, prog):
        raise HTTPException(403, "Not your church program")
    name = (file.filename or "").lower()
    ct = (file.content_type or "")
    if not (ct.startswith("video/") or name.endswith((".mp4", ".webm", ".mov"))):
        raise HTTPException(400, "Please upload a video file (MP4 or WebM)")
    was_home = bool(getattr(prog, "featured_on_home", False) or getattr(prog, "request_home_display", False))
    # One media only: video replaces any existing video AND removes photos
    _delete_video_files_and_rows(session, program_id)
    _delete_photo_files_and_rows(session, program_id)

    data = await file.read()
    if len(data) > 40 * 1024 * 1024:
        raise HTTPException(400, "Video too large (max 40 MB)")
    ext = name.rsplit(".", 1)[-1] if "." in name else "mp4"
    if ext not in ("mp4", "webm", "mov"):
        ext = "mp4"
    fname = f"progvid_{program_id}_{uuid.uuid4().hex[:8]}.{ext}"
    dest = VIDEO_UPLOAD / fname
    dest.write_bytes(data)
    session.add(ProgramVideo(
        program_id=program_id,
        file_path=f"/static/uploads/program_videos/{fname}",
        caption=caption.strip() or None,
        uploaded_by=user.id,
    ))
    # Media change → re-request home approval for Global programs
    from app.models import ChurchUnit
    church = session.get(ChurchUnit, prog.church_id)
    lv = str(getattr(church.level, "value", church.level)).lower() if church else ""
    if was_home or lv in ("global", "global_church"):
        if lv in ("global", "global_church") and was_home:
            prog.request_home_display = True
            prog.featured_on_home = False
            prog.home_display_hours = None
            prog.home_display_starts_at = None
            prog.home_display_ends_at = None
            session.add(prog)
    session.commit()
    return RedirectResponse(f"/programs/{program_id}", status_code=303)


@router.post("/video/{video_id}/delete")
async def delete_program_video(
    video_id: int,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin, UserRole.data_officer)),
    session: Session = Depends(get_session),
):
    v = session.get(ProgramVideo, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    prog = session.get(SpecialProgram, v.program_id)
    if not prog or not _can_manage_program(user, prog):
        raise HTTPException(403, "Not allowed")
    pid = v.program_id
    try:
        p = Path("app/static/uploads/program_videos") / Path(v.file_path).name
        if p.exists():
            p.unlink(missing_ok=True)
    except Exception:
        pass
    session.delete(v)
    session.commit()
    return RedirectResponse(f"/programs/{pid}", status_code=303)

@router.post("/{program_id}/request-home")
async def request_home_display(
    program_id: int,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    """Global church asks General Admin to show this program on the public home page."""
    from app.auth import role_val
    p = session.get(SpecialProgram, program_id)
    if not p:
        raise HTTPException(404, "Program not found")
    if user.church_id != p.church_id and role_val(user.role) != "general_admin":
        raise HTTPException(403, "Not your program")
    church = session.get(ChurchUnit, p.church_id)
    lv = str(getattr(church.level, "value", church.level)).lower() if church else ""
    if lv not in ("global", "global_church"):
        raise HTTPException(400, "Only Global church programs can be requested for the home page")
    try:
        p.request_home_display = True
    except Exception:
        raise HTTPException(500, "Database missing home-display columns — redeploy / restart app")
    p.featured_on_home = False
    session.add(p)
    session.commit()
    return RedirectResponse(f"/programs/{program_id}", status_code=303)


@router.post("/{program_id}/cancel-home-request")
async def cancel_home_request(
    program_id: int,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    """Global church withdraws home-page request (program stays on children pages only)."""
    from app.auth import role_val
    p = session.get(SpecialProgram, program_id)
    if not p:
        raise HTTPException(404, "Program not found")
    if user.church_id != p.church_id and role_val(user.role) != "general_admin":
        raise HTTPException(403, "Not your program")
    p.request_home_display = False
    # If not approved yet, fine; if approved, global can also stop wanting it — unfeature
    p.featured_on_home = False
    p.home_display_ends_at = None
    p.home_display_starts_at = None
    p.home_display_hours = None
    session.add(p)
    session.commit()
    return RedirectResponse(f"/programs/{program_id}", status_code=303)
