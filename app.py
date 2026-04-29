from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO
import threading
import io
import logging
import os
import re
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
    with db.engine.connect() as conn:
        try:
            conn.execute(db.text("ALTER TABLE accounts ADD COLUMN conf_file VARCHAR(255)"))
            conn.commit()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------

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
        log.error(e)
    return data


def _write_conf(path: Path, data: dict):
    lines = []
    for k, v in data.items():
        lines.append(f'{k}={v}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _account_to_conf_data(account):
    return {
        'email': account.email,
        'user': account.username,
        'pass': f'enc:{account.password_enc}',
        'imap_host': account.imap_host,
        'imap_port': account.imap_port,
        'imap_ssl': str(account.imap_ssl).lower(),
        'smtp_host': account.smtp_host,
        'smtp_port': account.smtp_port,
        'smtp_ssl': str(account.smtp_ssl).lower(),
        'smtp_tls': str(account.smtp_tls).lower(),
    }


# -----------------------------------------------------------------------------
# PAGE
# -----------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


# -----------------------------------------------------------------------------
# ACCOUNTS
# -----------------------------------------------------------------------------

@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    return jsonify([a.to_dict() for a in Account.query.all()])


@app.route('/api/accounts', methods=['POST'])
def create_account():
    data = request.json

    account = Account(
        name=data['name'],
        email=data['email'],
        username=data['username'],
        imap_host=data['imap_host'],
        imap_port=int(data['imap_port']),
        imap_ssl=data.get('imap_ssl', True),
        smtp_host=data['smtp_host'],
        smtp_port=int(data['smtp_port']),
        smtp_ssl=data.get('smtp_ssl', False),
        smtp_tls=data.get('smtp_tls', True),
    )

    account.set_password(data['password'])

    conf_file = f"{_safe_filename(account.name)}.conf"
    account.conf_file = conf_file

    db.session.add(account)
    db.session.commit()

    _write_conf(ACCOUNTS_DIR / conf_file, _account_to_conf_data(account))

    return jsonify(account.to_dict())


@app.route('/api/accounts/<int:account_id>', methods=['PUT'])
def update_account(account_id):
    account = Account.query.get_or_404(account_id)
    data = request.json

    fields = [
        'name', 'email', 'username',
        'imap_host', 'imap_port', 'imap_ssl',
        'smtp_host', 'smtp_port', 'smtp_ssl', 'smtp_tls'
    ]

    for field in fields:
        if field in data:
            setattr(account, field, data[field])

    if data.get('password'):
        account.set_password(data['password'])

    if not account.conf_file:
        account.conf_file = f"{_safe_filename(account.name)}.conf"

    db.session.commit()

    _write_conf(
        ACCOUNTS_DIR / account.conf_file,
        _account_to_conf_data(account)
    )

    return jsonify(account.to_dict())


@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
def delete_account(account_id):
    account = Account.query.get_or_404(account_id)

    if account.conf_file:
        fp = ACCOUNTS_DIR / account.conf_file
        if fp.exists():
            fp.unlink()

    db.session.delete(account)
    db.session.commit()

    return jsonify({'ok': True})


@app.route('/api/accounts/<int:account_id>/purge', methods=['DELETE'])
def purge_account(account_id):
    account = Account.query.get_or_404(account_id)

    Email.query.filter_by(account_id=account.id).delete()

    if account.conf_file:
        fp = ACCOUNTS_DIR / account.conf_file
        if fp.exists():
            fp.unlink()

    db.session.delete(account)
    db.session.commit()

    return jsonify({'ok': True})


@app.route('/api/accounts/<int:account_id>/test', methods=['POST'])
def test_account(account_id):
    account = Account.query.get_or_404(account_id)
    return jsonify(mail_service.test_connection(account))


@app.route('/api/accounts/<int:account_id>/sync', methods=['POST'])
def sync_account(account_id):
    def run():
        with app.app_context():
            mail_service.sync_account(account_id)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/sync/all', methods=['POST'])
def sync_all():
    def run():
        with app.app_context():
            for acc in Account.query.all():
                mail_service.sync_account(acc.id)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


# -----------------------------------------------------------------------------
# EMAILS
# -----------------------------------------------------------------------------

@app.route('/api/emails')
def get_emails():
    account_id = request.args.get('account_id', type=int)
    folder = request.args.get('folder', 'INBOX')
    search = request.args.get('search', '').strip()

    query = Email.query

    if account_id:
        query = query.filter_by(account_id=account_id)

    if folder:
        query = query.filter_by(folder=folder)

    if search:
        like = f'%{search}%'
        query = query.filter(
            db.or_(
                Email.subject.ilike(like),
                Email.from_addr.ilike(like),
                Email.body_text.ilike(like)
            )
        )

    query = query.order_by(Email.date.desc())

    emails = query.limit(500).all()

    return jsonify({
        'emails': [e.to_list_dict() for e in emails],
        'total': len(emails)
    })


@app.route('/api/emails/<int:email_id>')
def get_email(email_id):
    email = Email.query.get_or_404(email_id)

    if not email.is_read:
        email.is_read = True
        db.session.commit()

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
    val = request.json.get('read', True)
    email.is_read = val
    db.session.commit()
    return jsonify({'read': val})


@app.route('/api/emails/<int:email_id>/spam', methods=['POST'])
def set_spam(email_id):
    email = Email.query.get_or_404(email_id)
    spam = request.json.get('spam', True)

    email.folder = 'Spam' if spam else 'INBOX'
    db.session.commit()

    return jsonify({'ok': True})


@app.route('/api/emails/<int:email_id>', methods=['DELETE'])
def delete_email(email_id):
    email = Email.query.get_or_404(email_id)

    db.session.delete(email)
    db.session.commit()

    return jsonify({'ok': True})


@app.route('/api/emails/send', methods=['POST'])
def send_email():
    account_id = request.form.get('account_id', type=int)
    account = Account.query.get_or_404(account_id)

    files = request.files.getlist('attachments')

    result = mail_service.send_email(
        account=account,
        to=request.form.get('to', ''),
        cc=request.form.get('cc', ''),
        bcc=request.form.get('bcc', ''),
        subject=request.form.get('subject', ''),
        body=request.form.get('body', ''),
        attachments=files
    )

    return jsonify(result)


# -----------------------------------------------------------------------------
# BULK ACTIONS
# -----------------------------------------------------------------------------

@app.route('/api/emails/bulk', methods=['POST'])
def bulk_action():
    data = request.json

    action = data.get('action')
    ids = data.get('ids', [])
    folder = data.get('folder', 'INBOX')
    account_id = data.get('account_id')

    query = Email.query

    if ids:
        query = query.filter(Email.id.in_(ids))
    else:
        query = query.filter_by(folder=folder)
        if account_id:
            query = query.filter_by(account_id=account_id)

    emails = query.all()
    count = 0

    for email in emails:
        if action == 'delete':
            db.session.delete(email)

        elif action == 'mark_read':
            email.is_read = True

        elif action == 'mark_unread':
            email.is_read = False

        elif action == 'mark_spam':
            email.folder = 'Spam'

        elif action == 'mark_not_spam':
            email.folder = 'INBOX'

        count += 1

    db.session.commit()

    return jsonify({
        'ok': True,
        'count': count
    })


# -----------------------------------------------------------------------------
# FOLDERS
# -----------------------------------------------------------------------------

@app.route('/api/folders/counts')
def folder_counts():
    account_id = request.args.get('account_id', type=int)

    query = Email.query

    if account_id:
        query = query.filter_by(account_id=account_id)

    rows = query.all()

    result = {}

    for e in rows:
        if e.folder not in result:
            result[e.folder] = {'total': 0, 'unread': 0}

        result[e.folder]['total'] += 1

        if not e.is_read:
            result[e.folder]['unread'] += 1

    return jsonify(result)


# -----------------------------------------------------------------------------
# ATTACHMENTS
# -----------------------------------------------------------------------------

@app.route('/api/attachments/<int:attachment_id>')
def download_attachment(attachment_id):
    att = Attachment.query.get_or_404(attachment_id)

    return send_file(
        io.BytesIO(att.data),
        download_name=att.filename,
        mimetype=att.content_type,
        as_attachment=True
    )


# -----------------------------------------------------------------------------
# SOCKET
# -----------------------------------------------------------------------------

@socketio.on('connect')
def on_connect():
    log.info('cliente conectado')


# -----------------------------------------------------------------------------
# RUN
# -----------------------------------------------------------------------------

if __name__ == '__main__':
    socketio.run(
        app,
        host='0.0.0.0',
        port=15002,
        debug=False,
        allow_unsafe_werkzeug=True
    )