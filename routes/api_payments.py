import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO

import qrcode
import requests

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from models import db, Order, Payment
from extensions import limiter, csrf
from utils import utc_now, create_notification, role_required, get_gateway_config

api_payments = Blueprint('api_payments', __name__, url_prefix='')


def generate_midtrans_snap_token(order, midtrans_order_id):
    """Generate Midtrans Snap token for payment"""
    server_key = current_app.config.get('MIDTRANS_SERVER_KEY', '')
    is_production = current_app.config.get('MIDTRANS_IS_PRODUCTION', False)
    
    # Determine API URL
    if is_production:
        snap_url = 'https://app.midtrans.com/snap/v1/transactions'
    else:
        snap_url = 'https://app.sandbox.midtrans.com/snap/v1/transactions'
    
    # Prepare transaction details
    transaction_details = {
        'order_id': midtrans_order_id,
        'gross_amount': int(order.total)
    }
    
    # Prepare item details
    item_details = []
    for item in order.items:
        item_details.append({
            'id': str(item.menu_item_id),
            'price': int(item.price),
            'quantity': item.quantity,
            'name': item.name[:50]  # Midtrans limits name to 50 chars
        })
    
    # Note: Discount is already factored into the order total,
    # no need to add separate line items for it in Midtrans
    
    # Customer details
    customer_details = {
        'first_name': order.customer_name or 'Customer',
        'email': 'customer@kasir.local'
    }
    
    if current_user.is_authenticated:
        customer_details['first_name'] = current_user.full_name or current_user.username
        customer_details['email'] = current_user.email or 'customer@kasir.local'
    
    # Build request payload
    payload = {
        'transaction_details': transaction_details,
        'item_details': item_details,
        'customer_details': customer_details
    }
    
    # Create authorization header
    auth_string = base64.b64encode(f"{server_key}:".encode()).decode()
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Basic {auth_string}'
    }
    
    try:
        response = requests.post(snap_url, json=payload, headers=headers, timeout=30)
        if response.status_code == 201:
            data = response.json()
            return data.get('token')
        else:
            print(f"Midtrans error: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"Midtrans connection error: {e}")
        return None


@api_payments.route('/api/payment/midtrans', methods=['POST'])
def api_create_midtrans_payment():
    try:
        data = request.json
        order_id = data.get('order_id')
        
        order = Order.query.get_or_404(order_id)
        
        # Create Midtrans transaction (simplified - in production use midtransclient)
        midtrans_order_id = f"DTO-{order.order_number}"
        
        # Update payment
        if order.payment:
            order.payment.payment_method = 'midtrans'
            order.payment.midtrans_order_id = midtrans_order_id
            order.payment.status = 'pending'
            # In production, generate actual Snap token here
            order.payment.payment_url = f"https://app.sandbox.midtrans.com/snap/v2/vtweb/{midtrans_order_id}"
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'payment_url': order.payment.payment_url,
            'order_id': midtrans_order_id
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@csrf.exempt
@api_payments.route('/api/payment/midtrans/callback', methods=['POST'])
@limiter.limit("30 per minute")  # Rate limit webhook calls
def api_midtrans_callback():
    """Handle Midtrans payment notification webhook - CSRF exempt for external service"""
    try:
        data = request.json
        order_id = data.get('order_id')
        transaction_status = data.get('transaction_status')
        
        # Verify signature for security (Midtrans sends signature_key)
        signature_key = data.get('signature_key')
        server_key = current_app.config.get('MIDTRANS_SERVER_KEY', '')
        
        if signature_key and server_key:
            # Midtrans signature: SHA512(order_id+status_code+gross_amount+server_key)
            status_code = data.get('status_code', '')
            gross_amount = data.get('gross_amount', '')
            expected_signature = hashlib.sha512(
                f"{order_id}{status_code}{gross_amount}{server_key}".encode()
            ).hexdigest()
            
            if signature_key != expected_signature:
                return jsonify({'error': 'Invalid signature'}), 403
        
        # Find payment by midtrans_order_id
        payment = Payment.query.filter_by(midtrans_order_id=order_id).first()
        
        if payment:
            payment.midtrans_status = transaction_status
            
            if transaction_status in ['capture', 'settlement']:
                payment.status = 'paid'
                payment.paid_at = utc_now()
                payment.order.status = 'processing'
                
                # Create success notification
                amount_formatted = f"{payment.amount:,}".replace(',', '.')
                create_notification(
                    type='payment_success',
                    title='Pembayaran Berhasil!',
                    message=f'Order #{payment.order.order_number} - Rp {amount_formatted}',
                    data={'order_id': payment.order_id, 'payment_id': payment.id}
                )
                
            elif transaction_status in ['deny', 'cancel', 'expire']:
                payment.status = 'failed'
                
                # Create failure notification
                create_notification(
                    type='payment_failed',
                    title='Pembayaran Gagal',
                    message=f'Order #{payment.order.order_number} - Status: {transaction_status}',
                    data={'order_id': payment.order_id, 'payment_id': payment.id}
                )
            
            db.session.commit()
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_payments.route('/api/payment/<int:order_id>/status', methods=['POST'])
@login_required
def update_payment_status(order_id):
    order = db.session.get(Order, order_id)
    if not order or not order.payment:
        return jsonify({'error': 'Order not found'}), 404
    
    data = request.json
    status = data.get('status', 'pending')
    
    if status == 'paid':
        order.payment.status = 'paid'
        order.payment.paid_at = utc_now()
        order.status = 'completed'
    elif status == 'pending':
        order.payment.status = 'pending'
    elif status == 'failed':
        order.payment.status = 'failed'
    
    db.session.commit()
    return jsonify({'success': True, 'status': order.payment.status})


@api_payments.route('/api/payment-gateway/test', methods=['POST'])
@login_required
@role_required('admin')
def api_test_payment_gateway():
    """Test Midtrans API connection"""
    server_key = current_app.config.get('MIDTRANS_SERVER_KEY', '')
    is_production = current_app.config.get('MIDTRANS_IS_PRODUCTION', False)
    
    if not server_key:
        return jsonify({
            'success': False,
            'message': 'Server Key belum dikonfigurasi'
        })
    
    if is_production:
        api_url = 'https://api.midtrans.com/v2/ping'
    else:
        api_url = 'https://api.sandbox.midtrans.com/v2/ping'
    
    try:
        auth_string = base64.b64encode(f'{server_key}:'.encode()).decode()
        headers = {
            'Authorization': f'Basic {auth_string}',
            'Accept': 'application/json'
        }
        
        response = requests.get(api_url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            return jsonify({
                'success': True,
                'message': f'Koneksi berhasil! Mode: {"Production" if is_production else "Sandbox"}',
                'environment': 'production' if is_production else 'sandbox'
            })
        else:
            return jsonify({
                'success': False,
                'message': f'Koneksi gagal: HTTP {response.status_code}'
            })
    except requests.exceptions.Timeout:
        return jsonify({
            'success': False,
            'message': 'Koneksi timeout. Coba lagi nanti.'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error: {str(e)}'
        })


# ========== BRI QRIS API ==========

def bri_get_access_token():
    """Get BRI API access token using OAuth2 client credentials with RSA signature"""
    client_id = get_gateway_config('bri_client_id', 'BRI_CLIENT_ID')
    client_secret = get_gateway_config('bri_client_secret', 'BRI_CLIENT_SECRET')
    private_key_path = get_gateway_config('bri_private_key_path', 'BRI_PRIVATE_KEY_PATH')
    is_production = get_gateway_config('bri_is_production', 'BRI_IS_PRODUCTION').lower() == 'true'
    
    if not client_id or not client_secret:
        return None, 'BRI_CLIENT_ID atau BRI_CLIENT_SECRET belum dikonfigurasi'
    
    base_url = 'https://partner.api.bri.co.id' if is_production else 'https://sandbox.partner.api.bri.co.id'
    
    # BRI API requires WIB (UTC+7) timestamp
    wib = timezone(timedelta(hours=7))
    now_wib = datetime.now(wib)
    timestamp = now_wib.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now_wib.microsecond // 1000:03d}+07:00'
    
    # Generate signature: SHA256withRSA(client_id + "|" + timestamp)
    signature = ''
    if private_key_path and os.path.exists(private_key_path):
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            
            with open(private_key_path, 'rb') as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None)
            
            string_to_sign = f"{client_id}|{timestamp}"
            sig_bytes = private_key.sign(
                string_to_sign.encode('utf-8'),
                padding.PKCS1v15(),
                hashes.SHA256()
            )
            signature = base64.b64encode(sig_bytes).decode('utf-8')
        except Exception as e:
            return None, f'Gagal generate signature RSA: {str(e)}'
    else:
        # Sandbox mode: some implementations use client_secret-based auth
        string_to_sign = f"{client_id}|{timestamp}"
        signature = base64.b64encode(
            hashlib.sha256(f"{string_to_sign}|{client_secret}".encode()).digest()
        ).decode('utf-8')
    
    try:
        resp = requests.post(
            f'{base_url}/snap/v1.0/access-token/b2b',
            json={'grantType': 'client_credentials'},
            headers={
                'Content-Type': 'application/json',
                'X-CLIENT-KEY': client_id,
                'X-TIMESTAMP': timestamp,
                'X-SIGNATURE': signature,
            },
            timeout=15
        )
        
        if resp.status_code == 200:
            data = resp.json()
            return data.get('accessToken'), None
        else:
            return None, f'BRI token error: HTTP {resp.status_code} - {resp.text[:200]}'
    except Exception as e:
        return None, f'BRI connection error: {str(e)}'


@api_payments.route('/api/payment/qris-bri/create', methods=['POST'])
@login_required
def api_create_qris_bri():
    """Generate BRI QRIS MPM Dynamic QR code for an order"""
    data = request.json
    order_id = data.get('order_id')
    order = Order.query.get_or_404(order_id)
    
    merchant_id = get_gateway_config('bri_merchant_id', 'BRI_MERCHANT_ID')
    terminal_id = get_gateway_config('bri_terminal_id', 'BRI_TERMINAL_ID')
    is_production = get_gateway_config('bri_is_production', 'BRI_IS_PRODUCTION').lower() == 'true'
    
    if not merchant_id or not terminal_id:
        return jsonify({'success': False, 'error': 'BRI Merchant ID atau Terminal ID belum dikonfigurasi'}), 400
    
    # Get access token
    access_token, err = bri_get_access_token()
    if not access_token:
        return jsonify({'success': False, 'error': err}), 400
    
    base_url = 'https://partner.api.bri.co.id' if is_production else 'https://sandbox.partner.api.bri.co.id'
    partner_ref = f"DTO-{order.order_number}-{int(datetime.now().timestamp())}"
    
    try:
        wib = timezone(timedelta(hours=7))
        now_wib = datetime.now(wib)
        timestamp = now_wib.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now_wib.microsecond // 1000:03d}+07:00'
        
        payload = {
            'partnerReferenceNo': partner_ref,
            'amount': f"{int(order.total)}.00",
            'currency': 'IDR',
            'merchantId': merchant_id,
            'terminalId': terminal_id,
            'additionalInfo': {
                'billInfo': [
                    {'label': 'OrderId', 'value': order.order_number},
                    {'label': 'Customer', 'value': order.customer_name or 'Guest'}
                ]
            }
        }
        
        resp = requests.post(
            f'{base_url}/snap/v1.0/qris-mpm-generate',
            json=payload,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
                'X-TIMESTAMP': timestamp,
            },
            timeout=15
        )
        
        resp_data = resp.json()
        
        if resp.status_code == 200 and resp_data.get('responseCode') == '00':
            qr_content = resp_data.get('qr_content', '')
            original_ref = resp_data.get('originalReferenceNo', '')
            
            # Update payment record
            if order.payment:
                order.payment.payment_method = 'qris_bri'
                order.payment.midtrans_order_id = partner_ref
                order.payment.midtrans_transaction_id = original_ref
                order.payment.status = 'pending'
            else:
                payment = Payment(
                    order_id=order.id,
                    payment_method='qris_bri',
                    amount=order.total,
                    status='pending',
                    midtrans_order_id=partner_ref,
                    midtrans_transaction_id=original_ref
                )
                db.session.add(payment)
            
            db.session.commit()
            
            # Generate QR image using qrcode library
            qr_image_url = None
            try:
                qr = qrcode.make(qr_content)
                qr_buffer = BytesIO()
                qr.save(qr_buffer, format='PNG')
                qr_buffer.seek(0)
                qr_image_url = 'data:image/png;base64,' + base64.b64encode(qr_buffer.getvalue()).decode()
            except Exception:
                pass
            
            return jsonify({
                'success': True,
                'qr_content': qr_content,
                'qr_image': qr_image_url,
                'partner_ref': partner_ref,
                'original_ref': original_ref,
                'amount': order.total
            })
        else:
            return jsonify({
                'success': False,
                'error': f"BRI QRIS error: {resp_data.get('responseDesc', resp.text[:200])}"
            }), 400
    except Exception as e:
        return jsonify({'success': False, 'error': f'Error: {str(e)}'}), 500


@api_payments.route('/api/payment/qris-bri/status', methods=['POST'])
@login_required
def api_check_qris_bri_status():
    """Check BRI QRIS payment status"""
    data = request.json
    order_id = data.get('order_id')
    order = Order.query.get_or_404(order_id)
    
    if not order.payment or not order.payment.midtrans_transaction_id:
        return jsonify({'success': False, 'error': 'No QRIS transaction found'}), 400
    
    merchant_id = get_gateway_config('bri_merchant_id', 'BRI_MERCHANT_ID')
    terminal_id = get_gateway_config('bri_terminal_id', 'BRI_TERMINAL_ID')
    is_production = get_gateway_config('bri_is_production', 'BRI_IS_PRODUCTION').lower() == 'true'
    
    access_token, err = bri_get_access_token()
    if not access_token:
        return jsonify({'success': False, 'error': err}), 400
    
    base_url = 'https://partner.api.bri.co.id' if is_production else 'https://sandbox.partner.api.bri.co.id'
    
    try:
        wib = timezone(timedelta(hours=7))
        now_wib = datetime.now(wib)
        timestamp = now_wib.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now_wib.microsecond // 1000:03d}+07:00'
        
        resp = requests.post(
            f'{base_url}/snap/v1.0/qris-mpm-query',
            json={
                'originalReferenceNo': order.payment.midtrans_transaction_id,
                'merchantId': merchant_id,
                'terminalId': terminal_id,
            },
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
                'X-TIMESTAMP': timestamp,
            },
            timeout=15
        )
        
        resp_data = resp.json()
        
        if resp.status_code == 200:
            # Check if paid - BRI response codes vary
            bri_status = resp_data.get('latestTransactionStatus', resp_data.get('transactionStatusDesc', ''))
            is_paid = bri_status.lower() in ('paid', 'settlement', 'success', '00')
            
            if is_paid and order.payment.status != 'paid':
                order.payment.status = 'paid'
                order.payment.paid_at = utc_now()
                order.status = 'completed'
                db.session.commit()
            
            return jsonify({
                'success': True,
                'status': 'paid' if is_paid else 'pending',
                'bri_status': bri_status,
                'details': resp_data
            })
        else:
            return jsonify({'success': False, 'error': f'Status check failed: {resp.text[:200]}'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@api_payments.route('/api/payment-gateway/test-bri', methods=['POST'])
@login_required
@role_required('admin')
def api_test_bri_gateway():
    """Test BRI QRIS API connection by requesting access token"""
    token, err = bri_get_access_token()
    if token:
        return jsonify({
            'success': True,
            'message': f'Koneksi BRI QRIS berhasil! Token diterima.',
        })
    else:
        return jsonify({
            'success': False,
            'message': err or 'Gagal mendapatkan token BRI'
        })


# ========== TRIPAY PAYMENT GATEWAY ==========

def tripay_create_transaction(order, payment_method_code):
    """Create a Tripay closed payment transaction"""
    api_key = get_gateway_config('tripay_api_key', 'TRIPAY_API_KEY')
    private_key = get_gateway_config('tripay_private_key', 'TRIPAY_PRIVATE_KEY')
    merchant_code = get_gateway_config('tripay_merchant_code', 'TRIPAY_MERCHANT_CODE')
    is_production = get_gateway_config('tripay_is_production', 'TRIPAY_IS_PRODUCTION').lower() == 'true'

    if not api_key or not private_key or not merchant_code:
        return None, 'TRIPAY_API_KEY, TRIPAY_PRIVATE_KEY, atau TRIPAY_MERCHANT_CODE belum dikonfigurasi'

    base_url = 'https://tripay.co.id/api' if is_production else 'https://tripay.co.id/api-sandbox'
    merchant_ref = f"DTO-{order.order_number}"

    # Calculate signature: HMAC-SHA256(merchant_code + merchant_ref + amount)
    amount = int(order.total)
    signature = hmac.new(
        private_key.encode('utf-8'),
        f"{merchant_code}{merchant_ref}{amount}".encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    # Build order items
    order_items = []
    for item in order.items:
        order_items.append({
            'sku': str(item.menu_item_id),
            'name': item.name[:50],
            'price': int(item.price),
            'quantity': item.quantity
        })

    payload = {
        'method': payment_method_code,
        'merchant_ref': merchant_ref,
        'amount': amount,
        'customer_name': order.customer_name or 'Customer',
        'customer_email': 'customer@kasir.local',
        'order_items': order_items,
        'expired_time': int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp()),
        'signature': signature
    }

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }

    try:
        resp = requests.post(f'{base_url}/transaction/create', json=payload, headers=headers, timeout=30)
        data = resp.json()

        if data.get('success'):
            result = data.get('data', {})
            return result, None
        else:
            return None, data.get('message', 'Tripay transaction error')
    except Exception as e:
        return None, f'Tripay connection error: {str(e)}'


@api_payments.route('/api/payment/tripay/create', methods=['POST'])
@login_required
def api_create_tripay_payment():
    """Create Tripay payment for an order"""
    try:
        data = request.json
        order_id = data.get('order_id')
        payment_method = data.get('payment_method', 'QRIS')

        order = Order.query.get_or_404(order_id)

        result, err = tripay_create_transaction(order, payment_method)
        if not result:
            return jsonify({'success': False, 'error': err}), 400

        # Update payment record
        if order.payment:
            order.payment.payment_method = 'tripay'
            order.payment.midtrans_order_id = result.get('merchant_ref', '')
            order.payment.midtrans_transaction_id = result.get('reference', '')
            order.payment.status = 'pending'
            order.payment.payment_url = result.get('checkout_url', '')
        else:
            payment = Payment(
                order_id=order.id,
                payment_method='tripay',
                amount=order.total,
                status='pending',
                midtrans_order_id=result.get('merchant_ref', ''),
                midtrans_transaction_id=result.get('reference', ''),
                payment_url=result.get('checkout_url', '')
            )
            db.session.add(payment)

        db.session.commit()

        # Generate QR image if QRIS
        qr_image_url = None
        qr_string = result.get('qr_string') or result.get('qr_url', '')
        if qr_string:
            try:
                qr = qrcode.make(qr_string)
                qr_buffer = BytesIO()
                qr.save(qr_buffer, format='PNG')
                qr_buffer.seek(0)
                qr_image_url = 'data:image/png;base64,' + base64.b64encode(qr_buffer.getvalue()).decode()
            except Exception:
                pass

        return jsonify({
            'success': True,
            'reference': result.get('reference', ''),
            'merchant_ref': result.get('merchant_ref', ''),
            'checkout_url': result.get('checkout_url', ''),
            'qr_image': qr_image_url,
            'pay_code': result.get('pay_code', ''),
            'pay_url': result.get('pay_url', ''),
            'amount': order.total,
            'expired_time': result.get('expired_time')
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@api_payments.route('/api/payment/tripay/status', methods=['POST'])
@login_required
def api_check_tripay_status():
    """Check Tripay payment status"""
    data = request.json
    order_id = data.get('order_id')
    order = Order.query.get_or_404(order_id)

    if not order.payment or not order.payment.midtrans_transaction_id:
        return jsonify({'success': False, 'error': 'No Tripay transaction found'}), 400

    api_key = get_gateway_config('tripay_api_key', 'TRIPAY_API_KEY')
    is_production = get_gateway_config('tripay_is_production', 'TRIPAY_IS_PRODUCTION').lower() == 'true'
    base_url = 'https://tripay.co.id/api' if is_production else 'https://tripay.co.id/api-sandbox'

    try:
        resp = requests.get(
            f'{base_url}/transaction/detail',
            params={'reference': order.payment.midtrans_transaction_id},
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=15
        )
        resp_data = resp.json()

        if resp_data.get('success'):
            detail = resp_data.get('data', {})
            tripay_status = detail.get('status', '')

            if tripay_status == 'PAID' and order.payment.status != 'paid':
                order.payment.status = 'paid'
                order.payment.paid_at = utc_now()
                order.status = 'completed'

                amount_formatted = f"{order.payment.amount:,}".replace(',', '.')
                create_notification(
                    type='payment_success',
                    title='Pembayaran Tripay Berhasil!',
                    message=f'Order #{order.order_number} - Rp {amount_formatted}',
                    data={'order_id': order.id, 'payment_id': order.payment.id}
                )
                db.session.commit()

            return jsonify({
                'success': True,
                'status': 'paid' if tripay_status == 'PAID' else tripay_status.lower(),
                'tripay_status': tripay_status,
                'details': detail
            })
        else:
            return jsonify({'success': False, 'error': resp_data.get('message', 'Status check failed')}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@csrf.exempt
@api_payments.route('/api/payment/tripay/callback', methods=['POST'])
@limiter.limit("30 per minute")
def api_tripay_callback():
    """Handle Tripay payment callback webhook"""
    try:
        private_key = get_gateway_config('tripay_private_key', 'TRIPAY_PRIVATE_KEY')
        callback_signature = request.headers.get('X-Callback-Signature', '')
        raw_body = request.get_data(as_text=True)

        # Verify signature
        if private_key and callback_signature:
            expected_signature = hmac.new(
                private_key.encode('utf-8'),
                raw_body.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(callback_signature, expected_signature):
                import logging
                logging.getLogger(__name__).warning(
                    f'Tripay callback signature mismatch from {request.remote_addr}'
                )
                return jsonify({'error': 'Invalid signature'}), 403

        data = request.json
        merchant_ref = data.get('merchant_ref', '')
        status = data.get('status', '')

        payment = Payment.query.filter_by(midtrans_order_id=merchant_ref).first()
        if payment:
            if status == 'PAID':
                payment.status = 'paid'
                payment.paid_at = utc_now()
                payment.order.status = 'completed'

                amount_formatted = f"{payment.amount:,}".replace(',', '.')
                create_notification(
                    type='payment_success',
                    title='Pembayaran Tripay Berhasil!',
                    message=f'Order #{payment.order.order_number} - Rp {amount_formatted}',
                    data={'order_id': payment.order_id, 'payment_id': payment.id}
                )
            elif status in ('EXPIRED', 'FAILED'):
                payment.status = 'failed'
                create_notification(
                    type='payment_failed',
                    title='Pembayaran Tripay Gagal',
                    message=f'Order #{payment.order.order_number} - Status: {status}',
                    data={'order_id': payment.order_id, 'payment_id': payment.id}
                )

            db.session.commit()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_payments.route('/api/payment-gateway/test-tripay', methods=['POST'])
@login_required
@role_required('admin')
def api_test_tripay_gateway():
    """Test Tripay API connection"""
    api_key = get_gateway_config('tripay_api_key', 'TRIPAY_API_KEY')
    is_production = get_gateway_config('tripay_is_production', 'TRIPAY_IS_PRODUCTION').lower() == 'true'

    if not api_key:
        return jsonify({
            'success': False,
            'message': 'TRIPAY_API_KEY belum dikonfigurasi'
        })

    base_url = 'https://tripay.co.id/api' if is_production else 'https://tripay.co.id/api-sandbox'

    try:
        resp = requests.get(
            f'{base_url}/merchant/payment-channel',
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=10
        )

        if resp.status_code == 200:
            data = resp.json()
            if data.get('success'):
                channels = data.get('data', [])
                active_count = sum(1 for ch in channels if ch.get('active'))
                return jsonify({
                    'success': True,
                    'message': f'Koneksi Tripay berhasil! {active_count} metode pembayaran aktif. Mode: {"Production" if is_production else "Sandbox"}'
                })
            else:
                return jsonify({'success': False, 'message': data.get('message', 'Unknown error')})
        else:
            return jsonify({'success': False, 'message': f'HTTP {resp.status_code}'})
    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'message': 'Koneksi timeout'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'})


# ========== DUITKU PAYMENT GATEWAY ==========

def duitku_create_invoice(order):
    """Create a Duitku invoice payment"""
    api_key = get_gateway_config('duitku_api_key', 'DUITKU_API_KEY')
    merchant_code = get_gateway_config('duitku_merchant_code', 'DUITKU_MERCHANT_CODE')
    is_production = get_gateway_config('duitku_is_production', 'DUITKU_IS_PRODUCTION').lower() == 'true'

    if not api_key or not merchant_code:
        return None, 'DUITKU_API_KEY atau DUITKU_MERCHANT_CODE belum dikonfigurasi'

    base_url = 'https://api-prod.duitku.com' if is_production else 'https://api-sandbox.duitku.com'
    merchant_order_id = f"DTO-{order.order_number}"
    amount = int(order.total)

    # Signature: SHA256(merchantCode + paymentAmount + merchantOrderId + apiKey)
    signature = hashlib.sha256(
        f"{merchant_code}{amount}{merchant_order_id}{api_key}".encode('utf-8')
    ).hexdigest()

    # Determine callback/return URLs
    app_url = current_app.config.get('APP_URL', '') or request.host_url.rstrip('/')
    callback_url = f"{app_url}/api/payment/duitku/callback"
    return_url = f"{app_url}/orders"

    payload = {
        'merchantCode': merchant_code,
        'paymentAmount': amount,
        'merchantOrderId': merchant_order_id,
        'productDetails': f'Order #{order.order_number}',
        'email': 'customer@kasir.local',
        'callbackUrl': callback_url,
        'returnUrl': return_url,
        'signature': signature,
        'customerVaName': order.customer_name or 'Customer',
        'expiryPeriod': 1440  # 24 hours in minutes
    }

    if current_user.is_authenticated and current_user.email:
        payload['email'] = current_user.email

    headers = {'Content-Type': 'application/json'}

    try:
        resp = requests.post(
            f'{base_url}/api/merchant/createInvoice',
            json=payload, headers=headers, timeout=30
        )
        data = resp.json()

        if data.get('statusCode') == '00':
            return data, None
        else:
            return None, data.get('statusMessage', f'Duitku error: {resp.text[:200]}')
    except Exception as e:
        return None, f'Duitku connection error: {str(e)}'


@api_payments.route('/api/payment/duitku/create', methods=['POST'])
@login_required
def api_create_duitku_payment():
    """Create Duitku payment for an order"""
    try:
        data = request.json
        order_id = data.get('order_id')
        order = Order.query.get_or_404(order_id)

        result, err = duitku_create_invoice(order)
        if not result:
            return jsonify({'success': False, 'error': err}), 400

        merchant_order_id = f"DTO-{order.order_number}"
        payment_url = result.get('paymentUrl', '')
        reference = result.get('reference', '')

        if order.payment:
            order.payment.payment_method = 'duitku'
            order.payment.midtrans_order_id = merchant_order_id
            order.payment.midtrans_transaction_id = reference
            order.payment.status = 'pending'
            order.payment.payment_url = payment_url
        else:
            payment = Payment(
                order_id=order.id,
                payment_method='duitku',
                amount=order.total,
                status='pending',
                midtrans_order_id=merchant_order_id,
                midtrans_transaction_id=reference,
                payment_url=payment_url
            )
            db.session.add(payment)

        db.session.commit()

        return jsonify({
            'success': True,
            'payment_url': payment_url,
            'reference': reference,
            'merchant_order_id': merchant_order_id,
            'amount': order.total
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@csrf.exempt
@api_payments.route('/api/payment/duitku/callback', methods=['POST'])
@limiter.limit("30 per minute")
def api_duitku_callback():
    """Handle Duitku payment callback webhook"""
    try:
        api_key = get_gateway_config('duitku_api_key', 'DUITKU_API_KEY')
        merchant_code = request.form.get('merchantCode', '')
        amount = request.form.get('amount', '')
        merchant_order_id = request.form.get('merchantOrderId', '')
        result_code = request.form.get('resultCode', '')
        callback_signature = request.form.get('signature', '')

        # Verify signature: MD5(merchantCode + amount + merchantOrderId + apiKey)
        if api_key and callback_signature:
            expected_signature = hashlib.md5(
                f"{merchant_code}{amount}{merchant_order_id}{api_key}".encode('utf-8')
            ).hexdigest()
            if not hmac.compare_digest(callback_signature, expected_signature):
                import logging
                logging.getLogger(__name__).warning(
                    'Duitku callback signature mismatch from %s', request.remote_addr
                )
                return jsonify({'error': 'Invalid signature'}), 403

        payment = Payment.query.filter_by(midtrans_order_id=merchant_order_id).first()
        if payment:
            if result_code == '00':
                payment.status = 'paid'
                payment.paid_at = utc_now()
                payment.order.status = 'completed'

                amount_formatted = f"{payment.amount:,}".replace(',', '.')
                create_notification(
                    type='payment_success',
                    title='Pembayaran Duitku Berhasil!',
                    message=f'Order #{payment.order.order_number} - Rp {amount_formatted}',
                    data={'order_id': payment.order_id, 'payment_id': payment.id}
                )
            elif result_code == '01':
                payment.status = 'failed'
                create_notification(
                    type='payment_failed',
                    title='Pembayaran Duitku Gagal',
                    message=f'Order #{payment.order.order_number} - Status: Failed',
                    data={'order_id': payment.order_id, 'payment_id': payment.id}
                )

            db.session.commit()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_payments.route('/api/payment/duitku/status', methods=['POST'])
@login_required
def api_check_duitku_status():
    """Check Duitku payment status"""
    data = request.json
    order_id = data.get('order_id')
    order = Order.query.get_or_404(order_id)

    if not order.payment or not order.payment.midtrans_order_id:
        return jsonify({'success': False, 'error': 'No Duitku transaction found'}), 400

    api_key = get_gateway_config('duitku_api_key', 'DUITKU_API_KEY')
    merchant_code = get_gateway_config('duitku_merchant_code', 'DUITKU_MERCHANT_CODE')
    is_production = get_gateway_config('duitku_is_production', 'DUITKU_IS_PRODUCTION').lower() == 'true'
    base_url = 'https://api-prod.duitku.com' if is_production else 'https://api-sandbox.duitku.com'

    merchant_order_id = order.payment.midtrans_order_id
    signature = hashlib.md5(
        f"{merchant_code}{merchant_order_id}{api_key}".encode('utf-8')
    ).hexdigest()

    try:
        resp = requests.post(
            f'{base_url}/api/merchant/transactionStatus',
            json={
                'merchantCode': merchant_code,
                'merchantOrderId': merchant_order_id,
                'signature': signature
            },
            headers={'Content-Type': 'application/json'},
            timeout=15
        )
        resp_data = resp.json()
        status_code = resp_data.get('statusCode', '')

        if status_code == '00' and order.payment.status != 'paid':
            order.payment.status = 'paid'
            order.payment.paid_at = utc_now()
            order.status = 'completed'

            amount_formatted = f"{order.payment.amount:,}".replace(',', '.')
            create_notification(
                type='payment_success',
                title='Pembayaran Duitku Berhasil!',
                message=f'Order #{order.order_number} - Rp {amount_formatted}',
                data={'order_id': order.id, 'payment_id': order.payment.id}
            )
            db.session.commit()

        return jsonify({
            'success': True,
            'status': 'paid' if status_code == '00' else 'pending',
            'duitku_status': resp_data.get('statusMessage', ''),
            'details': resp_data
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@api_payments.route('/api/payment-gateway/test-duitku', methods=['POST'])
@login_required
@role_required('admin')
def api_test_duitku_gateway():
    """Test Duitku API connection"""
    api_key = get_gateway_config('duitku_api_key', 'DUITKU_API_KEY')
    merchant_code = get_gateway_config('duitku_merchant_code', 'DUITKU_MERCHANT_CODE')
    is_production = get_gateway_config('duitku_is_production', 'DUITKU_IS_PRODUCTION').lower() == 'true'

    if not api_key or not merchant_code:
        return jsonify({
            'success': False,
            'message': 'DUITKU_API_KEY atau DUITKU_MERCHANT_CODE belum dikonfigurasi'
        })

    base_url = 'https://api-prod.duitku.com' if is_production else 'https://api-sandbox.duitku.com'

    # Test by getting payment methods
    amount = 10000
    signature = hashlib.sha256(
        f"{merchant_code}{amount}{api_key}".encode('utf-8')
    ).hexdigest()

    try:
        resp = requests.post(
            f'{base_url}/api/merchant/paymentmethod/getpaymentmethod',
            json={
                'merchantcode': merchant_code,
                'amount': amount,
                'datetime': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                'signature': signature
            },
            headers={'Content-Type': 'application/json'},
            timeout=10
        )

        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data.get('paymentFee'), list):
                method_count = len(data['paymentFee'])
                return jsonify({
                    'success': True,
                    'message': f'Koneksi Duitku berhasil! {method_count} metode pembayaran tersedia. Mode: {"Production" if is_production else "Sandbox"}'
                })
            elif data.get('Message'):
                return jsonify({'success': False, 'message': data['Message']})
            else:
                return jsonify({
                    'success': True,
                    'message': f'Koneksi Duitku berhasil! Mode: {"Production" if is_production else "Sandbox"}'
                })
        else:
            return jsonify({'success': False, 'message': f'HTTP {resp.status_code}'})
    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'message': 'Koneksi timeout'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'})


# ========== DOKU CHECKOUT PAYMENT GATEWAY ==========

def doku_generate_signature(client_id, secret_key, request_id, request_timestamp, request_target, body_json=None):
    """Generate DOKU HMAC-SHA256 signature"""
    # Calculate digest from body (SHA256 + Base64)
    if body_json:
        body_string = json.dumps(body_json, separators=(',', ':'))
        digest = base64.b64encode(
            hashlib.sha256(body_string.encode('utf-8')).digest()
        ).decode('utf-8')
    else:
        digest = ''

    # Build component signature string
    component = f"Client-Id:{client_id}\nRequest-Id:{request_id}\nRequest-Timestamp:{request_timestamp}\nRequest-Target:{request_target}"
    if digest:
        component += f"\nDigest:{digest}"

    # HMAC-SHA256 sign
    signature = base64.b64encode(
        hmac.new(
            secret_key.encode('utf-8'),
            component.encode('utf-8'),
            hashlib.sha256
        ).digest()
    ).decode('utf-8')

    return f"HMACSHA256={signature}"


@api_payments.route('/api/payment/doku/create', methods=['POST'])
@login_required
def api_create_doku_payment():
    """Create DOKU Checkout payment for an order"""
    try:
        data = request.json
        order_id = data.get('order_id')
        order = Order.query.get_or_404(order_id)

        client_id = get_gateway_config('doku_client_id', 'DOKU_CLIENT_ID')
        secret_key = get_gateway_config('doku_secret_key', 'DOKU_SECRET_KEY')
        is_production = get_gateway_config('doku_is_production', 'DOKU_IS_PRODUCTION').lower() == 'true'

        if not client_id or not secret_key:
            return jsonify({'success': False, 'error': 'DOKU_CLIENT_ID atau DOKU_SECRET_KEY belum dikonfigurasi'}), 400

        base_url = 'https://api.doku.com' if is_production else 'https://api-sandbox.doku.com'
        request_target = '/checkout/v1/payment'
        invoice_number = f"DTO-{order.order_number}"

        request_id = str(uuid.uuid4())
        request_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        # Determine callback URL
        app_url = current_app.config.get('APP_URL', '') or request.host_url.rstrip('/')

        payload = {
            'order': {
                'amount': int(order.total),
                'invoice_number': invoice_number,
                'currency': 'IDR',
                'callback_url': f"{app_url}/api/payment/doku/callback",
                'line_items': [{
                    'name': item.name[:50],
                    'price': int(item.price),
                    'quantity': item.quantity
                } for item in order.items]
            },
            'payment': {
                'payment_due_date': 60
            },
            'customer': {
                'name': order.customer_name or 'Customer',
                'email': 'customer@kasir.local'
            }
        }

        if current_user.is_authenticated and current_user.email:
            payload['customer']['email'] = current_user.email

        signature = doku_generate_signature(
            client_id, secret_key, request_id, request_timestamp, request_target, payload
        )

        headers = {
            'Client-Id': client_id,
            'Request-Id': request_id,
            'Request-Timestamp': request_timestamp,
            'Signature': signature,
            'Content-Type': 'application/json'
        }

        resp = requests.post(
            f'{base_url}{request_target}',
            data=json.dumps(payload, separators=(',', ':')),
            headers=headers,
            timeout=30
        )

        resp_data = resp.json()

        if resp.status_code in (200, 201):
            response_data = resp_data.get('response', resp_data)
            payment_info = response_data.get('payment', {})
            payment_url = payment_info.get('url', '')
            order_info = response_data.get('order', {})

            if order.payment:
                order.payment.payment_method = 'doku'
                order.payment.midtrans_order_id = invoice_number
                order.payment.midtrans_transaction_id = order_info.get('invoice_number', invoice_number)
                order.payment.status = 'pending'
                order.payment.payment_url = payment_url
            else:
                payment = Payment(
                    order_id=order.id,
                    payment_method='doku',
                    amount=order.total,
                    status='pending',
                    midtrans_order_id=invoice_number,
                    midtrans_transaction_id=order_info.get('invoice_number', invoice_number),
                    payment_url=payment_url
                )
                db.session.add(payment)

            db.session.commit()

            return jsonify({
                'success': True,
                'payment_url': payment_url,
                'invoice_number': invoice_number,
                'amount': order.total
            })
        else:
            error_msg = resp_data.get('error', {}).get('message', resp.text[:200])
            return jsonify({'success': False, 'error': f'DOKU error: {error_msg}'}), 400

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@csrf.exempt
@api_payments.route('/api/payment/doku/callback', methods=['POST'])
@limiter.limit("30 per minute")
def api_doku_callback():
    """Handle DOKU payment notification webhook"""
    try:
        client_id = get_gateway_config('doku_client_id', 'DOKU_CLIENT_ID')
        secret_key = get_gateway_config('doku_secret_key', 'DOKU_SECRET_KEY')

        # Verify signature from headers
        received_signature = request.headers.get('Signature', '')
        header_client_id = request.headers.get('Client-Id', '')
        header_request_id = request.headers.get('Request-Id', '')
        header_timestamp = request.headers.get('Request-Timestamp', '')

        if secret_key and received_signature:
            raw_body = request.get_data(as_text=True)
            digest = base64.b64encode(
                hashlib.sha256(raw_body.encode('utf-8')).digest()
            ).decode('utf-8')

            # Reconstruct signature
            request_target = '/api/payment/doku/callback'
            component = f"Client-Id:{header_client_id}\nRequest-Id:{header_request_id}\nRequest-Timestamp:{header_timestamp}\nRequest-Target:{request_target}\nDigest:{digest}"
            expected_signature = base64.b64encode(
                hmac.new(
                    secret_key.encode('utf-8'),
                    component.encode('utf-8'),
                    hashlib.sha256
                ).digest()
            ).decode('utf-8')

            if received_signature != f"HMACSHA256={expected_signature}":
                import logging
                logging.getLogger(__name__).warning(
                    'DOKU callback signature mismatch from %s', request.remote_addr
                )
                return jsonify({'error': 'Invalid signature'}), 403

        data = request.json
        if not data:
            return jsonify({'error': 'No data'}), 400

        # Extract transaction info
        order_info = data.get('order', {})
        transaction_info = data.get('transaction', {})
        invoice_number = order_info.get('invoice_number', '')
        status = transaction_info.get('status', '')

        payment = Payment.query.filter_by(midtrans_order_id=invoice_number).first()
        if payment:
            if status == 'SUCCESS':
                payment.status = 'paid'
                payment.paid_at = utc_now()
                payment.order.status = 'completed'

                amount_formatted = f"{payment.amount:,}".replace(',', '.')
                create_notification(
                    type='payment_success',
                    title='Pembayaran DOKU Berhasil!',
                    message=f'Order #{payment.order.order_number} - Rp {amount_formatted}',
                    data={'order_id': payment.order_id, 'payment_id': payment.id}
                )
            elif status in ('FAILED', 'EXPIRED'):
                payment.status = 'failed'
                create_notification(
                    type='payment_failed',
                    title='Pembayaran DOKU Gagal',
                    message=f'Order #{payment.order.order_number} - Status: {status}',
                    data={'order_id': payment.order_id, 'payment_id': payment.id}
                )

            db.session.commit()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_payments.route('/api/payment/doku/status', methods=['POST'])
@login_required
def api_check_doku_status():
    """Check DOKU payment status (by re-querying the order)"""
    data = request.json
    order_id = data.get('order_id')
    order = Order.query.get_or_404(order_id)

    if not order.payment:
        return jsonify({'success': False, 'error': 'No DOKU transaction found'}), 400

    # DOKU Checkout relies on webhook notifications for status updates
    # Return current stored status
    return jsonify({
        'success': True,
        'status': order.payment.status,
        'invoice_number': order.payment.midtrans_order_id
    })


@api_payments.route('/api/payment-gateway/test-doku', methods=['POST'])
@login_required
@role_required('admin')
def api_test_doku_gateway():
    """Test DOKU API connection by making a small checkout request"""
    client_id = get_gateway_config('doku_client_id', 'DOKU_CLIENT_ID')
    secret_key = get_gateway_config('doku_secret_key', 'DOKU_SECRET_KEY')
    is_production = get_gateway_config('doku_is_production', 'DOKU_IS_PRODUCTION').lower() == 'true'

    if not client_id or not secret_key:
        return jsonify({
            'success': False,
            'message': 'DOKU_CLIENT_ID atau DOKU_SECRET_KEY belum dikonfigurasi'
        })

    base_url = 'https://api.doku.com' if is_production else 'https://api-sandbox.doku.com'
    request_target = '/checkout/v1/payment'

    request_id = str(uuid.uuid4())
    request_timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    payload = {
        'order': {
            'amount': 1000,
            'invoice_number': f'TEST-{int(datetime.now().timestamp())}',
            'currency': 'IDR'
        },
        'payment': {'payment_due_date': 5},
        'customer': {'name': 'Test', 'email': 'test@kasir.local'}
    }

    signature = doku_generate_signature(
        client_id, secret_key, request_id, request_timestamp, request_target, payload
    )

    headers = {
        'Client-Id': client_id,
        'Request-Id': request_id,
        'Request-Timestamp': request_timestamp,
        'Signature': signature,
        'Content-Type': 'application/json'
    }

    try:
        resp = requests.post(
            f'{base_url}{request_target}',
            data=json.dumps(payload, separators=(',', ':')),
            headers=headers,
            timeout=10
        )

        if resp.status_code in (200, 201):
            return jsonify({
                'success': True,
                'message': f'Koneksi DOKU berhasil! Mode: {"Production" if is_production else "Sandbox"}'
            })
        elif resp.status_code == 401:
            return jsonify({'success': False, 'message': 'Autentikasi gagal. Periksa Client ID dan Secret Key.'})
        else:
            return jsonify({'success': False, 'message': f'HTTP {resp.status_code}: {resp.text[:200]}'})
    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'message': 'Koneksi timeout'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Error: {str(e)}'})
