"""Platform payments: General Admin sets details; church admins view; confirm + receipt."""
from pathlib import Path
from datetime import datetime
from typing import Optional
import secrets
import string

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import User, UserRole, ChurchUnit, PaymentSettings, ChurchPayment
from app.auth import require_user, require_roles, role_val

router = APIRouter(tags=["payments"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _gen_ref(prefix="PAY"):
    return prefix + "-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))


def _get_or_create_settings(session: Session) -> PaymentSettings:
    s = session.exec(select(PaymentSettings).where(PaymentSettings.is_active == True)).first()
    if not s:
        s = PaymentSettings(
            title="Knowsoft Churchgate service fee",
            instructions="Transfer the amount below and notify Knowsoft Admin with your church name and payment reference.",
            bank_name="",
            account_name="Knowsoft Technologies",
            account_number="",
            currency="NGN",
            default_amount=0.0,
            is_active=True,
        )
        session.add(s)
        session.commit()
        session.refresh(s)
    return s


# ---------- Church admin: payment page ----------
@router.get("/payments", response_class=HTMLResponse)
async def church_payments_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    rv = role_val(user.role)
    if rv not in ("church_admin", "general_admin", "data_officer"):
        raise HTTPException(403, "Church staff only")
    settings = _get_or_create_settings(session)
    payments = []
    church = None
    if user.church_id:
        church = session.get(ChurchUnit, user.church_id)
        payments = list(session.exec(
            select(ChurchPayment).where(ChurchPayment.church_id == user.church_id)
            .order_by(ChurchPayment.created_at.desc())
        ).all())
    elif rv == "general_admin":
        return RedirectResponse("/admin/payments", status_code=303)
    return templates.TemplateResponse("payments/church.html", {
        "request": request, "user": user, "settings": settings,
        "payments": payments, "church": church,
    })


# ---------- General Admin ----------
@router.get("/admin/payments", response_class=HTMLResponse)
async def admin_payments_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    settings = _get_or_create_settings(session)
    payments = list(session.exec(
        select(ChurchPayment).order_by(ChurchPayment.created_at.desc()).limit(100)
    ).all())
    rows = []
    for p in payments:
        ch = session.get(ChurchUnit, p.church_id)
        rows.append({"p": p, "church": ch})
    churches = list(session.exec(
        select(ChurchUnit).where(ChurchUnit.approval_status == "approved", ChurchUnit.is_active == True)
        .order_by(ChurchUnit.name)
    ).all())
    return templates.TemplateResponse("payments/admin.html", {
        "request": request, "user": user, "settings": settings,
        "rows": rows, "churches": churches,
    })


@router.post("/admin/payments/settings")
async def admin_save_settings(
    title: str = Form(...),
    instructions: str = Form(""),
    bank_name: str = Form(""),
    account_name: str = Form(""),
    account_number: str = Form(""),
    currency: str = Form("NGN"),
    default_amount: float = Form(0),
    other_details: str = Form(""),
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    s = _get_or_create_settings(session)
    s.title = title.strip()
    s.instructions = instructions.strip() or None
    s.bank_name = bank_name.strip() or None
    s.account_name = account_name.strip() or None
    s.account_number = account_number.strip() or None
    s.currency = (currency or "NGN").strip()
    s.default_amount = float(default_amount or 0)
    s.other_details = other_details.strip() or None
    s.updated_at = datetime.utcnow()
    s.updated_by = user.id
    session.add(s)
    session.commit()
    return RedirectResponse("/admin/payments", status_code=303)


@router.post("/admin/payments/create")
async def admin_create_payment(
    church_id: int = Form(...),
    amount: float = Form(...),
    currency: str = Form("NGN"),
    description: str = Form(""),
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    church = session.get(ChurchUnit, church_id)
    if not church:
        raise HTTPException(404, "Church not found")
    amount = float(amount)
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    ref = _gen_ref()
    pay = ChurchPayment(
        church_id=church_id,
        amount=amount,
        currency=(currency or "NGN").strip(),
        description=description.strip() or f"Payment for {church.name}",
        status="pending",
        reference=ref,
        created_by=user.id,
    )
    session.add(pay)
    session.commit()
    return RedirectResponse("/admin/payments", status_code=303)


@router.post("/admin/payments/{payment_id}/confirm")
async def admin_confirm_payment(
    payment_id: int,
    receipt_note: str = Form(""),
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    """Confirm payment received and issue a receipt visible on the church payment page."""
    pay = session.get(ChurchPayment, payment_id)
    if not pay:
        raise HTTPException(404, "Payment not found")
    if pay.status == "confirmed":
        return RedirectResponse("/admin/payments", status_code=303)
    now = datetime.utcnow()
    pay.status = "confirmed"
    pay.paid_at = pay.paid_at or now
    pay.confirmed_at = now
    pay.confirmed_by = user.id
    pay.receipt_number = pay.receipt_number or _gen_ref("RCPT")
    pay.receipt_note = (receipt_note.strip() or None) or "Payment received. Thank you."
    session.add(pay)
    session.commit()
    return RedirectResponse("/admin/payments", status_code=303)


@router.post("/admin/payments/{payment_id}/cancel")
async def admin_cancel_payment(
    payment_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    pay = session.get(ChurchPayment, payment_id)
    if not pay:
        raise HTTPException(404)
    pay.status = "cancelled"
    session.add(pay)
    session.commit()
    return RedirectResponse("/admin/payments", status_code=303)
