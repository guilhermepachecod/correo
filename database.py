from flask_sqlalchemy import SQLAlchemy
from cryptography.fernet import Fernet
from datetime import datetime
import os
import base64
import hashlib

db = SQLAlchemy()

# Derive a stable encryption key from a fixed secret
_SECRET = b'mailapp-fernet-key-v1-stable-2024'
_KEY = base64.urlsafe_b64encode(hashlib.sha256(_SECRET).digest())
_fernet = Fernet(_KEY)


class Account(db.Model):
    __tablename__ = 'accounts'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True)
    imap_host = db.Column(db.String(255), nullable=False)
    imap_port = db.Column(db.Integer, default=993)
    imap_ssl = db.Column(db.Boolean, default=True)
    smtp_host = db.Column(db.String(255), nullable=False)
    smtp_port = db.Column(db.Integer, default=587)
    smtp_ssl = db.Column(db.Boolean, default=False)
    smtp_tls = db.Column(db.Boolean, default=True)
    username = db.Column(db.String(255), nullable=False)
    password_enc = db.Column(db.Text, nullable=False)
    conf_file = db.Column(db.String(255), nullable=True)  # nome do .conf associado
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_sync = db.Column(db.DateTime, nullable=True)

    emails = db.relationship('Email', backref='account', lazy='dynamic',
                             cascade='all, delete-orphan')

    def set_password(self, plain: str):
        # Se já vier com prefixo enc: (lido do .conf), descriptografa e re-criptografa
        if plain.startswith('enc:'):
            self.password_enc = plain[4:]
        else:
            self.password_enc = _fernet.encrypt(plain.encode()).decode()

    def get_password(self) -> str:
        return _fernet.decrypt(self.password_enc.encode()).decode()

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'email': self.email,
            'imap_host': self.imap_host,
            'imap_port': self.imap_port,
            'imap_ssl': self.imap_ssl,
            'smtp_host': self.smtp_host,
            'smtp_port': self.smtp_port,
            'smtp_ssl': self.smtp_ssl,
            'smtp_tls': self.smtp_tls,
            'username': self.username,
            'conf_file': self.conf_file,
            'last_sync': self.last_sync.isoformat() if self.last_sync else None,
        }


class Email(db.Model):
    __tablename__ = 'emails'

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    message_id = db.Column(db.String(500), nullable=True)
    folder = db.Column(db.String(255), default='INBOX')
    uid = db.Column(db.Integer, nullable=True)

    subject = db.Column(db.Text, default='(sem assunto)')
    from_addr = db.Column(db.Text, default='')
    from_name = db.Column(db.String(500), default='')
    to_addr = db.Column(db.Text, default='')
    cc_addr = db.Column(db.Text, default='')
    reply_to = db.Column(db.Text, default='')
    date = db.Column(db.DateTime, default=datetime.utcnow)

    body_text = db.Column(db.Text, default='')
    body_html = db.Column(db.Text, default='')

    is_read = db.Column(db.Boolean, default=False)
    is_starred = db.Column(db.Boolean, default=False)
    has_attachments = db.Column(db.Boolean, default=False)

    in_reply_to = db.Column(db.String(500), nullable=True)
    thread_id = db.Column(db.String(500), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    attachments = db.relationship('Attachment', backref='email', lazy='dynamic',
                                  cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('account_id', 'folder', 'uid', name='uq_email_uid'),
    )

    def to_list_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'folder': self.folder,
            'subject': self.subject,
            'from_addr': self.from_addr,
            'from_name': self.from_name,
            'to_addr': self.to_addr,
            'date': self.date.isoformat() if self.date else None,
            'is_read': self.is_read,
            'is_starred': self.is_starred,
            'has_attachments': self.has_attachments,
            'preview': (self.body_text or '')[:120].strip(),
        }

    def to_dict(self):
        d = self.to_list_dict()
        d.update({
            'cc_addr': self.cc_addr,
            'reply_to': self.reply_to,
            'body_text': self.body_text,
            'body_html': self.body_html,
            'in_reply_to': self.in_reply_to,
            'message_id': self.message_id,
            'attachments': [a.to_dict() for a in self.attachments],
        })
        return d


class Attachment(db.Model):
    __tablename__ = 'attachments'

    id = db.Column(db.Integer, primary_key=True)
    email_id = db.Column(db.Integer, db.ForeignKey('emails.id'), nullable=False)
    filename = db.Column(db.String(500), nullable=False)
    content_type = db.Column(db.String(255), default='application/octet-stream')
    size = db.Column(db.Integer, default=0)
    data = db.Column(db.LargeBinary, nullable=False)

    def to_dict(self):
        return {
            'id': self.id,
            'filename': self.filename,
            'content_type': self.content_type,
            'size': self.size,
        }