"""
routes/idle.py
Endpoints de status e controle do IMAP IDLE manager.
"""
from flask import Blueprint, jsonify

from database import Account

bp = Blueprint('idle', __name__)

def _idle():
    from extensions import idle_manager
    return idle_manager


@bp.get('/api/idle/status')
def idle_status():
    im = _idle()
    if not im:
        return jsonify({'status': 'starting', 'accounts': {}})
    result = {}
    for account in Account.query.all():
        t = im._threads.get(account.id)
        result[str(account.id)] = {
            'email':   account.email,
            'running': t is not None and t.is_alive(),
        }
    return jsonify({'status': 'running', 'accounts': result})


@bp.post('/api/accounts/<int:account_id>/idle-restart')
def restart_idle(account_id):
    Account.query.get_or_404(account_id)
    im = _idle()
    if im:
        im.restart_account(account_id)
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'IDLE não iniciado ainda'})
