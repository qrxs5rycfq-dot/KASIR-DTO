from datetime import datetime, timezone


def utc_now():
    """Return current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# Association table for User-Role many-to-many relationship
user_roles = db.Table('user_roles',
    db.Column('user_id', db.Integer, db.ForeignKey('users.id'), primary_key=True),
    db.Column('role_id', db.Integer, db.ForeignKey('roles.id'), primary_key=True)
)

# Association table for Role-Permission many-to-many relationship  
role_permissions = db.Table('role_permissions',
    db.Column('role_id', db.Integer, db.ForeignKey('roles.id'), primary_key=True),
    db.Column('permission_id', db.Integer, db.ForeignKey('permissions.id'), primary_key=True)
)

class Permission(db.Model):
    __tablename__ = 'permissions'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utc_now)
    
    def __repr__(self):
        return f'<Permission {self.name}>'

class Role(db.Model):
    __tablename__ = 'roles'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    
    permissions = db.relationship('Permission', secondary=role_permissions, 
                                  backref=db.backref('roles', lazy='dynamic'))
    
    def has_permission(self, permission_name):
        return any(p.name == permission_name for p in self.permissions)
    
    def __repr__(self):
        return f'<Role {self.name}>'

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    avatar = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)
    force_password_change = db.Column(db.Boolean, default=False)  # Force password change on first login
    printer_name = db.Column(db.String(100))  # Store last connected printer name
    printer_id = db.Column(db.String(100))  # Store Bluetooth device ID for auto-reconnect
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)  # NULL = owner/admin sees all
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    last_login = db.Column(db.DateTime)
    
    roles = db.relationship('Role', secondary=user_roles,
                           backref=db.backref('users', lazy='dynamic'))
    orders = db.relationship('Order', backref='user', lazy='dynamic')
    branch = db.relationship('Branch', backref=db.backref('users', lazy='dynamic'))
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def has_role(self, role_name):
        return any(r.name == role_name for r in self.roles)
    
    def has_permission(self, permission_name):
        for role in self.roles:
            if role.has_permission(permission_name):
                return True
        return False
    
    def get_primary_role(self):
        if self.roles:
            # Priority: admin > manager > kasir > customer
            role_priority = {'admin': 0, 'manager': 1, 'kasir': 2, 'customer': 3}
            sorted_roles = sorted(self.roles, key=lambda r: role_priority.get(r.name, 99))
            return sorted_roles[0].name
        return 'customer'
    
    def __repr__(self):
        return f'<User {self.username}>'

class Category(db.Model):
    __tablename__ = 'categories'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    icon = db.Column(db.String(50))
    order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    
    menu_items = db.relationship('MenuItem', backref='category', lazy='dynamic')
    branch = db.relationship('Branch', backref=db.backref('categories', lazy='dynamic'))
    
    def __repr__(self):
        return f'<Category {self.name}>'

class MenuItem(db.Model):
    __tablename__ = 'menu_items'
    
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20))
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Integer, nullable=False)
    image = db.Column(db.String(500))
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'))
    is_popular = db.Column(db.Boolean, default=False)
    is_available = db.Column(db.Boolean, default=True)
    has_spicy_option = db.Column(db.Boolean, default=False)
    has_temperature_option = db.Column(db.Boolean, default=False)  # For drinks (hot/cold)
    stock = db.Column(db.Integer, default=100)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    branch = db.relationship('Branch', backref=db.backref('menu_items', lazy='dynamic'))
    
    def to_dict(self):
        return {
            'id': self.id,
            'code': self.code,
            'name': self.name,
            'description': self.description,
            'price': self.price,
            'image': self.image,
            'category': self.category.name if self.category else None,
            'category_id': self.category_id,
            'is_popular': self.is_popular,
            'is_available': self.is_available,
            'has_spicy_option': self.has_spicy_option,
            'has_temperature_option': self.has_temperature_option,
            'stock': self.stock
        }
    
    def __repr__(self):
        return f'<MenuItem {self.name}>'

class Table(db.Model):
    __tablename__ = 'tables'
    __table_args__ = (db.UniqueConstraint('number', 'branch_id', name='uq_table_number_branch'),)
    
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(100))
    capacity = db.Column(db.Integer, default=4)
    qr_code = db.Column(db.String(255))
    status = db.Column(db.String(20), default='available')  # available, occupied, reserved
    is_active = db.Column(db.Boolean, default=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    
    orders = db.relationship('Order', backref='table', lazy='dynamic')
    branch = db.relationship('Branch', backref=db.backref('tables', lazy='dynamic'))
    
    def __repr__(self):
        return f'<Table {self.number}>'

class Order(db.Model):
    __tablename__ = 'orders'
    
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(50), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    table_id = db.Column(db.Integer, db.ForeignKey('tables.id'))
    customer_name = db.Column(db.String(100))
    order_type = db.Column(db.String(20), default='dine_in')  # dine_in, takeaway, online
    source = db.Column(db.String(30), default='pos')  # pos, grabfood, gofood, shopeefood, online
    status = db.Column(db.String(20), default='pending')  # pending, processing, completed, cancelled
    subtotal = db.Column(db.Integer, default=0)
    tax = db.Column(db.Integer, default=0)
    discount = db.Column(db.Integer, default=0)
    total = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    items = db.relationship('OrderItem', backref='order', lazy='dynamic', cascade='all, delete-orphan')
    payment = db.relationship('Payment', backref='order', uselist=False, cascade='all, delete-orphan')
    branch = db.relationship('Branch', backref=db.backref('orders', lazy='dynamic'))
    
    def calculate_totals(self):
        self.subtotal = sum(item.subtotal for item in self.items)
        self.tax = 0
        self.total = self.subtotal - self.discount
        
    def to_dict(self):
        return {
            'id': self.id,
            'order_number': self.order_number,
            'customer_name': self.customer_name,
            'table': self.table.number if self.table else None,
            'order_type': self.order_type,
            'source': self.source,
            'status': self.status,
            'subtotal': self.subtotal,
            'tax': self.tax,
            'discount': self.discount,
            'total': self.total,
            'notes': self.notes,
            'items': [item.to_dict() for item in self.items],
            'payment': self.payment.to_dict() if self.payment else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<Order {self.order_number}>'

class OrderItem(db.Model):
    __tablename__ = 'order_items'
    
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id'))
    name = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    menu_price_snapshot = db.Column(db.Integer, nullable=True)  # Price from menu at time of order
    quantity = db.Column(db.Integer, default=1)
    subtotal = db.Column(db.Integer, nullable=False)
    spice_level = db.Column(db.String(20))  # none, mild, medium, hot, extra_hot
    temperature = db.Column(db.String(20))  # hot, cold, normal
    notes = db.Column(db.Text)
    item_status = db.Column(db.String(20), default='pending')  # pending, cooking, ready, served
    created_at = db.Column(db.DateTime, default=utc_now)
    
    menu_item = db.relationship('MenuItem')
    
    def to_dict(self):
        return {
            'id': self.id,
            'menu_item_id': self.menu_item_id,
            'name': self.name,
            'price': self.price,
            'menu_price_snapshot': self.menu_price_snapshot,
            'quantity': self.quantity,
            'subtotal': self.subtotal,
            'spice_level': self.spice_level,
            'temperature': self.temperature,
            'notes': self.notes,
            'item_status': self.item_status
        }
    
    def __repr__(self):
        return f'<OrderItem {self.name} x{self.quantity}>'

class Payment(db.Model):
    __tablename__ = 'payments'
    
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    payment_method = db.Column(db.String(50), nullable=False)  # cash, midtrans, transfer
    amount = db.Column(db.Integer, nullable=False)
    paid_amount = db.Column(db.Integer, default=0)
    change_amount = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default='pending')  # pending, paid, failed, refunded
    midtrans_order_id = db.Column(db.String(100))
    midtrans_transaction_id = db.Column(db.String(100))
    midtrans_status = db.Column(db.String(50))
    snap_token = db.Column(db.String(255))  # Midtrans Snap token
    payment_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=utc_now)
    paid_at = db.Column(db.DateTime)
    
    def to_dict(self):
        return {
            'id': self.id,
            'payment_method': self.payment_method,
            'amount': self.amount,
            'paid_amount': self.paid_amount,
            'change_amount': self.change_amount,
            'status': self.status,
            'payment_url': self.payment_url,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'paid_at': self.paid_at.isoformat() if self.paid_at else None
        }
    
    def __repr__(self):
        return f'<Payment {self.id} - {self.status}>'

class Income(db.Model):
    __tablename__ = 'incomes'
    
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    total_orders = db.Column(db.Integer, default=0)
    total_income = db.Column(db.Integer, default=0)
    total_tax = db.Column(db.Integer, default=0)
    total_discount = db.Column(db.Integer, default=0)
    net_income = db.Column(db.Integer, default=0)
    cash_income = db.Column(db.Integer, default=0)
    online_income = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    def __repr__(self):
        return f'<Income {self.date}>'

class Setting(db.Model):
    __tablename__ = 'settings'
    
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)
    description = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    def __repr__(self):
        return f'<Setting {self.key}>'

class Cart(db.Model):
    """Shopping cart stored in database - supports multiple items per user/session"""
    __tablename__ = 'carts'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    session_id = db.Column(db.String(100), nullable=True)  # For guest users
    table_id = db.Column(db.Integer, db.ForeignKey('tables.id'), nullable=True)
    order_type = db.Column(db.String(20), default='dine_in')
    customer_name = db.Column(db.String(100))
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    items = db.relationship('CartItem', backref='cart', lazy='dynamic', cascade='all, delete-orphan')
    user = db.relationship('User', backref='carts')
    table = db.relationship('Table')
    
    def get_subtotal(self):
        return sum(item.subtotal for item in self.items)
    
    def get_tax(self):
        return 0
    
    def get_total(self):
        return self.get_subtotal()
    
    def get_item_count(self):
        return sum(item.quantity for item in self.items)
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'table_id': self.table_id,
            'order_type': self.order_type,
            'customer_name': self.customer_name,
            'items': [item.to_dict() for item in self.items],
            'subtotal': self.get_subtotal(),
            'tax': self.get_tax(),
            'total': self.get_total(),
            'item_count': self.get_item_count()
        }
    
    def __repr__(self):
        return f'<Cart {self.id}>'

class CartItem(db.Model):
    """Individual item in a shopping cart"""
    __tablename__ = 'cart_items'
    
    id = db.Column(db.Integer, primary_key=True)
    cart_id = db.Column(db.Integer, db.ForeignKey('carts.id'), nullable=False)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, default=1)
    subtotal = db.Column(db.Integer, nullable=False)
    spice_level = db.Column(db.String(20))  # none, mild, medium, hot, extra_hot
    temperature = db.Column(db.String(20))  # hot, cold, normal
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    menu_item = db.relationship('MenuItem')
    
    def update_subtotal(self):
        self.subtotal = self.price * self.quantity
    
    def to_dict(self):
        return {
            'id': self.id,
            'menu_item_id': self.menu_item_id,
            'name': self.name,
            'price': self.price,
            'quantity': self.quantity,
            'subtotal': self.subtotal,
            'spice_level': self.spice_level,
            'temperature': self.temperature,
            'notes': self.notes
        }
    
    def __repr__(self):
        return f'<CartItem {self.name} x{self.quantity}>'


class Discount(db.Model):
    """Discount and promo model"""
    __tablename__ = 'discounts'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(50), unique=True, nullable=False)  # Promo code
    description = db.Column(db.Text)
    discount_type = db.Column(db.String(20), default='percentage')  # percentage, fixed
    value = db.Column(db.Integer, nullable=False)  # Percentage (0-100) or fixed amount
    min_purchase = db.Column(db.Integer, default=0)  # Minimum purchase amount to apply
    max_discount = db.Column(db.Integer, nullable=True)  # Maximum discount for percentage type
    usage_limit = db.Column(db.Integer, nullable=True)  # Total usage limit (null = unlimited)
    usage_count = db.Column(db.Integer, default=0)  # Current usage count
    start_date = db.Column(db.DateTime, nullable=True)  # Start date for promo
    end_date = db.Column(db.DateTime, nullable=True)  # End date for promo
    is_active = db.Column(db.Boolean, default=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    def is_valid(self, subtotal=0):
        """Check if discount is valid for the given subtotal"""
        now = utc_now()
        
        # Check if active
        if not self.is_active:
            return False, "Promo tidak aktif"
        
        # Check date range - handle both naive and aware datetime comparison
        if self.start_date:
            start = self.start_date
            # Make naive datetime timezone-aware (assume UTC)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if now < start:
                return False, "Promo belum dimulai"
        
        if self.end_date:
            end = self.end_date
            # Make naive datetime timezone-aware (assume UTC)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if now > end:
                return False, "Promo sudah berakhir"
        
        # Check usage limit
        if self.usage_limit and self.usage_count >= self.usage_limit:
            return False, "Promo sudah mencapai batas penggunaan"
        
        # Check minimum purchase
        if subtotal < self.min_purchase:
            return False, f"Minimum pembelian Rp {self.min_purchase:,}".replace(",", ".")
        
        return True, "Valid"
    
    def calculate_discount(self, subtotal):
        """Calculate discount amount for given subtotal"""
        if self.discount_type == 'percentage':
            discount = int(subtotal * self.value / 100)
            # Apply max discount cap if set
            if self.max_discount and discount > self.max_discount:
                discount = self.max_discount
            return discount
        else:  # fixed
            return min(self.value, subtotal)  # Can't discount more than subtotal
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'code': self.code,
            'description': self.description,
            'discount_type': self.discount_type,
            'value': self.value,
            'min_purchase': self.min_purchase,
            'max_discount': self.max_discount,
            'usage_limit': self.usage_limit,
            'usage_count': self.usage_count,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<Discount {self.code}>'


class City(db.Model):
    """City/Kota model for multi-outlet hierarchy. Branch belongs to a City and a Brand."""
    __tablename__ = 'cities'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    branches = db.relationship('Branch', backref='city', lazy='dynamic')
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'code': self.code,
            'is_active': self.is_active,
            'branch_count': self.branches.count(),
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<City {self.name}>'


class Brand(db.Model):
    """Brand/Merek model for multi-outlet hierarchy. Branch belongs to a City and a Brand."""
    __tablename__ = 'brands'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    code = db.Column(db.String(20), unique=True, nullable=False)
    description = db.Column(db.Text)
    logo = db.Column(db.String(255))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    branches = db.relationship('Branch', backref='brand', lazy='dynamic')
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'code': self.code,
            'description': self.description,
            'logo': self.logo,
            'is_active': self.is_active,
            'branch_count': self.branches.count(),
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<Brand {self.name}>'


class Branch(db.Model):
    """Branch/Cabang model for multi-location restaurant management"""
    __tablename__ = 'branches'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(20), unique=True, nullable=False)
    address = db.Column(db.Text)
    phone = db.Column(db.String(20))
    manager_name = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)
    opening_time = db.Column(db.String(5), default='08:00')
    closing_time = db.Column(db.String(5), default='22:00')
    city_id = db.Column(db.Integer, db.ForeignKey('cities.id'), nullable=True)
    brand_id = db.Column(db.Integer, db.ForeignKey('brands.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'code': self.code,
            'address': self.address,
            'phone': self.phone,
            'manager_name': self.manager_name,
            'is_active': self.is_active,
            'opening_time': self.opening_time,
            'closing_time': self.closing_time,
            'city_id': self.city_id,
            'city_name': self.city.name if self.city else None,
            'brand_id': self.brand_id,
            'brand_name': self.brand.name if self.brand else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<Branch {self.name}>'


class BranchMenuStock(db.Model):
    """Per-branch stock and availability for shared menu items"""
    __tablename__ = 'branch_menu_stock'
    __table_args__ = (db.UniqueConstraint('branch_id', 'menu_item_id', name='uq_branch_menu'),)
    
    id = db.Column(db.Integer, primary_key=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=False)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id'), nullable=False)
    stock = db.Column(db.Integer, default=100)
    is_available = db.Column(db.Boolean, default=True)
    
    branch = db.relationship('Branch', backref=db.backref('menu_stocks', lazy='dynamic'))
    menu_item = db.relationship('MenuItem', backref=db.backref('branch_stocks', lazy='dynamic'))
    
    def __repr__(self):
        return f'<BranchMenuStock branch={self.branch_id} item={self.menu_item_id} stock={self.stock}>'


class CashierShift(db.Model):
    """Shift management for cashier operations"""
    __tablename__ = 'cashier_shifts'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False, default=utc_now)
    end_time = db.Column(db.DateTime, nullable=True)
    opening_cash = db.Column(db.Integer, default=0)
    closing_cash = db.Column(db.Integer, nullable=True)
    total_sales = db.Column(db.Integer, default=0)
    total_orders = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), default='open')  # open, closed
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    
    user = db.relationship('User', backref=db.backref('shifts', lazy='dynamic'))
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'user_name': self.user.full_name or self.user.username if self.user else None,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'opening_cash': self.opening_cash,
            'closing_cash': self.closing_cash,
            'total_sales': self.total_sales,
            'total_orders': self.total_orders,
            'notes': self.notes,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<CashierShift {self.id} - {self.status}>'


class Expense(db.Model):
    """Expense tracking for restaurant operations"""
    __tablename__ = 'expenses'
    
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=lambda: utc_now().date())
    category = db.Column(db.String(50), nullable=False)  # ingredients, utilities, salary, supplies, maintenance, other
    description = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    notes = db.Column(db.Text)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    
    user = db.relationship('User', backref=db.backref('expenses', lazy='dynamic'))
    
    CATEGORIES = {
        'ingredients': 'Bahan Baku',
        'utilities': 'Utilitas (Listrik/Air/Gas)',
        'salary': 'Gaji Karyawan',
        'supplies': 'Perlengkapan',
        'maintenance': 'Perawatan',
        'other': 'Lainnya'
    }
    
    def to_dict(self):
        return {
            'id': self.id,
            'date': self.date.isoformat() if self.date else None,
            'category': self.category,
            'category_label': self.CATEGORIES.get(self.category, self.category),
            'description': self.description,
            'amount': self.amount,
            'notes': self.notes,
            'user_name': (self.user.full_name or self.user.username) if self.user else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<Expense {self.id} - {self.category} - {self.amount}>'


class Notification(db.Model):
    """Notification model for real-time alerts"""
    __tablename__ = 'notifications'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # null = broadcast to all
    type = db.Column(db.String(50), nullable=False)  # order_new, payment_success, payment_failed, order_ready
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text)
    data = db.Column(db.Text)  # JSON data for additional info
    is_read = db.Column(db.Boolean, default=False)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    read_at = db.Column(db.DateTime, nullable=True)
    
    # Relationship
    user = db.relationship('User', backref=db.backref('notifications', lazy='dynamic'))
    
    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'type': self.type,
            'title': self.title,
            'message': self.message,
            'data': self.data,
            'is_read': self.is_read,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'read_at': self.read_at.isoformat() if self.read_at else None
        }
    
    def __repr__(self):
        return f'<Notification {self.id} - {self.type}>'


class PendingPrint(db.Model):
    """Server-side pending print queue for reliable printing across page navigations"""
    __tablename__ = 'pending_prints'
    
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)
    receipt_data = db.Column(db.Text, nullable=False)  # JSON encoded ESC/POS commands
    copies = db.Column(db.Integer, default=3)  # Number of copies to print
    current_copy = db.Column(db.Integer, default=1)  # Current copy being printed
    status = db.Column(db.String(20), default='pending')  # pending, printing, completed, failed
    retry_count = db.Column(db.Integer, default=0)
    error_message = db.Column(db.Text, nullable=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    updated_at = db.Column(db.DateTime, default=utc_now, onupdate=utc_now)
    printed_at = db.Column(db.DateTime, nullable=True)
    
    # Relationship
    order = db.relationship('Order', backref=db.backref('pending_prints', lazy='dynamic'))
    
    def to_dict(self):
        return {
            'id': self.id,
            'order_id': self.order_id,
            'receipt_data': self.receipt_data,
            'copies': self.copies,
            'current_copy': self.current_copy,
            'status': self.status,
            'retry_count': self.retry_count,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'printed_at': self.printed_at.isoformat() if self.printed_at else None
        }
    
    def __repr__(self):
        return f'<PendingPrint {self.id} order={self.order_id} status={self.status}>'


class ExternalOrder(db.Model):
    """Track orders received from external food delivery platforms"""
    __tablename__ = 'external_orders'
    
    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(30), nullable=False)  # grabfood, gofood, shopeefood
    external_order_id = db.Column(db.String(100), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=True)
    raw_data = db.Column(db.Text)  # Store original webhook payload as JSON
    status = db.Column(db.String(30), default='received')  # received, accepted, printed, failed
    error_message = db.Column(db.Text, nullable=True)
    signature_status = db.Column(db.String(20), default='unchecked')  # unchecked, valid, invalid, skipped
    retry_count = db.Column(db.Integer, default=0)
    last_retry_at = db.Column(db.DateTime, nullable=True)
    branch_id = db.Column(db.Integer, db.ForeignKey('branches.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    processed_at = db.Column(db.DateTime, nullable=True)
    accepted_at = db.Column(db.DateTime, nullable=True)
    printed_at = db.Column(db.DateTime, nullable=True)
    
    order = db.relationship('Order', backref=db.backref('external_order', uselist=False))
    branch = db.relationship('Branch', backref=db.backref('external_orders', lazy='dynamic'))
    
    def to_dict(self):
        return {
            'id': self.id,
            'platform': self.platform,
            'external_order_id': self.external_order_id,
            'order_id': self.order_id,
            'status': self.status,
            'signature_status': self.signature_status,
            'retry_count': self.retry_count,
            'error_message': self.error_message,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'processed_at': self.processed_at.isoformat() if self.processed_at else None,
            'accepted_at': self.accepted_at.isoformat() if self.accepted_at else None,
            'printed_at': self.printed_at.isoformat() if self.printed_at else None
        }
    
    def __repr__(self):
        return f'<ExternalOrder {self.platform}:{self.external_order_id}>'


class WebhookLog(db.Model):
    """Log all webhook attempts including signature failures"""
    __tablename__ = 'webhook_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(30), nullable=False)
    request_ip = db.Column(db.String(45))
    request_headers = db.Column(db.Text)  # JSON: relevant headers for debugging
    request_body_hash = db.Column(db.String(64))  # SHA-256 hash of body (not the body itself for security)
    signature_provided = db.Column(db.String(255))
    signature_expected_hash = db.Column(db.String(64))  # Hash of expected (not the value itself)
    result = db.Column(db.String(20), nullable=False)  # success, sig_invalid, sig_missing, error, rate_limited
    error_detail = db.Column(db.Text, nullable=True)
    external_order_id = db.Column(db.Integer, db.ForeignKey('external_orders.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now)
    
    def to_dict(self):
        return {
            'id': self.id,
            'platform': self.platform,
            'request_ip': self.request_ip,
            'result': self.result,
            'error_detail': self.error_detail,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
