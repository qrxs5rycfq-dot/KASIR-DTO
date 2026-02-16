from flask_socketio import emit, join_room
from extensions import socketio
from models import db, PendingPrint
from utils import utc_now


def emit_print_job(pending_print, branch_id=None):
    """Emit a new print job via WebSocket to all connected printer clients.
    Called after PendingPrint records are created."""
    data = pending_print.to_dict()
    room = f'branch_{branch_id}' if branch_id else None
    socketio.emit('new_print_job', data, room=room, namespace='/')
    # Also broadcast to 'all' room for owner/admin devices
    socketio.emit('new_print_job', data, room='branch_all', namespace='/')


def register_socket_handlers(socketio):
    """Register all WebSocket event handlers on the given SocketIO instance."""

    @socketio.on('connect')
    def handle_ws_connect():
        """Handle WebSocket connection from printer clients"""
        pass

    @socketio.on('join_branch')
    def handle_join_branch(data):
        """Join a branch-specific room for targeted print events"""
        branch_id = data.get('branch_id', 'all')
        join_room(f'branch_{branch_id}')
        emit('joined', {'branch_id': branch_id, 'message': 'Connected to print channel'})

        # Kirim pending prints ke client yang baru terhubung
        try:
            query = PendingPrint.query.filter_by(status='pending').order_by(PendingPrint.created_at)
            if branch_id != 'all':
                query = query.filter_by(branch_id=branch_id)
            pending_prints = query.limit(50).all()
            for p in pending_prints:
                emit('new_print_job', p.to_dict())
        except Exception as e:
            print(f"Error sending pending prints on join: {e}")

    @socketio.on('printer_status')
    def handle_printer_status(data):
        """Receive printer status updates from Android/browser clients"""
        emit('printer_status_update', data, broadcast=True)

    @socketio.on('print_complete')
    def handle_print_complete(data):
        """Handle print completion from Android/browser client"""
        print_id = data.get('print_id')
        if print_id:
            pending = db.session.get(PendingPrint, int(print_id))
            if pending:
                pending.status = 'completed'
                pending.printed_at = utc_now()
                db.session.commit()
                emit('print_status', {'print_id': print_id, 'status': 'completed'}, broadcast=True)

    @socketio.on('print_failed')
    def handle_print_failed(data):
        """Handle print failure from Android/browser client"""
        print_id = data.get('print_id')
        error = data.get('error', 'Unknown error')
        if print_id:
            pending = db.session.get(PendingPrint, int(print_id))
            if pending:
                pending.retry_count += 1
                pending.error_message = error
                if pending.retry_count >= 5:
                    pending.status = 'failed'
                else:
                    pending.status = 'pending'
                db.session.commit()
                emit('print_status', {'print_id': print_id, 'status': pending.status, 'retry_count': pending.retry_count}, broadcast=True)

    @socketio.on('print_job_received')
    def handle_print_job_received(data):
        """Handle acknowledgment that print job was received by client"""
        print_id = data.get('print_id')
        if print_id:
            pending = db.session.get(PendingPrint, int(print_id))
            if pending and pending.status == 'pending':
                pending.status = 'processing'
                db.session.commit()
