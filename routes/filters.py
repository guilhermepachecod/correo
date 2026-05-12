"""
routes/filters.py
CRUD de filtros automáticos de e-mail por conta.
"""
from flask import Blueprint, request, jsonify

from database import db, Account, EmailFilter

bp = Blueprint('filters', __name__)


@bp.get('/api/accounts/<int:account_id>/filters')
def get_filters(account_id):
    Account.query.get_or_404(account_id)
    filters = (EmailFilter.query.filter_by(account_id=account_id)
                                .order_by(EmailFilter.priority.asc()).all())
    return jsonify([f.to_dict() for f in filters])


@bp.post('/api/accounts/<int:account_id>/filters')
def create_filter(account_id):
    Account.query.get_or_404(account_id)
    data = request.json
    if not data.get('name'):
        return jsonify({'error': 'name obrigatório'}), 400
    f = EmailFilter(
        account_id=account_id,
        name=data['name'],
        enabled=data.get('enabled', True),
        priority=data.get('priority', 0),
        cond_from=data.get('cond_from') or None,
        cond_to=data.get('cond_to') or None,
        cond_subject=data.get('cond_subject') or None,
        cond_has_attach=data.get('cond_has_attach') if data.get('cond_has_attach') is not None else None,
        action_move_to=data.get('action_move_to') or None,
        action_mark_read=data.get('action_mark_read', False),
        action_star=data.get('action_star', False),
        action_delete=data.get('action_delete', False),
    )
    db.session.add(f)
    db.session.commit()
    return jsonify(f.to_dict()), 201


@bp.put('/api/filters/<int:filter_id>')
def update_filter(filter_id):
    f    = EmailFilter.query.get_or_404(filter_id)
    data = request.json
    NULLABLE_STR = {'cond_from', 'cond_to', 'cond_subject', 'action_move_to'}
    for field in ['name', 'enabled', 'priority', 'cond_from', 'cond_to',
                  'cond_subject', 'cond_has_attach', 'action_move_to',
                  'action_mark_read', 'action_star', 'action_delete']:
        if field not in data:
            continue
        val = data[field]
        if field in NULLABLE_STR:
            val = val or None
        elif field == 'cond_has_attach':
            val = val if val is not None else None
        setattr(f, field, val)
    db.session.commit()
    return jsonify(f.to_dict())


@bp.delete('/api/filters/<int:filter_id>')
def delete_filter(filter_id):
    f = EmailFilter.query.get_or_404(filter_id)
    db.session.delete(f)
    db.session.commit()
    return jsonify({'ok': True})
