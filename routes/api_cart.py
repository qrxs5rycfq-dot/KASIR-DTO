import uuid

from flask import Blueprint, jsonify, request, session
from flask_login import current_user
from models import db, MenuItem, Cart, CartItem
from utils import get_default_branch_id

api_cart = Blueprint('api_cart', __name__, url_prefix='')


def get_or_create_cart():
    """Get current user's cart or create new one"""
    if current_user.is_authenticated:
        cart = Cart.query.filter_by(user_id=current_user.id).first()
        if not cart:
            cart = Cart(user_id=current_user.id, branch_id=get_default_branch_id())
            db.session.add(cart)
            db.session.commit()
    else:
        # For guest users, use session
        session_id = session.get('cart_session_id')
        if not session_id:
            session_id = str(uuid.uuid4())
            session['cart_session_id'] = session_id
        
        cart = Cart.query.filter_by(session_id=session_id).first()
        if not cart:
            cart = Cart(session_id=session_id)
            db.session.add(cart)
            db.session.commit()
    
    return cart

@api_cart.route('/api/cart')
def api_get_cart():
    """Get current cart items"""
    cart = get_or_create_cart()
    return jsonify({'success': True, 'cart': cart.to_dict()})

@api_cart.route('/api/cart/add', methods=['POST'])
def api_add_to_cart():
    """Add item to cart"""
    try:
        data = request.json
        menu_item_id = data.get('menu_item_id')
        quantity = data.get('quantity', 1)
        spice_level = data.get('spice_level')
        temperature = data.get('temperature')
        notes = data.get('notes', '')
        
        menu_item = db.session.get(MenuItem, menu_item_id)
        if not menu_item:
            return jsonify({'success': False, 'error': 'Menu item not found'}), 404
        
        cart = get_or_create_cart()
        
        # Check if same item with same options exists
        existing_item = CartItem.query.filter_by(
            cart_id=cart.id,
            menu_item_id=menu_item_id,
            spice_level=spice_level,
            temperature=temperature,
            notes=notes
        ).first()
        
        if existing_item:
            existing_item.quantity += quantity
            existing_item.update_subtotal()
        else:
            cart_item = CartItem(
                cart_id=cart.id,
                menu_item_id=menu_item_id,
                name=menu_item.name,
                price=menu_item.price,
                quantity=quantity,
                subtotal=menu_item.price * quantity,
                spice_level=spice_level,
                temperature=temperature,
                notes=notes
            )
            db.session.add(cart_item)
        
        db.session.commit()
        return jsonify({'success': True, 'cart': cart.to_dict()})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@api_cart.route('/api/cart/update/<int:item_id>', methods=['PUT'])
def api_update_cart_item(item_id):
    """Update cart item quantity"""
    try:
        data = request.json
        quantity = data.get('quantity', 1)
        
        cart = get_or_create_cart()
        cart_item = CartItem.query.filter_by(id=item_id, cart_id=cart.id).first()
        
        if not cart_item:
            return jsonify({'success': False, 'error': 'Item not found'}), 404
        
        if quantity <= 0:
            db.session.delete(cart_item)
        else:
            cart_item.quantity = quantity
            cart_item.update_subtotal()
        
        db.session.commit()
        return jsonify({'success': True, 'cart': cart.to_dict()})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@api_cart.route('/api/cart/remove/<int:item_id>', methods=['DELETE'])
def api_remove_cart_item(item_id):
    """Remove item from cart"""
    try:
        cart = get_or_create_cart()
        cart_item = CartItem.query.filter_by(id=item_id, cart_id=cart.id).first()
        
        if not cart_item:
            return jsonify({'success': False, 'error': 'Item not found'}), 404
        
        db.session.delete(cart_item)
        db.session.commit()
        return jsonify({'success': True, 'cart': cart.to_dict()})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@api_cart.route('/api/cart/clear', methods=['DELETE'])
def api_clear_cart():
    """Clear all items from cart"""
    try:
        cart = get_or_create_cart()
        CartItem.query.filter_by(cart_id=cart.id).delete()
        db.session.commit()
        return jsonify({'success': True, 'cart': cart.to_dict()})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@api_cart.route('/api/cart/settings', methods=['PUT'])
def api_update_cart_settings():
    """Update cart settings (table, order type, customer name)"""
    try:
        data = request.json
        cart = get_or_create_cart()
        
        if 'table_id' in data:
            cart.table_id = data['table_id'] if data['table_id'] else None
        if 'order_type' in data:
            cart.order_type = data['order_type']
        if 'customer_name' in data:
            cart.customer_name = data['customer_name']
        
        db.session.commit()
        return jsonify({'success': True, 'cart': cart.to_dict()})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
