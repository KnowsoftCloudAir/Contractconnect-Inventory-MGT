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
    jsonify, abort, session
)
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
        if not User.query.filter_by(email='admin@contraconnect.local').first():
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
    role = db.Column(db.String(20), nullable=False, default='provider')  # admin | provider
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


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash('Admin access required.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def provider_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ('admin', 'provider'):
            flash('Login required.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
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
        if current_user.role == 'admin':
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
            if user.role == 'admin':
                # Do not allow admin via public provider login
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
        user = User.query.filter_by(email=email, is_active=True, role='admin').first()
        if user and user.check_password(password):
            login_user(user, remember=False)  # do not persist admin session long-term
            flash(f'Welcome, {user.full_name}.', 'success')
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

        require_strong = current_user.role == 'admin'
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
    return redirect(url_for('admin_facilities'))


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
    if User.query.filter_by(email='admin@contraconnect.local').first():
        return

    admin = User(email='admin@contraconnect.local', full_name='System Administrator', role='admin', is_active=True)
    admin.set_password(os.environ.get('ADMIN_PASSWORD', 'Contra@Admin2026!'))
    db.session.add(admin)

    # Sample products
    products = [
        Product(name='Copper IUD', method_code='IUD', unit='piece', unit_cost=Decimal('8.50')),
        Product(name='Levonorgestrel Implant', method_code='Implant', unit='set', unit_cost=Decimal('18.00')),
        Product(name='Injectable (DMPA)', method_code='Injectable', unit='vial', unit_cost=Decimal('2.20')),
        Product(name='Combined Oral Contraceptive', method_code='Pill', unit='cycle', unit_cost=Decimal('0.85')),
        Product(name='Male Condom', method_code='Condom', unit='piece', unit_cost=Decimal('0.12')),
        Product(name='Emergency Contraceptive', method_code='EC', unit='pack', unit_cost=Decimal('1.50')),
    ]
    db.session.add_all(products)

    # Sample facilities
    fac1 = Facility(name='Central Market Kiosk', facility_type='kiosk', address='Market Road', contact_person='Amina Yusuf', phone='08012345678', target_clients_monthly=120, is_active=True)
    fac2 = Facility(name='PHC Riverside', facility_type='phc', address='Riverside Avenue', contact_person='Dr. Okonkwo', phone='08098765432', target_clients_monthly=300, is_active=True)
    fac3 = Facility(name='Youth Hub Kiosk', facility_type='kiosk', address='Campus Gate', contact_person='Chinedu Eze', phone='08055554444', target_clients_monthly=80, is_active=True)
    db.session.add_all([fac1, fac2, fac3])
    db.session.flush()

    # Provider users
    p1 = User(email='kiosk1@contraconnect.local', full_name='Amina Yusuf', role='provider', facility_id=fac1.id, is_active=True)
    p1.set_password(os.environ.get('PROVIDER_PASSWORD', 'Provider@2026'))
    p2 = User(email='phc1@contraconnect.local', full_name='Dr. Okonkwo', role='provider', facility_id=fac2.id, is_active=True)
    p2.set_password(os.environ.get('PROVIDER_PASSWORD', 'Provider@2026'))
    db.session.add_all([p1, p2])

    db.session.commit()

    # Initial stock for fac1 & fac2
    for fac in [fac1, fac2]:
        for prod in products:
            qty = 50 if prod.method_code != 'Condom' else 500
            stock = StockItem(facility_id=fac.id, product_id=prod.id, quantity_on_hand=qty, reorder_level=15)
            db.session.add(stock)
            tx = StockTransaction(
                facility_id=fac.id, product_id=prod.id, transaction_type='receipt',
                quantity=qty, unit_cost=prod.unit_cost, reference='SEED', created_by=admin.id
            )
            db.session.add(tx)

    # Sample expenditures
    today = datetime.utcnow().date()
    exps = [
        Expenditure(facility_id=fac1.id, category='staff', description='Kiosk attendant stipend', amount=Decimal('25000'), expenditure_date=today - timedelta(days=10), created_by=admin.id),
        Expenditure(facility_id=fac1.id, category='logistics', description='Last-mile delivery', amount=Decimal('8500'), expenditure_date=today - timedelta(days=5), created_by=admin.id),
        Expenditure(facility_id=fac2.id, category='staff', description='Nurse overtime', amount=Decimal('42000'), expenditure_date=today - timedelta(days=8), created_by=admin.id),
        Expenditure(facility_id=None, category='platform_build', description='Platform development phase 1', amount=Decimal('1500000'), expenditure_date=today - timedelta(days=60), is_platform_cost=True, created_by=admin.id),
        Expenditure(facility_id=None, category='logistics', description='Central warehouse rent share', amount=Decimal('35000'), expenditure_date=today - timedelta(days=15), created_by=admin.id),
        Expenditure(facility_id=fac1.id, category='commodity', description='Buffer stock purchase', amount=Decimal('18000'), expenditure_date=today - timedelta(days=20), created_by=admin.id),
    ]
    db.session.add_all(exps)

    # Sample encounters for uptake & cost demos
    import random
    methods = ['Copper IUD', 'Levonorgestrel Implant', 'Injectable (DMPA)', 'Combined Oral Contraceptive', 'Male Condom']
    reasons = ['Side-effect concerns', 'Partner opposition', 'Wants to conceive soon', 'Religious reasons', 'Prefers traditional method', 'Cost concern']
    for i in range(45):
        fac = random.choice([fac1, fac2])
        outcome = random.choices(['accepted', 'refused', 'discontinued'], weights=[0.65, 0.25, 0.10])[0]
        method = random.choice(methods)
        prod = next((p for p in products if p.name == method), products[0])
        qty = 1 if outcome == 'accepted' and prod.method_code != 'Condom' else (random.randint(3, 12) if outcome == 'accepted' else 0)
        enc = ClientEncounter(
            facility_id=fac.id,
            encounter_date=today - timedelta(days=random.randint(1, 80)),
            client_age_band=random.choice(['<20', '20-24', '25-29', '30-34', '35+']),
            client_parity=random.choice(['0', '1-2', '3+']),
            education_level=random.choice(['None', 'Primary', 'Secondary', 'Tertiary']),
            method_offered=method,
            method_accepted=method if outcome == 'accepted' else None,
            outcome=outcome,
            refusal_reason=random.choice(reasons) if outcome == 'refused' else None,
            discontinuation_reason=random.choice(reasons) if outcome == 'discontinued' else None,
            quantity_dispensed=qty,
            product_id=prod.id if outcome == 'accepted' else None,
            created_by=p1.id
        )
        db.session.add(enc)
        if outcome == 'accepted' and qty > 0:
            stock = StockItem.query.filter_by(facility_id=fac.id, product_id=prod.id).first()
            if stock and stock.quantity_on_hand >= qty:
                stock.quantity_on_hand -= qty
                tx = StockTransaction(
                    facility_id=fac.id, product_id=prod.id, transaction_type='issue',
                    quantity=-qty, unit_cost=prod.unit_cost, reference='SEED-ENC', created_by=p1.id
                )
                db.session.add(tx)

    db.session.commit()
    print('Seed data created successfully.')


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
