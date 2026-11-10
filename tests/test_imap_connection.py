"""
Testa services/imap_connection.py (Fase 2.2) inteiramente com mocks de
imapclient.IMAPClient — sem servidor IMAP real (não tenho um disponível
neste ambiente). O circuit breaker de autenticação é o ponto mais crítico
aqui: precisa NUNCA relogar sozinho depois de uma falha de auth, porque é
o que evita o ban do fail2ban em mail.soft.dev.br.
"""
import pytest
from imapclient.exceptions import LoginError

import services.imap_connection as imap_connection
from services.imap_connection import (
    AccountConnection, AuthCircuitOpen, ConnectionManager,
    get_capabilities, next_backoff,
)


# ── next_backoff ──────────────────────────────────────────────────────────────

def test_next_backoff_exponential_with_cap():
    b = 0
    seen = []
    for _ in range(12):
        b = next_backoff(b)
        seen.append(b)
    assert seen[:5] == [2, 4, 8, 16, 32]
    assert seen[-1] == imap_connection.BACKOFF_MAX
    assert all(v <= imap_connection.BACKOFF_MAX for v in seen)


# ── get_capabilities ──────────────────────────────────────────────────────────

class _FakeClientCaps:
    def capabilities(self):
        return [b'IMAP4rev1', b'IDLE', b'UIDPLUS']


def test_get_capabilities_decodes_bytes():
    assert get_capabilities(_FakeClientCaps()) == {'IMAP4rev1', 'IDLE', 'UIDPLUS'}


# ── Fake IMAPClient para os testes de AccountConnection ──────────────────────

class FakeIMAPClient:
    """Substitui imapclient.IMAPClient nos testes. Controlado via atributos
    de classe para simular diferentes servidores/contas."""
    login_behavior = 'ok'     # 'ok' | 'auth_fail' | 'network_fail'
    instances = []
    login_calls = 0
    shutdown_calls = 0

    def __init__(self, host, port=None, ssl=True, timeout=None):
        self.host = host
        FakeIMAPClient.instances.append(self)
        self._shut_down = False

    def login(self, username, password):
        FakeIMAPClient.login_calls += 1
        if FakeIMAPClient.login_behavior == 'auth_fail':
            raise LoginError('AUTHENTICATIONFAILED')
        if FakeIMAPClient.login_behavior == 'network_fail':
            raise OSError('connection refused')
        return b'OK'

    def capabilities(self):
        return [b'IMAP4rev1', b'IDLE']

    def shutdown(self):
        FakeIMAPClient.shutdown_calls += 1
        self._shut_down = True

    def noop(self):
        return b'OK'


@pytest.fixture(autouse=True)
def _reset_fake_client(monkeypatch):
    FakeIMAPClient.login_behavior = 'ok'
    FakeIMAPClient.instances = []
    FakeIMAPClient.login_calls = 0
    FakeIMAPClient.shutdown_calls = 0
    monkeypatch.setattr(imap_connection, 'IMAPClient', FakeIMAPClient)
    yield


def _make_conn(account_id=1, on_auth_error=None, on_capabilities=None):
    return AccountConnection(
        account_id=account_id, host='imap.example.com', port=993, use_ssl=True,
        username='user@example.com', password_fn=lambda: 'senha',
        on_auth_error=on_auth_error, on_capabilities=on_capabilities,
    )


# ── AccountConnection ─────────────────────────────────────────────────────────

def test_execute_reuses_connection_across_calls():
    conn = _make_conn()
    assert conn.execute(lambda c: c.noop()) == b'OK'
    assert conn.execute(lambda c: c.noop()) == b'OK'
    assert FakeIMAPClient.login_calls == 1          # uma única conexão/login reaproveitada
    assert len(FakeIMAPClient.instances) == 1


def test_execute_reconnects_after_fn_raises():
    conn = _make_conn()
    conn.execute(lambda c: c.noop())
    with pytest.raises(RuntimeError):
        conn.execute(lambda c: (_ for _ in ()).throw(RuntimeError('boom')))
    assert FakeIMAPClient.shutdown_calls == 1
    # próxima chamada reconecta (novo login)
    conn.execute(lambda c: c.noop())
    assert FakeIMAPClient.login_calls == 2


def test_auth_failure_opens_circuit_and_calls_callback():
    FakeIMAPClient.login_behavior = 'auth_fail'
    events = []
    conn = _make_conn(account_id=42, on_auth_error=lambda aid, msg: events.append((aid, msg)))

    with pytest.raises(LoginError):
        conn.execute(lambda c: c.noop())

    assert conn.auth_blocked is True
    assert events and events[0][0] == 42
    assert 'AUTHENTICATIONFAILED' in events[0][1]

    # próxima chamada NUNCA tenta logar de novo sozinha — levanta
    # AuthCircuitOpen sem instanciar outro client
    calls_before = FakeIMAPClient.login_calls
    with pytest.raises(AuthCircuitOpen):
        conn.execute(lambda c: c.noop())
    assert FakeIMAPClient.login_calls == calls_before


def test_reset_auth_error_allows_retry():
    FakeIMAPClient.login_behavior = 'auth_fail'
    conn = _make_conn()
    with pytest.raises(LoginError):
        conn.execute(lambda c: c.noop())
    assert conn.auth_blocked is True

    FakeIMAPClient.login_behavior = 'ok'
    conn.reset_auth_error()
    assert conn.auth_blocked is False
    assert conn.execute(lambda c: c.noop()) == b'OK'


def test_network_failure_does_not_open_auth_circuit():
    FakeIMAPClient.login_behavior = 'network_fail'
    conn = _make_conn()
    with pytest.raises(OSError):
        conn.execute(lambda c: c.noop())
    assert conn.auth_blocked is False   # falha de rede != falha de autenticação


def test_on_capabilities_called_after_successful_login():
    received = []
    conn = _make_conn(on_capabilities=lambda aid, caps: received.append((aid, caps)))
    conn.execute(lambda c: c.noop())
    assert received == [(1, {'IMAP4rev1', 'IDLE'})]


def test_close_shuts_down_and_clears_client():
    conn = _make_conn()
    conn.execute(lambda c: c.noop())
    conn.close()
    assert FakeIMAPClient.shutdown_calls == 1
    conn.execute(lambda c: c.noop())   # reconecta normalmente depois de close()
    assert FakeIMAPClient.login_calls == 2


# ── ConnectionManager ─────────────────────────────────────────────────────────

def test_manager_register_returns_same_instance_for_same_account():
    mgr = ConnectionManager()
    c1 = mgr.register(1, 'h', 993, True, 'u', lambda: 'p')
    c2 = mgr.register(1, 'h', 993, True, 'u', lambda: 'p')
    assert c1 is c2
    assert mgr.get(1) is c1
    assert mgr.get(999) is None


def test_manager_reset_auth_error_delegates():
    mgr = ConnectionManager()
    FakeIMAPClient.login_behavior = 'auth_fail'
    conn = mgr.register(5, 'h', 993, True, 'u', lambda: 'p')
    with pytest.raises(LoginError):
        conn.execute(lambda c: c.noop())
    assert conn.auth_blocked is True

    FakeIMAPClient.login_behavior = 'ok'
    mgr.reset_auth_error(5)
    assert conn.auth_blocked is False


def test_manager_close_all_shuts_down_every_connection():
    mgr = ConnectionManager()
    c1 = mgr.register(1, 'h', 993, True, 'u', lambda: 'p')
    c2 = mgr.register(2, 'h', 993, True, 'u', lambda: 'p')
    c1.execute(lambda c: c.noop())
    c2.execute(lambda c: c.noop())
    mgr.close_all()
    assert FakeIMAPClient.shutdown_calls == 2
    assert mgr.get(1) is None and mgr.get(2) is None
