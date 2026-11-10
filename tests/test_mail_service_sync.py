"""
Testa sync_folder_incremental (Fase 2.4) inteiramente com um fake client —
sem servidor IMAP real. Cobre exatamente os bugs que a reescrita resolve:
B2 (poda não apaga linha), B3 (UID em vez de sequência — aqui nem existe
número de sequência no código), B6 (UIDVALIDITY reindexa a pasta), B9 (sem
cap que descarta mensagem).
"""
from app import app
from database import db, Account, Email, FolderState
from services.mail_service import MailService

RAW_EMAIL_TMPL = (
    b'From: Sender <sender@example.com>\r\n'
    b'To: me@example.com\r\n'
    b'Subject: Teste %d\r\n'
    b'Message-ID: <msg-%d@example.com>\r\n'
    b'Date: Mon, 1 Jan 2024 10:00:00 +0000\r\n'
    b'Content-Type: text/plain; charset=utf-8\r\n'
    b'\r\n'
    b'Corpo do email de teste %d.\r\n'
)


def _raw(uid: int) -> bytes:
    return RAW_EMAIL_TMPL % (uid, uid, uid)


class FakeClient:
    def __init__(self, uidvalidity=111, uidnext=100):
        self.uidvalidity   = uidvalidity
        self.uidnext       = uidnext
        self.all_uids      = set()
        self.flags_by_uid  = {}

    def select_folder(self, folder, readonly=False):
        return {b'UIDVALIDITY': self.uidvalidity, b'UIDNEXT': self.uidnext,
                b'EXISTS': len(self.all_uids), b'FLAGS': ()}

    def search(self, criteria='ALL'):
        return sorted(self.all_uids)

    def fetch(self, uids, data):
        result = {}
        for uid in uids:
            item = {}
            if 'BODY.PEEK[]' in data:
                item[b'BODY[]'] = _raw(uid)
            if 'FLAGS' in data:
                item[b'FLAGS'] = self.flags_by_uid.get(uid, ())
            result[uid] = item
        return result


def _make_account(email):
    acc = Account(name='t', email=email, imap_host='h', smtp_host='s',
                  username=email, password_enc='x')
    db.session.add(acc)
    db.session.commit()
    return acc


def _ms_with_fake(monkeypatch, fake_client):
    ms = MailService(socketio=None)
    monkeypatch.setattr(ms, '_safe_exec', lambda account, fn: fn(fake_client))
    return ms


def test_first_sync_downloads_all_without_cap(monkeypatch):
    """Resolve B9: sem cap [-N:] que descartava mensagem."""
    with app.app_context():
        db.session.query(Email).delete()
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('sync1@example.com')

        fake = FakeClient()
        fake.all_uids = set(range(1, 301))  # 300 mensagens — maior que qualquer batch
        ms = _ms_with_fake(monkeypatch, fake)

        result = ms.sync_folder_incremental(acc, 'INBOX')
        assert result['ok'] is True
        assert result['new'] == 300
        assert Email.query.filter_by(account_id=acc.id, folder='INBOX').count() == 300

        fs = FolderState.query.filter_by(account_id=acc.id, folder='INBOX').first()
        assert fs.uidvalidity == 111
        assert fs.uidnext == 100


def test_second_sync_only_fetches_new_uids_and_updates_flags(monkeypatch):
    with app.app_context():
        db.session.query(Email).delete()
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('sync2@example.com')

        fake = FakeClient()
        fake.all_uids = {1, 2, 3}
        ms = _ms_with_fake(monkeypatch, fake)
        ms.sync_folder_incremental(acc, 'INBOX')

        # chega uid 4, e uid 1 é marcado como lido e com estrela no servidor
        fake.all_uids.add(4)
        fake.flags_by_uid[1] = (b'\\Seen', b'\\Flagged')
        result = ms.sync_folder_incremental(acc, 'INBOX')

        assert result['new'] == 1
        assert result['flag_changes'] == 1
        assert Email.query.filter_by(account_id=acc.id, folder='INBOX').count() == 4

        e1 = Email.query.filter_by(account_id=acc.id, folder='INBOX', uid=1).first()
        assert e1.is_read is True
        assert e1.is_starred is True


def test_message_removed_on_server_is_removed_locally_not_just_flagged(monkeypatch):
    """Resolve o equivalente de B7 do lado da leitura: removemos pelo UID
    search ALL, nunca por EXPUNGE genérico."""
    with app.app_context():
        db.session.query(Email).delete()
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('sync3@example.com')

        fake = FakeClient()
        fake.all_uids = {1, 2, 3}
        ms = _ms_with_fake(monkeypatch, fake)
        ms.sync_folder_incremental(acc, 'INBOX')

        fake.all_uids.discard(2)
        result = ms.sync_folder_incremental(acc, 'INBOX')

        assert result['removed'] == 1
        remaining = {e.uid for e in Email.query.filter_by(account_id=acc.id, folder='INBOX').all()}
        assert remaining == {1, 3}


def test_uidvalidity_change_reindexes_folder(monkeypatch):
    """Resolve B6: UIDVALIDITY mudou → cache local da pasta é refeito sem duplicar."""
    with app.app_context():
        db.session.query(Email).delete()
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('sync4@example.com')

        fake = FakeClient(uidvalidity=111)
        fake.all_uids = {1, 2}
        ms = _ms_with_fake(monkeypatch, fake)
        ms.sync_folder_incremental(acc, 'INBOX')
        assert Email.query.filter_by(account_id=acc.id, folder='INBOX').count() == 2

        # caixa recriada no servidor: UIDVALIDITY muda, UIDs são reaproveitados
        fake.uidvalidity = 222
        fake.all_uids = {1, 2, 3}
        result = ms.sync_folder_incremental(acc, 'INBOX')

        assert result['new'] == 3   # reindexado do zero
        rows = Email.query.filter_by(account_id=acc.id, folder='INBOX').all()
        assert len(rows) == 3       # sem duplicados
        fs = FolderState.query.filter_by(account_id=acc.id, folder='INBOX').first()
        assert fs.uidvalidity == 222
