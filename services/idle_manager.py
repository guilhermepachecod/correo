"""
services/idle_manager.py
IDLE por conta (RFC 2177), por UID — Fase 2.5. Nenhuma aritmética de número
de sequência (bug B3): qualquer resposta não solicitada do IDLE simplesmente
dispara uma sync incremental por UID da INBOX
(services.mail_service.sync_folder_incremental). Pastas com role conhecido
(sent/trash/junk/drafts/archive) são sincronizadas por poll periódico, na
mesma conexão de comandos persistente usada pelas ações (services.imap_connection).

Cada conta usa até 2 conexões: a dedicada de IDLE (aberta aqui) e a de
comandos (gerenciada por services.imap_connection, compartilhada com
sync manual/ações). Falha de autenticação nunca gera retry automático —
ver AuthCircuitOpen / MailService._on_auth_error (circuit breaker).
"""
import logging
import os
import threading
import time

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

from database import Account
from services.imap_connection import manager as conn_manager, AuthCircuitOpen

log = logging.getLogger(__name__)


class IMAPIdleManager:
    IDLE_RENEW_SECONDS = 25 * 60   # RFC 2177 recomenda renovar antes de 29 min
    IDLE_CHECK_TIMEOUT  = 20        # idle_check bloqueia até esse tempo por ciclo
    RECONNECT_BASE      = 2
    RECONNECT_MAX       = 300
    POLL_SECONDS         = int(os.environ.get('CORREO_POLL_SECONDS', 120))

    def __init__(self, mail_service, app):
        self._ms         = mail_service
        self._app        = app
        self._threads    = {}       # account_id → Thread
        self._stop_flags = {}       # account_id → Event
        self._lock       = threading.Lock()

    # ── Controle de threads ───────────────────────────────────────────────────

    def start_account(self, account_id: int) -> None:
        with self._lock:
            if account_id in self._threads and self._threads[account_id].is_alive():
                return
            stop = threading.Event()
            self._stop_flags[account_id] = stop
            t = threading.Thread(target=self._loop, args=(account_id, stop),
                                 daemon=True, name=f'idle-{account_id}')
            self._threads[account_id] = t
            t.start()
            log.info(f'IDLE iniciado para account_id={account_id}')

    def stop_account(self, account_id: int) -> None:
        with self._lock:
            flag = self._stop_flags.get(account_id)
            if flag:
                flag.set()
        conn_manager.close(account_id)

    def stop_all(self) -> None:
        with self._lock:
            for flag in self._stop_flags.values():
                flag.set()
        conn_manager.close_all()

    def restart_account(self, account_id: int) -> None:
        """Único jeito de uma conta com auth_error voltar a tentar: editar a
        conta (routes/accounts.py chama isso) ou clicar em 'reconectar'
        (routes/idle.py)."""
        self._ms.reset_auth_error(account_id)
        self.stop_account(account_id)
        time.sleep(1)
        self.start_account(account_id)

    def has_auth_error(self, account_id: int) -> bool:
        account = Account.query.get(account_id)
        return bool(account and account.auth_error)

    # ── Loop principal ────────────────────────────────────────────────────────

    def _loop(self, account_id: int, stop: threading.Event) -> None:
        backoff   = self.RECONNECT_BASE
        last_poll = 0.0

        while not stop.is_set():
            with self._app.app_context():
                account = Account.query.get(account_id)
                if not account:
                    return
                blocked = bool(account.auth_error)

            if blocked:
                # Circuit breaker de autenticação: nunca relogar sozinho.
                stop.wait(60)
                continue

            try:
                with self._app.app_context():
                    last_poll = self._run_session(account_id, stop, last_poll)
                backoff = self.RECONNECT_BASE
            except AuthCircuitOpen:
                stop.wait(60)
                continue
            except Exception as e:
                if stop.is_set():
                    break
                log.warning(f'IDLE account_id={account_id}: {e}; reconectando em {backoff}s')
                stop.wait(backoff)
                backoff = min(backoff * 2, self.RECONNECT_MAX)
                continue

            if not stop.is_set():
                stop.wait(2)

    def _run_session(self, account_id: int, stop: threading.Event, last_poll: float) -> float:
        """Uma 'sessão' = conecta, sincroniza, entra em IDLE até renovar (25min)
        ou cair. Retorna o novo last_poll (timestamp do último poll de outras
        pastas) pro loop continuar contando entre sessões."""
        account = Account.query.get(account_id)
        if not account:
            return last_poll

        self._ms.refresh_folder_roles(account)

        client = IMAPClient(account.imap_host, port=account.imap_port,
                            ssl=account.imap_ssl, timeout=30)
        try:
            client.login(account.username, account.get_password())
        except LoginError as e:
            self._ms._on_auth_error(account_id, str(e))
            try: client.shutdown()
            except Exception: pass
            raise AuthCircuitOpen(str(e)) from e

        self._ms._emit('idle_status', {'account_id': account_id, 'email': account.email, 'status': 'connected'})
        log.info(f'IDLE conectado {account.email}')

        try:
            self._ms._emit('sync_start', {'account_id': account_id, 'email': account.email})
            result = self._ms.sync_folder_incremental(account, 'INBOX')
            self._ms._emit('sync_done', {
                'account_id': account_id, 'email': account.email,
                'new': result.get('new', 0), 'flag_changes': result.get('flag_changes', 0),
            })

            if time.time() - last_poll >= self.POLL_SECONDS:
                self._poll_other_folders(account)
                last_poll = time.time()

            client.select_folder('INBOX', readonly=True)
            deadline = time.time() + self.IDLE_RENEW_SECONDS

            while not stop.is_set() and time.time() < deadline:
                client.idle()
                try:
                    responses = client.idle_check(timeout=self.IDLE_CHECK_TIMEOUT)
                finally:
                    client.idle_done()

                if responses:
                    log.info(f'IDLE {account.email}: {len(responses)} resposta(s), ressincronizando INBOX por UID')
                    result = self._ms.sync_folder_incremental(account, 'INBOX')
                    if result.get('new') or result.get('flag_changes') or result.get('removed'):
                        self._ms._emit('sync_done', {
                            'account_id': account_id, 'email': account.email,
                            'new': result.get('new', 0), 'flag_changes': result.get('flag_changes', 0),
                        })

                if time.time() - last_poll >= self.POLL_SECONDS:
                    self._poll_other_folders(account)
                    last_poll = time.time()

            return last_poll
        finally:
            try: client.shutdown()
            except Exception: pass
            self._ms._emit('idle_status', {'account_id': account_id, 'email': account.email, 'status': 'disconnected'})

    def _poll_other_folders(self, account: Account) -> None:
        """Sincroniza todas as pastas com role conhecido e emite **um único**
        sync_done agregado no final — evitar um evento por pasta é o que
        evita a sidebar re-renderizar (e piscar) várias vezes em sequência
        só porque a conta tem Enviados/Lixeira/Spam/Rascunhos sincronizando
        no mesmo ciclo de poll."""
        total_new, total_flags, total_removed = 0, 0, 0
        for folder in self._ms.sync_folders_to_track(account):
            try:
                result = self._ms.sync_folder_incremental(account, folder)
                total_new     += result.get('new', 0)
                total_flags   += result.get('flag_changes', 0)
                total_removed += result.get('removed', 0)
            except AuthCircuitOpen:
                raise
            except Exception as e:
                log.warning(f'poll pasta {account.email}/{folder} error: {e}')

        if total_new or total_flags or total_removed:
            self._ms._emit('sync_done', {
                'account_id': account.id, 'email': account.email,
                'new': total_new, 'flag_changes': total_flags,
            })
