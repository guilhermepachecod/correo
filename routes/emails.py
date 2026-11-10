"""
routes/emails.py
Operações sobre e-mails: listagem, leitura, flags, spam,
exclusão, envio, ações em massa, mover e threads.
"""
import io
import threading
from flask import Blueprint, request, jsonify, send_file, abort, current_app

from database import db, Account, Email, Attachment, enforce_email_limit
from services.sanitize import sanitize_email_html

bp = Blueprint('emails', __name__)

def _ms():
    from extensions import mail_service
    return mail_service


def _sanitized_dict(email: Email) -> dict:
    d = email.to_dict()
    d['body_html'] = sanitize_email_html(d.get('body_html') or '')
    return d


# ── Listagem ──────────────────────────────────────────────────────────────────

@bp.get('/api/emails')
def get_emails():
    account_id = request.args.get('account_id', type=int)
    folder     = request.args.get('folder', 'INBOX')
    page       = request.args.get('page', 1, type=int)
    per_page   = request.args.get('per_page', 50, type=int)
    search     = request.args.get('search', '').strip()
    thread_id  = request.args.get('thread_id', '').strip()

    q = Email.query
    if account_id: q = q.filter_by(account_id=account_id)
    if folder:     q = q.filter_by(folder=folder)
    if thread_id:  q = q.filter_by(thread_id=thread_id)
    if search:
        like = f'%{search}%'
        q = q.filter(db.or_(
            Email.subject.ilike(like),
            Email.from_addr.ilike(like),
            Email.body_text.ilike(like),
        ))
    q = q.order_by(Email.date.desc())
    pag = q.paginate(page=page, per_page=per_page, error_out=False)
    return jsonify({
        'emails': [e.to_list_dict() for e in pag.items],
        'total':  pag.total,
        'pages':  pag.pages,
        'page':   page,
    })


@bp.get('/api/emails/<int:email_id>')
def get_email(email_id):
    email = Email.query.get_or_404(email_id)
    if not email.is_read:
        account = email.account  # carrega antes do commit para evitar DetachedInstanceError
        email.is_read = True
        db.session.commit()
        _ms().mark_read_on_server(email, account)
    return jsonify(_sanitized_dict(email))


# ── Flags ─────────────────────────────────────────────────────────────────────

@bp.post('/api/emails/<int:email_id>/star')
def toggle_star(email_id):
    email = Email.query.get_or_404(email_id)
    email.is_starred = not email.is_starred
    db.session.commit()
    return jsonify({'starred': email.is_starred})


@bp.post('/api/emails/<int:email_id>/read')
def set_read(email_id):
    email   = Email.query.get_or_404(email_id)
    account = email.account  # carrega antes do commit
    is_read = request.json.get('read', True)
    email.is_read = is_read
    db.session.commit()
    if is_read:
        _ms().mark_read_on_server(email, account)
    else:
        _ms().mark_unread_on_server(email, account)
    return jsonify({'read': email.is_read})


@bp.post('/api/emails/<int:email_id>/spam')
def set_spam(email_id):
    email        = Email.query.get_or_404(email_id)
    account      = email.account
    mark_as_spam = request.json.get('spam', True)
    result       = _ms().set_spam(email, mark_as_spam, account)
    if result:
        db.session.refresh(email)
    return jsonify({'ok': result, 'folder': email.folder})


# ── Delete / Move ─────────────────────────────────────────────────────────────

@bp.delete('/api/emails/<int:email_id>')
def delete_email(email_id):
    email   = Email.query.get_or_404(email_id)
    account = email.account  # carrega antes de qualquer delete/commit
    _ms().delete_email(email, account)
    for att in email.attachments:
        att.delete_from_disk()
    db.session.delete(email)
    db.session.commit()
    return jsonify({'ok': True})


@bp.post('/api/emails/<int:email_id>/move')
def move_email(email_id):
    email   = Email.query.get_or_404(email_id)
    folder  = request.json.get('folder')
    if not folder:
        return jsonify({'error': 'folder obrigatório'}), 400
    account = email.account
    _ms().move_email(email, folder, account)
    return jsonify({'ok': True})


# ── Send ──────────────────────────────────────────────────────────────────────

@bp.post('/api/emails/send')
def send_email():
    account_id = request.form.get('account_id', type=int)
    if not account_id:
        return jsonify({'error': 'account_id obrigatório'}), 400
    account = Account.query.get_or_404(account_id)
    result  = _ms().send_email(
        account=account,
        to=request.form.get('to', ''),
        cc=request.form.get('cc', ''),
        bcc=request.form.get('bcc', ''),
        subject=request.form.get('subject', ''),
        body=request.form.get('body', ''),
        body_html=request.form.get('body_html', ''),
        reply_to_id=request.form.get('reply_to_id', type=int),
        attachments=request.files.getlist('attachments'),
    )
    return jsonify(result)


# ── Bulk ──────────────────────────────────────────────────────────────────────

@bp.post('/api/emails/bulk')
def bulk_action():
    """
    Executa ações em massa com UMA única conexão IMAP por pasta.
    Agrupa todos os UIDs e manda em um único comando — evita bloqueio no servidor.
    """
    import imaplib
    from collections import defaultdict
    data       = request.json
    action     = data.get('action')
    ids        = data.get('ids', [])
    folder     = data.get('folder', 'INBOX')
    account_id = data.get('account_id')
    ms         = _ms()
    log        = current_app.logger

    if ids:
        emails = Email.query.filter(Email.id.in_(ids)).all()
    else:
        q = Email.query.filter_by(folder=folder)
        if account_id:
            q = q.filter_by(account_id=account_id)
        emails = q.all()

    if not emails:
        return jsonify({'ok': True, 'count': 0})

    # Agrupa por (account_id, folder) — uma conexão IMAP por grupo
    groups = defaultdict(list)
    for email in emails:
        groups[(email.account_id, email.folder)].append(email)

    count = 0

    for (acc_id, fldr), group in groups.items():
        acc  = group[0].account
        uids = [str(e.uid) for e in group if e.uid]
        if not uids:
            continue
        uid_set  = ','.join(uids)
        is_gmail = 'gmail' in (acc.imap_host or '').lower()

        try:
            from services.imap_pool import pool, connection
            pool.register(acc)

            if action == 'mark_read':
                with connection(acc_id) as conn:
                    conn.select(f'"{fldr}"')
                    conn.uid('store', uid_set, '+FLAGS', '\\Seen')
                db.session.query(Email).filter(
                    Email.id.in_([e.id for e in group])
                ).update({'is_read': True}, synchronize_session=False)
                db.session.commit()
                count += len(group)

            elif action == 'mark_unread':
                with connection(acc_id) as conn:
                    conn.select(f'"{fldr}"')
                    conn.uid('store', uid_set, '-FLAGS', '\\Seen')
                db.session.query(Email).filter(
                    Email.id.in_([e.id for e in group])
                ).update({'is_read': False}, synchronize_session=False)
                db.session.commit()
                count += len(group)

            elif action == 'delete':
                trash = ms.find_folder(acc, ['trash', 'deleted', 'lixeira'])
                with connection(acc_id) as conn:
                    conn.select(f'"{fldr}"')
                    if trash and fldr.lower() != trash.lower():
                        try:
                            if is_gmail:
                                conn.uid('move', uid_set, f'"{trash}"')
                            else:
                                conn.uid('copy', uid_set, f'"{trash}"')
                                conn.uid('store', uid_set, '+FLAGS', '\\Deleted')
                                conn.expunge()
                        except Exception:
                            conn.uid('store', uid_set, '+FLAGS', '\\Deleted')
                            conn.expunge()
                    else:
                        conn.uid('store', uid_set, '+FLAGS', '\\Deleted')
                        conn.expunge()
                for e in group:
                    for att in e.attachments:
                        att.delete_from_disk()
                db.session.query(Email).filter(
                    Email.id.in_([e.id for e in group])
                ).delete(synchronize_session=False)
                db.session.commit()
                count += len(group)

            elif action in ('mark_spam', 'mark_not_spam'):
                target = ms.find_folder(acc, ['spam', 'junk']) if action == 'mark_spam' else 'INBOX'
                if target and fldr.lower() != target.lower():
                    with connection(acc_id) as conn:
                        conn.select(f'"{fldr}"')
                        try:
                            if is_gmail:
                                conn.uid('move', uid_set, f'"{target}"')
                            else:
                                conn.uid('copy', uid_set, f'"{target}"')
                                conn.uid('store', uid_set, '+FLAGS', '\\Deleted')
                                conn.expunge()
                        except Exception:
                            conn.uid('store', uid_set, '+FLAGS', '\\Deleted')
                            conn.expunge()
                    db.session.query(Email).filter(
                        Email.id.in_([e.id for e in group])
                    ).update({'folder': target}, synchronize_session=False)
                    db.session.commit()
                    count += len(group)

        except Exception as e:
            log.warning(f'bulk {action} error (acc={acc_id} folder={fldr}): {e}')
            try: db.session.rollback()
            except Exception: pass

    return jsonify({'ok': True, 'count': count})


# ── Threads ───────────────────────────────────────────────────────────────────

@bp.get('/api/threads/<path:thread_id>')
def get_thread(thread_id):
    account_id = request.args.get('account_id', type=int)
    q = Email.query.filter_by(thread_id=thread_id)
    if account_id:
        q = q.filter_by(account_id=account_id)
    emails = q.order_by(Email.date.asc()).all()
    return jsonify([_sanitized_dict(e) for e in emails])


# ── Attachments ───────────────────────────────────────────────────────────────

@bp.get('/api/attachments/<int:attachment_id>')
def download_attachment(attachment_id):
    att = Attachment.query.get_or_404(attachment_id)
    try:
        data = att.get_data()
    except FileNotFoundError:
        abort(404)
    return send_file(io.BytesIO(data), download_name=att.filename,
                     mimetype=att.content_type, as_attachment=True)


# ── IMAP Search ───────────────────────────────────────────────────────────────

@bp.post('/api/accounts/<int:account_id>/search-imap')
def search_imap(account_id):
    Account.query.get_or_404(account_id)
    data   = request.json
    query  = data.get('query', '').strip()
    folder = data.get('folder', 'INBOX')
    if not query:
        return jsonify({'error': 'query obrigatório'}), 400
    app = current_app._get_current_object()
    def run():
        with app.app_context():
            _ms().search_imap(account_id, folder, query)
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


# ── Manutenção ────────────────────────────────────────────────────────────────

@bp.post('/api/accounts/<int:account_id>/enforce-limit')
def run_enforce_limit(account_id):
    Account.query.get_or_404(account_id)
    removed = enforce_email_limit(account_id)
    return jsonify({'ok': True, 'removed': removed})