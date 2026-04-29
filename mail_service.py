import imaplib, smtplib, email as email_lib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import parseaddr, parsedate_to_datetime
from database import Email, Attachment, Account
from datetime import datetime

class MailService:
    def __init__(self, db, socketio):
        self.db = db
        self.socketio = socketio

    def _imap(self, acc):
        c = imaplib.IMAP4_SSL(acc.imap_host, acc.imap_port)
        c.login(acc.username, acc.get_password())
        return c

    def sync_account(self, acc_id):
        from database import db as _db
        acc = Account.query.get(acc_id)
        c = self._imap(acc)
        c.select("INBOX")

        _, data = c.uid("search", None, "ALL")
        uids = data[0].split()[-100:]

        existing = {e.uid for e in Email.query.filter_by(account_id=acc.id).all()}

        for u in uids:
            uid = int(u)
            if uid in existing:
                continue

            _, msg = c.uid("fetch", u, "(RFC822)")
            raw = msg[0][1]

            m = email_lib.message_from_bytes(raw)

            e = Email(
                account_id=acc.id,
                folder="INBOX",
                uid=uid,
                subject=m.get("Subject", ""),
                from_addr=parseaddr(m.get("From"))[1],
                date=datetime.utcnow(),
                body_text=str(m.get_payload())
            )
            _db.session.add(e)

        _db.session.commit()
        c.logout()

    def send_email(self, account, to, subject, body):
        acc = Account.query.get(account)

        msg = MIMEMultipart()
        msg["From"] = acc.email
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        s = smtplib.SMTP(acc.smtp_host, acc.smtp_port)
        s.starttls()
        s.login(acc.username, acc.get_password())
        s.sendmail(acc.email, [to], msg.as_string())
        s.quit()

        return {"ok": True}