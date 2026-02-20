from datetime import datetime, timezone
from functools import wraps
from flask import redirect, url_for, flash, current_app
from flask_login import current_user
from werkzeug.utils import secure_filename
from models import db, User, Branch, BranchMenuStock, City, Brand
import hashlib
import hmac
import os
import time
import uuid


def utc_now():
    """Return current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


def format_number_filter(value):
    """Format angka dengan pemisah ribuan"""
    try:
        return f"{int(value):,}".replace(",", ".")
    except (ValueError, TypeError):
        return value


def format_currency(value):
    """Format sebagai mata uang Rupiah"""
    try:
        return f"Rp {int(value):,}".replace(",", ".")
    except (ValueError, TypeError):
        return value


def get_user_branch_id():
    """Get the current user's branch_id. Returns None for admin/owner (sees all)."""
    if not current_user.is_authenticated:
        return None
    # Admin always sees all branches regardless of DB branch_id
    if current_user.has_role('admin'):
        return None
    return current_user.branch_id


def get_default_branch_id():
    """Get branch_id for data creation. Returns user's branch or Pusat branch for admin/owner.
    Ensures records always have a valid branch_id."""
    bid = get_user_branch_id()
    if bid is not None:
        return bid
    # Admin/owner: default to Pusat branch
    pusat = Branch.query.filter_by(code='PUSAT').first()
    return pusat.id if pusat else None


def branch_filter(query, model):
    """Apply branch filter to a query. Admin/owner (branch_id=NULL) sees all data."""
    bid = get_user_branch_id()
    if bid is not None:
        return query.filter(model.branch_id == bid)
    return query


def get_branch_stock(menu_item_id, branch_id):
    """Get BranchMenuStock for a menu item at a specific branch. Creates default if missing."""
    if branch_id is None:
        return None
    bms = BranchMenuStock.query.filter_by(branch_id=branch_id, menu_item_id=menu_item_id).first()
    if not bms:
        bms = BranchMenuStock(branch_id=branch_id, menu_item_id=menu_item_id, stock=100, is_available=True)
        db.session.add(bms)
        db.session.flush()
    return bms


def get_menu_with_branch_stock(menu_items, branch_id):
    """Attach per-branch stock/availability to menu item dicts. Returns list of dicts."""
    if branch_id is None:
        # Owner sees global data – use MenuItem's own stock as fallback
        return [item.to_dict() for item in menu_items]
    
    # Batch-load all branch stock for this branch
    item_ids = [item.id for item in menu_items]
    stocks = {bms.menu_item_id: bms for bms in
              BranchMenuStock.query.filter(
                  BranchMenuStock.branch_id == branch_id,
                  BranchMenuStock.menu_item_id.in_(item_ids)
              ).all()} if item_ids else {}
    
    result = []
    for item in menu_items:
        d = item.to_dict()
        bms = stocks.get(item.id)
        if bms:
            d['stock'] = bms.stock
            d['is_available'] = bms.is_available
        else:
            d['stock'] = 100  # default
            d['is_available'] = True
        result.append(d)
    return result


def permission_required(permission):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if not current_user.has_permission(permission):
                flash('Anda tidak memiliki akses ke halaman ini.', 'danger')
                return redirect(url_for('views.dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if not any(current_user.has_role(role) for role in roles):
                flash('Anda tidak memiliki akses ke halaman ini.', 'danger')
                return redirect(url_for('views.dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def allowed_file(filename):
    """Check if file extension is allowed"""
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_image(file):
    """Save uploaded image and return the relative path"""
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        # Generate unique filename
        ext = filename.rsplit('.', 1)[1].lower()
        unique_filename = f"{uuid.uuid4().hex}.{ext}"
        
        upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
        os.makedirs(upload_folder, exist_ok=True)
        
        filepath = os.path.join(upload_folder, unique_filename)
        file.save(filepath)
        return f"/uploads/{unique_filename}"
    return None


def get_setting(key, default=None):
    from models import Setting
    s = Setting.query.filter_by(key=key).first()
    return s.value if s else default


def get_gateway_config(key, env_key=None):
    """Get payment gateway config: DB setting first, then env var fallback."""
    db_val = get_setting(f'pg_{key}')
    if db_val:
        return db_val
    return os.environ.get(env_key or key.upper(), '')


def set_setting(key, value, description=None):
    from models import Setting
    s = Setting.query.filter_by(key=key).first()
    if s:
        s.value = value
        if description:
            s.description = description
    else:
        s = Setting(key=key, value=value, description=description or '')
        db.session.add(s)
    db.session.commit()


def create_notification(type, title, message, user_id=None, data=None, target_roles=None):
    """Create a notification for a user or broadcast, and send FCM push to relevant users"""
    from models import Notification, User, Role
    import json
    notification = Notification(
        type=type,
        title=title,
        message=message,
        user_id=user_id,
        data=json.dumps(data) if data else None
    )
    db.session.add(notification)
    db.session.commit()

    # Send FCM push notification to relevant users
    try:
        _send_fcm_push(title, message, data, user_id=user_id, target_roles=target_roles)
    except Exception:
        # FCM is best-effort, don't break notification creation on failure
        import logging
        logging.getLogger(__name__).warning('FCM push failed', exc_info=True)

    return notification


def _send_fcm_push(title, message, data=None, user_id=None, target_roles=None):
    """Send FCM push notification using Firebase Admin SDK with service account JSON"""
    from models import User, Role
    import logging
    logger = logging.getLogger(__name__)

    # Initialize Firebase Admin SDK if not already initialized
    try:
        import firebase_admin
        from firebase_admin import credentials, messaging
    except ImportError:
        logger.warning('firebase-admin not installed, skipping FCM push')
        return

    cred_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '')
    if not cred_path or not os.path.exists(cred_path):
        logger.debug('Firebase service account JSON not configured, skipping FCM')
        return

    # Initialize Firebase app (once)
    if not firebase_admin._apps:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)

    # Collect FCM tokens
    tokens = []
    if user_id:
        user = db.session.get(User, user_id)
        if user and user.fcm_token:
            tokens.append(user.fcm_token)
    elif target_roles:
        users = User.query.filter(
            User.is_active == True,
            User.fcm_token.isnot(None),
            User.fcm_token != ''
        ).all()
        for u in users:
            if any(r.name in target_roles for r in u.roles):
                tokens.append(u.fcm_token)
    else:
        users = User.query.filter(
            User.is_active == True,
            User.fcm_token.isnot(None),
            User.fcm_token != ''
        ).all()
        tokens = [u.fcm_token for u in users]

    if not tokens:
        return

    # Build notification data
    default_target_url = '/orders'
    notification_data = dict(data) if data else {}
    notification_data['url'] = notification_data.get('url', default_target_url)

    # Send to each token using Firebase Admin SDK (v1 API)
    for token in tokens:
        try:
            msg = messaging.Message(
                notification=messaging.Notification(title=title, body=message),
                data={k: str(v) for k, v in notification_data.items()},
                token=token,
                android=messaging.AndroidConfig(
                    priority='high',
                    notification=messaging.AndroidNotification(
                        sound='default',
                        click_action='OPEN_ACTIVITY'
                    )
                )
            )
            messaging.send(msg)
        except Exception as e:
            logger.warning(f'FCM send to token failed: {e}')


def validate_token(token):
    """Validate API token and return user"""
    if not token:
        return None

    try:
        parts = token.split(':')
        if len(parts) != 3:
            return None

        user_id, timestamp, signature = parts

        # Cek timestamp (24 jam)
        if int(timestamp) < time.time() - 86400:
            return None

        # Verifikasi signature
        secret = current_app.config.get('SECRET_KEY')
        expected = hmac.new(
            secret.encode(),
            f"{user_id}:{timestamp}".encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected):
            return None

        return db.session.get(User, int(user_id))
    except Exception:
        return None
