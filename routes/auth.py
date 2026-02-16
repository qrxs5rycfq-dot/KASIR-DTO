import hashlib
import hmac
import re
import time

from flask import Blueprint, render_template, jsonify, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user

from extensions import csrf, limiter
from models import db, User, Role, Branch
from utils import utc_now, validate_token

auth_bp = Blueprint('auth', __name__, url_prefix='')


@auth_bp.route('/api/auth/token', methods=['POST'])
@csrf.exempt
@limiter.limit("10 per minute")
def api_auth_token():
    """Authenticate Android print service and return session token.
    Expects JSON: {username, password, device_name}"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'JSON required'}), 400

    username = data.get('username', '')
    password = data.get('password', '')
    device_name = data.get('device_name', 'Android Print Service')
    requested_role = data.get('role', '')  # Optional: client can request specific role

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

    # Check branch active
    if user.branch_id:
        branch = db.session.get(Branch, user.branch_id)
        if branch and not branch.is_active:
            return jsonify({'success': False, 'error': 'Branch is inactive'}), 403

    # Check if user has requested role (if specified)
    if requested_role and not user.has_role(requested_role):
        return jsonify({'success': False, 'error': f'User does not have role: {requested_role}'}), 403

    # Generate a simple token (HMAC of user_id + timestamp)
    timestamp = str(int(time.time()))
    token_data = f"{user.id}:{timestamp}"
    from flask import current_app
    secret = current_app.config.get('SECRET_KEY')
    if not secret:
        return jsonify({'success': False, 'error': 'Server configuration error'}), 500
    token = hmac.new(secret.encode(), token_data.encode(), hashlib.sha256).hexdigest()

    permissions = set()
    for role in user.roles:
        for permission in role.permissions:
            permissions.add(permission.name)

    return jsonify({
        'success': True,
        'token': f"{user.id}:{timestamp}:{token}",
        'user': {
            'id': user.id,
            'username': user.username,
            'branch_id': user.branch_id,
            'role': user.roles[0].name if user.roles else None,
            'roles': [r.name for r in user.roles],
            'permissions': list(permissions)
        },
        'device_name': device_name
    })


@auth_bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('views.dashboard'))
    return redirect(url_for('auth.login'))


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        # Check if force password change is required
        if hasattr(current_user, 'force_password_change') and current_user.force_password_change:
            return redirect(url_for('auth.change_password'))
        return redirect(url_for('views.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = request.form.get('remember', False)

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            if not user.is_active:
                flash('Akun Anda telah dinonaktifkan. Hubungi administrator.', 'danger')
                return render_template('auth/login.html')

            # Check if user's branch is active (branch users only)
            if user.branch_id:
                user_branch = db.session.get(Branch, user.branch_id)
                if not user_branch:
                    flash('Cabang yang ditugaskan tidak ditemukan. Hubungi owner/admin pusat.', 'danger')
                    return render_template('auth/login.html')
                if not user_branch.is_active:
                    flash(f'Cabang "{user_branch.name}" sedang nonaktif. Hubungi owner/admin pusat.', 'danger')
                    return render_template('auth/login.html')

            login_user(user, remember=remember)
            user.last_login = utc_now()
            db.session.commit()

            # Check if force password change is required
            if hasattr(user, 'force_password_change') and user.force_password_change:
                flash('Silakan ganti password default Anda untuk keamanan.', 'warning')
                return redirect(url_for('auth.change_password'))

            next_page = request.args.get('next')
            flash(f'Selamat datang, {user.full_name or user.username}!', 'success')

            # Redirect koki to kitchen display
            if user.has_role('koki') and not user.has_role('admin'):
                return redirect(next_page or url_for('views.kitchen'))

            return redirect(next_page or url_for('views.dashboard'))
        else:
            flash('Username atau password salah!', 'danger')

    return render_template('auth/login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('views.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        full_name = request.form.get('full_name')

        # Validation
        if User.query.filter_by(username=username).first():
            flash('Username sudah digunakan!', 'danger')
            return render_template('auth/register.html')

        if User.query.filter_by(email=email).first():
            flash('Email sudah digunakan!', 'danger')
            return render_template('auth/register.html')

        if password != confirm_password:
            flash('Password tidak cocok!', 'danger')
            return render_template('auth/register.html')

        if len(password) < 6:
            flash('Password minimal 6 karakter!', 'danger')
            return render_template('auth/register.html')

        # Create user
        customer_role = Role.query.filter_by(name='customer').first()
        user = User(
            username=username,
            email=email,
            full_name=full_name
        )
        user.set_password(password)
        if customer_role:
            user.roles.append(customer_role)

        db.session.add(user)
        db.session.commit()

        flash('Registrasi berhasil! Silakan login.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('auth/register.html')


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Anda telah logout.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Force password change page for first-time login or security requirements"""
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        # Validate current password
        if not current_user.check_password(current_password):
            flash('Password saat ini salah!', 'danger')
            return render_template('auth/change_password.html')

        # Validate new password
        if len(new_password) < 8:
            flash('Password baru minimal 8 karakter!', 'danger')
            return render_template('auth/change_password.html')

        if new_password != confirm_password:
            flash('Password baru tidak cocok!', 'danger')
            return render_template('auth/change_password.html')

        # Check password strength (at least 1 uppercase, 1 lowercase, 1 number)
        if not re.search(r'[A-Z]', new_password):
            flash('Password harus mengandung minimal 1 huruf besar!', 'danger')
            return render_template('auth/change_password.html')
        if not re.search(r'[a-z]', new_password):
            flash('Password harus mengandung minimal 1 huruf kecil!', 'danger')
            return render_template('auth/change_password.html')
        if not re.search(r'[0-9]', new_password):
            flash('Password harus mengandung minimal 1 angka!', 'danger')
            return render_template('auth/change_password.html')

        # Update password
        current_user.set_password(new_password)
        current_user.force_password_change = False
        db.session.commit()

        flash('Password berhasil diubah!', 'success')
        return redirect(url_for('views.dashboard'))

    return render_template('auth/change_password.html')


@auth_bp.route('/api/fcm/register', methods=['POST'])
@login_required
def api_register_fcm_token():
    """Register FCM token for push notifications"""
    data = request.json
    token = data.get('fcm_token', '').strip()
    if not token:
        return jsonify({'success': False, 'error': 'FCM token is required'}), 400

    current_user.fcm_token = token
    db.session.commit()
    return jsonify({'success': True, 'message': 'FCM token registered'})
