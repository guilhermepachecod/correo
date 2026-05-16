"""
services/imap_pool.py
Conexões IMAP por operação — abre, executa, fecha.
Não mantém conexão persistente paralela ao IDLE.
"""
import imaplib
import threading
import logging
from contextlib import contextmanager

log = logging.getLogger(__name__)

_meta: dict = {}
_meta_lock  = threading.Lock()


def register(account) -> None:
    with _meta_lock:
        _meta[account.id] = {
            'host':    account.imap_host,
            'port':    account.imap_port,
            'ssl':     account.imap_ssl,
            'user':    account.username,
            'pass_fn': account.get_password,
        }


def _connect(account_id: int) -> imaplib.IMAP4:
    with _meta_lock:
        m = _meta.get(account_id)
    if not m:
        raise RuntimeError(f'Conta {account_id} não registrada')
    conn = (imaplib.IMAP4_SSL if m['ssl'] else imaplib.IMAP4)(m['host'], m['port'])
    conn.login(m['user'], m['pass_fn']())
    return conn


@contextmanager
def connection(account_id: int):
    conn = _connect(account_id)
    try:
        yield conn
    finally:
        try: conn.logout()
        except Exception: pass


def safe_exec(account, fn, retries: int = 1):
    last_err = None
    for attempt in range(retries + 1):
        try:
            with connection(account.id) as conn:
                return fn(conn)
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, BrokenPipeError) as e:
            last_err = e
            if attempt < retries:
                log.warning(f'IMAP retry {attempt+1} ({account.email}): {e}')
            else:
                raise
    raise last_err


# Compatibilidade — pool.register(), pool.safe_exec(), pool.invalidate() etc.
class _Pool:
    def register(self, account):        register(account)
    def safe_exec(self, account, fn, retries=1): return safe_exec(account, fn, retries)
    def invalidate(self, account_id):   pass   # sem conexão persistente
    def close_all(self):                pass   # sem conexão persistente
    def get(self, account_id):          return _connect(account_id)  # caller fecha


pool = _Pool()