from datetime import datetime

from flask import Blueprint, jsonify, request, session, current_app
from flask_login import login_required, current_user
from sqlalchemy import func
from models import db, MenuItem, Table, Order, OrderItem, Payment, Cart, CartItem, Discount, Notification, CashierShift
from extensions import limiter
from utils import (
    utc_now, get_default_branch_id, get_branch_stock, branch_filter,
    create_notification, role_required, get_user_branch_id,
)

api_orders = Blueprint('api_orders', __name__, url_prefix='')


@api_orders.route('/api/order', methods=['POST'])
@limiter.limit("60 per minute")  # Protect order creation from abuse
def api_create_order():
    try:
        # Import here to avoid circular imports
        from routes.api_print import create_print_job
        from routes.api_payments import generate_midtrans_snap_token

        data = request.json
        items = data.get('items', [])
        table_id = data.get('table_id')
        order_type = data.get('order_type', 'dine_in')
        customer_name = data.get('customer_name', '')
        notes = data.get('notes', '')
        payment_method = data.get('payment_method', 'cash')
        paid_amount = data.get('paid_amount', 0)
        
        if not items:
            return jsonify({'error': 'Keranjang kosong'}), 400
        
        # Generate order number
        order_number = f"ORD{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        # Create order
        order = Order(
            order_number=order_number,
            user_id=current_user.id if current_user.is_authenticated else None,
            table_id=table_id,
            customer_name=customer_name,
            order_type=order_type,
            notes=notes,
            branch_id=get_default_branch_id()
        )
        db.session.add(order)
        db.session.flush()
        
        # Add order items
        order_branch_id = get_default_branch_id()
        for item in items:
            menu_item = db.session.get(MenuItem, item.get('menu_item_id') or item.get('id'))
            if menu_item:
                qty = item.get('quantity', 1)
                
                # Check stock availability (per-branch)
                if order_branch_id:
                    bms = get_branch_stock(menu_item.id, order_branch_id)
                    if bms.stock < qty:
                        db.session.rollback()
                        return jsonify({'error': f'Stok "{menu_item.name}" tidak cukup (sisa {bms.stock})'}), 400
                else:
                    # Owner/admin fallback: use global stock
                    if menu_item.stock < qty:
                        db.session.rollback()
                        return jsonify({'error': f'Stok "{menu_item.name}" tidak cukup (sisa {menu_item.stock})'}), 400
                
                order_item = OrderItem(
                    order_id=order.id,
                    menu_item_id=menu_item.id,
                    name=menu_item.name,
                    price=menu_item.price,
                    quantity=qty,
                    subtotal=menu_item.price * qty,
                    spice_level=item.get('spice_level'),
                    temperature=item.get('temperature'),
                    notes=item.get('notes')
                )
                db.session.add(order_item)
                
                # Decrement stock (per-branch)
                if order_branch_id:
                    bms = get_branch_stock(menu_item.id, order_branch_id)
                    bms.stock -= qty
                else:
                    menu_item.stock -= qty
        
        # Calculate totals
        order.calculate_totals()
        
        # Apply discount if provided
        discount_amount = data.get('discount', 0)
        discount_code = data.get('discount_code')
        
        if discount_amount > 0 and discount_code:
            # Find and update discount usage
            discount = Discount.query.filter_by(code=discount_code).first()
            if discount:
                discount.usage_count += 1
            
            # Apply discount to order
            order.discount = discount_amount
            order.total = max(0, order.subtotal - discount_amount)
        
        # Create payment record
        final_total = order.total
        is_cash_paid = payment_method == 'cash' and paid_amount >= final_total
        payment = Payment(
            order_id=order.id,
            payment_method=payment_method,
            amount=final_total,
            paid_amount=paid_amount if payment_method == 'cash' else 0,
            change_amount=max(0, paid_amount - final_total) if payment_method == 'cash' else 0,
            status='paid' if is_cash_paid else 'pending'
        )
        
        if is_cash_paid:
            payment.paid_at = utc_now()
            order.status = 'processing'
        
        db.session.add(payment)
        db.session.commit()
        
        # Create notification for new order (target kasir and koki)
        total_formatted = f"{order.total:,}".replace(',', '.')
        create_notification(
            type='order_new',
            title='Pesanan Baru!',
            message=f'Order #{order.order_number} - {customer_name or "Guest"} - Rp {total_formatted}',
            data={'order_id': order.id, 'order_number': order.order_number},
            target_roles=['kasir', 'koki', 'admin', 'manager']
        )
        
        # Update table status
        if table_id:
            table = db.session.get(Table, table_id)
            if table:
                table.status = 'occupied'
                db.session.commit()
        
        # Create print job
        create_print_job(order)
        
        # For online payment (Midtrans), generate Snap token
        if payment_method == 'online':
            midtrans_order_id = f"DTO-{order.order_number}"
            payment.midtrans_order_id = midtrans_order_id
            payment.payment_method = 'midtrans'
            
            # Generate Midtrans Snap token using Snap API
            snap_token = generate_midtrans_snap_token(order, midtrans_order_id)
            
            if snap_token:
                payment.snap_token = snap_token
                db.session.commit()
                
                return jsonify({
                    'success': True,
                    'order': order.to_dict(),
                    'payment_method': 'online',
                    'snap_token': snap_token,
                    'midtrans_client_key': current_app.config.get('MIDTRANS_CLIENT_KEY'),
                    'message': 'Pesanan dibuat, silakan lakukan pembayaran.'
                })
            else:
                # Fallback: still create order but mark as pending
                db.session.commit()
                return jsonify({
                    'success': True,
                    'order': order.to_dict(),
                    'payment_method': 'online',
                    'snap_token': None,
                    'error_payment': 'Gagal menghubungi payment gateway, silakan coba lagi.',
                    'message': 'Pesanan dibuat dengan status pending.'
                })
        
        # Clear cart after successful order
        if current_user.is_authenticated:
            cart = Cart.query.filter_by(user_id=current_user.id).first()
        else:
            session_id = session.get('cart_session_id')
            cart = Cart.query.filter_by(session_id=session_id).first() if session_id else None
        
        if cart:
            CartItem.query.filter_by(cart_id=cart.id).delete()
            db.session.commit()
        
        return jsonify({
            'success': True,
            'order': order.to_dict(),
            'payment_method': 'cash',
            'message': 'Pesanan berhasil dibuat!'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@api_orders.route('/api/order/<int:order_id>', methods=['GET'])
@login_required
def api_get_order(order_id):
    """Get order details by ID for the view modal."""
    order = Order.query.get_or_404(order_id)
    return jsonify(order.to_dict())

@api_orders.route('/api/order/<int:order_id>/status', methods=['PUT'])
@login_required
def api_update_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    data = request.json
    
    new_status = data.get('status', order.status)
    old_status = order.status
    order.status = new_status
    
    # Handle table status
    if new_status in ('completed', 'cancelled') and order.table:
        order.table.status = 'available'
    
    # Restore stock when order is cancelled
    if new_status == 'cancelled' and old_status != 'cancelled':
        for item in order.items:
            if item.menu_item_id:
                menu_item = db.session.get(MenuItem, item.menu_item_id)
                if menu_item:
                    if order.branch_id:
                        bms = get_branch_stock(menu_item.id, order.branch_id)
                        bms.stock += item.quantity
                    else:
                        menu_item.stock += item.quantity
    
    db.session.commit()
    
    return jsonify({'success': True, 'order': order.to_dict()})


@api_orders.route('/api/order/<int:order_id>', methods=['PUT'])
@login_required
@role_required('admin', 'manager')
def api_edit_order(order_id):
    """Edit order details (admin/manager only)."""
    order = Order.query.get_or_404(order_id)
    data = request.json

    if 'customer_name' in data:
        order.customer_name = data['customer_name']
    if 'table_id' in data:
        if data['table_id']:
            table = db.session.get(Table, data['table_id'])
            if table:
                order.table_id = table.id
        else:
            order.table_id = None
    if 'notes' in data:
        order.notes = data['notes']

    # Edit order items
    if 'items' in data:
        for item_data in data['items']:
            item = db.session.get(OrderItem, item_data.get('id'))
            if item and item.order_id == order.id:
                if 'quantity' in item_data:
                    old_qty = item.quantity
                    new_qty = int(item_data['quantity'])
                    if new_qty <= 0:
                        continue
                    diff = new_qty - old_qty
                    item.quantity = new_qty
                    item.subtotal = item.price * new_qty
                    # Adjust stock
                    if item.menu_item_id and diff != 0:
                        menu_item = db.session.get(MenuItem, item.menu_item_id)
                        if menu_item:
                            if order.branch_id:
                                bms = get_branch_stock(menu_item.id, order.branch_id)
                                bms.stock -= diff
                            else:
                                menu_item.stock -= diff
                if 'notes' in item_data:
                    item.notes = item_data['notes']

        # Recalculate totals
        order.subtotal = sum(i.subtotal for i in order.items)
        order.total = order.subtotal - (order.discount or 0)

    db.session.commit()
    return jsonify({'success': True, 'order': order.to_dict()})


@api_orders.route('/api/order/<int:order_id>', methods=['DELETE'])
@login_required
@role_required('admin', 'manager')
def api_delete_order(order_id):
    """Delete an order (admin/manager only). Restores stock."""
    order = Order.query.get_or_404(order_id)

    # Restore stock for non-cancelled orders
    if order.status != 'cancelled':
        for item in order.items:
            if item.menu_item_id:
                menu_item = db.session.get(MenuItem, item.menu_item_id)
                if menu_item:
                    if order.branch_id:
                        bms = get_branch_stock(menu_item.id, order.branch_id)
                        bms.stock += item.quantity
                    else:
                        menu_item.stock += item.quantity

    # Free up table
    if order.table:
        order.table.status = 'available'

    # Delete order (cascade removes items and payment)
    db.session.delete(order)
    db.session.commit()

    return jsonify({'success': True, 'message': 'Pesanan berhasil dihapus'})

@api_orders.route('/api/kitchen/orders')
@login_required
@role_required('admin', 'manager', 'koki', 'kasir')
def api_kitchen_orders():
    """Get orders for kitchen display"""
    # Get orders from today that are not completed/cancelled
    today = utc_now().date()
    orders = branch_filter(Order.query, Order).filter(
        Order.created_at >= datetime.combine(today, datetime.min.time()),
        Order.status.in_(['pending', 'processing'])
    ).order_by(Order.created_at.asc()).all()
    
    result = []
    for order in orders:
        order_data = {
            'id': order.id,
            'order_number': order.order_number,
            'table': order.table.number if order.table else 'Takeaway',
            'customer_name': order.customer_name or 'Guest',
            'order_type': order.order_type,
            'status': order.status,
            'created_at': order.created_at.strftime('%H:%M'),
            'items': []
        }
        
        for item in order.items:
            order_data['items'].append({
                'id': item.id,
                'name': item.name,
                'quantity': item.quantity,
                'spice_level': item.spice_level,
                'temperature': item.temperature,
                'notes': item.notes,
                'item_status': item.item_status or 'pending'
            })
        
        result.append(order_data)
    
    return jsonify(result)

@api_orders.route('/api/kitchen/item/<int:item_id>/status', methods=['PUT'])
@login_required
@role_required('admin', 'manager', 'koki', 'kasir')
def api_update_item_status(item_id):
    """Update individual item status"""
    item = OrderItem.query.get_or_404(item_id)
    data = request.get_json()
    new_status = data.get('status')
    
    if new_status not in ['pending', 'cooking', 'ready', 'served']:
        return jsonify({'error': 'Invalid status'}), 400
    
    item.item_status = new_status
    db.session.commit()
    
    # Check if all items in order are ready/served, update order status
    order = item.order
    all_items_status = [i.item_status for i in order.items]
    
    if all(s == 'served' for s in all_items_status):
        order.status = 'completed'
        if order.table:
            order.table.status = 'available'
    elif all(s in ['ready', 'served'] for s in all_items_status):
        order.status = 'processing'  # Ready to serve
    elif any(s == 'cooking' for s in all_items_status):
        order.status = 'processing'
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'item_id': item_id,
        'status': new_status,
        'order_status': order.status
    })

@api_orders.route('/api/kitchen/order/<int:order_id>/status', methods=['PUT'])
@login_required
@role_required('admin', 'manager', 'koki', 'kasir')
def api_update_order_kitchen_status(order_id):
    """Update all items in an order to a status"""
    order = Order.query.get_or_404(order_id)
    data = request.get_json()
    new_status = data.get('status')
    
    if new_status not in ['pending', 'cooking', 'ready', 'served']:
        return jsonify({'error': 'Invalid status'}), 400
    
    # Update all items
    for item in order.items:
        item.item_status = new_status
    
    # Update order status
    if new_status == 'served':
        order.status = 'completed'
        if order.table:
            order.table.status = 'available'
    elif new_status in ['cooking', 'ready']:
        order.status = 'processing'
    else:
        order.status = 'pending'
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'order_id': order_id,
        'item_status': new_status,
        'order_status': order.status
    })


@api_orders.route('/api/shift/open', methods=['POST'])
@login_required
@role_required('admin', 'manager', 'kasir')
def api_shift_open():
    """Open a new cashier shift"""
    # Check if user already has an open shift
    open_shift = CashierShift.query.filter_by(user_id=current_user.id, status='open').first()
    if open_shift:
        return jsonify({'success': False, 'error': 'Anda sudah memiliki shift yang aktif'}), 400
    
    data = request.get_json()
    opening_cash = int(data.get('opening_cash', 0))
    
    shift = CashierShift(
        user_id=current_user.id,
        opening_cash=opening_cash,
        notes=data.get('notes', ''),
        branch_id=get_default_branch_id()
    )
    db.session.add(shift)
    db.session.commit()
    
    return jsonify({'success': True, 'shift': shift.to_dict()})

@api_orders.route('/api/shift/close', methods=['POST'])
@login_required
@role_required('admin', 'manager', 'kasir')
def api_shift_close():
    """Close the current cashier shift"""
    shift = CashierShift.query.filter_by(user_id=current_user.id, status='open').first()
    if not shift:
        return jsonify({'success': False, 'error': 'Tidak ada shift yang aktif'}), 400
    
    data = request.get_json()
    shift.closing_cash = int(data.get('closing_cash', 0))
    shift.end_time = utc_now()
    shift.status = 'closed'
    shift.notes = data.get('notes', shift.notes)
    
    # Calculate total sales during shift using database aggregation
    shift_stats = db.session.query(
        func.count(Order.id).label('total_orders'),
        func.coalesce(func.sum(Order.total), 0).label('total_sales')
    ).filter(
        Order.created_at >= shift.start_time,
        Order.created_at <= shift.end_time,
        Order.user_id == current_user.id,
        Order.status.in_(['processing', 'completed'])
    ).first()
    
    shift.total_orders = shift_stats.total_orders
    shift.total_sales = shift_stats.total_sales
    
    db.session.commit()
    
    return jsonify({'success': True, 'shift': shift.to_dict()})

@api_orders.route('/api/shift/current')
@login_required
def api_shift_current():
    """Get current open shift for the user"""
    shift = CashierShift.query.filter_by(user_id=current_user.id, status='open').first()
    return jsonify({
        'success': True,
        'shift': shift.to_dict() if shift else None
    })


# Statistics API
@api_orders.route('/api/stats')
@login_required
def api_get_stats():
    today = datetime.now().date()
    
    # Today's stats
    today_orders = branch_filter(Order.query, Order).filter(
        db.func.date(Order.created_at) == today
    ).all()
    
    total_income = sum(o.total for o in today_orders if o.payment and o.payment.status == 'paid')
    total_orders = len(today_orders)
    
    # Popular items today
    popular_items = {}
    for order in today_orders:
        for item in order.items:
            if item.name not in popular_items:
                popular_items[item.name] = 0
            popular_items[item.name] += item.quantity
    
    most_popular = sorted(popular_items.items(), key=lambda x: x[1], reverse=True)[:5]
    
    return jsonify({
        'total_income': total_income,
        'total_orders': total_orders,
        'most_popular': most_popular,
        'average_transaction': total_income / total_orders if total_orders > 0 else 0
    })


# ========== NOTIFICATION SYSTEM ==========
@api_orders.route('/api/notifications')
@login_required
def api_get_notifications():
    """Get notifications for current user"""
    notifications = branch_filter(Notification.query, Notification).filter(
        (Notification.user_id == current_user.id) | (Notification.user_id == None)
    ).order_by(Notification.created_at.desc()).limit(50).all()
    
    unread_count = branch_filter(Notification.query, Notification).filter(
        ((Notification.user_id == current_user.id) | (Notification.user_id == None)),
        Notification.is_read == False
    ).count()
    
    return jsonify({
        'notifications': [n.to_dict() for n in notifications],
        'unread_count': unread_count
    })


@api_orders.route('/api/notifications/<int:notification_id>/read', methods=['POST'])
@login_required
def api_mark_notification_read(notification_id):
    """Mark a notification as read"""
    notification = Notification.query.get_or_404(notification_id)
    notification.is_read = True
    notification.read_at = utc_now()
    db.session.commit()
    return jsonify({'success': True})


@api_orders.route('/api/notifications/read-all', methods=['POST'])
@login_required
def api_mark_all_notifications_read():
    """Mark all notifications as read for current user"""
    Notification.query.filter(
        ((Notification.user_id == current_user.id) | (Notification.user_id == None)),
        Notification.is_read == False
    ).update({'is_read': True, 'read_at': utc_now()}, synchronize_session=False)
    db.session.commit()
    return jsonify({'success': True})
