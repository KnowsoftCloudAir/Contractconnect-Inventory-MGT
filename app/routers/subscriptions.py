"""Member subscriptions: GA sets prices; members request; GA confirms; auto-expire."""
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import secrets
import string

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import get_session
from app.models import (
    User, UserRole, ChurchMember, SubscriptionSettings, MemberSubscription
)
from app.auth import require_user, require_roles, role_val

router = APIRouter(tags=["subscriptions"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _settings(session: Session) -> SubscriptionSettings:
    s = session.exec(select(SubscriptionSettings).where(SubscriptionSettings.is_active == True)).first()
    if not s:
        s = SubscriptionSettings(
            title="Churchgate member subscription",
            currency="NGN",
            monthly_price=1500.0,
            annual_price=15000.0,
            instructions="Transfer to the account below. Use your email as payment narration, then submit this form. General Admin will confirm.",
            bank_name="GTBank",
            account_name="Knowsoft Technologies",
            account_number="0123456789",
            is_active=True,
        )
        session.add(s)
        session.commit()
        session.refresh(s)
    return s


def expire_due_subscriptions(session: Session) -> int:
    """Deactivate membership when subscription ends."""
    now = datetime.utcnow()
    n = 0
    active = list(session.exec(
        select(MemberSubscription).where(MemberSubscription.status == "active")
    ).all())
    for sub in active:
        if sub.ends_at and sub.ends_at <= now:
            sub.status = "expired"
            session.add(sub)
            # Deactivate member approval
            user = session.get(User, sub.user_id)
            if user and user.member_id:
                mem = session.get(ChurchMember, user.member_id)
                if mem and mem.approval_status == "approved":
                    # only if no other active sub
                    other = session.exec(
                        select(MemberSubscription).where(
                            MemberSubscription.user_id == user.id,
                            MemberSubscription.status == "active",
                            MemberSubscription.id != sub.id,
                        )
                    ).first()
                    if not other:
                        mem.approval_status = "deactivated"
                        session.add(mem)
            n += 1
    if n:
        session.commit()
    return n


def check_sample_member(session: Session, user: User) -> dict:
    """Time-bound sample: 2h use + 5 min warning, then deactivate."""
    info = {
        "is_sample": bool(getattr(user, "is_sample_account", False)),
        "show_warning": False,
        "expired": False,
        "minutes_left": None,
        "message": None,
    }
    if not info["is_sample"]:
        return info
    now = datetime.utcnow()
    started = getattr(user, "sample_started_at", None)
    if not started:
        user.sample_started_at = now
        session.add(user)
        session.commit()
        started = now
        session.refresh(user)
    elapsed = (now - started).total_seconds()
    # 2 hours = 7200; warning from 7200; hard stop 7200+300=7500
    if elapsed >= 7500:
        info["expired"] = True
        info["message"] = "Sample access has expired. Please register as a full member."
        user.is_active = False
        if user.member_id:
            mem = session.get(ChurchMember, user.member_id)
            if mem:
                mem.approval_status = "deactivated"
                session.add(mem)
        session.add(user)
        session.commit()
        return info
    if elapsed >= 7200:
        left = max(0, int((7500 - elapsed) / 60))
        info["show_warning"] = True
        info["minutes_left"] = left
        info["message"] = (
            "Thank you for using sample member. Note this will expire in "
            f"{left} minute(s). Kindly register as a member to enjoy perpetual membership."
        )
    return info


@router.get("/member/subscription", response_class=HTMLResponse)
async def member_subscription_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    expire_due_subscriptions(session)
    settings = _settings(session)
    subs = list(session.exec(
        select(MemberSubscription).where(MemberSubscription.user_id == user.id)
        .order_by(MemberSubscription.created_at.desc())
    ).all())
    active = next((s for s in subs if s.status == "active"), None)
    sample = check_sample_member(session, user)
    return templates.TemplateResponse("subscriptions/member.html", {
        "request": request, "user": user, "settings": settings,
        "subs": subs, "active": active, "sample": sample,
    })


@router.post("/member/subscription/request")
async def member_subscription_request(
    plan: str = Form("monthly"),
    custom_days: int = Form(30),
    payment_reference: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    settings = _settings(session)
    plan = (plan or "monthly").lower()
    if plan == "monthly":
        amount, days = settings.monthly_price, 30
    elif plan == "annual":
        amount, days = settings.annual_price, 365
    else:
        days = max(settings.custom_min_days or 7, int(custom_days or 30))
        # pro-rate from monthly
        amount = round(settings.monthly_price * (days / 30.0), 2)
        plan = "custom"
    ref = payment_reference.strip() or (
        "SUB-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    )
    sub = MemberSubscription(
        user_id=user.id,
        member_id=user.member_id,
        plan=plan,
        amount=amount,
        currency=settings.currency or "NGN",
        duration_days=days,
        status="pending",
        payment_reference=ref,
    )
    session.add(sub)
    session.commit()
    return RedirectResponse("/member/subscription", status_code=303)


@router.get("/admin/subscriptions", response_class=HTMLResponse)
async def admin_subscriptions_page(
    request: Request,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    expire_due_subscriptions(session)
    settings = _settings(session)
    rows = list(session.exec(
        select(MemberSubscription).order_by(MemberSubscription.created_at.desc()).limit(150)
    ).all())
    data = []
    for s in rows:
        u = session.get(User, s.user_id)
        data.append({"s": s, "user": u})
    return templates.TemplateResponse("subscriptions/admin.html", {
        "request": request, "user": user, "settings": settings, "rows": data,
    })


@router.post("/admin/subscriptions/settings")
async def admin_subscription_settings(
    title: str = Form(...),
    currency: str = Form("NGN"),
    monthly_price: float = Form(1500),
    annual_price: float = Form(15000),
    custom_min_days: int = Form(7),
    instructions: str = Form(""),
    bank_name: str = Form(""),
    account_name: str = Form(""),
    account_number: str = Form(""),
    other_details: str = Form(""),
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    s = _settings(session)
    s.title = title.strip()
    s.currency = currency.strip() or "NGN"
    s.monthly_price = float(monthly_price)
    s.annual_price = float(annual_price)
    s.custom_min_days = int(custom_min_days or 7)
    s.instructions = instructions.strip() or None
    s.bank_name = bank_name.strip() or None
    s.account_name = account_name.strip() or None
    s.account_number = account_number.strip() or None
    s.other_details = other_details.strip() or None
    s.updated_at = datetime.utcnow()
    session.add(s)
    session.commit()
    return RedirectResponse("/admin/subscriptions", status_code=303)


@router.post("/admin/subscriptions/{sub_id}/confirm")
async def admin_confirm_subscription(
    sub_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    sub = session.get(MemberSubscription, sub_id)
    if not sub:
        raise HTTPException(404)
    now = datetime.utcnow()
    sub.status = "active"
    sub.starts_at = now
    sub.ends_at = now + timedelta(days=sub.duration_days or 30)
    sub.confirmed_at = now
    sub.confirmed_by = user.id
    session.add(sub)
    # Reactivate member
    u = session.get(User, sub.user_id)
    if u:
        u.is_active = True
        session.add(u)
        if u.member_id:
            mem = session.get(ChurchMember, u.member_id)
            if mem:
                mem.approval_status = "approved"
                session.add(mem)
    session.commit()
    return RedirectResponse("/admin/subscriptions", status_code=303)


@router.post("/admin/subscriptions/{sub_id}/reject")
async def admin_reject_subscription(
    sub_id: int,
    user: User = Depends(require_roles(UserRole.general_admin)),
    session: Session = Depends(get_session),
):
    sub = session.get(MemberSubscription, sub_id)
    if not sub:
        raise HTTPException(404)
    sub.status = "rejected"
    session.add(sub)
    session.commit()
    return RedirectResponse("/admin/subscriptions", status_code=303)
