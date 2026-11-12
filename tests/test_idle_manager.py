"""
Testa services/idle_manager.py (Fase 2.5) com um IMAPClient fake — sem
servidor real. O ponto mais crítico: falha de autenticação nunca gera
retry automático (é o que evita o ban do fail2ban em mail.soft.dev.br).

Desde a unificação de conexão (bug real em produção: e-dominios.com.br
matava com EOF a segunda conexão simultânea por conta), o IDLE passa a usar
services.imap_connection.manager — o mesmo singleton usado por
mail_service._safe_exec — em vez de abrir seu próprio IMAPClient. Por isso
o fake precisa ser injetado em services.imap_connection (não mais em
idle_manager), e os testes limpam esse singleton global entre execuções
pra não vazar conexões cacheadas de um teste pro outro (ids de conta podem
ser reaproveitados entre testes no mesmo banco sqlite de teste).
"""
import threading
import time

import pytest
from imapclient.exceptions import LoginError

import services.imap_connection as imap_connection_module
from services.imap_connection import manager as conn_manager, AuthCircuitOpen
from app import app
from database import db, Account
from services.idle_manager import IMAPIdleManager
from services.mail_service import MailService


class FakeIdleClient:
    login_behavior = 'ok'   # 'ok' | 'auth_fail'
    login_calls = 0
    shutdown_calls = 0
    idle_calls = 0
    instances = 0

    def __init__(self, host, port=None, ssl=True, timeout=None):
        FakeIdleClient.instances += 1
        self.selected = None

    def login(self, username, password):
        FakeIdleClient.login_calls += 1
        if FakeIdleClient.login_behavior == 'auth_fail':
            raise LoginError('AUTHENTICATIONFAILED')

    def select_folder(self, folder, readonly=False):
        self.selected = folder
        return {}

    def capabilities(self):
        return [b'IMAP4rev1', b'IDLE']

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
    FakeIdleClient.instances = 0
    monkeypatch.setattr(imap_connection_module, 'IMAPClient', FakeIdleClient)
    conn_manager.close_all()
    yield
    conn_manager.close_all()


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
        # A conexão agora é compartilhada/persistente (services.imap_connection) —
        # _run_session não a derruba mais ao sair do loop de IDLE.
        assert FakeIdleClient.shutdown_calls == 0
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

        with pytest.raises(AuthCircuitOpen):
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


def test_poll_other_folders_emits_a_single_aggregated_sync_done(monkeypatch):
    """O bug que causava a sidebar piscando: um sync_done por pasta. Com
    4 pastas sincronizando no mesmo ciclo de poll, tem que emitir só 1
    evento com os totais somados — não 4."""
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('idle4@example.com')

        ms = MailService(socketio=None)
        monkeypatch.setattr(ms, 'sync_folders_to_track',
                            lambda account: ['Sent', 'Trash', 'Junk', 'Drafts'])

        results_by_folder = {
            'Sent':   {'ok': True, 'new': 1, 'flag_changes': 0, 'removed': 0},
            'Trash':  {'ok': True, 'new': 0, 'flag_changes': 2, 'removed': 0},
            'Junk':   {'ok': True, 'new': 0, 'flag_changes': 0, 'removed': 1},
            'Drafts': {'ok': True, 'new': 0, 'flag_changes': 0, 'removed': 0},
        }
        monkeypatch.setattr(ms, 'sync_folder_incremental',
                            lambda account, folder: results_by_folder[folder])

        events = []
        monkeypatch.setattr(ms, '_emit', lambda event, data: events.append((event, data)))

        im = IMAPIdleManager(ms, app)
        im._poll_other_folders(acc)

        sync_done_events = [e for e in events if e[0] == 'sync_done']
        assert len(sync_done_events) == 1
        assert sync_done_events[0][1]['new'] == 1
        assert sync_done_events[0][1]['flag_changes'] == 2


def test_poll_other_folders_emits_nothing_when_no_changes(monkeypatch):
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('idle5@example.com')

        ms = MailService(socketio=None)
        monkeypatch.setattr(ms, 'sync_folders_to_track', lambda account: ['Sent', 'Trash'])
        monkeypatch.setattr(ms, 'sync_folder_incremental',
                            lambda account, folder: {'ok': True, 'new': 0, 'flag_changes': 0, 'removed': 0})
        events = []
        monkeypatch.setattr(ms, '_emit', lambda event, data: events.append((event, data)))

        im = IMAPIdleManager(ms, app)
        im._poll_other_folders(acc)

        assert events == []


def test_run_session_shares_a_single_connection_with_mail_service(monkeypatch):
    """Bug real em produção: e-dominios.com.br mata com 'socket error: EOF'
    uma segunda conexão simultânea por conta. IDLE e sync precisam usar a
    MESMA conexão (services.imap_connection.manager) — nunca uma segunda
    aberta só para o IDLE. Aqui simulamos uma sessão completa (conecta,
    sync inicial, resposta de IDLE dispara resync, poll de outras pastas)
    sem mockar sync_folder_incremental, pra confirmar que tudo passa pela
    mesma conexão e só um IMAPClient é criado para a conta inteira."""
    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('idle6@example.com')
        account_id = acc.id

        ms = MailService(socketio=None)
        monkeypatch.setattr(ms, 'refresh_folder_roles', lambda account: {})
        monkeypatch.setattr(ms, 'sync_folders_to_track', lambda account: ['INBOX.Sent'])
        monkeypatch.setattr(ms, '_emit', lambda event, data: None)

        sync_calls = []

        def fake_sync(account, folder):
            # Chama _safe_exec de verdade — precisa passar pela MESMA conexão
            # já aberta pelo IDLE, não abrir uma nova.
            ms._safe_exec(account, lambda c: c.select_folder(folder, readonly=True))
            sync_calls.append(folder)
            return {'ok': True, 'new': 0, 'flag_changes': 0, 'removed': 0}

        monkeypatch.setattr(ms, 'sync_folder_incremental', fake_sync)

        im = IMAPIdleManager(ms, app)
        im.IDLE_RENEW_SECONDS = 1
        im.IDLE_CHECK_TIMEOUT = 0

        # Força a 1ª iteração do while a reportar uma "resposta" de IDLE,
        # disparando um resync, e garante que o poll de outras pastas também
        # rode dentro da mesma sessão.
        call_count = {'n': 0}

        def idle_check_once(self, timeout=None):
            call_count['n'] += 1
            return [(1, b'EXISTS')] if call_count['n'] == 1 else []

        monkeypatch.setattr(FakeIdleClient, 'idle_check', idle_check_once)

        stop = threading.Event()
        im._run_session(account_id, stop, last_poll=0)

        # ordem real: sync inicial da INBOX -> poll de outras pastas (já
        # devido, last_poll=0) -> resync da INBOX disparado pela 1a resposta
        # fake de IDLE
        assert sync_calls == ['INBOX', 'INBOX.Sent', 'INBOX']
        assert FakeIdleClient.login_calls == 1
        assert FakeIdleClient.instances == 1   # nunca uma segunda conexão
