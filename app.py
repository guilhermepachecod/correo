"""
app.py
Ponto de entrada. Apenas: configuração, init de extensões,
registro de blueprints e arranque do IDLE manager.
"""
import os
import threading
import logging
from pathlib import Path

from flask import Flask, render_template

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY='mailapp-secret-2024',
        SQLALCHEMY_DATABASE_URI='sqlite:////app/database/mail.db',
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={
            'connect_args': {'check_same_thread': False},
            'poolclass': __import__('sqlalchemy.pool', fromlist=['NullPool']).NullPool,
        },
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        ACCOUNTS_DIR=str(Path('/app/accounts')),
    )

    # ── Extensões ──────────────────────────────────────────────────────────────
    import extensions as ext
    from database import db as _db
    _db.init_app(app)
    ext.db = _db

    ext.socketio.init_app(app, cors_allowed_origins='*', async_mode='threading')

    from services.mail_service import MailService
    ext.mail_service = MailService(ext.socketio)

    # ── Banco e migrações ──────────────────────────────────────────────────────
    with app.app_context():
        _db.create_all()
        _run_migrations(_db)

    # ── Blueprints ─────────────────────────────────────────────────────────────
    from routes.accounts import bp as accounts_bp
    from routes.emails   import bp as emails_bp
    from routes.folders  import bp as folders_bp
    from routes.filters  import bp as filters_bp
    from routes.idle     import bp as idle_bp

    for bp in [accounts_bp, emails_bp, folders_bp, filters_bp, idle_bp]:
        app.register_blueprint(bp)

    # ── Página principal ───────────────────────────────────────────────────────
    @app.route('/')
    def index():
        return render_template('index.html')

    # ── WebSocket ──────────────────────────────────────────────────────────────
    @ext.socketio.on('connect')
    def on_connect():
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

threading.Thread(target=_start_idle, args=(app,), daemon=True, name='idle-starter').start()

if __name__ == '__main__':
    import extensions as ext
    ext.socketio.run(app, host='0.0.0.0', port=15002,
                     debug=False, allow_unsafe_werkzeug=True)
