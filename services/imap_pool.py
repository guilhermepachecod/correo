"""
services/imap_pool.py
Pool de conexões IMAP — uma conexão persistente por conta.
Reconecta automaticamente se a conexão cair. Thread-safe via lock por conta.
"""
import imaplib
import threading
import logging

log = logging.getLogger(__name__)


class IMAPPool:

    def __init__(self):
        self._conns       = {}
        self._locks       = {}
        self._meta        = {}
        self._global_lock = threading.Lock()

    def _get_lock(self, account_id: int) -> threading.Lock:
        with self._global_lock:
            if account_id not in self._locks:
                self._locks[account_id] = threading.Lock()
            return self._locks[account_id]

    def register(self, account) -> None:
        with self._global_lock:
            self._meta[account.id] = {
                'host':    account.imap_host,
                'port':    account.imap_port,
                'ssl':     account.imap_ssl,
                'user':    account.username,
                'pass_fn': account.get_password,
            }

    def _connect_raw(self, account_id: int) -> imaplib.IMAP4:
        m = self._meta[account_id]
        conn = (imaplib.IMAP4_SSL if m['ssl'] else imaplib.IMAP4)(m['host'], m['port'])
        conn.login(m['user'], m['pass_fn']())
        log.info(f'IMAP pool: nova conexão para account_id={account_id}')
        return conn

    def _is_alive(self, conn: imaplib.IMAP4) -> bool:
        try:
            conn.noop()
            return True
        except Exception:
            return False

    def get(self, account_id: int) -> imaplib.IMAP4:
        lock = self._get_lock(account_id)
        with lock:
            if account_id not in self._meta:
                raise RuntimeError(f'Conta {account_id} não registrada no pool')
            conn = self._conns.get(account_id)
            if conn is None or not self._is_alive(conn):
                if conn:
                    try: conn.logout()
                    except Exception: pass
                conn = self._connect_raw(account_id)
                self._conns[account_id] = conn
            return conn

    def invalidate(self, account_id: int) -> None:
        with self._global_lock:
            conn = self._conns.pop(account_id, None)
            if conn:
                try: conn.logout()
                except Exception: pass

    def close_all(self) -> None:
        with self._global_lock:
            for conn in self._conns.values():
                try: conn.logout()
                except Exception: pass
            self._conns.clear()


# Singleton — importado por mail_service e idle_manager
pool = IMAPPool()
