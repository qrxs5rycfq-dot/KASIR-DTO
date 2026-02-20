from datetime import datetime, timedelta
import os
import json
import hmac
import random
import hashlib

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from functools import wraps

from models import db, Branch, ExternalOrder, Order, OrderItem, Payment, MenuItem, PendingPrint, Notification, WebhookLog
from socket_handlers import emit_print_job

webhooks_bp = Blueprint('webhooks', __name__, url_prefix='')

# Will be set via init_webhooks_extensions()
_limiter = None
_csrf = None


def init_webhooks_extensions(csrf_instance, limiter_instance):
    """Initialize extensions for the webhooks blueprint. Call after app registration."""
    global _limiter, _csrf
    _csrf = csrf_instance
    _limiter = limiter_instance
    # Exempt CSRF for webhook view functions
    for endpoint, view_func in webhooks_bp.view_functions.items():
        if getattr(view_func, '_csrf_exempt', False):
            csrf_instance.exempt(view_func)


# ============================================
# FOOD DELIVERY PLATFORM INTEGRATION (Production)
# ============================================
# Supports real integration with:
# - GrabFood (partner-api.grab.com) - OAuth2 client_credentials, HMAC-SHA256
# - GoFood/GoBiz (api.gobiz.co.id) - OAuth2 client_credentials, event-based webhooks
# - ShopeeFood (partner.shopeemobile.com) - partner_key HMAC-SHA256 signing
#
# Two integration modes per platform:
# 1. WEBHOOK MODE: Platform sends orders to our endpoint (real-time)
# 2. POLLING MODE: We periodically fetch new orders from platform API (fallback)
# ============================================

# --- Platform API Configuration ---
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

# In-memory token cache (per platform)
_oauth_tokens = {}


def utc_now():
    """Return current UTC time (timezone-aware)."""
    from datetime import timezone
    return datetime.now(timezone.utc)


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                from flask import redirect, url_for
                return redirect(url_for('auth.login'))
            if not any(current_user.has_role(role) for role in roles):
                from flask import redirect, url_for, flash
                flash('Anda tidak memiliki akses ke halaman ini.', 'danger')
                return redirect(url_for('views.dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def create_notification(type, title, message, user_id=None, data=None):
    """Helper function to create a notification"""
    notification = Notification(
        type=type,
        title=title,
        message=message,
        user_id=user_id,
        data=json.dumps(data) if data else None,
        branch_id=_get_default_branch_id()
    )
    db.session.add(notification)
    db.session.commit()
    return notification


def _get_default_branch_id():
    """Get default branch id (Pusat) for webhook context where there is no logged-in user."""
    pusat = Branch.query.filter_by(code='PUSAT').first()
    return pusat.id if pusat else None


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


# ---- OAuth2 Token Management ----

def get_oauth_token(platform):
    """
    Get a valid OAuth2 access token for a platform.
    Uses client_credentials grant (GrabFood, GoFood).
    Caches tokens until expiry.
    """
    import requests as http_requests

    # Check cache
    cached = _oauth_tokens.get(platform)
    if cached and cached['expires_at'] > datetime.now():
        return cached['access_token']

    cfg = get_platform_config(platform)
    token_url = cfg.get('token_url', '')
    client_id = cfg.get('client_id', '')
    client_secret = cfg.get('client_secret', '')

    if not token_url or not client_id or not client_secret:
        return None

    try:
        if platform == 'grabfood':
            # GrabFood: POST with form data
            resp = http_requests.post(token_url, data={
                'client_id': client_id,
                'client_secret': client_secret,
                'grant_type': 'client_credentials',
                'scope': cfg.get('scope', 'food.partner_api'),
            }, headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15)

        elif platform == 'gofood':
            # GoFood/GoBiz: POST with Basic auth
            resp = http_requests.post(token_url,
                auth=(client_id, client_secret),
                data={
                    'grant_type': 'client_credentials',
                    'scope': cfg.get('scope', 'gofood:order:read gofood:order:write'),
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=15)
        else:
            return None

        if resp.status_code == 200:
            data = resp.json()
            expires_in = data.get('expires_in', 3600)
            _oauth_tokens[platform] = {
                'access_token': data['access_token'],
                'expires_at': datetime.now() + timedelta(seconds=expires_in - 60),
            }
            return data['access_token']
        else:
            current_app.logger.error(f'OAuth token error for {platform}: {resp.status_code} {resp.text}')
            return None
    except Exception as e:
        current_app.logger.error(f'OAuth token request failed for {platform}: {e}')
        return None


# ---- Platform-Specific Signature Verification ----

def verify_webhook_signature(platform, request_obj):
    """
    Verify webhook signature using the platform's actual method.

    GrabFood: HMAC-SHA256(body, secret) → X-Grab-Signature header
    GoFood:   Direct token comparison → X-Callback-Token header
    ShopeeFood: HMAC-SHA256(base_string, partner_key) → Authorization header

    Returns: (is_valid, sig_status, detail)
    """
    cfg = get_platform_config(platform)
    pcfg = PLATFORM_CONFIG.get(platform, {})

    if platform == 'grabfood':
        secret = cfg.get('webhook_secret', '')
        if not secret:
            return True, 'skipped', 'No GRABFOOD_WEBHOOK_SECRET configured'

        sig_header = request_obj.headers.get('X-Grab-Signature', '')
        if not sig_header:
            # Fallback for direct secret
            fallback = request_obj.headers.get('X-Webhook-Secret', '')
            if fallback and hmac.compare_digest(fallback, secret):
                return True, 'valid', 'Direct secret match (fallback)'
            return False, 'missing', 'Missing X-Grab-Signature header'

        body = request_obj.get_data()
        computed = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig_header, computed):
            return True, 'valid', 'HMAC-SHA256 signature valid'
        return False, 'invalid', 'X-Grab-Signature HMAC-SHA256 mismatch'

    elif platform == 'gofood':
        # GoFood uses X-Callback-Token — a static token comparison, NOT HMAC of body
        secret = cfg.get('webhook_secret', '')
        if not secret:
            return True, 'skipped', 'No GOFOOD_WEBHOOK_SECRET configured'

        callback_token = request_obj.headers.get('X-Callback-Token', '')
        if not callback_token:
            fallback = request_obj.headers.get('X-Webhook-Secret', '')
            if fallback and hmac.compare_digest(fallback, secret):
                return True, 'valid', 'Direct secret match (fallback)'
            return False, 'missing', 'Missing X-Callback-Token header'

        if hmac.compare_digest(callback_token, secret):
            return True, 'valid', 'X-Callback-Token verified'
        return False, 'invalid', 'X-Callback-Token mismatch'

    elif platform == 'shopeefood':
        # ShopeeFood: HMAC-SHA256(partner_key, base_string)
        # base_string = partner_id + api_path + timestamp + access_token + shop_id
        partner_key = cfg.get('partner_key', '') or cfg.get('webhook_secret', '')
        if not partner_key:
            return True, 'skipped', 'No SHOPEEFOOD_PARTNER_KEY configured'

        sig_header = request_obj.headers.get('Authorization', '')
        if not sig_header:
            sig_header = request_obj.headers.get('X-Shopee-Hmac-SHA256', '')
        if not sig_header:
            fallback = request_obj.headers.get('X-Webhook-Secret', '')
            if fallback and hmac.compare_digest(fallback, partner_key):
                return True, 'valid', 'Direct secret match (fallback)'
            return False, 'missing', 'Missing Authorization/X-Shopee-Hmac-SHA256 header'

        # Try HMAC verification of body
        body = request_obj.get_data()
        computed = hmac.new(partner_key.encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig_header, computed):
            return True, 'valid', 'HMAC-SHA256 signature valid'

        # Also try direct comparison (some setups use static token)
        if hmac.compare_digest(sig_header, partner_key):
            return True, 'valid', 'Direct key match'

        return False, 'invalid', 'Shopee signature mismatch'

    return False, 'invalid', f'Unknown platform: {platform}'


def log_webhook_attempt(platform, request_obj, result, error_detail=None, ext_order_id=None):
    """Log webhook attempt for auditing and debugging signature failures"""
    try:
        body = request_obj.get_data()
        body_hash = hashlib.sha256(body).hexdigest() if body else None

        relevant_headers = {}
        for h in ['Content-Type', 'X-Grab-Signature', 'X-Callback-Token',
                   'X-Shopee-Hmac-SHA256', 'X-Webhook-Secret', 'X-Forwarded-For',
                   'User-Agent', 'X-Branch-Code', 'X-Store-ID', 'Authorization']:
            val = request_obj.headers.get(h)
            if val:
                if any(s in h.lower() for s in ('signature', 'secret', 'token', 'hmac', 'authorization')):
                    relevant_headers[h] = val[:4] + '****' if len(val) > 4 else '****'
                else:
                    relevant_headers[h] = val

        log_entry = WebhookLog(
            platform=platform,
            request_ip=request_obj.headers.get('X-Forwarded-For', request_obj.remote_addr),
            request_headers=json.dumps(relevant_headers),
            request_body_hash=body_hash,
            signature_provided=(request_obj.headers.get('X-Grab-Signature', '') or
                               request_obj.headers.get('X-Callback-Token', '') or
                               request_obj.headers.get('X-Shopee-Hmac-SHA256', '') or
                               request_obj.headers.get('Authorization', ''))[:8] + '****',
            result=result,
            error_detail=error_detail,
            external_order_id=ext_order_id
        )
        db.session.add(log_entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


# ---- Platform-Specific Payload Extraction ----

def extract_grabfood_order(data):
    """
    Extract order from real GrabFood webhook payload.

    GrabFood SubmitOrder payload structure (from official SDK):
    {
      "orderID": "...",
      "shortOrderNumber": "123",
      "merchantID": "...",
      "partnerMerchantID": "...",
      "paymentType": "CASHLESS",
      "orderTime": "2025-...",
      "currency": {"code": "IDR", "symbol": "Rp", "exponent": 0},
      "items": [{"id": "...", "grabItemID": "...", "quantity": 1, "price": 35000,
                 "tax": 0, "specifications": "extra spicy",
                 "modifiers": [{"id": "...", "name": "...", "quantity": 1, "price": 5000}]}],
      "price": {"subtotal": 35000, "tax": 0, "merchantChargeFee": 0,
                "grabFundPromo": 0, "merchantFundPromo": 0, "eaterPayment": 35000},
      "receiver": {"name": "John", "phones": ["+62812..."], "address": {...}},
      "featureFlags": {"orderAcceptedType": "AUTO" | "MANUAL"}
    }
    """
    result = {'external_id': '', 'customer_name': '', 'items': [], 'notes': '',
              'store_id': '', 'total': 0}

    # Order ID: real field is "orderID", fallback to "order_id"
    result['external_id'] = (data.get('orderID') or data.get('order_id') or
                              data.get('shortOrderNumber', ''))

    # Store/Merchant ID for branch routing
    result['store_id'] = data.get('merchantID') or data.get('merchant_id') or data.get('partnerMerchantID', '')

    # Customer from receiver object (real GrabFood structure)
    receiver = data.get('receiver', {})
    if isinstance(receiver, dict):
        result['customer_name'] = receiver.get('name', '')
    # Fallback to eater (older format)
    if not result['customer_name']:
        eater = data.get('eater', {})
        if isinstance(eater, dict):
            result['customer_name'] = eater.get('name', '')
    if not result['customer_name']:
        result['customer_name'] = data.get('customer_name', '')

    # Items from real structure
    raw_items = data.get('items', []) or data.get('order_items', [])
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        # In real GrabFood, "id" is the partner's externalID, not the item name
        # The item name is in the linked menu, so we use 'id' to match
        item_id = item.get('id', '')
        grab_item_id = item.get('grabItemID') or item.get('grab_item_id', '')
        item_name = item.get('name', '') or item_id  # Some payloads include name

        # Price in real API is in minor unit (cents), but for IDR exponent=0 so it's normal
        currency = data.get('currency', {})
        exponent = currency.get('exponent', 0) if isinstance(currency, dict) else 0

        raw_price = int(item.get('price', 0) or 0)
        item_price = raw_price if exponent == 0 else int(raw_price / (10 ** exponent))

        item_qty = int(item.get('quantity', 1) or 1)
        item_notes = item.get('specifications', '') or item.get('notes', '')

        # Process modifiers/add-ons
        modifiers = item.get('modifiers', []) or []
        modifier_notes = []
        for mod in modifiers:
            if isinstance(mod, dict):
                mod_name = mod.get('name', '')
                if mod_name:
                    modifier_notes.append(mod_name)
        if modifier_notes:
            item_notes = (item_notes + ' | ' + ', '.join(modifier_notes)).strip(' | ')

        result['items'].append({
            'external_id': item_id,
            'grab_item_id': grab_item_id,
            'name': item_name,
            'quantity': item_qty,
            'price': item_price,
            'notes': item_notes,
        })

    # Total from price object
    price_obj = data.get('price', {})
    if isinstance(price_obj, dict):
        result['total'] = int(price_obj.get('subtotal', 0) or 0)

    # Notes
    result['notes'] = data.get('specialInstructions', '') or data.get('notes', '') or data.get('special_instructions', '')

    return result


def extract_gofood_order(data):
    """
    Extract order from real GoFood/GoBiz webhook payload.

    GoFood notification payload structure (from official docs):
    {
      "header": {
        "version": 1,
        "timestamp": "2025-...",
        "event_name": "gofood.order.awaiting_merchant_acceptance",
        "event_id": "uuid"
      },
      "body": {
        "service_type": "gofood",
        "outlet": {"id": "M008272", "external_outlet_id": "1", "name": "..."},
        "customer": {"id": "...", "name": "Jane Doe", "phone_number": "..."},
        "order": {
          "order_id": "OF12345",
          "items": [{"item_id": "I98765", "name": "Nasi Goreng", "quantity": 2,
                     "price": 25000, "special_instructions": "No chili"}],
          "total_price": 58000,
          "note": "Ring the bell"
        }
      }
    }
    """
    result = {'external_id': '', 'customer_name': '', 'items': [], 'notes': '',
              'store_id': '', 'total': 0, 'event_name': ''}

    # GoFood has header + body structure
    header = data.get('header', {})
    body = data.get('body', data)  # Fallback to top-level if no wrapper

    result['event_name'] = header.get('event_name', '') if isinstance(header, dict) else ''

    # Order data is nested in body.order
    order_data = body.get('order', body) if isinstance(body, dict) else data

    # Order ID
    if isinstance(order_data, dict):
        result['external_id'] = (order_data.get('order_id') or order_data.get('id') or
                                  data.get('order_id') or data.get('id', ''))
    else:
        result['external_id'] = data.get('order_id') or data.get('id', '')

    # Store/Outlet ID for branch routing
    outlet = body.get('outlet', {}) if isinstance(body, dict) else {}
    if isinstance(outlet, dict):
        result['store_id'] = outlet.get('id') or outlet.get('external_outlet_id', '')

    # Customer
    customer = body.get('customer', {}) if isinstance(body, dict) else {}
    if isinstance(customer, dict):
        result['customer_name'] = customer.get('name', '')
    if not result['customer_name']:
        result['customer_name'] = data.get('customer_name', '')

    # Items
    raw_items = []
    if isinstance(order_data, dict):
        raw_items = order_data.get('items', []) or []
    if not raw_items:
        raw_items = data.get('items', []) or data.get('order_items', [])

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        result['items'].append({
            'external_id': item.get('item_id') or item.get('id', ''),
            'name': item.get('name') or item.get('food_name', 'Item'),
            'quantity': int(item.get('quantity') or item.get('qty', 1)),
            'price': int(item.get('price') or item.get('unit_price', 0)),
            'notes': item.get('special_instructions') or item.get('notes') or item.get('variant_name', ''),
        })

    # Total
    if isinstance(order_data, dict):
        result['total'] = int(order_data.get('total_price', 0) or 0)
    if not result['total']:
        result['total'] = int(data.get('total_price', 0) or 0)

    # Notes
    if isinstance(order_data, dict):
        result['notes'] = order_data.get('note', '') or order_data.get('notes', '')
    if not result['notes']:
        result['notes'] = data.get('notes', '') or data.get('customer_note', '')

    return result


def extract_shopeefood_order(data):
    """
    Extract order from real ShopeeFood/Shopee Open Platform webhook payload.

    Shopee push notification structure:
    {
      "shop_id": 123456,
      "code": 0,  (0 = success)
      "data": {
        "ordersn": "220421ABCDEF",
        "order_status": "READY_TO_SHIP",
        "buyer_username": "buyer123",
        "item_list": [
          {"item_id": 123, "item_name": "Nasi Goreng", "model_quantity_purchased": 2,
           "model_original_price": 35000}
        ],
        "total_amount": 70000,
        "message_to_seller": "Extra pedas"
      }
    }

    Also supports simpler format for ShopeeFood-specific integration:
    {
      "order_code": "SPF12345",
      "buyer": {"name": "..."},
      "items": [{"name": "...", "quantity": 1, "price": 35000}]
    }
    """
    result = {'external_id': '', 'customer_name': '', 'items': [], 'notes': '',
              'store_id': '', 'total': 0}

    # Check for Shopee Open Platform nested "data" structure
    shopee_data = data.get('data', data)  # Use "data" sub-object if present

    # Store ID
    result['store_id'] = str(data.get('shop_id', '') or data.get('store_id', ''))

    # Order ID
    if isinstance(shopee_data, dict):
        result['external_id'] = (shopee_data.get('ordersn') or shopee_data.get('order_sn') or
                                  shopee_data.get('order_code') or data.get('order_code') or
                                  data.get('order_id', ''))
    else:
        result['external_id'] = data.get('order_code') or data.get('order_id', '')

    # Customer
    buyer = data.get('buyer', {})
    if isinstance(buyer, dict):
        result['customer_name'] = buyer.get('name') or buyer.get('username', '')
    if not result['customer_name'] and isinstance(shopee_data, dict):
        result['customer_name'] = shopee_data.get('buyer_username', '')
    if not result['customer_name']:
        result['customer_name'] = data.get('customer_name', '')

    # Items - Shopee Open Platform uses "item_list", ShopeeFood uses "items"
    raw_items = []
    if isinstance(shopee_data, dict):
        raw_items = shopee_data.get('item_list', []) or []
    if not raw_items:
        raw_items = data.get('items', []) or data.get('order_items', [])

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        result['items'].append({
            'external_id': str(item.get('item_id') or item.get('id', '')),
            'name': item.get('item_name') or item.get('name', 'Item'),
            'quantity': int(item.get('model_quantity_purchased') or item.get('quantity') or item.get('amount', 1)),
            'price': int(item.get('model_original_price') or item.get('price') or item.get('original_price', 0)),
            'notes': item.get('special_instructions', '') or item.get('notes', ''),
        })

    # Total
    if isinstance(shopee_data, dict):
        result['total'] = int(shopee_data.get('total_amount', 0) or 0)
    if not result['total']:
        result['total'] = int(data.get('total_amount', 0) or 0)

    # Notes
    if isinstance(shopee_data, dict):
        result['notes'] = shopee_data.get('message_to_seller', '') or shopee_data.get('note', '')
    if not result['notes']:
        result['notes'] = data.get('message_to_seller', '') or data.get('note', '') or data.get('notes', '')

    return result


# ---- Branch Resolution ----

def resolve_branch_from_platform(platform, parsed_data, request_obj):
    """
    Resolve branch using platform's store_id from payload (NOT X-Branch-Code).

    Priority:
    1. Match store_id from payload to configured PLATFORM_STORE_BRANCH_MAP
    2. Match X-Store-ID header
    3. Fallback X-Branch-Code header (backward compat)
    4. Default to Pusat
    """
    branch_id = None
    store_id = parsed_data.get('store_id', '')
    store_branch_map_str = current_app.config.get(f'{platform.upper()}_STORE_BRANCH_MAP', '')

    # 1) Match store_id from payload
    if store_id and store_branch_map_str:
        for mapping in store_branch_map_str.split(','):
            parts = mapping.strip().split(':')
            if len(parts) == 2 and parts[0].strip() == str(store_id):
                branch = Branch.query.filter_by(code=parts[1].strip(), is_active=True).first()
                if branch:
                    branch_id = branch.id
                    break

    # 2) X-Store-ID header
    if not branch_id:
        header_store_id = request_obj.headers.get('X-Store-ID', '')
        if header_store_id and store_branch_map_str:
            for mapping in store_branch_map_str.split(','):
                parts = mapping.strip().split(':')
                if len(parts) == 2 and parts[0].strip() == header_store_id:
                    branch = Branch.query.filter_by(code=parts[1].strip(), is_active=True).first()
                    if branch:
                        branch_id = branch.id
                        break

    # 3) X-Branch-Code fallback
    if not branch_id:
        branch_code = request_obj.headers.get('X-Branch-Code', '')
        if branch_code:
            branch = Branch.query.filter_by(code=branch_code, is_active=True).first()
            if branch:
                branch_id = branch.id

    # 4) Default Pusat
    if not branch_id:
        pusat = Branch.query.filter_by(code='PUSAT').first()
        if pusat:
            branch_id = pusat.id

    return branch_id


# ---- Core Order Processing ----

def process_platform_order(platform, data, branch_id=None):
    """
    Process an incoming order from a food delivery platform.

    Uses platform-specific extractors, creates order with proper status flow:
    received → accepted → printed

    Printing is DEFERRED to the print worker (not inline in webhook).
    """
    # Extract with platform-specific parser
    extractors = {
        'grabfood': extract_grabfood_order,
        'gofood': extract_gofood_order,
        'shopeefood': extract_shopeefood_order,
    }
    extractor = extractors.get(platform)
    if not extractor:
        return None, 'Platform tidak dikenal'

    parsed = extractor(data)
    external_id = parsed['external_id']
    customer_name = parsed['customer_name']
    items_data = parsed['items']
    notes = parsed['notes']

    if not external_id:
        return None, 'Order ID tidak ditemukan dalam payload'

    # Idempotency check
    existing = ExternalOrder.query.filter_by(
        platform=platform, external_order_id=str(external_id)
    ).first()
    if existing:
        return existing, 'Order sudah diproses sebelumnya'

    # Create ExternalOrder record
    ext_order = ExternalOrder(
        platform=platform,
        external_order_id=str(external_id),
        raw_data=json.dumps(data),
        status='received',
        branch_id=branch_id
    )
    db.session.add(ext_order)
    db.session.flush()

    try:
        # Generate order number
        platform_prefix = {'grabfood': 'GRB', 'gofood': 'GOF', 'shopeefood': 'SPF'}
        random_suffix = random.randint(100, 999)
        order_number = f"{platform_prefix.get(platform, 'EXT')}{datetime.now().strftime('%Y%m%d%H%M%S')}{random_suffix}"

        # Fallback customer name
        if not customer_name:
            platform_labels = {'grabfood': 'GrabFood', 'gofood': 'GoFood', 'shopeefood': 'ShopeeFood'}
            suffix = external_id[-6:] if len(external_id) >= 6 else external_id
            customer_name = f"{platform_labels.get(platform, platform)} #{suffix}"

        platform_labels = {'grabfood': 'GrabFood', 'gofood': 'GoFood', 'shopeefood': 'ShopeeFood'}
        order = Order(
            order_number=order_number,
            customer_name=customer_name,
            order_type='online',
            source=platform,
            status='processing',
            notes=f"[{platform_labels.get(platform, platform)}] {notes}".strip(),
            branch_id=branch_id
        )
        db.session.add(order)
        db.session.flush()

        # Process items with price snapshot and menu matching
        total = 0
        for item in items_data:
            item_name = item.get('name', 'Item')
            item_qty = item.get('quantity', 1)
            item_price = item.get('price', 0)
            item_notes = item.get('notes', '')
            item_ext_id = item.get('external_id', '')

            # Match menu item: try external_id first (partner's ID), then exact name, then partial
            menu_item = None
            if item_ext_id:
                menu_item = MenuItem.query.filter_by(id=item_ext_id).first() if isinstance(item_ext_id, str) and item_ext_id.isdigit() else None
            if not menu_item:
                menu_item = MenuItem.query.filter(
                    db.func.lower(MenuItem.name) == item_name.lower()
                ).first()
            if not menu_item:
                menu_item = MenuItem.query.filter(
                    MenuItem.name.ilike(f'%{item_name}%')
                ).first()

            menu_price_snapshot = menu_item.price if menu_item else None
            if menu_item and item_price == 0:
                item_price = menu_item.price

            subtotal = item_price * item_qty
            total += subtotal

            order_item = OrderItem(
                order_id=order.id,
                menu_item_id=menu_item.id if menu_item else None,
                name=item_name,
                price=item_price,
                menu_price_snapshot=menu_price_snapshot,
                quantity=item_qty,
                subtotal=subtotal,
                notes=item_notes,
                item_status='pending'
            )
            db.session.add(order_item)

        # Use platform-provided total if available and our calculated total is 0
        if total == 0 and parsed.get('total', 0) > 0:
            total = parsed['total']

        order.subtotal = total
        order.total = total

        # Payment (pre-paid via platform)
        payment = Payment(
            order_id=order.id,
            payment_method=platform,
            amount=total,
            paid_amount=total,
            status='paid',
            paid_at=utc_now()
        )
        db.session.add(payment)

        # Update external order status → accepted
        ext_order.order_id = order.id
        ext_order.status = 'accepted'
        ext_order.processed_at = utc_now()
        ext_order.accepted_at = utc_now()

        # Notification
        total_formatted = f"{total:,}".replace(',', '.')
        create_notification(
            type='order_new',
            title=f'Pesanan {platform_labels.get(platform, platform)}!',
            message=f'Order #{order_number} - {customer_name} - Rp {total_formatted}',
            data={'order_id': order.id, 'order_number': order_number, 'platform': platform}
        )

        # Schedule print (DEFERRED — NOT printed inline in webhook)
        receipt_data = [
            {"type": "text", "value": "================================", "align": "center"},
            {"type": "text", "value": platform_labels.get(platform, platform).upper(), "align": "center", "bold": True, "size": "large"},
            {"type": "text", "value": "================================", "align": "center"},
            {"type": "text", "value": f"Order: #{order_number}", "bold": True},
            {"type": "text", "value": f"Pelanggan: {customer_name}"},
            {"type": "text", "value": f"Waktu: {datetime.now().strftime('%d/%m/%Y %H:%M')}"},
            {"type": "text", "value": "--------------------------------", "align": "center"},
        ]

        for item in items_data:
            receipt_data.append({"type": "text", "value": f"{item.get('quantity', 1)}x {item.get('name', 'Item')}"})
            if item.get('price', 0) > 0:
                price_fmt = f"Rp {item['price'] * item.get('quantity', 1):,}".replace(',', '.')
                receipt_data.append({"type": "text", "value": f"   {price_fmt}", "align": "right"})
            if item.get('notes'):
                receipt_data.append({"type": "text", "value": f"   Catatan: {item['notes']}"})

        receipt_data.extend([
            {"type": "text", "value": "--------------------------------", "align": "center"},
            {"type": "text", "value": f"TOTAL: Rp {total:,}".replace(',', '.'), "bold": True, "align": "right"},
            {"type": "text", "value": "================================", "align": "center"},
        ])
        if notes:
            receipt_data.append({"type": "text", "value": f"Catatan: {notes}"})
        receipt_data.append({"type": "text", "value": ""})
        receipt_data.append({"type": "cut"})

        pending_print = PendingPrint(
            order_id=order.id,
            receipt_data=json.dumps(receipt_data),
            copies=3,  # ← Diubah dari 2 menjadi 3
            status='pending',
            branch_id=branch_id
        )
        db.session.add(pending_print)

        db.session.commit()

        # Emit via WebSocket to connected printer clients
        emit_print_job(pending_print, branch_id)

        return ext_order, None

    except Exception as e:
        ext_order.status = 'failed'
        ext_order.error_message = str(e)
        db.session.commit()
        return ext_order, str(e)


# ---- Unified Webhook Handler ----

def handle_webhook(platform):
    """
    Unified webhook handler. Fast response, deferred printing.
    1. Verify platform-specific signature
    2. Log the attempt
    3. Create order (print scheduled, not inline)
    4. Return 200 quickly (platforms retry on timeout)
    """
    is_valid, sig_status, sig_detail = verify_webhook_signature(platform, request)

    if not is_valid:
        log_webhook_attempt(platform, request, 'sig_' + sig_status, sig_detail)
        return jsonify({'error': 'Unauthorized', 'detail': sig_detail}), 401

    data = request.get_json(silent=True)
    if not data:
        log_webhook_attempt(platform, request, 'error', 'Invalid JSON payload')
        return jsonify({'error': 'Invalid JSON payload'}), 400

    # Extract using platform-specific parser for branch resolution
    extractors = {
        'grabfood': extract_grabfood_order,
        'gofood': extract_gofood_order,
        'shopeefood': extract_shopeefood_order,
    }
    parsed = extractors[platform](data)
    branch_id = resolve_branch_from_platform(platform, parsed, request)

    ext_order, error = process_platform_order(platform, data, branch_id)

    if ext_order:
        ext_order.signature_status = sig_status
        db.session.commit()

    if error and not ext_order:
        log_webhook_attempt(platform, request, 'error', error)
        return jsonify({'success': False, 'error': error}), 400

    if error:
        log_webhook_attempt(platform, request, 'success', error, ext_order.id)
        return jsonify({'success': True, 'message': error, 'external_order_id': ext_order.id}), 200

    log_webhook_attempt(platform, request, 'success', None, ext_order.id)
    return jsonify({
        'success': True,
        'message': 'Order received',
        'order_id': ext_order.order_id,
        'external_order_id': ext_order.id
    }), 200


# ---- Webhook Endpoints ----

def _exempt_csrf(view_func):
    """Mark a view function as CSRF-exempt. Applied when blueprint is registered."""
    view_func._csrf_exempt = True
    return view_func


@webhooks_bp.route('/api/webhook/grabfood', methods=['POST'])
@_exempt_csrf
def webhook_grabfood():
    """GrabFood webhook — receives order via X-Grab-Signature (HMAC-SHA256)"""
    return handle_webhook('grabfood')


@webhooks_bp.route('/api/webhook/gofood', methods=['POST'])
@_exempt_csrf
def webhook_gofood():
    """GoFood webhook — receives order via X-Callback-Token"""
    return handle_webhook('gofood')


@webhooks_bp.route('/api/webhook/shopeefood', methods=['POST'])
@_exempt_csrf
def webhook_shopeefood():
    """ShopeeFood webhook — receives order via HMAC-SHA256 signature"""
    return handle_webhook('shopeefood')


# ---- Order Status Management ----

@webhooks_bp.route('/api/webhook/order/<int:ext_id>/mark-printed', methods=['POST'])
@_exempt_csrf
def webhook_mark_printed(ext_id):
    """Mark external order as printed (called by print worker after successful print)"""
    ext_order = ExternalOrder.query.get_or_404(ext_id)
    if ext_order.status == 'accepted':
        ext_order.status = 'printed'
        ext_order.printed_at = utc_now()
        db.session.commit()
    return jsonify({'success': True, 'status': ext_order.status}), 200


# ---- Platform API: Accept Order ----

@webhooks_bp.route('/api/platform/<platform>/accept/<int:ext_id>', methods=['POST'])
@login_required
@role_required('admin', 'kasir')
def platform_accept_order(platform, ext_id):
    """
    Accept an order on the platform side (for platforms that require manual acceptance).
    GrabFood: PUT /partner/v1/order/prepare
    GoFood: PUT /integrations/gofood/outlets/{outlet}/v1/orders/delivery/{order_id}/accepted
    """
    import requests as http_requests

    ext_order = ExternalOrder.query.get_or_404(ext_id)
    cfg = get_platform_config(platform)

    if platform == 'grabfood':
        token = get_oauth_token('grabfood')
        if not token:
            return jsonify({'success': False, 'error': 'Gagal mendapatkan token OAuth2 GrabFood. Cek CLIENT_ID/SECRET.'}), 400

        try:
            api_base = cfg['api_base']
            order_id = ext_order.external_order_id
            resp = http_requests.put(
                f'{api_base}/partner/v1/order/prepare',
                json={'orderID': order_id},
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                },
                timeout=15
            )
            if resp.status_code in (200, 204):
                return jsonify({'success': True, 'message': 'Order diterima di GrabFood'})
            return jsonify({'success': False, 'error': f'GrabFood API error: {resp.status_code} {resp.text}'}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    elif platform == 'gofood':
        token = get_oauth_token('gofood')
        if not token:
            return jsonify({'success': False, 'error': 'Gagal mendapatkan token OAuth2 GoFood. Cek CLIENT_ID/SECRET.'}), 400

        try:
            api_base = cfg['api_base']
            outlet_id = cfg.get('merchant_id', '')
            order_id = ext_order.external_order_id
            resp = http_requests.put(
                f'{api_base}/integrations/gofood/outlets/{outlet_id}/v1/orders/delivery/{order_id}/accepted',
                headers={
                    'Authorization': f'Bearer {token}',
                    'Content-Type': 'application/json',
                },
                timeout=15
            )
            if resp.status_code in (200, 204):
                return jsonify({'success': True, 'message': 'Order diterima di GoFood'})
            return jsonify({'success': False, 'error': f'GoFood API error: {resp.status_code} {resp.text}'}), 400
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({'success': False, 'error': 'Platform tidak mendukung accept API'}), 400


# ---- Webhook Replay Tool (Admin) ----

@webhooks_bp.route('/admin/webhook/<int:ext_id>/replay', methods=['POST'])
@login_required
@role_required('admin')
def webhook_replay(ext_id):
    """Replay a failed/received webhook order from stored raw_data"""
    ext_order = ExternalOrder.query.get_or_404(ext_id)

    if ext_order.status not in ('failed', 'received'):
        return jsonify({'success': False, 'error': 'Hanya order gagal/received yang bisa di-replay'}), 400

    try:
        data = json.loads(ext_order.raw_data)
    except (json.JSONDecodeError, TypeError):
        return jsonify({'success': False, 'error': 'Raw data tidak valid'}), 400

    old_platform = ext_order.platform
    branch_id = ext_order.branch_id
    retry_count = (ext_order.retry_count or 0) + 1

    # Remove old record so duplicate check allows re-processing
    db.session.delete(ext_order)
    db.session.commit()

    new_ext, error = process_platform_order(old_platform, data, branch_id)

    if new_ext:
        new_ext.retry_count = retry_count
        new_ext.last_retry_at = utc_now()
        db.session.commit()

    if error and not new_ext:
        return jsonify({'success': False, 'error': error}), 400

    return jsonify({
        'success': True,
        'message': 'Order berhasil di-replay',
        'order_id': new_ext.order_id if new_ext else None
    }), 200
