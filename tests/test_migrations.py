"""
Testa o sistema de migração versionado (database.run_migrations) — Fase 2.1.
Usa o banco sqlite temporário criado por conftest.py.
"""
from sqlalchemy import inspect

from app import app
from database import db, run_migrations, Account, FolderState


def test_migrations_create_expected_schema():
    with app.app_context():
        insp = inspect(db.engine)
        tables = set(insp.get_table_names())
        assert {'accounts', 'emails', 'attachments', 'folder_state', 'schema_version'} <= tables

        account_cols = {c['name'] for c in insp.get_columns('accounts')}
        assert {'auth_error', 'auth_error_at', 'capabilities',
                'marker_color', 'save_sent_copy'} <= account_cols

        email_cols = {c['name'] for c in insp.get_columns('emails')}
        assert 'body_cached' in email_cols


def test_migrations_are_idempotent():
    with app.app_context():
        run_migrations(db)
        run_migrations(db)
        with db.engine.connect() as conn:
            count = conn.execute(db.text('SELECT COUNT(*) FROM schema_version')).scalar()
        assert count == len(__import__('database').MIGRATIONS)


def test_folder_state_unique_per_account_and_folder():
    with app.app_context():
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()

        acc = Account(name='t', email='t-migrations@example.com', imap_host='h',
                      smtp_host='s', username='t-migrations@example.com', password_enc='x')
        db.session.add(acc)
        db.session.commit()

        fs = FolderState(account_id=acc.id, folder='INBOX', role='inbox')
        db.session.add(fs)
        db.session.commit()
        assert fs.to_dict() == {'folder': 'INBOX', 'role': 'inbox'}

        dup = FolderState(account_id=acc.id, folder='INBOX', role='inbox')
        db.session.add(dup)
        try:
            db.session.commit()
            assert False, 'deveria ter violado o UNIQUE(account_id, folder)'
        except Exception:
            db.session.rollback()
