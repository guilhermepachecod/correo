"""
routes/folders.py
Listagem de pastas IMAP e contagem de e-mails por pasta.
"""
from flask import Blueprint, request, jsonify

from database import db, Account, Email

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
    return jsonify(result)
