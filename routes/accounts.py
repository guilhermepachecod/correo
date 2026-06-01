"""
routes/accounts.py
CRUD de contas de e-mail, teste de conexão, sync manual e sync de .conf.
"""
import threading
from pathlib import Path
from flask import Blueprint, request, jsonify, current_app

from database import db, Account, MAX_ACCOUNTS
from services.conf_manager import (safe_filename, write_conf,
                                    account_to_conf_data, sync_conf_to_db)

bp = Blueprint('accounts', __name__)

# Resolvido em tempo de request para evitar import circular
def _ms():
    from extensions import mail_service
    return mail_service

def _idle():
    from extensions import idle_manager
    return idle_manager

def _accounts_dir() -> Path:
    return Path(current_app.config['ACCOUNTS_DIR'])


@bp.get('/api/accounts')
def get_accounts():
    return jsonify([a.to_dict() for a in Account.query.all()])


@bp.post('/api/accounts')
def create_account():
    if Account.query.count() >= MAX_ACCOUNTS:
        return jsonify({'error': f'Limite de {MAX_ACCOUNTS} contas atingido'}), 400
    data = request.json
    for field in ['name', 'email', 'imap_host', 'imap_port', 'smtp_host', 'smtp_port',
                  'username', 'password']:
        if not data.get(field):
            return jsonify({'error': f'Campo obrigatório: {field}'}), 400
    try:
        base = safe_filename(data['name'])
        conf_filename = f'{base}.conf'
        counter = 1
        while (_accounts_dir() / conf_filename).exists():
            conf_filename = f'{base}_{counter}.conf'; counter += 1

        account = Account(
            name=data['name'], email=data['email'],
            imap_host=data['imap_host'], imap_port=int(data['imap_port']),
            imap_ssl=data.get('imap_ssl', True),
            smtp_host=data['smtp_host'], smtp_port=int(data['smtp_port']),
            smtp_ssl=data.get('smtp_ssl', False), smtp_tls=data.get('smtp_tls', True),
            username=data['username'], conf_file=conf_filename,
        )
        account.set_password(data['password'])
        db.session.add(account)
        db.session.commit()
        write_conf(_accounts_dir() / conf_filename, account_to_conf_data(account))
        im = _idle()
        if im: im.start_account(account.id)
        return jsonify(account.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@bp.put('/api/accounts/<int:account_id>')
def update_account(account_id):
    account = Account.query.get_or_404(account_id)
    data = request.json
    old_conf = account.conf_file
    for field in ['name', 'email', 'imap_host', 'imap_port', 'imap_ssl',
                  'smtp_host', 'smtp_port', 'smtp_ssl', 'smtp_tls', 'username']:
        if field in data:
            setattr(account, field, data[field])
    if data.get('password'):
        account.set_password(data['password'])
    new_conf = f'{safe_filename(account.name)}.conf'
    adir = _accounts_dir()
    if old_conf and old_conf != new_conf:
        old_path, new_path = adir / old_conf, adir / new_conf
        if old_path.exists() and not new_path.exists():
            old_path.rename(new_path)
            account.conf_file = new_conf
    elif not old_conf:
        account.conf_file = new_conf
    db.session.commit()
    write_conf(adir / account.conf_file, account_to_conf_data(account))
    im = _idle()
    if im: im.restart_account(account_id)
    return jsonify(account.to_dict())


@bp.delete('/api/accounts/<int:account_id>')
def delete_account(account_id):
    account = Account.query.get_or_404(account_id)
    im = _idle()
    if im: im.stop_account(account_id)
    if account.conf_file:
        p = _accounts_dir() / account.conf_file
        if p.exists(): p.unlink()
    db.session.delete(account)
    db.session.commit()
    return jsonify({'ok': True})


@bp.delete('/api/accounts/<int:account_id>/purge')
def purge_account(account_id):
    account = Account.query.get_or_404(account_id)
    im = _idle()
    if im: im.stop_account(account_id)
    for email in account.emails:
        for att in email.attachments:
            att.delete_from_disk()
    if account.conf_file:
        p = _accounts_dir() / account.conf_file
        if p.exists(): p.unlink()
    db.session.delete(account)
    db.session.commit()
    return jsonify({'ok': True})


@bp.post('/api/accounts/<int:account_id>/test')
def test_account(account_id):
    account = Account.query.get_or_404(account_id)
    return jsonify(_ms().test_connection(account))


@bp.post('/api/accounts/<int:account_id>/sync')
def sync_account(account_id):
    Account.query.get_or_404(account_id)
    app = current_app._get_current_object()
    def run():
        with app.app_context():
            _ms().sync_account(account_id)
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


@bp.post('/api/sync/all')
def sync_all():
    app = current_app._get_current_object()
    def run():
        with app.app_context():
            for account in Account.query.all():
                _ms().sync_account(account.id)
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


@bp.post('/api/accounts/sync-conf')
def sync_conf():
    from database import MAX_ACCOUNTS as _MAX
    result = sync_conf_to_db(
        accounts_dir=_accounts_dir(),
        db=db,
        Account=Account,
        MAX_ACCOUNTS=_MAX,
        idle_manager=_idle(),
    )
    return jsonify(result)
