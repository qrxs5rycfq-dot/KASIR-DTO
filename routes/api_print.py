import json
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy import or_
from models import db, PendingPrint
from extensions import limiter
from socket_handlers import emit_print_job
from utils import utc_now, get_user_branch_id, get_default_branch_id, branch_filter, validate_token

# Import USB printer module
try:
    from usb_printer import usb_printer, USBPrinterManager
    USB_PRINTING_AVAILABLE = USBPrinterManager.is_available()
except ImportError:
    usb_printer = None
    USB_PRINTING_AVAILABLE = False

api_print = Blueprint('api_print', __name__, url_prefix='')


@api_print.route('/api/pending-prints/unprocessed', methods=['GET'])
@login_required
def get_unprocessed_prints():
    """Get all pending prints that haven't been processed yet"""
    # Bisa pakai token dari query parameter untuk Android
    token = request.args.get('token')
    user = None
    
    if token:
        # Validasi token untuk akses tanpa session
        user = validate_token(token)
        if not user:
            return jsonify({'success': False, 'error': 'Invalid token'}), 401
        
        # Check if user has print permission
        if not (user.has_role('admin', 'manager', 'kasir', 'printer_operator')):
            return jsonify({'success': False, 'error': 'User does not have print permission'}), 403
            
        branch_id = user.branch_id
    else:
        # Pakai session biasa
        if not current_user.is_authenticated:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        
        # Check if user has print permission
        if not (current_user.has_role('admin', 'manager', 'kasir', 'printer_operator')):
            return jsonify({'success': False, 'error': 'User does not have print permission'}), 403
            
        user = current_user
        branch_id = get_user_branch_id()
    
    # Ambil pending prints dengan status pending, atau processing yang stuck > 2 menit
    two_min_ago = utc_now() - timedelta(minutes=2)
    query = PendingPrint.query.filter(
        or_(
            PendingPrint.status == 'pending',
            db.and_(PendingPrint.status == 'processing', PendingPrint.updated_at < two_min_ago)
        )
    ).order_by(PendingPrint.created_at)
    
    if branch_id:
        query = query.filter_by(branch_id=branch_id)
    
    pending_prints = query.limit(50).all()  # Batasi 50 print terbanyak
    
    # Mark prints as processing when fetched
    for p in pending_prints:
        p.status = 'processing'
        p.updated_at = utc_now()
    db.session.commit()
    
    return jsonify({
        'success': True,
        'pending_prints': [p.to_dict() for p in pending_prints],
        'count': len(pending_prints)
    })


def create_print_job(order):
    """Create print job for order and emit via WebSocket"""
    try:
        # CEK SEMUA PRINT JOBS, BUKAN HANYA STATUS TERTENTU
        existing_prints = PendingPrint.query.filter_by(order_id=order.id).all()
        
        if existing_prints:
            print(f"⚠️ Order #{order.order_number} already has {len(existing_prints)} print jobs:")
            for p in existing_prints:
                print(f"   - ID:{p.id}, current_copy:{p.current_copy}")
            
            # KEMBALIKAN TANPA MEMBUAT BARU
            # Tapi emit ulang untuk yang pertama
            emit_print_job(existing_prints[0], order.branch_id)
            return existing_prints[0]
        
        print(f"📝 Creating 3 print jobs for order #{order.order_number}")
        
        # Label 3 copy: Pelanggan, Kasir, Koki
        copy_labels = ['Pelanggan', 'Kasir', 'Koki']
        
        # Helper format currency
        def fmt_rp(amount):
            return f"Rp {amount:,}".replace(',', '.')
        
        # Helper buat separator & padding yang responsive terhadap lebar kertas
        # 80mm ~ 48 char, 60mm ~ 35 char, 40mm ~ 24 char
        def sep_double(w):
            return '=' * w
        
        def sep_single(w):
            return '-' * w
        
        def pad_lr(left, right, w):
            """Pad left-right text to fill width w"""
            space = w - len(left) - len(right)
            if space < 1:
                return left + ' ' + right
            return left + ' ' * space + right
        
        def truncate(text, max_len):
            if len(text) <= max_len:
                return text
            return text[:max_len - 3] + '...'
        
        # Default width (80mm). Android PrintService akan menyesuaikan saat render.
        W = 48
        
        # Bangun receipt data per copy
        def build_receipt(copy_num, copy_label):
            is_kitchen = (copy_label == 'Koki')
            data = []
            
            # ===== HEADER =====
            data.append({"type": "text", "value": sep_double(W), "align": "center"})
            data.append({"type": "text", "value": "DAPOER TERAS OBOR", "align": "center", "bold": True, "size": "large"})
            data.append({"type": "text", "value": "Kuliner Nusantara", "align": "center"})
            data.append({"type": "text", "value": sep_double(W), "align": "center"})
            
            if not is_kitchen:
                data.append({"type": "text", "value": "Jl. Rw. Belong, Jakarta Barat", "align": "center"})
                data.append({"type": "text", "value": ""})
            
            # ===== COPY INDICATOR =====
            indicator = f"[ {copy_label.upper()} - {copy_num}/3 ]"
            data.append({"type": "text", "value": indicator, "align": "center", "bold": True})
            data.append({"type": "text", "value": sep_single(W), "align": "center"})
            
            # ===== ORDER INFO =====
            data.append({"type": "text", "value": f"No   : #{order.order_number}", "bold": True})
            data.append({"type": "text", "value": f"Tgl  : {datetime.now().strftime('%d/%m/%Y %H:%M')}"})
            table_text = order.table.number if order.table else 'Takeaway'
            data.append({"type": "text", "value": f"Meja : {table_text}"})
            
            if not is_kitchen:
                data.append({"type": "text", "value": f"Nama : {order.customer_name or 'Guest'}"})
                order_type_text = 'Dine In' if order.order_type == 'dine_in' else 'Take Away'
                data.append({"type": "text", "value": f"Tipe : {order_type_text}"})
                data.append({"type": "text", "value": f"Kasir: {order.user.username if order.user else '-'}"})
            
            # ===== DETAIL PESANAN =====
            data.append({"type": "text", "value": sep_single(W), "align": "center"})
            title = "PESANAN DAPUR" if is_kitchen else "DETAIL PESANAN"
            data.append({"type": "text", "value": title, "align": "center", "bold": True})
            data.append({"type": "text", "value": sep_single(W), "align": "center"})
            
            spice_labels = {'none': 'Tdk Pedas', 'medium': 'Sedang', 'hot': 'Pedas'}
            temp_labels = {'hot': 'Panas', 'cold': 'Dingin'}
            
            for item in order.items:
                # Nama item
                item_name = truncate(item.name, W - 2)
                data.append({"type": "text", "value": item_name, "bold": True})
                
                # Qty & harga (baris kedua)
                qty_text = f"  {item.quantity}x @{fmt_rp(item.price)}"
                price_text = fmt_rp(item.price * item.quantity)
                data.append({"type": "text", "value": pad_lr(qty_text, price_text, W)})
                
                # Detail item (spice, temp, notes) - selalu tampilkan
                details = []
                if item.spice_level and item.spice_level != 'none':
                    details.append(spice_labels.get(item.spice_level, item.spice_level))
                if item.temperature and item.temperature != 'normal':
                    details.append(temp_labels.get(item.temperature, item.temperature))
                if details:
                    data.append({"type": "text", "value": f"  >> {', '.join(details)}"})
                
                if item.notes:
                    note_text = truncate(f"  *) {item.notes}", W)
                    data.append({"type": "text", "value": note_text})
            
            data.append({"type": "text", "value": sep_single(W), "align": "center"})
            
            # ===== TOTALS (tidak untuk Koki) =====
            if not is_kitchen:
                data.append({"type": "text", "value": pad_lr("Subtotal", fmt_rp(order.subtotal), W)})
                
                if order.discount and order.discount > 0:
                    data.append({"type": "text", "value": pad_lr("Diskon", f"-{fmt_rp(order.discount)}", W)})
                
                data.append({"type": "text", "value": sep_double(W), "align": "center"})
                data.append({"type": "text", "value": pad_lr("TOTAL", fmt_rp(order.total), W), "bold": True, "size": "large"})
                data.append({"type": "text", "value": sep_double(W), "align": "center"})
                
                # Payment info
                if order.payment:
                    method_map = {'cash': 'TUNAI', 'online': 'ONLINE', 'midtrans': 'MIDTRANS', 'transfer': 'TRANSFER'}
                    method_text = method_map.get(order.payment.payment_method, order.payment.payment_method.upper())
                    data.append({"type": "text", "value": pad_lr(f"Bayar ({method_text})", fmt_rp(order.payment.paid_amount), W)})
                    if order.payment.change_amount and order.payment.change_amount > 0:
                        data.append({"type": "text", "value": pad_lr("Kembali", fmt_rp(order.payment.change_amount), W)})
                
                data.append({"type": "text", "value": sep_single(W), "align": "center"})
            
            # ===== FOOTER =====
            data.append({"type": "text", "value": ""})
            if is_kitchen:
                data.append({"type": "text", "value": f"#{order.order_number} - Meja {table_text}", "align": "center", "bold": True})
                data.append({"type": "text", "value": f"[ KOKI - {copy_num}/3 ]", "align": "center"})
            else:
                data.append({"type": "text", "value": "Terima Kasih", "align": "center", "bold": True})
                data.append({"type": "text", "value": "Selamat Menikmati!", "align": "center"})
                data.append({"type": "text", "value": ""})
                data.append({"type": "text", "value": "~ Dapoer Teras Obor ~", "align": "center"})
                data.append({"type": "text", "value": f"[ {copy_label} - {copy_num}/3 ]", "align": "center"})
            
            data.append({"type": "text", "value": sep_double(W), "align": "center"})
            data.append({"type": "text", "value": ""})
            data.append({"type": "cut"})
            
            return data
        
        # GUNAKAN ARRAY UNTUK MENAMPUNG ID
        created_ids = []
        
        # GUNAKAN SATU TRANSAKSI UNTUK 3 RECORD - tiap copy punya receipt data sendiri
        for copy_num in range(1, 4):
            label = copy_labels[copy_num - 1]
            receipt_data = build_receipt(copy_num, label)
            receipt_data_json = json.dumps(receipt_data)
            
            pending_print = PendingPrint(
                order_id=order.id,
                receipt_data=receipt_data_json,
                copies=3,
                current_copy=copy_num,
                status='pending',
                branch_id=order.branch_id
            )
            db.session.add(pending_print)
            db.session.flush()  # Dapatkan ID
            created_ids.append(pending_print.id)
            print(f"   Created print job ID:{pending_print.id} (copy {copy_num}/3 - {label})")
        
        # COMMIT SEKALI DI AKHIR
        db.session.commit()
        print(f"✅ Successfully created {len(created_ids)} print jobs: {created_ids}")
        
        # EMIT UNTUK SEMUA COPY (3 lembar)
        for pid in created_ids:
            p = db.session.get(PendingPrint, pid)
            if p:
                emit_print_job(p, order.branch_id)
        
        first_print = db.session.get(PendingPrint, created_ids[0])
        return first_print
        
    except Exception as e:
        db.session.rollback()
        print(f"❌ Error creating print job: {e}")
        traceback.print_exc()
        return None


# Printer Status API
@api_print.route('/api/printer-status')
@login_required
def get_printer_status():
    """Get saved printer info for current user"""
    return jsonify({
        'success': True,
        'printer_name': current_user.printer_name,
        'printer_id': current_user.printer_id
    })

@api_print.route('/api/printer-status', methods=['POST'])
@login_required
def save_printer_status():
    """Save printer info to database for current user"""
    data = request.get_json()
    printer_name = data.get('printer_name')
    printer_id = data.get('printer_id')
    
    current_user.printer_name = printer_name
    if printer_id is not None:
        current_user.printer_id = printer_id
    db.session.commit()
    
    return jsonify({
        'success': True,
        'printer_name': printer_name,
        'printer_id': printer_id
    })


# ============================================
# SERVER-SIDE PRINT QUEUE API
# ============================================

@api_print.route('/api/pending-prints')
@login_required
def get_pending_prints():
    """Get all pending prints from server-side queue"""
    pending = branch_filter(PendingPrint.query, PendingPrint).filter_by(status='pending').order_by(PendingPrint.created_at).all()
    return jsonify({
        'success': True,
        'pending_prints': [p.to_dict() for p in pending],
        'count': len(pending)
    })


@api_print.route('/api/pending-prints', methods=['POST'])
@login_required
def add_pending_print():
    """Add a new pending print to server-side queue"""
    data = request.get_json()
    
    pending = PendingPrint(
        order_id=data.get('order_id'),
        receipt_data=json.dumps(data.get('receipt_data', [])),
        copies=data.get('copies', 3),
        current_copy=data.get('current_copy', 1),
        status='pending',
        branch_id=get_default_branch_id()
    )
    
    db.session.add(pending)
    db.session.commit()
    
    # Emit via WebSocket to connected printer clients
    emit_print_job(pending, pending.branch_id)
    
    return jsonify({
        'success': True,
        'pending_print': pending.to_dict()
    })


@api_print.route('/api/pending-prints/<int:print_id>/complete', methods=['POST'])
@login_required
def complete_pending_print(print_id):
    """Mark a pending print as completed"""
    pending = PendingPrint.query.get_or_404(print_id)
    pending.status = 'completed'
    pending.printed_at = utc_now()
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': 'Print marked as completed'
    })


@api_print.route('/api/pending-prints/<int:print_id>/fail', methods=['POST'])
@login_required
def fail_pending_print(print_id):
    """Mark a pending print as failed and increment retry count"""
    data = request.get_json() or {}
    pending = PendingPrint.query.get_or_404(print_id)
    
    pending.retry_count += 1
    pending.error_message = data.get('error_message', 'Unknown error')
    
    # Mark as permanently failed after 5 retries, otherwise reset to pending
    if pending.retry_count >= 5:
        pending.status = 'failed'
    else:
        pending.status = 'pending'
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'retry_count': pending.retry_count,
        'status': pending.status
    })


@api_print.route('/api/pending-prints/<int:print_id>', methods=['DELETE'])
@login_required
def delete_pending_print(print_id):
    """Delete a pending print from queue"""
    pending = PendingPrint.query.get_or_404(print_id)
    db.session.delete(pending)
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': 'Print deleted from queue'
    })


@api_print.route('/api/pending-prints/clear', methods=['POST'])
@login_required
def clear_pending_prints():
    """Clear all pending prints"""
    bid = get_user_branch_id()
    query = PendingPrint.query.filter_by(status='pending')
    if bid is not None:
        query = query.filter(PendingPrint.branch_id == bid)
    query.delete()
    db.session.commit()
    
    return jsonify({
        'success': True,
        'message': 'All pending prints cleared'
    })


# ============================================
# USB PRINTER ROUTES
# ============================================

@api_print.route('/api/usb-printer/status')
@login_required
def usb_printer_status():
    """Get USB printer status and availability"""
    if not USB_PRINTING_AVAILABLE:
        return jsonify({
            'success': True,
            'available': False,
            'connected': False,
            'message': 'USB printing not available (install python-escpos and pyusb)'
        })
    
    return jsonify({
        'success': True,
        'available': True,
        'connected': usb_printer.connected if usb_printer else False,
        'printer_info': usb_printer.printer_info if usb_printer else None
    })


@api_print.route('/api/usb-printer/devices')
@login_required
def list_usb_printers():
    """List available USB printers"""
    if not USB_PRINTING_AVAILABLE:
        return jsonify({
            'success': False,
            'message': 'USB printing not available',
            'devices': []
        })
    
    devices = USBPrinterManager.list_usb_devices()
    return jsonify({
        'success': True,
        'devices': devices
    })


@api_print.route('/api/usb-printer/connect', methods=['POST'])
@login_required
def connect_usb_printer():
    """Connect to USB printer"""
    if not USB_PRINTING_AVAILABLE:
        return jsonify({
            'success': False,
            'message': 'USB printing not available'
        })
    
    data = request.get_json() or {}
    vendor_id = data.get('vendor_id')
    product_id = data.get('product_id')
    
    success, message = usb_printer.connect(vendor_id, product_id)
    
    return jsonify({
        'success': success,
        'message': message,
        'connected': usb_printer.connected
    })


@api_print.route('/api/usb-printer/disconnect', methods=['POST'])
@login_required
def disconnect_usb_printer():
    """Disconnect USB printer"""
    if usb_printer:
        usb_printer.disconnect()
    
    return jsonify({
        'success': True,
        'message': 'Disconnected'
    })


@api_print.route('/api/usb-printer/test', methods=['POST'])
@login_required
def test_usb_print():
    """Test print on USB printer"""
    if not USB_PRINTING_AVAILABLE or not usb_printer:
        return jsonify({
            'success': False,
            'message': 'USB printer not available'
        })
    
    success, message = usb_printer.test_print()
    return jsonify({
        'success': success,
        'message': message
    })


@api_print.route('/api/usb-printer/print', methods=['POST'])
@login_required
def usb_print_receipt():
    """Print receipt on USB printer"""
    if not USB_PRINTING_AVAILABLE or not usb_printer:
        return jsonify({
            'success': False,
            'message': 'USB printer not available'
        })
    
    data = request.get_json()
    
    if 'order_data' in data:
        success, message = usb_printer.print_receipt(data['order_data'])
    elif 'raw_commands' in data:
        success, message = usb_printer.print_raw(data['raw_commands'])
    else:
        return jsonify({
            'success': False,
            'message': 'No print data provided'
        })
    
    return jsonify({
        'success': success,
        'message': message
    })


@api_print.route('/api/usb-printer/print-pending', methods=['POST'])
@login_required
def usb_print_pending():
    """Print all pending receipts using USB printer"""
    if not USB_PRINTING_AVAILABLE or not usb_printer or not usb_printer.connected:
        return jsonify({
            'success': False,
            'message': 'USB printer not connected'
        })
    
    pending = PendingPrint.query.filter_by(status='pending').order_by(PendingPrint.created_at).all()
    
    if not pending:
        return jsonify({
            'success': True,
            'message': 'No pending prints',
            'printed': 0
        })
    
    printed_count = 0
    failed_count = 0
    
    for item in pending:
        try:
            commands = json.loads(item.receipt_data)
            success, _ = usb_printer.print_raw(commands)
            
            if success:
                item.status = 'completed'
                item.printed_at = utc_now()
                printed_count += 1
            else:
                item.retry_count += 1
                if item.retry_count >= 5:
                    item.status = 'failed'
                failed_count += 1
                
        except Exception as e:
            item.retry_count += 1
            item.error_message = str(e)
            if item.retry_count >= 5:
                item.status = 'failed'
            failed_count += 1
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'printed': printed_count,
        'failed': failed_count,
        'message': f'{printed_count} receipts printed, {failed_count} failed'
    })
