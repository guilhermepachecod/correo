from flask_sqlalchemy import SQLAlchemy
from cryptography.fernet import Fernet, InvalidToken
from datetime import datetime
import os

db = SQLAlchemy()

_fernet_key_env = os.environ.get('CORREO_FERNET_KEY')
if not _fernet_key_env:
    raise RuntimeError(
        'CORREO_FERNET_KEY não definida. Gere uma com scripts/gen_keys.py '
        'e coloque no .env antes de subir a aplicação.'
    )
try:
    _fernet = Fernet(_fernet_key_env.encode())
except Exception as e:
    raise RuntimeError(f'CORREO_FERNET_KEY inválida (precisa ser uma chave Fernet base64): {e}')

ATTACHMENTS_DIR        = os.environ.get('ATTACHMENTS_DIR', '/app/database/attachments')
MAX_EMAILS_PER_ACCOUNT = int(os.environ.get('MAX_EMAILS_PER_ACCOUNT', 2000))
MAX_ACCOUNTS           = int(os.environ.get('MAX_ACCOUNTS', 20))


class Account(db.Model):
    __tablename__ = 'accounts'
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(120), nullable=False)
    email        = db.Column(db.String(255), nullable=False, unique=True)
    imap_host    = db.Column(db.String(255), nullable=False)
    imap_port    = db.Column(db.Integer, default=993)
    imap_ssl     = db.Column(db.Boolean, default=True)
    smtp_host    = db.Column(db.String(255), nullable=False)
    smtp_port    = db.Column(db.Integer, default=587)
    smtp_ssl     = db.Column(db.Boolean, default=False)
    smtp_tls     = db.Column(db.Boolean, default=True)
    username     = db.Column(db.String(255), nullable=False)
    password_enc = db.Column(db.Text, nullable=False)
    conf_file    = db.Column(db.String(255), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    last_sync    = db.Column(db.DateTime, nullable=True)
    # Preenchidos pelo gerenciador de conexão (Fase 2) — circuit breaker de
    # autenticação: enquanto auth_error não for nulo, nenhuma tentativa nova
    # de login é feita automaticamente (evita loop de ban no fail2ban).
    auth_error    = db.Column(db.Text, nullable=True)
    auth_error_at = db.Column(db.DateTime, nullable=True)
    capabilities  = db.Column(db.Text, nullable=True)  # JSON da CAPABILITY do servidor
    emails  = db.relationship('Email', backref='account', lazy='dynamic', cascade='all, delete-orphan')
    filters = db.relationship('EmailFilter', backref='account', lazy='dynamic', cascade='all, delete-orphan')

    def set_password(self, plain: str):
        self.password_enc = plain[4:] if plain.startswith('enc:') else _fernet.encrypt(plain.encode()).decode()

    def get_password(self) -> str:
        """Descriptografa com a chave atual; se falhar, tenta a chave legada
        e, em caso de sucesso, recriptografa com a chave atual (migração
        transparente — ver legacy_crypto.py e scripts/rotate_key.py)."""
        try:
            return _fernet.decrypt(self.password_enc.encode()).decode()
        except InvalidToken:
            from legacy_crypto import legacy_fernet
            plain = legacy_fernet.decrypt(self.password_enc.encode()).decode()
            self.password_enc = _fernet.encrypt(plain.encode()).decode()
            db.session.commit()
            return plain

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'email': self.email,
                'imap_host': self.imap_host, 'imap_port': self.imap_port, 'imap_ssl': self.imap_ssl,
                'smtp_host': self.smtp_host, 'smtp_port': self.smtp_port, 'smtp_ssl': self.smtp_ssl,
                'smtp_tls': self.smtp_tls, 'username': self.username, 'conf_file': self.conf_file,
                'last_sync': self.last_sync.isoformat() if self.last_sync else None}


class Email(db.Model):
    __tablename__ = 'emails'
    id          = db.Column(db.Integer, primary_key=True)
    account_id  = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    message_id  = db.Column(db.String(500), nullable=True)
    folder      = db.Column(db.String(255), default='INBOX')
    uid         = db.Column(db.Integer, nullable=True)
    thread_id   = db.Column(db.String(500), nullable=True, index=True)
    subject     = db.Column(db.Text, default='(sem assunto)')
    from_addr   = db.Column(db.Text, default='')
    from_name   = db.Column(db.String(500), default='')
    to_addr     = db.Column(db.Text, default='')
    cc_addr     = db.Column(db.Text, default='')
    reply_to    = db.Column(db.Text, default='')
    date        = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    body_text   = db.Column(db.Text, default='')
    body_html   = db.Column(db.Text, default='')
    is_read         = db.Column(db.Boolean, default=False)
    is_starred      = db.Column(db.Boolean, default=False)
    has_attachments = db.Column(db.Boolean, default=False)
    in_reply_to     = db.Column(db.String(500), nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    # body_cached=false quando corpo/anexos foram podados (Fase 2.7) — ainda
    # não há nada que escreva false; reservado para a poda de armazenamento.
    body_cached     = db.Column(db.Boolean, default=True)
    attachments = db.relationship('Attachment', backref='email', lazy='dynamic', cascade='all, delete-orphan')
    __table_args__ = (
        db.UniqueConstraint('account_id', 'folder', 'uid', name='uq_email_uid'),
        db.Index('ix_email_account_folder_date', 'account_id', 'folder', 'date'),
    )

    def to_list_dict(self):
        return {'id': self.id, 'account_id': self.account_id, 'folder': self.folder,
                'subject': self.subject, 'from_addr': self.from_addr, 'from_name': self.from_name,
                'to_addr': self.to_addr, 'date': self.date.isoformat() if self.date else None,
                'is_read': self.is_read, 'is_starred': self.is_starred,
                'has_attachments': self.has_attachments, 'thread_id': self.thread_id,
                'preview': (self.body_text or '')[:120].strip()}

    def to_dict(self):
        d = self.to_list_dict()
        d.update({'cc_addr': self.cc_addr, 'reply_to': self.reply_to,
                  'body_text': self.body_text, 'body_html': self.body_html,
                  'in_reply_to': self.in_reply_to, 'message_id': self.message_id,
                  'attachments': [a.to_dict() for a in self.attachments]})
        return d


class Attachment(db.Model):
    __tablename__ = 'attachments'
    id           = db.Column(db.Integer, primary_key=True)
    email_id     = db.Column(db.Integer, db.ForeignKey('emails.id'), nullable=False)
    filename     = db.Column(db.String(500), nullable=False)
    content_type = db.Column(db.String(255), default='application/octet-stream')
    size         = db.Column(db.Integer, default=0)
    file_path    = db.Column(db.String(1000), nullable=True)
    data         = db.Column(db.LargeBinary, nullable=True)

    def get_data(self) -> bytes:
        if self.file_path:
            with open(os.path.join(ATTACHMENTS_DIR, self.file_path), 'rb') as f:
                return f.read()
        return self.data or b''

    def save_to_disk(self, raw: bytes):
        os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
        full_dir = os.path.join(ATTACHMENTS_DIR, str(self.email_id or 'tmp'))
        os.makedirs(full_dir, exist_ok=True)
        safe_name = ''.join(c for c in self.filename if c.isalnum() or c in '._- ')[:200]
        path = os.path.join(full_dir, safe_name)
        base, ext = os.path.splitext(path)
        counter = 1
        while os.path.exists(path):
            path = f'{base}_{counter}{ext}'; counter += 1
        with open(path, 'wb') as f:
            f.write(raw)
        self.file_path = os.path.relpath(path, ATTACHMENTS_DIR)
        self.data = None

    def delete_from_disk(self):
        if self.file_path:
            full = os.path.join(ATTACHMENTS_DIR, self.file_path)
            try:
                os.remove(full)
                try: os.rmdir(os.path.dirname(full))
                except OSError: pass
            except FileNotFoundError: pass

    def to_dict(self):
        return {'id': self.id, 'filename': self.filename,
                'content_type': self.content_type, 'size': self.size}


class EmailFilter(db.Model):
    __tablename__ = 'email_filters'
    id         = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    name       = db.Column(db.String(255), nullable=False)
    enabled    = db.Column(db.Boolean, default=True)
    priority   = db.Column(db.Integer, default=0)
    cond_from        = db.Column(db.String(500), nullable=True)
    cond_to          = db.Column(db.String(500), nullable=True)
    cond_subject     = db.Column(db.String(500), nullable=True)
    cond_has_attach  = db.Column(db.Boolean, nullable=True)
    action_move_to   = db.Column(db.String(255), nullable=True)
    action_mark_read = db.Column(db.Boolean, default=False)
    action_star      = db.Column(db.Boolean, default=False)
    action_delete    = db.Column(db.Boolean, default=False)
    action_label     = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def matches(self, email: 'Email') -> bool:
        if self.cond_from    and self.cond_from.lower()    not in (email.from_addr or '').lower(): return False
        if self.cond_to      and self.cond_to.lower()      not in (email.to_addr   or '').lower(): return False
        if self.cond_subject and self.cond_subject.lower() not in (email.subject   or '').lower(): return False
        if self.cond_has_attach is not None and email.has_attachments != self.cond_has_attach: return False
        return True

    def to_dict(self):
        return {'id': self.id, 'account_id': self.account_id, 'name': self.name,
                'enabled': self.enabled, 'priority': self.priority,
                'cond_from': self.cond_from, 'cond_to': self.cond_to,
                'cond_subject': self.cond_subject, 'cond_has_attach': self.cond_has_attach,
                'action_move_to': self.action_move_to, 'action_mark_read': self.action_mark_read,
                'action_star': self.action_star, 'action_delete': self.action_delete}


class FolderState(db.Model):
    """Estado de sincronização por pasta IMAP (Fase 2). uidvalidity/uidnext/
    highestmodseq só passam a ser preenchidos pelo algoritmo de sync
    incremental (2.4) — por enquanto só `role` é escrito, por
    services.mail_service.refresh_folder_roles (2.3)."""
    __tablename__ = 'folder_state'
    id            = db.Column(db.Integer, primary_key=True)
    account_id    = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    folder        = db.Column(db.String(255), nullable=False)
    uidvalidity   = db.Column(db.Integer, nullable=True)
    uidnext       = db.Column(db.Integer, nullable=True)
    highestmodseq = db.Column(db.Integer, nullable=True)
    last_sync     = db.Column(db.DateTime, nullable=True)
    role          = db.Column(db.String(20), nullable=True)  # inbox|sent|trash|junk|drafts|archive
    __table_args__ = (
        db.UniqueConstraint('account_id', 'folder', name='uq_folder_state_account_folder'),
    )

    def to_dict(self):
        return {'folder': self.folder, 'role': self.role}


class AppSettings(db.Model):
    """Preferências globais da aplicação — salvas no banco, sincronizadas entre dispositivos."""
    __tablename__ = 'app_settings'
    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False, default='')

    @staticmethod
    def get(key: str, default=None):
        row = AppSettings.query.get(key)
        return row.value if row else default

    @staticmethod
    def set(key: str, value: str):
        row = AppSettings.query.get(key)
        if row:
            row.value = value
        else:
            db.session.add(AppSettings(key=key, value=value))
        db.session.commit()

    @staticmethod
    def get_all() -> dict:
        return {row.key: row.value for row in AppSettings.query.all()}


def enforce_email_limit(account_id: int) -> int:
    total = db.session.query(db.func.count(Email.id)).filter_by(account_id=account_id).scalar() or 0
    if total <= MAX_EMAILS_PER_ACCOUNT:
        return 0
    old_emails = (db.session.query(Email).filter_by(account_id=account_id)
                             .order_by(Email.date.asc()).limit(total - MAX_EMAILS_PER_ACCOUNT).all())
    for email in old_emails:
        for att in email.attachments: att.delete_from_disk()
        db.session.delete(email)
    db.session.commit()
    return len(old_emails)


def _migration_001_legacy(conn) -> None:
    """Migrações antigas (pré-Fase 2), convertidas para o formato versionado
    sem mudar o efeito: cada ALTER/CREATE é tentado e ignorado se já aplicado
    (ex.: ALTER TABLE ... ADD COLUMN falha com "duplicate column" se a coluna
    já existe — é o próprio mecanismo de idempotência que o código anterior
    já usava)."""
    statements = [
        "ALTER TABLE accounts ADD COLUMN conf_file VARCHAR(255)",
        "ALTER TABLE emails ADD COLUMN thread_id VARCHAR(500)",
        "ALTER TABLE attachments ADD COLUMN file_path VARCHAR(1000)",
        """CREATE TABLE IF NOT EXISTS attachments_new (
            id INTEGER PRIMARY KEY,
            email_id INTEGER NOT NULL REFERENCES emails(id),
            filename VARCHAR(500) NOT NULL,
            content_type VARCHAR(255) DEFAULT 'application/octet-stream',
            size INTEGER DEFAULT 0,
            file_path VARCHAR(1000),
            data BLOB
        )""",
        "INSERT OR IGNORE INTO attachments_new SELECT id, email_id, filename, content_type, size, file_path, data FROM attachments",
        "DROP TABLE IF EXISTS attachments_old",
        "ALTER TABLE attachments RENAME TO attachments_old",
        "ALTER TABLE attachments_new RENAME TO attachments",
        "DROP TABLE IF EXISTS attachments_old",
        "CREATE INDEX IF NOT EXISTS ix_email_account_folder_date ON emails (account_id, folder, date)",
    ]
    for sql in statements:
        try:
            conn.execute(db.text(sql)); conn.commit()
        except Exception:
            pass


def _migration_002_folder_state(conn) -> None:
    """Fase 2.1: tabela folder_state + colunas novas em emails/accounts."""
    statements = [
        """CREATE TABLE IF NOT EXISTS folder_state (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            folder VARCHAR(255) NOT NULL,
            uidvalidity INTEGER,
            uidnext INTEGER,
            highestmodseq INTEGER,
            last_sync DATETIME,
            role VARCHAR(20),
            UNIQUE(account_id, folder)
        )""",
        "ALTER TABLE emails ADD COLUMN body_cached BOOLEAN DEFAULT 1",
        "ALTER TABLE accounts ADD COLUMN auth_error TEXT",
        "ALTER TABLE accounts ADD COLUMN auth_error_at DATETIME",
        "ALTER TABLE accounts ADD COLUMN capabilities TEXT",
    ]
    for sql in statements:
        try:
            conn.execute(db.text(sql)); conn.commit()
        except Exception:
            pass


# Lista ordenada de migrações. Cada uma roda exatamente uma vez por banco —
# a versão aplicada fica registrada em schema_version. Para adicionar uma
# migração nova, só acrescentar uma tupla (N, fn) no fim, nunca remover ou
# reordenar as existentes.
MIGRATIONS = [
    (1, _migration_001_legacy),
    (2, _migration_002_folder_state),
]


def run_migrations(db_instance) -> None:
    with db_instance.engine.connect() as conn:
        conn.execute(db_instance.text(
            'CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)'
        ))
        conn.commit()
        applied = {
            row[0] for row in
            conn.execute(db_instance.text('SELECT version FROM schema_version'))
        }
        for version, migration_fn in MIGRATIONS:
            if version in applied:
                continue
            migration_fn(conn)
            conn.execute(
                db_instance.text('INSERT INTO schema_version (version) VALUES (:v)'),
                {'v': version},
            )
            conn.commit()


def resolve_thread_id(message_id: str, in_reply_to: str) -> str:
    if in_reply_to:
        parent = db.session.query(Email.thread_id).filter(Email.message_id == in_reply_to).first()
        if parent and parent.thread_id:
            return parent.thread_id
    return message_id or ''