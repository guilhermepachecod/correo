"""
Testa refresh_folder_roles e role_folder (Fase 2.3/2.4), sem servidor real —
_safe_exec é substituído por um fake que devolve o formato de
imapclient.list_folders(): lista de (flags, delimiter, name).
"""
from app import app
from database import db, Account, FolderState
from services.mail_service import MailService


def _make_account(email):
    acc = Account(name='t', email=email, imap_host='h', smtp_host='s',
                  username=email, password_enc='x')
    db.session.add(acc)
    db.session.commit()
    return acc


def test_refresh_folder_roles_writes_folder_state(monkeypatch):
    with app.app_context():
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('folders-test@example.com')

        raw_list = [
            ((b'\\HasNoChildren',), '.', 'INBOX'),
            ((b'\\HasNoChildren', b'\\Trash'), '.', 'INBOX.Trash'),
            ((b'\\HasNoChildren', b'\\Sent'), '.', 'INBOX.Sent'),
            ((b'\\HasNoChildren',), '.', 'INBOX.Projetos'),  # sem SPECIAL-USE nem nome conhecido
        ]

        ms = MailService(socketio=None)
        monkeypatch.setattr(ms, '_safe_exec', lambda account, fn: raw_list)

        result = ms.refresh_folder_roles(acc)
        assert result == {
            'INBOX': 'inbox',
            'INBOX.Trash': 'trash',
            'INBOX.Sent': 'sent',
            'INBOX.Projetos': None,
        }

        rows = {fs.folder: fs.role for fs in
                FolderState.query.filter_by(account_id=acc.id).all()}
        assert rows == result

        # roda de novo (idempotência: atualiza em vez de duplicar)
        result2 = ms.refresh_folder_roles(acc)
        assert result2 == result
        assert FolderState.query.filter_by(account_id=acc.id).count() == 4


def test_refresh_folder_roles_returns_empty_on_imap_error(monkeypatch):
    with app.app_context():
        acc = _make_account('folders-error@example.com')
        ms = MailService(socketio=None)

        def _raise(account, fn):
            raise RuntimeError('conexão recusada')
        monkeypatch.setattr(ms, '_safe_exec', _raise)

        assert ms.refresh_folder_roles(acc) == {}


def test_role_folder_uses_folder_state_first(monkeypatch):
    with app.app_context():
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('role-folder@example.com')
        db.session.add(FolderState(account_id=acc.id, folder='INBOX.Trash', role='trash'))
        db.session.commit()

        ms = MailService(socketio=None)
        assert ms.role_folder(acc, 'trash') == 'INBOX.Trash'


def test_role_folder_falls_back_to_name_keyword(monkeypatch):
    with app.app_context():
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('role-folder-fallback@example.com')

        ms = MailService(socketio=None)
        monkeypatch.setattr(ms, 'get_folders', lambda account: ['INBOX', 'INBOX.Lixeira'])
        assert ms.role_folder(acc, 'trash') == 'INBOX.Lixeira'
        assert ms.role_folder(acc, 'archive') is None
