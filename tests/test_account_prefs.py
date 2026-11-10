"""
Testa as preferências novas por conta (Fase 5.1/5.2): cor do marcador e
toggle de cópia em Enviados — tanto a API de contas quanto o efeito em
mail_service.send_email.
"""
from unittest.mock import MagicMock

from app import app
from database import db, Account


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def _base_payload(**overrides):
    data = {
        'name': 'teste', 'email': 'prefs@example.com', 'username': 'prefs@example.com',
        'password': 'segredo123', 'imap_host': 'imap.example.com', 'imap_port': 993,
        'smtp_host': 'smtp.example.com', 'smtp_port': 587,
    }
    data.update(overrides)
    return data


def test_create_account_with_marker_color_and_save_sent_copy_false():
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        client = app.test_client()
        _login(client)

        r = client.post('/api/accounts', json=_base_payload(marker_color='#ff0000', save_sent_copy=False),
                        headers={'X-Correo-CSRF': 'tok'})
        assert r.status_code == 201
        data = r.get_json()
        assert data['marker_color'] == '#ff0000'
        assert data['save_sent_copy'] is False

        acc = Account.query.filter_by(email='prefs@example.com').first()
        assert acc.marker_color == '#ff0000'
        assert acc.save_sent_copy is False


def test_create_account_defaults_save_sent_copy_true_and_no_color():
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        client = app.test_client()
        _login(client)

        r = client.post('/api/accounts', json=_base_payload(email='defaults@example.com'),
                        headers={'X-Correo-CSRF': 'tok'})
        data = r.get_json()
        assert data['save_sent_copy'] is True
        assert data['marker_color'] is None


def test_update_only_marker_color_does_not_restart_idle(monkeypatch):
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        client = app.test_client()
        _login(client)

        created = client.post('/api/accounts', json=_base_payload(email='norestart@example.com'),
                              headers={'X-Correo-CSRF': 'tok'}).get_json()

        restart_calls = []
        fake_idle = MagicMock()
        fake_idle.restart_account = lambda aid: restart_calls.append(aid)
        import extensions
        monkeypatch.setattr(extensions, 'idle_manager', fake_idle)

        r = client.put(f'/api/accounts/{created["id"]}', json={'marker_color': '#00ff00'},
                       headers={'X-Correo-CSRF': 'tok'})
        assert r.status_code == 200
        assert r.get_json()['marker_color'] == '#00ff00'
        assert restart_calls == []  # não deveria reconectar só por isso


def test_update_imap_host_restarts_idle(monkeypatch):
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        client = app.test_client()
        _login(client)

        created = client.post('/api/accounts', json=_base_payload(email='restart@example.com'),
                              headers={'X-Correo-CSRF': 'tok'}).get_json()

        restart_calls = []
        fake_idle = MagicMock()
        fake_idle.restart_account = lambda aid: restart_calls.append(aid)
        import extensions
        monkeypatch.setattr(extensions, 'idle_manager', fake_idle)

        r = client.put(f'/api/accounts/{created["id"]}', json={'imap_host': 'imap2.example.com'},
                       headers={'X-Correo-CSRF': 'tok'})
        assert r.status_code == 200
        assert restart_calls == [created['id']]


def test_send_email_skips_sent_copy_when_disabled(monkeypatch):
    import services.mail_service as mail_service_module
    from services.mail_service import MailService

    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = Account(name='t', email='nosent@example.com', imap_host='h', smtp_host='s',
                      username='nosent@example.com', password_enc='', save_sent_copy=False)
        acc.set_password('senha-teste')
        db.session.add(acc)
        db.session.commit()

        ms = MailService(socketio=None)
        safe_exec_calls = []
        monkeypatch.setattr(ms, '_safe_exec', lambda account, fn: safe_exec_calls.append(1))
        monkeypatch.setattr(ms, 'role_folder', lambda account, role: 'Sent')

        fake_smtp = MagicMock()
        monkeypatch.setattr(mail_service_module.smtplib, 'SMTP', lambda *a, **k: fake_smtp)
        monkeypatch.setattr(mail_service_module.smtplib, 'SMTP_SSL', lambda *a, **k: fake_smtp)

        result = ms.send_email(acc, to='dest@example.com', cc='', bcc='',
                               subject='oi', body='corpo')

        assert result['ok'] is True
        assert safe_exec_calls == []  # não tentou salvar em Enviados
