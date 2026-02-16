import base64
import hashlib
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO

import qrcode
import requests

from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from models import db, Order, Payment
from extensions import limiter, csrf
from utils import utc_now, create_notification, role_required

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
    client_id = os.environ.get('BRI_CLIENT_ID', '')
    client_secret = os.environ.get('BRI_CLIENT_SECRET', '')
    private_key_path = os.environ.get('BRI_PRIVATE_KEY_PATH', '')
    is_production = os.environ.get('BRI_IS_PRODUCTION', 'false').lower() == 'true'
    
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
    
    merchant_id = os.environ.get('BRI_MERCHANT_ID', '')
    terminal_id = os.environ.get('BRI_TERMINAL_ID', '')
    is_production = os.environ.get('BRI_IS_PRODUCTION', 'false').lower() == 'true'
    
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
    
    merchant_id = os.environ.get('BRI_MERCHANT_ID', '')
    terminal_id = os.environ.get('BRI_TERMINAL_ID', '')
    is_production = os.environ.get('BRI_IS_PRODUCTION', 'false').lower() == 'true'
    
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
