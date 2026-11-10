"""
app.py
Ponto de entrada. Apenas: configuração, init de extensões,
registro de blueprints e arranque do IDLE manager.
"""
import os
import threading
import logging
from pathlib import Path

from flask import Flask, render_template, request, session, jsonify, redirect
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Rotas liberadas sem sessão autenticada.
_PUBLIC_PATHS   = {'/login', '/api/login', '/healthz'}
_PUBLIC_PREFIXES = ('/static/',)


def _require_secret(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f'{name} não definida. Gere as chaves com scripts/gen_keys.py '
            f'e defina as variáveis no .env antes de subir a aplicação.'
        )
    return value


def create_app() -> Flask:
    app = Flask(__name__)

    allowed_origins = [o.strip() for o in
                       os.environ.get('CORREO_ALLOWED_ORIGINS', '').split(',') if o.strip()] or None
    cookie_secure = os.environ.get('CORREO_COOKIE_SECURE', 'false').lower() in ('1', 'true', 'yes')

    app.config.update(
        SECRET_KEY=_require_secret('CORREO_SECRET_KEY'),
        SQLALCHEMY_DATABASE_URI=os.environ.get('CORREO_DATABASE_URI', 'sqlite:////app/database/mail.db'),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={
            'connect_args': {
                'check_same_thread': False,
                'timeout': 30,           # espera até 30s antes de dar "database is locked"
            },
            'poolclass': __import__('sqlalchemy.pool', fromlist=['NullPool']).NullPool,
        },
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        ACCOUNTS_DIR=os.environ.get('ACCOUNTS_DIR', '/app/accounts'),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=cookie_secure,
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 90,  # 90 dias — "manter conectado"
    )

    # Confia no reverse proxy (Caddy) para IP/proto/host reais.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

    # ── Extensões ──────────────────────────────────────────────────────────────
    import extensions as ext
    from database import db as _db
    _db.init_app(app)
    ext.db = _db

    ext.socketio.init_app(app, cors_allowed_origins=allowed_origins, async_mode='threading')

    from services.mail_service import MailService
    ext.mail_service = MailService(ext.socketio)

    # ── Banco e migrações ──────────────────────────────────────────────────────
    with app.app_context():
        _db.create_all()
        _run_migrations(_db)
        # WAL mode: permite leituras simultâneas com escritas (resolve database is locked)
        try:
            with _db.engine.connect() as conn:
                conn.execute(_db.text('PRAGMA journal_mode=WAL'))
                conn.execute(_db.text('PRAGMA synchronous=NORMAL'))
                conn.execute(_db.text('PRAGMA busy_timeout=30000'))
                conn.commit()
        except Exception as e:
            log.warning(f'PRAGMA WAL error: {e}')

    # ── Blueprints ─────────────────────────────────────────────────────────────
    from routes.auth     import bp as auth_bp
    from routes.accounts import bp as accounts_bp
    from routes.emails   import bp as emails_bp
    from routes.folders  import bp as folders_bp
    from routes.filters  import bp as filters_bp
    from routes.idle     import bp as idle_bp
    from routes.settings import bp as settings_bp

    for bp in [auth_bp, accounts_bp, emails_bp, folders_bp, filters_bp, idle_bp, settings_bp]:
        app.register_blueprint(bp)

    # ── Autenticação e CSRF (toda rota exceto as públicas) ────────────────────
    @app.before_request
    def _require_auth():
        path = request.path
        if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            return None
        if not session.get('authenticated'):
            if path.startswith('/api/'):
                return jsonify({'error': 'not_authenticated'}), 401
            return redirect('/login')
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and path.startswith('/api/'):
            token = request.headers.get('X-Correo-CSRF')
            if not token or token != session.get('csrf'):
                return jsonify({'error': 'csrf_invalid'}), 403
        return None

    # ── Healthcheck público (sem dados, sem autenticação) ─────────────────────
    @app.route('/healthz')
    def healthz():
        return jsonify({'ok': True})

    # ── Página principal ───────────────────────────────────────────────────────
    @app.route('/')
    def index():
        return render_template('index.html')

    # ── WebSocket ──────────────────────────────────────────────────────────────
    @ext.socketio.on('connect')
    def on_connect():
        if not session.get('authenticated'):
            log.warning('WebSocket: conexão rejeitada (sem sessão)')
            return False
        log.info('Cliente conectado via WebSocket')

    # ── Dirs obrigatórios ──────────────────────────────────────────────────────
    Path(app.config['ACCOUNTS_DIR']).mkdir(parents=True, exist_ok=True)
    from database import ATTACHMENTS_DIR
    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

    return app


def _run_migrations(db) -> None:
    migrations = [
        "ALTER TABLE accounts ADD COLUMN conf_file VARCHAR(255)",
        "ALTER TABLE emails ADD COLUMN thread_id VARCHAR(500)",
        "ALTER TABLE attachments ADD COLUMN file_path VARCHAR(1000)",
        # Remove NOT NULL do campo data para permitir armazenamento em disco
        # SQLite não suporta ALTER COLUMN — recria a tabela
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
    ]
    with db.engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(db.text(sql)); conn.commit()
            except Exception:
                pass
        try:
            conn.execute(db.text(
                "CREATE INDEX IF NOT EXISTS ix_email_account_folder_date "
                "ON emails (account_id, folder, date)"
            ))
            conn.commit()
        except Exception:
            pass


def _start_idle(app: Flask) -> None:
    """Inicia o IDLE manager em background (delay de 3s para o app subir)."""
    import time, extensions as ext
    time.sleep(3)
    try:
        from services.idle_manager import IMAPIdleManager
        from database import Account
        with app.app_context():
            im = IMAPIdleManager(ext.mail_service, app)
            ext.idle_manager = im
            ext.mail_service.idle_manager = im  # proxy methods em MailService também funcionam
            accounts = Account.query.all()
            log.info(f'Iniciando IDLE para {len(accounts)} conta(s)')
            for account in accounts:
                im.start_account(account.id)
    except Exception as e:
        log.error(f'Erro ao iniciar IDLE manager: {e}')


# ── Entry point ────────────────────────────────────────────────────────────────

app = create_app()

# Scripts de manutenção (rotate_key.py etc.) importam create_app()/app sem
# precisar do IDLE manager — setam CORREO_SKIP_IDLE=1 antes do import.
if not os.environ.get('CORREO_SKIP_IDLE'):
    threading.Thread(target=_start_idle, args=(app,), daemon=True, name='idle-starter').start()

if __name__ == '__main__':
    # Apenas para desenvolvimento local. Em produção, usar gunicorn
    # (ver Dockerfile): gunicorn -w 1 --threads 64 -b 0.0.0.0:15002 app:app
    import extensions as ext
    log.warning('Rodando com o servidor de desenvolvimento do Flask — não usar em produção.')
    ext.socketio.run(app, host='0.0.0.0', port=15002, debug=False)