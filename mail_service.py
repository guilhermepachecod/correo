import imaplib
import smtplib
import email as email_lib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
import logging
import re
from datetime import datetime, timezone

from database import Email, Attachment, Account

log = logging.getLogger(__name__)


def _decode_header_value(value):
    if not value:
        return ''
    parts = decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or 'utf-8', errors='replace'))
            except Exception:
                result.append(part.decode('latin-1', errors='replace'))
        else:
            result.append(str(part))
    return ''.join(result)


def _get_body(msg):
    text, html = '', ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get('Content-Disposition', ''))
            if 'attachment' in cd:
                continue
            if ct == 'text/plain' and not text:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                text = payload.decode(charset, errors='replace') if payload else ''
            elif ct == 'text/html' and not html:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                html = payload.decode(charset, errors='replace') if payload else ''
    else:
        ct = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or 'utf-8'
        body = payload.decode(charset, errors='replace') if payload else ''
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
            'filename': filename,
            'content_type': part.get_content_type(),
            'size': len(data),
            'data': data,
        })
    return attachments


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

    def test_connection(self, account: Account):
        errors = []
        # Test IMAP
        try:
            conn = self._imap_connect(account)
            conn.logout()
        except Exception as e:
            errors.append(f'IMAP: {e}')
        # Test SMTP
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
        if errors:
            return {'ok': False, 'errors': errors}
        return {'ok': True}

    def get_folders(self, account: Account):
        try:
            conn = self._imap_connect(account)
            _, folder_list = conn.list()
            conn.logout()
            folders = []
            for item in folder_list:
                if isinstance(item, bytes):
                    decoded = item.decode('utf-8', errors='replace')
                    parts = decoded.split('"/"')
                    if len(parts) >= 2:
                        name = parts[-1].strip().strip('"')
                        folders.append(name)
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
            for folder in folders_to_sync:
                self._sync_folder(account, folder)
            account.last_sync = datetime.utcnow()
            _db.session.commit()
            self._emit('sync_done', {'account_id': account_id, 'email': account.email})
        except Exception as e:
            log.error(f'sync_account error: {e}')
            self._emit('sync_error', {'account_id': account_id, 'error': str(e)})

    def _get_sync_folders(self, account: Account):
        folders = self.get_folders(account)
        priority = ['INBOX', 'Sent', 'Drafts', 'Junk', 'Spam', 'Trash']
        result = []
        for p in priority:
            for f in folders:
                if p.lower() in f.lower() and f not in result:
                    result.append(f)
        for f in folders:
            if f not in result:
                result.append(f)
        return result[:10]

    def _sync_folder(self, account: Account, folder: str):
        from database import db as _db
        try:
            conn = self._imap_connect(account)
            status, _ = conn.select(f'"{folder}"', readonly=True)
            if status != 'OK':
                conn.logout()
                return

            # Get UIDs already stored
            existing = set(
                row[0] for row in
                _db.session.query(Email.uid).filter_by(account_id=account.id, folder=folder).all()
            )

            _, data = conn.uid('search', None, 'ALL')
            all_uids = data[0].split() if data[0] else []
            # Fetch newest 200 emails
            uids_to_fetch = all_uids[-200:]
            new_uids = [u for u in uids_to_fetch if int(u) not in existing]

            if not new_uids:
                conn.logout()
                return

            uid_str = ','.join(u.decode() for u in new_uids)
            _, msg_data = conn.uid('fetch', uid_str, '(RFC822 FLAGS)')

            i = 0
            while i < len(msg_data):
                item = msg_data[i]
                if not isinstance(item, tuple):
                    i += 1
                    continue
                meta = item[0].decode() if isinstance(item[0], bytes) else item[0]
                raw = item[1]
                uid_match = re.search(r'UID (\d+)', meta)
                if not uid_match:
                    i += 1
                    continue
                uid = int(uid_match.group(1))
                is_read = '\\Seen' in meta

                try:
                    msg = email_lib.message_from_bytes(raw)
                    subject = _decode_header_value(msg.get('Subject', ''))
                    from_full = _decode_header_value(msg.get('From', ''))
                    from_name, from_addr = parseaddr(from_full)
                    to_addr = _decode_header_value(msg.get('To', ''))
                    cc_addr = _decode_header_value(msg.get('Cc', ''))
                    reply_to = _decode_header_value(msg.get('Reply-To', ''))
                    message_id = msg.get('Message-ID', '').strip()
                    in_reply_to = msg.get('In-Reply-To', '').strip()

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
                        has_attachments=len(att_data) > 0,
                    )
                    _db.session.add(email_obj)
                    _db.session.flush()

                    for a in att_data:
                        att = Attachment(
                            email_id=email_obj.id,
                            filename=a['filename'],
                            content_type=a['content_type'],
                            size=a['size'],
                            data=a['data'],
                        )
                        _db.session.add(att)

                    _db.session.commit()
                    self._emit('new_email', {
                        'account_id': account.id,
                        'folder': folder,
                        'email': email_obj.to_list_dict(),
                    })
                except Exception as e:
                    _db.session.rollback()
                    log.warning(f'Error parsing email UID {uid}: {e}')
                i += 1

            conn.logout()
        except Exception as e:
            log.error(f'_sync_folder {folder} error: {e}')

    def mark_read_on_server(self, email_obj: Email):
        try:
            account = email_obj.account
            conn = self._imap_connect(account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Seen')
            conn.logout()
        except Exception as e:
            log.warning(f'mark_read_on_server error: {e}')

    def delete_email(self, email_obj: Email):
        try:
            account = email_obj.account
            conn = self._imap_connect(account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
            conn.expunge()
            conn.logout()
        except Exception as e:
            log.warning(f'delete_email error: {e}')

    def move_email(self, email_obj: Email, target_folder: str):
        try:
            account = email_obj.account
            conn = self._imap_connect(account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('copy', str(email_obj.uid), f'"{target_folder}"')
            conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
            conn.expunge()
            conn.logout()
            email_obj.folder = target_folder
            from database import db as _db
            _db.session.commit()
        except Exception as e:
            log.warning(f'move_email error: {e}')

    def send_email(self, account: Account, to: str, cc: str, bcc: str,
                   subject: str, body: str, reply_to_id=None, attachments=None):
        try:
            msg = MIMEMultipart('alternative' if not attachments else 'mixed')
            msg['From'] = f'{account.name} <{account.email}>'
            msg['To'] = to
            if cc:
                msg['Cc'] = cc
            msg['Subject'] = subject

            if reply_to_id:
                from database import db as _db
                orig = Email.query.get(reply_to_id)
                if orig and orig.message_id:
                    msg['In-Reply-To'] = orig.message_id
                    msg['References'] = orig.message_id

            text_part = MIMEText(body, 'plain', 'utf-8')
            msg.attach(text_part)

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

            # Save to Sent folder via IMAP
            try:
                conn = self._imap_connect(account)
                folders = self.get_folders(account)
                sent_folder = next((f for f in folders if 'sent' in f.lower()), None)
                if sent_folder:
                    conn.append(f'"{sent_folder}"', '\\Seen', None, msg.as_bytes())
                conn.logout()
            except Exception as e:
                log.warning(f'Could not save to Sent: {e}')

            return {'ok': True}
        except Exception as e:
            log.error(f'send_email error: {e}')
            return {'ok': False, 'error': str(e)}
