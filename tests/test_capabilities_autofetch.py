"""
Testa MailService._capabilities() — nunca deve assumir 'sem UIDPLUS'
silenciosamente só porque o cache ainda não foi preenchido. Isso é o que
fazia 'excluir' parar de funcionar silenciosamente em contas recém
conectadas/reconectadas: sem UIDPLUS detectado, o código nunca chamava
uid_expunge nem removia a linha local.
"""
from app import app
from database import db, Account
from services.mail_service import MailService


class FakeClient:
    def __init__(self, caps):
        self._caps = caps

    def capabilities(self):
        return self._caps


def _make_account(email, capabilities=None):
    acc = Account(name='t', email=email, imap_host='h', smtp_host='s',
                  username=email, password_enc='x', capabilities=capabilities)
    db.session.add(acc)
    db.session.commit()
    return acc


def test_capabilities_fetched_live_when_never_cached(monkeypatch):
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('nevercached@example.com', capabilities=None)

        ms = MailService(socketio=None)
        fake = FakeClient([b'IMAP4rev1', b'UIDPLUS', b'MOVE'])
        monkeypatch.setattr(ms, '_safe_exec', lambda account, fn: fn(fake))

        caps = ms._capabilities(acc)

        assert caps == {'IMAP4rev1', 'UIDPLUS', 'MOVE'}
        # resultado fica cacheado pra não precisar buscar de novo
        assert acc.capabilities is not None
        assert 'UIDPLUS' in acc.capabilities


def test_capabilities_uses_cache_when_already_known(monkeypatch):
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('cached@example.com', capabilities='["UIDPLUS", "MOVE"]')

        ms = MailService(socketio=None)
        calls = []
        monkeypatch.setattr(ms, '_safe_exec', lambda account, fn: calls.append(1))

        caps = ms._capabilities(acc)

        assert caps == {'UIDPLUS', 'MOVE'}
        assert calls == []  # não precisou abrir conexão — já tinha cache


def test_capabilities_returns_empty_set_on_connection_error(monkeypatch):
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('connerror@example.com', capabilities=None)

        ms = MailService(socketio=None)
        def _raise(account, fn):
            raise RuntimeError('conexão recusada')
        monkeypatch.setattr(ms, '_safe_exec', _raise)

        assert ms._capabilities(acc) == set()
