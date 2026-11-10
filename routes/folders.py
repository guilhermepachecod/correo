"""
routes/folders.py
Listagem de pastas IMAP, contagem de e-mails por pasta e sync incremental
avulso de uma pasta (disparado ao abrir ela na UI — Fase 2.5 pendente).
"""
import threading

from flask import Blueprint, request, jsonify, current_app

from database import db, Account, Email, FolderState

bp = Blueprint('folders', __name__)

def _ms():
    from extensions import mail_service
    return mail_service


@bp.get('/api/accounts/<int:account_id>/folders')
def get_folders(account_id):
    """Lista as pastas IMAP da conta com o role detectado (SPECIAL-USE ou
    nome — ver services/special_folders.py). Não consumido pelo frontend
    hoje (static/js/filters.js usa /api/folders/counts, que é local)."""
    account = Account.query.get_or_404(account_id)
    roles = _ms().refresh_folder_roles(account)
    folders = _ms().get_folders(account)
    return jsonify([{'name': f, 'role': roles.get(f)} for f in folders])


@bp.get('/api/folders/counts')
def folder_counts():
    """Contagem por pasta — só lê o banco, nunca toca o IMAP. Inclui toda
    pasta conhecida via folder_state (Fase 2.3), mesmo com 0 e-mails em
    cache, pra pasta não 'desaparecer' da sidebar quando o cache local
    esvazia (ex.: lixeira limpa) — era isso que causava Lixeira
    aparecendo/sumindo a cada sync."""
    account_id = request.args.get('account_id', type=int)
    q = db.session.query(
        Email.folder,
        db.func.count(Email.id).label('total'),
        db.func.sum(db.case((Email.is_read == False, 1), else_=0)).label('unread'),
    )
    if account_id:
        q = q.filter(Email.account_id == account_id)
    q = q.group_by(Email.folder)
    result = {
        row.folder: {'total': row.total, 'unread': int(row.unread or 0)}
        for row in q.all()
    }

    fs_q = FolderState.query
    if account_id:
        fs_q = fs_q.filter_by(account_id=account_id)
    for fs in fs_q.all():
        result.setdefault(fs.folder, {'total': 0, 'unread': 0})

    return jsonify(result)


@bp.post('/api/accounts/<int:account_id>/folders/<path:folder>/sync')
def sync_folder(account_id, folder):
    """Sync incremental avulso de uma pasta — disparado quando o usuário
    abre ela na UI, pra não depender só do ciclo de poll (Fase 2.5)."""
    Account.query.get_or_404(account_id)
    app = current_app._get_current_object()
    ms = _ms()

    def run():
        with app.app_context():
            account = Account.query.get(account_id)
            if not account:
                return
            ms._emit('sync_start', {'account_id': account_id, 'email': account.email})
            try:
                result = ms.sync_folder_incremental(account, folder)
            except Exception as e:
                ms._emit('sync_error', {'account_id': account_id, 'error': str(e)})
                return
            if not result.get('ok'):
                ms._emit('sync_error', {'account_id': account_id, 'error': result.get('error', 'erro desconhecido')})
                return
            ms._emit('sync_done', {
                'account_id': account_id, 'email': account.email,
                'new': result.get('new', 0), 'flag_changes': result.get('flag_changes', 0),
            })

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})
