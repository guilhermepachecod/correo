from flask import Flask, render_template, request, jsonify, send_file, abort
from flask_socketio import SocketIO, emit
import threading
import io
import logging

from database import db, Account, Email, Attachment
from mail_service import MailService

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'mailapp-secret-2024'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////app/database/mail.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

db.init_app(app)
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')
mail_service = MailService(db, socketio)

with app.app_context():
    db.create_all()


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


# ── Accounts ───────────────────────────────────────────────────────────────────

@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    accounts = Account.query.all()
    return jsonify([a.to_dict() for a in accounts])


@app.route('/api/accounts', methods=['POST'])
def create_account():
    data = request.json
    required = ['name', 'email', 'imap_host', 'imap_port', 'smtp_host', 'smtp_port', 'username', 'password']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'Campo obrigatório: {field}'}), 400
    try:
        account = Account(
            name=data['name'],
            email=data['email'],
            imap_host=data['imap_host'],
            imap_port=int(data['imap_port']),
            imap_ssl=data.get('imap_ssl', True),
            smtp_host=data['smtp_host'],
            smtp_port=int(data['smtp_port']),
            smtp_ssl=data.get('smtp_ssl', True),
            smtp_tls=data.get('smtp_tls', False),
            username=data['username'],
        )
        account.set_password(data['password'])
        db.session.add(account)
        db.session.commit()
        return jsonify(account.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/accounts/<int:account_id>', methods=['PUT'])
def update_account(account_id):
    account = Account.query.get_or_404(account_id)
    data = request.json
    for field in ['name', 'email', 'imap_host', 'imap_port', 'imap_ssl',
                  'smtp_host', 'smtp_port', 'smtp_ssl', 'smtp_tls', 'username']:
        if field in data:
            setattr(account, field, data[field])
    if data.get('password'):
        account.set_password(data['password'])
    db.session.commit()
    return jsonify(account.to_dict())


@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
def delete_account(account_id):
    account = Account.query.get_or_404(account_id)
    db.session.delete(account)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/accounts/<int:account_id>/test', methods=['POST'])
def test_account(account_id):
    account = Account.query.get_or_404(account_id)
    result = mail_service.test_connection(account)
    return jsonify(result)


@app.route('/api/accounts/<int:account_id>/sync', methods=['POST'])
def sync_account(account_id):
    account = Account.query.get_or_404(account_id)
    def run():
        with app.app_context():
            mail_service.sync_account(account_id)
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True, 'message': 'Sincronização iniciada'})


@app.route('/api/sync/all', methods=['POST'])
def sync_all():
    def run():
        with app.app_context():
            accounts = Account.query.all()
            for account in accounts:
                mail_service.sync_account(account.id)
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


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
    folders = mail_service.get_folders(account)
    return jsonify(folders)


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