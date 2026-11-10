"""
Testa as ações servidor-primeiro (Fase 2.6): delete/move/spam/flags só
mudam o banco local depois que o fake IMAP confirma; nunca chamam EXPUNGE
genérico (sempre uid_expunge com a lista exata de UIDs — resolve B7).
"""
from app import app
from database import db, Account, Email, FolderState
from services.mail_service import MailService


class RecordingClient:
    """Fake client que grava os comandos recebidos, pra garantir que nunca
    chamamos expunge() genérico (só uid_expunge com UIDs específicos)."""
    def __init__(self):
        self.calls = []
        self.selected = None

    def select_folder(self, folder, readonly=False):
        self.selected = folder
        self.calls.append(('select_folder', folder))
        return {}

    def move(self, uids, folder):
        self.calls.append(('move', tuple(uids), folder))

    def copy(self, uids, folder):
        self.calls.append(('copy', tuple(uids), folder))

    def add_flags(self, uids, flags):
        self.calls.append(('add_flags', tuple(uids), tuple(flags)))

    def remove_flags(self, uids, flags):
        self.calls.append(('remove_flags', tuple(uids), tuple(flags)))

    def uid_expunge(self, uids):
        self.calls.append(('uid_expunge', tuple(uids)))

    def expunge(self):
        # Nunca deve ser chamado — é o bug B7 (apaga \Deleted de outro cliente).
        raise AssertionError('expunge() genérico chamado — regressão do bug B7')


def _setup(monkeypatch, caps=('UIDPLUS', 'MOVE')):
    db.session.query(Email).delete()
    db.session.query(FolderState).delete()
    db.session.query(Account).delete()
    db.session.commit()
    acc = Account(name='t', email='actions@example.com', imap_host='h', smtp_host='s',
                  username='actions@example.com', password_enc='x', capabilities=f'{list(caps)}'.replace("'", '"'))
    db.session.add(acc)
    db.session.commit()

    email = Email(account_id=acc.id, folder='INBOX', uid=42, subject='x',
                  from_addr='a@b.com', message_id='<1@x>')
    db.session.add(email)
    db.session.commit()

    client = RecordingClient()
    ms = MailService(socketio=None)
    monkeypatch.setattr(ms, '_safe_exec', lambda account, fn: fn(client))
    return ms, acc, email, client


def test_delete_moves_to_trash_using_move_when_available(monkeypatch):
    with app.app_context():
        ms, acc, email, client = _setup(monkeypatch)
        db.session.add(FolderState(account_id=acc.id, folder='INBOX.Trash', role='trash'))
        db.session.commit()

        result = ms.delete_email(email, acc)

        assert result == {'ok': True, 'permanent': False}
        assert ('move', (42,), 'INBOX.Trash') in client.calls
        assert not any(c[0] == 'uid_expunge' for c in client.calls)
        assert Email.query.get(email.id).folder == 'INBOX.Trash'


def test_delete_in_trash_purges_permanently_with_uid_expunge(monkeypatch):
    with app.app_context():
        ms, acc, email, client = _setup(monkeypatch)
        db.session.add(FolderState(account_id=acc.id, folder='INBOX.Trash', role='trash'))
        db.session.commit()
        email.folder = 'INBOX.Trash'
        db.session.commit()

        result = ms.delete_email(email, acc)

        assert result == {'ok': True, 'permanent': True}
        assert ('add_flags', (42,), ('\\Deleted',)) in client.calls
        assert ('uid_expunge', (42,)) in client.calls
        assert Email.query.get(email.id) is None


def test_delete_without_uidplus_only_flags_and_keeps_local_row(monkeypatch):
    """Sem UIDPLUS nunca chamamos EXPUNGE genérico — mensagem fica só
    flagada no servidor e o registro local NÃO é removido (resolve B1/B7)."""
    with app.app_context():
        ms, acc, email, client = _setup(monkeypatch, caps=())
        email.folder = 'INBOX'  # sem lixeira conhecida e sem UIDPLUS
        db.session.commit()

        result = ms.delete_email(email, acc)

        assert result['ok'] is True
        assert result['permanent'] is False
        assert 'warning' in result
        assert not any(c[0] == 'uid_expunge' for c in client.calls)
        assert not any(c[0] == 'expunge' for c in client.calls)
        assert Email.query.get(email.id) is not None  # NÃO removido localmente


def test_delete_server_error_does_not_touch_local_db(monkeypatch):
    """Resolve B1: erro no servidor nunca apaga o registro local."""
    with app.app_context():
        ms, acc, email, _client = _setup(monkeypatch)

        def _raise(account, fn):
            raise RuntimeError('servidor fora do ar')
        monkeypatch.setattr(ms, '_safe_exec', _raise)

        result = ms.delete_email(email, acc)

        assert result['ok'] is False
        assert Email.query.get(email.id) is not None
        assert Email.query.get(email.id).folder == 'INBOX'


def test_move_email_uses_copy_flag_expunge_when_no_move_capability(monkeypatch):
    with app.app_context():
        ms, acc, email, client = _setup(monkeypatch, caps=('UIDPLUS',))
        result = ms.move_email(email, 'INBOX.Projetos', acc)

        assert result == {'ok': True}
        assert ('copy', (42,), 'INBOX.Projetos') in client.calls
        assert ('add_flags', (42,), ('\\Deleted',)) in client.calls
        assert ('uid_expunge', (42,)) in client.calls
        assert Email.query.get(email.id).folder == 'INBOX.Projetos'


def test_set_spam_moves_to_junk_role_folder(monkeypatch):
    with app.app_context():
        ms, acc, email, client = _setup(monkeypatch)
        db.session.add(FolderState(account_id=acc.id, folder='INBOX.Spam', role='junk'))
        db.session.commit()

        result = ms.set_spam(email, True, acc)

        assert result == {'ok': True}
        assert ('move', (42,), 'INBOX.Spam') in client.calls
        assert Email.query.get(email.id).folder == 'INBOX.Spam'


def test_mark_read_and_star_send_correct_flags(monkeypatch):
    with app.app_context():
        ms, acc, email, client = _setup(monkeypatch)

        assert ms.mark_read_on_server(email, acc) is True
        assert ('add_flags', (42,), ('\\Seen',)) in client.calls

        assert ms.mark_unread_on_server(email, acc) is True
        assert ('remove_flags', (42,), ('\\Seen',)) in client.calls

        assert ms.mark_star_on_server(email, True, acc) is True
        assert ('add_flags', (42,), ('\\Flagged',)) in client.calls

        assert ms.mark_star_on_server(email, False, acc) is True
        assert ('remove_flags', (42,), ('\\Flagged',)) in client.calls
