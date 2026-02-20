from flask import Blueprint, jsonify
from models import MenuItem
from utils import get_user_branch_id, get_menu_with_branch_stock

api_menu = Blueprint('api_menu', __name__, url_prefix='')


@api_menu.route('/api/menu')
def api_get_menu():
    bid = get_user_branch_id()
    menu_items = MenuItem.query.filter_by(is_available=True).all() if bid is None else MenuItem.query.all()
    items = get_menu_with_branch_stock(menu_items, bid)
    # Filter to available only (per-branch availability)
    return jsonify([i for i in items if i['is_available']])

@api_menu.route('/api/menu/category/<int:category_id>')
def api_get_menu_by_category(category_id):
    bid = get_user_branch_id()
    base = MenuItem.query.filter_by(category_id=category_id)
    menu_items = base.filter_by(is_available=True).all() if bid is None else base.all()
    items = get_menu_with_branch_stock(menu_items, bid)
    return jsonify([i for i in items if i['is_available']])
