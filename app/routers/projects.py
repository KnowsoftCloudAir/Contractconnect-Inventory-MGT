from pathlib import Path
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import User, UserRole, ChurchUnit, SpecialProject, SpecialProjectContribution
from app.auth import require_user, require_roles

router = APIRouter(prefix="/projects", tags=["projects"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

@router.get("/", response_class=HTMLResponse)
async def list_projects(
    request: Request,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin, UserRole.data_officer)),
    session: Session = Depends(get_session)
):
    if user.role == UserRole.general_admin:
        projects = session.exec(select(SpecialProject).order_by(SpecialProject.created_at.desc())).all()
    else:
        if not user.church_id:
            raise HTTPException(400, "No church linked")
        projects = session.exec(
            select(SpecialProject).where(SpecialProject.church_id == user.church_id)
            .order_by(SpecialProject.created_at.desc())
        ).all()
    rows = []
    for p in projects:
        contribs = session.exec(
            select(SpecialProjectContribution).where(SpecialProjectContribution.project_id == p.id)
        ).all()
        total = sum(c.amount for c in contribs)
        rows.append({"project": p, "collected": total, "count": len(contribs)})
    return templates.TemplateResponse("projects/list.html", {
        "request": request, "user": user, "rows": rows
    })

@router.get("/create", response_class=HTMLResponse)
async def create_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    return templates.TemplateResponse("projects/create.html", {"request": request, "user": user})

@router.post("/create")
async def create_project(
    title: str = Form(...),
    description: str = Form(""),
    target_amount: float = Form(0),
    account_name: str = Form(""),
    account_number: str = Form(""),
    bank_name: str = Form(""),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin)),
    session: Session = Depends(get_session)
):
    if not user.church_id and user.role != UserRole.general_admin:
        raise HTTPException(400, "No church linked")
    church_id = user.church_id
    if not church_id:
        raise HTTPException(400, "Link a church to create a project")
    p = SpecialProject(
        church_id=church_id,
        title=title.strip(),
        description=description.strip() or None,
        target_amount=target_amount or 0,
        account_name=account_name.strip() or None,
        account_number=account_number.strip() or None,
        bank_name=bank_name.strip() or None,
        is_active=True,
    )
    session.add(p)
    session.commit()
    return RedirectResponse("/projects/", status_code=303)

@router.post("/{project_id}/contribute")
async def add_contribution(
    project_id: int,
    amount: float = Form(...),
    contributor_name: str = Form(""),
    note: str = Form(""),
    user: User = Depends(require_roles(UserRole.church_admin, UserRole.general_admin, UserRole.data_officer)),
    session: Session = Depends(get_session)
):
    p = session.get(SpecialProject, project_id)
    if not p:
        raise HTTPException(404, "Project not found")
    if user.role != UserRole.general_admin and p.church_id != user.church_id:
        raise HTTPException(403, "Not your project")
    session.add(SpecialProjectContribution(
        project_id=project_id,
        amount=amount,
        contributor_name=contributor_name.strip() or None,
        note=note.strip() or None,
        recorded_by=user.id,
    ))
    session.commit()
    return RedirectResponse("/projects/", status_code=303)
