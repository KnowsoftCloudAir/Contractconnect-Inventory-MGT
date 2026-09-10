"""
CONTRAconnect - Contraceptive Commodity & Cost Analytics Platform
Flask MVP focused on inventory + client encounters + cost allocation
"""

import os
from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, flash, request,
    jsonify, abort, session, send_file
)
from io import BytesIO
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
import tempfile
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
except Exception:  # pragma: no cover
    matplotlib = None
    plt = None
try:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
except Exception:  # pragma: no cover
    Presentation = None
try:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak
    from reportlab.lib.units import inch
except Exception:  # pragma: no cover
    pass
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, case, and_, or_
from sqlalchemy.orm import joinedload

# ---------------------------------------------------------------------------
# App Config
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'contraconnect-dev-secret-change-me')

# Database URL (Render Postgres uses postgres:// — SQLAlchemy needs postgresql://)
_db_url = os.environ.get('DATABASE_URL', 'sqlite:////tmp/contraconnect.db')
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024  # 8MB uploads
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

try:
    os.makedirs(app.instance_path, exist_ok=True)
except Exception:
    pass

db = SQLAlchemy(app)
login_manager = LoginManager(app)

@app.context_processor
def inject_branding():
    try:
        b = report_branding()
    except Exception:
        b = {'app_name': 'CONTRAconnect', 'app_logo': ''}
    return {'app_brand': b}


login_manager.login_view = 'login'
login_manager.login_message_category = 'warning'

# ---------------------------------------------------------------------------
# Password policy
# ---------------------------------------------------------------------------
import re

def validate_password_strength(password, min_length=8, require_strong=False):
    """Return (ok: bool, message: str). require_strong=True for admin-level."""
    if not password or len(password) < min_length:
        return False, f'Password must be at least {min_length} characters.'
    if require_strong:
        if not re.search(r'[A-Z]', password):
            return False, 'Password must include at least one uppercase letter.'
        if not re.search(r'[a-z]', password):
            return False, 'Password must include at least one lowercase letter.'
        if not re.search(r'[0-9]', password):
            return False, 'Password must include at least one number.'
        if not re.search(r'[^A-Za-z0-9]', password):
            return False, 'Password must include at least one special character.'
        if len(password) < 10:
            return False, 'Admin password must be at least 10 characters.'
    return True, 'OK'



# Create tables (and seed once) under Gunicorn / Render — safe to call repeatedly
_db_ready = False

def ensure_db():
    global _db_ready
    if _db_ready:
        return
    try:
        db.create_all()
        if not User.query.filter(User.role.in_(['general_admin', 'admin'])).first():
            seed_data()
        ensure_expense_codes()
        _db_ready = True
    except Exception as e:
        app.logger.exception('ensure_db failed: %s', e)


def log_activity(action, detail=None, user=None):
    try:
        u = user or (current_user if current_user.is_authenticated else None)
        entry = ActivityLog(
            user_id=u.id if u and getattr(u, 'id', None) else None,
            email=getattr(u, 'email', None) if u else None,
            action=action,
            detail=(detail or '')[:255],
            path=(request.path or '')[:255],
            method=request.method,
            ip_address=request.headers.get('X-Forwarded-For', request.remote_addr or '')[:64],
            user_agent=(request.headers.get('User-Agent') or '')[:255],
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


@app.before_request
def _before_request_init_db():
    ensure_db()
    # Skip static and auth endpoints for onboarding gate
    if request.endpoint in (
        'static', 'login', 'logout', 'register', 'admin_access',
        'forgot_password', 'reset_password', 'onboarding', None,
        'assetlinks', 'web_manifest', 'index', 'about',
    ):
        return
    if current_user.is_authenticated:
        # Force onboarding completion / wait for approval
        status = getattr(current_user, 'onboarding_status', 'active') or 'active'
        if status in ('pending_profile', 'pending_approval', 'rejected') and request.endpoint != 'onboarding':
            return redirect(url_for('onboarding'))
        # Lightweight page-view footprint (skip noisy endpoints)
        # Log significant GET destinations only (avoid flooding)
        if request.method == 'GET' and request.endpoint in (
            'admin_dashboard', 'admin_costs', 'admin_facilities', 'export_financial_excel',
            'export_full_excel', 'admin_period_report', 'invoice_list', 'admin_activity',
            'admin_backup_download', 'staff_dashboard', 'provider_dashboard',
        ):
            try:
                log_activity('page_view', request.endpoint)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(40), nullable=False, default='provider')
    # Roles: project_manager | finance_analyst | rh_consultant | mel_consultant |
    #        demand_consultant | sdoc_consultant | logistics_consultant |
    #        provider | general_admin | program_admin | finance_admin (legacy)
    staff_title = db.Column(db.String(120))  # e.g. Project Manager NPSA 10
    facility_id = db.Column(db.Integer, db.ForeignKey('facilities.id'), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    # First-login onboarding
    must_complete_onboarding = db.Column(db.Boolean, default=False)
    onboarding_status = db.Column(db.String(30), default='active')  # pending_profile | pending_approval | active | rejected
    activation_code = db.Column(db.String(40))  # issued by admin
    profile_photo = db.Column(db.String(255))
    phone = db.Column(db.String(40))
    organization = db.Column(db.String(150))
    role_confirmed = db.Column(db.Boolean, default=False)
    ethics_accepted = db.Column(db.Boolean, default=False)
    ethics_accepted_at = db.Column(db.DateTime)
    onboarding_notes = db.Column(db.Text)
    last_login_at = db.Column(db.DateTime)
    password_changed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    facility = db.relationship('Facility', back_populates='users')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def role_label(self):
        labels = {
            'project_manager': 'Project Manager',
            'finance_analyst': 'Financial Analyst',
            'rh_consultant': 'RH / Service Delivery Lead',
            'mel_consultant': 'MEL Consultant',
            'demand_consultant': 'Demand Generation Consultant',
            'sdoc_consultant': 'Service Delivery Ops Consultant',
            'logistics_consultant': 'Logistics & Supply Consultant',
            'provider': 'Service Provider',
            'general_admin': 'General Admin',
            'program_admin': 'Program Admin',
            'finance_admin': 'Finance Admin',
            'admin': 'Admin',
        }
        return labels.get(self.role, self.role)


class Facility(db.Model):
    __tablename__ = 'facilities'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    facility_type = db.Column(db.String(50), nullable=False)  # kiosk | phc | other
    address = db.Column(db.String(255))
    city = db.Column(db.String(80), default='Pilot City')
    contact_person = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    target_clients_monthly = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    users = db.relationship('User', back_populates='facility')
    stock_items = db.relationship('StockItem', back_populates='facility', cascade='all, delete-orphan')
    transactions = db.relationship('StockTransaction', back_populates='facility')
    encounters = db.relationship('ClientEncounter', back_populates='facility')
    expenditures = db.relationship('Expenditure', back_populates='facility')
    requests = db.relationship('ReplenishmentRequest', back_populates='facility')
    messages = db.relationship('Message', back_populates='facility', cascade='all, delete-orphan')


class Product(db.Model):
    """Master product catalogue controlled by Admin"""
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    method_code = db.Column(db.String(40), nullable=False)  # e.g. IUD, Implant, Pill, Injectable, Condom
    unit = db.Column(db.String(30), default='piece')
    unit_cost = db.Column(db.Numeric(12, 2), nullable=False, default=0)  # admin-defined cost
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class StockItem(db.Model):
    """Current stock balance per facility per product"""
    __tablename__ = 'stock_items'
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey('facilities.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    quantity_on_hand = db.Column(db.Integer, default=0)
    reorder_level = db.Column(db.Integer, default=10)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    facility = db.relationship('Facility', back_populates='stock_items')
    product = db.relationship('Product')

    __table_args__ = (db.UniqueConstraint('facility_id', 'product_id', name='uq_facility_product'),)


class StockTransaction(db.Model):
    """Immutable ledger of every stock movement"""
    __tablename__ = 'stock_transactions'
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey('facilities.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    transaction_type = db.Column(db.String(20), nullable=False)  # receipt | issue | adjustment | transfer
    quantity = db.Column(db.Integer, nullable=False)  # positive for receipt, negative for issue
    unit_cost = db.Column(db.Numeric(12, 2))
    reference = db.Column(db.String(100))  # batch, encounter_id, request_id etc.
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    facility = db.relationship('Facility', back_populates='transactions')
    product = db.relationship('Product')
    creator = db.relationship('User')


class ReplenishmentRequest(db.Model):
    __tablename__ = 'replenishment_requests'
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey('facilities.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    quantity_requested = db.Column(db.Integer, nullable=False)
    unit_cost = db.Column(db.Numeric(12, 2))  # snapshot of admin cost at request time
    justification = db.Column(db.Text)
    status = db.Column(db.String(20), default='pending')  # pending | approved | rejected | dispatched
    admin_notes = db.Column(db.Text)
    requested_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)

    facility = db.relationship('Facility', back_populates='requests')
    product = db.relationship('Product')
    requester = db.relationship('User', foreign_keys=[requested_by])
    reviewer = db.relationship('User', foreign_keys=[reviewed_by])


class ClientEncounter(db.Model):
    """Captures method uptake, demographics, refusal / discontinuation reasons"""
    __tablename__ = 'client_encounters'
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey('facilities.id'), nullable=False)
    encounter_date = db.Column(db.Date, default=datetime.utcnow().date)
    client_age_band = db.Column(db.String(20))  # <20, 20-24, 25-29, 30-34, 35+
    client_parity = db.Column(db.String(20))
    education_level = db.Column(db.String(40))
    method_offered = db.Column(db.String(60))
    method_accepted = db.Column(db.String(60))  # null if refused
    outcome = db.Column(db.String(30), nullable=False)  # accepted | refused | discontinued | counselled_only
    refusal_reason = db.Column(db.String(100))  # coded
    discontinuation_reason = db.Column(db.String(100))
    counseling_notes = db.Column(db.Text)
    observation_checklist = db.Column(db.Text)  # JSON or simple text for MVP
    # Client identity & feedback (care-seeking panel)
    client_code = db.Column(db.String(40))  # anonymous/site code — not full name by default
    client_sex = db.Column(db.String(20))
    client_residence = db.Column(db.String(120))
    client_phone = db.Column(db.String(40))  # optional; handle per privacy policy
    consent_to_share_story = db.Column(db.Boolean, default=False)
    patient_testimony = db.Column(db.Text)  # feedback / story for reports
    satisfaction_score = db.Column(db.Integer)  # 1-5
    quantity_dispensed = db.Column(db.Integer, default=0)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    facility = db.relationship('Facility', back_populates='encounters')
    product = db.relationship('Product')
    creator = db.relationship('User')


class Expenditure(db.Model):
    """Digital accounting ledger entries"""
    __tablename__ = 'expenditures'
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey('facilities.id'), nullable=True)  # null = platform-level
    category = db.Column(db.String(80), nullable=False)  # commodity | logistics | staff | utilities | platform_build | other
    description = db.Column(db.String(255))
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    expenditure_date = db.Column(db.Date, default=datetime.utcnow().date)
    is_platform_cost = db.Column(db.Boolean, default=False)  # track platform build separately
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    facility = db.relationship('Facility', back_populates='expenditures')
    creator = db.relationship('User')


class Message(db.Model):
    """Two-way messaging between Admin and a facility / provider"""
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    facility_id = db.Column(db.Integer, db.ForeignKey('facilities.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('messages.id'), nullable=True)  # reply thread
    subject = db.Column(db.String(200))
    body = db.Column(db.Text, nullable=False)
    is_from_admin = db.Column(db.Boolean, default=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    facility = db.relationship('Facility', back_populates='messages')
    sender = db.relationship('User', foreign_keys=[sender_id])
    parent = db.relationship('Message', remote_side=[id], backref='replies')


class Invoice(db.Model):
    """Consultant payment request: submit → Finance review → Project Manager approve."""
    __tablename__ = 'invoices'
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(40), unique=True, nullable=False)
    submitter_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    # Payment details
    payee_name = db.Column(db.String(150), nullable=False)
    bank_name = db.Column(db.String(120))
    account_number = db.Column(db.String(60))
    account_name = db.Column(db.String(150))
    # Deliverable / claim
    deliverable_title = db.Column(db.String(255), nullable=False)
    deliverable_description = db.Column(db.Text)
    period_start = db.Column(db.Date)
    period_end = db.Column(db.Date)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    currency = db.Column(db.String(10), default='USD')
    evidence_notes = db.Column(db.Text)  # links/descriptions of evidence
    evidence_filename = db.Column(db.String(255))  # optional uploaded file name
    # Workflow
    status = db.Column(db.String(40), default='draft')
    # draft | submitted | finance_review | finance_rejected | pm_approved | pm_rejected | paid
    finance_reviewer_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    finance_reviewed_at = db.Column(db.DateTime)
    finance_notes = db.Column(db.Text)
    pm_approver_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    pm_approved_at = db.Column(db.DateTime)
    pm_notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    submitter = db.relationship('User', foreign_keys=[submitter_id])
    finance_reviewer = db.relationship('User', foreign_keys=[finance_reviewer_id])
    pm_approver = db.relationship('User', foreign_keys=[pm_approver_id])





class ActivityLog(db.Model):
    """User footprint / activity monitor."""
    __tablename__ = 'activity_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    email = db.Column(db.String(120))
    action = db.Column(db.String(80), nullable=False)  # login, logout, page_view, create, update, export, etc.
    detail = db.Column(db.String(255))
    path = db.Column(db.String(255))
    method = db.Column(db.String(10))
    ip_address = db.Column(db.String(64))
    user_agent = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', foreign_keys=[user_id])


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User')



class ExpenseCode(db.Model):
    """Chart of accounts / expense codes for project cost claims."""
    __tablename__ = 'expense_codes'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    category = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)

    budget_lines = db.relationship('BudgetLine', back_populates='expense_code')
    expense_requests = db.relationship('ExpenseRequest', back_populates='expense_code')


class BudgetLine(db.Model):
    """Budget allocation per expense code for a fiscal period."""
    __tablename__ = 'budget_lines'
    id = db.Column(db.Integer, primary_key=True)
    expense_code_id = db.Column(db.Integer, db.ForeignKey('expense_codes.id'), nullable=False)
    fiscal_year = db.Column(db.String(20), default='2026')
    period_label = db.Column(db.String(40), default='Pilot')  # Pilot / Scale-up / Annual
    budget_amount = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    variance_narration = db.Column(db.Text)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    expense_code = db.relationship('ExpenseCode', back_populates='budget_lines')


class ExpenseRequest(db.Model):
    """Project cost claim: submit → finance review → PM approve → finance pay."""
    __tablename__ = 'expense_requests'
    id = db.Column(db.Integer, primary_key=True)
    request_number = db.Column(db.String(40), unique=True, nullable=False)
    requester_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    expense_code_id = db.Column(db.Integer, db.ForeignKey('expense_codes.id'), nullable=False)
    # Auto from code
    category = db.Column(db.String(80), nullable=False)
    description = db.Column(db.Text, nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    currency = db.Column(db.String(10), default='USD')
    payee_name = db.Column(db.String(150), nullable=False)
    bank_name = db.Column(db.String(120))
    account_number = db.Column(db.String(60))
    account_name = db.Column(db.String(150))
    evidence_notes = db.Column(db.Text)
    evidence_filename = db.Column(db.String(255))
    # Workflow: draft | submitted | finance_review | finance_rejected | pm_approved | pm_rejected | paid
    status = db.Column(db.String(40), default='draft')
    finance_reviewer_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    finance_reviewed_at = db.Column(db.DateTime)
    finance_notes = db.Column(db.Text)
    pm_approver_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    pm_approved_at = db.Column(db.DateTime)
    pm_notes = db.Column(db.Text)
    paid_by_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    paid_at = db.Column(db.DateTime)
    voucher_number = db.Column(db.String(40))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    requester = db.relationship('User', foreign_keys=[requester_id])
    expense_code = db.relationship('ExpenseCode', back_populates='expense_requests')
    finance_reviewer = db.relationship('User', foreign_keys=[finance_reviewer_id])
    pm_approver = db.relationship('User', foreign_keys=[pm_approver_id])
    paid_by = db.relationship('User', foreign_keys=[paid_by_id])


class AppSetting(db.Model):
    """Key-value settings including report branding and logos."""
    __tablename__ = 'app_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def get_setting(key, default=''):
    row = AppSetting.query.filter_by(key=key).first()
    return row.value if row and row.value is not None else default


def set_setting(key, value):
    row = AppSetting.query.filter_by(key=key).first()
    if not row:
        row = AppSetting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()
    return row


def report_branding():
    """Defaults for Benin City Mayor Challenge + editable logo path."""
    return {
        'programme_title': get_setting('programme_title', 'Benin City Mayor Challenge'),
        'report_subtitle': get_setting('report_subtitle', 'CONTRAconnect — UNDP Supported Programme'),
        'logo_path': get_setting('report_logo_path', ''),  # relative under static/
        'org_line': get_setting('org_line', 'United Nations Development Programme (UNDP)'),
        'app_name': get_setting('app_name', 'CONTRAconnect'),
        'app_logo': get_setting('app_logo_path', ''),
    }


def amount_in_words(amount):
    """Simple English words for money amounts (USD/NGN style)."""
    try:
        amount = float(amount)
    except Exception:
        return str(amount)
    ones = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine',
            'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen',
            'Seventeen', 'Eighteen', 'Nineteen']
    tens = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']

    def under_1000(n):
        n = int(n)
        if n < 20:
            return ones[n]
        if n < 100:
            return (tens[n // 10] + (' ' + ones[n % 10] if n % 10 else '')).strip()
        return (ones[n // 100] + ' Hundred' + (' and ' + under_1000(n % 100) if n % 100 else '')).strip()

    whole = int(amount)
    cents = int(round((amount - whole) * 100))
    if whole == 0:
        words = 'Zero'
    elif whole < 1000:
        words = under_1000(whole)
    elif whole < 1000000:
        words = under_1000(whole // 1000) + ' Thousand' + (' ' + under_1000(whole % 1000) if whole % 1000 else '')
    else:
        words = under_1000(whole // 1000000) + ' Million' + (
            ' ' + under_1000((whole % 1000000) // 1000) + ' Thousand' if (whole % 1000000) // 1000 else ''
        ) + (' ' + under_1000(whole % 1000) if whole % 1000 else '')
    words = words.strip() + ' only'
    if cents:
        words = words.replace(' only', f' and {cents:02d}/100 only')
    return words


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# Role hierarchy — programme staff + legacy admin roles
STAFF_ROLES = (
    'project_manager', 'finance_analyst',
    'rh_consultant', 'mel_consultant', 'demand_consultant',
    'sdoc_consultant', 'logistics_consultant',
)
ADMIN_ROLES = ('general_admin', 'program_admin', 'finance_admin', 'admin', 'project_manager', 'finance_analyst')
FINANCE_ROLES = ('finance_analyst', 'finance_admin', 'general_admin', 'admin')
# Ops / programme (everything except pure finance tools for PM)
PROGRAM_OPS_ROLES = (
    'project_manager', 'program_admin', 'general_admin', 'admin',
    'rh_consultant', 'mel_consultant', 'demand_consultant',
    'sdoc_consultant', 'logistics_consultant',
)
CONSULTANT_ROLES = (
    'rh_consultant', 'mel_consultant', 'demand_consultant',
    'sdoc_consultant', 'logistics_consultant',
)


def _is_admin_role(role):
    return role in ADMIN_ROLES or role in STAFF_ROLES


def _can_export_financial(role):
    # Project Manager: no finance downloads; Finance Analyst + system admins: yes
    return role in FINANCE_ROLES


def _can_manage_admins(role):
    return role in ('general_admin', 'admin', 'project_manager')


def _can_review_invoices_finance(role):
    return role in ('finance_analyst', 'finance_admin', 'general_admin', 'admin')


def _can_approve_invoices_pm(role):
    return role in ('project_manager', 'program_admin', 'general_admin', 'admin')


def _can_submit_invoice(role):
    return role in CONSULTANT_ROLES or role in ('project_manager', 'finance_analyst', 'general_admin', 'admin')


def _home_for_role(role):
    if role in ('finance_analyst', 'finance_admin'):
        return 'admin_costs'
    if role == 'provider':
        return 'provider_dashboard'
    if role in STAFF_ROLES or role in ADMIN_ROLES:
        return 'staff_dashboard'
    return 'index'


def admin_required(f):
    """Programme staff or admin (not pure provider)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not _is_admin_role(current_user.role):
            flash('Staff access required.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def general_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not _can_manage_admins(current_user.role):
            flash('Project Manager / General Admin access required.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def finance_access_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not _can_export_financial(current_user.role):
            flash('Finance access required. Project Manager cannot download financial exports.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def program_ops_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))
        if current_user.role in PROGRAM_OPS_ROLES or current_user.role in ('finance_analyst', 'finance_admin'):
            # Finance analyst can view ops; restricted actions handled in UI
            return f(*args, **kwargs)
        flash('Programme access required.', 'danger')
        return redirect(url_for('login'))
    return decorated


def provider_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Login required.', 'danger')
            return redirect(url_for('login'))
        if current_user.role == 'provider' or _is_admin_role(current_user.role):
            return f(*args, **kwargs)
        flash('Login required.', 'danger')
        return redirect(url_for('login'))
    return decorated


# ---------------------------------------------------------------------------
# Cost Allocation Engine (core of the learning question)
# ---------------------------------------------------------------------------
def compute_cost_metrics(start_date=None, end_date=None):
    """
    Returns unit cost per client, operating cost per kiosk, and breakdowns.
    Platform build costs are tracked separately and excluded from operational unit costs.
    """
    if not end_date:
        end_date = datetime.utcnow().date()
    if not start_date:
        start_date = end_date - timedelta(days=90)

    # Total clients served (accepted encounters with quantity > 0 or outcome accepted)
    clients_q = db.session.query(func.count(ClientEncounter.id)).filter(
        ClientEncounter.encounter_date.between(start_date, end_date),
        ClientEncounter.outcome.in_(['accepted', 'discontinued'])
    )
    total_clients = clients_q.scalar() or 0

    # Operational expenditures (exclude pure platform_build)
    ops_exp = db.session.query(func.coalesce(func.sum(Expenditure.amount), 0)).filter(
        Expenditure.expenditure_date.between(start_date, end_date),
        Expenditure.is_platform_cost == False
    ).scalar() or Decimal('0')

    platform_exp = db.session.query(func.coalesce(func.sum(Expenditure.amount), 0)).filter(
        Expenditure.expenditure_date.between(start_date, end_date),
        Expenditure.is_platform_cost == True
    ).scalar() or Decimal('0')

    unit_cost_per_client = (ops_exp / total_clients) if total_clients > 0 else Decimal('0')

    # Per-facility operating cost
    facilities = Facility.query.filter_by(is_active=True).all()
    per_kiosk = []
    for fac in facilities:
        fac_clients = db.session.query(func.count(ClientEncounter.id)).filter(
            ClientEncounter.facility_id == fac.id,
            ClientEncounter.encounter_date.between(start_date, end_date),
            ClientEncounter.outcome.in_(['accepted', 'discontinued'])
        ).scalar() or 0

        fac_ops = db.session.query(func.coalesce(func.sum(Expenditure.amount), 0)).filter(
            Expenditure.facility_id == fac.id,
            Expenditure.expenditure_date.between(start_date, end_date),
            Expenditure.is_platform_cost == False
        ).scalar() or Decimal('0')

        # Also attribute a share of unallocated (facility_id is null) operational costs
        # Simple equal share for MVP; can be volume-weighted later
        unallocated = db.session.query(func.coalesce(func.sum(Expenditure.amount), 0)).filter(
            Expenditure.facility_id.is_(None),
            Expenditure.expenditure_date.between(start_date, end_date),
            Expenditure.is_platform_cost == False
        ).scalar() or Decimal('0')
        share = unallocated / len(facilities) if facilities else Decimal('0')
        total_fac_ops = fac_ops + share

        per_kiosk.append({
            'facility_id': fac.id,
            'name': fac.name,
            'type': fac.facility_type,
            'clients': fac_clients,
            'operating_cost': float(total_fac_ops),
            'unit_cost': float(total_fac_ops / fac_clients) if fac_clients > 0 else 0.0
        })

    # Method-level cost approximation (using product unit_cost * quantity issued)
    method_stats = db.session.query(
        Product.method_code,
        Product.name,
        func.sum(case((StockTransaction.transaction_type == 'issue', -StockTransaction.quantity), else_=0)).label('qty_issued'),
        func.sum(case((StockTransaction.transaction_type == 'issue', -StockTransaction.quantity * StockTransaction.unit_cost), else_=0)).label('commodity_cost')
    ).join(StockTransaction, StockTransaction.product_id == Product.id).filter(
        StockTransaction.created_at.between(
            datetime.combine(start_date, datetime.min.time()),
            datetime.combine(end_date, datetime.max.time())
        )
    ).group_by(Product.method_code, Product.name).all()

    return {
        'period': {'start': start_date.isoformat(), 'end': end_date.isoformat()},
        'total_clients': total_clients,
        'total_operational_cost': float(ops_exp),
        'platform_build_cost': float(platform_exp),
        'unit_cost_per_client': float(unit_cost_per_client),
        'per_kiosk': per_kiosk,
        'method_stats': [
            {
                'method_code': m.method_code,
                'name': m.name,
                'qty_issued': int(m.qty_issued or 0),
                'commodity_cost': float(m.commodity_cost or 0)
            } for m in method_stats
        ]
    }


def get_method_uptake(start_date=None, end_date=None):
    if not end_date:
        end_date = datetime.utcnow().date()
    if not start_date:
        start_date = end_date - timedelta(days=90)

    rows = db.session.query(
        ClientEncounter.method_accepted,
        func.count(ClientEncounter.id)
    ).filter(
        ClientEncounter.encounter_date.between(start_date, end_date),
        ClientEncounter.outcome == 'accepted',
        ClientEncounter.method_accepted.isnot(None)
    ).group_by(ClientEncounter.method_accepted).all()

    total = sum(r[1] for r in rows) or 1
    return [{'method': r[0], 'count': r[1], 'pct': round(100 * r[1] / total, 1)} for r in rows]


def get_refusal_reasons(start_date=None, end_date=None):
    if not end_date:
        end_date = datetime.utcnow().date()
    if not start_date:
        start_date = end_date - timedelta(days=90)

    rows = db.session.query(
        ClientEncounter.refusal_reason,
        func.count(ClientEncounter.id)
    ).filter(
        ClientEncounter.encounter_date.between(start_date, end_date),
        ClientEncounter.outcome == 'refused',
        ClientEncounter.refusal_reason.isnot(None)
    ).group_by(ClientEncounter.refusal_reason).all()

    return [{'reason': r[0] or 'Unspecified', 'count': r[1]} for r in rows]


# ---------------------------------------------------------------------------
# Routes - Auth
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    """Public marketing home; authenticated users go to their workspace."""
    if current_user.is_authenticated:
        if getattr(current_user, 'onboarding_status', 'active') in ('pending_profile', 'pending_approval', 'rejected'):
            return redirect(url_for('onboarding'))
        if current_user.role == 'provider':
            return redirect(url_for('provider_dashboard'))
        if _is_admin_role(current_user.role):
            return redirect(url_for(_home_for_role(current_user.role)))
    content = homepage_content()
    return render_template('public_home.html', content=content)


@app.route('/about')
def about():
    content = homepage_content()
    return render_template('public_about.html', content=content)


def homepage_content():
    """Editable public homepage fields (defaults for Benin City Mayor Challenge)."""
    import json as _json
    defaults = {
        'hero_title': 'Benin City Mayor Challenge',
        'hero_tagline': 'Expanding equitable contraceptive choice through CONTRAconnect digital innovation',
        'lga_name': 'Winning Local Government Area — Edo State',
        'lga_detail': 'Highlighting the LGA recognised under the Mayor Challenge pathway in Edo State, Nigeria.',
        'origin': (
            'The Bloomberg Philanthropies Mayor’s Challenge invites cities worldwide to propose bold, '
            'evidence-driven ideas that improve urban life. Benin City’s entry focuses on closing gaps in '
            'access to quality family planning through mobile service points, community mobilisation, and '
            'a digital platform — CONTRAconnect — that links supply, service delivery, and learning.'
        ),
        'funder': (
            'Programme support is advanced through partnerships aligned with the Mayor’s Challenge ecosystem '
            'and United Nations Development Programme (UNDP) collaboration with city and national stakeholders, '
            'strengthening local systems rather than parallel structures.'
        ),
        'objectives': (
            'Objectives include: (1) increase informed uptake of modern contraception via mobile kiosks and '
            'parent PHC linkages; (2) prevent stock-outs with predictive inventory and redistribution; '
            '(3) generate real-time learning on method mix, refusal reasons, and unit costs; and '
            '(4) equip city leaders with dashboards for adaptive management.'
        ),
        'benefits': (
            'For Edo State and Nigeria, the model demonstrates how city-led innovation can reduce unmet need, '
            'improve commodity security, create accountable digital footprints for quality of care, and offer a '
            'replicable blueprint for other LGAs — advancing reproductive health, gender equity, and data-driven governance.'
        ),
        'about_body': (
            'CONTRAconnect is the digital backbone of Benin City’s Mayor Challenge family-planning initiative. '
            'It connects kiosks, PHCs, consultants, and city administrators around inventory, encounters, cost '
            'analytics, and approved payment workflows — with privacy and role-based access at the core.'
        ),
        'video_url': 'https://www.youtube.com/embed/dQw4w9WgXcQ',  # placeholder — admin replaces
        'slide1_caption': 'Mobile kiosks bring counselling and methods closer to communities.',
        'slide2_caption': 'Mobilisers and SBC campaigns drive informed demand.',
        'slide3_caption': 'Client feedback shapes continuous quality improvement.',
        'story1': '“The nurse explained every option. I chose a method that fits my life — and I did not wait all day.” — Client, pilot kiosk',
        'story2': '“When stock was low, the app prompted redistribution. We avoided a stock-out before outreach day.” — Logistics team',
        'story3': '“Having cost per client helps us plan with the city finance team honestly.” — Programme officer',
        'slide1_img': 'img/slide1.jpg',
        'slide2_img': 'img/slide2.jpg',
        'slide3_img': 'img/slide3.jpg',
    }
    raw = get_setting('homepage_json', '')
    if raw:
        try:
            data = _json.loads(raw)
            defaults.update({k: v for k, v in data.items() if v is not None and v != ''})
        except Exception:
            pass
    return defaults


def save_homepage_content(data: dict):
    import json as _json
    set_setting('homepage_json', _json.dumps(data, ensure_ascii=False))


@app.route('/admin/homepage', methods=['GET', 'POST'])
@login_required
@general_admin_required
def admin_homepage_editor():
    content = homepage_content()
    if request.method == 'POST':
        fields = [
            'hero_title', 'hero_tagline', 'lga_name', 'lga_detail',
            'origin', 'funder', 'objectives', 'benefits', 'about_body',
            'video_url', 'slide1_caption', 'slide2_caption', 'slide3_caption',
            'story1', 'story2', 'story3',
        ]
        for k in fields:
            content[k] = request.form.get(k, content.get(k, ''))
        # Normalize youtube watch URLs to embed
        v = content.get('video_url') or ''
        if 'youtube.com/watch' in v and 'v=' in v:
            vid = v.split('v=')[1].split('&')[0]
            content['video_url'] = f'https://www.youtube.com/embed/{vid}'
        elif 'youtu.be/' in v:
            vid = v.split('youtu.be/')[1].split('?')[0]
            content['video_url'] = f'https://www.youtube.com/embed/{vid}'
        for i in (1, 2, 3):
            f = request.files.get(f'slide{i}_file')
            if f and f.filename:
                ext = f.filename.rsplit('.', 1)[-1].lower()
                if ext in ('png', 'jpg', 'jpeg', 'webp'):
                    rel = f'uploads/homepage/slide{i}.{ext}'
                    dest = os.path.join(app.root_path, 'static', rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    f.save(dest)
                    content[f'slide{i}_img'] = rel
        save_homepage_content(content)
        log_activity('homepage_updated')
        flash('Homepage content saved. Public visitors will see the updates.', 'success')
        return redirect(url_for('admin_homepage_editor'))
    return render_template('admin_homepage.html', content=content)



@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email, is_active=True).first()
        if user and user.check_password(password):
            if getattr(user, 'onboarding_status', 'active') == 'rejected':
                flash('Your account registration was not approved. Contact the administrator.', 'danger')
                return redirect(url_for('login'))
            login_user(user, remember=True)
            user.last_login_at = datetime.utcnow()
            db.session.commit()
            log_activity('login', f'role={user.role}', user=user)
            flash(f'Welcome, {user.full_name} ({user.role_label}).', 'success')
            if getattr(user, 'onboarding_status', 'active') in ('pending_profile', 'pending_approval'):
                return redirect(url_for('onboarding'))
            return redirect(url_for('index'))
        flash('Invalid email or password.', 'danger')
        log_activity('login_failed', email)
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    if current_user.is_authenticated:
        log_activity('logout')
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


@app.route('/admin-access', methods=['GET', 'POST'])
def admin_access():
    """Confidential admin-only sign-in (not linked from public landing or navbar)."""
    if current_user.is_authenticated:
        if current_user.role == 'admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('provider_dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email, is_active=True).first()
        if user and _is_admin_role(user.role) and user.check_password(password):
            login_user(user, remember=False)
            flash(f'Welcome, {user.full_name} ({user.role_label}).', 'success')
            return redirect(url_for(_home_for_role(user.role)))
        # Generic message — do not reveal whether email exists
        flash('Invalid credentials or unauthorised access.', 'danger')
        return redirect(url_for('admin_access'))
    return render_template('admin_login.html')



@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Allow any authenticated user (admin or provider) to change their password."""
    if request.method == 'POST':
        current_pw = request.form.get('current_password', '')
        new_pw = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')

        if not current_user.check_password(current_pw):
            flash('Current password is incorrect.', 'danger')
            return redirect(url_for('change_password'))

        if new_pw != confirm:
            flash('New password and confirmation do not match.', 'danger')
            return redirect(url_for('change_password'))

        require_strong = _is_admin_role(current_user.role)
        min_len = 10 if require_strong else 8
        ok, msg = validate_password_strength(new_pw, min_length=min_len, require_strong=require_strong)
        if not ok:
            flash(msg, 'danger')
            return redirect(url_for('change_password'))

        if current_user.check_password(new_pw):
            flash('New password must be different from the current password.', 'warning')
            return redirect(url_for('change_password'))

        current_user.set_password(new_pw)
        db.session.commit()
        flash('Password updated successfully.', 'success')
        return redirect(url_for('index'))

    return render_template('change_password.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    """Provider self-registration (pending admin approval via is_active)"""
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        full_name = request.form.get('full_name', '').strip()
        password = request.form.get('password', '')
        facility_name = request.form.get('facility_name', '').strip()
        facility_type = request.form.get('facility_type', 'kiosk')
        phone = request.form.get('phone', '').strip()

        # Basic validation
        if not email or not full_name or not password or not facility_name:
            flash('Please fill in all required fields (Name, Email, Password, Facility).', 'danger')
            return redirect(url_for('register'))
        ok, msg = validate_password_strength(password, min_length=8, require_strong=False)
        if not ok:
            flash(msg, 'danger')
            return redirect(url_for('register'))

        try:
            if User.query.filter_by(email=email).first():
                flash('Email already registered.', 'warning')
                return redirect(url_for('register'))

            # Create facility first
            fac = Facility(
                name=facility_name,
                facility_type=facility_type or 'kiosk',
                contact_person=full_name,
                phone=phone,
                is_active=False  # admin activates later
            )
            db.session.add(fac)
            db.session.flush()

            user = User(
                email=email,
                full_name=full_name,
                role='provider',
                facility_id=fac.id,
                is_active=False  # pending approval
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash('Registration submitted. An administrator will activate your account.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            app.logger.exception('Registration failed: %s', e)
            flash(
                'Registration failed due to a server error. '
                'Please try again or contact the administrator. '
                f'(Hint: check that the database is configured on the host.)',
                'danger'
            )
            return redirect(url_for('register'))
    return render_template('register.html')


# ---------------------------------------------------------------------------
# Admin Routes
# ---------------------------------------------------------------------------
@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    facilities = Facility.query.order_by(Facility.name).all()
    pending_users = User.query.filter_by(is_active=False, role='provider').count()
    pending_requests = ReplenishmentRequest.query.filter_by(status='pending').count()
    total_products = Product.query.filter_by(is_active=True).count()

    cost_data = compute_cost_metrics()
    uptake = get_method_uptake()
    refusals = get_refusal_reasons()

    return render_template(
        'admin_dashboard.html',
        facilities=facilities,
        pending_users=pending_users,
        pending_requests=pending_requests,
        total_products=total_products,
        cost_data=cost_data,
        uptake=uptake,
        refusals=refusals
    )


@app.route('/admin/facilities')
@login_required
@admin_required
def admin_facilities():
    facilities = Facility.query.order_by(Facility.created_at.desc()).all()
    return render_template('admin_facilities.html', facilities=facilities)


@app.route('/admin/facilities/<int:fid>/activate', methods=['POST'])
@login_required
@admin_required
def activate_facility(fid):
    fac = db.session.get(Facility, fid)
    if not fac:
        abort(404)
    fac.is_active = True
    for u in fac.users:
        u.is_active = True
    db.session.commit()
    flash(f'{fac.name} and its users activated.', 'success')
    next_url = request.form.get('next') or url_for('admin_facility_detail', fid=fid)
    return redirect(next_url)


@app.route('/admin/facilities/<int:fid>/deactivate', methods=['POST'])
@login_required
@admin_required
def deactivate_facility(fid):
    """Deactivate facility: providers cannot submit replenishment requests."""
    fac = db.session.get(Facility, fid)
    if not fac:
        abort(404)
    fac.is_active = False
    for u in fac.users:
        u.is_active = False
    db.session.commit()
    flash(f'{fac.name} deactivated. Providers can no longer submit stock requests.', 'warning')
    next_url = request.form.get('next') or url_for('admin_facility_detail', fid=fid)
    return redirect(next_url)


@app.route('/admin/facilities/<int:fid>')
@login_required
@admin_required
def admin_facility_detail(fid):
    """Full facility profile: details, users, read-only stock, messages, password reset."""
    fac = db.session.get(Facility, fid)
    if not fac:
        abort(404)
    stock_items = StockItem.query.filter_by(facility_id=fac.id).options(
        joinedload(StockItem.product)
    ).all()
    recent_tx = StockTransaction.query.filter_by(facility_id=fac.id).order_by(
        StockTransaction.created_at.desc()
    ).limit(20).all()
    open_requests = ReplenishmentRequest.query.filter_by(
        facility_id=fac.id
    ).order_by(ReplenishmentRequest.created_at.desc()).limit(15).all()
    messages = Message.query.filter_by(facility_id=fac.id).order_by(
        Message.created_at.desc()
    ).limit(50).all()
    # mark admin-visible messages from provider as read when admin opens page
    for m in messages:
        if not m.is_from_admin and not m.is_read:
            m.is_read = True
    db.session.commit()
    return render_template(
        'admin_facility_detail.html',
        facility=fac,
        stock_items=stock_items,
        recent_tx=recent_tx,
        open_requests=open_requests,
        messages=messages,
    )


@app.route('/admin/facilities/<int:fid>/reset-password', methods=['POST'])
@login_required
@admin_required
def admin_reset_provider_password(fid):
    fac = db.session.get(Facility, fid)
    if not fac:
        abort(404)
    user_id = request.form.get('user_id')
    new_pw = request.form.get('new_password', '').strip()
    confirm = request.form.get('confirm_password', '').strip()
    user = db.session.get(User, int(user_id)) if user_id else None
    if not user or user.facility_id != fac.id or user.role != 'provider':
        flash('Invalid provider user.', 'danger')
        return redirect(url_for('admin_facility_detail', fid=fid))
    if new_pw != confirm:
        flash('Passwords do not match.', 'danger')
        return redirect(url_for('admin_facility_detail', fid=fid))
    ok, msg = validate_password_strength(new_pw, min_length=8, require_strong=False)
    if not ok:
        flash(msg, 'danger')
        return redirect(url_for('admin_facility_detail', fid=fid))
    user.set_password(new_pw)
    db.session.commit()
    flash(f'Password updated for {user.full_name} ({user.email}). Share it securely with them.', 'success')
    return redirect(url_for('admin_facility_detail', fid=fid))


@app.route('/admin/facilities/<int:fid>/message', methods=['POST'])
@login_required
@admin_required
def admin_send_message(fid):
    fac = db.session.get(Facility, fid)
    if not fac:
        abort(404)
    subject = request.form.get('subject', '').strip() or 'Message from Admin'
    body = request.form.get('body', '').strip()
    if not body:
        flash('Message body is required.', 'danger')
        return redirect(url_for('admin_facility_detail', fid=fid))
    msg = Message(
        facility_id=fac.id,
        sender_id=current_user.id,
        subject=subject,
        body=body,
        is_from_admin=True,
        is_read=False,
    )
    db.session.add(msg)
    db.session.commit()
    flash('Message sent to facility.', 'success')
    return redirect(url_for('admin_facility_detail', fid=fid))


@app.route('/admin/products', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_products():
    if request.method == 'POST':
        p = Product(
            name=request.form['name'],
            method_code=request.form['method_code'],
            unit=request.form.get('unit', 'piece'),
            unit_cost=Decimal(request.form.get('unit_cost', '0')),
            description=request.form.get('description', '')
        )
        db.session.add(p)
        db.session.commit()
        flash('Product added.', 'success')
        return redirect(url_for('admin_products'))
    products = Product.query.order_by(Product.name).all()
    return render_template('admin_products.html', products=products)


@app.route('/admin/stock/dispatch', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_dispatch():
    facilities = Facility.query.filter_by(is_active=True).all()
    products = Product.query.filter_by(is_active=True).all()
    if request.method == 'POST':
        fac_id = int(request.form['facility_id'])
        prod_id = int(request.form['product_id'])
        qty = int(request.form['quantity'])
        notes = request.form.get('notes', '')

        product = db.session.get(Product, prod_id)
        # Update or create stock item
        stock = StockItem.query.filter_by(facility_id=fac_id, product_id=prod_id).first()
        if not stock:
            stock = StockItem(facility_id=fac_id, product_id=prod_id, quantity_on_hand=0)
            db.session.add(stock)
        stock.quantity_on_hand += qty

        tx = StockTransaction(
            facility_id=fac_id,
            product_id=prod_id,
            transaction_type='receipt',
            quantity=qty,
            unit_cost=product.unit_cost,
            reference='ADMIN-DISPATCH',
            notes=notes,
            created_by=current_user.id
        )
        db.session.add(tx)
        db.session.commit()
        flash(f'Dispatched {qty} {product.name} to facility.', 'success')
        return redirect(url_for('admin_dispatch'))
    return render_template('admin_dispatch.html', facilities=facilities, products=products)


@app.route('/admin/requests')
@login_required
@admin_required
def admin_requests():
    requests_list = ReplenishmentRequest.query.order_by(
        ReplenishmentRequest.created_at.desc()
    ).options(joinedload(ReplenishmentRequest.facility), joinedload(ReplenishmentRequest.product)).all()
    return render_template('admin_requests.html', requests=requests_list)


@app.route('/admin/requests/<int:rid>/<action>', methods=['POST'])
@login_required
@admin_required
def admin_request_action(rid, action):
    req = db.session.get(ReplenishmentRequest, rid)
    if not req or action not in ('approve', 'reject', 'dispatch'):
        abort(404)
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.utcnow()
    req.admin_notes = request.form.get('admin_notes', '')

    if action == 'approve':
        req.status = 'approved'
    elif action == 'reject':
        req.status = 'rejected'
    elif action == 'dispatch':
        req.status = 'dispatched'
        # Create receipt transaction
        stock = StockItem.query.filter_by(facility_id=req.facility_id, product_id=req.product_id).first()
        if not stock:
            stock = StockItem(facility_id=req.facility_id, product_id=req.product_id, quantity_on_hand=0)
            db.session.add(stock)
        stock.quantity_on_hand += req.quantity_requested
        tx = StockTransaction(
            facility_id=req.facility_id,
            product_id=req.product_id,
            transaction_type='receipt',
            quantity=req.quantity_requested,
            unit_cost=req.unit_cost,
            reference=f'REQ-{req.id}',
            notes='Auto-receipt from approved request',
            created_by=current_user.id
        )
        db.session.add(tx)
    db.session.commit()
    flash(f'Request {action}d.', 'success')
    return redirect(url_for('admin_requests'))


@app.route('/admin/expenditures', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_expenditures():
    facilities = Facility.query.filter_by(is_active=True).all()
    if request.method == 'POST':
        fac_id = request.form.get('facility_id') or None
        if fac_id:
            fac_id = int(fac_id)
        exp = Expenditure(
            facility_id=fac_id,
            category=request.form['category'],
            description=request.form.get('description', ''),
            amount=Decimal(request.form['amount']),
            expenditure_date=datetime.strptime(request.form['expenditure_date'], '%Y-%m-%d').date(),
            is_platform_cost=bool(request.form.get('is_platform_cost')),
            created_by=current_user.id
        )
        db.session.add(exp)
        db.session.commit()
        flash('Expenditure recorded.', 'success')
        return redirect(url_for('admin_expenditures'))
    expenditures = Expenditure.query.order_by(Expenditure.expenditure_date.desc()).limit(100).all()
    return render_template('admin_expenditures.html', expenditures=expenditures, facilities=facilities)


@app.route('/admin/costs')
@login_required
@admin_required
def admin_costs():
    """Dedicated cost analytics dashboard"""
    days = int(request.args.get('days', 90))
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)
    cost_data = compute_cost_metrics(start, end)
    uptake = get_method_uptake(start, end)
    return render_template('admin_costs.html', cost_data=cost_data, uptake=uptake, days=days)


def _style_header(ws, row=1):
    fill = PatternFill('solid', fgColor='0D6E6E')
    font = Font(bold=True, color='FFFFFF')
    for cell in ws[row]:
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal='center', wrap_text=True)


def _autosize(ws, max_width=40):
    for col in ws.columns:
        length = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                length = max(length, len(str(cell.value or '')))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max(length + 2, 12), max_width)


@app.route('/admin/export/financial')
@login_required
@finance_access_required
def export_financial_excel():
    """Excel report for financial analysis: unit cost, per-kiosk costs, ledgers, method commodity cost."""
    days = int(request.args.get('days', 90))
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)
    cost_data = compute_cost_metrics(start, end)
    uptake = get_method_uptake(start, end)

    wb = Workbook()

    # --- Summary ---
    ws = wb.active
    ws.title = 'Cost Summary'
    ws.append(['CONTRAconnect — Financial Analysis Report'])
    ws.append(['Period start', cost_data['period']['start']])
    ws.append(['Period end', cost_data['period']['end']])
    ws.append([])
    ws.append(['Metric', 'Value'])
    ws.append(['Total clients served', cost_data['total_clients']])
    ws.append(['Total operational cost', cost_data['total_operational_cost']])
    ws.append(['Platform build cost (separate)', cost_data['platform_build_cost']])
    ws.append(['Unit cost per client (ops ÷ clients)', cost_data['unit_cost_per_client']])
    ws['A1'].font = Font(bold=True, size=14, color='0D6E6E')
    _autosize(ws)

    # --- Per facility ---
    ws2 = wb.create_sheet('Cost by Facility')
    ws2.append(['Facility', 'Type', 'Clients served', 'Operating cost', 'Unit cost per client', 'Efficiency note'])
    _style_header(ws2)
    avg = cost_data['unit_cost_per_client'] or 0
    for k in cost_data['per_kiosk']:
        note = 'No activity'
        if k['clients'] > 0:
            if avg and k['unit_cost'] > avg * 1.3:
                note = 'Above average — review'
            elif avg and k['unit_cost'] < avg * 0.7:
                note = 'Efficient'
            else:
                note = 'On track'
        ws2.append([k['name'], k['type'], k['clients'], k['operating_cost'], k['unit_cost'], note])
    _autosize(ws2)

    # --- Method commodity ---
    ws3 = wb.create_sheet('Commodity by Method')
    ws3.append(['Method code', 'Product', 'Qty issued', 'Commodity cost'])
    _style_header(ws3)
    for m in cost_data['method_stats']:
        ws3.append([m['method_code'], m['name'], m['qty_issued'], m['commodity_cost']])
    _autosize(ws3)

    # --- Method uptake ---
    ws4 = wb.create_sheet('Method Uptake')
    ws4.append(['Method', 'Count', 'Percent'])
    _style_header(ws4)
    for u in uptake:
        ws4.append([u['method'], u['count'], u['pct']])
    _autosize(ws4)

    # --- Ledger detail ---
    ws5 = wb.create_sheet('Expenditure Ledger')
    ws5.append(['Date', 'Facility', 'Category', 'Description', 'Amount', 'Platform cost?'])
    _style_header(ws5)
    q = Expenditure.query.filter(
        Expenditure.expenditure_date.between(start, end)
    ).order_by(Expenditure.expenditure_date.desc()).all()
    for e in q:
        ws5.append([
            e.expenditure_date.isoformat() if e.expenditure_date else '',
            e.facility.name if e.facility else 'Central',
            e.category,
            e.description or '',
            float(e.amount or 0),
            'Yes' if e.is_platform_cost else 'No',
        ])
    _autosize(ws5)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f'CONTRAconnect_Financial_{start.isoformat()}_to_{end.isoformat()}.xlsx'
    return send_file(
        buf,
        as_attachment=True,
        download_name=fname,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


@app.route('/admin/export/full')
@login_required
@admin_required
def export_full_excel():
    """Complete operational data dump: facilities, users, stock, transactions, encounters, requests, messages, expenditures."""
    if current_user.role == 'finance_admin':
        return redirect(url_for('export_financial_excel'))
    wb = Workbook()

    # Facilities
    ws = wb.active
    ws.title = 'Facilities'
    ws.append(['ID', 'Name', 'Type', 'Address', 'City', 'Contact', 'Phone', 'Target clients/mo', 'Active', 'Created'])
    _style_header(ws)
    for f in Facility.query.order_by(Facility.id).all():
        ws.append([
            f.id, f.name, f.facility_type, f.address or '', f.city or '',
            f.contact_person or '', f.phone or '', f.target_clients_monthly,
            'Yes' if f.is_active else 'No',
            f.created_at.strftime('%Y-%m-%d %H:%M') if f.created_at else '',
        ])
    _autosize(ws)

    # Users
    ws = wb.create_sheet('Users')
    ws.append(['ID', 'Email', 'Full name', 'Role', 'Facility ID', 'Facility name', 'Active', 'Created'])
    _style_header(ws)
    for u in User.query.order_by(User.id).all():
        ws.append([
            u.id, u.email, u.full_name, u.role, u.facility_id,
            u.facility.name if u.facility else '',
            'Yes' if u.is_active else 'No',
            u.created_at.strftime('%Y-%m-%d %H:%M') if u.created_at else '',
        ])
    _autosize(ws)

    # Products
    ws = wb.create_sheet('Products')
    ws.append(['ID', 'Name', 'Method code', 'Unit', 'Unit cost', 'Active'])
    _style_header(ws)
    for p in Product.query.order_by(Product.id).all():
        ws.append([p.id, p.name, p.method_code, p.unit, float(p.unit_cost or 0), 'Yes' if p.is_active else 'No'])
    _autosize(ws)

    # Stock balances
    ws = wb.create_sheet('Stock Balances')
    ws.append(['Facility', 'Product', 'Method', 'On hand', 'Reorder level', 'Last updated'])
    _style_header(ws)
    for s in StockItem.query.options(joinedload(StockItem.facility), joinedload(StockItem.product)).all():
        ws.append([
            s.facility.name if s.facility else '',
            s.product.name if s.product else '',
            s.product.method_code if s.product else '',
            s.quantity_on_hand,
            s.reorder_level,
            s.last_updated.strftime('%Y-%m-%d %H:%M') if s.last_updated else '',
        ])
    _autosize(ws)

    # Stock transactions
    ws = wb.create_sheet('Stock Transactions')
    ws.append(['ID', 'Date', 'Facility', 'Product', 'Type', 'Qty', 'Unit cost', 'Reference', 'Notes'])
    _style_header(ws)
    for t in StockTransaction.query.order_by(StockTransaction.created_at.desc()).limit(5000).all():
        ws.append([
            t.id,
            t.created_at.strftime('%Y-%m-%d %H:%M') if t.created_at else '',
            t.facility.name if t.facility else '',
            t.product.name if t.product else '',
            t.transaction_type,
            t.quantity,
            float(t.unit_cost or 0),
            t.reference or '',
            t.notes or '',
        ])
    _autosize(ws)

    # Encounters
    ws = wb.create_sheet('Client Encounters')
    ws.append([
        'ID', 'Date', 'Facility', 'Age band', 'Parity', 'Education',
        'Method offered', 'Method accepted', 'Outcome', 'Refusal reason',
        'Discontinuation reason', 'Qty dispensed', 'Product',
    ])
    _style_header(ws)
    for e in ClientEncounter.query.order_by(ClientEncounter.encounter_date.desc()).limit(10000).all():
        ws.append([
            e.id,
            e.encounter_date.isoformat() if e.encounter_date else '',
            e.facility.name if e.facility else '',
            e.client_age_band or '',
            e.client_parity or '',
            e.education_level or '',
            e.method_offered or '',
            e.method_accepted or '',
            e.outcome or '',
            e.refusal_reason or '',
            e.discontinuation_reason or '',
            e.quantity_dispensed or 0,
            e.product.name if e.product else '',
        ])
    _autosize(ws)

    # Requests
    ws = wb.create_sheet('Replenishment Requests')
    ws.append(['ID', 'Facility', 'Product', 'Qty', 'Unit cost', 'Status', 'Justification', 'Created'])
    _style_header(ws)
    for r in ReplenishmentRequest.query.order_by(ReplenishmentRequest.created_at.desc()).all():
        ws.append([
            r.id,
            r.facility.name if r.facility else '',
            r.product.name if r.product else '',
            r.quantity_requested,
            float(r.unit_cost or 0),
            r.status,
            r.justification or '',
            r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else '',
        ])
    _autosize(ws)

    # Expenditures
    ws = wb.create_sheet('Expenditures')
    ws.append(['ID', 'Date', 'Facility', 'Category', 'Description', 'Amount', 'Platform cost?'])
    _style_header(ws)
    for e in Expenditure.query.order_by(Expenditure.expenditure_date.desc()).all():
        ws.append([
            e.id,
            e.expenditure_date.isoformat() if e.expenditure_date else '',
            e.facility.name if e.facility else 'Central',
            e.category,
            e.description or '',
            float(e.amount or 0),
            'Yes' if e.is_platform_cost else 'No',
        ])
    _autosize(ws)

    # Messages
    ws = wb.create_sheet('Messages')
    ws.append(['ID', 'Facility', 'From admin?', 'Sender', 'Subject', 'Body', 'Read?', 'Created'])
    _style_header(ws)
    for m in Message.query.order_by(Message.created_at.desc()).limit(2000).all():
        ws.append([
            m.id,
            m.facility.name if m.facility else '',
            'Yes' if m.is_from_admin else 'No',
            m.sender.full_name if m.sender else '',
            m.subject or '',
            m.body or '',
            'Yes' if m.is_read else 'No',
            m.created_at.strftime('%Y-%m-%d %H:%M') if m.created_at else '',
        ])
    _autosize(ws)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f'CONTRAconnect_FullData_{datetime.utcnow().strftime("%Y%m%d_%H%M")}.xlsx'
    return send_file(
        buf,
        as_attachment=True,
        download_name=fname,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )



# ---------------------------------------------------------------------------
# Provider Routes
# ---------------------------------------------------------------------------
@app.route('/provider')
@login_required
@provider_required
def provider_dashboard():
    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))
    fac = current_user.facility
    if not fac or not fac.is_active:
        flash('Your facility is not yet activated.', 'warning')
        return render_template('provider_pending.html')

    stock_items = StockItem.query.filter_by(facility_id=fac.id).options(
        joinedload(StockItem.product)
    ).all()
    recent_tx = StockTransaction.query.filter_by(facility_id=fac.id).order_by(
        StockTransaction.created_at.desc()
    ).limit(10).all()
    open_requests = ReplenishmentRequest.query.filter_by(
        facility_id=fac.id, status='pending'
    ).count()
    month_encounters = ClientEncounter.query.filter(
        ClientEncounter.facility_id == fac.id,
        ClientEncounter.encounter_date >= datetime.utcnow().date().replace(day=1)
    ).count()

    return render_template(
        'provider_dashboard.html',
        facility=fac,
        stock_items=stock_items,
        recent_tx=recent_tx,
        open_requests=open_requests,
        month_encounters=month_encounters
    )


@app.route('/provider/request', methods=['GET', 'POST'])
@login_required
@provider_required
def provider_request():
    fac = current_user.facility
    if not fac or not fac.is_active or not current_user.is_active:
        flash('Your facility is deactivated. You cannot submit stock requests. Contact the administrator.', 'danger')
        return redirect(url_for('provider_dashboard'))
    products = Product.query.filter_by(is_active=True).all()
    if request.method == 'POST':
        prod = db.session.get(Product, int(request.form['product_id']))
        req = ReplenishmentRequest(
            facility_id=fac.id,
            product_id=prod.id,
            quantity_requested=int(request.form['quantity']),
            unit_cost=prod.unit_cost,
            justification=request.form.get('justification', ''),
            requested_by=current_user.id
        )
        db.session.add(req)
        db.session.commit()
        flash('Replenishment request submitted.', 'success')
        return redirect(url_for('provider_dashboard'))
    return render_template('provider_request.html', products=products, facility=fac)


@app.route('/provider/encounter', methods=['GET', 'POST'])
@login_required
@provider_required
def provider_encounter():
    fac = current_user.facility
    products = Product.query.filter_by(is_active=True).all()
    if request.method == 'POST':
        outcome = request.form['outcome']
        product_id = request.form.get('product_id') or None
        qty = int(request.form.get('quantity_dispensed') or 0)
        method_accepted = request.form.get('method_accepted') or None

        sat = request.form.get('satisfaction_score')
        try:
            sat_i = int(sat) if sat else None
        except Exception:
            sat_i = None
        enc = ClientEncounter(
            facility_id=fac.id,
            encounter_date=datetime.strptime(request.form['encounter_date'], '%Y-%m-%d').date(),
            client_age_band=request.form.get('client_age_band'),
            client_parity=request.form.get('client_parity'),
            education_level=request.form.get('education_level'),
            method_offered=request.form.get('method_offered'),
            method_accepted=method_accepted if outcome == 'accepted' else None,
            outcome=outcome,
            refusal_reason=request.form.get('refusal_reason') if outcome == 'refused' else None,
            discontinuation_reason=request.form.get('discontinuation_reason') if outcome == 'discontinued' else None,
            counseling_notes=request.form.get('counseling_notes'),
            observation_checklist=request.form.get('observation_checklist'),
            client_code=request.form.get('client_code', '').strip() or None,
            client_sex=request.form.get('client_sex') or None,
            client_residence=request.form.get('client_residence', '').strip() or None,
            client_phone=request.form.get('client_phone', '').strip() or None,
            consent_to_share_story=bool(request.form.get('consent_to_share_story')),
            patient_testimony=request.form.get('patient_testimony', '').strip() or None,
            satisfaction_score=sat_i,
            quantity_dispensed=qty if outcome == 'accepted' else 0,
            product_id=int(product_id) if product_id else None,
            created_by=current_user.id
        )
        db.session.add(enc)
        db.session.flush()

        # Auto-create stock issue if accepted + quantity
        if outcome == 'accepted' and qty > 0 and product_id:
            prod_id = int(product_id)
            stock = StockItem.query.filter_by(facility_id=fac.id, product_id=prod_id).first()
            if stock and stock.quantity_on_hand >= qty:
                stock.quantity_on_hand -= qty
                product = db.session.get(Product, prod_id)
                tx = StockTransaction(
                    facility_id=fac.id,
                    product_id=prod_id,
                    transaction_type='issue',
                    quantity=-qty,
                    unit_cost=product.unit_cost,
                    reference=f'ENC-{enc.id}',
                    notes='Dispensed during client encounter',
                    created_by=current_user.id
                )
                db.session.add(tx)
            else:
                flash('Insufficient stock for the selected quantity. Encounter saved without stock deduction.', 'warning')

        db.session.commit()
        flash('Client encounter recorded.', 'success')
        return redirect(url_for('provider_dashboard'))
    return render_template('provider_encounter.html', products=products, facility=fac)


@app.route('/provider/stock')
@login_required
@provider_required
def provider_stock():
    fac = current_user.facility
    stock_items = StockItem.query.filter_by(facility_id=fac.id).options(
        joinedload(StockItem.product)
    ).all()
    transactions = StockTransaction.query.filter_by(facility_id=fac.id).order_by(
        StockTransaction.created_at.desc()
    ).limit(50).all()
    return render_template('provider_stock.html', stock_items=stock_items, transactions=transactions, facility=fac)



@app.route('/provider/messages', methods=['GET', 'POST'])
@login_required
@provider_required
def provider_messages():
    """Provider inbox: read admin messages and reply."""
    if current_user.role == 'admin':
        return redirect(url_for('admin_dashboard'))
    fac = current_user.facility
    if not fac:
        flash('No facility linked.', 'warning')
        return redirect(url_for('provider_dashboard'))

    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        parent_id = request.form.get('parent_id') or None
        subject = request.form.get('subject', '').strip() or 'Reply from provider'
        if not body:
            flash('Message body is required.', 'danger')
            return redirect(url_for('provider_messages'))
        parent = None
        if parent_id:
            parent = db.session.get(Message, int(parent_id))
            if parent and parent.facility_id != fac.id:
                parent = None
        msg = Message(
            facility_id=fac.id,
            sender_id=current_user.id,
            parent_id=parent.id if parent else None,
            subject=subject if not parent else (parent.subject or 'Reply'),
            body=body,
            is_from_admin=False,
            is_read=False,
        )
        db.session.add(msg)
        db.session.commit()
        flash('Message sent.', 'success')
        return redirect(url_for('provider_messages'))

    messages = Message.query.filter_by(facility_id=fac.id).order_by(
        Message.created_at.desc()
    ).limit(80).all()
    # mark messages from admin as read
    for m in messages:
        if m.is_from_admin and not m.is_read:
            m.is_read = True
    db.session.commit()
    return render_template('provider_messages.html', facility=fac, messages=messages)



# ---------------------------------------------------------------------------
# General Admin — manage other admin accounts
# ---------------------------------------------------------------------------
@app.route('/admin/users', methods=['GET', 'POST'])
@login_required
@general_admin_required
def admin_manage_users():
    """Create / activate / deactivate Program and Finance admins."""
    if request.method == 'POST':
        action = request.form.get('action', 'create')
        if action == 'create':
            email = request.form.get('email', '').strip().lower()
            full_name = request.form.get('full_name', '').strip()
            role = request.form.get('role', 'program_admin')
            password = request.form.get('password', '')
            if role not in ('program_admin', 'finance_admin', 'general_admin'):
                flash('Invalid role.', 'danger')
                return redirect(url_for('admin_manage_users'))
            if User.query.filter_by(email=email).first():
                flash('Email already in use.', 'warning')
                return redirect(url_for('admin_manage_users'))
            ok, msg = validate_password_strength(password, min_length=10, require_strong=True)
            if not ok:
                flash(msg, 'danger')
                return redirect(url_for('admin_manage_users'))
            u = User(email=email, full_name=full_name, role=role, is_active=True)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash(f'{role.replace("_", " ").title()} created: {email}', 'success')
        elif action == 'toggle':
            uid = int(request.form.get('user_id'))
            u = db.session.get(User, uid)
            if not u or not _is_admin_role(u.role):
                flash('Invalid admin user.', 'danger')
            elif u.id == current_user.id:
                flash('You cannot deactivate your own account.', 'warning')
            else:
                u.is_active = not u.is_active
                db.session.commit()
                state = 'activated' if u.is_active else 'deactivated'
                flash(f'{u.full_name} {state}.', 'success')
        elif action == 'set_role':
            uid = int(request.form.get('user_id'))
            role = request.form.get('role')
            u = db.session.get(User, uid)
            if not u or not _is_admin_role(u.role):
                flash('Invalid admin user.', 'danger')
            elif u.id == current_user.id:
                flash('You cannot change your own role here.', 'warning')
            elif role not in ('program_admin', 'finance_admin', 'general_admin'):
                flash('Invalid role.', 'danger')
            else:
                u.role = role
                db.session.commit()
                flash(f'Role updated for {u.full_name}.', 'success')
        return redirect(url_for('admin_manage_users'))

    admins = User.query.filter(User.role.in_(list(ADMIN_ROLES))).order_by(User.created_at.desc()).all()
    return render_template('admin_users.html', admins=admins)


@app.route('/admin/facilities/<int:fid>/assign-user', methods=['POST'])
@login_required
@program_ops_required
def admin_assign_facility_user(fid):
    """Assign or create a real provider user for a facility (no dummy data)."""
    fac = db.session.get(Facility, fid)
    if not fac:
        abort(404)
    email = request.form.get('email', '').strip().lower()
    full_name = request.form.get('full_name', '').strip()
    password = request.form.get('password', '').strip()
    phone = request.form.get('phone', '').strip()
    if not email or not full_name or not password:
        flash('Name, email and password are required.', 'danger')
        return redirect(url_for('admin_facility_detail', fid=fid))
    ok, msg = validate_password_strength(password, min_length=8, require_strong=False)
    if not ok:
        flash(msg, 'danger')
        return redirect(url_for('admin_facility_detail', fid=fid))
    existing = User.query.filter_by(email=email).first()
    if existing:
        if existing.role != 'provider':
            flash('Email belongs to a non-provider account.', 'danger')
            return redirect(url_for('admin_facility_detail', fid=fid))
        existing.facility_id = fac.id
        existing.full_name = full_name
        existing.is_active = fac.is_active
        existing.set_password(password)
        user = existing
    else:
        user = User(email=email, full_name=full_name, role='provider', facility_id=fac.id, is_active=fac.is_active)
        user.set_password(password)
        db.session.add(user)
    fac.contact_person = full_name
    if phone:
        fac.phone = phone
    db.session.commit()
    flash(f'Provider {user.email} assigned to {fac.name}. Share the password securely.', 'success')
    return redirect(url_for('admin_facility_detail', fid=fid))


# ---------------------------------------------------------------------------
# Period reports — PowerPoint + PDF
# ---------------------------------------------------------------------------
def _period_bounds(period):
    end = datetime.utcnow().date()
    if period == 'monthly':
        start = end.replace(day=1)
        label = end.strftime('%B %Y')
    elif period == 'quarterly':
        q = (end.month - 1) // 3
        start = end.replace(month=q * 3 + 1, day=1)
        label = f'Q{q+1} {end.year}'
    elif period == 'yearly':
        start = end.replace(month=1, day=1)
        label = str(end.year)
    else:
        start = end - timedelta(days=90)
        label = f'{start.isoformat()} to {end.isoformat()}'
    return start, end, label


def _make_chart_images(cost_data, uptake, tmpdir):
    paths = {}
    # Uptake doughnut-like bar
    if uptake:
        fig, ax = plt.subplots(figsize=(6, 3.5))
        ax.bar([u['method'][:18] for u in uptake], [u['count'] for u in uptake], color='#0d6e6e')
        ax.set_title('Method uptake')
        ax.tick_params(axis='x', rotation=30)
        fig.tight_layout()
        p = f'{tmpdir}/uptake.png'
        fig.savefig(p, dpi=120)
        plt.close(fig)
        paths['uptake'] = p
    if cost_data.get('per_kiosk'):
        fig, ax = plt.subplots(figsize=(6, 3.5))
        names = [k['name'][:20] for k in cost_data['per_kiosk']]
        vals = [k['operating_cost'] for k in cost_data['per_kiosk']]
        ax.bar(names, vals, color='#e85d04')
        ax.set_title('Operating cost by facility')
        ax.tick_params(axis='x', rotation=25)
        fig.tight_layout()
        p = f'{tmpdir}/kiosk_cost.png'
        fig.savefig(p, dpi=120)
        plt.close(fig)
        paths['kiosk'] = p
    if cost_data.get('method_stats'):
        fig, ax = plt.subplots(figsize=(5, 5))
        labels = [m['method_code'] for m in cost_data['method_stats']]
        sizes = [m['commodity_cost'] or 0.01 for m in cost_data['method_stats']]
        ax.pie(sizes, labels=labels, autopct='%1.0f%%', colors=['#0d6e6e','#e85d04','#198754','#0d6efd','#6f42c1','#dc3545'])
        ax.set_title('Commodity cost by method')
        fig.tight_layout()
        p = f'{tmpdir}/method_cost.png'
        fig.savefig(p, dpi=120)
        plt.close(fig)
        paths['method'] = p
    return paths


@app.route('/admin/reports/<period>/<fmt>')
@login_required
@admin_required
def admin_period_report(period, fmt):
    """Monthly / quarterly / yearly report as pptx or pdf — branded Benin City Mayor Challenge."""
    if period not in ('monthly', 'quarterly', 'yearly', 'programmatic'):
        abort(404)
    if fmt not in ('pptx', 'pdf'):
        abort(404)
    start, end, label = _period_bounds(period if period != 'programmatic' else 'quarterly')
    if period == 'programmatic':
        label = f'Programmatic briefing ({start.isoformat()} → {end.isoformat()})'
    cost_data = compute_cost_metrics(start, end)
    uptake = get_method_uptake(start, end)
    facilities = Facility.query.order_by(Facility.name).all()
    fac_rows = []
    for f in facilities:
        clients = db.session.query(func.count(ClientEncounter.id)).filter(
            ClientEncounter.facility_id == f.id,
            ClientEncounter.encounter_date.between(start, end),
            ClientEncounter.outcome.in_(['accepted', 'discontinued']),
        ).scalar() or 0
        stock_lines = StockItem.query.filter_by(facility_id=f.id).count()
        fac_rows.append({
            'id': f.id, 'name': f.name, 'type': f.facility_type, 'active': f.is_active,
            'clients': clients, 'contact': f.contact_person or '',
            'address': f.address or '', 'stock_lines': stock_lines,
        })
    testimonies = ClientEncounter.query.filter(
        ClientEncounter.encounter_date.between(start, end),
        ClientEncounter.patient_testimony.isnot(None),
        ClientEncounter.patient_testimony != '',
        ClientEncounter.consent_to_share_story == True,
    ).order_by(ClientEncounter.encounter_date.desc()).limit(12).all()
    brand = report_branding()

    with tempfile.TemporaryDirectory() as tmpdir:
        charts = _make_chart_images(cost_data, uptake, tmpdir)
        if fmt == 'pptx':
            return _build_pptx_report(label, period, cost_data, uptake, fac_rows, charts, start, end, testimonies, brand)
        return _build_pdf_report(label, period, cost_data, uptake, fac_rows, charts, start, end, testimonies, brand)



def _logo_abs_path(brand):
    rel = (brand or {}).get('logo_path') or ''
    if not rel:
        return None
    p = os.path.join(app.root_path, 'static', rel)
    return p if os.path.isfile(p) else None


def _build_pptx_report(label, period, cost_data, uptake, fac_rows, charts, start, end, testimonies=None, brand=None):
    brand = brand or report_branding()
    testimonies = testimonies or []
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    teal = RGBColor(0x0D, 0x6E, 0x6E)
    logo = _logo_abs_path(brand)

    def header_bar(slide, title_text):
        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(13.333), Inches(0.85))  # rectangle
        shape.fill.solid()
        shape.fill.fore_color.rgb = teal
        shape.line.fill.background()
        box = slide.shapes.add_textbox(Inches(0.4), Inches(0.18), Inches(10), Inches(0.55))
        p = box.text_frame.paragraphs[0]
        p.text = f"{brand.get('programme_title', 'Benin City Mayor Challenge')}  |  {title_text}"
        p.font.size = Pt(16)
        p.font.bold = True
        p.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        if logo:
            try:
                slide.shapes.add_picture(logo, Inches(11.6), Inches(0.12), height=Inches(0.6))
            except Exception:
                pass
        sub = slide.shapes.add_textbox(Inches(0.4), Inches(0.9), Inches(12), Inches(0.35))
        sp = sub.text_frame.paragraphs[0]
        sp.text = brand.get('org_line') or brand.get('report_subtitle') or ''
        sp.font.size = Pt(11)
        sp.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    def add_title_slide():
        layout = prs.slide_layouts[6]
        slide = prs.slides.add_slide(layout)
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(13.333), Inches(7.5))
        bar.fill.solid()
        bar.fill.fore_color.rgb = teal
        bar.line.fill.background()
        if logo:
            try:
                slide.shapes.add_picture(logo, Inches(5.9), Inches(1.2), height=Inches(1.1))
            except Exception:
                pass
        box = slide.shapes.add_textbox(Inches(0.8), Inches(2.6), Inches(11.7), Inches(1.2))
        p = box.text_frame.paragraphs[0]
        p.text = brand.get('programme_title', 'Benin City Mayor Challenge')
        p.font.size = Pt(34)
        p.font.bold = True
        p.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        box2 = slide.shapes.add_textbox(Inches(0.8), Inches(3.9), Inches(11.7), Inches(1.5))
        t2 = box2.text_frame
        t2.paragraphs[0].text = f"CONTRAconnect programmatic report — {label}"
        t2.paragraphs[0].font.size = Pt(20)
        t2.paragraphs[0].font.color.rgb = RGBColor(0xE8, 0xF5, 0xF5)
        p3 = t2.add_paragraph()
        p3.text = f"Period {start.isoformat()} to {end.isoformat()}  ·  Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
        p3.font.size = Pt(13)
        p3.font.color.rgb = RGBColor(0xCC, 0xEE, 0xEE)

    def add_section(title):
        layout = prs.slide_layouts[6]
        slide = prs.slides.add_slide(layout)
        header_bar(slide, title)
        return slide

    add_title_slide()

    # KPI slide
    slide = add_section('Programme snapshot')
    kpis = [
        f"Clients served: {cost_data['total_clients']}",
        f"Operational cost: {cost_data['total_operational_cost']:,.2f}",
        f"Platform build (tracked separately): {cost_data['platform_build_cost']:,.2f}",
        f"Unit cost per client: {cost_data['unit_cost_per_client']:,.2f}",
        f"Active service points in report: {sum(1 for r in fac_rows if r.get('active'))}",
    ]
    box = slide.shapes.add_textbox(Inches(0.5), Inches(1.4), Inches(6), Inches(5))
    tf = box.text_frame
    tf.word_wrap = True
    for i, line in enumerate(kpis):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = '●  ' + line
        p.font.size = Pt(18)
        p.space_after = Pt(10)
    if charts.get('uptake'):
        slide.shapes.add_picture(charts['uptake'], Inches(7.0), Inches(1.5), width=Inches(5.8))

    # Data analysis
    slide = add_section('Data analysis — method uptake')
    if charts.get('uptake'):
        slide.shapes.add_picture(charts['uptake'], Inches(0.5), Inches(1.4), width=Inches(6.2))
    box = slide.shapes.add_textbox(Inches(7.0), Inches(1.4), Inches(5.8), Inches(5))
    tf = box.text_frame
    tf.word_wrap = True
    tf.paragraphs[0].text = 'Method mix (accepted encounters)'
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.size = Pt(14)
    for u in (uptake or [])[:10]:
        p = tf.add_paragraph()
        p.text = f"{u['method']}: {u['count']} ({u['pct']}%)"
        p.font.size = Pt(13)

    # Financial analysis
    slide = add_section('Financial analysis')
    box = slide.shapes.add_textbox(Inches(0.5), Inches(1.35), Inches(12), Inches(0.8))
    box.text_frame.paragraphs[0].text = (
        'Unit cost per client = operational cost ÷ clients served. Platform build costs are excluded from unit cost.'
    )
    box.text_frame.paragraphs[0].font.size = Pt(13)
    if charts.get('kiosk'):
        slide.shapes.add_picture(charts['kiosk'], Inches(0.4), Inches(2.2), width=Inches(6.3))
    if charts.get('method'):
        slide.shapes.add_picture(charts['method'], Inches(7.0), Inches(2.2), width=Inches(5.5))

    # Per facility / kiosk / PHC slides
    for r in fac_rows[:12]:
        slide = add_section(f"Service point — {r['name']}")
        box = slide.shapes.add_textbox(Inches(0.5), Inches(1.4), Inches(12), Inches(5))
        tf = box.text_frame
        tf.word_wrap = True
        lines = [
            f"Type: {r['type'].upper()}   ·   Status: {'Active' if r['active'] else 'Inactive'}",
            f"Clients served in period: {r['clients']}",
            f"Stock product lines on hand: {r.get('stock_lines', '—')}",
            f"Contact: {r.get('contact') or '—'}",
            f"Address: {r.get('address') or '—'}",
        ]
        for i, line in enumerate(lines):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = line
            p.font.size = Pt(18)
            p.space_after = Pt(8)

    # Patient testimonies
    slide = add_section('Client voices (consented testimonies)')
    box = slide.shapes.add_textbox(Inches(0.5), Inches(1.35), Inches(12.3), Inches(5.8))
    tf = box.text_frame
    tf.word_wrap = True
    if not testimonies:
        tf.paragraphs[0].text = 'No consented patient testimonies recorded in this period. Capture feedback on the encounter form (with consent).'
        tf.paragraphs[0].font.size = Pt(14)
    else:
        for i, tmy in enumerate(testimonies[:6]):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            fac_name = tmy.facility.name if tmy.facility else 'Site'
            score = f" · Satisfaction {tmy.satisfaction_score}/5" if tmy.satisfaction_score else ''
            p.text = f"“{(tmy.patient_testimony or '')[:280]}”"
            p.font.size = Pt(13)
            p.font.italic = True
            p2 = tf.add_paragraph()
            p2.text = f"— {tmy.client_code or 'Client'} · {fac_name} · {tmy.encounter_date}{score}"
            p2.font.size = Pt(11)
            p2.space_after = Pt(12)

    # Closing
    slide = add_section('Closing')
    box = slide.shapes.add_textbox(Inches(0.8), Inches(2.5), Inches(11.5), Inches(3))
    tf = box.text_frame
    tf.paragraphs[0].text = brand.get('programme_title', 'Benin City Mayor Challenge')
    tf.paragraphs[0].font.size = Pt(24)
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.color.rgb = teal
    p = tf.add_paragraph()
    p.text = brand.get('report_subtitle') or 'CONTRAconnect digital platform'
    p.font.size = Pt(16)
    p = tf.add_paragraph()
    p.text = brand.get('org_line') or ''
    p.font.size = Pt(14)

    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)
    fname = f"{brand.get('programme_title','Benin_City').replace(' ','_')}_{period}_{start}.pptx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.presentationml.presentation')


def _build_pdf_report(label, period, cost_data, uptake, fac_rows, charts, start, end, testimonies=None, brand=None):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=0.6*inch, rightMargin=0.6*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('T', parent=styles['Heading1'], textColor=colors.HexColor('#0d6e6e'))
    story = []
    brand = brand or report_branding()
    story.append(Paragraph(brand.get('programme_title', 'Benin City Mayor Challenge'), title_style))
    story.append(Paragraph(f"CONTRAconnect {period.title()} Report — {label}", styles['Heading2']))
    story.append(Paragraph(f'Period: {start.isoformat()} to {end.isoformat()}', styles['Normal']))
    story.append(Spacer(1, 12))
    story.append(Paragraph('Key indicators', styles['Heading2']))
    data = [
        ['Metric', 'Value'],
        ['Clients served', str(cost_data['total_clients'])],
        ['Total operational cost', f"{cost_data['total_operational_cost']:,.2f}"],
        ['Platform build cost (separate)', f"{cost_data['platform_build_cost']:,.2f}"],
        ['Unit cost per client', f"{cost_data['unit_cost_per_client']:,.2f}"],
    ]
    t = Table(data, colWidths=[3.5*inch, 2.5*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6e6e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f7f7')]),
    ]))
    story.append(t)
    story.append(Spacer(1, 14))
    if charts.get('uptake'):
        story.append(Paragraph('Method uptake', styles['Heading2']))
        story.append(RLImage(charts['uptake'], width=5.5*inch, height=3.2*inch))
    if charts.get('kiosk'):
        story.append(Paragraph('Operating cost by facility', styles['Heading2']))
        story.append(RLImage(charts['kiosk'], width=5.5*inch, height=3.2*inch))
    story.append(PageBreak())
    story.append(Paragraph('Cost analysis', styles['Heading2']))
    story.append(Paragraph(
        'Unit cost per client = operational cost ÷ clients. Platform build costs are excluded from unit cost.',
        styles['Normal']))
    if charts.get('method'):
        story.append(RLImage(charts['method'], width=4*inch, height=4*inch))
    story.append(Spacer(1, 12))
    story.append(Paragraph('Facilities summary', styles['Heading2']))
    fdata = [['Facility', 'Type', 'Active', 'Clients', 'Contact']]
    for r in fac_rows:
        fdata.append([r['name'], r['type'], 'Yes' if r['active'] else 'No', str(r['clients']), r['contact'][:30]])
    ft = Table(fdata, colWidths=[2.2*inch, 1*inch, 0.8*inch, 1*inch, 2*inch])
    ft.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0d6e6e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
    ]))
    story.append(ft)
    doc.build(story)
    buf.seek(0)
    fname = f'CONTRAconnect_{period}_{label.replace(" ", "_")}.pdf'
    return send_file(buf, as_attachment=True, download_name=fname, mimetype='application/pdf')





# ---------------------------------------------------------------------------
# Onboarding, password reset, activity monitor
# ---------------------------------------------------------------------------
@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = User.query.filter_by(email=email, is_active=True).first()
        # Always show same message (do not reveal account existence)
        msg = 'If an account exists for that email, a reset link is available below (demo mode) or was sent by email.'
        if user:
            import secrets
            token = secrets.token_urlsafe(32)
            prt = PasswordResetToken(
                user_id=user.id,
                token=token,
                expires_at=datetime.utcnow() + timedelta(hours=2),
            )
            db.session.add(prt)
            db.session.commit()
            log_activity('password_reset_request', email, user=user)
            # Demo / no-SMTP: show one-time link on screen
            reset_url = url_for('reset_password', token=token, _external=True)
            flash(msg, 'info')
            flash(f'Reset link (valid 2 hours): {reset_url}', 'warning')
        else:
            flash(msg, 'info')
        return redirect(url_for('forgot_password'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    prt = PasswordResetToken.query.filter_by(token=token, used=False).first()
    if not prt or prt.expires_at < datetime.utcnow():
        flash('This reset link is invalid or has expired.', 'danger')
        return redirect(url_for('forgot_password'))
    user = db.session.get(User, prt.user_id)
    if request.method == 'POST':
        pw = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if pw != confirm:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('reset_password', token=token))
        strong = _is_admin_role(user.role) if user else False
        ok, msg = validate_password_strength(pw, min_length=10 if strong else 8, require_strong=strong)
        if not ok:
            flash(msg, 'danger')
            return redirect(url_for('reset_password', token=token))
        user.set_password(pw)
        user.password_changed_at = datetime.utcnow()
        prt.used = True
        db.session.commit()
        log_activity('password_reset_complete', user=user)
        flash('Password updated. You can sign in now.', 'success')
        return redirect(url_for('login'))
    return render_template('reset_password.html', token=token)


@app.route('/onboarding', methods=['GET', 'POST'])
@login_required
def onboarding():
    user = current_user
    status = getattr(user, 'onboarding_status', 'active') or 'active'
    if status == 'active' and not getattr(user, 'must_complete_onboarding', False):
        return redirect(url_for('index'))
    if status == 'pending_approval':
        return render_template('onboarding.html', stage='waiting')
    if status == 'rejected':
        return render_template('onboarding.html', stage='rejected')

    if request.method == 'POST':
        code = request.form.get('activation_code', '').strip()
        if not user.activation_code or code != user.activation_code:
            flash('Invalid activation code. Use the code issued by your administrator.', 'danger')
            return redirect(url_for('onboarding'))
        if not request.form.get('role_confirmed'):
            flash('Please confirm you are willing to perform the assigned role.', 'danger')
            return redirect(url_for('onboarding'))
        if not request.form.get('ethics_accepted'):
            flash('You must accept the organisation ethics & privacy commitment.', 'danger')
            return redirect(url_for('onboarding'))
        user.phone = request.form.get('phone', '').strip()
        user.organization = request.form.get('organization', '').strip()
        user.full_name = request.form.get('full_name', user.full_name).strip() or user.full_name
        user.role_confirmed = True
        user.ethics_accepted = True
        user.ethics_accepted_at = datetime.utcnow()
        user.onboarding_notes = request.form.get('onboarding_notes', '').strip()
        photo = request.files.get('profile_photo')
        if photo and photo.filename:
            ext = photo.filename.rsplit('.', 1)[-1].lower()
            if ext in ('png', 'jpg', 'jpeg', 'webp'):
                fname = f"user_{user.id}_{int(datetime.utcnow().timestamp())}.{ext}"
                dest_dir = os.path.join(app.root_path, 'static', 'uploads', 'profiles')
                os.makedirs(dest_dir, exist_ok=True)
                photo.save(os.path.join(dest_dir, fname))
                user.profile_photo = f'uploads/profiles/{fname}'
        user.onboarding_status = 'pending_approval'
        user.must_complete_onboarding = False
        db.session.commit()
        log_activity('onboarding_submitted')
        flash('Profile submitted. An administrator will approve your access.', 'success')
        return redirect(url_for('onboarding'))

    return render_template('onboarding.html', stage='form')


@app.route('/admin/staff-approvals')
@login_required
@general_admin_required
def admin_staff_approvals():
    pending = User.query.filter_by(onboarding_status='pending_approval').order_by(User.created_at.desc()).all()
    awaiting_profile = User.query.filter_by(onboarding_status='pending_profile').order_by(User.created_at.desc()).all()
    return render_template('admin_staff_approvals.html', pending=pending, awaiting_profile=awaiting_profile)


@app.route('/admin/staff-approvals/<int:uid>/<action>', methods=['POST'])
@login_required
@general_admin_required
def admin_staff_approval_action(uid, action):
    user = db.session.get(User, uid)
    if not user:
        abort(404)
    if action == 'approve':
        user.onboarding_status = 'active'
        user.is_active = True
        user.must_complete_onboarding = False
        user.activation_code = None
        flash(f'{user.full_name} approved for login.', 'success')
        log_activity('staff_approved', user.email)
    elif action == 'reject':
        user.onboarding_status = 'rejected'
        user.is_active = False
        flash(f'{user.full_name} rejected.', 'warning')
        log_activity('staff_rejected', user.email)
    db.session.commit()
    return redirect(url_for('admin_staff_approvals'))


@app.route('/admin/staff-invite', methods=['POST'])
@login_required
@general_admin_required
def admin_staff_invite():
    """Admin pre-creates staff with temporary password + activation code."""
    import secrets
    email = request.form.get('email', '').strip().lower()
    full_name = request.form.get('full_name', '').strip()
    role = request.form.get('role', 'mel_consultant')
    temp_pw = request.form.get('temp_password', '').strip() or secrets.token_urlsafe(8)
    code = request.form.get('activation_code', '').strip() or secrets.token_hex(3).upper()
    if User.query.filter_by(email=email).first():
        flash('Email already exists.', 'warning')
        return redirect(url_for('admin_staff_approvals'))
    ok, msg = validate_password_strength(temp_pw, min_length=8, require_strong=False)
    if not ok:
        flash(msg, 'danger')
        return redirect(url_for('admin_staff_approvals'))
    u = User(
        email=email,
        full_name=full_name,
        role=role,
        staff_title=request.form.get('staff_title', ''),
        is_active=True,
        must_complete_onboarding=True,
        onboarding_status='pending_profile',
        activation_code=code,
    )
    u.set_password(temp_pw)
    db.session.add(u)
    db.session.commit()
    log_activity('staff_invited', email)
    flash(f'Staff invited. Email: {email} · Temp password: {temp_pw} · Activation code: {code} — share securely.', 'success')
    return redirect(url_for('admin_staff_approvals'))


@app.route('/admin/activity')
@login_required
@general_admin_required
def admin_activity():
    """Monitor user footprints and activities."""
    q = ActivityLog.query.order_by(ActivityLog.created_at.desc())
    user_filter = request.args.get('user_id')
    action_filter = request.args.get('action')
    if user_filter:
        q = q.filter_by(user_id=int(user_filter))
    if action_filter:
        q = q.filter_by(action=action_filter)
    logs = q.limit(300).all()
    users = User.query.order_by(User.full_name).all()
    return render_template('admin_activity.html', logs=logs, users=users,
                           user_filter=user_filter, action_filter=action_filter)




# ---------------------------------------------------------------------------
# Expense requests, budget, variance, payment vouchers
# ---------------------------------------------------------------------------
def _can_review_expense(role):
    return role in ('finance_analyst', 'finance_admin', 'general_admin', 'admin')


def _can_approve_expense_pm(role):
    return role in ('project_manager', 'program_admin', 'general_admin', 'admin')


@app.route('/api/expense-code/<int:cid>')
@login_required
def api_expense_code(cid):
    c = db.session.get(ExpenseCode, cid)
    if not c:
        return jsonify({}), 404
    return jsonify({'id': c.id, 'code': c.code, 'category': c.category, 'description': c.description})


@app.route('/expenses')
@login_required
def expense_list():
    if current_user.role == 'provider':
        flash('Expense claims are for programme staff.', 'warning')
        return redirect(url_for('provider_dashboard'))
    q = ExpenseRequest.query.order_by(ExpenseRequest.created_at.desc())
    if not (_can_review_expense(current_user.role) or _can_approve_expense_pm(current_user.role)):
        q = q.filter_by(requester_id=current_user.id)
    items = q.limit(200).all()
    return render_template('expense_list.html', items=items)


@app.route('/expenses/new', methods=['GET', 'POST'])
@login_required
def expense_new():
    if current_user.role == 'provider':
        flash('Not available for service-provider accounts.', 'warning')
        return redirect(url_for('provider_dashboard'))
    codes = ExpenseCode.query.filter_by(is_active=True).order_by(ExpenseCode.code).all()
    if request.method == 'POST':
        code_id = int(request.form.get('expense_code_id'))
        code = db.session.get(ExpenseCode, code_id)
        if not code:
            flash('Select a valid expense code.', 'danger')
            return redirect(url_for('expense_new'))
        try:
            amount = Decimal(request.form.get('amount', '0'))
        except Exception:
            flash('Invalid amount.', 'danger')
            return redirect(url_for('expense_new'))
        if amount <= 0:
            flash('Amount must be positive.', 'danger')
            return redirect(url_for('expense_new'))
        action = request.form.get('action', 'draft')
        num = f"EXP-{datetime.utcnow().strftime('%Y%m%d')}-{current_user.id}-{int(datetime.utcnow().timestamp()) % 10000}"
        er = ExpenseRequest(
            request_number=num,
            requester_id=current_user.id,
            expense_code_id=code.id,
            category=code.category,
            description=request.form.get('description', '').strip() or code.description,
            amount=amount,
            currency=request.form.get('currency', 'USD'),
            payee_name=request.form.get('payee_name', '').strip() or current_user.full_name,
            bank_name=request.form.get('bank_name', '').strip(),
            account_number=request.form.get('account_number', '').strip(),
            account_name=request.form.get('account_name', '').strip(),
            evidence_notes=request.form.get('evidence_notes', '').strip(),
            status='submitted' if action == 'submit' else 'draft',
        )
        f = request.files.get('evidence_file')
        if f and f.filename:
            safe = f"{num}_{f.filename.replace(' ', '_')[:80]}"
            d = os.path.join(app.root_path, 'static', 'uploads', 'expenses')
            os.makedirs(d, exist_ok=True)
            f.save(os.path.join(d, safe))
            er.evidence_filename = safe
        db.session.add(er)
        db.session.commit()
        log_activity('expense_submitted', er.request_number)
        flash(f'Expense {er.request_number} saved ({er.status}).', 'success')
        return redirect(url_for('expense_detail', eid=er.id))
    return render_template('expense_form.html', codes=codes)


@app.route('/expenses/<int:eid>')
@login_required
def expense_detail(eid):
    er = db.session.get(ExpenseRequest, eid)
    if not er:
        abort(404)
    if er.requester_id != current_user.id and not (
        _can_review_expense(current_user.role) or _can_approve_expense_pm(current_user.role)
    ):
        abort(403)
    return render_template('expense_detail.html', er=er)


@app.route('/expenses/<int:eid>/submit', methods=['POST'])
@login_required
def expense_submit(eid):
    er = db.session.get(ExpenseRequest, eid)
    if not er or er.requester_id != current_user.id or er.status != 'draft':
        flash('Only your draft requests can be submitted.', 'warning')
        return redirect(url_for('expense_list'))
    er.status = 'submitted'
    db.session.commit()
    flash('Submitted for Finance review.', 'success')
    return redirect(url_for('expense_detail', eid=eid))


@app.route('/expenses/<int:eid>/finance-review', methods=['POST'])
@login_required
def expense_finance_review(eid):
    if not _can_review_expense(current_user.role):
        flash('Finance review privilege required.', 'danger')
        return redirect(url_for('expense_detail', eid=eid))
    er = db.session.get(ExpenseRequest, eid)
    if not er or er.status != 'submitted':
        flash('Not awaiting finance review.', 'warning')
        return redirect(url_for('expense_list'))
    er.finance_notes = request.form.get('finance_notes', '').strip()
    er.finance_reviewer_id = current_user.id
    er.finance_reviewed_at = datetime.utcnow()
    if request.form.get('decision') == 'approve':
        er.status = 'finance_review'
        flash('Cleared for Project Manager approval.', 'success')
    else:
        er.status = 'finance_rejected'
        flash('Expense rejected by Finance.', 'warning')
    db.session.commit()
    return redirect(url_for('expense_detail', eid=eid))


@app.route('/expenses/<int:eid>/pm-approve', methods=['POST'])
@login_required
def expense_pm_approve(eid):
    if not _can_approve_expense_pm(current_user.role):
        flash('Project Manager approval privilege required.', 'danger')
        return redirect(url_for('expense_detail', eid=eid))
    er = db.session.get(ExpenseRequest, eid)
    if not er or er.status != 'finance_review':
        flash('Must pass Finance review first.', 'warning')
        return redirect(url_for('expense_list'))
    er.pm_notes = request.form.get('pm_notes', '').strip()
    er.pm_approver_id = current_user.id
    er.pm_approved_at = datetime.utcnow()
    if request.form.get('decision') == 'approve':
        er.status = 'pm_approved'
        flash('Approved. Finance can now pay and issue voucher.', 'success')
    else:
        er.status = 'pm_rejected'
        flash('Rejected by Project Manager.', 'warning')
    db.session.commit()
    return redirect(url_for('expense_detail', eid=eid))


@app.route('/expenses/<int:eid>/mark-paid', methods=['POST'])
@login_required
def expense_mark_paid(eid):
    if not _can_review_expense(current_user.role):
        flash('Only Finance can mark paid.', 'danger')
        return redirect(url_for('expense_detail', eid=eid))
    er = db.session.get(ExpenseRequest, eid)
    if not er or er.status != 'pm_approved':
        flash('Only PM-approved expenses can be paid.', 'warning')
        return redirect(url_for('expense_list'))
    er.status = 'paid'
    er.paid_by_id = current_user.id
    er.paid_at = datetime.utcnow()
    er.voucher_number = er.voucher_number or f"PV-{datetime.utcnow().strftime('%Y%m%d')}-{er.id}"
    # Post to ledger under expense category
    db.session.add(Expenditure(
        facility_id=None,
        category=er.category,
        description=f"{er.request_number} / {er.expense_code.code}: {er.description[:120]}",
        amount=er.amount,
        expenditure_date=datetime.utcnow().date(),
        is_platform_cost=(er.expense_code.code.startswith('EXP-PLT')),
        created_by=current_user.id,
    ))
    db.session.commit()
    log_activity('expense_paid', er.request_number)
    flash('Marked paid and posted to budget actuals. Download the payment voucher.', 'success')
    return redirect(url_for('expense_voucher_pdf', eid=eid))


@app.route('/expenses/<int:eid>/voucher.pdf')
@login_required
def expense_voucher_pdf(eid):
    er = db.session.get(ExpenseRequest, eid)
    if not er:
        abort(404)
    if er.requester_id != current_user.id and not (
        _can_review_expense(current_user.role) or _can_approve_expense_pm(current_user.role)
    ):
        abort(403)
    brand = report_branding()
    buf = BytesIO()
    from reportlab.lib.pagesizes import A4
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=0.6*inch, rightMargin=0.6*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    title = ParagraphStyle('VT', parent=styles['Heading1'], textColor=colors.HexColor('#0b6e6e'), fontSize=16)
    story = []
    story.append(Paragraph(brand.get('app_name', 'CONTRAconnect'), title))
    story.append(Paragraph(brand.get('programme_title', 'Benin City Mayor Challenge'), styles['Heading2']))
    story.append(Paragraph('<b>PAYMENT VOUCHER</b>', styles['Heading2']))
    story.append(Spacer(1, 8))
    data = [
        ['Voucher No.', er.voucher_number or '—', 'Request No.', er.request_number],
        ['Date', (er.paid_at or er.updated_at or datetime.utcnow()).strftime('%Y-%m-%d'), 'Status', er.status],
        ['Expense code', er.expense_code.code if er.expense_code else '—', 'Category', er.category],
        ['Payee', er.payee_name, 'Currency', er.currency],
        ['Amount (figures)', f'{float(er.amount):,.2f}', 'Amount (words)', amount_in_words(er.amount)],
        ['Bank', er.bank_name or '—', 'Account', f'{er.account_name or "—"} / {er.account_number or "—"}'],
        ['Requester', er.requester.full_name if er.requester else '—', 'Submitted', er.created_at.strftime('%Y-%m-%d') if er.created_at else '—'],
        ['Finance reviewer', er.finance_reviewer.full_name if er.finance_reviewer else '—',
         'Reviewed', er.finance_reviewed_at.strftime('%Y-%m-%d') if er.finance_reviewed_at else '—'],
        ['Approved by (PM)', er.pm_approver.full_name if er.pm_approver else '—',
         'Approved', er.pm_approved_at.strftime('%Y-%m-%d') if er.pm_approved_at else '—'],
        ['Paid by', er.paid_by.full_name if er.paid_by else '—',
         'Paid on', er.paid_at.strftime('%Y-%m-%d') if er.paid_at else '—'],
    ]
    t = Table(data, colWidths=[1.3*inch, 2.2*inch, 1.3*inch, 2.2*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#e6f4f4')),
        ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#e6f4f4')),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    story.append(t)
    story.append(Spacer(1, 12))
    story.append(Paragraph('<b>Expense description / breakdown</b>', styles['Heading3']))
    story.append(Paragraph((er.description or '').replace('\n', '<br/>'), styles['Normal']))
    if er.expense_code:
        story.append(Paragraph(f"Code description: {er.expense_code.description}", styles['Normal']))
    if er.finance_notes:
        story.append(Paragraph(f"Finance notes: {er.finance_notes}", styles['Normal']))
    if er.pm_notes:
        story.append(Paragraph(f"PM notes: {er.pm_notes}", styles['Normal']))
    story.append(Spacer(1, 24))
    story.append(Paragraph('_________________________&nbsp;&nbsp;&nbsp;&nbsp;_________________________&nbsp;&nbsp;&nbsp;&nbsp;_________________________', styles['Normal']))
    story.append(Paragraph('Requester&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Finance&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Project Manager', styles['Normal']))
    doc.build(story)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"PaymentVoucher_{er.voucher_number or er.request_number}.pdf",
                     mimetype='application/pdf')


@app.route('/admin/budget', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_budget():
    """Finance budget template: amounts + variance narration per expense code."""
    if not _can_review_expense(current_user.role) and current_user.role not in ('project_manager', 'general_admin', 'admin'):
        flash('Budget access restricted.', 'danger')
        return redirect(url_for('index'))
    fy = request.args.get('fy', '2026')
    period = request.args.get('period', 'Pilot')
    codes = ExpenseCode.query.filter_by(is_active=True).order_by(ExpenseCode.code).all()
    if request.method == 'POST' and _can_review_expense(current_user.role):
        fy = request.form.get('fiscal_year', fy)
        period = request.form.get('period_label', period)
        for c in codes:
            amt = request.form.get(f'budget_{c.id}', '').strip()
            narr = request.form.get(f'narr_{c.id}', '').strip()
            try:
                budget_amt = Decimal(amt) if amt else Decimal('0')
            except Exception:
                budget_amt = Decimal('0')
            line = BudgetLine.query.filter_by(expense_code_id=c.id, fiscal_year=fy, period_label=period).first()
            if not line:
                line = BudgetLine(expense_code_id=c.id, fiscal_year=fy, period_label=period)
                db.session.add(line)
            line.budget_amount = budget_amt
            line.variance_narration = narr
            line.updated_by = current_user.id
        db.session.commit()
        flash('Budget template saved.', 'success')
        return redirect(url_for('admin_budget', fy=fy, period=period))

    lines = {bl.expense_code_id: bl for bl in BudgetLine.query.filter_by(fiscal_year=fy, period_label=period).all()}
    # Actuals from paid expense requests
    actuals = {}
    paid = ExpenseRequest.query.filter_by(status='paid').all()
    for er in paid:
        actuals[er.expense_code_id] = actuals.get(er.expense_code_id, Decimal('0')) + (er.amount or 0)
    rows = []
    for c in codes:
        bl = lines.get(c.id)
        budget = float(bl.budget_amount) if bl else 0.0
        actual = float(actuals.get(c.id, 0))
        var = budget - actual
        rows.append({
            'code': c, 'budget': budget, 'actual': actual, 'variance': var,
            'narration': bl.variance_narration if bl else '',
            'line': bl,
        })
    return render_template('admin_budget.html', rows=rows, fy=fy, period=period,
                           can_edit=_can_review_expense(current_user.role))


@app.route('/admin/budget/variance-report')
@login_required
@admin_required
def budget_variance_report():
    fy = request.args.get('fy', '2026')
    period = request.args.get('period', 'Pilot')
    codes = ExpenseCode.query.filter_by(is_active=True).order_by(ExpenseCode.code).all()
    lines = {bl.expense_code_id: bl for bl in BudgetLine.query.filter_by(fiscal_year=fy, period_label=period).all()}
    actuals = {}
    for er in ExpenseRequest.query.filter_by(status='paid').all():
        actuals[er.expense_code_id] = actuals.get(er.expense_code_id, Decimal('0')) + (er.amount or 0)
    rows = []
    for c in codes:
        bl = lines.get(c.id)
        budget = float(bl.budget_amount) if bl else 0.0
        actual = float(actuals.get(c.id, 0))
        rows.append({
            'code': c.code, 'category': c.category, 'description': c.description,
            'budget': budget, 'actual': actual, 'variance': budget - actual,
            'narration': (bl.variance_narration if bl else '') or '',
        })
    return render_template('budget_variance.html', rows=rows, fy=fy, period=period)



# ---------------------------------------------------------------------------
# Staff home + Invoice / payment workflow
# ---------------------------------------------------------------------------
@app.route('/staff')
@login_required
@admin_required
def staff_dashboard():
    """Role-aware landing page for programme staff."""
    role = current_user.role
    pending_finance = Invoice.query.filter_by(status='submitted').count()
    pending_pm = Invoice.query.filter_by(status='finance_review').count()
    my_invoices = Invoice.query.filter_by(submitter_id=current_user.id).order_by(
        Invoice.created_at.desc()
    ).limit(8).all()
    recent_all = Invoice.query.order_by(Invoice.created_at.desc()).limit(10).all()
    return render_template(
        'staff_dashboard.html',
        pending_finance=pending_finance,
        pending_pm=pending_pm,
        my_invoices=my_invoices,
        recent_all=recent_all,
        role=role,
    )


@app.route('/invoices')
@login_required
@admin_required
def invoice_list():
    role = current_user.role
    q = Invoice.query.order_by(Invoice.created_at.desc())
    if role in CONSULTANT_ROLES:
        q = q.filter(
            (Invoice.submitter_id == current_user.id) |
            (Invoice.status.in_(['submitted', 'finance_review', 'pm_approved', 'paid', 'finance_rejected', 'pm_rejected']))
        )
    invoices = q.limit(200).all()
    return render_template('invoice_list.html', invoices=invoices)


@app.route('/invoices/new', methods=['GET', 'POST'])
@login_required
@admin_required
def invoice_new():
    if not _can_submit_invoice(current_user.role):
        flash('You are not authorised to submit payment invoices.', 'danger')
        return redirect(url_for('invoice_list'))
    if request.method == 'POST':
        action = request.form.get('action', 'draft')
        inv_no = f"INV-{datetime.utcnow().strftime('%Y%m%d')}-{current_user.id}-{int(datetime.utcnow().timestamp()) % 10000}"
        try:
            amount = Decimal(request.form.get('amount', '0'))
        except Exception:
            flash('Invalid amount.', 'danger')
            return redirect(url_for('invoice_new'))
        status = 'submitted' if action == 'submit' else 'draft'
        inv = Invoice(
            invoice_number=inv_no,
            submitter_id=current_user.id,
            payee_name=request.form.get('payee_name', '').strip() or current_user.full_name,
            bank_name=request.form.get('bank_name', '').strip(),
            account_number=request.form.get('account_number', '').strip(),
            account_name=request.form.get('account_name', '').strip(),
            deliverable_title=request.form.get('deliverable_title', '').strip(),
            deliverable_description=request.form.get('deliverable_description', '').strip(),
            amount=amount,
            currency=request.form.get('currency', 'USD'),
            evidence_notes=request.form.get('evidence_notes', '').strip(),
            status=status,
        )
        ps = request.form.get('period_start')
        pe = request.form.get('period_end')
        if ps:
            inv.period_start = datetime.strptime(ps, '%Y-%m-%d').date()
        if pe:
            inv.period_end = datetime.strptime(pe, '%Y-%m-%d').date()
        # Optional file upload
        f = request.files.get('evidence_file')
        if f and f.filename:
            safe = f"{inv_no}_{f.filename.replace(' ', '_')[:80]}"
            upload_dir = os.path.join(app.root_path, 'static', 'uploads', 'invoices')
            os.makedirs(upload_dir, exist_ok=True)
            f.save(os.path.join(upload_dir, safe))
            inv.evidence_filename = safe
        if not inv.deliverable_title or amount <= 0:
            flash('Deliverable title and a positive amount are required.', 'danger')
            return redirect(url_for('invoice_new'))
        db.session.add(inv)
        db.session.commit()
        flash(f'Invoice {inv.invoice_number} saved as {status}.', 'success')
        return redirect(url_for('invoice_detail', iid=inv.id))
    return render_template('invoice_form.html')


@app.route('/invoices/<int:iid>')
@login_required
@admin_required
def invoice_detail(iid):
    inv = db.session.get(Invoice, iid)
    if not inv:
        abort(404)
    return render_template('invoice_detail.html', inv=inv)


@app.route('/invoices/<int:iid>/submit', methods=['POST'])
@login_required
@admin_required
def invoice_submit(iid):
    inv = db.session.get(Invoice, iid)
    if not inv or inv.submitter_id != current_user.id:
        abort(404)
    if inv.status != 'draft':
        flash('Only draft invoices can be submitted.', 'warning')
        return redirect(url_for('invoice_detail', iid=iid))
    inv.status = 'submitted'
    db.session.commit()
    flash('Invoice submitted for Finance review.', 'success')
    return redirect(url_for('invoice_detail', iid=iid))


@app.route('/invoices/<int:iid>/finance-review', methods=['POST'])
@login_required
@admin_required
def invoice_finance_review(iid):
    if not _can_review_invoices_finance(current_user.role):
        flash('Only the Financial Analyst can review invoices.', 'danger')
        return redirect(url_for('invoice_detail', iid=iid))
    inv = db.session.get(Invoice, iid)
    if not inv or inv.status != 'submitted':
        flash('Invoice is not awaiting finance review.', 'warning')
        return redirect(url_for('invoice_list'))
    decision = request.form.get('decision')
    inv.finance_notes = request.form.get('finance_notes', '').strip()
    inv.finance_reviewer_id = current_user.id
    inv.finance_reviewed_at = datetime.utcnow()
    if decision == 'approve':
        inv.status = 'finance_review'  # means finance cleared → awaiting PM
        flash('Finance review completed. Awaiting Project Manager approval.', 'success')
    else:
        inv.status = 'finance_rejected'
        flash('Invoice rejected by Finance.', 'warning')
    db.session.commit()
    return redirect(url_for('invoice_detail', iid=iid))


@app.route('/invoices/<int:iid>/pm-approve', methods=['POST'])
@login_required
@admin_required
def invoice_pm_approve(iid):
    if not _can_approve_invoices_pm(current_user.role):
        flash('Only the Project Manager can give final approval.', 'danger')
        return redirect(url_for('invoice_detail', iid=iid))
    inv = db.session.get(Invoice, iid)
    if not inv or inv.status != 'finance_review':
        flash('Invoice must pass Finance review before PM approval.', 'warning')
        return redirect(url_for('invoice_list'))
    decision = request.form.get('decision')
    inv.pm_notes = request.form.get('pm_notes', '').strip()
    inv.pm_approver_id = current_user.id
    inv.pm_approved_at = datetime.utcnow()
    if decision == 'approve':
        inv.status = 'pm_approved'
        flash('Invoice approved by Project Manager.', 'success')
    else:
        inv.status = 'pm_rejected'
        flash('Invoice rejected by Project Manager.', 'warning')
    db.session.commit()
    return redirect(url_for('invoice_detail', iid=iid))


@app.route('/invoices/<int:iid>/mark-paid', methods=['POST'])
@login_required
@admin_required
def invoice_mark_paid(iid):
    if not _can_review_invoices_finance(current_user.role):
        flash('Only Finance can mark invoices as paid.', 'danger')
        return redirect(url_for('invoice_detail', iid=iid))
    inv = db.session.get(Invoice, iid)
    if not inv or inv.status != 'pm_approved':
        flash('Only PM-approved invoices can be marked paid.', 'warning')
        return redirect(url_for('invoice_list'))
    inv.status = 'paid'
    db.session.commit()
    flash('Invoice marked as paid.', 'success')
    return redirect(url_for('invoice_detail', iid=iid))





# ---------------------------------------------------------------------------
# Editable sample / demo data for all reports
# ---------------------------------------------------------------------------
def load_report_sample_data():
    """Populate realistic sample operational + financial data for dashboards and reports."""
    import random
    ensure_expense_codes()
    admin = User.query.filter(User.role.in_(['general_admin', 'admin', 'project_manager', 'finance_analyst'])).first()
    products = Product.query.filter_by(is_active=True).all()
    facilities = Facility.query.filter_by(is_active=True).all()
    if not products or not facilities:
        return {'ok': False, 'msg': 'Need products and facilities first (run seed).'}

    # Budget lines with sample budgets
    codes = ExpenseCode.query.filter_by(is_active=True).all()
    sample_budgets = {
        'EXP-STAFF-01': 24528, 'EXP-STAFF-02': 28000, 'EXP-TRAIN-01': 4308, 'EXP-TRAIN-02': 3055,
        'EXP-LOG-01': 3000, 'EXP-LOG-02': 10000, 'EXP-SBC-01': 9700, 'EXP-SBC-02': 10004,
        'EXP-COMM-01': 80000, 'EXP-COMM-02': 25000, 'EXP-EQP-01': 24500, 'EXP-EQP-02': 9550,
        'EXP-OPS-01': 10000, 'EXP-PLT-01': 30000, 'EXP-VND-01': 8000,
    }
    for c in codes:
        bl = BudgetLine.query.filter_by(expense_code_id=c.id, fiscal_year='2026', period_label='Pilot').first()
        if not bl:
            bl = BudgetLine(expense_code_id=c.id, fiscal_year='2026', period_label='Pilot')
            db.session.add(bl)
        bl.budget_amount = Decimal(str(sample_budgets.get(c.code, 5000)))
        if not bl.variance_narration:
            bl.variance_narration = 'Sample narration — replace with finance analysis.'

    # Stock at facilities
    for fac in facilities:
        for prod in products:
            si = StockItem.query.filter_by(facility_id=fac.id, product_id=prod.id).first()
            qty = random.randint(30, 200) if prod.method_code != 'Condom' else random.randint(200, 800)
            if not si:
                si = StockItem(facility_id=fac.id, product_id=prod.id, quantity_on_hand=qty, reorder_level=15)
                db.session.add(si)
                db.session.add(StockTransaction(
                    facility_id=fac.id, product_id=prod.id, transaction_type='receipt',
                    quantity=qty, unit_cost=prod.unit_cost, reference='SAMPLE-LOAD',
                    created_by=admin.id if admin else None,
                ))
            else:
                si.quantity_on_hand = max(si.quantity_on_hand, qty)

    # Sample expenditures (ledger)
    if Expenditure.query.filter(Expenditure.description.like('SAMPLE:%')).count() < 5:
        samples_exp = [
            ('staff', 'SAMPLE: Field nurse stipends Q1', 8200, False),
            ('logistics', 'SAMPLE: Distribution runs — 4 kiosks', 2100, False),
            ('utilities', 'SAMPLE: Storehouse power & security', 1800, False),
            ('platform_build', 'SAMPLE: Platform security hardening', 4500, True),
            ('commodity', 'SAMPLE: Emergency commodity top-up', 6200, False),
        ]
        for cat, desc, amt, plat in samples_exp:
            db.session.add(Expenditure(
                facility_id=facilities[0].id if not plat else None,
                category=cat, description=desc, amount=Decimal(str(amt)),
                expenditure_date=datetime.utcnow().date() - timedelta(days=random.randint(5, 60)),
                is_platform_cost=plat, created_by=admin.id if admin else None,
            ))

    # Client encounters with method mix + testimonies
    methods = [p.method_code for p in products]
    outcomes = ['accepted', 'accepted', 'accepted', 'refused', 'counselled_only', 'discontinued']
    age_bands = ['<20', '20-24', '25-29', '30-34', '35+']
    stories = [
        ('I received clear counselling and chose a method that fits my family plan.', 5),
        ('The kiosk was close to the market — I did not lose a full work day.', 5),
        ('Nurse explained side effects honestly. I felt respected.', 4),
        ('Stock was available the same day I decided.', 5),
        ('Mobiliser invited me; the service was free of pressure.', 4),
    ]
    existing_sample_enc = ClientEncounter.query.filter(ClientEncounter.client_code.like('SAMP-%')).count()
    if existing_sample_enc < 40:
        for i in range(45):
            fac = random.choice(facilities)
            prod = random.choice(products)
            outcome = random.choice(outcomes)
            day = datetime.utcnow().date() - timedelta(days=random.randint(0, 80))
            accepted = outcome == 'accepted'
            story = None
            consent = False
            score = None
            if accepted and random.random() < 0.25:
                story, score = random.choice(stories)
                consent = True
            enc = ClientEncounter(
                facility_id=fac.id,
                encounter_date=day,
                client_age_band=random.choice(age_bands),
                client_parity=random.choice(['0', '1-2', '3+']),
                education_level=random.choice(['Primary', 'Secondary', 'Tertiary', 'None']),
                method_offered=prod.method_code,
                method_accepted=prod.method_code if accepted else None,
                outcome=outcome,
                refusal_reason='Partner opposition' if outcome == 'refused' else None,
                discontinuation_reason='Side effects' if outcome == 'discontinued' else None,
                counseling_notes='SAMPLE encounter for reports',
                client_code=f'SAMP-{1000+i}',
                client_sex='Female',
                client_residence=fac.city or 'Benin City',
                consent_to_share_story=consent,
                patient_testimony=story,
                satisfaction_score=score,
                quantity_dispensed=1 if accepted else 0,
                product_id=prod.id if accepted else None,
                created_by=admin.id if admin else None,
            )
            db.session.add(enc)
            if accepted:
                # stock issue
                db.session.add(StockTransaction(
                    facility_id=fac.id, product_id=prod.id, transaction_type='issue',
                    quantity=1, unit_cost=prod.unit_cost, reference='SAMPLE-ENC',
                    created_by=admin.id if admin else None,
                ))

    # Financial records + expenses in every workflow state
    # Clear prior SAMPLE expenses so reload refreshes pipeline
    ExpenseRequest.query.filter(ExpenseRequest.request_number.like('SAMPLE-%')).delete(synchronize_session=False)
    Expenditure.query.filter(Expenditure.description.like('SAMPLE-FIN:%')).delete(synchronize_session=False)
    db.session.flush()

    requester = admin
    rid = requester.id if requester else 1
    now = datetime.utcnow()
    payees = [
        'Sample Vendor Ltd', 'Edo Field Logistics Co.', 'Benin Clinical Supplies',
        'Knowsoft Consulting', 'Community Mobilisers Collective', 'City Health Stores'
    ]
    # Extra ledger / financial records
    fin_rows = [
        ('staff', 'SAMPLE-FIN: NPSA Project Manager salary month 1', 2450, False, 25),
        ('staff', 'SAMPLE-FIN: Financial Analyst support month 1', 1800, False, 24),
        ('logistics', 'SAMPLE-FIN: Kiosk restock vehicle hire', 950, False, 20),
        ('utilities', 'SAMPLE-FIN: Central warehouse utilities', 620, False, 18),
        ('commodity', 'SAMPLE-FIN: Injectable commodity batch', 12500, False, 30),
        ('commodity', 'SAMPLE-FIN: Implant commodity batch', 9800, False, 28),
        ('platform_build', 'SAMPLE-FIN: Cloud hosting quarterly', 3200, True, 15),
        ('platform_build', 'SAMPLE-FIN: Security audit', 2100, True, 12),
        ('other', 'SAMPLE-FIN: Stakeholder launch event', 1400, False, 40),
        ('staff', 'SAMPLE-FIN: Training facilitator fees', 2100, False, 22),
    ]
    for cat, desc, amt, plat, days_ago in fin_rows:
        db.session.add(Expenditure(
            facility_id=None if plat else facilities[0].id,
            category=cat, description=desc, amount=Decimal(str(amt)),
            expenditure_date=now.date() - timedelta(days=days_ago),
            is_platform_cost=plat, created_by=rid,
        ))

    # Build expense requests: submitted, finance_review, pm_approved, paid, rejected
    pipeline = []
    # Awaiting Finance review
    for i, c in enumerate(codes[:3]):
        pipeline.append({
            'n': f'SAMPLE-AWF-{i+1:03d}', 'code': c, 'status': 'submitted',
            'amt': random.randint(400, 2200), 'payee': payees[i % len(payees)],
            'desc': f'SAMPLE awaiting Finance review — {c.description[:60]}',
            'days': 2 + i,
        })
    # Awaiting PM (cleared by Finance)
    for i, c in enumerate(codes[3:6]):
        pipeline.append({
            'n': f'SAMPLE-APM-{i+1:03d}', 'code': c, 'status': 'finance_review',
            'amt': random.randint(600, 3500), 'payee': payees[(i+2) % len(payees)],
            'desc': f'SAMPLE cleared by Finance, awaiting PM — {c.description[:60]}',
            'days': 5 + i, 'fin_days': 3 + i,
        })
    # PM approved — ready to pay
    for i, c in enumerate(codes[6:9]):
        pipeline.append({
            'n': f'SAMPLE-APR-{i+1:03d}', 'code': c, 'status': 'pm_approved',
            'amt': random.randint(800, 4000), 'payee': payees[(i+1) % len(payees)],
            'desc': f'SAMPLE approved by PM, ready for payment — {c.description[:60]}',
            'days': 10 + i, 'fin_days': 8 + i, 'pm_days': 6 + i,
        })
    # Paid (post to variance actuals)
    for i, c in enumerate(codes[:10]):
        pipeline.append({
            'n': f'SAMPLE-PAID-{i+1:03d}', 'code': c, 'status': 'paid',
            'amt': random.randint(500, int(float(sample_budgets.get(c.code, 5000)) * 0.35) or 1500),
            'payee': payees[i % len(payees)],
            'desc': f'SAMPLE paid and posted to budget actuals — {c.description[:60]}',
            'days': 20 + i, 'fin_days': 18 + i, 'pm_days': 15 + i, 'paid_days': 12 + i,
        })
    # Rejected samples
    if len(codes) > 10:
        c = codes[10]
        pipeline.append({
            'n': 'SAMPLE-REJ-001', 'code': c, 'status': 'finance_rejected',
            'amt': 1500, 'payee': payees[0],
            'desc': 'SAMPLE rejected by Finance — incomplete evidence',
            'days': 7, 'fin_days': 5,
        })
    if len(codes) > 11:
        c = codes[11]
        pipeline.append({
            'n': 'SAMPLE-REJ-002', 'code': c, 'status': 'pm_rejected',
            'amt': 2200, 'payee': payees[1],
            'desc': 'SAMPLE rejected by PM — outside approved activity plan',
            'days': 9, 'fin_days': 7, 'pm_days': 6,
        })

    for p in pipeline:
        c = p['code']
        er = ExpenseRequest(
            request_number=p['n'],
            requester_id=rid,
            expense_code_id=c.id,
            category=c.category,
            description=p['desc'],
            amount=Decimal(str(p['amt'])),
            currency='USD',
            payee_name=p['payee'],
            bank_name='Sample Bank PLC',
            account_number='0123456789',
            account_name=p['payee'],
            evidence_notes='SAMPLE attachment reference for demo reporting',
            status=p['status'],
            created_at=now - timedelta(days=p['days']),
        )
        if p.get('fin_days') is not None or p['status'] in ('finance_review', 'pm_approved', 'paid', 'finance_rejected', 'pm_rejected'):
            er.finance_reviewer_id = rid
            er.finance_reviewed_at = now - timedelta(days=p.get('fin_days', p['days'] - 1))
            er.finance_notes = 'SAMPLE finance review note'
        if p.get('pm_days') is not None or p['status'] in ('pm_approved', 'paid', 'pm_rejected'):
            er.pm_approver_id = rid
            er.pm_approved_at = now - timedelta(days=p.get('pm_days', p['days'] - 2))
            er.pm_notes = 'SAMPLE PM decision note'
        if p['status'] == 'paid':
            er.paid_by_id = rid
            er.paid_at = now - timedelta(days=p.get('paid_days', 5))
            er.voucher_number = f"PV-{p['n']}"
            # Mirror into ledger as SAMPLE-FIN paid claim
            db.session.add(Expenditure(
                facility_id=None,
                category=c.category,
                description=f"SAMPLE-FIN: Paid {p['n']} / {c.code}",
                amount=Decimal(str(p['amt'])),
                expenditure_date=er.paid_at.date() if er.paid_at else now.date(),
                is_platform_cost=c.code.startswith('EXP-PLT'),
                created_by=rid,
            ))
        db.session.add(er)

    # Homepage stories can stay as CMS; tag settings
    set_setting('sample_data_loaded_at', datetime.utcnow().isoformat())
    db.session.commit()
    return {'ok': True, 'msg': 'Sample report data loaded (encounters, stock, ledgers, budget, paid expenses, testimonies).'}


def clear_report_sample_data():
    """Remove records tagged as SAMPLE so live data is not mixed."""
    ClientEncounter.query.filter(ClientEncounter.client_code.like('SAMP-%')).delete(synchronize_session=False)
    Expenditure.query.filter(Expenditure.description.like('SAMPLE:%')).delete(synchronize_session=False)
    ExpenseRequest.query.filter(ExpenseRequest.request_number.like('SAMPLE-%')).delete(synchronize_session=False)
    StockTransaction.query.filter(StockTransaction.reference.in_(['SAMPLE-LOAD', 'SAMPLE-ENC'])).delete(synchronize_session=False)
    db.session.commit()
    set_setting('sample_data_loaded_at', '')
    return {'ok': True, 'msg': 'Sample-tagged data cleared. Budget lines and real records kept.'}


@app.route('/admin/sample-data', methods=['GET', 'POST'])
@login_required
@general_admin_required
def admin_sample_data():
    """Load / clear / tweak sample data used by dashboards and all reports."""
    msg = None
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'load':
            result = load_report_sample_data()
            flash(result['msg'], 'success' if result['ok'] else 'warning')
        elif action == 'clear':
            result = clear_report_sample_data()
            flash(result['msg'], 'info')
        elif action == 'save_kpis':
            # Editable KPI overlays stored in settings (optional report footnotes)
            set_setting('sample_kpi_notes', request.form.get('sample_kpi_notes', '').strip())
            set_setting('sample_report_disclaimer', request.form.get('sample_report_disclaimer', '').strip())
            flash('Report notes saved.', 'success')
        elif action == 'save_testimony':
            # Quick-add a consented testimony encounter
            fac_id = request.form.get('facility_id')
            text_t = request.form.get('testimony', '').strip()
            if fac_id and text_t:
                db.session.add(ClientEncounter(
                    facility_id=int(fac_id),
                    encounter_date=datetime.utcnow().date(),
                    outcome='accepted',
                    method_accepted=request.form.get('method', 'Injectable'),
                    client_code='SAMP-MANUAL',
                    consent_to_share_story=True,
                    patient_testimony=text_t,
                    satisfaction_score=int(request.form.get('score') or 5),
                    created_by=current_user.id,
                ))
                db.session.commit()
                flash('Testimony added for reports.', 'success')
        return redirect(url_for('admin_sample_data'))

    stats = {
        'encounters': ClientEncounter.query.count(),
        'sample_encounters': ClientEncounter.query.filter(ClientEncounter.client_code.like('SAMP-%')).count(),
        'expenditures': Expenditure.query.count(),
        'paid_expenses': ExpenseRequest.query.filter_by(status='paid').count(),
        'awaiting_finance': ExpenseRequest.query.filter_by(status='submitted').count(),
        'awaiting_pm': ExpenseRequest.query.filter_by(status='finance_review').count(),
        'approved_ready': ExpenseRequest.query.filter_by(status='pm_approved').count(),
        'budget_lines': BudgetLine.query.count(),
        'testimonies': ClientEncounter.query.filter(
            ClientEncounter.consent_to_share_story == True,
            ClientEncounter.patient_testimony.isnot(None),
        ).count(),
        'loaded_at': get_setting('sample_data_loaded_at', ''),
        'kpi_notes': get_setting('sample_kpi_notes', ''),
        'disclaimer': get_setting('sample_report_disclaimer',
                                  'Figures may include demonstration sample data for pilot reporting.'),
    }
    facilities = Facility.query.order_by(Facility.name).all()
    products = Product.query.filter_by(is_active=True).all()
    # Editable budget snapshot
    codes = ExpenseCode.query.filter_by(is_active=True).order_by(ExpenseCode.code).all()
    budget_rows = []
    for c in codes:
        bl = BudgetLine.query.filter_by(expense_code_id=c.id, fiscal_year='2026', period_label='Pilot').first()
        budget_rows.append({'code': c, 'budget': float(bl.budget_amount) if bl else 0,
                            'narration': bl.variance_narration if bl else ''})
    return render_template('admin_sample_data.html', stats=stats, facilities=facilities,
                           products=products, budget_rows=budget_rows)


@app.route('/admin/sample-data/budget', methods=['POST'])
@login_required
@general_admin_required
def admin_sample_data_budget():
    """Inline edit of pilot budget sample amounts from sample-data page."""
    codes = ExpenseCode.query.filter_by(is_active=True).all()
    for c in codes:
        amt = request.form.get(f'b_{c.id}', '').strip()
        narr = request.form.get(f'n_{c.id}', '').strip()
        try:
            budget_amt = Decimal(amt) if amt else Decimal('0')
        except Exception:
            budget_amt = Decimal('0')
        bl = BudgetLine.query.filter_by(expense_code_id=c.id, fiscal_year='2026', period_label='Pilot').first()
        if not bl:
            bl = BudgetLine(expense_code_id=c.id, fiscal_year='2026', period_label='Pilot')
            db.session.add(bl)
        bl.budget_amount = budget_amt
        bl.variance_narration = narr
        bl.updated_by = current_user.id
    db.session.commit()
    flash('Sample budget figures updated — variance report will use these values.', 'success')
    return redirect(url_for('admin_sample_data'))



# ---------------------------------------------------------------------------
# Report branding + comprehensive backup / restore
# ---------------------------------------------------------------------------
@app.route('/admin/branding', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_branding():
    """Edit programme title and upload logo used on all reports / PowerPoints."""
    if current_user.role not in ('project_manager', 'general_admin', 'admin', 'program_admin', 'finance_analyst'):
        flash('Not authorised to edit branding.', 'danger')
        return redirect(url_for('index'))
    brand = report_branding()
    if request.method == 'POST':
        set_setting('programme_title', request.form.get('programme_title', '').strip() or 'Benin City Mayor Challenge')
        set_setting('report_subtitle', request.form.get('report_subtitle', '').strip())
        set_setting('org_line', request.form.get('org_line', '').strip())
        set_setting('app_name', request.form.get('app_name', '').strip() or 'CONTRAconnect')
        f = request.files.get('logo')
        if f and f.filename:
            ext = f.filename.rsplit('.', 1)[-1].lower()
            if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
                rel = f'uploads/branding/report_logo.{ext}'
                dest_dir = os.path.join(app.root_path, 'static', 'uploads', 'branding')
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(app.root_path, 'static', rel)
                f.save(dest)
                set_setting('report_logo_path', rel)
        af = request.files.get('app_logo')
        if af and af.filename:
            ext = af.filename.rsplit('.', 1)[-1].lower()
            if ext in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
                rel = f'uploads/branding/app_logo.{ext}'
                dest_dir = os.path.join(app.root_path, 'static', 'uploads', 'branding')
                os.makedirs(dest_dir, exist_ok=True)
                af.save(os.path.join(app.root_path, 'static', rel))
                set_setting('app_logo_path', rel)
        flash('Branding settings saved. All new reports will use this heading and logo.', 'success')
        return redirect(url_for('admin_branding'))
    return render_template('admin_branding.html', brand=brand)


@app.route('/admin/backup')
@login_required
@general_admin_required
def admin_backup_page():
    return render_template('admin_backup.html')


@app.route('/admin/backup/download')
@login_required
@general_admin_required
def admin_backup_download():
    """Full backup: SQL dump of all tables + static uploads (logos, invoice evidence)."""
    import zipfile
    import json as _json
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Dump every mapped table as JSON (portable across SQLite/Postgres)
        meta = {'created_at': datetime.utcnow().isoformat() + 'Z', 'tables': {}}
        for table in db.metadata.sorted_tables:
            rows = [dict(r) for r in db.session.execute(table.select()).mappings().all()]
            # stringify non-JSON types
            clean = []
            for row in rows:
                item = {}
                for k, v in row.items():
                    if hasattr(v, 'isoformat'):
                        item[k] = v.isoformat()
                    elif isinstance(v, Decimal):
                        item[k] = str(v)
                    else:
                        item[k] = v
                clean.append(item)
            meta['tables'][table.name] = clean
            zf.writestr(f'data/{table.name}.json', _json.dumps(clean, ensure_ascii=False, indent=2))
        zf.writestr('manifest.json', _json.dumps({
            'app': 'CONTRAconnect',
            'created_at': meta['created_at'],
            'table_count': len(meta['tables']),
            'row_counts': {t: len(r) for t, r in meta['tables'].items()},
        }, indent=2))
        # Include uploaded files
        upload_root = os.path.join(app.root_path, 'static', 'uploads')
        if os.path.isdir(upload_root):
            for root, _dirs, files in os.walk(upload_root):
                for name in files:
                    full = os.path.join(root, name)
                    arc = os.path.relpath(full, app.root_path)
                    zf.write(full, arc)
        # Branding icons optional
        icons = os.path.join(app.root_path, 'static', 'icons')
        if os.path.isdir(icons):
            for name in os.listdir(icons):
                full = os.path.join(icons, name)
                if os.path.isfile(full):
                    zf.write(full, f'static/icons/{name}')
    buf.seek(0)
    fname = f'CONTRAconnect_backup_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.zip'
    return send_file(buf, as_attachment=True, download_name=fname, mimetype='application/zip')


@app.route('/admin/backup/restore', methods=['POST'])
@login_required
@general_admin_required
def admin_backup_restore():
    """Restore from a backup zip produced by this app. Replaces table data."""
    import zipfile
    import json as _json
    f = request.files.get('backup_file')
    if not f or not f.filename.endswith('.zip'):
        flash('Please upload a CONTRAconnect backup .zip file.', 'danger')
        return redirect(url_for('admin_backup_page'))
    try:
        data = f.read()
        zf = zipfile.ZipFile(BytesIO(data))
        # Restore uploads first
        for name in zf.namelist():
            if name.startswith('static/uploads/') and not name.endswith('/'):
                target = os.path.join(app.root_path, name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, 'wb') as out:
                    out.write(zf.read(name))
        # Restore tables in FK-safe order (metadata sorted_tables is dependency order)
        if not request.form.get('confirm') == 'RESTORE':
            flash('Type RESTORE in the confirmation box to proceed.', 'warning')
            return redirect(url_for('admin_backup_page'))
        # Disable FK checks where possible
        bind = db.session.get_bind()
        dialect = bind.dialect.name if bind else 'sqlite'
        if dialect == 'sqlite':
            db.session.execute(db.text('PRAGMA foreign_keys=OFF'))
        for table in reversed(list(db.metadata.sorted_tables)):
            db.session.execute(table.delete())
        db.session.commit()
        for table in db.metadata.sorted_tables:
            path_in_zip = f'data/{table.name}.json'
            if path_in_zip not in zf.namelist():
                continue
            rows = _json.loads(zf.read(path_in_zip))
            if not rows:
                continue
            # Insert in batches
            db.session.execute(table.insert(), rows)
        db.session.commit()
        if dialect == 'sqlite':
            db.session.execute(db.text('PRAGMA foreign_keys=ON'))
            db.session.commit()
        flash('Backup restored successfully. Please sign in again if sessions were reset.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.exception('Restore failed')
        flash(f'Restore failed: {e}', 'danger')
    return redirect(url_for('admin_backup_page'))



# ---------------------------------------------------------------------------
# API endpoints for charts (JSON)
# ---------------------------------------------------------------------------
@app.route('/api/cost-metrics')
@login_required
def api_cost_metrics():
    days = int(request.args.get('days', 90))
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)
    return jsonify(compute_cost_metrics(start, end))


@app.route('/api/method-uptake')
@login_required
def api_method_uptake():
    days = int(request.args.get('days', 90))
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)
    return jsonify(get_method_uptake(start, end))


# ---------------------------------------------------------------------------
# Seed data for demo
# ---------------------------------------------------------------------------


def ensure_expense_codes():
    if ExpenseCode.query.first():
        return
    sample_codes = [
        ('EXP-STAFF-01', 'Staff', 'Field staff wage subsidies and stipends'),
        ('EXP-STAFF-02', 'Staff', 'Consultant professional fees'),
        ('EXP-TRAIN-01', 'Training', 'Induction and clinical refresher training'),
        ('EXP-TRAIN-02', 'Training', 'Quality of care review meetings'),
        ('EXP-LOG-01', 'Logistics', 'Commodity distribution — vehicle hire and fuel'),
        ('EXP-LOG-02', 'Logistics', 'Central storage rental and cold-chain utilities'),
        ('EXP-SBC-01', 'Demand generation', 'Community dialogues and stakeholder advocacy'),
        ('EXP-SBC-02', 'Demand generation', 'IEC materials, radio and digital campaigns'),
        ('EXP-COMM-01', 'Commodities', 'Family planning commodities (LARC and short-acting)'),
        ('EXP-COMM-02', 'Commodities', 'Medical consumables and IPC supplies'),
        ('EXP-EQP-01', 'Equipment', 'Mobile kiosk fabrication and clinical equipment'),
        ('EXP-EQP-02', 'Equipment', 'Field IT devices and maintenance'),
        ('EXP-OPS-01', 'Office operations', 'Utilities, internet, stationery and security'),
        ('EXP-PLT-01', 'Platform', 'Digital platform hosting, security and support'),
        ('EXP-VND-01', 'Vendor', 'Vendor administrative service charge'),
    ]
    for code, cat, desc in sample_codes:
        db.session.add(ExpenseCode(code=code, category=cat, description=desc, is_active=True))
    db.session.commit()

def seed_data():
    """
    Bootstrap master data only — no dummy encounters/expenditures in production.
    Creates:
      - 1 General Admin (password from ADMIN_PASSWORD or default strong password)
      - Product catalogue
      - 3 empty facilities ready to assign to real providers (no fake clients/costs)
    Set SEED_DEMO_DATA=1 to also load sample encounters for training demos.
    """
    if User.query.filter(User.role.in_(['general_admin', 'admin'])).first():
        return

    admin_pw = os.environ.get('ADMIN_PASSWORD', 'Contra@Admin2026!')
    admin = User(
        email=os.environ.get('ADMIN_EMAIL', 'admin@contraconnect.local'),
        full_name='General Administrator',
        role='general_admin',
        is_active=True,
        onboarding_status='active',
        must_complete_onboarding=False,
        role_confirmed=True,
        ethics_accepted=True,
    )
    admin.set_password(admin_pw)
    db.session.add(admin)

    products = [
        Product(name='Copper IUD', method_code='IUD', unit='piece', unit_cost=Decimal('8.50')),
        Product(name='Levonorgestrel Implant', method_code='Implant', unit='set', unit_cost=Decimal('18.00')),
        Product(name='Injectable (DMPA)', method_code='Injectable', unit='vial', unit_cost=Decimal('2.20')),
        Product(name='Combined Oral Contraceptive', method_code='Pill', unit='cycle', unit_cost=Decimal('0.85')),
        Product(name='Male Condom', method_code='Condom', unit='piece', unit_cost=Decimal('0.12')),
        Product(name='Emergency Contraceptive', method_code='EC', unit='pack', unit_cost=Decimal('1.50')),
    ]
    db.session.add_all(products)

    # Three real-world-ready facilities (no dummy users — assign later via registration or admin)
    fac1 = Facility(
        name='Central Market Kiosk', facility_type='kiosk', address='Market Road',
        city='Pilot City', contact_person='', phone='', target_clients_monthly=120, is_active=True,
    )
    fac2 = Facility(
        name='PHC Riverside', facility_type='phc', address='Riverside Avenue',
        city='Pilot City', contact_person='', phone='', target_clients_monthly=300, is_active=True,
    )
    fac3 = Facility(
        name='Youth Hub Kiosk', facility_type='kiosk', address='Campus Gate',
        city='Pilot City', contact_person='', phone='', target_clients_monthly=80, is_active=True,
    )
    db.session.add_all([fac1, fac2, fac3])
    db.session.commit()

    # Programme staff accounts (change passwords after first login)
    staff_pw = os.environ.get('STAFF_PASSWORD', 'Staff@2026!')
    staff = [
        ('pm@contraconnect.local', 'Project Manager', 'project_manager', 'Project Manager NPSA 10'),
        ('finance@contraconnect.local', 'Financial Analyst', 'finance_analyst', 'Financial Analyst NPSA 8'),
        ('rh@contraconnect.local', 'RH Consultant', 'rh_consultant', 'Reproductive Health Consultant'),
        ('mel@contraconnect.local', 'MEL Consultant', 'mel_consultant', 'Monitoring, Evaluation and Learning Consultant'),
        ('demand@contraconnect.local', 'Demand Generation Consultant', 'demand_consultant', 'Demand Generation Consultant'),
        ('sdoc@contraconnect.local', 'SDOC Consultant', 'sdoc_consultant', 'Service Delivery Operations Coordination Consultant'),
        ('logistics@contraconnect.local', 'Logistics Consultant', 'logistics_consultant', 'Logistics & Supply Consultant'),
    ]
    for email, name, role, title in staff:
        if not User.query.filter_by(email=email).first():
            u = User(
                email=email, full_name=name, role=role, staff_title=title, is_active=True,
                onboarding_status='active', must_complete_onboarding=False,
                role_confirmed=True, ethics_accepted=True,
            )
            u.set_password(staff_pw)
            db.session.add(u)
    db.session.commit()

    # Optional demo data only when explicitly enabled
    if os.environ.get('SEED_DEMO_DATA', '').strip() in ('1', 'true', 'yes'):
        _seed_demo_activity(admin, products, [fac1, fac2, fac3])
    print('Seed data created (clean facilities + catalogue + general admin).')



    # Sample expense codes (chart of accounts)
    if not ExpenseCode.query.first():
        sample_codes = [
            ('EXP-STAFF-01', 'Staff', 'Field staff wage subsidies and stipends'),
            ('EXP-STAFF-02', 'Staff', 'Consultant professional fees'),
            ('EXP-TRAIN-01', 'Training', 'Induction and clinical refresher training'),
            ('EXP-TRAIN-02', 'Training', 'Quality of care review meetings'),
            ('EXP-LOG-01', 'Logistics', 'Commodity distribution — vehicle hire and fuel'),
            ('EXP-LOG-02', 'Logistics', 'Central storage rental and cold-chain utilities'),
            ('EXP-SBC-01', 'Demand generation', 'Community dialogues and stakeholder advocacy'),
            ('EXP-SBC-02', 'Demand generation', 'IEC materials, radio and digital campaigns'),
            ('EXP-COMM-01', 'Commodities', 'Family planning commodities (LARC and short-acting)'),
            ('EXP-COMM-02', 'Commodities', 'Medical consumables and IPC supplies'),
            ('EXP-EQP-01', 'Equipment', 'Mobile kiosk fabrication and clinical equipment'),
            ('EXP-EQP-02', 'Equipment', 'Field IT devices and maintenance'),
            ('EXP-OPS-01', 'Office operations', 'Utilities, internet, stationery and security'),
            ('EXP-PLT-01', 'Platform', 'Digital platform hosting, security and support'),
            ('EXP-VND-01', 'Vendor', 'Vendor administrative service charge'),
        ]
        for code, cat, desc in sample_codes:
            db.session.add(ExpenseCode(code=code, category=cat, description=desc, is_active=True))
        db.session.commit()


def _seed_demo_activity(admin, products, facilities):
    """Optional demo encounters — off by default so real data is not polluted."""
    import random
    fac1, fac2 = facilities[0], facilities[1]
    prov_pw = os.environ.get('PROVIDER_PASSWORD', 'Provider@2026')
    p1 = User(email='kiosk1@contraconnect.local', full_name='Demo Kiosk User', role='provider',
              facility_id=fac1.id, is_active=True)
    p1.set_password(prov_pw)
    p2 = User(email='phc1@contraconnect.local', full_name='Demo PHC User', role='provider',
              facility_id=fac2.id, is_active=True)
    p2.set_password(prov_pw)
    db.session.add_all([p1, p2])
    fac1.contact_person = p1.full_name
    fac2.contact_person = p2.full_name
    for fac in (fac1, fac2):
        for prod in products:
            qty = 50 if prod.method_code != 'Condom' else 500
            db.session.add(StockItem(facility_id=fac.id, product_id=prod.id, quantity_on_hand=qty, reorder_level=15))
            db.session.add(StockTransaction(
                facility_id=fac.id, product_id=prod.id, transaction_type='receipt',
                quantity=qty, unit_cost=prod.unit_cost, reference='DEMO-SEED', created_by=admin.id,
            ))
    db.session.commit()
    print('Demo activity seeded (SEED_DEMO_DATA=1).')



# ---------------------------------------------------------------------------
# CLI / startup
# ---------------------------------------------------------------------------
@app.cli.command('init-db')
def init_db():
    """Initialize the database and seed demo data."""
    db.create_all()
    seed_data()
    print('Database initialized.')



@app.route('/.well-known/assetlinks.json')
def assetlinks():
    """Digital Asset Links for Android TWA / Play Store. Replace package_name and sha256 after you build the Android package."""
    import json
    data = [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": os.environ.get('ANDROID_PACKAGE_NAME', 'com.contraconnect.app'),
            "sha256_cert_fingerprints": [
                os.environ.get('ANDROID_SHA256', 'REPLACE_WITH_YOUR_UPLOAD_KEY_SHA256')
            ]
        }
    }]
    return app.response_class(json.dumps(data), mimetype='application/json')


@app.route('/static/manifest.json')
def web_manifest():
    return app.send_static_file('manifest.json')


application = app  # WSGI alias for gunicorn / Render


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        seed_data()
    app.run(debug=True, host='0.0.0.0', port=5000)
