import os

from flask import Flask, jsonify, request, redirect, url_for, flash, render_template
from flask_login import current_user
from flask_wtf.csrf import CSRFError

from config import config
from models import db, User, Role, Permission, Category, MenuItem, Table, BranchMenuStock, Branch, City, Brand
from extensions import login_manager, csrf, limiter, socketio
from utils import format_number_filter, format_currency

# Import USB printer module
try:
    from usb_printer import usb_printer, USBPrinterManager
    USB_PRINTING_AVAILABLE = USBPrinterManager.is_available()
except ImportError:
    usb_printer = None
    USB_PRINTING_AVAILABLE = False

# Initialize Flask app
app = Flask(__name__)
app.config.from_object(config['development'])

# Initialize extensions with app
csrf.init_app(app)
_cors_origins = os.environ.get('SOCKETIO_CORS_ORIGINS', '*')
socketio.init_app(app, cors_allowed_origins=_cors_origins, async_mode='gevent', logger=False, engineio_logger=False)
limiter.init_app(app)
db.init_app(app)
login_manager.init_app(app)

# User loader callback
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Custom Jinja2 filters
app.jinja_env.filters['format_number'] = format_number_filter
app.jinja_env.filters['format_currency'] = format_currency

# Context processor to make config available in all templates
@app.context_processor
def inject_config():
    ctx = {
        'config': {
            'MIDTRANS_CLIENT_KEY': app.config.get('MIDTRANS_CLIENT_KEY', 'SB-Mid-client-XXXXXX'),
            'MIDTRANS_IS_PRODUCTION': app.config.get('MIDTRANS_IS_PRODUCTION', False),
            'APP_NAME': 'Dapoer Teras Obor'
        }
    }
    # Inject current branch info for sidebar
    if current_user.is_authenticated:
        if current_user.branch_id:
            ctx['current_branch'] = current_user.branch
        else:
            ctx['current_branch'] = None  # Admin/owner sees all
        ctx['all_branches'] = Branch.query.filter_by(is_active=True).all()
        ctx['all_cities'] = City.query.filter_by(is_active=True).order_by(City.name).all()
        ctx['all_brands'] = Brand.query.filter_by(is_active=True).order_by(Brand.name).all()
        ctx['is_owner'] = current_user.branch_id is None and current_user.has_role('admin')
    return ctx

@app.after_request
def add_header(response):
    """Add headers to prevent caching for HTML pages and security headers"""
    if 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    
    # Security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    return response

# CSRF Error handler
@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': False, 'error': 'CSRF token missing or invalid'}), 400
    flash('Sesi telah berakhir. Silakan coba lagi.', 'danger')
    return redirect(request.referrer or url_for('auth.login'))

# Rate limit error handler
@app.errorhandler(429)
def ratelimit_handler(e):
    """Custom handler for rate limit exceeded"""
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'success': False, 
            'error': 'Terlalu banyak permintaan. Silakan tunggu sebentar.'
        }), 429
    
    flash('Terlalu banyak permintaan. Silakan tunggu 1 menit dan coba lagi.', 'warning')
    return redirect(request.referrer or url_for('auth.login'))

@app.errorhandler(404)
def page_not_found(e):
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    return render_template('errors/500.html'), 500

# Register Blueprints
from routes.auth import auth_bp
from routes.views import views_bp
from routes.admin import admin_bp
from routes.api_menu import api_menu
from routes.api_cart import api_cart
from routes.api_orders import api_orders
from routes.api_payments import api_payments
from routes.api_print import api_print
from routes.webhooks import webhooks_bp

app.register_blueprint(auth_bp)
app.register_blueprint(views_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(api_menu)
app.register_blueprint(api_cart)
app.register_blueprint(api_orders)
app.register_blueprint(api_payments)
app.register_blueprint(api_print)
app.register_blueprint(webhooks_bp)

# Register WebSocket event handlers
from socket_handlers import register_socket_handlers
register_socket_handlers(socketio)

# ==================== Database Initialization ====================

def _get_column_sql(column, dialect_name):
    """Convert a SQLAlchemy column to an ALTER TABLE ADD COLUMN SQL fragment."""
    from sqlalchemy import Integer, String, Float, Boolean, DateTime, Text, Date
    from sqlalchemy.types import VARCHAR, TEXT as SA_TEXT

    col_type = column.type
    if dialect_name == 'sqlite':
        if isinstance(col_type, Boolean):
            sql_type = 'BOOLEAN'
        elif isinstance(col_type, Integer):
            sql_type = 'INTEGER'
        elif isinstance(col_type, Float):
            sql_type = 'REAL'
        elif isinstance(col_type, (DateTime, Date)):
            sql_type = 'TIMESTAMP'
        elif isinstance(col_type, Text):
            sql_type = 'TEXT'
        elif isinstance(col_type, String):
            length = getattr(col_type, 'length', None)
            sql_type = f'VARCHAR({length})' if length else 'TEXT'
        else:
            sql_type = 'TEXT'
    else:
        if isinstance(col_type, Boolean):
            sql_type = 'TINYINT(1)'
        elif isinstance(col_type, Integer):
            sql_type = 'INTEGER'
        elif isinstance(col_type, Float):
            sql_type = 'DOUBLE'
        elif isinstance(col_type, DateTime):
            sql_type = 'DATETIME'
        elif isinstance(col_type, Date):
            sql_type = 'DATE'
        elif isinstance(col_type, Text):
            sql_type = 'TEXT'
        elif isinstance(col_type, String):
            length = getattr(col_type, 'length', None) or 255
            sql_type = f'VARCHAR({length})'
        else:
            sql_type = 'VARCHAR(255)'

    default_clause = ''
    if column.default is not None:
        val = column.default.arg if hasattr(column.default, 'arg') else None
        if val is not None and not callable(val):
            if isinstance(val, bool):
                default_clause = f" DEFAULT {1 if val else 0}"
            elif isinstance(val, (int, float)):
                default_clause = f" DEFAULT {val}"
            elif isinstance(val, str):
                safe_val = val.replace("'", "''")
                default_clause = f" DEFAULT '{safe_val}'"

    return f"{column.name} {sql_type}{default_clause}"


def _quote_ident(name, dialect_name='sqlite'):
    """Quote a SQL identifier to prevent injection. Uses backticks for MySQL, double quotes for others."""
    if dialect_name == 'mysql':
        return '`' + name.replace('`', '``') + '`'
    return '"' + name.replace('"', '""') + '"'


def auto_migrate():
    """Automatically add missing tables and columns to the database.
    
    Compares SQLAlchemy model definitions with the actual database schema.
    Only ADDs missing columns/tables — never drops or modifies existing ones.
    Existing data is always preserved.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    dialect_name = db.engine.dialect.name
    db_tables = set(inspector.get_table_names())
    added_count = 0

    for table_name, table_obj in db.metadata.tables.items():
        if table_name not in db_tables:
            continue

        db_columns = {col['name'] for col in inspector.get_columns(table_name)}
        model_columns = [col for col in table_obj.columns if col.name not in db_columns]

        if not model_columns:
            continue

        quoted_table = _quote_ident(table_name, dialect_name)
        with db.engine.connect() as conn:
            for col in model_columns:
                col_sql = _get_column_sql(col, dialect_name)
                stmt = f"ALTER TABLE {quoted_table} ADD COLUMN {col_sql}"
                try:
                    conn.execute(text(stmt))
                    added_count += 1
                    print(f"  ✅ {table_name}.{col.name}")
                except Exception as e:
                    print(f"  ⚠️ Skipped {table_name}.{col.name}: {e}")
            conn.commit()

    if added_count > 0:
        print(f"🔄 Auto-migrate: added {added_count} new column(s)")
    else:
        print("✅ Database schema is up to date")


def fix_legacy_data():
    """Fix legacy data: assign NULL branch_id records to Pusat branch."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    dialect_name = db.engine.dialect.name
    table_names = set(inspector.get_table_names())

    if 'branches' not in table_names:
        return

    with db.engine.connect() as conn:
        result = conn.execute(text("SELECT id FROM branches WHERE code = 'PUSAT' LIMIT 1"))
        row = result.fetchone()
        if row:
            pusat_id = row[0]
            for tbl in table_names:
                if tbl == 'branches':
                    continue
                cols = {c['name'] for c in inspector.get_columns(tbl)}
                if 'branch_id' in cols:
                    quoted = _quote_ident(tbl, dialect_name)
                    result = conn.execute(
                        text(f"UPDATE {quoted} SET branch_id = :bid WHERE branch_id IS NULL"),
                        {'bid': pusat_id}
                    )
                    if result.rowcount > 0:
                        print(f"  Assigned {result.rowcount} {tbl} records to Pusat branch")
            conn.commit()

def init_db():
    with app.app_context():
        db.create_all()
        
        # Auto-migrate: add any missing columns to existing tables
        auto_migrate()
        
        # Fix legacy data
        fix_legacy_data()
        
        # Create default permissions
        permissions_data = [
            ('view_dashboard', 'Dapat melihat dashboard'),
            ('manage_orders', 'Dapat mengelola pesanan'),
            ('manage_menu', 'Dapat mengelola menu'),
            ('manage_users', 'Dapat mengelola pengguna'),
            ('manage_tables', 'Dapat mengelola meja'),
            ('view_reports', 'Dapat melihat laporan'),
            ('manage_settings', 'Dapat mengelola pengaturan'),
            ('process_payment', 'Dapat memproses pembayaran'),
            ('view_income', 'Dapat melihat penghasilan'),
            ('manage_income', 'Dapat mengelola penghasilan'),
            ('print_receipts', 'Dapat mencetak struk'),  # New permission for printing
        ]
        
        for perm_name, perm_desc in permissions_data:
            if not Permission.query.filter_by(name=perm_name).first():
                perm = Permission(name=perm_name, description=perm_desc)
                db.session.add(perm)
        
        db.session.commit()
        
        # Create default roles
        roles_data = {
            'admin': {
                'description': 'Administrator dengan akses penuh',
                'permissions': [p[0] for p in permissions_data]
            },
            'manager': {
                'description': 'Manager dengan akses laporan dan manajemen',
                'permissions': ['view_dashboard', 'manage_orders', 'manage_menu', 'view_reports', 'manage_tables', 'process_payment', 'view_income', 'print_receipts']
            },
            'kasir': {
                'description': 'Kasir untuk proses pembayaran',
                'permissions': ['view_dashboard', 'manage_orders', 'process_payment', 'print_receipts']
            },
            'koki': {
                'description': 'Koki untuk mengelola pesanan di dapur',
                'permissions': ['view_dashboard', 'manage_orders']
            },
            'printer_operator': {  # New role for dedicated printer stations
                'description': 'Operator printer untuk mencetak struk',
                'permissions': ['view_dashboard', 'print_receipts']
            },
            'customer': {
                'description': 'Pelanggan untuk memesan online',
                'permissions': ['view_dashboard']
            }
        }
        
        for role_name, role_data in roles_data.items():
            role = Role.query.filter_by(name=role_name).first()
            if not role:
                role = Role(name=role_name, description=role_data['description'])
                db.session.add(role)
                db.session.commit()
            
            # Add permissions to role
            for perm_name in role_data['permissions']:
                perm = Permission.query.filter_by(name=perm_name).first()
                if perm and perm not in role.permissions:
                    role.permissions.append(perm)
        
        db.session.commit()
        
        # Create default city
        default_city = City.query.filter_by(code='PUSAT').first()
        if not default_city:
            default_city = City(
                name='Kota Pusat',
                code='PUSAT',
                is_active=True
            )
            db.session.add(default_city)
            db.session.commit()
        
        # Create default brand
        default_brand = Brand.query.filter_by(code='DTO').first()
        if not default_brand:
            default_brand = Brand(
                name='Dapoer Teras Obor',
                code='DTO',
                description='Brand utama restoran',
                is_active=True
            )
            db.session.add(default_brand)
            db.session.commit()
        
        # Create default branch (Pusat / HQ)
        default_branch = Branch.query.filter_by(code='PUSAT').first()
        if not default_branch:
            default_branch = Branch(
                name='Cabang Pusat',
                code='PUSAT',
                address='Alamat cabang pusat',
                is_active=True,
                city_id=default_city.id,
                brand_id=default_brand.id
            )
            db.session.add(default_branch)
            db.session.commit()
        else:
            # Assign city and brand to existing Pusat branch if missing
            if default_branch.city_id is None:
                default_branch.city_id = default_city.id
            if default_branch.brand_id is None:
                default_branch.brand_id = default_brand.id
            db.session.commit()
        
        # Create default admin user (owner - no branch = sees all)
        if not User.query.filter_by(username='admin').first():
            admin_role = Role.query.filter_by(name='admin').first()
            admin = User(
                username='admin',
                email='admin@kasir.com',
                full_name='Owner / Administrator',
                force_password_change=True,
                branch_id=None  # Owner sees all branches
            )
            admin.set_password('admin123')
            admin.roles.append(admin_role)
            db.session.add(admin)
        
        # Create default kasir user (assigned to Pusat branch)
        if not User.query.filter_by(username='kasir').first():
            kasir_role = Role.query.filter_by(name='kasir').first()
            kasir = User(
                username='kasir',
                email='kasir@kasir.com',
                full_name='Kasir Utama',
                force_password_change=True,
                branch_id=default_branch.id
            )
            kasir.set_password('kasir123')
            kasir.roles.append(kasir_role)
            db.session.add(kasir)
        
        # Create default koki user (assigned to Pusat branch)
        if not User.query.filter_by(username='koki').first():
            koki_role = Role.query.filter_by(name='koki').first()
            koki = User(
                username='koki',
                email='koki@kasir.com',
                full_name='Koki Dapur',
                force_password_change=True,
                branch_id=default_branch.id
            )
            koki.set_password('koki123')
            koki.roles.append(koki_role)
            db.session.add(koki)
        
        # Create default printer operator user (assigned to Pusat branch)
        if not User.query.filter_by(username='printer').first():
            printer_role = Role.query.filter_by(name='printer_operator').first()
            printer_op = User(
                username='printer',
                email='printer@kasir.com',
                full_name='Printer Operator',
                force_password_change=True,
                branch_id=default_branch.id
            )
            printer_op.set_password('printer123')
            printer_op.roles.append(printer_role)
            db.session.add(printer_op)
        
        db.session.commit()
        
        # Create default categories
        categories_data = [
            ('Nasi Goreng', 'Menu nasi goreng berbagai varian', 'fa-bowl-rice', 1),
            ('Mie', 'Menu mie berbagai varian', 'fa-utensils', 2),
            ('Kwetiau', 'Menu kwetiau berbagai varian', 'fa-plate-wheat', 3),
            ('Menu Lain', 'Menu lainnya', 'fa-drumstick-bite', 4),
            ('Paket', 'Menu paket hemat', 'fa-box', 5),
            ('Snack', 'Makanan ringan', 'fa-cookie', 6),
            ('Minuman', 'Berbagai minuman segar', 'fa-mug-hot', 7),
        ]
        
        for cat_name, cat_desc, cat_icon, cat_order in categories_data:
            if not Category.query.filter_by(name=cat_name).first():
                cat = Category(name=cat_name, description=cat_desc, icon=cat_icon, order=cat_order, branch_id=default_branch.id)
                db.session.add(cat)
        
        db.session.commit()
        
        # Create menu items from PDF menu (Solaria style)
        seed_menu_items(default_branch.id)
        
        # Create default tables
        for i in range(1, 21):
            table_num = f"{i:02d}"
            if not Table.query.filter_by(number=table_num, branch_id=default_branch.id).first():
                table = Table(
                    number=table_num,
                    name=f"Meja {i}",
                    capacity=4 if i <= 15 else 6,
                    branch_id=default_branch.id
                )
                db.session.add(table)
        
        db.session.commit()
        
        # Create BranchMenuStock entries for default branch
        all_menu_items = MenuItem.query.all()
        for mi in all_menu_items:
            if not BranchMenuStock.query.filter_by(branch_id=default_branch.id, menu_item_id=mi.id).first():
                bms = BranchMenuStock(branch_id=default_branch.id, menu_item_id=mi.id, stock=100, is_available=True)
                db.session.add(bms)
        
        db.session.commit()
        print("Database initialized successfully!")

def seed_menu_items(branch_id=None):
    """Seed menu items from Solaria menu PDF"""
    
    # Get categories
    nasi_goreng = Category.query.filter_by(name='Nasi Goreng').first()
    mie = Category.query.filter_by(name='Mie').first()
    kwetiau = Category.query.filter_by(name='Kwetiau').first()
    menu_lain = Category.query.filter_by(name='Menu Lain').first()
    paket = Category.query.filter_by(name='Paket').first()
    snack = Category.query.filter_by(name='Snack').first()
    minuman = Category.query.filter_by(name='Minuman').first()
    
    menu_items_data = [
        # Nasi Goreng
        {'code': '111', 'name': 'Nasi Goreng Mlarat', 'price': 20000, 'category': nasi_goreng, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1512058564366-18510be2db19?w=400&h=300&fit=crop'},
        {'code': '121', 'name': 'Nasi Goreng Spesial', 'price': 22000, 'category': nasi_goreng, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1631452180519-c014fe946bc7?w=400&h=300&fit=crop'},
        {'code': '131', 'name': 'Nasi Goreng Cabe Ijo', 'price': 22000, 'category': nasi_goreng, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1603133872878-684f208fb84b?w=400&h=300&fit=crop'},
        {'code': '141', 'name': 'Nasi Goreng Sosis', 'price': 23000, 'category': nasi_goreng, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1596560548464-f010549b84d7?w=400&h=300&fit=crop'},
        {'code': '151', 'name': 'Nasi Goreng Modern Warno', 'price': 24000, 'category': nasi_goreng, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1617093727343-374698b1b08d?w=400&h=300&fit=crop'},
        {'code': '161', 'name': 'Nasi Goreng Terimaskenthir', 'price': 25000, 'category': nasi_goreng, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1512058564366-18510be2db19?w=400&h=300&fit=crop'},
        {'code': '171', 'name': 'Nasi Goreng Pete', 'price': 25000, 'category': nasi_goreng, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1569058242253-92a9c755a0ec?w=400&h=300&fit=crop'},
        {'code': '181', 'name': 'Nasi Goreng Seafood', 'price': 28000, 'category': nasi_goreng, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1512058564366-18510be2db19?w=400&h=300&fit=crop'},
        
        # Mie
        {'code': '212', 'name': 'Mie Goreng Ayam', 'price': 22000, 'category': mie, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1612874742237-6526221588e3?w=400&h=300&fit=crop'},
        {'code': '222', 'name': 'Mie Siram Ayam', 'price': 22000, 'category': mie, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1555126634-323283e090fa?w=400&h=300&fit=crop'},
        {'code': '232', 'name': 'Mie Goreng Seafood', 'price': 28000, 'category': mie, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1617093727343-374698b1b08d?w=400&h=300&fit=crop'},
        {'code': '242', 'name': 'Mie Siram Seafood', 'price': 28000, 'category': mie, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1569718212165-3a8278d5f624?w=400&h=300&fit=crop'},
        {'code': '252', 'name': 'Mie Goreng Sapi', 'price': 30000, 'category': mie, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1612874742237-6526221588e3?w=400&h=300&fit=crop'},
        {'code': '262', 'name': 'Mie Siram Sapi', 'price': 30000, 'category': mie, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1569718212165-3a8278d5f624?w=400&h=300&fit=crop'},
        
        # Kwetiau
        {'code': '414', 'name': 'Kwetiau Ayam Goreng', 'price': 25000, 'category': kwetiau, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1585032226651-759b368d7246?w=400&h=300&fit=crop'},
        {'code': '424', 'name': 'Kwetiau Ayam Siram', 'price': 25000, 'category': kwetiau, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1617093727343-374698b1b08d?w=400&h=300&fit=crop'},
        {'code': '434', 'name': 'Kwetiau Seafood Goreng', 'price': 28000, 'category': kwetiau, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1612874742237-6526221588e3?w=400&h=300&fit=crop'},
        {'code': '444', 'name': 'Kwetiau Seafood Siram', 'price': 28000, 'category': kwetiau, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1569718212165-3a8278d5f624?w=400&h=300&fit=crop'},
        {'code': '454', 'name': 'Kwetiau Sapi Goreng', 'price': 30000, 'category': kwetiau, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1585032226651-759b368d7246?w=400&h=300&fit=crop'},
        {'code': '464', 'name': 'Kwetiau Sapi Siram', 'price': 30000, 'category': kwetiau, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1617093727343-374698b1b08d?w=400&h=300&fit=crop'},
        
        # Menu Lain
        {'code': '515', 'name': 'Cap Cay Goreng Ayam', 'price': 23000, 'category': menu_lain, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1512058564366-18510be2db19?w=400&h=300&fit=crop'},
        {'code': '525', 'name': 'Cap Cay Goreng Seafood', 'price': 28000, 'category': menu_lain, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1603133872878-684f208fb84b?w=400&h=300&fit=crop'},
        {'code': '535', 'name': 'Sapo Tahu Ayam', 'price': 27000, 'category': menu_lain, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1546069901-ba9599a7e63c?w=400&h=300&fit=crop'},
        {'code': '545', 'name': 'Sapo Tahu Seafood', 'price': 30000, 'category': menu_lain, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1512058564366-18510be2db19?w=400&h=300&fit=crop'},
        {'code': '555', 'name': 'Nasi Putih', 'price': 5000, 'category': menu_lain, 'has_spicy': False, 'popular': False, 'image': 'https://images.unsplash.com/photo-1516684732162-798a0062be99?w=400&h=300&fit=crop'},
        {'code': '565', 'name': 'Telur Mata Sapi / Dadar', 'price': 5000, 'category': menu_lain, 'has_spicy': False, 'popular': False, 'image': 'https://images.unsplash.com/photo-1510693206972-df098062cb71?w=400&h=300&fit=crop'},
        
        # Snack
        {'code': '313', 'name': 'Fish Cake', 'price': 12000, 'category': snack, 'has_spicy': False, 'popular': False, 'image': 'https://images.unsplash.com/photo-1604908176997-125f25cc6f3d?w=400&h=300&fit=crop'},
        {'code': '323', 'name': 'Kentang Goreng', 'price': 15000, 'category': snack, 'has_spicy': False, 'popular': True, 'image': 'https://images.unsplash.com/photo-1576107232684-1279f390859f?w=400&h=300&fit=crop'},
        {'code': '333', 'name': 'Otak Otak', 'price': 15000, 'category': snack, 'has_spicy': False, 'popular': False, 'image': 'https://images.unsplash.com/photo-1604908176997-125f25cc6f3d?w=400&h=300&fit=crop'},
        {'code': '343', 'name': 'Sosis Goreng', 'price': 15000, 'category': snack, 'has_spicy': False, 'popular': True, 'image': 'https://images.unsplash.com/photo-1612874742237-6526221588e3?w=400&h=300&fit=crop'},
        {'code': '353', 'name': 'Sosis Bakar', 'price': 15000, 'category': snack, 'has_spicy': False, 'popular': False, 'image': 'https://images.unsplash.com/photo-1604908176997-125f25cc6f3d?w=400&h=300&fit=crop'},
        {'code': '363', 'name': 'Mix OTP', 'price': 20000, 'category': snack, 'has_spicy': False, 'popular': True, 'image': 'https://images.unsplash.com/photo-1576107232684-1279f390859f?w=400&h=300&fit=crop'},
        
        # Paket
        {'code': '616', 'name': 'Nasi Goreng Cabe Ijo + Teh', 'price': 25000, 'category': paket, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1631452180519-c014fe946bc7?w=400&h=300&fit=crop'},
        {'code': '626', 'name': 'Kwetiau Ayam Goreng + Teh', 'price': 28000, 'category': paket, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1585032226651-759b368d7246?w=400&h=300&fit=crop'},
        {'code': '636', 'name': 'Nasi Goreng Spesial + Lemon Tea', 'price': 33000, 'category': paket, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1631452180519-c014fe946bc7?w=400&h=300&fit=crop'},
        {'code': '646', 'name': 'Kwetiau Ayam Goreng + Thai Tea', 'price': 35000, 'category': paket, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1585032226651-759b368d7246?w=400&h=300&fit=crop'},
        {'code': '656', 'name': '2 Thai Tea + Kentang Goreng', 'price': 38000, 'category': paket, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1576107232684-1279f390859f?w=400&h=300&fit=crop'},
        {'code': '666', 'name': '2 Cappucino + Mix OTP', 'price': 45000, 'category': paket, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1509042239860-f550ce710b93?w=400&h=300&fit=crop'},
        {'code': '676', 'name': 'Nasi Goreng + Kwetiau Seafood + Blackcurant', 'price': 45000, 'category': paket, 'has_spicy': True, 'popular': True, 'image': 'https://images.unsplash.com/photo-1631452180519-c014fe946bc7?w=400&h=300&fit=crop'},
        {'code': '686', 'name': 'Nasi + Sapo Tahu Seafood + Lemonade', 'price': 45000, 'category': paket, 'has_spicy': True, 'popular': False, 'image': 'https://images.unsplash.com/photo-1546069901-ba9599a7e63c?w=400&h=300&fit=crop'},
        
        # Minuman
        {'code': '717', 'name': 'Teh Mlarat', 'price': 3000, 'category': minuman, 'has_spicy': False, 'popular': False, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1576092768241-dec231879fc3?w=400&h=300&fit=crop'},
        {'code': '727', 'name': 'Teh Manis', 'price': 5000, 'category': minuman, 'has_spicy': False, 'popular': True, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1576092768241-dec231879fc3?w=400&h=300&fit=crop'},
        {'code': '737', 'name': 'Air Mineral', 'price': 5000, 'category': minuman, 'has_spicy': False, 'popular': False, 'has_temp': False, 'image': 'https://images.unsplash.com/photo-1523362628745-0c100150b504?w=400&h=300&fit=crop'},
        {'code': '747', 'name': 'Kopi Hitam', 'price': 6000, 'category': minuman, 'has_spicy': False, 'popular': False, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1514432324607-a09d9b4aefdd?w=400&h=300&fit=crop'},
        {'code': '757', 'name': 'Green Tea', 'price': 13000, 'category': minuman, 'has_spicy': False, 'popular': True, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1556679343-c7306c1976bc?w=400&h=300&fit=crop'},
        {'code': '767', 'name': 'Thai Tea', 'price': 15000, 'category': minuman, 'has_spicy': False, 'popular': True, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1558857563-b371033873b8?w=400&h=300&fit=crop'},
        {'code': '777', 'name': 'Green Tea Milk', 'price': 15000, 'category': minuman, 'has_spicy': False, 'popular': True, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1515823064-d6e0c04616a7?w=400&h=300&fit=crop'},
        {'code': '787', 'name': 'Milo', 'price': 15000, 'category': minuman, 'has_spicy': False, 'popular': True, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1517578239113-b03992dcdd25?w=400&h=300&fit=crop'},
        {'code': '797', 'name': 'Thai Tea Milo', 'price': 15000, 'category': minuman, 'has_spicy': False, 'popular': True, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1558857563-b371033873b8?w=400&h=300&fit=crop'},
        {'code': '708', 'name': 'Cappucino', 'price': 15000, 'category': minuman, 'has_spicy': False, 'popular': True, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1509042239860-f550ce710b93?w=400&h=300&fit=crop'},
        {'code': '718', 'name': 'Teh Tarik', 'price': 15000, 'category': minuman, 'has_spicy': False, 'popular': True, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1571934811356-5cc061b6821f?w=400&h=300&fit=crop'},
        {'code': '728', 'name': 'Lemon Tea', 'price': 15000, 'category': minuman, 'has_spicy': False, 'popular': False, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1556679343-c7306c1976bc?w=400&h=300&fit=crop'},
        {'code': '738', 'name': 'Lemonade', 'price': 15000, 'category': minuman, 'has_spicy': False, 'popular': False, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1621263764928-df1444c5e859?w=400&h=300&fit=crop'},
        {'code': '748', 'name': 'Blackcurrant', 'price': 15000, 'category': minuman, 'has_spicy': False, 'popular': False, 'has_temp': True, 'image': 'https://images.unsplash.com/photo-1544145945-f90425340c7e?w=400&h=300&fit=crop'},
    ]
    
    for item_data in menu_items_data:
        existing_item = MenuItem.query.filter_by(code=item_data['code']).first()
        if not existing_item:
            menu_item = MenuItem(
                code=item_data['code'],
                name=item_data['name'],
                price=item_data['price'],
                category_id=item_data['category'].id if item_data['category'] else None,
                has_spicy_option=item_data.get('has_spicy', False),
                has_temperature_option=item_data.get('has_temp', False),
                is_popular=item_data.get('popular', False),
                image=item_data.get('image', ''),
                description=f"Menu {item_data['name']} yang lezat",
                branch_id=branch_id
            )
            db.session.add(menu_item)
        else:
            # Update image URL if it changed
            existing_item.image = item_data.get('image', existing_item.image)
    
    db.session.commit()


if __name__ == '__main__':
    os.makedirs('static/css', exist_ok=True)
    os.makedirs('static/js', exist_ok=True)
    os.makedirs('static/images', exist_ok=True)
    os.makedirs('static/qrcodes', exist_ok=True)
    os.makedirs('templates', exist_ok=True)
    os.makedirs('uploads', exist_ok=True)
    
    # Initialize database
    init_db()
    
    print("""
    🍽️  KASIR MODERN - FULL FEATURES
    ====================================
    🎯 FITUR:
    1. ✅ Login & Register
    2. ✅ Role & Permission
    3. ✅ Pesanan Manual & Online (QR Code)
    4. ✅ Profile & Logout
    5. ✅ Payment Gateway (Midtrans)
    6. ✅ Spice Level & Hot/Cold Options
    7. ✅ Statistics & Reports (PDF & Excel)
    8. ✅ Admin Management
    9. ✅ Income Management
    10. ✅ Menu dari PDF Solaria
    11. ✅ Kitchen Display untuk Koki
    12. ✅ Manajemen Meja (Tambah/Hapus)
    13. ✅ Printer Station dengan Role Printer Operator
    
    📱 Modern UI dengan Tailwind CSS
    🎨 Glassmorphism Design
    🔐 Secure Authentication
    
    👤 Default Login:
       Admin: admin / admin123
       Kasir: kasir / kasir123
       Koki:  koki / koki123
       Printer: printer / printer123
    
    �� Server: http://localhost:8000
    """)
    
    # Use debug mode only in development (controlled by environment variable)
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    socketio.run(app, debug=debug_mode, host='0.0.0.0', port=8000, use_reloader=debug_mode, allow_unsafe_werkzeug=True)
