import imaplib
import smtplib
import email as email_lib
from email import policy as email_policy
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
import logging
import re
import threading
from datetime import datetime

from database import Email, Attachment, Account

log = logging.getLogger(__name__)

BATCH_SIZE = 50


# ── Connection Pool ────────────────────────────────────────────────────────────

class IMAPPool:
    """
    Pool de conexões IMAP — uma conexão persistente por conta.
    Reconecta automaticamente se a conexão cair.
    Thread-safe via lock por conta.
    """

    def __init__(self):
        self._conns       = {}   # account_id -> imaplib connection
        self._locks       = {}   # account_id -> threading.Lock
        self._meta        = {}   # account_id -> dict de credenciais
        self._global_lock = threading.Lock()

    def _get_lock(self, account_id):
        with self._global_lock:
            if account_id not in self._locks:
                self._locks[account_id] = threading.Lock()
            return self._locks[account_id]

    def register(self, account: Account):
        with self._global_lock:
            self._meta[account.id] = {
                'host':    account.imap_host,
                'port':    account.imap_port,
                'ssl':     account.imap_ssl,
                'user':    account.username,
                'pass_fn': account.get_password,
            }

    def _connect_raw(self, account_id) -> imaplib.IMAP4:
        m = self._meta[account_id]
        if m['ssl']:
            conn = imaplib.IMAP4_SSL(m['host'], m['port'])
        else:
            conn = imaplib.IMAP4(m['host'], m['port'])
        conn.login(m['user'], m['pass_fn']())
        log.info(f'IMAP pool: nova conexão para account_id={account_id}')
        return conn

    def _is_alive(self, conn) -> bool:
        try:
            conn.noop()
            return True
        except Exception:
            return False

    def get(self, account_id) -> imaplib.IMAP4:
        lock = self._get_lock(account_id)
        with lock:
            if account_id not in self._meta:
                raise RuntimeError(f'Conta {account_id} não registrada no pool')
            conn = self._conns.get(account_id)
            if conn is None or not self._is_alive(conn):
                if conn:
                    try: conn.logout()
                    except Exception: pass
                conn = self._connect_raw(account_id)
                self._conns[account_id] = conn
            return conn

    def invalidate(self, account_id):
        with self._global_lock:
            conn = self._conns.pop(account_id, None)
            if conn:
                try: conn.logout()
                except Exception: pass

    def close_all(self):
        with self._global_lock:
            for conn in self._conns.values():
                try: conn.logout()
                except Exception: pass
            self._conns.clear()


_pool = IMAPPool()


# ── Encoding helpers ───────────────────────────────────────────────────────────

def _decode_header_value(value):
    if not value:
        return ''
    try:
        parts = decode_header(value)
        result = []
        for part, charset in parts:
            if isinstance(part, bytes):
                cs = charset or 'utf-8'
                try:
                    result.append(part.decode(cs, errors='replace'))
                except (LookupError, UnicodeDecodeError):
                    result.append(part.decode('latin-1', errors='replace'))
            else:
                result.append(str(part))
        return ''.join(result)
    except Exception:
        return str(value)


def _decode_payload(part):
    try:
        payload = part.get_payload(decode=True)
        if not payload:
            return ''
        charset = part.get_content_charset() or 'utf-8'
        try:
            return payload.decode(charset, errors='replace')
        except (LookupError, UnicodeDecodeError):
            return payload.decode('latin-1', errors='replace')
    except Exception:
        return ''


def _get_body(msg):
    text, html = '', ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get('Content-Disposition', ''))
            if 'attachment' in cd:
                continue
            if ct == 'text/plain' and not text:
                text = _decode_payload(part)
            elif ct == 'text/html' and not html:
                html = _decode_payload(part)
    else:
        ct = msg.get_content_type()
        body = _decode_payload(msg)
        if ct == 'text/html':
            html = body
        else:
            text = body
    return text, html


def _get_attachments(msg):
    attachments = []
    for part in msg.walk():
        cd = str(part.get('Content-Disposition', ''))
        if 'attachment' not in cd and 'inline' not in cd:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = _decode_header_value(filename)
        data = part.get_payload(decode=True)
        if not data:
            continue
        attachments.append({
            'filename':     filename,
            'content_type': part.get_content_type(),
            'size':         len(data),
            'data':         data,
        })
    return attachments


# ── MailService ────────────────────────────────────────────────────────────────

class MailService:
    def __init__(self, db, socketio):
        self.db           = db
        self.socketio     = socketio
        self._folder_cache = {}  # account_id -> [folders]
        # Throttle: impede sync simultâneo da mesma conta
        self._syncing_accounts = set()
        self._sync_lock = threading.Lock()

    def _emit(self, event, data):
        try:
            self.socketio.emit(event, data)
        except Exception:
            pass

    def _get_conn(self, account: Account) -> imaplib.IMAP4:
        _pool.register(account)
        return _pool.get(account.id)

    def _safe_exec(self, account: Account, fn):
        """Executa fn(conn) com retry automático em caso de falha."""
        try:
            conn = self._get_conn(account)
            return fn(conn)
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, BrokenPipeError) as e:
            log.warning(f'IMAP error, reconectando ({account.email}): {e}')
            _pool.invalidate(account.id)
            conn = self._get_conn(account)
            return fn(conn)

    # ── Test ──────────────────────────────────────────────────────────────────

    def test_connection(self, account: Account):
        errors = []
        try:
            _pool.register(account)
            _pool.invalidate(account.id)
            conn = _pool.get(account.id)
            conn.noop()
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

    # ── Folders ───────────────────────────────────────────────────────────────

    def get_folders(self, account: Account):
        if account.id in self._folder_cache:
            return self._folder_cache[account.id]
        try:
            def _list(conn):
                _, folder_list = conn.list()
                return folder_list

            folder_list = self._safe_exec(account, _list)
            folders = []
            for item in folder_list:
                if isinstance(item, bytes):
                    decoded = item.decode('utf-8', errors='replace')
                    match = re.search(r'\(.*?\)\s+"?[./]"?\s+(.*)', decoded)
                    if match:
                        name = match.group(1).strip().strip('"')
                        folders.append(name)
            log.info(f'Pastas de {account.email}: {folders}')
            self._folder_cache[account.id] = folders
            return folders
        except Exception as e:
            log.error(f'get_folders error: {e}')
            return ['INBOX']

    def _find_folder(self, account: Account, keywords):
        folders = self.get_folders(account)
        for kw in keywords:
            for f in folders:
                if kw in f.lower():
                    return f
        return None

    def _get_sync_folders(self, account: Account):
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

    # ── Sync ──────────────────────────────────────────────────────────────────

    def sync_account(self, account_id: int):
        # Throttle: impede sync simultâneo da mesma conta
        with self._sync_lock:
            if account_id in self._syncing_accounts:
                log.info(f'Sync já em andamento para account_id={account_id}, ignorando')
                return
            self._syncing_accounts.add(account_id)

        try:
            self._do_sync_account(account_id)
        finally:
            with self._sync_lock:
                self._syncing_accounts.discard(account_id)

    def _do_sync_account(self, account_id: int):
        from database import db as _db, Account as _Account
        account = _Account.query.get(account_id)
        if not account:
            return

        log.info(f'Sync {account.email}')
        self._emit('sync_start', {'account_id': account_id, 'email': account.email})
        self._folder_cache.pop(account_id, None)

        try:
            folders = self._get_sync_folders(account)
            log.info(f'Pastas para sync: {folders}')
            total_new = 0
            total_flag_changes = 0

            for folder in folders:
                new, flag_changes = self._sync_folder(account, folder)
                total_new += new
                total_flag_changes += flag_changes

            account.last_sync = datetime.utcnow()
            _db.session.commit()
            self._emit('sync_done', {
                'account_id': account_id,
                'email': account.email,
                'new': total_new,
                'flag_changes': total_flag_changes,
            })

        except Exception as e:
            log.error(f'sync_account error: {e}')
            _pool.invalidate(account_id)
            self._emit('sync_error', {'account_id': account_id, 'error': str(e)})

    def _sync_folder(self, account: Account, folder: str):
        """Retorna (new_count, flag_change_count)."""
        from database import db as _db
        new_count = 0
        flag_changes = 0

        try:
            def _select(conn):
                return conn.select(f'"{folder}"', readonly=True)

            status, _ = self._safe_exec(account, _select)
            if status != 'OK':
                log.warning(f'Não foi possível selecionar: {folder}')
                return 0, 0

            # ── 1. Busca todos os UIDs do servidor ────────────────────────────
            def _search(conn):
                return conn.uid('search', None, 'ALL')

            _, data = self._safe_exec(account, _search)
            all_uids_bytes = data[0].split() if data[0] else []
            all_uids_server = {int(u) for u in all_uids_bytes}

            # ── 2. UIDs existentes no banco ────────────────────────────────────
            existing_rows = (
                _db.session.query(Email.uid, Email.id, Email.is_read)
                    .filter_by(account_id=account.id, folder=folder)
                    .all()
            )
            existing_uid_map = {row.uid: {'id': row.id, 'is_read': row.is_read} for row in existing_rows}
            existing_uids = set(existing_uid_map.keys())

            # ── 3. Novos UIDs: baixa body completo ────────────────────────────
            new_uids_int = list(all_uids_server - existing_uids)
            # Limita aos 200 mais recentes
            new_uids_int = sorted(new_uids_int)[-200:]

            if new_uids_int:
                uid_str = ','.join(str(u) for u in new_uids_int)

                def _fetch(conn):
                    return conn.uid('fetch', uid_str, '(UID FLAGS BODY.PEEK[])')

                _, msg_data = self._safe_exec(account, _fetch)

                batch = []
                i = 0
                while i < len(msg_data):
                    item = msg_data[i]
                    if not isinstance(item, tuple):
                        i += 1
                        continue

                    meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                    raw  = item[1]
                    uid_match = re.search(r'UID (\d+)', meta)
                    if not uid_match:
                        i += 1
                        continue

                    uid     = int(uid_match.group(1))
                    is_read = '\\Seen' in meta

                    try:
                        msg = email_lib.message_from_bytes(raw, policy=email_policy.compat32)

                        subject     = _decode_header_value(msg.get('Subject', ''))
                        from_full   = _decode_header_value(msg.get('From', ''))
                        from_name, from_addr = parseaddr(from_full)
                        to_addr     = _decode_header_value(msg.get('To', ''))
                        cc_addr     = _decode_header_value(msg.get('Cc', ''))
                        reply_to    = _decode_header_value(msg.get('Reply-To', ''))
                        message_id  = (msg.get('Message-ID') or '').strip()
                        in_reply_to = (msg.get('In-Reply-To') or '').strip()

                        try:
                            date = parsedate_to_datetime(msg.get('Date', ''))
                            if date.tzinfo:
                                date = date.replace(tzinfo=None)
                        except Exception:
                            date = datetime.utcnow()

                        body_text, body_html = _get_body(msg)
                        att_data = _get_attachments(msg)

                        email_obj = Email(
                            account_id=account.id, folder=folder, uid=uid,
                            message_id=message_id, in_reply_to=in_reply_to,
                            subject=subject or '(sem assunto)',
                            from_addr=from_addr, from_name=from_name,
                            to_addr=to_addr, cc_addr=cc_addr, reply_to=reply_to,
                            date=date, body_text=body_text, body_html=body_html,
                            is_read=is_read, has_attachments=bool(att_data),
                        )
                        batch.append((email_obj, att_data))
                        new_count += 1

                        if len(batch) >= BATCH_SIZE:
                            self._flush_batch(_db, account, batch)
                            batch = []

                    except Exception as e:
                        log.warning(f'Parse error UID {uid}: {e}')

                    i += 1

                if batch:
                    self._flush_batch(_db, account, batch)

            # ── 4. UIDs existentes: sync incremental de flags ──────────────────
            # Só verifica os 500 UIDs mais recentes para não sobrecarregar
            uids_to_check = sorted(existing_uids & all_uids_server)[-500:]
            if uids_to_check:
                uid_str = ','.join(str(u) for u in uids_to_check)

                def _fetch_flags(conn):
                    return conn.uid('fetch', uid_str, '(UID FLAGS)')

                try:
                    _, flag_data = self._safe_exec(account, _fetch_flags)
                    updates = []
                    for item in flag_data:
                        if not isinstance(item, (tuple, list)):
                            continue
                        meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                        uid_match = re.search(r'UID (\d+)', meta)
                        if not uid_match:
                            continue
                        uid = int(uid_match.group(1))
                        server_read = '\\Seen' in meta
                        row = existing_uid_map.get(uid)
                        if row and row['is_read'] != server_read:
                            updates.append((row['id'], server_read))

                    if updates:
                        for email_id, is_read in updates:
                            _db.session.query(Email).filter_by(id=email_id).update(
                                {'is_read': is_read}, synchronize_session=False
                            )
                        _db.session.commit()
                        flag_changes = len(updates)
                        log.info(f'{folder}: {flag_changes} flags atualizadas')
                        if flag_changes > 0:
                            self._emit('flags_updated', {
                                'account_id': account.id,
                                'folder': folder,
                                'updates': [{'id': eid, 'is_read': r} for eid, r in updates],
                            })
                except Exception as e:
                    log.warning(f'Flag sync error for {folder}: {e}')

        except Exception as e:
            log.error(f'_sync_folder {folder} error: {e}')
            _pool.invalidate(account.id)
            try:
                from database import db as _db2
                _db2.session.rollback()
            except Exception:
                pass

        return new_count, flag_changes

    def _flush_batch(self, _db, account, batch):
        try:
            for email_obj, _ in batch:
                _db.session.add(email_obj)
            _db.session.flush()

            for email_obj, att_data in batch:
                for a in att_data:
                    _db.session.add(Attachment(
                        email_id=email_obj.id,
                        filename=a['filename'],
                        content_type=a['content_type'],
                        size=a['size'],
                        data=a['data'],
                    ))

            _db.session.commit()

            for email_obj, _ in batch:
                if email_obj.folder.upper() == 'INBOX':
                    self._emit('new_email', {
                        'account_id': account.id,
                        'folder':     email_obj.folder,
                        'email':      email_obj.to_list_dict(),
                    })
        except Exception as e:
            log.error(f'_flush_batch error: {e}')
            try:
                _db.session.rollback()
            except Exception:
                pass

    # ── Flag operations ────────────────────────────────────────────────────────

    def mark_read_on_server(self, email_obj: Email):
        try:
            def _mark(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Seen')
            self._safe_exec(email_obj.account, _mark)
        except Exception as e:
            log.warning(f'mark_read error: {e}')

    def mark_unread_on_server(self, email_obj: Email):
        try:
            def _mark(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('store', str(email_obj.uid), '-FLAGS', '\\Seen')
            self._safe_exec(email_obj.account, _mark)
        except Exception as e:
            log.warning(f'mark_unread error: {e}')

    def set_spam(self, email_obj: Email, mark_as_spam: bool):
        from database import db as _db
        try:
            account = email_obj.account
            target  = self._find_folder(account, ['spam', 'junk']) if mark_as_spam else 'INBOX'
            if not target:
                log.warning('Pasta de spam não encontrada')
                return False
            if email_obj.folder.lower() == target.lower():
                return True

            def _move(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('copy', str(email_obj.uid), f'"{target}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
                conn.expunge()

            self._safe_exec(account, _move)
            email_obj.folder = target
            _db.session.commit()
            return True
        except Exception as e:
            log.warning(f'set_spam error: {e}')
            return False

    def delete_email(self, email_obj: Email):
        try:
            def _delete(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
                conn.expunge()
            self._safe_exec(email_obj.account, _delete)
        except Exception as e:
            log.warning(f'delete_email error: {e}')

    def move_email(self, email_obj: Email, target_folder: str):
        from database import db as _db
        try:
            def _move(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('copy', str(email_obj.uid), f'"{target_folder}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
                conn.expunge()
            self._safe_exec(email_obj.account, _move)
            email_obj.folder = target_folder
            _db.session.commit()
        except Exception as e:
            log.warning(f'move_email error: {e}')

    # ── Send ──────────────────────────────────────────────────────────────────

    def send_email(self, account: Account, to: str, cc: str, bcc: str,
                   subject: str, body: str, reply_to_id=None, attachments=None):
        try:
            msg = MIMEMultipart('mixed')
            msg['From']    = f'{account.name} <{account.email}>'
            msg['To']      = to
            if cc: msg['Cc'] = cc
            msg['Subject'] = subject

            if reply_to_id:
                orig = Email.query.get(reply_to_id)
                if orig and orig.message_id:
                    msg['In-Reply-To'] = orig.message_id
                    msg['References']  = orig.message_id

            msg.attach(MIMEText(body, 'plain', 'utf-8'))

            if attachments:
                for f in attachments:
                    if not f or not f.filename:
                        continue
                    data = f.read()
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(data)
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
                if account.smtp_tls:
                    s.starttls()
            s.login(account.username, account.get_password())
            s.sendmail(account.email, recipients, msg.as_string())
            s.quit()

            # Salva na pasta Sent via pool
            try:
                sent_folder = self._find_folder(account, ['sent'])
                if sent_folder:
                    raw = msg.as_bytes()
                    def _append(conn):
                        conn.append(f'"{sent_folder}"', '\\Seen', None, raw)
                    self._safe_exec(account, _append)
            except Exception as e:
                log.warning(f'Could not save to Sent: {e}')

            return {'ok': True}
        except Exception as e:
            log.error(f'send_email error: {e}')
            return {'ok': False, 'error': str(e)}