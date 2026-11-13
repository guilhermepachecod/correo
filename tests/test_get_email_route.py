"""
Testa GET /api/emails/<id> — o bug real reportado: abrir uma mensagem não
lida ficava lento (podia chegar a 20-30s) porque mark_read_on_server fazia
um roundtrip IMAP de verdade (SELECT + add_flags) *dentro* da requisição
HTTP, e isso competia pelo mesmo lock de conexão compartilhado com o IDLE
(ver services/idle_manager.py). A resposta HTTP não pode esperar por isso —
o estado local (is_read) já é suficiente pra UI, e o servidor sincroniza
em background, igual ao padrão já usado em bulk_action/sync manual.
"""
import time

from app import app
from database import db, Account, Email


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def _make_email(is_read=False, body_cached=True):
    db.session.query(Email).delete()
    db.session.query(Account).delete()
    db.session.commit()
    acc = Account(name='t', email='getmail@example.com', imap_host='h', smtp_host='s',
                  username='getmail@example.com', password_enc='x')
    db.session.add(acc)
    db.session.commit()
    email = Email(account_id=acc.id, folder='INBOX', uid=9, subject='x',
                  from_addr='a@b.com', is_read=is_read, body_cached=body_cached,
                  body_text='corpo')
    db.session.add(email)
    db.session.commit()
    return email


def test_opening_unread_email_does_not_block_on_mark_read_on_server(monkeypatch):
    import extensions

    with app.app_context():
        email = _make_email(is_read=False)
        email_id = email.id

        calls = []

        def slow_mark_read(email_obj, account=None):
            time.sleep(0.5)   # simula o roundtrip IMAP real
            calls.append(email_obj.id)
            return True

        monkeypatch.setattr(extensions.mail_service, 'mark_read_on_server', slow_mark_read)

    client = app.test_client()
    _login(client)

    start = time.monotonic()
    r = client.get(f'/api/emails/{email_id}')
    elapsed = time.monotonic() - start

    assert r.status_code == 200
    assert r.get_json()['is_read'] is True
    # a resposta não pode ter esperado o mark_read_on_server (0.5s) inteiro
    assert elapsed < 0.3

    with app.app_context():
        assert Email.query.get(email_id).is_read is True

    for _ in range(20):
        if calls:
            break
        time.sleep(0.05)
    assert calls == [email_id]   # mas foi chamado mesmo assim, em background


def test_opening_already_read_email_never_calls_mark_read_on_server(monkeypatch):
    import extensions

    with app.app_context():
        email = _make_email(is_read=True)
        email_id = email.id

        calls = []
        monkeypatch.setattr(extensions.mail_service, 'mark_read_on_server',
                            lambda email_obj, account=None: calls.append(email_obj.id))

    client = app.test_client()
    _login(client)

    r = client.get(f'/api/emails/{email_id}')
    assert r.status_code == 200

    time.sleep(0.2)
    assert calls == []


def test_fetch_full_body_still_runs_synchronously_when_body_not_cached(monkeypatch):
    """A busca do corpo continua bloqueando a resposta (tem que voltar com o
    corpo pronto) — só o mark_read_on_server é que saiu do caminho crítico."""
    import extensions

    with app.app_context():
        email = _make_email(is_read=True, body_cached=False)
        email_id = email.id

        def fake_fetch_full_body(email_obj):
            email_obj.body_text = 'corpo buscado agora'
            email_obj.body_cached = True
            db.session.commit()
            return True

        monkeypatch.setattr(extensions.mail_service, 'fetch_full_body', fake_fetch_full_body)

    client = app.test_client()
    _login(client)

    r = client.get(f'/api/emails/{email_id}')
    assert r.status_code == 200
    assert r.get_json()['body_text'] == 'corpo buscado agora'
