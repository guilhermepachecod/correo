from flask import Flask, render_template, request, jsonify, send_file, abort
from flask_socketio import SocketIO, emit
import threading
import io
import logging
import os
import re
import configparser
from pathlib import Path

from database import db, Account, Email, Attachment
from mail_service import MailService

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'mailapp-secret-2024'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////app/database/mail.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

ACCOUNTS_DIR = Path('/app/accounts')
ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)

db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')
mail_service = MailService(db, socketio)

with app.app_context():
    db.create_all()
    # Migration: adiciona conf_file se não existir
    with db.engine.connect() as conn:
        try:
            conn.execute(db.text("ALTER TABLE accounts ADD COLUMN conf_file VARCHAR(255)"))
            conn.commit()
        except Exception:
            pass


# ── .conf helpers ──────────────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    return re.sub(r'[^\w\-]', '_', name).strip('_') or 'conta'


def _read_conf(path: Path) -> dict:
    data = {}
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, _, v = line.partition('=')
                data[k.strip().lower()] = v.strip()
    except Exception as e:
        log.error(f'Erro ao ler {path}: {e}')
    return data


def _write_conf(path: Path, data: dict):
    lines = []
    order = ['email', 'user', 'pass',
             'imap_host', 'imap_port', 'imap_ssl',
             'smtp_host', 'smtp_port', 'smtp_ssl', 'smtp_tls']
    for key in order:
        if key in data:
            lines.append(f'{key}={data[key]}')
    for key, val in data.items():
        if key not in order:
            lines.append(f'{key}={val}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _sync_conf_to_db():
    result = {'created': [], 'updated': [], 'deleted': [], 'errors': []}
    conf_files = list(ACCOUNTS_DIR.glob('*.conf'))
    conf_emails = set()

    for conf_path in conf_files:
        name = conf_path.stem
        data = _read_conf(conf_path)
        email = data.get('email', '').strip()
        if not email:
            result['errors'].append(f'{conf_path.name}: campo "email" ausente')
            continue

        conf_emails.add(email.lower())
        username = data.get('user', email)
        raw_pass = data.get('pass', '')
        imap_host = data.get('imap_host', '')
        imap_port = int(data.get('imap_port', 993))
        imap_ssl = data.get('imap_ssl', 'true').lower() in ('true', '1', 'yes')
        smtp_host = data.get('smtp_host', '')
        smtp_port = int(data.get('smtp_port', 587))
        smtp_ssl = data.get('smtp_ssl', 'false').lower() in ('true', '1', 'yes')
        smtp_tls = data.get('smtp_tls', 'true').lower() in ('true', '1', 'yes')

        if not imap_host or not smtp_host:
            result['errors'].append(f'{conf_path.name}: imap_host ou smtp_host ausente')
            continue

        account = Account.query.filter_by(email=email).first()

        if account:
            account.name = name
            account.username = username
            account.imap_host = imap_host
            account.imap_port = imap_port
            account.imap_ssl = imap_ssl
            account.smtp_host = smtp_host
            account.smtp_port = smtp_port
            account.smtp_ssl = smtp_ssl
            account.smtp_tls = smtp_tls
            account.conf_file = conf_path.name
            if raw_pass and not raw_pass.startswith('enc:'):
                account.set_password(raw_pass)
            db.session.commit()
            result['updated'].append(email)
        else:
            if not raw_pass:
                result['errors'].append(f'{conf_path.name}: campo "pass" ausente para nova conta')
                continue
            account = Account(
                name=name, email=email, username=username,
                imap_host=imap_host, imap_port=imap_port, imap_ssl=imap_ssl,
                smtp_host=smtp_host, smtp_port=smtp_port, smtp_ssl=smtp_ssl,
                smtp_tls=smtp_tls, conf_file=conf_path.name,
            )
            account.set_password(raw_pass)
            db.session.add(account)
            db.session.commit()
            result['created'].append(email)

        if raw_pass and not raw_pass.startswith('enc:'):
            encrypted = account.password_enc
            data['pass'] = f'enc:{encrypted}'
            _write_conf(conf_path, data)

    all_accounts = Account.query.all()
    for account in all_accounts:
        if account.conf_file and account.email.lower() not in conf_emails:
            conf_path = ACCOUNTS_DIR / account.conf_file
            if not conf_path.exists():
                db.session.delete(account)
                db.session.commit()
                result['deleted'].append(account.email)

    return result


def _account_to_conf_data(account: Account) -> dict:
    return {
        'email': account.email,
        'user': account.username,
        'pass': f'enc:{account.password_enc}',
        'imap_host': account.imap_host,
        'imap_port': str(account.imap_port),
        'imap_ssl': 'true' if account.imap_ssl else 'false',
        'smtp_host': account.smtp_host,
        'smtp_port': str(account.smtp_port),
        'smtp_ssl': 'true' if account.smtp_ssl else 'false',
        'smtp_tls': 'true' if account.smtp_tls else 'false',
    }


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── Accounts ───────────────────────────────────────────────────────────────────

@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    return jsonify([a.to_dict() for a in Account.query.all()])


@app.route('/api/accounts', methods=['POST'])
def create_account():
    data = request.json
    required = ['name', 'email', 'imap_host', 'imap_port', 'smtp_host', 'smtp_port', 'username', 'password']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'Campo obrigatório: {field}'}), 400
    try:
        conf_filename = f"{_safe_filename(data['name'])}.conf"
        base = _safe_filename(data['name'])
        counter = 1
        while (ACCOUNTS_DIR / conf_filename).exists():
            conf_filename = f'{base}_{counter}.conf'
            counter += 1

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
        _write_conf(ACCOUNTS_DIR / conf_filename, _account_to_conf_data(account))
        return jsonify(account.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/accounts/<int:account_id>', methods=['PUT'])
def update_account(account_id):
    account = Account.query.get_or_404(account_id)
    data = request.json
    old_conf_file = account.conf_file
    for field in ['name', 'email', 'imap_host', 'imap_port', 'imap_ssl',
                  'smtp_host', 'smtp_port', 'smtp_ssl', 'smtp_tls', 'username']:
        if field in data:
            setattr(account, field, data[field])
    if data.get('password'):
        account.set_password(data['password'])
    new_conf_filename = f"{_safe_filename(account.name)}.conf"
    if old_conf_file and old_conf_file != new_conf_filename:
        old_path = ACCOUNTS_DIR / old_conf_file
        new_path = ACCOUNTS_DIR / new_conf_filename
        if old_path.exists() and not new_path.exists():
            old_path.rename(new_path)
            account.conf_file = new_conf_filename
    elif not old_conf_file:
        account.conf_file = new_conf_filename
    db.session.commit()
    _write_conf(ACCOUNTS_DIR / account.conf_file, _account_to_conf_data(account))
    return jsonify(account.to_dict())


@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
def delete_account(account_id):
    account = Account.query.get_or_404(account_id)
    if account.conf_file:
        conf_path = ACCOUNTS_DIR / account.conf_file
        if conf_path.exists():
            conf_path.unlink()
    db.session.delete(account)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/accounts/<int:account_id>/purge', methods=['DELETE'])
def purge_account(account_id):
    account = Account.query.get_or_404(account_id)
    if account.conf_file:
        conf_path = ACCOUNTS_DIR / account.conf_file
        if conf_path.exists():
            conf_path.unlink()
    db.session.delete(account)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/accounts/<int:account_id>/test', methods=['POST'])
def test_account(account_id):
    account = Account.query.get_or_404(account_id)
    return jsonify(mail_service.test_connection(account))


@app.route('/api/accounts/<int:account_id>/sync', methods=['POST'])
def sync_account(account_id):
    account = Account.query.get_or_404(account_id)
    def run():
        with app.app_context():
            mail_service.sync_account(account_id)
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/sync/all', methods=['POST'])
def sync_all():
    def run():
        with app.app_context():
            for account in Account.query.all():
                mail_service.sync_account(account.id)
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/accounts/sync-conf', methods=['POST'])
def sync_conf():
    return jsonify(_sync_conf_to_db())


# ── Emails ─────────────────────────────────────────────────────────────────────

@app.route('/api/emails')
def get_emails():
    account_id = request.args.get('account_id', type=int)
    folder = request.args.get('folder', 'INBOX')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    search = request.args.get('search', '').strip()

    query = Email.query
    if account_id:
        query = query.filter_by(account_id=account_id)
    if folder:
        query = query.filter_by(folder=folder)
    if search:
        like = f'%{search}%'
        query = query.filter(
            db.or_(Email.subject.ilike(like), Email.from_addr.ilike(like),
                   Email.body_text.ilike(like))
        )
    query = query.order_by(Email.date.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return jsonify({
        'emails': [e.to_list_dict() for e in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'page': page,
    })


@app.route('/api/emails/<int:email_id>')
def get_email(email_id):
    email = Email.query.get_or_404(email_id)
    if not email.is_read:
        email.is_read = True
        db.session.commit()
        mail_service.mark_read_on_server(email)
    return jsonify(email.to_dict())


@app.route('/api/emails/<int:email_id>/star', methods=['POST'])
def toggle_star(email_id):
    email = Email.query.get_or_404(email_id)
    email.is_starred = not email.is_starred
    db.session.commit()
    return jsonify({'starred': email.is_starred})


@app.route('/api/emails/<int:email_id>/read', methods=['POST'])
def set_read(email_id):
    email = Email.query.get_or_404(email_id)
    is_read = request.json.get('read', True)
    email.is_read = is_read
    db.session.commit()
    if is_read:
        mail_service.mark_read_on_server(email)
    else:
        mail_service.mark_unread_on_server(email)
    return jsonify({'read': email.is_read})


@app.route('/api/emails/<int:email_id>/spam', methods=['POST'])
def set_spam(email_id):
    email = Email.query.get_or_404(email_id)
    mark_as_spam = request.json.get('spam', True)
    result = mail_service.set_spam(email, mark_as_spam)
    if result:
        db.session.refresh(email)
    return jsonify({'ok': result, 'folder': email.folder})


@app.route('/api/emails/<int:email_id>', methods=['DELETE'])
def delete_email(email_id):
    email = Email.query.get_or_404(email_id)
    mail_service.delete_email(email)
    db.session.delete(email)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/emails/send', methods=['POST'])
def send_email():
    account_id = request.form.get('account_id', type=int)
    if not account_id:
        return jsonify({'error': 'account_id obrigatório'}), 400
    account = Account.query.get_or_404(account_id)
    files = request.files.getlist('attachments')
    result = mail_service.send_email(
        account=account,
        to=request.form.get('to', ''),
        cc=request.form.get('cc', ''),
        bcc=request.form.get('bcc', ''),
        subject=request.form.get('subject', ''),
        body=request.form.get('body', ''),
        reply_to_id=request.form.get('reply_to_id', type=int),
        attachments=files,
    )
    return jsonify(result)


# ── Bulk actions ───────────────────────────────────────────────────────────────

@app.route('/api/emails/bulk', methods=['POST'])
def bulk_action():
    data = request.json
    action = data.get('action')
    ids = data.get('ids', [])          # lista de IDs, vazio = todos da pasta
    folder = data.get('folder', 'INBOX')
    account_id = data.get('account_id')

    if ids:
        emails = Email.query.filter(Email.id.in_(ids)).all()
    else:
        # todos da pasta
        query = Email.query.filter_by(folder=folder)
        if account_id:
            query = query.filter_by(account_id=account_id)
        emails = query.all()

    count = 0
    for email in emails:
        try:
            if action == 'mark_read':
                if not email.is_read:
                    email.is_read = True
                    mail_service.mark_read_on_server(email)
            elif action == 'mark_unread':
                if email.is_read:
                    email.is_read = False
                    mail_service.mark_unread_on_server(email)
            elif action == 'mark_spam':
                mail_service.set_spam(email, True)
            elif action == 'mark_not_spam':
                mail_service.set_spam(email, False)
            elif action == 'delete':
                mail_service.delete_email(email)
                db.session.delete(email)
            count += 1
        except Exception as e:
            log.warning(f'bulk_action {action} error on email {email.id}: {e}')

    db.session.commit()
    return jsonify({'ok': True, 'count': count})


@app.route('/api/emails/<int:email_id>/folders', methods=['POST'])
def move_email(email_id):
    email = Email.query.get_or_404(email_id)
    folder = request.json.get('folder')
    if not folder:
        return jsonify({'error': 'folder obrigatório'}), 400
    mail_service.move_email(email, folder)
    return jsonify({'ok': True})


# ── Folders ────────────────────────────────────────────────────────────────────

@app.route('/api/accounts/<int:account_id>/folders')
def get_folders(account_id):
    account = Account.query.get_or_404(account_id)
    return jsonify(mail_service.get_folders(account))


@app.route('/api/folders/counts')
def folder_counts():
    account_id = request.args.get('account_id', type=int)
    query = db.session.query(
        Email.folder,
        db.func.count(Email.id).label('total'),
        db.func.sum(db.case((Email.is_read == False, 1), else_=0)).label('unread')
    )
    if account_id:
        query = query.filter(Email.account_id == account_id)
    query = query.group_by(Email.folder)
    result = {}
    for row in query.all():
        result[row.folder] = {'total': row.total, 'unread': int(row.unread or 0)}
    return jsonify(result)


# ── Attachments ────────────────────────────────────────────────────────────────

@app.route('/api/attachments/<int:attachment_id>')
def download_attachment(attachment_id):
    att = Attachment.query.get_or_404(attachment_id)
    return send_file(
        io.BytesIO(att.data),
        download_name=att.filename,
        mimetype=att.content_type,
        as_attachment=True,
    )


# ── WebSocket ──────────────────────────────────────────────────────────────────

@socketio.on('connect')
def on_connect():
    log.info('Cliente conectado via WebSocket')


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=15002, debug=False, allow_unsafe_werkzeug=True)