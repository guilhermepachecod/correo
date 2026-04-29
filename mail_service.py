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
from datetime import datetime

from database import Email, Attachment, Account

log = logging.getLogger(__name__)

BATCH_SIZE = 50  # commit a cada N emails


# ── Encoding helpers ───────────────────────────────────────────────────────────

def _decode_header_value(value):
    """Decodifica cabeçalhos MIME com fallback seguro para UTF-8/latin-1."""
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
    """Decodifica payload de uma parte MIME com suporte a quoted-printable e base64."""
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
    """Extrai body text e html de uma mensagem, tratando multipart corretamente."""
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
    """Extrai anexos da mensagem."""
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
            'filename': filename,
            'content_type': part.get_content_type(),
            'size': len(data),
            'data': data,
        })
    return attachments


# ── MailService ────────────────────────────────────────────────────────────────

class MailService:
    def __init__(self, db, socketio):
        self.db = db
        self.socketio = socketio

    def _emit(self, event, data):
        try:
            self.socketio.emit(event, data)
        except Exception:
            pass

    def _imap_connect(self, account: Account):
        if account.imap_ssl:
            conn = imaplib.IMAP4_SSL(account.imap_host, account.imap_port)
        else:
            conn = imaplib.IMAP4(account.imap_host, account.imap_port)
        conn.login(account.username, account.get_password())
        return conn

    def _find_folder(self, account: Account, keywords):
        """Encontra a primeira pasta que contém uma das keywords."""
        folders = self.get_folders(account)
        for kw in keywords:
            for f in folders:
                if kw in f.lower():
                    return f
        return None

    def test_connection(self, account: Account):
        errors = []
        try:
            conn = self._imap_connect(account)
            conn.logout()
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

    def get_folders(self, account: Account):
        try:
            conn = self._imap_connect(account)
            _, folder_list = conn.list()
            conn.logout()
            folders = []
            for item in folder_list:
                if isinstance(item, bytes):
                    decoded = item.decode('utf-8', errors='replace')
                    match = re.search(r'\(.*?\)\s+"?[./]"?\s+(.*)', decoded)
                    if match:
                        name = match.group(1).strip().strip('"')
                        folders.append(name)
            log.info(f'Pastas encontradas para {account.email}: {folders}')
            return folders
        except Exception as e:
            log.error(f'get_folders error: {e}')
            return ['INBOX']

    def sync_account(self, account_id: int):
        from database import db as _db, Account as _Account
        account = _Account.query.get(account_id)
        if not account:
            return
        log.info(f'Syncing account {account.email}')
        self._emit('sync_start', {'account_id': account_id, 'email': account.email})
        try:
            folders_to_sync = self._get_sync_folders(account)
            log.info(f'Sincronizando pastas: {folders_to_sync}')
            total_new = 0
            for folder in folders_to_sync:
                n = self._sync_folder(account, folder)
                total_new += n
            account.last_sync = datetime.utcnow()
            _db.session.commit()
            self._emit('sync_done', {
                'account_id': account_id,
                'email': account.email,
                'new': total_new,
            })
        except Exception as e:
            log.error(f'sync_account error: {e}')
            self._emit('sync_error', {'account_id': account_id, 'error': str(e)})

    def _get_sync_folders(self, account: Account):
        folders = self.get_folders(account)
        priority_keywords = ['inbox', 'sent', 'drafts', 'junk', 'spam', 'trash', 'archive']
        result = []
        for keyword in priority_keywords:
            for f in folders:
                if keyword in f.lower() and f not in result:
                    result.append(f)
        for f in folders:
            if f not in result:
                result.append(f)
        return result[:10]

    def _sync_folder(self, account: Account, folder: str) -> int:
        """Sincroniza uma pasta. Retorna quantidade de novos emails."""
        from database import db as _db
        new_count = 0
        try:
            conn = self._imap_connect(account)
            status, _ = conn.select(f'"{folder}"', readonly=True)
            if status != 'OK':
                log.warning(f'Não foi possível selecionar pasta: {folder}')
                conn.logout()
                return 0

            # Cache de UIDs já existentes no banco — evita re-sync redundante
            existing_uids = set(
                row[0] for row in
                _db.session.query(Email.uid)
                    .filter_by(account_id=account.id, folder=folder)
                    .all()
            )

            _, data = conn.uid('search', None, 'ALL')
            all_uids = data[0].split() if data[0] else []
            # Últimos 200 apenas
            uids_to_check = all_uids[-200:]
            new_uids = [u for u in uids_to_check if int(u) not in existing_uids]

            log.info(f'Pasta {folder}: {len(all_uids)} total, {len(new_uids)} novos')

            if not new_uids:
                conn.logout()
                return 0

            # Fetch otimizado: apenas headers primeiro (BODY.PEEK evita marcar como lido)
            uid_str = ','.join(u.decode() for u in new_uids)
            _, msg_data = conn.uid(
                'fetch', uid_str,
                '(UID FLAGS BODY.PEEK[])'
            )

            batch_objects = []
            batch_attachments = []
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
                    # Usa email.policy.default para melhor suporte a encoding
                    msg = email_lib.message_from_bytes(raw, policy=email_policy.compat32)

                    subject   = _decode_header_value(msg.get('Subject', ''))
                    from_full = _decode_header_value(msg.get('From', ''))
                    from_name, from_addr = parseaddr(from_full)
                    to_addr   = _decode_header_value(msg.get('To', ''))
                    cc_addr   = _decode_header_value(msg.get('Cc', ''))
                    reply_to  = _decode_header_value(msg.get('Reply-To', ''))
                    message_id  = (msg.get('Message-ID') or '').strip()
                    in_reply_to = (msg.get('In-Reply-To') or '').strip()

                    date_str = msg.get('Date', '')
                    try:
                        date = parsedate_to_datetime(date_str)
                        if date.tzinfo:
                            date = date.replace(tzinfo=None)
                    except Exception:
                        date = datetime.utcnow()

                    body_text, body_html = _get_body(msg)
                    att_data = _get_attachments(msg)

                    email_obj = Email(
                        account_id=account.id,
                        folder=folder,
                        uid=uid,
                        message_id=message_id,
                        in_reply_to=in_reply_to,
                        subject=subject or '(sem assunto)',
                        from_addr=from_addr,
                        from_name=from_name,
                        to_addr=to_addr,
                        cc_addr=cc_addr,
                        reply_to=reply_to,
                        date=date,
                        body_text=body_text,
                        body_html=body_html,
                        is_read=is_read,
                        has_attachments=bool(att_data),
                    )
                    batch_objects.append((email_obj, att_data))
                    new_count += 1

                    # Commit em lote a cada BATCH_SIZE emails
                    if len(batch_objects) >= BATCH_SIZE:
                        self._flush_batch(_db, account, batch_objects)
                        batch_objects = []

                except Exception as e:
                    log.warning(f'Error parsing email UID {uid}: {e}')

                i += 1

            # Flush do restante
            if batch_objects:
                self._flush_batch(_db, account, batch_objects)

            conn.logout()

        except Exception as e:
            log.error(f'_sync_folder {folder} error: {e}')
            try:
                _db.session.rollback()
            except Exception:
                pass

        return new_count

    def _flush_batch(self, _db, account, batch):
        """Persiste um lote de emails de uma vez."""
        try:
            for email_obj, att_data in batch:
                _db.session.add(email_obj)
            _db.session.flush()  # gera IDs sem commitar

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

            # Emite evento só para novos emails do INBOX
            for email_obj, _ in batch:
                if email_obj.folder.upper() == 'INBOX':
                    self._emit('new_email', {
                        'account_id': account.id,
                        'folder': email_obj.folder,
                        'email': email_obj.to_list_dict(),
                    })

        except Exception as e:
            log.error(f'_flush_batch error: {e}')
            try:
                _db.session.rollback()
            except Exception:
                pass

    # ── Server-side flags ──────────────────────────────────────────────────────

    def mark_read_on_server(self, email_obj: Email):
        try:
            conn = self._imap_connect(email_obj.account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Seen')
            conn.logout()
        except Exception as e:
            log.warning(f'mark_read_on_server error: {e}')

    def mark_unread_on_server(self, email_obj: Email):
        try:
            conn = self._imap_connect(email_obj.account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('store', str(email_obj.uid), '-FLAGS', '\\Seen')
            conn.logout()
        except Exception as e:
            log.warning(f'mark_unread_on_server error: {e}')

    def set_spam(self, email_obj: Email, mark_as_spam: bool):
        from database import db as _db
        try:
            account = email_obj.account
            if mark_as_spam:
                target = self._find_folder(account, ['spam', 'junk'])
                if not target:
                    log.warning('Pasta de spam não encontrada')
                    return False
            else:
                target = 'INBOX'

            if email_obj.folder.lower() == target.lower():
                return True

            conn = self._imap_connect(account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('copy', str(email_obj.uid), f'"{target}"')
            conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
            conn.expunge()
            conn.logout()

            email_obj.folder = target
            _db.session.commit()
            return True
        except Exception as e:
            log.warning(f'set_spam error: {e}')
            return False

    def delete_email(self, email_obj: Email):
        try:
            conn = self._imap_connect(email_obj.account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
            conn.expunge()
            conn.logout()
        except Exception as e:
            log.warning(f'delete_email error: {e}')

    def move_email(self, email_obj: Email, target_folder: str):
        from database import db as _db
        try:
            conn = self._imap_connect(email_obj.account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('copy', str(email_obj.uid), f'"{target_folder}"')
            conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
            conn.expunge()
            conn.logout()
            email_obj.folder = target_folder
            _db.session.commit()
        except Exception as e:
            log.warning(f'move_email error: {e}')

    def send_email(self, account: Account, to: str, cc: str, bcc: str,
                   subject: str, body: str, reply_to_id=None, attachments=None):
        try:
            msg = MIMEMultipart('mixed')
            msg['From']    = f'{account.name} <{account.email}>'
            msg['To']      = to
            if cc:
                msg['Cc']  = cc
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
            if cc:
                recipients += [r.strip() for r in cc.split(',') if r.strip()]
            if bcc:
                recipients += [r.strip() for r in bcc.split(',') if r.strip()]

            if account.smtp_ssl:
                s = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=30)
            else:
                s = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30)
                if account.smtp_tls:
                    s.starttls()
            s.login(account.username, account.get_password())
            s.sendmail(account.email, recipients, msg.as_string())
            s.quit()

            # Salva na pasta Sent via IMAP
            try:
                conn = self._imap_connect(account)
                sent_folder = self._find_folder(account, ['sent'])
                if sent_folder:
                    conn.append(f'"{sent_folder}"', '\\Seen', None, msg.as_bytes())
                conn.logout()
            except Exception as e:
                log.warning(f'Could not save to Sent: {e}')

            return {'ok': True}
        except Exception as e:
            log.error(f'send_email error: {e}')
            return {'ok': False, 'error': str(e)}