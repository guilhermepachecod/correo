"""
services/imap_connection.py
Fase 2.2 — gerenciador de conexão IMAP persistente por conta (usa
`imapclient` em vez de `imaplib` cru).

IMPORTANTE: este módulo ainda **não é usado** por idle_manager.py nem por
mail_service.py — eles continuam funcionando como hoje, sobre
services/imap_pool.py (uma conexão nova por operação). A troca de verdade
é a Fase 2.4/2.5, quando este módulo ganha o loop de reconexão/backoff e a
conexão dedicada de IDLE. Por ora, isto é só a fundação: uma conexão de
comandos persistente por conta, protegida por lock, com circuit breaker de
autenticação (nunca relogar automaticamente depois de uma falha de
autenticação — é o que evita o ban do fail2ban em mail.soft.dev.br).

Testado só com mocks de imapclient.IMAPClient (tests/test_imap_connection.py)
— nunca testado contra um servidor IMAP real nesta rodada.
"""
import logging
import threading

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

log = logging.getLogger(__name__)

BACKOFF_BASE = 2      # segundos
BACKOFF_MAX  = 300    # 5 minutos


def next_backoff(current: float) -> float:
    """Backoff exponencial de reconexão: 2, 4, 8, 16... até BACKOFF_MAX.
    Função pura — quem chama decide quando esperar; este módulo não dorme."""
    if current <= 0:
        return BACKOFF_BASE
    return min(current * 2, BACKOFF_MAX)


def get_capabilities(client) -> set[str]:
    """Lê a CAPABILITY do client já conectado/logado."""
    raw = client.capabilities()
    return {c.decode() if isinstance(c, bytes) else c for c in raw}


class AuthCircuitOpen(Exception):
    """A conta está com erro de autenticação pendente — não tenta logar de
    novo até reset_auth_error() ser chamado explicitamente (conta editada
    ou usuário pediu retry manual)."""


class AccountConnection:
    """Uma conexão de comandos persistente para uma conta, protegida por
    threading.Lock. execute(fn) garante login antes de rodar fn(client) e
    invalida a conexão (sem relogar sozinho) se fn(client) levantar — a
    próxima chamada reconecta. Nunca tenta logar de novo depois de uma
    falha de autenticação (AuthCircuitOpen) sem reset_auth_error()."""

    def __init__(self, account_id: int, host: str, port: int, use_ssl: bool,
                 username: str, password_fn, on_auth_error=None, on_capabilities=None):
        self.account_id      = account_id
        self.host             = host
        self.port             = port
        self.use_ssl          = use_ssl
        self.username         = username
        self._password_fn     = password_fn
        self._on_auth_error   = on_auth_error      # callback(account_id, error_message)
        self._on_capabilities = on_capabilities    # callback(account_id, set[str])

        self._lock        = threading.Lock()
        self._client       = None
        self._auth_blocked = False

    @property
    def auth_blocked(self) -> bool:
        return self._auth_blocked

    def reset_auth_error(self) -> None:
        with self._lock:
            self._auth_blocked = False

    def _connect_locked(self):
        client = IMAPClient(self.host, port=self.port, ssl=self.use_ssl, timeout=30)
        try:
            client.login(self.username, self._password_fn())
        except LoginError as e:
            self._auth_blocked = True
            try:
                client.shutdown()
            except Exception:
                pass
            if self._on_auth_error:
                try:
                    self._on_auth_error(self.account_id, str(e))
                except Exception:
                    log.exception(f'on_auth_error callback falhou account_id={self.account_id}')
            raise

        if self._on_capabilities:
            try:
                self._on_capabilities(self.account_id, get_capabilities(client))
            except Exception:
                log.warning(f'capabilities error account_id={self.account_id}', exc_info=True)
        return client

    def execute(self, fn):
        """Executa fn(client) sob o lock, conectando se necessário.
        Levanta AuthCircuitOpen se a conta estiver bloqueada por erro de
        autenticação; propaga qualquer outro erro (quem decide o backoff
        da reconexão é o chamador — ver next_backoff)."""
        with self._lock:
            if self._auth_blocked:
                raise AuthCircuitOpen(f'conta {self.account_id} com erro de autenticação pendente')
            if self._client is None:
                self._client = self._connect_locked()
            try:
                return fn(self._client)
            except Exception as e:
                log.warning(f'imap_connection account_id={self.account_id} erro, invalidando conexão: {e}')
                try:
                    self._client.shutdown()
                except Exception:
                    pass
                self._client = None
                raise

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.shutdown()
                except Exception:
                    pass
                self._client = None


class ConnectionManager:
    """Registro de uma AccountConnection por account_id."""

    def __init__(self):
        self._connections: dict[int, AccountConnection] = {}
        self._lock = threading.Lock()

    def register(self, account_id: int, host: str, port: int, use_ssl: bool,
                 username: str, password_fn, on_auth_error=None,
                 on_capabilities=None) -> AccountConnection:
        with self._lock:
            conn = self._connections.get(account_id)
            if conn is None:
                conn = AccountConnection(account_id, host, port, use_ssl, username,
                                         password_fn, on_auth_error, on_capabilities)
                self._connections[account_id] = conn
            return conn

    def get(self, account_id: int):
        return self._connections.get(account_id)

    def reset_auth_error(self, account_id: int) -> None:
        conn = self._connections.get(account_id)
        if conn:
            conn.reset_auth_error()

    def close(self, account_id: int) -> None:
        with self._lock:
            conn = self._connections.pop(account_id, None)
        if conn:
            conn.close()

    def close_all(self) -> None:
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for conn in connections:
            conn.close()


manager = ConnectionManager()
