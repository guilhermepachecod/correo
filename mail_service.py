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
from datetime import datetime

from database import Email, Attachment, Account

log = logging.getLogger(__name__)


def safe_decode(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', errors='replace')
    return str(v or '')


def _decode_header_value(value):
    if not value:
        return ''
    parts = decode_header(value)
    out = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                out.append(part.decode(charset or 'utf-8', errors='replace'))
            except:
                out.append(part.decode('latin-1', errors='replace'))
        else:
            out.append(str(part))
    return ''.join(out)


def _get_body(msg):
    text, html = '', ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get('Content-Disposition', ''))
            if 'attachment' in cd:
                continue
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or 'utf-8'
            body = payload.decode(charset, errors='replace') if payload else ''
            if ct == 'text/plain' and not text:
                text = body
            elif ct == 'text/html' and not html:
                html = body
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or 'utf-8'
        body = payload.decode(charset, errors='replace') if payload else ''
        if msg.get_content_type() == 'text/html':
            html = body
        else:
            text = body
    return text, html


def _get_attachments(msg):
    out = []
    for part in msg.walk():
        cd = str(part.get('Content-Disposition', ''))
        if 'attachment' not in cd:
            continue
        filename = _decode_header_value(part.get_filename())
        data = part.get_payload(decode=True)
        if not data:
            continue
        out.append({
            'filename': filename,
            'content_type': part.get_content_type(),
            'size': len(data),
            'data': data
        })
    return out


class MailService:
    def __init__(self, db, socketio):
        self.db = db
        self.socketio = socketio

    def _emit(self, e, d):
        try:
            self.socketio.emit(e, d)
        except:
            pass

    def _imap_connect(self, account):
        conn = imaplib.IMAP4_SSL(account.imap_host, account.imap_port) if account.imap_ssl else imaplib.IMAP4(account.imap_host, account.imap_port)
        conn.login(account.username, account.get_password())
        return conn

    def test_connection(self, account):
        errors = []
        try:
            c = self._imap_connect(account)
            c.logout()
        except Exception as e:
            errors.append(str(e))
        try:
            s = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port) if account.smtp_ssl else smtplib.SMTP(account.smtp_host, account.smtp_port)
            if account.smtp_tls:
                s.starttls()
            s.login(account.username, account.get_password())
            s.quit()
        except Exception as e:
            errors.append(str(e))
        return {'ok': not errors, 'errors': errors}

    def get_folders(self, account):
        try:
            c = self._imap_connect(account)
            _, data = c.list()
            c.logout()
            folders = []
            for f in data:
                decoded = safe_decode(f)
                m = re.search(r'\) ".+" (.+)', decoded)
                if m:
                    folders.append(m.group(1))
            return folders or ['INBOX']
        except:
            return ['INBOX']

    def sync_account(self, account_id):
        from database import db as _db, Account as _Account
        account = _Account.query.get(account_id)
        if not account:
            return

        self._emit('sync_start', {'account_id': account_id})

        try:
            for folder in self._get_sync_folders(account):
                self._sync_folder(account, folder)

            account.last_sync = datetime.utcnow()
            _db.session.commit()

            self._emit('sync_done', {'account_id': account_id})
        except Exception as e:
            self._emit('sync_error', {'error': str(e)})

    def _get_sync_folders(self, account):
        folders = self.get_folders(account)
        priority = ['inbox', 'sent', 'draft', 'spam', 'junk', 'trash']
        ordered = []
        for p in priority:
            for f in folders:
                if p in f.lower() and f not in ordered:
                    ordered.append(f)
        for f in folders:
            if f not in ordered:
                ordered.append(f)
        return ordered[:10]

    def _sync_folder(self, account, folder):
        from database import db as _db

        c = self._imap_connect(account)
        c.select(f'"{folder}"', readonly=True)

        _, data = c.uid('search', None, 'ALL')
        uids = data[0].split() if data and data[0] else []

        uids = uids[-100:]

        existing = set(
            r[0] for r in _db.session.query(Email.uid).filter_by(account_id=account.id, folder=folder).all()
        )

        new = [u for u in uids if int(u) not in existing]
        if not new:
            c.logout()
            return

        uid_str = ','.join(u.decode() for u in new)
        _, msgs = c.uid('fetch', uid_str, '(RFC822 FLAGS)')

        batch = []
        att_batch = []

        i = 0
        while i < len(msgs):
            item = msgs[i]
            if not isinstance(item, tuple):
                i += 1
                continue

            meta = safe_decode(item[0])
            raw = item[1]

            try:
                uid = int(meta.split('UID ')[-1].split()[0])
                is_read = '\\Seen' in meta

                msg = email_lib.message_from_bytes(raw)

                subject = _decode_header_value(msg.get('Subject'))
                from_full = _decode_header_value(msg.get('From'))
                from_name, from_addr = parseaddr(from_full)

                date = parsedate_to_datetime(msg.get('Date')) if msg.get('Date') else datetime.utcnow()
                if getattr(date, "tzinfo", None):
                    date = date.replace(tzinfo=None)

                text, html = _get_body(msg)
                attachments = _get_attachments(msg)

                email_obj = Email(
                    account_id=account.id,
                    folder=folder,
                    uid=uid,
                    subject=subject or '(sem assunto)',
                    from_addr=from_addr,
                    from_name=from_name,
                    date=date,
                    body_text=text,
                    body_html=html,
                    is_read=is_read,
                    has_attachments=bool(attachments)
                )

                _db.session.add(email_obj)
                _db.session.flush()

                for a in attachments:
                    att_batch.append(Attachment(
                        email_id=email_obj.id,
                        filename=a['filename'],
                        content_type=a['content_type'],
                        size=a['size'],
                        data=a['data']
                    ))

                batch.append(email_obj)

                if len(batch) >= 50:
                    _db.session.add_all(att_batch)
                    _db.session.commit()
                    batch.clear()
                    att_batch.clear()

            except:
                _db.session.rollback()

            i += 1

        if batch:
            _db.session.add_all(att_batch)
            _db.session.commit()

        c.logout()

    def mark_read_on_server(self, email):
        try:
            c = self._imap_connect(email.account)
            c.select(f'"{email.folder}"')
            c.uid('store', str(email.uid), '+FLAGS', '\\Seen')
            c.logout()
        except:
            pass

    def mark_unread_on_server(self, email):
        try:
            c = self._imap_connect(email.account)
            c.select(f'"{email.folder}"')
            c.uid('store', str(email.uid), '-FLAGS', '\\Seen')
            c.logout()
        except:
            pass

    def delete_email(self, email):
        try:
            c = self._imap_connect(email.account)
            c.select(f'"{email.folder}"')
            c.uid('store', str(email.uid), '+FLAGS', '\\Deleted')
            c.expunge()
            c.logout()
        except:
            pass