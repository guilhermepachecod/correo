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


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------

def _decode_header_value(value):
    if not value:
        return ''

    result = []

    for part, charset in decode_header(value):
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or 'utf-8', errors='replace'))
            except Exception:
                result.append(part.decode('latin-1', errors='replace'))
        else:
            result.append(str(part))

    return ''.join(result)


def _get_body(msg):
    text = ''
    html = ''

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get('Content-Disposition', ''))

            if 'attachment' in disp.lower():
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            charset = part.get_content_charset() or 'utf-8'

            try:
                content = payload.decode(charset, errors='replace')
            except Exception:
                content = payload.decode('utf-8', errors='replace')

            if ctype == 'text/plain' and not text:
                text = content

            elif ctype == 'text/html' and not html:
                html = content
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or 'utf-8'

        if payload:
            content = payload.decode(charset, errors='replace')
        else:
            content = ''

        if msg.get_content_type() == 'text/html':
            html = content
        else:
            text = content

    return text, html


def _get_attachments(msg):
    files = []

    for part in msg.walk():
        filename = part.get_filename()

        if not filename:
            continue

        data = part.get_payload(decode=True)

        if not data:
            continue

        files.append({
            'filename': _decode_header_value(filename),
            'content_type': part.get_content_type(),
            'size': len(data),
            'data': data
        })

    return files


# -----------------------------------------------------------------------------
# SERVICE
# -----------------------------------------------------------------------------

class MailService:

    def __init__(self, db, socketio):
        self.db = db
        self.socketio = socketio

    def _emit(self, event, data):
        try:
            self.socketio.emit(event, data)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # CONNECTIONS
    # -------------------------------------------------------------------------

    def _imap_connect(self, account: Account):
        if account.imap_ssl:
            conn = imaplib.IMAP4_SSL(
                account.imap_host,
                account.imap_port
            )
        else:
            conn = imaplib.IMAP4(
                account.imap_host,
                account.imap_port
            )

        conn.login(account.username, account.get_password())
        return conn

    def _smtp_connect(self, account: Account):
        if account.smtp_ssl:
            s = smtplib.SMTP_SSL(
                account.smtp_host,
                account.smtp_port,
                timeout=30
            )
        else:
            s = smtplib.SMTP(
                account.smtp_host,
                account.smtp_port,
                timeout=30
            )

            if account.smtp_tls:
                s.starttls()

        s.login(account.username, account.get_password())
        return s

    # -------------------------------------------------------------------------
    # TEST
    # -------------------------------------------------------------------------

    def test_connection(self, account: Account):
        errors = []

        try:
            c = self._imap_connect(account)
            c.logout()
        except Exception as e:
            errors.append(f'IMAP: {e}')

        try:
            s = self._smtp_connect(account)
            s.quit()
        except Exception as e:
            errors.append(f'SMTP: {e}')

        return {
            'ok': len(errors) == 0,
            'errors': errors
        }

    # -------------------------------------------------------------------------
    # FOLDERS
    # -------------------------------------------------------------------------

    def get_folders(self, account: Account):
        try:
            conn = self._imap_connect(account)

            status, data = conn.list()

            conn.logout()

            if status != 'OK':
                return ['INBOX']

            folders = []

            for item in data:
                if isinstance(item, bytes):
                    row = item.decode('utf-8', errors='replace')
                else:
                    row = str(item)

                m = re.search(r'"/"\s+"?(.*?)"?$', row)

                if not m:
                    m = re.search(r'\)\s+"?(.*?)"?$', row)

                if m:
                    folders.append(m.group(1).strip('"'))

            if not folders:
                folders = ['INBOX']

            return folders

        except Exception as e:
            log.warning(e)
            return ['INBOX']

    # -------------------------------------------------------------------------
    # SYNC
    # -------------------------------------------------------------------------

    def sync_account(self, account_id):
        from database import Account as DBAccount
        from database import db as _db

        account = DBAccount.query.get(account_id)

        if not account:
            return

        self._emit('sync_start', {'account_id': account.id})

        try:
            folders = self.get_folders(account)

            for folder in folders[:10]:
                self._sync_folder(account, folder)

            account.last_sync = datetime.utcnow()
            _db.session.commit()

            self._emit('sync_done', {'account_id': account.id})

        except Exception as e:
            log.error(e)
            self._emit('sync_error', {
                'account_id': account.id,
                'error': str(e)
            })

    def _sync_folder(self, account, folder):
        from database import db as _db

        try:
            conn = self._imap_connect(account)

            status, _ = conn.select(f'"{folder}"', readonly=True)

            if status != 'OK':
                conn.logout()
                return

            _, data = conn.uid('search', None, 'ALL')

            if not data or not data[0]:
                conn.logout()
                return

            uids = data[0].split()[-200:]

            existing = set(
                row[0] for row in
                _db.session.query(Email.uid)
                .filter_by(account_id=account.id, folder=folder)
                .all()
            )

            new_uids = [u for u in uids if int(u) not in existing]

            if not new_uids:
                conn.logout()
                return

            uid_csv = ','.join(x.decode() for x in new_uids)

            _, msgs = conn.uid('fetch', uid_csv, '(RFC822 FLAGS)')

            for item in msgs:
                if not isinstance(item, tuple):
                    continue

                meta = item[0].decode(errors='replace')
                raw = item[1]

                m = re.search(r'UID (\d+)', meta)

                if not m:
                    continue

                uid = int(m.group(1))
                is_read = '\\Seen' in meta

                try:
                    msg = email_lib.message_from_bytes(raw)

                    subject = _decode_header_value(
                        msg.get('Subject', '')
                    ) or '(sem assunto)'

                    from_full = _decode_header_value(
                        msg.get('From', '')
                    )

                    from_name, from_addr = parseaddr(from_full)

                    to_addr = _decode_header_value(
                        msg.get('To', '')
                    )

                    cc_addr = _decode_header_value(
                        msg.get('Cc', '')
                    )

                    message_id = msg.get('Message-ID', '').strip()

                    date_raw = msg.get('Date', '')

                    try:
                        dt = parsedate_to_datetime(date_raw)
                        if dt.tzinfo:
                            dt = dt.replace(tzinfo=None)
                    except Exception:
                        dt = datetime.utcnow()

                    body_text, body_html = _get_body(msg)

                    files = _get_attachments(msg)

                    row = Email(
                        account_id=account.id,
                        folder=folder,
                        uid=uid,
                        message_id=message_id,
                        subject=subject,
                        from_addr=from_addr,
                        from_name=from_name,
                        to_addr=to_addr,
                        cc_addr=cc_addr,
                        date=dt,
                        body_text=body_text,
                        body_html=body_html,
                        is_read=is_read,
                        has_attachments=len(files) > 0
                    )

                    _db.session.add(row)
                    _db.session.flush()

                    for f in files:
                        _db.session.add(
                            Attachment(
                                email_id=row.id,
                                filename=f['filename'],
                                content_type=f['content_type'],
                                size=f['size'],
                                data=f['data']
                            )
                        )

                    _db.session.commit()

                    self._emit('new_email', {
                        'account_id': account.id,
                        'folder': folder,
                        'email': row.to_list_dict()
                    })

                except Exception as e:
                    _db.session.rollback()
                    log.warning(e)

            conn.logout()

        except Exception as e:
            log.warning(e)

    # -------------------------------------------------------------------------
    # FLAGS
    # -------------------------------------------------------------------------

    def mark_read_on_server(self, email_obj):
        try:
            conn = self._imap_connect(email_obj.account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Seen')
            conn.logout()
        except Exception as e:
            log.warning(e)

    def mark_unread_on_server(self, email_obj):
        try:
            conn = self._imap_connect(email_obj.account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('store', str(email_obj.uid), '-FLAGS', '\\Seen')
            conn.logout()
        except Exception as e:
            log.warning(e)

    # -------------------------------------------------------------------------
    # MOVE / DELETE
    # -------------------------------------------------------------------------

    def delete_email(self, email_obj):
        try:
            conn = self._imap_connect(email_obj.account)
            conn.select(f'"{email_obj.folder}"')
            conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
            conn.expunge()
            conn.logout()
        except Exception as e:
            log.warning(e)

    def set_spam(self, email_obj, spam=True):
        from database import db as _db

        target = 'Spam' if spam else 'INBOX'

        try:
            conn = self._imap_connect(email_obj.account)

            conn.select(f'"{email_obj.folder}"')
            conn.uid('copy', str(email_obj.uid), f'"{target}"')
            conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
            conn.expunge()
            conn.logout()

            email_obj.folder = target
            _db.session.commit()

            return True

        except Exception as e:
            log.warning(e)
            return False

    def move_email(self, email_obj, target_folder):
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
            log.warning(e)

    # -------------------------------------------------------------------------
    # SEND
    # -------------------------------------------------------------------------

    def send_email(
        self,
        account,
        to='',
        cc='',
        bcc='',
        subject='',
        body='',
        reply_to_id=None,
        attachments=None
    ):
        try:
            msg = MIMEMultipart()

            msg['From'] = f'{account.name} <{account.email}>'
            msg['To'] = to

            if cc:
                msg['Cc'] = cc

            msg['Subject'] = subject

            if reply_to_id:
                orig = Email.query.get(reply_to_id)
                if orig and orig.message_id:
                    msg['In-Reply-To'] = orig.message_id
                    msg['References'] = orig.message_id

            msg.attach(
                MIMEText(body, 'plain', 'utf-8')
            )

            if attachments:
                for f in attachments:
                    if not f or not f.filename:
                        continue

                    data = f.read()

                    part = MIMEBase(
                        'application',
                        'octet-stream'
                    )

                    part.set_payload(data)
                    encoders.encode_base64(part)

                    part.add_header(
                        'Content-Disposition',
                        f'attachment; filename="{f.filename}"'
                    )

                    msg.attach(part)

            recipients = []

            for block in [to, cc, bcc]:
                if block:
                    recipients += [
                        x.strip()
                        for x in block.split(',')
                        if x.strip()
                    ]

            smtp = self._smtp_connect(account)

            smtp.sendmail(
                account.email,
                recipients,
                msg.as_string()
            )

            smtp.quit()

            return {'ok': True}

        except Exception as e:
            log.error(e)
            return {
                'ok': False,
                'error': str(e)
            }