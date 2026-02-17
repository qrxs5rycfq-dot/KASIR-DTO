import os
import base64
from datetime import datetime, timedelta
from io import BytesIO

import qrcode
from flask import (
    Blueprint, render_template, jsonify, request, redirect,
    url_for, flash, send_file, current_app
)
from flask_login import login_required, current_user

from extensions import limiter, csrf
from models import (
    db, User, Role, Category, MenuItem, Table, Order, OrderItem,
    Payment, Income, Setting, Cart, CartItem, Discount, BranchMenuStock,
    Branch, ExternalOrder, WebhookLog, City, Brand
)
from utils import (
    role_required, allowed_file, save_uploaded_image,
    get_user_branch_id, get_default_branch_id, branch_filter,
    utc_now, get_branch_stock, get_setting, set_setting
)

admin_bp = Blueprint('admin', __name__, url_prefix='')


def generate_table_qr(table_number):
    configured_url = current_app.config.get('APP_URL', '')
    app_url = configured_url.rstrip('/') if configured_url else request.host_url.rstrip('/')
    order_url = f"{app_url}/order/online/{table_number}"

    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(order_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    # Save to file
    qr_folder = current_app.config.get('QR_CODE_FOLDER', 'static/qrcodes')
    os.makedirs(qr_folder, exist_ok=True)
    qr_path = os.path.join(qr_folder, f"table_{table_number}.png")
    img.save(qr_path)

    # Also return base64 for display
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()

    return qr_path, img_str


# ========== PLATFORM CONFIG (needed for integrations page) ==========

PLATFORM_CONFIG = {
    'grabfood': {
        'name': 'GrabFood',
        'oauth_token_url': 'https://partner-api.grab.com/grabid/v1/oauth2/token',
        'oauth_token_url_sandbox': 'https://partner-api.stg-myteksi.com/grabid/v1/oauth2/token',
        'api_base': 'https://partner-api.grab.com',
        'api_base_sandbox': 'https://partner-api.stg-myteksi.com',
        'scope': 'food.partner_api',
        'sig_header': 'X-Grab-Signature',
        'sig_algo': 'sha256',
        'webhook_events': ['order.placed', 'order.cancelled', 'order.completed'],
    },
    'gofood': {
        'name': 'GoFood',
        'oauth_token_url': 'https://accounts.go-jek.com/oauth2/token',
        'oauth_token_url_sandbox': 'https://integration-goauth.gojekapi.com/oauth2/token',
        'api_base': 'https://api.gobiz.co.id',
        'api_base_sandbox': 'https://api.partner-sandbox.gobiz.co.id',
        'scope': 'gofood:order:read gofood:order:write gofood:catalog:read',
        'sig_header': 'X-Callback-Token',
        'sig_algo': 'token',
        'webhook_events': ['gofood.order.awaiting_merchant_acceptance', 'gofood.order.merchant_accepted',
                           'gofood.order.cancelled', 'gofood.order.completed',
                           'gofood.order.driver_otw_pickup', 'gofood.order.driver_arrived'],
    },
    'shopeefood': {
        'name': 'ShopeeFood',
        'api_base': 'https://partner.shopeemobile.com',
        'api_base_sandbox': 'https://partner.test-stable.shopeemobile.com',
        'sig_header': 'Authorization',
        'sig_algo': 'sha256',
        'webhook_events': ['ORDER_STATUS_UPDATE', 'ORDER_CREATE', 'ORDER_CANCEL'],
    }
}


def get_platform_config(platform):
    """Get configuration for a platform from environment/app config"""
    prefix = platform.upper()
    is_sandbox = current_app.config.get(f'{prefix}_SANDBOX', True)
    pcfg = PLATFORM_CONFIG.get(platform, {})

    return {
        'client_id': os.environ.get(f'{prefix}_CLIENT_ID', ''),
        'client_secret': os.environ.get(f'{prefix}_CLIENT_SECRET', ''),
        'webhook_secret': os.environ.get(f'{prefix}_WEBHOOK_SECRET', ''),
        'partner_id': os.environ.get(f'{prefix}_PARTNER_ID', ''),
        'partner_key': os.environ.get(f'{prefix}_PARTNER_KEY', ''),
        'merchant_id': os.environ.get(f'{prefix}_MERCHANT_ID', ''),
        'store_id': os.environ.get(f'{prefix}_STORE_ID', ''),
        'store_branch_map': os.environ.get(f'{prefix}_STORE_BRANCH_MAP', ''),
        'is_sandbox': is_sandbox,
        'api_base': pcfg.get('api_base_sandbox') if is_sandbox else pcfg.get('api_base', ''),
        'token_url': pcfg.get('oauth_token_url_sandbox') if is_sandbox else pcfg.get('oauth_token_url', ''),
        'scope': pcfg.get('scope', ''),
    }


# ========== USER MANAGEMENT ==========

@admin_bp.route('/admin/users')
@login_required
@role_required('admin')
def admin_users():
    users = branch_filter(User.query, User).all()
    roles = Role.query.all()
    return render_template('admin/users.html', users=users, roles=roles)

@admin_bp.route('/admin/users/create', methods=['POST'])
@login_required
@role_required('admin')
def admin_create_user():
    username = request.form.get('username')
    email = request.form.get('email')
    password = request.form.get('password')
    full_name = request.form.get('full_name')
    role_id = request.form.get('role_id')

    if User.query.filter_by(username=username).first():
        flash('Username sudah digunakan!', 'danger')
        return redirect(url_for('admin.admin_users'))

    user = User(
        username=username,
        email=email,
        full_name=full_name,
        branch_id=int(request.form['branch_id']) if request.form.get('branch_id', '').strip() else get_user_branch_id()
    )
    user.set_password(password)

    if role_id:
        role = db.session.get(Role, role_id)
        if role:
            user.roles.append(role)

    db.session.add(user)
    db.session.commit()

    flash('User berhasil dibuat!', 'success')
    return redirect(url_for('admin.admin_users'))

@admin_bp.route('/admin/users/<int:user_id>/toggle', methods=['POST'])
@login_required
@role_required('admin')
def admin_toggle_user(user_id):
    user = User.query.get_or_404(user_id)
    user.is_active = not user.is_active
    db.session.commit()

    status = 'diaktifkan' if user.is_active else 'dinonaktifkan'
    flash(f'User {user.username} berhasil {status}!', 'success')
    return redirect(url_for('admin.admin_users'))

@admin_bp.route('/admin/users/<int:user_id>/edit', methods=['POST'])
@login_required
@role_required('admin')
def admin_edit_user(user_id):
    user = User.query.get_or_404(user_id)

    user.full_name = request.form.get('full_name', user.full_name)
    user.username = request.form.get('username', user.username)
    user.email = request.form.get('email', user.email)

    password = request.form.get('password', '').strip()
    if password:
        user.set_password(password)

    role_id = request.form.get('role_id')
    if role_id:
        role = db.session.get(Role, role_id)
        if role:
            user.roles = [role]

    branch_id = request.form.get('branch_id', '').strip()
    user.branch_id = int(branch_id) if branch_id else None

    db.session.commit()
    flash(f'User {user.username} berhasil diperbarui!', 'success')
    return redirect(url_for('admin.admin_users'))

@admin_bp.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
@role_required('admin')
def admin_delete_user(user_id):
    if current_user.id == user_id:
        flash('Tidak bisa menghapus akun sendiri!', 'danger')
        return redirect(url_for('admin.admin_users'))

    user = User.query.get_or_404(user_id)
    username = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f'User {username} berhasil dihapus!', 'success')
    return redirect(url_for('admin.admin_users'))


# ========== MENU MANAGEMENT ==========

@admin_bp.route('/admin/menu')
@login_required
@role_required('admin', 'manager')
def admin_menu():
    categories = Category.query.order_by(Category.order).all()
    menu_items = MenuItem.query.all()
    return render_template('admin/menu.html', categories=categories, menu_items=menu_items)

@admin_bp.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """Serve uploaded files"""
    upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
    return send_file(os.path.join(upload_folder, filename))

@admin_bp.route('/admin/menu/create', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_create_menu():
    code = request.form.get('code')
    name = request.form.get('name')
    price = int(request.form.get('price', 0))
    category_id = request.form.get('category_id')
    description = request.form.get('description')
    is_popular = request.form.get('is_popular') == 'on'
    has_spicy_option = request.form.get('has_spicy_option') == 'on'
    has_temperature_option = request.form.get('has_temperature_option') == 'on'

    # Handle image: URL or upload
    image = request.form.get('image_url', '').strip()
    if 'image_file' in request.files:
        file = request.files['image_file']
        if file and file.filename:
            uploaded_path = save_uploaded_image(file)
            if uploaded_path:
                image = uploaded_path

    # Default image if none provided
    if not image:
        image = "https://via.placeholder.com/300x200?text=No+Image"

    menu_item = MenuItem(
        code=code,
        name=name,
        price=price,
        category_id=category_id,
        description=description,
        is_popular=is_popular,
        has_spicy_option=has_spicy_option,
        has_temperature_option=has_temperature_option,
        image=image
    )

    db.session.add(menu_item)
    db.session.flush()

    # Create BranchMenuStock entries for all branches (including inactive)
    branches = Branch.query.all()
    for branch in branches:
        bms = BranchMenuStock(branch_id=branch.id, menu_item_id=menu_item.id, stock=100, is_available=True)
        db.session.add(bms)

    db.session.commit()

    flash('Menu berhasil ditambahkan!', 'success')
    return redirect(url_for('admin.admin_menu'))


@admin_bp.route('/admin/menu/<int:id>/edit', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_edit_menu(id):
    menu_item = MenuItem.query.get_or_404(id)

    menu_item.code = request.form.get('code', menu_item.code)
    menu_item.name = request.form.get('name', menu_item.name)
    menu_item.price = int(request.form.get('price', menu_item.price))
    menu_item.category_id = request.form.get('category_id', menu_item.category_id)
    menu_item.description = request.form.get('description', menu_item.description)
    menu_item.is_popular = request.form.get('is_popular') == 'on'
    menu_item.has_spicy_option = request.form.get('has_spicy_option') == 'on'
    menu_item.has_temperature_option = request.form.get('has_temperature_option') == 'on'

    # Update per-branch stock/availability
    bid = get_user_branch_id()
    new_available = request.form.get('is_available') == 'on'
    if bid:
        bms = get_branch_stock(menu_item.id, bid)
        bms.is_available = new_available
    else:
        # Owner: update global is_available + all branches
        menu_item.is_available = new_available
        BranchMenuStock.query.filter_by(menu_item_id=menu_item.id).update({'is_available': new_available})

    # Handle image: URL or upload
    image_url = request.form.get('image_url', '').strip()
    if image_url:
        menu_item.image = image_url

    if 'image_file' in request.files:
        file = request.files['image_file']
        if file and file.filename:
            uploaded_path = save_uploaded_image(file)
            if uploaded_path:
                menu_item.image = uploaded_path

    db.session.commit()

    flash('Menu berhasil diperbarui!', 'success')
    return redirect(url_for('admin.admin_menu'))


@admin_bp.route('/admin/menu/<int:id>/delete', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_delete_menu(id):
    menu_item = MenuItem.query.get_or_404(id)

    # Check if menu item is used in any orders
    order_items = OrderItem.query.filter_by(menu_item_id=id).first()
    if order_items:
        flash('Menu tidak dapat dihapus karena sudah digunakan dalam pesanan. Nonaktifkan saja jika tidak ingin ditampilkan.', 'error')
        return redirect(url_for('admin.admin_menu'))

    # Delete from cart items first
    CartItem.query.filter_by(menu_item_id=id).delete()

    # Delete branch stock entries
    BranchMenuStock.query.filter_by(menu_item_id=id).delete()

    db.session.delete(menu_item)
    db.session.commit()

    flash('Menu berhasil dihapus!', 'success')
    return redirect(url_for('admin.admin_menu'))


@admin_bp.route('/api/menu/<int:id>')
@login_required
@role_required('admin', 'manager')
def api_get_menu_item(id):
    """Get menu item data for edit form"""
    menu_item = MenuItem.query.get_or_404(id)
    bid = get_user_branch_id()
    data = {
        'id': menu_item.id,
        'code': menu_item.code,
        'name': menu_item.name,
        'price': menu_item.price,
        'category_id': menu_item.category_id,
        'description': menu_item.description or '',
        'image': menu_item.image or '',
        'is_popular': menu_item.is_popular,
        'is_available': menu_item.is_available,
        'has_spicy_option': menu_item.has_spicy_option,
        'has_temperature_option': menu_item.has_temperature_option
    }
    # Return per-branch availability if user is branch-specific
    if bid:
        bms = get_branch_stock(menu_item.id, bid)
        data['is_available'] = bms.is_available
        data['stock'] = bms.stock
    return jsonify(data)

@admin_bp.route('/api/menu/<int:id>/toggle', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def api_toggle_menu(id):
    """Toggle menu item availability (on/off / habis)"""
    menu_item = MenuItem.query.get_or_404(id)
    bid = get_user_branch_id()

    if bid:
        # Branch user: toggle per-branch availability
        bms = get_branch_stock(menu_item.id, bid)
        bms.is_available = not bms.is_available
        new_status = bms.is_available
    else:
        # Owner: toggle global + all branches
        menu_item.is_available = not menu_item.is_available
        new_status = menu_item.is_available
        BranchMenuStock.query.filter_by(menu_item_id=menu_item.id).update({'is_available': new_status})

    db.session.commit()
    return jsonify({'success': True, 'is_available': new_status, 'name': menu_item.name})


# ========== PRINTER ==========

@admin_bp.route('/admin/printer')
@login_required
@role_required('admin', 'manager')
def admin_printer():
    """Redirect to Printer Station - the dedicated printer page"""
    return redirect(url_for('views.printer_station'))


# ========== TABLE MANAGEMENT ==========

@admin_bp.route('/admin/tables')
@login_required
@role_required('admin', 'manager')
def admin_tables():
    tables = Table.query.all()
    return render_template('admin/tables.html', tables=tables)

@admin_bp.route('/admin/tables/<int:table_id>/qr')
@login_required
@role_required('admin', 'manager')
def admin_table_qr(table_id):
    table = Table.query.get_or_404(table_id)
    qr_path, qr_base64 = generate_table_qr(table.number)
    table.qr_code = qr_path
    db.session.commit()

    return jsonify({
        'success': True,
        'qr_code': f"data:image/png;base64,{qr_base64}",
        'table_number': table.number
    })

@admin_bp.route('/admin/tables/add', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_table_add():
    """Add new table"""
    number = request.form.get('number', '').strip()
    name = request.form.get('name', '').strip()
    capacity = int(request.form.get('capacity', 4))

    if not number:
        flash('Nomor meja wajib diisi!', 'danger')
        return redirect(url_for('admin.admin_tables'))

    if Table.query.filter_by(number=number).first():
        flash('Nomor meja sudah ada!', 'danger')
        return redirect(url_for('admin.admin_tables'))

    table = Table(
        number=number,
        name=name or f"Meja {number}",
        capacity=capacity
    )
    db.session.add(table)
    db.session.commit()

    flash(f'Meja {number} berhasil ditambahkan!', 'success')
    return redirect(url_for('admin.admin_tables'))

@admin_bp.route('/admin/tables/<int:table_id>/delete', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_table_delete(table_id):
    """Delete a table"""
    table = Table.query.get_or_404(table_id)

    # Check if table has active orders
    has_active_order = Order.query.filter_by(table_id=table_id).filter(
        Order.status.in_(['pending', 'processing'])
    ).first() is not None

    if has_active_order:
        flash('Tidak bisa hapus meja dengan pesanan aktif!', 'danger')
        return redirect(url_for('admin.admin_tables'))

    table_num = table.number
    db.session.delete(table)
    db.session.commit()

    flash(f'Meja {table_num} berhasil dihapus!', 'success')
    return redirect(url_for('admin.admin_tables'))

@admin_bp.route('/admin/tables/<int:table_id>/toggle', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_table_toggle(table_id):
    """Toggle table status between available and occupied"""
    table = Table.query.get_or_404(table_id)

    if table.status == 'available':
        table.status = 'occupied'
    else:
        table.status = 'available'

    db.session.commit()

    return jsonify({
        'success': True,
        'status': table.status,
        'message': f'Meja {table.number} status: {table.status}'
    })


# ========== DISCOUNT/PROMO MANAGEMENT ==========

@admin_bp.route('/admin/discounts')
@login_required
@role_required('admin', 'manager')
def admin_discounts():
    """Discount management page"""
    discounts = branch_filter(Discount.query, Discount).order_by(Discount.created_at.desc()).all()
    return render_template('admin/discounts.html', discounts=discounts)


@admin_bp.route('/admin/discounts/create', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_discount_create():
    """Create new discount/promo"""
    try:
        name = request.form.get('name')
        code = request.form.get('code', '').upper().strip()
        description = request.form.get('description', '')
        discount_type = request.form.get('discount_type', 'percentage')
        value = int(request.form.get('value', 0))
        min_purchase = int(request.form.get('min_purchase', 0))
        max_discount = request.form.get('max_discount')
        usage_limit = request.form.get('usage_limit')
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')
        is_active = request.form.get('is_active') == 'on'

        # Validate required fields
        if not name or not code or value <= 0:
            flash('Nama, kode, dan nilai diskon harus diisi dengan benar', 'danger')
            return redirect(url_for('admin.admin_discounts'))

        # Check if code already exists
        existing = Discount.query.filter_by(code=code).first()
        if existing:
            flash(f'Kode promo "{code}" sudah digunakan', 'danger')
            return redirect(url_for('admin.admin_discounts'))

        # Parse optional fields
        max_discount = int(max_discount) if max_discount else None
        usage_limit = int(usage_limit) if usage_limit else None
        start_date = datetime.fromisoformat(start_date) if start_date else None
        end_date = datetime.fromisoformat(end_date) if end_date else None

        discount = Discount(
            name=name,
            code=code,
            description=description,
            discount_type=discount_type,
            value=value,
            min_purchase=min_purchase,
            max_discount=max_discount,
            usage_limit=usage_limit,
            start_date=start_date,
            end_date=end_date,
            is_active=is_active,
            branch_id=get_default_branch_id()
        )

        db.session.add(discount)
        db.session.commit()

        flash(f'Promo "{name}" berhasil ditambahkan', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Gagal menambahkan promo: {str(e)}', 'danger')

    return redirect(url_for('admin.admin_discounts'))


@admin_bp.route('/admin/discounts/<int:discount_id>/edit', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_discount_edit(discount_id):
    """Edit existing discount"""
    discount = Discount.query.get_or_404(discount_id)

    try:
        discount.name = request.form.get('name', discount.name)
        discount.description = request.form.get('description', '')
        discount.discount_type = request.form.get('discount_type', discount.discount_type)
        discount.value = int(request.form.get('value', discount.value))
        discount.min_purchase = int(request.form.get('min_purchase', 0))

        max_discount = request.form.get('max_discount')
        discount.max_discount = int(max_discount) if max_discount else None

        usage_limit = request.form.get('usage_limit')
        discount.usage_limit = int(usage_limit) if usage_limit else None

        start_date = request.form.get('start_date')
        discount.start_date = datetime.fromisoformat(start_date) if start_date else None

        end_date = request.form.get('end_date')
        discount.end_date = datetime.fromisoformat(end_date) if end_date else None

        discount.is_active = request.form.get('is_active') == 'on'

        db.session.commit()
        flash(f'Promo "{discount.name}" berhasil diperbarui', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Gagal memperbarui promo: {str(e)}', 'danger')

    return redirect(url_for('admin.admin_discounts'))


@admin_bp.route('/admin/discounts/<int:discount_id>/delete', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_discount_delete(discount_id):
    """Delete a discount"""
    discount = Discount.query.get_or_404(discount_id)

    try:
        name = discount.name
        db.session.delete(discount)
        db.session.commit()
        flash(f'Promo "{name}" berhasil dihapus', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Gagal menghapus promo: {str(e)}', 'danger')

    return redirect(url_for('admin.admin_discounts'))


@admin_bp.route('/admin/discounts/<int:discount_id>/toggle', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_discount_toggle(discount_id):
    """Toggle discount active status"""
    discount = Discount.query.get_or_404(discount_id)
    discount.is_active = not discount.is_active
    db.session.commit()

    status = "aktif" if discount.is_active else "nonaktif"
    return jsonify({
        'success': True,
        'is_active': discount.is_active,
        'message': f'Promo "{discount.name}" sekarang {status}'
    })


@admin_bp.route('/api/discount/validate', methods=['POST'])
@login_required
def api_validate_discount():
    """Validate a discount code for given subtotal"""
    data = request.get_json()
    code = data.get('code', '').upper().strip()
    subtotal = int(data.get('subtotal', 0))

    if not code:
        return jsonify({'valid': False, 'message': 'Kode promo tidak boleh kosong'})

    discount = Discount.query.filter_by(code=code).first()
    if not discount:
        return jsonify({'valid': False, 'message': 'Kode promo tidak ditemukan'})

    is_valid, message = discount.is_valid(subtotal)
    if not is_valid:
        return jsonify({'valid': False, 'message': message})

    discount_amount = discount.calculate_discount(subtotal)

    return jsonify({
        'valid': True,
        'message': 'Promo berhasil digunakan!',
        'discount': discount.to_dict(),
        'discount_amount': discount_amount
    })


@admin_bp.route('/api/discounts/active')
@login_required
def api_active_discounts():
    """Get all currently active discounts"""
    now = utc_now()
    discounts = branch_filter(Discount.query, Discount).filter(
        Discount.is_active == True,
        (Discount.start_date == None) | (Discount.start_date <= now),
        (Discount.end_date == None) | (Discount.end_date >= now),
        (Discount.usage_limit == None) | (Discount.usage_count < Discount.usage_limit)
    ).all()

    return jsonify({
        'discounts': [d.to_dict() for d in discounts]
    })


# ========== BRANCH / CABANG MANAGEMENT ==========

@admin_bp.route('/admin/branches')
@login_required
@role_required('admin', 'manager')
def admin_branches():
    """Branch management page"""
    branches = Branch.query.order_by(Branch.created_at.desc()).all()
    cities = City.query.order_by(City.name).all()
    brands = Brand.query.order_by(Brand.name).all()
    return render_template('admin/branches.html', branches=branches, cities=cities, brands=brands, active_page='admin_branches')

@admin_bp.route('/admin/branches/create', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_branch_create():
    """Create a new branch"""
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip().upper()
    address = request.form.get('address', '').strip()
    phone = request.form.get('phone', '').strip()
    manager_name = request.form.get('manager_name', '').strip()
    opening_time = request.form.get('opening_time', '08:00').strip()
    closing_time = request.form.get('closing_time', '22:00').strip()
    city_id = request.form.get('city_id', '').strip()
    brand_id = request.form.get('brand_id', '').strip()

    if not name or not code:
        flash('Nama dan kode cabang wajib diisi.', 'danger')
        return redirect(url_for('admin.admin_branches'))

    # Check for duplicate code
    existing = Branch.query.filter_by(code=code).first()
    if existing:
        flash('Kode cabang sudah digunakan.', 'danger')
        return redirect(url_for('admin.admin_branches'))

    branch = Branch(
        name=name,
        code=code,
        address=address,
        phone=phone,
        manager_name=manager_name,
        opening_time=opening_time,
        closing_time=closing_time,
        city_id=int(city_id) if city_id and city_id.isdigit() else None,
        brand_id=int(brand_id) if brand_id and brand_id.isdigit() else None
    )
    db.session.add(branch)
    db.session.flush()

    # Create BranchMenuStock entries for all existing menu items
    all_menu_items = MenuItem.query.all()
    for mi in all_menu_items:
        bms = BranchMenuStock(branch_id=branch.id, menu_item_id=mi.id, stock=100, is_available=True)
        db.session.add(bms)

    db.session.commit()

    flash(f'Cabang "{name}" berhasil ditambahkan!', 'success')
    return redirect(url_for('admin.admin_branches'))

@admin_bp.route('/admin/branches/<int:branch_id>/edit', methods=['POST'])
@login_required
@role_required('admin', 'manager')
def admin_branch_edit(branch_id):
    """Edit a branch"""
    branch = db.session.get(Branch, branch_id)
    if not branch:
        flash('Cabang tidak ditemukan.', 'danger')
        return redirect(url_for('admin.admin_branches'))

    branch.name = request.form.get('name', branch.name).strip()
    new_code = request.form.get('code', branch.code).strip().upper()

    # Check for duplicate code (exclude current branch)
    existing = Branch.query.filter(Branch.code == new_code, Branch.id != branch_id).first()
    if existing:
        flash('Kode cabang sudah digunakan.', 'danger')
        return redirect(url_for('admin.admin_branches'))

    branch.code = new_code
    branch.address = request.form.get('address', branch.address).strip()
    branch.phone = request.form.get('phone', branch.phone).strip()
    branch.manager_name = request.form.get('manager_name', branch.manager_name).strip()
    branch.opening_time = request.form.get('opening_time', branch.opening_time).strip()
    branch.closing_time = request.form.get('closing_time', branch.closing_time).strip()
    city_id_str = request.form.get('city_id', '').strip()
    brand_id_str = request.form.get('brand_id', '').strip()
    branch.city_id = int(city_id_str) if city_id_str and city_id_str.isdigit() else None
    branch.brand_id = int(brand_id_str) if brand_id_str and brand_id_str.isdigit() else None

    db.session.commit()
    flash(f'Cabang "{branch.name}" berhasil diperbarui!', 'success')
    return redirect(url_for('admin.admin_branches'))

@admin_bp.route('/admin/branches/<int:branch_id>/toggle', methods=['POST'])
@login_required
@role_required('admin')
def admin_branch_toggle(branch_id):
    """Toggle branch active status - only owner (admin with no branch) can do this"""
    # Only owner/admin pusat (branch_id=NULL) can toggle branches
    if current_user.branch_id is not None:
        return jsonify({'success': False, 'error': 'Hanya owner/admin pusat yang dapat mengubah status cabang'}), 403

    branch = db.session.get(Branch, branch_id)
    if not branch:
        return jsonify({'success': False, 'error': 'Cabang tidak ditemukan'}), 404

    # Prevent deactivating the Pusat branch
    if branch.code == 'PUSAT' and branch.is_active:
        return jsonify({'success': False, 'error': 'Cabang Pusat tidak dapat dinonaktifkan'}), 400

    branch.is_active = not branch.is_active
    db.session.commit()

    status = 'aktif' if branch.is_active else 'nonaktif'
    return jsonify({
        'success': True,
        'is_active': branch.is_active,
        'message': f'Cabang "{branch.name}" sekarang {status}'
    })

@admin_bp.route('/admin/branches/<int:branch_id>/delete', methods=['POST'])
@login_required
@role_required('admin')
def admin_branch_delete(branch_id):
    """Delete a branch - only owner (admin with no branch) can do this"""
    # Only owner/admin pusat (branch_id=NULL) can delete branches
    if current_user.branch_id is not None:
        flash('Hanya owner/admin pusat yang dapat menghapus cabang.', 'danger')
        return redirect(url_for('admin.admin_branches'))

    branch = db.session.get(Branch, branch_id)
    if not branch:
        flash('Cabang tidak ditemukan.', 'danger')
        return redirect(url_for('admin.admin_branches'))

    # Prevent deleting the Pusat branch
    if branch.code == 'PUSAT':
        flash('Cabang Pusat tidak dapat dihapus.', 'danger')
        return redirect(url_for('admin.admin_branches'))

    name = branch.name
    # Clean up BranchMenuStock entries first (NOT NULL constraint on branch_id)
    BranchMenuStock.query.filter_by(branch_id=branch_id).delete()
    db.session.delete(branch)
    db.session.commit()

    flash(f'Cabang "{name}" berhasil dihapus.', 'success')
    return redirect(url_for('admin.admin_branches'))


# ========== CITY MANAGEMENT ==========

@admin_bp.route('/admin/cities')
@login_required
@role_required('admin')
def admin_cities():
    """City management page"""
    cities = City.query.order_by(City.name).all()
    return render_template('admin/cities.html', cities=cities, active_page='admin_cities')

@admin_bp.route('/admin/cities/create', methods=['POST'])
@login_required
@role_required('admin')
def admin_city_create():
    """Create a new city"""
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip().upper()

    if not name or not code:
        flash('Nama dan kode kota wajib diisi.', 'danger')
        return redirect(url_for('admin.admin_cities'))

    if City.query.filter_by(code=code).first():
        flash('Kode kota sudah digunakan.', 'danger')
        return redirect(url_for('admin.admin_cities'))

    city = City(name=name, code=code)
    db.session.add(city)
    db.session.commit()

    flash(f'Kota "{name}" berhasil ditambahkan!', 'success')
    return redirect(url_for('admin.admin_cities'))

@admin_bp.route('/admin/cities/<int:city_id>/edit', methods=['POST'])
@login_required
@role_required('admin')
def admin_city_edit(city_id):
    """Edit a city"""
    city = db.session.get(City, city_id)
    if not city:
        flash('Kota tidak ditemukan.', 'danger')
        return redirect(url_for('admin.admin_cities'))

    city.name = request.form.get('name', city.name).strip()
    new_code = request.form.get('code', city.code).strip().upper()

    existing = City.query.filter(City.code == new_code, City.id != city_id).first()
    if existing:
        flash('Kode kota sudah digunakan.', 'danger')
        return redirect(url_for('admin.admin_cities'))

    city.code = new_code
    db.session.commit()
    flash(f'Kota "{city.name}" berhasil diperbarui!', 'success')
    return redirect(url_for('admin.admin_cities'))

@admin_bp.route('/admin/cities/<int:city_id>/toggle', methods=['POST'])
@login_required
@role_required('admin')
def admin_city_toggle(city_id):
    """Toggle city active status"""
    if current_user.branch_id is not None:
        return jsonify({'success': False, 'error': 'Hanya owner yang dapat mengubah status kota'}), 403

    city = db.session.get(City, city_id)
    if not city:
        return jsonify({'success': False, 'error': 'Kota tidak ditemukan'}), 404

    city.is_active = not city.is_active
    db.session.commit()

    return jsonify({
        'success': True,
        'is_active': city.is_active,
        'message': f'Kota "{city.name}" sekarang {"aktif" if city.is_active else "nonaktif"}'
    })

@admin_bp.route('/admin/cities/<int:city_id>/delete', methods=['POST'])
@login_required
@role_required('admin')
def admin_city_delete(city_id):
    """Delete a city"""
    if current_user.branch_id is not None:
        flash('Hanya owner yang dapat menghapus kota.', 'danger')
        return redirect(url_for('admin.admin_cities'))

    city = db.session.get(City, city_id)
    if not city:
        flash('Kota tidak ditemukan.', 'danger')
        return redirect(url_for('admin.admin_cities'))

    if city.branches.count() > 0:
        flash(f'Kota "{city.name}" masih memiliki {city.branches.count()} cabang. Hapus atau pindahkan cabang terlebih dahulu.', 'danger')
        return redirect(url_for('admin.admin_cities'))

    name = city.name
    db.session.delete(city)
    db.session.commit()
    flash(f'Kota "{name}" berhasil dihapus.', 'success')
    return redirect(url_for('admin.admin_cities'))


# ========== BRAND MANAGEMENT ==========

@admin_bp.route('/admin/brands')
@login_required
@role_required('admin')
def admin_brands():
    """Brand management page"""
    brands = Brand.query.order_by(Brand.name).all()
    return render_template('admin/brands.html', brands=brands, active_page='admin_brands')

@admin_bp.route('/admin/brands/create', methods=['POST'])
@login_required
@role_required('admin')
def admin_brand_create():
    """Create a new brand"""
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip().upper()
    description = request.form.get('description', '').strip()

    if not name or not code:
        flash('Nama dan kode brand wajib diisi.', 'danger')
        return redirect(url_for('admin.admin_brands'))

    if Brand.query.filter_by(code=code).first():
        flash('Kode brand sudah digunakan.', 'danger')
        return redirect(url_for('admin.admin_brands'))

    brand = Brand(name=name, code=code, description=description)
    db.session.add(brand)
    db.session.commit()

    flash(f'Brand "{name}" berhasil ditambahkan!', 'success')
    return redirect(url_for('admin.admin_brands'))

@admin_bp.route('/admin/brands/<int:brand_id>/edit', methods=['POST'])
@login_required
@role_required('admin')
def admin_brand_edit(brand_id):
    """Edit a brand"""
    brand = db.session.get(Brand, brand_id)
    if not brand:
        flash('Brand tidak ditemukan.', 'danger')
        return redirect(url_for('admin.admin_brands'))

    brand.name = request.form.get('name', brand.name).strip()
    new_code = request.form.get('code', brand.code).strip().upper()

    existing = Brand.query.filter(Brand.code == new_code, Brand.id != brand_id).first()
    if existing:
        flash('Kode brand sudah digunakan.', 'danger')
        return redirect(url_for('admin.admin_brands'))

    brand.code = new_code
    brand.description = request.form.get('description', brand.description or '').strip()
    db.session.commit()
    flash(f'Brand "{brand.name}" berhasil diperbarui!', 'success')
    return redirect(url_for('admin.admin_brands'))

@admin_bp.route('/admin/brands/<int:brand_id>/toggle', methods=['POST'])
@login_required
@role_required('admin')
def admin_brand_toggle(brand_id):
    """Toggle brand active status"""
    if current_user.branch_id is not None:
        return jsonify({'success': False, 'error': 'Hanya owner yang dapat mengubah status brand'}), 403

    brand = db.session.get(Brand, brand_id)
    if not brand:
        return jsonify({'success': False, 'error': 'Brand tidak ditemukan'}), 404

    brand.is_active = not brand.is_active
    db.session.commit()

    return jsonify({
        'success': True,
        'is_active': brand.is_active,
        'message': f'Brand "{brand.name}" sekarang {"aktif" if brand.is_active else "nonaktif"}'
    })

@admin_bp.route('/admin/brands/<int:brand_id>/delete', methods=['POST'])
@login_required
@role_required('admin')
def admin_brand_delete(brand_id):
    """Delete a brand"""
    if current_user.branch_id is not None:
        flash('Hanya owner yang dapat menghapus brand.', 'danger')
        return redirect(url_for('admin.admin_brands'))

    brand = db.session.get(Brand, brand_id)
    if not brand:
        flash('Brand tidak ditemukan.', 'danger')
        return redirect(url_for('admin.admin_brands'))

    if brand.branches.count() > 0:
        flash(f'Brand "{brand.name}" masih memiliki {brand.branches.count()} cabang. Hapus atau pindahkan cabang terlebih dahulu.', 'danger')
        return redirect(url_for('admin.admin_brands'))

    name = brand.name
    db.session.delete(brand)
    db.session.commit()
    flash(f'Brand "{name}" berhasil dihapus.', 'success')
    return redirect(url_for('admin.admin_brands'))


# ========== RESET DATABASE ==========

@admin_bp.route('/admin/reset-database', methods=['POST'])
@login_required
@role_required('admin')
def reset_database():
    """Reset all transactional data (orders, payments, carts) but keep menu, users, tables"""
    try:
        # Clear cart items first (foreign key)
        CartItem.query.delete()
        Cart.query.delete()

        # Clear order items (foreign key)
        OrderItem.query.delete()

        # Clear payments (foreign key)
        Payment.query.delete()

        # Clear orders
        Order.query.delete()

        # Clear income records
        Income.query.delete()

        # Reset table status
        Table.query.update({Table.status: 'available'})

        db.session.commit()
        flash('Database berhasil direset! Semua data pesanan, pembayaran, dan keranjang telah dihapus.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error saat reset database: {str(e)}', 'danger')

    return redirect(url_for('views.dashboard'))


# ========== INTEGRATIONS ==========

@admin_bp.route('/admin/integrations')
@login_required
@role_required('admin')
def admin_integrations():
    """Food delivery platform integration settings and monitoring"""
    from sqlalchemy import func

    platforms = {}
    for key, pcfg in PLATFORM_CONFIG.items():
        cfg = get_platform_config(key)
        platforms[key] = {
            'name': pcfg['name'],
            'icon': {'grabfood': 'fa-motorcycle', 'gofood': 'fa-utensils', 'shopeefood': 'fa-shopping-bag'}[key],
            'color': {'grabfood': 'green', 'gofood': 'red', 'shopeefood': 'orange'}[key],
            'webhook_url': request.url_root.rstrip('/') + f'/api/webhook/{key}',
            'secret': cfg.get('webhook_secret', ''),
            'client_id': cfg.get('client_id', ''),
            'store_id': cfg.get('store_id', ''),
            'store_branch_map': cfg.get('store_branch_map', ''),
            'enabled': bool(cfg.get('webhook_secret') or cfg.get('client_id')),
            'sig_header': pcfg['sig_header'],
            'sig_algo': {'sha256': 'HMAC-SHA256', 'token': 'Token Comparison'}.get(pcfg['sig_algo'], pcfg['sig_algo']),
            'has_oauth': bool(pcfg.get('oauth_token_url')),
            'is_sandbox': cfg.get('is_sandbox', True),
        }

    recent_orders = ExternalOrder.query.order_by(ExternalOrder.created_at.desc()).limit(20).all()
    recent_logs = WebhookLog.query.order_by(WebhookLog.created_at.desc()).limit(20).all()

    today = datetime.now().date()
    today_stats = db.session.query(
        ExternalOrder.platform,
        func.count(ExternalOrder.id).label('count')
    ).filter(
        func.date(ExternalOrder.created_at) == today
    ).group_by(ExternalOrder.platform).all()
    stats = {s.platform: s.count for s in today_stats}

    yesterday = datetime.now() - timedelta(hours=24)
    sig_failures = WebhookLog.query.filter(
        WebhookLog.created_at >= yesterday,
        WebhookLog.result.in_(['sig_invalid', 'sig_missing'])
    ).count()

    return render_template('admin/integrations.html',
                         platforms=platforms,
                         recent_orders=recent_orders,
                         recent_logs=recent_logs,
                         stats=stats,
                         sig_failures=sig_failures,
                         active_page='admin_integrations')


# ========== PAYMENT GATEWAY ==========

@admin_bp.route('/admin/payment-gateway')
@login_required
@role_required('admin')
def admin_payment_gateway():
    """Payment gateway settings page with Midtrans & BRI QRIS"""
    # Midtrans config
    server_key = current_app.config.get('MIDTRANS_SERVER_KEY', '')
    client_key = current_app.config.get('MIDTRANS_CLIENT_KEY', '')
    is_production = current_app.config.get('MIDTRANS_IS_PRODUCTION', False)

    masked_server_key = server_key[:8] + '****' + server_key[-4:] if len(server_key) > 12 else '****'
    masked_client_key = client_key[:8] + '****' + client_key[-4:] if len(client_key) > 12 else '****'

    # BRI QRIS config
    bri_client_id = os.environ.get('BRI_CLIENT_ID', '')
    bri_client_secret = os.environ.get('BRI_CLIENT_SECRET', '')
    bri_merchant_id = os.environ.get('BRI_MERCHANT_ID', '')
    bri_terminal_id = os.environ.get('BRI_TERMINAL_ID', '')
    bri_is_production = os.environ.get('BRI_IS_PRODUCTION', 'false').lower() == 'true'
    bri_private_key_path = os.environ.get('BRI_PRIVATE_KEY_PATH', '')

    masked_bri_client_id = bri_client_id[:8] + '****' + bri_client_id[-4:] if len(bri_client_id) > 12 else ('****' if bri_client_id else '')
    masked_bri_secret = bri_client_secret[:4] + '****' if len(bri_client_secret) > 4 else ('****' if bri_client_secret else '')

    # Active gateway from DB setting
    active_gateway = get_setting('active_payment_gateway', 'midtrans')

    # Tripay config
    tripay_api_key = os.environ.get('TRIPAY_API_KEY', '')
    tripay_private_key = os.environ.get('TRIPAY_PRIVATE_KEY', '')
    tripay_merchant_code = os.environ.get('TRIPAY_MERCHANT_CODE', '')
    tripay_is_production = os.environ.get('TRIPAY_IS_PRODUCTION', 'false').lower() == 'true'

    masked_tripay_api_key = tripay_api_key[:8] + '****' + tripay_api_key[-4:] if len(tripay_api_key) > 12 else ('****' if tripay_api_key else '')
    masked_tripay_private_key = tripay_private_key[:4] + '****' if len(tripay_private_key) > 4 else ('****' if tripay_private_key else '')

    return render_template('admin/payment_gateway.html',
                         masked_server_key=masked_server_key,
                         masked_client_key=masked_client_key,
                         is_production=is_production,
                         is_configured=bool(server_key and client_key),
                         bri_client_id=masked_bri_client_id,
                         bri_client_secret=masked_bri_secret,
                         bri_merchant_id=bri_merchant_id,
                         bri_terminal_id=bri_terminal_id,
                         bri_is_production=bri_is_production,
                         bri_is_configured=bool(bri_client_id and bri_client_secret and bri_merchant_id),
                         bri_private_key_path=bri_private_key_path,
                         tripay_api_key=masked_tripay_api_key,
                         tripay_private_key=masked_tripay_private_key,
                         tripay_merchant_code=tripay_merchant_code,
                         tripay_is_production=tripay_is_production,
                         tripay_is_configured=bool(tripay_api_key and tripay_private_key and tripay_merchant_code),
                         active_gateway=active_gateway,
                         active_page='admin_payment_gateway')


@admin_bp.route('/api/payment-gateway/set-active', methods=['POST'])
@login_required
@role_required('admin')
def api_set_active_gateway():
    """Toggle active payment gateway between midtrans and qris_bri"""
    data = request.json
    gateway = data.get('gateway', 'midtrans')
    if gateway not in ('midtrans', 'qris_bri', 'tripay'):
        return jsonify({'success': False, 'message': 'Gateway tidak valid'}), 400

    set_setting('active_payment_gateway', gateway, 'Active payment gateway (midtrans or qris_bri)')
    return jsonify({'success': True, 'message': f'Payment gateway diubah ke {gateway.upper()}', 'active': gateway})
