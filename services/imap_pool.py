"""
services/imap_pool.py
Conexões IMAP por operação — abre, executa, fecha.
NÃO mantém conexão persistente aberta em paralelo com o IDLE.

Thunderbird faz exatamente isso: a conexão IDLE é a única conexão
permanente. Operações pontuais (marcar lido, mover, sync) abrem uma
conexão nova, executam, fecham. O servidor vê comportamento normal.

O pool anterior mantinha 2 conexões abertas por conta simultaneamente
(IDLE + pool persistente), o que alguns servidores interpretam como bot.
"""
import imaplib
import threading
import logging
from contextlib import contextmanager

log = logging.getLogger(__name__)

# Metadados de conexão por conta — só guarda credenciais, não a conexão
_meta: dict = {}
_meta_lock  = threading.Lock()


def register(account) -> None:
    """Registra credenciais da conta. Não abre conexão."""
    with _meta_lock:
        _meta[account.id] = {
            'host':    account.imap_host,
            'port':    account.imap_port,
            'ssl':     account.imap_ssl,
            'user':    account.username,
            'pass_fn': account.get_password,
        }


def connect(account_id: int) -> imaplib.IMAP4:
    """Abre e retorna uma nova conexão IMAP autenticada. Caller responsável por fechar."""
    with _meta_lock:
        m = _meta.get(account_id)
    if not m:
        raise RuntimeError(f'Conta {account_id} não registrada')
    conn = (imaplib.IMAP4_SSL if m['ssl'] else imaplib.IMAP4)(m['host'], m['port'])
    conn.login(m['user'], m['pass_fn']())
    return conn


@contextmanager
def connection(account_id: int):
    """
    Context manager: abre conexão, yield, fecha sempre.
    Uso:
        with pool.connection(account.id) as conn:
            conn.select('INBOX')
            ...
    """
    conn = connect(account_id)
    try:
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def safe_exec(account, fn, retries: int = 1):
    """
    Abre uma conexão, executa fn(conn), fecha.
    Em caso de erro de conexão, tenta mais `retries` vezes.
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            with connection(account.id) as conn:
                return fn(conn)
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, BrokenPipeError) as e:
            last_err = e
            if attempt < retries:
                log.warning(f'IMAP erro na tentativa {attempt+1} ({account.email}): {e}, tentando novamente')
            else:
                log.error(f'IMAP falhou após {retries+1} tentativas ({account.email}): {e}')
                raise
    raise last_err


# Compat: mantém referência ao módulo como "pool" para não quebrar imports existentes
class _PoolCompat:
    """Wrapper de compatibilidade para código que usa pool.register(), pool.get(), etc."""

    def register(self, account):
        register(account)

    def get(self, account_id: int) -> imaplib.IMAP4:
        """Abre uma conexão. ATENÇÃO: caller deve fechar com conn.logout()."""
        with _meta_lock:
            m = _meta.get(account_id)
        if not m:
            raise RuntimeError(f'Conta {account_id} não registrada')
        return connect(account_id)

    def invalidate(self, account_id: int) -> None:
        """Noop — não há conexão persistente para invalidar."""
        pass

    def close_all(self) -> None:
        """Noop — não há conexões persistentes."""
        pass


pool = _PoolCompat()