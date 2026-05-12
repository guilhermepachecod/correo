"""
services/idle_manager.py
Mantém uma conexão IMAP IDLE por conta (RFC 2177).
Notifica o frontend via WebSocket quando chegam e-mails novos.
Depende de: imap_pool, email_parser, mail_service (injetado).
"""
import imaplib
import re
import socket
import threading
import time
import logging

from database import db, Email, Attachment, Account, EmailFilter, \
    enforce_email_limit, resolve_thread_id
from services.imap_pool import pool
from services.email_parser import parse_email_from_raw

log = logging.getLogger(__name__)


class IMAPIdleManager:
    IDLE_TIMEOUT      = 25 * 60   # 25 min em IDLE (RFC recomenda < 29 min)
    IDLE_PAUSE        = 30        # 30s desconectado entre sessões (era 5 min — muito lento)
    OTHER_FOLDERS_INT = 60 * 60   # sync outras pastas a cada 1 hora
    RECONNECT_BASE    = 15        # backoff inicial (s)
    RECONNECT_MAX     = 300       # backoff máximo 5 min (era 10 — muito conservador)

    def __init__(self, mail_service, app):
        self._ms          = mail_service
        self._app         = app
        self._threads     = {}       # account_id → Thread
        self._stop_flags  = {}       # account_id → Event
        self._lock        = threading.Lock()
        self._auth_failed = set()

    # ── Controle de threads ───────────────────────────────────────────────────

    def start_account(self, account_id: int) -> None:
        with self._lock:
            if account_id in self._threads and self._threads[account_id].is_alive():
                return
            stop = threading.Event()
            self._stop_flags[account_id] = stop
            t = threading.Thread(target=self._idle_loop, args=(account_id, stop),
                                 daemon=True, name=f'idle-{account_id}')
            self._threads[account_id] = t
            t.start()
            log.info(f'IDLE iniciado para account_id={account_id}')

    def stop_account(self, account_id: int) -> None:
        with self._lock:
            flag = self._stop_flags.get(account_id)
            if flag:
                flag.set()
            pool.invalidate(account_id)

    def stop_all(self) -> None:
        with self._lock:
            for flag in self._stop_flags.values():
                flag.set()
            pool.close_all()

    def restart_account(self, account_id: int) -> None:
        self._auth_failed.discard(account_id)
        self.stop_account(account_id)
        time.sleep(1)
        self.start_account(account_id)

    def has_auth_error(self, account_id: int) -> bool:
        return account_id in self._auth_failed

    # ── Loop principal ────────────────────────────────────────────────────────

    def _idle_loop(self, account_id: int, stop: threading.Event) -> None:
        backoff        = self.RECONNECT_BASE
        last_full_sync = 0

        while not stop.is_set():
            if account_id in self._auth_failed:
                log.info(f'IDLE {account_id}: aguardando retry manual')
                stop.wait()
                break

            try:
                with self._app.app_context():
                    now      = time.time()
                    do_full  = (now - last_full_sync) >= self.OTHER_FOLDERS_INT
                    self._run_idle_session(account_id, stop, do_full_sync=do_full)
                    if do_full:
                        last_full_sync = time.time()
                backoff = self.RECONNECT_BASE
                if not stop.is_set():
                    stop.wait(self.IDLE_PAUSE)

            except Exception as e:
                err = str(e)
                pool.invalidate(account_id)
                if any(kw in err.lower() for kw in [
                    'authentication failed', 'authenticationfailed',
                    'invalid credentials', 'login failed', '[auth]', 'authentication error'
                ]):
                    log.error(f'IDLE {account_id}: erro de autenticação permanente')
                    self._auth_failed.add(account_id)
                    self._ms._emit('sync_error', {
                        'account_id': account_id,
                        'error': f'Erro de autenticação: verifique usuário/senha ({err})',
                        'auth_failed': True,
                    })
                    stop.wait()
                    break
                if stop.is_set():
                    break
                log.info(f'IDLE {account_id}: reconectando em {backoff}s')
                stop.wait(backoff)
                backoff = min(backoff * 2, self.RECONNECT_MAX)

    # ── Sessão IDLE ───────────────────────────────────────────────────────────

    def _run_idle_session(self, account_id: int, stop: threading.Event,
                          do_full_sync: bool = False) -> None:
        account = Account.query.get(account_id)
        if not account:
            return

        log.info(f'IDLE sessão iniciando para {account.email}')
        pool.register(account)

        conn = (imaplib.IMAP4_SSL if account.imap_ssl else imaplib.IMAP4)(
            account.imap_host, account.imap_port)
        conn.login(account.username, account.get_password())

        try:
            status, data = conn.select('INBOX', readonly=False)
            if status != 'OK':
                raise RuntimeError(f'SELECT INBOX falhou: {data}')
            exists_count = int(data[0])

            self._ms._emit('sync_start', {'account_id': account_id, 'email': account.email})
            conn.socket().settimeout(60)
            self._initial_sync(account, conn)
            conn.socket().settimeout(None)
            self._ms._emit('sync_done', {'account_id': account_id, 'email': account.email, 'new': 0})

            if do_full_sync:
                log.info(f'IDLE {account.email}: sync completo de outras pastas')
                try: conn.logout()
                except Exception: pass
                try:
                    self._ms.sync_account_non_inbox(account_id)
                except Exception as e:
                    log.warning(f'IDLE sync outras pastas error: {e}')
                conn = (imaplib.IMAP4_SSL if account.imap_ssl else imaplib.IMAP4)(
                    account.imap_host, account.imap_port)
                conn.login(account.username, account.get_password())
                status, data = conn.select('INBOX', readonly=False)
                if status != 'OK':
                    raise RuntimeError(f'SELECT INBOX falhou após full sync: {data}')
                exists_count = int(data[0])

            self._ms._emit('idle_status', {
                'account_id': account_id, 'email': account.email, 'status': 'connected'})
            log.info(f'IDLE {account.email}: {exists_count} msgs, entrando em IDLE')

            while not stop.is_set():
                tag = conn._new_tag().decode()
                conn.send(f'{tag} IDLE\r\n'.encode())
                resp = conn.readline()
                if not resp.startswith(b'+'):
                    raise RuntimeError(f'IDLE não aceito: {resp}')

                deadline   = time.time() + self.IDLE_TIMEOUT
                new_exists = exists_count

                while not stop.is_set():
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    conn.socket().settimeout(min(remaining, 60))
                    try:
                        line = conn.readline()
                    except socket.timeout:
                        continue
                    except Exception:
                        raise
                    if not line:
                        raise RuntimeError('Conexão IDLE encerrada pelo servidor')
                    m = re.match(rb'\* (\d+) (EXISTS|EXPUNGE|RECENT)', line)
                    if m:
                        count = int(m.group(1))
                        kind  = m.group(2).decode()
                        if kind == 'EXISTS' and count > new_exists:
                            log.info(f'IDLE {account.email}: {count - new_exists} e-mail(s) novo(s)')
                            new_exists = count; break
                        elif kind == 'EXPUNGE':
                            new_exists = max(0, new_exists - 1); break

                try:
                    conn.send(b'DONE\r\n')
                    while True:
                        line = conn.readline()
                        if line.startswith(tag.encode()): break
                except Exception as e:
                    log.warning(f'IDLE DONE error: {e}')

                conn.socket().settimeout(30)

                if new_exists > exists_count and not stop.is_set():
                    fetched = self._fetch_new(account, conn, exists_count + 1, new_exists)
                    exists_count = new_exists
                    if fetched:
                        log.info(f'IDLE {account.email}: {fetched} e-mail(s) salvo(s)')
                elif new_exists < exists_count and not stop.is_set():
                    self._sync_deletions_with_conn(account, conn)
                    exists_count = new_exists
                else:
                    exists_count = new_exists

        finally:
            try: conn.logout()
            except Exception: pass
            self._ms._emit('idle_status', {
                'account_id': account_id, 'email': account.email, 'status': 'disconnected'})

    # ── Sync inicial ──────────────────────────────────────────────────────────

    def _initial_sync(self, account: Account, conn) -> None:
        try:
            _, data = conn.uid('search', None, 'ALL')
            server_uids = [int(u) for u in (data[0].split() if data[0] else [])]
            if not server_uids:
                return

            existing = {row[0] for row in db.session.query(Email.uid)
                                                     .filter_by(account_id=account.id, folder='INBOX').all()}
            new_uids = sorted(set(server_uids) - existing)

            if new_uids:
                if not existing:
                    # Primeira vez: baixa os 500 mais recentes em batches de 100
                    # (evita martelar o servidor com milhares de requests)
                    recent_new = new_uids[-500:]
                    log.info(f'IDLE sync inicial {account.email}: {len(recent_new)} e-mails (primeira vez, cap 500)')
                    for i in range(0, len(recent_new), 100):
                        batch   = recent_new[i:i+100]
                        uid_str = ','.join(str(u) for u in batch)
                        _, md   = conn.uid('fetch', uid_str, '(UID FLAGS BODY.PEEK[])')
                        self._process_fetch_data(account, md, 'INBOX')
                        log.info(f'IDLE sync {account.email}: {min(i+100,len(recent_new))}/{len(recent_new)}')
                else:
                    # Já tem dados: só os 100 mais recentes não baixados
                    recent = new_uids[-100:]
                    log.info(f'IDLE sync inicial {account.email}: {len(recent)} novos')
                    uid_str = ','.join(str(u) for u in recent)
                    _, md   = conn.uid('fetch', uid_str, '(UID FLAGS BODY.PEEK[])')
                    self._process_fetch_data(account, md, 'INBOX')

            recent_check = sorted(set(server_uids) & existing)[-200:]
            if recent_check:
                uid_str = ','.join(str(u) for u in recent_check)
                _, flag_data = conn.uid('fetch', uid_str, '(UID FLAGS)')
                self._sync_flags(account, flag_data)
        except Exception as e:
            log.error(f'IDLE sync inicial error {account.email}: {e}', exc_info=True)

    def _fetch_new(self, account: Account, conn, seq_from: int, seq_to: int) -> int:
        try:
            _, msg_data = conn.fetch(f'{seq_from}:{seq_to}', '(UID FLAGS BODY.PEEK[])')
            return self._process_fetch_data(account, msg_data, 'INBOX')
        except Exception as e:
            log.warning(f'IDLE fetch_new error {account.email}: {e}')
            return 0

    def _process_fetch_data(self, account: Account, msg_data, folder: str) -> int:
        saved = 0
        i = 0
        while i < len(msg_data):
            item = msg_data[i]
            if not isinstance(item, tuple): i += 1; continue
            meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
            m    = re.search(r'UID (\d+)', meta)
            if not m: i += 1; continue
            uid     = int(m.group(1))
            is_read = '\\Seen' in meta
            exists  = db.session.query(Email.id).filter_by(
                account_id=account.id, folder=folder, uid=uid).first()
            if exists: i += 1; continue
            try:
                email_obj, att_data = parse_email_from_raw(
                    item[1], uid, folder, account.id, is_read, resolve_thread_id)
                db.session.add(email_obj)
                db.session.flush()
                for a in att_data:
                    att = Attachment(email_id=email_obj.id, filename=a['filename'],
                                     content_type=a['content_type'], size=a['size'])
                    try: att.save_to_disk(a['data'])
                    except Exception: att.data = a['data']
                    db.session.add(att)
                db.session.commit()
                filters = list(EmailFilter.query.filter_by(account_id=account.id, enabled=True)
                                                .order_by(EmailFilter.priority.asc()).all())
                self._ms._apply_filters(email_obj, filters)
                self._ms._emit('new_email', {
                    'account_id': account.id,
                    'folder':     email_obj.folder,
                    'email':      email_obj.to_list_dict(),
                })
                saved += 1
            except Exception as e:
                log.warning(f'IDLE parse error UID {uid}: {e}')
                try: db.session.rollback()
                except Exception: pass
            i += 1
        enforce_email_limit(account.id)
        return saved

    def _sync_deletions_with_conn(self, account: Account, conn) -> None:
        try:
            _, data = conn.uid('search', None, 'ALL')
            server_uids = {int(u) for u in (data[0].split() if data[0] else [])}
            db_uids = {row[0]: row[1] for row in db.session.query(Email.uid, Email.id)
                                                            .filter_by(account_id=account.id, folder='INBOX').all()}
            deleted_ids = [db_uids[u] for u in (set(db_uids) - server_uids) if u in db_uids]
            if not deleted_ids:
                return
            log.info(f'IDLE {account.email}: removendo {len(deleted_ids)} e-mail(s) deletados')
            db.session.query(Email).filter(Email.id.in_(deleted_ids)).delete(synchronize_session=False)
            db.session.commit()
            self._ms._emit('sync_done', {'account_id': account.id, 'email': account.email, 'new': 0})
        except Exception as e:
            log.warning(f'_sync_deletions_with_conn error {account.email}: {e}')

    def _sync_flags(self, account: Account, flag_data) -> None:
        existing_map = dict(db.session.query(Email.uid, Email.id)
                                      .filter_by(account_id=account.id, folder='INBOX').all())
        updates = []
        for item in flag_data:
            if not isinstance(item, (tuple, list)): continue
            meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
            m    = re.search(r'UID (\d+)', meta)
            if not m: continue
            uid        = int(m.group(1))
            server_read = '\\Seen' in meta
            email_id   = existing_map.get(uid)
            if email_id:
                updates.append((email_id, server_read))
        if updates:
            for eid, is_read in updates:
                db.session.query(Email).filter_by(id=eid).update(
                    {'is_read': is_read}, synchronize_session=False)
            db.session.commit()
            self._ms._emit('flags_updated', {
                'account_id': account.id, 'folder': 'INBOX',
                'updates': [{'id': eid, 'is_read': r} for eid, r in updates],
            })