"""
Testa GET /api/emails/<id>/source (ver código-fonte bruto da mensagem,
pra diagnóstico de cabeçalhos tipo 'Received') e
MailService.fetch_raw_source — nunca marca a mensagem como lida
(BODY.PEEK[]) nem grava nada no banco.
"""
from app import app
from database import db, Account, Email
from services.mail_service import MailService


class FakeClient:
    def __init__(self, raw=None):
        self._raw = raw

    def select_folder(self, folder, readonly=False):
        return {}

    def fetch(self, uids, data):
        if self._raw is None:
            return {}
        return {uids[0]: {b'BODY[]': self._raw}}


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def _make_email():
    db.session.query(Email).delete()
    db.session.query(Account).delete()
    db.session.commit()
    acc = Account(name='t', email='source@example.com', imap_host='h', smtp_host='s',
                  username='source@example.com', password_enc='x')
    db.session.add(acc)
    db.session.commit()
    email = Email(account_id=acc.id, folder='INBOX', uid=7, subject='x',
                  from_addr='a@b.com', is_read=False, body_cached=True)
    db.session.add(email)
    db.session.commit()
    return email


def test_fetch_raw_source_returns_decoded_text(monkeypatch):
    with app.app_context():
        email = _make_email()
        ms = MailService(socketio=None)
        raw = b'Received: from mx.example.com\r\nSubject: x\r\n\r\ncorpo'
        monkeypatch.setattr(ms, '_safe_exec', lambda account, fn: fn(FakeClient(raw)))

        source = ms.fetch_raw_source(email)
        assert source == raw.decode('utf-8')
        assert 'Received:' in source


def test_fetch_raw_source_does_not_mark_as_read_or_touch_db(monkeypatch):
    with app.app_context():
        email = _make_email()
        ms = MailService(socketio=None)
        monkeypatch.setattr(ms, '_safe_exec', lambda account, fn: fn(FakeClient(b'Subject: x\r\n\r\ny')))

        ms.fetch_raw_source(email)

        refreshed = Email.query.get(email.id)
        assert refreshed.is_read is False
        assert refreshed.body_cached is True
        assert refreshed.body_text == ''  # nada foi reescrito no banco


def test_fetch_raw_source_returns_none_on_imap_error(monkeypatch):
    with app.app_context():
        email = _make_email()
        ms = MailService(socketio=None)
        def _raise(account, fn):
            raise RuntimeError('conexão recusada')
        monkeypatch.setattr(ms, '_safe_exec', _raise)

        assert ms.fetch_raw_source(email) is None


def test_source_route_returns_502_when_fetch_fails(monkeypatch):
    import extensions

    with app.app_context():
        email = _make_email()
        email_id = email.id
        monkeypatch.setattr(extensions.mail_service, 'fetch_raw_source', lambda e: None)

    client = app.test_client()
    _login(client)

    r = client.get(f'/api/emails/{email_id}/source')
    assert r.status_code == 502


def test_source_route_returns_source_on_success(monkeypatch):
    import extensions

    with app.app_context():
        email = _make_email()
        email_id = email.id
        monkeypatch.setattr(extensions.mail_service, 'fetch_raw_source', lambda e: 'Subject: oi\r\n\r\ncorpo')

    client = app.test_client()
    _login(client)

    r = client.get(f'/api/emails/{email_id}/source')
    assert r.status_code == 200
    assert r.get_json() == {'ok': True, 'source': 'Subject: oi\r\n\r\ncorpo'}
