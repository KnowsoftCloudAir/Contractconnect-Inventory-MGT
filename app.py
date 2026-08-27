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
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak
from reportlab.lib.units import inch
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
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

try:
    os.makedirs(app.instance_path, exist_ok=True)
except Exception:
    pass

db = SQLAlchemy(app)
login_manager = LoginManager(app)
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
        _db_ready = True
    except Exception as e:
        app.logger.exception('ensure_db failed: %s', e)


@app.before_request
def _before_request_init_db():
    ensure_db()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(40), nullable=False, default='provider')  # provider | general_admin | program_admin | finance_admin
    facility_id = db.Column(db.Integer, db.ForeignKey('facilities.id'), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    facility = db.relationship('Facility', back_populates='users')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


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


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# Admin role hierarchy
ADMIN_ROLES = ('general_admin', 'program_admin', 'finance_admin', 'admin')  # 'admin' legacy = general
FINANCE_ROLES = ('general_admin', 'finance_admin', 'admin')
PROGRAM_ROLES = ('general_admin', 'program_admin', 'admin')  # ops without restricting finance view for program


def _is_admin_role(role):
    return role in ADMIN_ROLES


def _can_export_financial(role):
    return role in FINANCE_ROLES or role == 'admin'


def _can_manage_admins(role):
    return role in ('general_admin', 'admin')


def admin_required(f):
    """Any admin role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not _is_admin_role(current_user.role):
            flash('Admin access required.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def general_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not _can_manage_admins(current_user.role):
            flash('General Admin access required.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def finance_access_required(f):
    """Finance dashboard + financial downloads: General Admin and Finance Admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not _can_export_financial(current_user.role):
            flash('Finance access required. Program Admins cannot download financial data.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


def program_ops_required(f):
    """Program operations (facilities, stock, encounters): General + Program admin (not Finance-only)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))
        if current_user.role in ('general_admin', 'program_admin', 'admin'):
            return f(*args, **kwargs)
        if current_user.role == 'finance_admin':
            flash('Program operations are not available to Finance Admin.', 'warning')
            return redirect(url_for('admin_costs'))
        flash('Admin access required.', 'danger')
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
    if current_user.is_authenticated:
        if _is_admin_role(current_user.role):
            if current_user.role == 'finance_admin':
                return redirect(url_for('admin_costs'))
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('provider_dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email, is_active=True).first()
        if user and user.check_password(password):
            if _is_admin_role(user.role):
                flash('Please use the authorised admin access page.', 'warning')
                return redirect(url_for('admin_access'))
            login_user(user, remember=True)
            flash(f'Welcome back, {user.full_name}!', 'success')
            return redirect(url_for('index'))
        flash('Invalid email or password.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
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
            flash(f'Welcome, {user.full_name} ({user.role.replace("_", " ").title()}).', 'success')
            if user.role == 'finance_admin':
                return redirect(url_for('admin_costs'))
            return redirect(url_for('admin_dashboard'))
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
    """Monthly / quarterly / yearly report as pptx or pdf."""
    if period not in ('monthly', 'quarterly', 'yearly'):
        abort(404)
    if fmt not in ('pptx', 'pdf'):
        abort(404)
    # Finance-only admins can get reports; program admins can too (ops + charts)
    start, end, label = _period_bounds(period)
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
        fac_rows.append({
            'name': f.name, 'type': f.facility_type, 'active': f.is_active,
            'clients': clients, 'contact': f.contact_person or '',
        })

    with tempfile.TemporaryDirectory() as tmpdir:
        charts = _make_chart_images(cost_data, uptake, tmpdir)
        if fmt == 'pptx':
            return _build_pptx_report(label, period, cost_data, uptake, fac_rows, charts, start, end)
        return _build_pdf_report(label, period, cost_data, uptake, fac_rows, charts, start, end)


def _build_pptx_report(label, period, cost_data, uptake, fac_rows, charts, start, end):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    def add_title_slide(title, subtitle):
        layout = prs.slide_layouts[6]  # blank
        slide = prs.slides.add_slide(layout)
        box = slide.shapes.add_textbox(Inches(0.8), Inches(2.5), Inches(11.5), Inches(1.5))
        tf = box.text_frame
        p = tf.paragraphs[0]
        p.text = title
        p.font.size = Pt(32)
        p.font.bold = True
        p.font.color.rgb = RGBColor(0x0D, 0x6E, 0x6E)
        box2 = slide.shapes.add_textbox(Inches(0.8), Inches(4.0), Inches(11.5), Inches(1))
        box2.text_frame.paragraphs[0].text = subtitle
        box2.text_frame.paragraphs[0].font.size = Pt(16)

    def add_section(title):
        layout = prs.slide_layouts[6]
        slide = prs.slides.add_slide(layout)
        box = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(12), Inches(0.6))
        box.text_frame.paragraphs[0].text = title
        box.text_frame.paragraphs[0].font.size = Pt(22)
        box.text_frame.paragraphs[0].font.bold = True
        return slide

    add_title_slide(
        f'CONTRAconnect {period.title()} Report',
        f'{label}  |  {start.isoformat()} → {end.isoformat()}  |  Generated {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} UTC'
    )

    # Dashboard KPIs
    slide = add_section('Admin dashboard — key indicators')
    kpis = [
        f"Clients served: {cost_data['total_clients']}",
        f"Operational cost: {cost_data['total_operational_cost']:,.2f}",
        f"Platform build (separate): {cost_data['platform_build_cost']:,.2f}",
        f"Unit cost / client: {cost_data['unit_cost_per_client']:,.2f}",
    ]
    box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), Inches(12), Inches(2))
    tf = box.text_frame
    tf.word_wrap = True
    for i, line in enumerate(kpis):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = '• ' + line
        p.font.size = Pt(18)
    if charts.get('uptake'):
        slide.shapes.add_picture(charts['uptake'], Inches(0.5), Inches(3.5), width=Inches(6))
    if charts.get('kiosk'):
        slide.shapes.add_picture(charts['kiosk'], Inches(6.8), Inches(3.5), width=Inches(6))

    # Cost analysis
    slide = add_section('Cost analysis')
    box = slide.shapes.add_textbox(Inches(0.5), Inches(1.0), Inches(12), Inches(1.5))
    tf = box.text_frame
    tf.paragraphs[0].text = (
        f"Unit cost per client = total operational cost ÷ clients served. "
        f"Platform build costs are tracked separately and excluded from unit cost."
    )
    tf.paragraphs[0].font.size = Pt(14)
    if charts.get('method'):
        slide.shapes.add_picture(charts['method'], Inches(0.5), Inches(2.5), width=Inches(5))
    if charts.get('kiosk'):
        slide.shapes.add_picture(charts['kiosk'], Inches(6.5), Inches(2.5), width=Inches(6))

    # Facilities summary
    slide = add_section('Facilities summary')
    rows = [['Facility', 'Type', 'Active', 'Clients in period', 'Contact']]
    for r in fac_rows:
        rows.append([r['name'], r['type'], 'Yes' if r['active'] else 'No', str(r['clients']), r['contact']])
    # simple text table
    box = slide.shapes.add_textbox(Inches(0.5), Inches(1.1), Inches(12), Inches(5.5))
    tf = box.text_frame
    tf.word_wrap = True
    for i, row in enumerate(rows[:18]):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = ' | '.join(row)
        p.font.size = Pt(12 if i else 13)
        p.font.bold = (i == 0)

    buf = BytesIO()
    prs.save(buf)
    buf.seek(0)
    fname = f'CONTRAconnect_{period}_{label.replace(" ", "_")}.pptx'
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.presentationml.presentation')


def _build_pdf_report(label, period, cost_data, uptake, fac_rows, charts, start, end):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=0.6*inch, rightMargin=0.6*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('T', parent=styles['Heading1'], textColor=colors.HexColor('#0d6e6e'))
    story = []
    story.append(Paragraph(f'CONTRAconnect {period.title()} Report — {label}', title_style))
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

    # Optional demo data only when explicitly enabled
    if os.environ.get('SEED_DEMO_DATA', '').strip() in ('1', 'true', 'yes'):
        _seed_demo_activity(admin, products, [fac1, fac2, fac3])
    print('Seed data created (clean facilities + catalogue + general admin).')


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


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        seed_data()
    app.run(debug=True, host='0.0.0.0', port=5000)
