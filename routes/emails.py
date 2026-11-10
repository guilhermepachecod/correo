"""
routes/emails.py
Operações sobre e-mails: listagem (com visões unificadas — Fase 2.9), leitura,
flags, spam, exclusão, envio, ações em massa, mover e threads.

Ações individuais e em massa são servidor-primeiro (Fase 2.6): o banco local
só muda depois que o servidor IMAP confirma. Em erro, devolve 502 e não
mexe no banco — a UI não deve remover o item otimisticamente sem reverter.
"""
import io
import threading
from collections import defaultdict
from flask import Blueprint, request, jsonify, send_file, abort, current_app

from database import db, Account, Email, Attachment, FolderState
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

_VIEW_ROLE = {'all': 'inbox', 'inbox': 'inbox', 'unread': 'inbox', 'sent': 'sent', 'trash': 'trash'}


def _apply_view_filter(q, view: str, account_id: int = None):
    """Aplica o filtro de uma visão unificada (Fase 2.9/5.1) a uma query de
    Email. Agrega por folder_state.role, não por nome de pasta — funciona
    com qualquer conta/servidor. Devolve (query, None) ou (None, 'mensagem
    de erro') se a view não existir."""
    if view == 'starred':
        return q.filter(Email.is_starred == True), None  # noqa: E712 — todas as pastas, todas as contas
    if view not in _VIEW_ROLE:
        return None, f'view inválida: {view}'

    folder_q = db.session.query(FolderState.account_id, FolderState.folder) \
                         .filter(FolderState.role == _VIEW_ROLE[view])
    if account_id:
        folder_q = folder_q.filter(FolderState.account_id == account_id)
    by_account = defaultdict(list)
    for acc_id, folder_name in folder_q.all():
        by_account[acc_id].append(folder_name)
    if by_account:
        q = q.filter(db.or_(*[
            db.and_(Email.account_id == aid, Email.folder.in_(folders))
            for aid, folders in by_account.items()
        ]))
    else:
        q = q.filter(db.false())
    if view == 'unread':
        q = q.filter(Email.is_read == False)  # noqa: E712
    return q, None


@bp.get('/api/emails')
def get_emails():
    account_id = request.args.get('account_id', type=int)
    view       = request.args.get('view', '').strip().lower()
    page       = request.args.get('page', 1, type=int)
    per_page   = request.args.get('per_page', 50, type=int)
    search     = request.args.get('search', '').strip()
    thread_id  = request.args.get('thread_id', '').strip()

    q = Email.query
    if account_id: q = q.filter_by(account_id=account_id)
    if thread_id:  q = q.filter_by(thread_id=thread_id)

    if view:
        q, err = _apply_view_filter(q, view, account_id)
        if err:
            return jsonify({'error': err}), 400
    else:
        folder = request.args.get('folder', 'INBOX')
        if folder:
            q = q.filter_by(folder=folder)

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


@bp.get('/api/views/counts')
def view_counts():
    """Contadores das visões unificadas (Fase 5.1), pra badge na sidebar."""
    result = {}
    for view in ('all', 'unread', 'starred'):
        q, err = _apply_view_filter(Email.query, view)
        result[view] = {'total': q.count() if not err else 0}
    return jsonify(result)


@bp.get('/api/emails/<int:email_id>')
def get_email(email_id):
    email = Email.query.get_or_404(email_id)
    if not email.body_cached:
        _ms().fetch_full_body(email)  # busca corpo sob demanda (Fase 2.7)
    if not email.is_read:
        account = email.account  # carrega antes do commit para evitar DetachedInstanceError
        email.is_read = True
        db.session.commit()
        _ms().mark_read_on_server(email, account)
    return jsonify(_sanitized_dict(email))


# ── Flags ─────────────────────────────────────────────────────────────────────

@bp.post('/api/emails/<int:email_id>/star')
def toggle_star(email_id):
    email    = Email.query.get_or_404(email_id)
    account  = email.account
    starred  = not email.is_starred
    if not _ms().mark_star_on_server(email, starred, account):
        return jsonify({'error': 'Falha ao atualizar no servidor IMAP'}), 502
    email.is_starred = starred
    db.session.commit()
    return jsonify({'starred': email.is_starred})


@bp.post('/api/emails/<int:email_id>/read')
def set_read(email_id):
    email   = Email.query.get_or_404(email_id)
    account = email.account  # carrega antes do commit
    is_read = request.json.get('read', True)
    ok = (_ms().mark_read_on_server(email, account) if is_read
          else _ms().mark_unread_on_server(email, account))
    if not ok:
        return jsonify({'error': 'Falha ao atualizar no servidor IMAP'}), 502
    email.is_read = is_read
    db.session.commit()
    return jsonify({'read': email.is_read})


@bp.post('/api/emails/<int:email_id>/spam')
def set_spam(email_id):
    email        = Email.query.get_or_404(email_id)
    account      = email.account
    mark_as_spam = request.json.get('spam', True)
    result       = _ms().set_spam(email, mark_as_spam, account)
    if not result.get('ok'):
        return jsonify(result), 502
    db.session.refresh(email)
    return jsonify({'ok': True, 'folder': email.folder})


# ── Delete / Move ─────────────────────────────────────────────────────────────

@bp.delete('/api/emails/<int:email_id>')
def delete_email(email_id):
    email   = Email.query.get_or_404(email_id)
    account = email.account  # carrega antes de qualquer delete/commit
    result  = _ms().delete_email(email, account)
    if not result.get('ok'):
        return jsonify(result), 502
    return jsonify(result)


@bp.post('/api/emails/<int:email_id>/move')
def move_email(email_id):
    email  = Email.query.get_or_404(email_id)
    folder = request.json.get('folder')
    if not folder:
        return jsonify({'error': 'folder obrigatório'}), 400
    account = email.account
    result  = _ms().move_email(email, folder, account)
    if not result.get('ok'):
        return jsonify(result), 502
    return jsonify(result)


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


# ── Bulk (Fase 2.6 — sempre em lote por conta/pasta, nunca EXPUNGE genérico) ──

BULK_IMAP_BATCH = 200  # UIDs por comando IMAP — evita linha de comando longa
                       # demais pro servidor (ex.: "apagar todos" com milhares
                       # de mensagens na Lixeira travava silenciosamente)


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


@bp.post('/api/emails/bulk')
def bulk_action():
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

    # Agrupa por (account_id, folder) — mas o comando IMAP em si sai em
    # lotes de BULK_IMAP_BATCH UIDs (ver _chunks), nunca tudo de uma vez
    groups = defaultdict(list)
    for email in emails:
        groups[(email.account_id, email.folder)].append(email)

    count, errors = 0, []

    for (acc_id, fldr), group in groups.items():
        acc = group[0].account
        by_uid = {e.uid: e for e in group if e.uid}
        if not by_uid:
            continue

        try:
            caps = ms._capabilities(acc)
        except Exception as e:
            log.warning(f'bulk {action} capabilities error (acc={acc_id}): {e}')
            errors.append(f'{acc.email}: {e}')
            continue
        has_move, has_uidplus = 'MOVE' in caps, 'UIDPLUS' in caps

        trash  = ms.role_folder(acc, 'trash')  if action == 'delete' else None
        target = (ms.role_folder(acc, 'junk') if action == 'mark_spam'
                  else ms.role_folder(acc, 'inbox') or 'INBOX') if action in ('mark_spam', 'mark_not_spam') else None

        for batch_uids in _chunks(list(by_uid.keys()), BULK_IMAP_BATCH):
            batch_emails = [by_uid[u] for u in batch_uids]
            batch_ids    = [e.id for e in batch_emails]
            try:
                if action == 'mark_read':
                    ms._safe_exec(acc, lambda c, _u=batch_uids: (c.select_folder(fldr, readonly=False), c.add_flags(_u, ['\\Seen'])))
                    Email.query.filter(Email.id.in_(batch_ids)).update({'is_read': True}, synchronize_session=False)
                    db.session.commit()
                    count += len(batch_emails)

                elif action == 'mark_unread':
                    ms._safe_exec(acc, lambda c, _u=batch_uids: (c.select_folder(fldr, readonly=False), c.remove_flags(_u, ['\\Seen'])))
                    Email.query.filter(Email.id.in_(batch_ids)).update({'is_read': False}, synchronize_session=False)
                    db.session.commit()
                    count += len(batch_emails)

                elif action == 'delete':
                    if trash and fldr != trash:
                        def _op(c, _u=batch_uids, _trash=trash):
                            c.select_folder(fldr, readonly=False)
                            if has_move:
                                c.move(_u, _trash)
                            else:
                                c.copy(_u, _trash)
                                c.add_flags(_u, ['\\Deleted'])
                                if has_uidplus:
                                    c.uid_expunge(_u)
                        ms._safe_exec(acc, _op)
                        Email.query.filter(Email.id.in_(batch_ids)).update({'folder': trash}, synchronize_session=False)
                        db.session.commit()
                        count += len(batch_emails)
                    else:
                        def _op(c, _u=batch_uids):
                            c.select_folder(fldr, readonly=False)
                            c.add_flags(_u, ['\\Deleted'])
                            if has_uidplus:
                                c.uid_expunge(_u)
                        ms._safe_exec(acc, _op)
                        if has_uidplus:
                            for e in batch_emails:
                                for att in e.attachments:
                                    att.delete_from_disk()
                            Email.query.filter(Email.id.in_(batch_ids)).delete(synchronize_session=False)
                            db.session.commit()
                            count += len(batch_emails)
                        # sem UIDPLUS: fica só flagado \Deleted no servidor,
                        # sem remover localmente (nunca EXPUNGE genérico — B7)

                elif action in ('mark_spam', 'mark_not_spam'):
                    if target and fldr != target:
                        def _op(c, _u=batch_uids, _target=target):
                            c.select_folder(fldr, readonly=False)
                            if has_move:
                                c.move(_u, _target)
                            else:
                                c.copy(_u, _target)
                                c.add_flags(_u, ['\\Deleted'])
                                if has_uidplus:
                                    c.uid_expunge(_u)
                        ms._safe_exec(acc, _op)
                        Email.query.filter(Email.id.in_(batch_ids)).update({'folder': target}, synchronize_session=False)
                        db.session.commit()
                        count += len(batch_emails)

            except Exception as e:
                # Um lote falhando não aborta os outros — com milhares de
                # mensagens, melhor processar o que der do que travar tudo.
                log.warning(f'bulk {action} error (acc={acc_id} folder={fldr} batch={len(batch_uids)}): {e}')
                errors.append(f'{acc.email}/{fldr}: {e}')
                try: db.session.rollback()
                except Exception: pass

    return jsonify({'ok': count > 0 or not errors, 'count': count, 'errors': errors})


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
    """Poda manual de corpos (Fase 2.7) — mesma função que roda automaticamente
    depois de cada sync incremental. Nome da rota mantido por compatibilidade."""
    Account.query.get_or_404(account_id)
    from services.mail_service import prune_old_bodies
    removed = prune_old_bodies(account_id)
    return jsonify({'ok': True, 'pruned': removed})
