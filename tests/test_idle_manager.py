"""
Testa services/idle_manager.py (Fase 2.5) com um IMAPClient fake — sem
servidor real. O ponto mais crítico: falha de autenticação nunca gera
retry automático (é o que evita o ban do fail2ban em mail.soft.dev.br).
"""
import threading
import time

import pytest
from imapclient.exceptions import LoginError

import services.idle_manager as idle_manager_module
from app import app
from database import db, Account
from services.idle_manager import IMAPIdleManager
from services.mail_service import MailService


class FakeIdleClient:
    login_behavior = 'ok'   # 'ok' | 'auth_fail'
    login_calls = 0
    shutdown_calls = 0
    idle_calls = 0

    def __init__(self, host, port=None, ssl=True, timeout=None):
        pass

    def login(self, username, password):
        FakeIdleClient.login_calls += 1
        if FakeIdleClient.login_behavior == 'auth_fail':
            raise LoginError('AUTHENTICATIONFAILED')

    def select_folder(self, folder, readonly=False):
        return {}

    def idle(self):
        FakeIdleClient.idle_calls += 1

    def idle_check(self, timeout=None):
        return []  # nenhuma resposta — força o loop a sair pelo deadline

    def idle_done(self):
        pass

    def shutdown(self):
        FakeIdleClient.shutdown_calls += 1


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    FakeIdleClient.login_behavior = 'ok'
    FakeIdleClient.login_calls = 0
    FakeIdleClient.shutdown_calls = 0
    FakeIdleClient.idle_calls = 0
    monkeypatch.setattr(idle_manager_module, 'IMAPClient', FakeIdleClient)
    yield


def _make_account(email):
    acc = Account(name='t', email=email, imap_host='h', smtp_host='s', username=email, password_enc='')
    acc.set_password('senha-de-teste')
    db.session.add(acc)
    db.session.commit()
    return acc


def test_run_session_syncs_inbox_and_emits_status(monkeypatch):
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('idle1@example.com')

        ms = MailService(socketio=None)
        monkeypatch.setattr(ms, 'refresh_folder_roles', lambda account: {})
        monkeypatch.setattr(ms, 'sync_folders_to_track', lambda account: [])
        sync_calls = []
        monkeypatch.setattr(ms, 'sync_folder_incremental',
                            lambda account, folder: (sync_calls.append(folder), {'ok': True, 'new': 0, 'flag_changes': 0})[1])
        events = []
        monkeypatch.setattr(ms, '_emit', lambda event, data: events.append((event, data)))

        im = IMAPIdleManager(ms, app)
        im.IDLE_RENEW_SECONDS = 0  # sai do loop de IDLE imediatamente após a sync inicial

        stop = threading.Event()
        im._run_session(acc.id, stop, last_poll=time.time())

        assert sync_calls == ['INBOX']
        assert FakeIdleClient.login_calls == 1
        assert FakeIdleClient.shutdown_calls == 1
        assert ('idle_status', {'account_id': acc.id, 'email': acc.email, 'status': 'connected'}) in events
        assert ('idle_status', {'account_id': acc.id, 'email': acc.email, 'status': 'disconnected'}) in events


def test_run_session_auth_failure_opens_circuit_without_sync(monkeypatch):
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('idle2@example.com')
        FakeIdleClient.login_behavior = 'auth_fail'

        ms = MailService(socketio=None)
        sync_calls = []
        monkeypatch.setattr(ms, 'sync_folder_incremental', lambda account, folder: sync_calls.append(folder))

        im = IMAPIdleManager(ms, app)
        stop = threading.Event()

        with pytest.raises(idle_manager_module.AuthCircuitOpen):
            im._run_session(acc.id, stop, last_poll=0)

        assert sync_calls == []  # nunca chegou a sincronizar
        db.session.refresh(acc)
        assert acc.auth_error is not None
        assert FakeIdleClient.shutdown_calls == 1


def test_loop_never_retries_login_while_auth_error_is_set(monkeypatch):
    """O ponto que evita o ban do fail2ban: com auth_error setado, o loop
    nunca tenta reconectar sozinho."""
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('idle3@example.com')
        acc.auth_error = 'senha inválida'
        db.session.commit()
        account_id = acc.id

    ms = MailService(socketio=None)
    im = IMAPIdleManager(ms, app)
    stop = threading.Event()

    t = threading.Thread(target=im._loop, args=(account_id, stop), daemon=True)
    t.start()
    time.sleep(0.2)   # tempo suficiente pro loop rodar algumas iterações
    stop.set()
    t.join(timeout=5)

    assert not t.is_alive()
    assert FakeIdleClient.login_calls == 0   # nenhuma tentativa de login
