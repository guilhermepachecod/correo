"""
services/mail_service.py
Operações de e-mail: sync de pastas, envio SMTP, flags, spam, move.
Depende de: imap_pool, email_parser, database.
O IMAPIdleManager é injetado externamente (evita import circular).
"""
import imaplib
import smtplib
import re
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from database import db, Email, Attachment, EmailFilter, Account, \
    enforce_email_limit, resolve_thread_id
from services.imap_pool import pool
from services.email_parser import parse_email_from_raw

log = logging.getLogger(__name__)

BATCH_SIZE = 50


class MailService:

    def __init__(self, socketio):
        self.socketio      = socketio
        self._folder_cache = {}
        self.idle_manager  = None  # injetado por app.py após subir

    # ── Emit ──────────────────────────────────────────────────────────────────

    def _emit(self, event: str, data: dict) -> None:
        try:
            self.socketio.emit(event, data)
        except Exception:
            pass

    # ── Conexão ───────────────────────────────────────────────────────────────

    def _get_conn(self, account: Account) -> imaplib.IMAP4:
        pool.register(account)
        return pool.get(account.id)

    def _safe_exec(self, account: Account, fn):
        """Executa fn(conn), reconectando uma vez em caso de erro de conexão."""
        try:
            return fn(self._get_conn(account))
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, BrokenPipeError) as e:
            log.warning(f'IMAP reconectando ({account.email}): {e}')
            pool.invalidate(account.id)
            return fn(self._get_conn(account))

    # ── Idle proxy ────────────────────────────────────────────────────────────

    def start_idle_for(self, account_id: int) -> None:
        if self.idle_manager:
            self.idle_manager.start_account(account_id)

    def stop_idle_for(self, account_id: int) -> None:
        if self.idle_manager:
            self.idle_manager.stop_account(account_id)

    def restart_idle_for(self, account_id: int) -> None:
        if self.idle_manager:
            self.idle_manager.restart_account(account_id)

    # ── Test ──────────────────────────────────────────────────────────────────

    def test_connection(self, account: Account) -> dict:
        errors = []
        try:
            pool.register(account)
            pool.invalidate(account.id)
            pool.get(account.id).noop()
        except Exception as e:
            errors.append(f'IMAP: {e}')
        try:
            if account.smtp_ssl:
                s = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=10)
            else:
                s = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=10)
                if account.smtp_tls:
                    s.starttls()
            s.login(account.username, account.get_password())
            s.quit()
        except Exception as e:
            errors.append(f'SMTP: {e}')
        return {'ok': not errors, 'errors': errors}

    # ── Pastas ────────────────────────────────────────────────────────────────

    def get_folders(self, account: Account) -> list[str]:
        if account.id in self._folder_cache:
            return self._folder_cache[account.id]
        try:
            folder_list = self._safe_exec(account, lambda c: c.list()[1])
            folders = []
            for item in folder_list:
                if isinstance(item, bytes):
                    decoded = item.decode('utf-8', errors='replace')
                    m = re.search(r'\(.*?\)\s+"?[./]"?\s+(.*)', decoded)
                    if m:
                        folders.append(m.group(1).strip().strip('"'))
            log.info(f'Pastas de {account.email}: {folders}')
            self._folder_cache[account.id] = folders
            return folders
        except Exception as e:
            log.error(f'get_folders error: {e}')
            return ['INBOX']

    def find_folder(self, account: Account, keywords: list[str]) -> str | None:
        for kw in keywords:
            for f in self.get_folders(account):
                if kw in f.lower():
                    return f
        return None

    def _get_sync_folders(self, account: Account) -> list[str]:
        folders = self.get_folders(account)
        priority = ['inbox', 'sent', 'drafts', 'junk', 'spam', 'trash', 'archive']
        result = []
        for kw in priority:
            for f in folders:
                if kw in f.lower() and f not in result:
                    result.append(f)
        for f in folders:
            if f not in result:
                result.append(f)
        return result[:10]

    # ── Sync ─────────────────────────────────────────────────────────────────

    def sync_account(self, account_id: int) -> None:
        """Sync manual completo de todas as pastas."""
        account = Account.query.get(account_id)
        if not account:
            return
        log.info(f'Sync manual {account.email}')
        self._emit('sync_start', {'account_id': account_id, 'email': account.email})
        self._folder_cache.pop(account_id, None)
        try:
            total_new, total_flags = 0, 0
            for folder in self._get_sync_folders(account):
                new, flags = self._sync_folder(account, folder)
                total_new  += new
                total_flags += flags
            account.last_sync = datetime.utcnow()
            db.session.commit()
            self._emit('sync_done', {
                'account_id': account_id, 'email': account.email,
                'new': total_new, 'flag_changes': total_flags,
            })
        except Exception as e:
            log.error(f'sync_account error: {e}')
            pool.invalidate(account_id)
            self._emit('sync_error', {'account_id': account_id, 'error': str(e)})

    def sync_account_non_inbox(self, account_id: int) -> None:
        """Sync silencioso de todas as pastas exceto INBOX (coberta pelo IDLE)."""
        account = Account.query.get(account_id)
        if not account:
            return
        try:
            folders = [f for f in self._get_sync_folders(account)
                       if not f.upper().startswith('INBOX') or '.' in f]
            log.info(f'Sync outras pastas {account.email}: {folders}')
            for folder in folders:
                self._sync_folder(account, folder)
        except Exception as e:
            log.warning(f'sync_account_non_inbox error {account.email}: {e}')

    def _sync_folder(self, account: Account, folder: str) -> tuple[int, int]:
        """Sync de uma pasta. Retorna (new_count, flag_change_count)."""
        new_count, flag_changes = 0, 0
        try:
            status, _ = self._safe_exec(account, lambda c: c.select(f'"{folder}"', readonly=True))
            if status != 'OK':
                return 0, 0

            _, data = self._safe_exec(account, lambda c: c.uid('search', None, 'ALL'))
            all_uids_server = {int(u) for u in (data[0].split() if data[0] else [])}

            existing_rows = db.session.query(Email.uid, Email.id, Email.is_read) \
                                      .filter_by(account_id=account.id, folder=folder).all()
            existing_uid_map = {r.uid: {'id': r.id, 'is_read': r.is_read} for r in existing_rows}
            existing_uids    = set(existing_uid_map)

            # Remove fantasmas
            ghost_ids = [existing_uid_map[u]['id'] for u in (existing_uids - all_uids_server)
                         if u in existing_uid_map]
            if ghost_ids:
                log.info(f'_sync_folder {folder}: removendo {len(ghost_ids)} fantasmas')
                db.session.query(Email).filter(Email.id.in_(ghost_ids)).delete(synchronize_session=False)
                db.session.commit()

            new_uids = sorted(all_uids_server - existing_uids)[-200:]
            if new_uids:
                uid_str = ','.join(str(u) for u in new_uids)
                _, msg_data = self._safe_exec(
                    account, lambda c: c.uid('fetch', uid_str, '(UID FLAGS BODY.PEEK[])'))
                batch, i = [], 0
                while i < len(msg_data):
                    item = msg_data[i]
                    if not isinstance(item, tuple):
                        i += 1; continue
                    meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                    m    = re.search(r'UID (\d+)', meta)
                    if not m:
                        i += 1; continue
                    uid     = int(m.group(1))
                    is_read = '\\Seen' in meta
                    try:
                        email_obj, att_data = parse_email_from_raw(
                            item[1], uid, folder, account.id, is_read, resolve_thread_id)
                        batch.append((email_obj, att_data))
                        new_count += 1
                        if len(batch) >= BATCH_SIZE:
                            self._flush_batch(account, batch); batch = []
                    except Exception as e:
                        log.warning(f'Parse error UID {uid}: {e}')
                    i += 1
                if batch:
                    self._flush_batch(account, batch)

            # Sync de flags dos 500 mais recentes
            check_uids = sorted(existing_uids & all_uids_server)[-500:]
            if check_uids:
                uid_str = ','.join(str(u) for u in check_uids)
                try:
                    _, flag_data = self._safe_exec(
                        account, lambda c: c.uid('fetch', uid_str, '(UID FLAGS)'))
                    updates = []
                    for item in flag_data:
                        if not isinstance(item, (tuple, list)): continue
                        meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                        m = re.search(r'UID (\d+)', meta)
                        if not m: continue
                        uid = int(m.group(1))
                        server_read = '\\Seen' in meta
                        row = existing_uid_map.get(uid)
                        if row and row['is_read'] != server_read:
                            updates.append((row['id'], server_read))
                    if updates:
                        for eid, is_read in updates:
                            db.session.query(Email).filter_by(id=eid).update(
                                {'is_read': is_read}, synchronize_session=False)
                        db.session.commit()
                        flag_changes = len(updates)
                        self._emit('flags_updated', {
                            'account_id': account.id, 'folder': folder,
                            'updates': [{'id': eid, 'is_read': r} for eid, r in updates],
                        })
                except Exception as e:
                    log.warning(f'Flag sync error {folder}: {e}')

        except Exception as e:
            log.error(f'_sync_folder {folder} error: {e}')
            pool.invalidate(account.id)
            try: db.session.rollback()
            except Exception: pass

        return new_count, flag_changes

    def _flush_batch(self, account: Account, batch: list) -> None:
        try:
            for email_obj, _ in batch:
                db.session.add(email_obj)
            db.session.flush()
            for email_obj, att_data in batch:
                for a in att_data:
                    att = Attachment(email_id=email_obj.id, filename=a['filename'],
                                     content_type=a['content_type'], size=a['size'])
                    try:
                        att.save_to_disk(a['data'])
                    except Exception as e:
                        log.warning(f'Falha ao salvar anexo em disco: {e}')
                        att.data = a['data']
                    db.session.add(att)
            db.session.commit()
            filters = list(EmailFilter.query.filter_by(account_id=account.id, enabled=True)
                                            .order_by(EmailFilter.priority.asc()).all())
            for email_obj, _ in batch:
                self._apply_filters(email_obj, filters)
                self._emit('new_email', {
                    'account_id': account.id,
                    'folder':     email_obj.folder,
                    'email':      email_obj.to_list_dict(),
                })
            enforce_email_limit(account.id)
        except Exception as e:
            log.error(f'_flush_batch error: {e}')
            try: db.session.rollback()
            except Exception: pass

    def _apply_filters(self, email_obj: Email, filters: list) -> None:
        for f in filters:
            if not f.matches(email_obj):
                continue
            changed = False
            if f.action_mark_read and not email_obj.is_read:
                email_obj.is_read = True; changed = True
            if f.action_star and not email_obj.is_starred:
                email_obj.is_starred = True; changed = True
            if f.action_delete:
                self.delete_email(email_obj)
                db.session.delete(email_obj)
                db.session.commit()
                return
            if f.action_move_to and email_obj.folder != f.action_move_to:
                self.move_email(email_obj, f.action_move_to); changed = True
            if changed:
                try: db.session.commit()
                except Exception: db.session.rollback()
            break

    # ── IMAP Search ───────────────────────────────────────────────────────────

    def search_imap(self, account_id: int, folder: str, query: str) -> None:
        account = Account.query.get(account_id)
        if not account:
            return
        self._emit('imap_search_start', {'account_id': account_id, 'query': query})
        try:
            status, _ = self._safe_exec(account, lambda c: c.select(f'"{folder}"', readonly=True))
            if status != 'OK':
                self._emit('imap_search_done', {'account_id': account_id, 'found': 0,
                                                 'error': f'Pasta {folder} inacessível'})
                return
            criterion = f'TEXT "{query.replace(chr(34), "")}"'
            _, data = self._safe_exec(account, lambda c: c.uid('search', None, criterion))
            uids = data[0].split() if data[0] else []
            if not uids:
                self._emit('imap_search_done', {'account_id': account_id, 'found': 0})
                return
            existing = {row[0] for row in db.session.query(Email.uid)
                                                     .filter_by(account_id=account_id, folder=folder).all()}
            new_uids = [u for u in uids if int(u) not in existing]
            if new_uids:
                uid_str  = ','.join(u.decode() for u in new_uids[:100])
                _, msg_data = self._safe_exec(
                    account, lambda c: c.uid('fetch', uid_str, '(UID FLAGS BODY.PEEK[])'))
                batch, i = [], 0
                while i < len(msg_data):
                    item = msg_data[i]
                    if not isinstance(item, tuple): i += 1; continue
                    meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                    m    = re.search(r'UID (\d+)', meta)
                    if not m: i += 1; continue
                    uid     = int(m.group(1))
                    is_read = '\\Seen' in meta
                    try:
                        email_obj, att_data = parse_email_from_raw(
                            item[1], uid, folder, account_id, is_read, resolve_thread_id)
                        batch.append((email_obj, att_data))
                        if len(batch) >= BATCH_SIZE:
                            self._flush_batch(account, batch); batch = []
                    except Exception as e:
                        log.warning(f'search_imap parse error UID {uid}: {e}')
                    i += 1
                if batch:
                    self._flush_batch(account, batch)
            self._emit('imap_search_done', {
                'account_id': account_id, 'found': len(uids),
                'new': len(new_uids), 'folder': folder, 'query': query,
            })
        except Exception as e:
            log.error(f'search_imap error: {e}')
            self._emit('imap_search_done', {'account_id': account_id, 'found': 0, 'error': str(e)})

    # ── Flag operations ───────────────────────────────────────────────────────

    def mark_read_on_server(self, email_obj: Email) -> None:
        try:
            def _mark(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Seen')
            self._safe_exec(email_obj.account, _mark)
        except Exception as e:
            log.warning(f'mark_read error: {e}')

    def mark_unread_on_server(self, email_obj: Email) -> None:
        try:
            def _mark(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('store', str(email_obj.uid), '-FLAGS', '\\Seen')
            self._safe_exec(email_obj.account, _mark)
        except Exception as e:
            log.warning(f'mark_unread error: {e}')

    def set_spam(self, email_obj: Email, mark_as_spam: bool) -> bool:
        try:
            account = email_obj.account
            target  = self.find_folder(account, ['spam', 'junk']) if mark_as_spam else 'INBOX'
            if not target or email_obj.folder.lower() == target.lower():
                return bool(target)
            def _move(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('copy', str(email_obj.uid), f'"{target}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
                conn.expunge()
            self._safe_exec(account, _move)
            email_obj.folder = target
            db.session.commit()
            return True
        except Exception as e:
            log.warning(f'set_spam error: {e}')
            return False

    def delete_email(self, email_obj: Email) -> None:
        try:
            def _delete(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
                conn.expunge()
            self._safe_exec(email_obj.account, _delete)
        except Exception as e:
            log.warning(f'delete_email error: {e}')

    def move_email(self, email_obj: Email, target_folder: str) -> None:
        try:
            def _move(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('copy', str(email_obj.uid), f'"{target_folder}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
                conn.expunge()
            self._safe_exec(email_obj.account, _move)
            email_obj.folder = target_folder
            db.session.commit()
        except Exception as e:
            log.warning(f'move_email error: {e}')

    # ── Send ──────────────────────────────────────────────────────────────────

    def send_email(self, account: Account, to: str, cc: str, bcc: str,
                   subject: str, body: str, body_html: str = '',
                   reply_to_id=None, attachments=None) -> dict:
        try:
            if body_html:
                msg = MIMEMultipart('mixed')
                alt = MIMEMultipart('alternative')
                alt.attach(MIMEText(body or '', 'plain', 'utf-8'))
                alt.attach(MIMEText(body_html, 'html', 'utf-8'))
                msg.attach(alt)
            else:
                msg = MIMEMultipart('mixed')
                msg.attach(MIMEText(body or '', 'plain', 'utf-8'))

            msg['From']    = f'{account.name} <{account.email}>'
            msg['To']      = to
            msg['Subject'] = subject
            if cc: msg['Cc'] = cc

            if reply_to_id:
                orig = Email.query.get(reply_to_id)
                if orig and orig.message_id:
                    msg['In-Reply-To'] = orig.message_id
                    msg['References']  = orig.message_id

            for f in (attachments or []):
                if not f or not f.filename: continue
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{f.filename}"')
                msg.attach(part)

            recipients = [r.strip() for r in to.split(',') if r.strip()]
            if cc:  recipients += [r.strip() for r in cc.split(',') if r.strip()]
            if bcc: recipients += [r.strip() for r in bcc.split(',') if r.strip()]

            if account.smtp_ssl:
                s = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=30)
            else:
                s = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30)
                if account.smtp_tls: s.starttls()
            s.login(account.username, account.get_password())
            s.sendmail(account.email, recipients, msg.as_string())
            s.quit()

            try:
                sent_folder = self.find_folder(account, ['sent'])
                if sent_folder:
                    raw = msg.as_bytes()
                    self._safe_exec(account,
                        lambda c: c.append(f'"{sent_folder}"', '\\Seen', None, raw))
            except Exception as e:
                log.warning(f'Could not save to Sent: {e}')

            return {'ok': True}
        except Exception as e:
            log.error(f'send_email error: {e}')
            return {'ok': False, 'error': str(e)}
