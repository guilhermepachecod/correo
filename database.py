from flask_sqlalchemy import SQLAlchemy
from cryptography.fernet import Fernet
from datetime import datetime
import base64, hashlib

db = SQLAlchemy()

_SECRET = b'mailapp-fernet-key-v1-stable-2024'
_KEY = base64.urlsafe_b64encode(hashlib.sha256(_SECRET).digest())
_fernet = Fernet(_KEY)


class Account(db.Model):
    __tablename__ = 'accounts'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120))
    email = db.Column(db.String(255), unique=True)
    imap_host = db.Column(db.String(255))
    imap_port = db.Column(db.Integer, default=993)
    imap_ssl = db.Column(db.Boolean, default=True)
    smtp_host = db.Column(db.String(255))
    smtp_port = db.Column(db.Integer, default=587)
    smtp_ssl = db.Column(db.Boolean, default=False)
    smtp_tls = db.Column(db.Boolean, default=True)
    username = db.Column(db.String(255))
    password_enc = db.Column(db.Text)
    conf_file = db.Column(db.String(255))
    last_sync = db.Column(db.DateTime)

    emails = db.relationship('Email', backref='account', cascade='all, delete-orphan')

    def set_password(self, p):
        self.password_enc = _fernet.encrypt(p.encode()).decode()

    def get_password(self):
        return _fernet.decrypt(self.password_enc.encode()).decode()

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "smtp_host": self.smtp_host,
        }


class Email(db.Model):
    __tablename__ = 'emails'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'))
    folder = db.Column(db.String(255))
    uid = db.Column(db.Integer)

    subject = db.Column(db.Text)
    from_addr = db.Column(db.Text)
    from_name = db.Column(db.Text)
    date = db.Column(db.DateTime)

    body_text = db.Column(db.Text)
    body_html = db.Column(db.Text)

    is_read = db.Column(db.Boolean, default=False)
    has_attachments = db.Column(db.Boolean, default=False)

    attachments = db.relationship('Attachment', backref='email', cascade='all, delete-orphan')

    __table_args__ = (db.UniqueConstraint('account_id', 'folder', 'uid'),)

    def to_list(self):
        return {
            "id": self.id,
            "subject": self.subject,
            "from": self.from_addr,
            "date": self.date.isoformat() if self.date else None,
            "is_read": self.is_read,
            "preview": (self.body_text or "")[:120]
        }

    def to_dict(self):
        d = self.to_list()
        d.update({
            "body_text": self.body_text,
            "body_html": self.body_html,
            "attachments": [a.to_dict() for a in self.attachments]
        })
        return d


class Attachment(db.Model):
    __tablename__ = 'attachments'

    id = db.Column(db.Integer, primary_key=True)
    email_id = db.Column(db.Integer, db.ForeignKey('emails.id'))
    filename = db.Column(db.String(255))
    content_type = db.Column(db.String(255))
    size = db.Column(db.Integer)
    data = db.Column(db.LargeBinary)

    def to_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "size": self.size
        }