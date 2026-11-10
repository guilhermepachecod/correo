"""
Testa o parsing da resposta LIST do IMAP e refresh_folder_roles (Fase 2.3),
sem servidor real — _safe_exec é substituído por um fake que devolve linhas
LIST sintéticas.
"""
from app import app
from database import db, Account, FolderState
from services.mail_service import MailService, _parse_list_line


def test_parse_list_line_variants():
    assert _parse_list_line(r'(\HasNoChildren \Trash) "." "INBOX.Trash"') == (
        [r'\HasNoChildren', r'\Trash'], 'INBOX.Trash',
    )
    assert _parse_list_line(r'(\HasNoChildren) "." INBOX') == ([r'\HasNoChildren'], 'INBOX')
    assert _parse_list_line(r'(\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"') == (
        [r'\HasNoChildren', r'\Sent'], '[Gmail]/Sent Mail',
    )
    assert _parse_list_line('garbage') is None


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
            b'(\\HasNoChildren) "." "INBOX"',
            b'(\\HasNoChildren \\Trash) "." "INBOX.Trash"',
            b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"',
            b'(\\HasNoChildren) "." "INBOX.Projetos"',  # sem SPECIAL-USE nem nome conhecido
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
