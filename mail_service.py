import imaplib
import smtplib
import email as email_lib
from email import policy as email_policy
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
import logging
import re
import threading
import time
import socket
from datetime import datetime
from collections import defaultdict

from database import Email, Attachment, Account, EmailFilter, \
    enforce_email_limit, resolve_thread_id, ATTACHMENTS_DIR

log = logging.getLogger(__name__)

BATCH_SIZE = 50

# ── Connection Pool ────────────────────────────────────────────────────────────

class IMAPPool:
    """
    Pool de conexões IMAP — uma conexão persistente por conta.
    Reconecta automaticamente se a conexão cair.
    Thread-safe via lock por conta.
    """

    def __init__(self):
        self._conns       = {}
        self._locks       = {}
        self._meta        = {}
        self._global_lock = threading.Lock()

    def _get_lock(self, account_id):
        with self._global_lock:
            if account_id not in self._locks:
                self._locks[account_id] = threading.Lock()
            return self._locks[account_id]

    def register(self, account: Account):
        with self._global_lock:
            self._meta[account.id] = {
                'host':    account.imap_host,
                'port':    account.imap_port,
                'ssl':     account.imap_ssl,
                'user':    account.username,
                'pass_fn': account.get_password,
            }

    def _connect_raw(self, account_id) -> imaplib.IMAP4:
        m = self._meta[account_id]
        if m['ssl']:
            conn = imaplib.IMAP4_SSL(m['host'], m['port'])
        else:
            conn = imaplib.IMAP4(m['host'], m['port'])
        conn.login(m['user'], m['pass_fn']())
        log.info(f'IMAP pool: nova conexão para account_id={account_id}')
        return conn

    def _is_alive(self, conn) -> bool:
        try:
            conn.noop()
            return True
        except Exception:
            return False

    def get(self, account_id) -> imaplib.IMAP4:
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

    def invalidate(self, account_id):
        with self._global_lock:
            conn = self._conns.pop(account_id, None)
            if conn:
                try: conn.logout()
                except Exception: pass

    def close_all(self):
        with self._global_lock:
            for conn in self._conns.values():
                try: conn.logout()
                except Exception: pass
            self._conns.clear()


_pool = IMAPPool()


# ── Encoding helpers ───────────────────────────────────────────────────────────

def _decode_header_value(value):
    if not value:
        return ''
    try:
        parts = decode_header(value)
        result = []
        for part, charset in parts:
            if isinstance(part, bytes):
                cs = charset or 'utf-8'
                try:
                    result.append(part.decode(cs, errors='replace'))
                except (LookupError, UnicodeDecodeError):
                    result.append(part.decode('latin-1', errors='replace'))
            else:
                result.append(str(part))
        return ''.join(result)
    except Exception:
        return str(value)


def _decode_payload(part):
    try:
        payload = part.get_payload(decode=True)
        if not payload:
            return ''
        charset = part.get_content_charset() or 'utf-8'
        try:
            return payload.decode(charset, errors='replace')
        except (LookupError, UnicodeDecodeError):
            return payload.decode('latin-1', errors='replace')
    except Exception:
        return ''


def _get_body(msg):
    text, html = '', ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get('Content-Disposition', ''))
            if 'attachment' in cd:
                continue
            if ct == 'text/plain' and not text:
                text = _decode_payload(part)
            elif ct == 'text/html' and not html:
                html = _decode_payload(part)
    else:
        ct = msg.get_content_type()
        body = _decode_payload(msg)
        if ct == 'text/html':
            html = body
        else:
            text = body
    return text, html


def _get_attachments(msg):
    attachments = []
    for part in msg.walk():
        cd = str(part.get('Content-Disposition', ''))
        if 'attachment' not in cd and 'inline' not in cd:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = _decode_header_value(filename)
        data = part.get_payload(decode=True)
        if not data:
            continue
        attachments.append({
            'filename':     filename,
            'content_type': part.get_content_type(),
            'size':         len(data),
            'data':         data,
        })
    return attachments


def _parse_email_from_raw(raw: bytes, uid: int, folder: str,
                           account_id: int, is_read: bool) -> tuple:
    """Parseia um e-mail raw e retorna (Email obj, att_data list)."""
    msg = email_lib.message_from_bytes(raw, policy=email_policy.compat32)

    subject     = _decode_header_value(msg.get('Subject', ''))
    from_full   = _decode_header_value(msg.get('From', ''))
    from_name, from_addr = parseaddr(from_full)
    to_addr     = _decode_header_value(msg.get('To', ''))
    cc_addr     = _decode_header_value(msg.get('Cc', ''))
    reply_to    = _decode_header_value(msg.get('Reply-To', ''))
    message_id  = (msg.get('Message-ID') or '').strip()
    in_reply_to = (msg.get('In-Reply-To') or '').strip()

    try:
        date = parsedate_to_datetime(msg.get('Date', ''))
        if date.tzinfo:
            date = date.replace(tzinfo=None)
    except Exception:
        date = datetime.utcnow()

    body_text, body_html = _get_body(msg)
    att_data = _get_attachments(msg)
    thread_id = resolve_thread_id(message_id, in_reply_to)

    email_obj = Email(
        account_id=account_id, folder=folder, uid=uid,
        message_id=message_id, in_reply_to=in_reply_to,
        thread_id=thread_id,
        subject=subject or '(sem assunto)',
        from_addr=from_addr, from_name=from_name,
        to_addr=to_addr, cc_addr=cc_addr, reply_to=reply_to,
        date=date, body_text=body_text, body_html=body_html,
        is_read=is_read, has_attachments=bool(att_data),
    )
    return email_obj, att_data


# ── IMAP IDLE Manager ──────────────────────────────────────────────────────────

class IMAPIdleManager:
    """
    Mantém uma conexão IMAP IDLE por conta.
    Quando o servidor notifica que chegou e-mail novo (EXISTS),
    faz fetch apenas do e-mail novo e emite via WebSocket.

    IDLE spec: RFC 2177
    - Cliente envia: IDLE
    - Servidor responde: + idling
    - Servidor envia notificações: * N EXISTS, * N EXPUNGE, etc.
    - Para sair: cliente envia DONE
    - Reconecta automaticamente se cair

    Timeout: reenvia IDLE a cada 25 min (servidores encerram após 30 min)
    """

    IDLE_TIMEOUT      = 10 * 60  # 10 minutos em IDLE depois desconecta
    IDLE_PAUSE        = 5 * 60   # 5 minutos desconectado (Thunderbird pode usar)
    OTHER_FOLDERS_INT = 60 * 60  # sync de outras pastas a cada 1 hora
    RECONNECT_BASE    = 30       # backoff inicial em caso de erro
    RECONNECT_MAX     = 600      # backoff máximo 10 minutos
    FOLDER            = 'INBOX'  # pasta a monitorar via IDLE

    def __init__(self, mail_service, app):
        self._ms        = mail_service   # referência ao MailService
        self._app       = app            # contexto Flask
        self._threads   = {}             # account_id -> Thread
        self._stop_flags = {}            # account_id -> threading.Event
        self._lock      = threading.Lock()

    def start_account(self, account_id: int):
        """Inicia IDLE para uma conta. Idempotente."""
        with self._lock:
            if account_id in self._threads and self._threads[account_id].is_alive():
                return
            stop = threading.Event()
            self._stop_flags[account_id] = stop
            t = threading.Thread(
                target=self._idle_loop,
                args=(account_id, stop),
                daemon=True,
                name=f'idle-{account_id}',
            )
            self._threads[account_id] = t
            t.start()
            log.info(f'IDLE iniciado para account_id={account_id}')

    def stop_account(self, account_id: int):
        """Para o IDLE de uma conta."""
        with self._lock:
            flag = self._stop_flags.get(account_id)
            if flag:
                flag.set()
            _pool.invalidate(account_id)

    def stop_all(self):
        with self._lock:
            for flag in self._stop_flags.values():
                flag.set()
            _pool.close_all()

    def restart_account(self, account_id: int):
        self.stop_account(account_id)
        time.sleep(1)
        self.start_account(account_id)

    def _idle_loop(self, account_id: int, stop: threading.Event):
        """
        Loop principal por conta:
        1. Conecta e faz sync inicial completo (INBOX + outras pastas)
        2. Entra em IDLE por IDLE_TIMEOUT segundos
        3. Desconecta e espera IDLE_PAUSE segundos (libera conexão para outros clientes)
        4. Repete — a cada OTHER_FOLDERS_INT também sincroniza outras pastas
        """
        backoff = self.RECONNECT_BASE
        last_full_sync = 0  # timestamp do último sync completo de outras pastas

        while not stop.is_set():
            try:
                with self._app.app_context():
                    now = time.time()
                    do_full = (now - last_full_sync) >= self.OTHER_FOLDERS_INT
                    self._run_idle_session(account_id, stop, do_full_sync=do_full)
                    if do_full:
                        last_full_sync = time.time()
                backoff = self.RECONNECT_BASE

                # Pausa entre sessões — libera a conexão para outros clientes (ex: Thunderbird)
                if not stop.is_set():
                    log.debug(f'IDLE {account_id}: pausa de {self.IDLE_PAUSE}s')
                    stop.wait(self.IDLE_PAUSE)

            except Exception as e:
                log.warning(f'IDLE account_id={account_id} erro: {e}')
                _pool.invalidate(account_id)
                if stop.is_set():
                    break
                log.info(f'IDLE account_id={account_id} reconectando em {backoff}s')
                stop.wait(backoff)
                backoff = min(backoff * 2, self.RECONNECT_MAX)

    def _run_idle_session(self, account_id: int, stop: threading.Event,
                          do_full_sync: bool = False):
        """
        Sessão IDLE única (tempo limitado):
        1. Conecta ao servidor
        2. Sync da INBOX (novos UIDs + flags)
        3. Se do_full_sync: sync de todas as outras pastas também
        4. Entra em IDLE por IDLE_TIMEOUT segundos
        5. Desconecta — conexão liberada para outros clientes
        """
        from database import db as _db, Account as _Account

        account = _Account.query.get(account_id)
        if not account:
            return

        log.info(f'IDLE sessão iniciando para {account.email}')
        _pool.register(account)

        # Conexão dedicada para IDLE (separada do pool de operações)
        if account.imap_ssl:
            conn = imaplib.IMAP4_SSL(account.imap_host, account.imap_port)
        else:
            conn = imaplib.IMAP4(account.imap_host, account.imap_port)
        conn.login(account.username, account.get_password())

        try:
            # Seleciona INBOX
            status, data = conn.select('INBOX', readonly=False)
            if status != 'OK':
                raise RuntimeError(f'SELECT INBOX falhou: {data}')

            # Extrai total de mensagens atual
            exists_count = int(data[0])

            # Sync da INBOX — busca UIDs novos e atualiza flags
            self._initial_sync(account, conn, _db)

            # Sync de outras pastas se for hora (não bloqueia IDLE)
            if do_full_sync:
                log.info(f'IDLE {account.email}: sync completo de outras pastas')
                try:
                    conn.logout()
                except Exception:
                    pass
                # Sync das outras pastas usando o pool normal (sem IDLE)
                self._ms.sync_account_non_inbox(account_id)
                # Reconecta para IDLE
                if account.imap_ssl:
                    conn = imaplib.IMAP4_SSL(account.imap_host, account.imap_port)
                else:
                    conn = imaplib.IMAP4(account.imap_host, account.imap_port)
                conn.login(account.username, account.get_password())
                status, data = conn.select('INBOX', readonly=False)
                if status != 'OK':
                    raise RuntimeError(f'SELECT INBOX falhou após full sync: {data}')
                exists_count = int(data[0])

            # Emite que está online
            self._ms._emit('idle_status', {
                'account_id': account_id,
                'email': account.email,
                'status': 'connected',
            })

            log.info(f'IDLE {account.email}: {exists_count} msgs, entrando em IDLE')

            while not stop.is_set():
                # Entra em IDLE
                tag = conn._new_tag().decode()
                conn.send(f'{tag} IDLE\r\n'.encode())

                # Lê resposta do servidor (deve ser "+ idling")
                resp = conn.readline()
                if not resp.startswith(b'+'):
                    raise RuntimeError(f'IDLE não aceito: {resp}')

                # Lê notificações até timeout ou stop
                deadline = time.time() + self.IDLE_TIMEOUT
                new_exists = exists_count

                while not stop.is_set():
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break  # timeout — sai para renovar IDLE

                    # Timeout no socket para não bloquear para sempre
                    conn.socket().settimeout(min(remaining, 60))

                    try:
                        line = conn.readline()
                    except socket.timeout:
                        continue
                    except Exception:
                        raise  # erro real — sai e reconecta

                    if not line:
                        raise RuntimeError('Conexão IDLE encerrada pelo servidor')

                    # Processa notificações unilaterais do servidor
                    # Formato: * N EXISTS  ou  * N EXPUNGE  ou  * N RECENT
                    m = re.match(rb'\* (\d+) (EXISTS|EXPUNGE|RECENT)', line)
                    if m:
                        count = int(m.group(1))
                        kind  = m.group(2).decode()
                        if kind == 'EXISTS' and count > new_exists:
                            log.info(f'IDLE {account.email}: {count - new_exists} e-mail(s) novo(s)')
                            new_exists = count
                            # Sai do IDLE para buscar os novos
                            break

                # Sai do IDLE: envia DONE
                try:
                    conn.send(b'DONE\r\n')
                    # Lê a resposta do tag IDLE (OK Completed)
                    while True:
                        line = conn.readline()
                        if line.startswith(tag.encode()):
                            break
                except Exception as e:
                    log.warning(f'IDLE DONE error: {e}')

                conn.socket().settimeout(30)

                # Se houve e-mails novos, faz fetch
                if new_exists > exists_count and not stop.is_set():
                    fetched = self._fetch_new(account, conn, _db, exists_count + 1, new_exists)
                    exists_count = new_exists
                    if fetched:
                        log.info(f'IDLE {account.email}: {fetched} e-mail(s) salvo(s)')
                else:
                    exists_count = new_exists

        finally:
            try:
                conn.logout()
            except Exception:
                pass
            self._ms._emit('idle_status', {
                'account_id': account_id,
                'email': account.email,
                'status': 'disconnected',
            })

    def _initial_sync(self, account: Account, conn, _db):
        """
        Sync inicial ao conectar:
        - Busca UIDs existentes no servidor
        - Compara com banco
        - Faz fetch só dos novos (máx 100 para não demorar)
        - Atualiza flags dos existentes
        """
        try:
            _, data = conn.uid('search', None, 'ALL')
            server_uids = [int(u) for u in (data[0].split() if data[0] else [])]

            if not server_uids:
                return

            existing = set(
                row[0] for row in
                _db.session.query(Email.uid)
                    .filter_by(account_id=account.id, folder='INBOX').all()
            )

            new_uids = sorted(set(server_uids) - existing)[-100:]

            if new_uids:
                log.info(f'IDLE sync inicial {account.email}: {len(new_uids)} novos')
                uid_str = ','.join(str(u) for u in new_uids)
                _, msg_data = conn.uid('fetch', uid_str, '(UID FLAGS BODY.PEEK[])')
                self._process_fetch_data(account, msg_data, 'INBOX', _db)

            # Sync de flags dos 200 mais recentes
            recent_uids = sorted(set(server_uids) & existing)[-200:]
            if recent_uids:
                uid_str = ','.join(str(u) for u in recent_uids)
                _, flag_data = conn.uid('fetch', uid_str, '(UID FLAGS)')
                self._sync_flags(account, flag_data, _db)

        except Exception as e:
            log.warning(f'IDLE sync inicial error {account.email}: {e}')

    def _fetch_new(self, account: Account, conn, _db,
                   seq_from: int, seq_to: int) -> int:
        """
        Faz fetch dos e-mails por sequence number (mais simples que UID após IDLE).
        Retorna quantidade salva.
        """
        try:
            seq_str = f'{seq_from}:{seq_to}'
            _, msg_data = conn.fetch(seq_str, '(UID FLAGS BODY.PEEK[])')
            return self._process_fetch_data(account, msg_data, 'INBOX', _db)
        except Exception as e:
            log.warning(f'IDLE fetch_new error {account.email}: {e}')
            return 0

    def _process_fetch_data(self, account: Account, msg_data, folder: str, _db) -> int:
        """Processa dados de fetch e salva e-mails novos no banco. Retorna count."""
        saved = 0
        i = 0
        while i < len(msg_data):
            item = msg_data[i]
            if not isinstance(item, tuple):
                i += 1
                continue

            meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
            raw  = item[1]

            uid_match = re.search(r'UID (\d+)', meta)
            if not uid_match:
                i += 1
                continue

            uid     = int(uid_match.group(1))
            is_read = '\\Seen' in meta

            # Verifica se já existe
            exists = _db.session.query(Email.id)\
                                .filter_by(account_id=account.id,
                                           folder=folder, uid=uid).first()
            if exists:
                i += 1
                continue

            try:
                email_obj, att_data = _parse_email_from_raw(
                    raw, uid, folder, account.id, is_read
                )
                _db.session.add(email_obj)
                _db.session.flush()

                for a in att_data:
                    att = Attachment(
                        email_id=email_obj.id,
                        filename=a['filename'],
                        content_type=a['content_type'],
                        size=a['size'],
                    )
                    try:
                        att.save_to_disk(a['data'])
                    except Exception:
                        att.data = a['data']
                    _db.session.add(att)

                _db.session.commit()

                # Aplica filtros
                filters = list(
                    EmailFilter.query
                        .filter_by(account_id=account.id, enabled=True)
                        .order_by(EmailFilter.priority.asc()).all()
                )
                self._ms._apply_filters(email_obj, filters)

                # Emite para o frontend
                self._ms._emit('new_email', {
                    'account_id': account.id,
                    'folder':     email_obj.folder,
                    'email':      email_obj.to_list_dict(),
                })

                saved += 1
            except Exception as e:
                log.warning(f'IDLE parse error UID {uid}: {e}')
                try: _db.session.rollback()
                except Exception: pass

            i += 1

        enforce_email_limit(account.id)
        return saved

    def _sync_flags(self, account: Account, flag_data, _db):
        """Atualiza flags (lido/não-lido) dos e-mails existentes."""
        updates = []
        existing_map = dict(
            _db.session.query(Email.uid, Email.id)
                .filter_by(account_id=account.id, folder='INBOX').all()
        )
        for item in flag_data:
            if not isinstance(item, (tuple, list)):
                continue
            meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
            uid_match = re.search(r'UID (\d+)', meta)
            if not uid_match:
                continue
            uid = int(uid_match.group(1))
            server_read = '\\Seen' in meta
            email_id = existing_map.get(uid)
            if email_id:
                updates.append((email_id, server_read))

        if updates:
            for email_id, is_read in updates:
                _db.session.query(Email).filter_by(id=email_id).update(
                    {'is_read': is_read}, synchronize_session=False
                )
            _db.session.commit()
            self._ms._emit('flags_updated', {
                'account_id': account.id,
                'folder': 'INBOX',
                'updates': [{'id': eid, 'is_read': r} for eid, r in updates],
            })


# ── MailService ────────────────────────────────────────────────────────────────

class MailService:
    def __init__(self, db, socketio):
        self.db            = db
        self.socketio      = socketio
        self._folder_cache = {}
        self.idle_manager  = None  # inicializado pelo app após criar o contexto

    def init_idle(self, app):
        """Inicializa o IDLE manager e arranca todos as contas ativas."""
        self.idle_manager = IMAPIdleManager(self, app)
        with app.app_context():
            from database import Account as _Account
            for account in _Account.query.all():
                self.idle_manager.start_account(account.id)
        log.info('IDLE manager inicializado')

    def start_idle_for(self, account_id: int):
        if self.idle_manager:
            self.idle_manager.start_account(account_id)

    def stop_idle_for(self, account_id: int):
        if self.idle_manager:
            self.idle_manager.stop_account(account_id)

    def restart_idle_for(self, account_id: int):
        if self.idle_manager:
            self.idle_manager.restart_account(account_id)

    def _emit(self, event, data):
        try:
            self.socketio.emit(event, data)
        except Exception:
            pass

    def _get_conn(self, account: Account) -> imaplib.IMAP4:
        _pool.register(account)
        return _pool.get(account.id)

    def _safe_exec(self, account: Account, fn):
        try:
            conn = self._get_conn(account)
            return fn(conn)
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, BrokenPipeError) as e:
            log.warning(f'IMAP error, reconectando ({account.email}): {e}')
            _pool.invalidate(account.id)
            conn = self._get_conn(account)
            return fn(conn)

    # ── Test ──────────────────────────────────────────────────────────────────

    def test_connection(self, account: Account):
        errors = []
        try:
            _pool.register(account)
            _pool.invalidate(account.id)
            conn = _pool.get(account.id)
            conn.noop()
        except Exception as e:
            errors.append(f'IMAP: {e}')
        try:
            if account.smtp_ssl:
                s = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=10)
            else:
                s = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=10)
                if account.smtp_tls:
                    s.starttls()
            s.login(account.username, account.get_password())
            s.quit()
        except Exception as e:
            errors.append(f'SMTP: {e}')
        return {'ok': not errors, 'errors': errors}

    # ── Folders ───────────────────────────────────────────────────────────────

    def get_folders(self, account: Account):
        if account.id in self._folder_cache:
            return self._folder_cache[account.id]
        try:
            def _list(conn):
                _, folder_list = conn.list()
                return folder_list

            folder_list = self._safe_exec(account, _list)
            folders = []
            for item in folder_list:
                if isinstance(item, bytes):
                    decoded = item.decode('utf-8', errors='replace')
                    match = re.search(r'\(.*?\)\s+"?[./]"?\s+(.*)', decoded)
                    if match:
                        name = match.group(1).strip().strip('"')
                        folders.append(name)
            log.info(f'Pastas de {account.email}: {folders}')
            self._folder_cache[account.id] = folders
            return folders
        except Exception as e:
            log.error(f'get_folders error: {e}')
            return ['INBOX']

    def _find_folder(self, account: Account, keywords):
        folders = self.get_folders(account)
        for kw in keywords:
            for f in folders:
                if kw in f.lower():
                    return f
        return None

    def _get_sync_folders(self, account: Account):
        folders = self.get_folders(account)
        priority = ['inbox', 'sent', 'drafts', 'junk', 'spam', 'trash', 'archive']
        result = []
        for kw in priority:
            for f in folders:
                if kw in f.lower() and f not in result:
                    result.append(f)
        for f in folders:
            if f not in result:
                result.append(f)
        return result[:10]

    # ── Sync manual (para pastas não-INBOX e sync forçado) ────────────────────

    def sync_account_non_inbox(self, account_id: int):
        """Sync silencioso de todas as pastas EXCETO INBOX (já coberta pelo IDLE)."""
        from database import db as _db, Account as _Account
        account = _Account.query.get(account_id)
        if not account:
            return
        try:
            folders = self._get_sync_folders(account)
            # Remove INBOX — já coberta pelo IDLE
            folders = [f for f in folders if not f.upper().startswith('INBOX') or '.' in f]
            log.info(f'Sync outras pastas {account.email}: {folders}')
            for folder in folders:
                self._sync_folder(account, folder)
        except Exception as e:
            log.warning(f'sync_account_non_inbox error {account.email}: {e}')

    def sync_account(self, account_id: int):
        """
        Sync manual: busca e-mails novos em TODAS as pastas.
        Usado pelo botão de sync manual — IDLE já cuida da INBOX automaticamente.
        """
        from database import db as _db, Account as _Account
        account = _Account.query.get(account_id)
        if not account:
            return

        log.info(f'Sync manual {account.email}')
        self._emit('sync_start', {'account_id': account_id, 'email': account.email})
        self._folder_cache.pop(account_id, None)

        try:
            folders = self._get_sync_folders(account)
            total_new = 0
            total_flag_changes = 0

            for folder in folders:
                new, flag_changes = self._sync_folder(account, folder)
                total_new += new
                total_flag_changes += flag_changes

            account.last_sync = datetime.utcnow()
            _db.session.commit()
            self._emit('sync_done', {
                'account_id': account_id,
                'email': account.email,
                'new': total_new,
                'flag_changes': total_flag_changes,
            })

        except Exception as e:
            log.error(f'sync_account error: {e}')
            _pool.invalidate(account_id)
            self._emit('sync_error', {'account_id': account_id, 'error': str(e)})

    def _sync_folder(self, account: Account, folder: str):
        """Sync de uma pasta. Retorna (new_count, flag_change_count)."""
        from database import db as _db
        new_count    = 0
        flag_changes = 0

        try:
            def _select(conn):
                return conn.select(f'"{folder}"', readonly=True)

            status, _ = self._safe_exec(account, _select)
            if status != 'OK':
                return 0, 0

            def _search(conn):
                return conn.uid('search', None, 'ALL')

            _, data = self._safe_exec(account, _search)
            all_uids_bytes = data[0].split() if data[0] else []
            all_uids_server = {int(u) for u in all_uids_bytes}

            existing_rows = (
                _db.session.query(Email.uid, Email.id, Email.is_read)
                    .filter_by(account_id=account.id, folder=folder).all()
            )
            existing_uid_map = {row.uid: {'id': row.id, 'is_read': row.is_read}
                                for row in existing_rows}
            existing_uids = set(existing_uid_map.keys())

            new_uids_int = sorted(list(all_uids_server - existing_uids))[-200:]

            if new_uids_int:
                uid_str = ','.join(str(u) for u in new_uids_int)

                def _fetch(conn):
                    return conn.uid('fetch', uid_str, '(UID FLAGS BODY.PEEK[])')

                _, msg_data = self._safe_exec(account, _fetch)
                batch = []
                i = 0
                while i < len(msg_data):
                    item = msg_data[i]
                    if not isinstance(item, tuple):
                        i += 1
                        continue
                    meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                    raw  = item[1]
                    uid_match = re.search(r'UID (\d+)', meta)
                    if not uid_match:
                        i += 1
                        continue

                    uid     = int(uid_match.group(1))
                    is_read = '\\Seen' in meta

                    try:
                        email_obj, att_data = _parse_email_from_raw(
                            raw, uid, folder, account.id, is_read
                        )
                        batch.append((email_obj, att_data))
                        new_count += 1
                        if len(batch) >= BATCH_SIZE:
                            self._flush_batch(_db, account, batch)
                            batch = []
                    except Exception as e:
                        log.warning(f'Parse error UID {uid}: {e}')
                    i += 1

                if batch:
                    self._flush_batch(_db, account, batch)

            # Sync flags dos 500 UIDs mais recentes
            uids_to_check = sorted(existing_uids & all_uids_server)[-500:]
            if uids_to_check:
                uid_str = ','.join(str(u) for u in uids_to_check)

                def _fetch_flags(conn):
                    return conn.uid('fetch', uid_str, '(UID FLAGS)')

                try:
                    _, flag_data = self._safe_exec(account, _fetch_flags)
                    updates = []
                    for item in flag_data:
                        if not isinstance(item, (tuple, list)):
                            continue
                        meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                        uid_match = re.search(r'UID (\d+)', meta)
                        if not uid_match:
                            continue
                        uid = int(uid_match.group(1))
                        server_read = '\\Seen' in meta
                        row = existing_uid_map.get(uid)
                        if row and row['is_read'] != server_read:
                            updates.append((row['id'], server_read))

                    if updates:
                        for email_id, is_read in updates:
                            _db.session.query(Email).filter_by(id=email_id).update(
                                {'is_read': is_read}, synchronize_session=False
                            )
                        _db.session.commit()
                        flag_changes = len(updates)
                        self._emit('flags_updated', {
                            'account_id': account.id,
                            'folder': folder,
                            'updates': [{'id': eid, 'is_read': r} for eid, r in updates],
                        })
                except Exception as e:
                    log.warning(f'Flag sync error for {folder}: {e}')

        except Exception as e:
            log.error(f'_sync_folder {folder} error: {e}')
            _pool.invalidate(account.id)
            try:
                _db.session.rollback()
            except Exception:
                pass

        return new_count, flag_changes

    def _flush_batch(self, _db, account, batch):
        try:
            for email_obj, _ in batch:
                _db.session.add(email_obj)
            _db.session.flush()

            for email_obj, att_data in batch:
                for a in att_data:
                    att = Attachment(
                        email_id=email_obj.id,
                        filename=a['filename'],
                        content_type=a['content_type'],
                        size=a['size'],
                    )
                    try:
                        att.save_to_disk(a['data'])
                    except Exception as e:
                        log.warning(f'Falha ao salvar anexo em disco: {e}')
                        att.data = a['data']
                    _db.session.add(att)

            _db.session.commit()

            filters = list(
                EmailFilter.query.filter_by(account_id=account.id, enabled=True)
                    .order_by(EmailFilter.priority.asc()).all()
            )
            for email_obj, _ in batch:
                self._apply_filters(email_obj, filters)
                self._emit('new_email', {
                    'account_id': account.id,
                    'folder':     email_obj.folder,
                    'email':      email_obj.to_list_dict(),
                })

            enforce_email_limit(account.id)

        except Exception as e:
            log.error(f'_flush_batch error: {e}')
            try:
                _db.session.rollback()
            except Exception:
                pass

    def _apply_filters(self, email_obj: Email, filters: list):
        from database import db as _db
        for f in filters:
            if not f.matches(email_obj):
                continue
            changed = False
            if f.action_mark_read and not email_obj.is_read:
                email_obj.is_read = True
                changed = True
            if f.action_star and not email_obj.is_starred:
                email_obj.is_starred = True
                changed = True
            if f.action_delete:
                self.delete_email(email_obj)
                _db.session.delete(email_obj)
                _db.session.commit()
                return
            if f.action_move_to and email_obj.folder != f.action_move_to:
                self.move_email(email_obj, f.action_move_to)
                changed = True
            if changed:
                try:
                    _db.session.commit()
                except Exception:
                    _db.session.rollback()
            break

    # ── IMAP Search ───────────────────────────────────────────────────────────

    def search_imap(self, account_id: int, folder: str, query: str):
        from database import db as _db, Account as _Account
        account = _Account.query.get(account_id)
        if not account:
            return

        self._emit('imap_search_start', {'account_id': account_id, 'query': query})
        try:
            def _select(conn):
                return conn.select(f'"{folder}"', readonly=True)
            status, _ = self._safe_exec(account, _select)
            if status != 'OK':
                self._emit('imap_search_done', {
                    'account_id': account_id, 'found': 0,
                    'error': f'Pasta {folder} inacessível'
                })
                return

            safe_q = query.replace('"', '')
            criterion = f'TEXT "{safe_q}"'

            def _search(conn):
                return conn.uid('search', None, criterion)
            _, data = self._safe_exec(account, _search)
            uids = data[0].split() if data[0] else []

            if not uids:
                self._emit('imap_search_done', {'account_id': account_id, 'found': 0})
                return

            existing = set(
                row[0] for row in
                _db.session.query(Email.uid)
                    .filter_by(account_id=account_id, folder=folder).all()
            )
            new_uids = [u for u in uids if int(u) not in existing]

            if new_uids:
                uid_str = ','.join(u.decode() for u in new_uids[:100])
                def _fetch(conn):
                    return conn.uid('fetch', uid_str, '(UID FLAGS BODY.PEEK[])')
                _, msg_data = self._safe_exec(account, _fetch)

                batch = []
                i = 0
                while i < len(msg_data):
                    item = msg_data[i]
                    if not isinstance(item, tuple):
                        i += 1
                        continue
                    meta = item[0].decode() if isinstance(item[0], bytes) else str(item[0])
                    raw  = item[1]
                    uid_match = re.search(r'UID (\d+)', meta)
                    if not uid_match:
                        i += 1
                        continue
                    uid     = int(uid_match.group(1))
                    is_read = '\\Seen' in meta
                    try:
                        email_obj, att_data = _parse_email_from_raw(
                            raw, uid, folder, account_id, is_read
                        )
                        batch.append((email_obj, att_data))
                        if len(batch) >= BATCH_SIZE:
                            self._flush_batch(_db, account, batch)
                            batch = []
                    except Exception as e:
                        log.warning(f'search_imap parse error UID {uid}: {e}')
                    i += 1
                if batch:
                    self._flush_batch(_db, account, batch)

            self._emit('imap_search_done', {
                'account_id': account_id,
                'found': len(uids),
                'new': len(new_uids),
                'folder': folder,
                'query': query,
            })
        except Exception as e:
            log.error(f'search_imap error: {e}')
            self._emit('imap_search_done', {
                'account_id': account_id, 'found': 0, 'error': str(e)
            })

    # ── Flag operations ───────────────────────────────────────────────────────

    def mark_read_on_server(self, email_obj: Email):
        try:
            def _mark(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Seen')
            self._safe_exec(email_obj.account, _mark)
        except Exception as e:
            log.warning(f'mark_read error: {e}')

    def mark_unread_on_server(self, email_obj: Email):
        try:
            def _mark(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('store', str(email_obj.uid), '-FLAGS', '\\Seen')
            self._safe_exec(email_obj.account, _mark)
        except Exception as e:
            log.warning(f'mark_unread error: {e}')

    def set_spam(self, email_obj: Email, mark_as_spam: bool):
        from database import db as _db
        try:
            account = email_obj.account
            target  = self._find_folder(account, ['spam', 'junk']) if mark_as_spam else 'INBOX'
            if not target:
                return False
            if email_obj.folder.lower() == target.lower():
                return True

            def _move(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('copy', str(email_obj.uid), f'"{target}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
                conn.expunge()

            self._safe_exec(account, _move)
            email_obj.folder = target
            _db.session.commit()
            return True
        except Exception as e:
            log.warning(f'set_spam error: {e}')
            return False

    def delete_email(self, email_obj: Email):
        try:
            def _delete(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
                conn.expunge()
            self._safe_exec(email_obj.account, _delete)
        except Exception as e:
            log.warning(f'delete_email error: {e}')

    def move_email(self, email_obj: Email, target_folder: str):
        from database import db as _db
        try:
            def _move(conn):
                conn.select(f'"{email_obj.folder}"')
                conn.uid('copy', str(email_obj.uid), f'"{target_folder}"')
                conn.uid('store', str(email_obj.uid), '+FLAGS', '\\Deleted')
                conn.expunge()
            self._safe_exec(email_obj.account, _move)
            email_obj.folder = target_folder
            _db.session.commit()
        except Exception as e:
            log.warning(f'move_email error: {e}')

    # ── Send ──────────────────────────────────────────────────────────────────

    def send_email(self, account: Account, to: str, cc: str, bcc: str,
                   subject: str, body: str, body_html: str = '',
                   reply_to_id=None, attachments=None):
        try:
            if body_html:
                msg      = MIMEMultipart('mixed')
                alt_part = MIMEMultipart('alternative')
                alt_part.attach(MIMEText(body or '', 'plain', 'utf-8'))
                alt_part.attach(MIMEText(body_html, 'html', 'utf-8'))
                msg.attach(alt_part)
            else:
                msg = MIMEMultipart('mixed')
                msg.attach(MIMEText(body or '', 'plain', 'utf-8'))

            msg['From']    = f'{account.name} <{account.email}>'
            msg['To']      = to
            if cc: msg['Cc'] = cc
            msg['Subject'] = subject

            if reply_to_id:
                orig = Email.query.get(reply_to_id)
                if orig and orig.message_id:
                    msg['In-Reply-To'] = orig.message_id
                    msg['References']  = orig.message_id

            if attachments:
                for f in attachments:
                    if not f or not f.filename:
                        continue
                    data = f.read()
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(data)
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition',
                                    f'attachment; filename="{f.filename}"')
                    msg.attach(part)

            recipients = [r.strip() for r in to.split(',') if r.strip()]
            if cc:  recipients += [r.strip() for r in cc.split(',') if r.strip()]
            if bcc: recipients += [r.strip() for r in bcc.split(',') if r.strip()]

            if account.smtp_ssl:
                s = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=30)
            else:
                s = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30)
                if account.smtp_tls:
                    s.starttls()
            s.login(account.username, account.get_password())
            s.sendmail(account.email, recipients, msg.as_string())
            s.quit()

            try:
                sent_folder = self._find_folder(account, ['sent'])
                if sent_folder:
                    raw = msg.as_bytes()
                    def _append(conn):
                        conn.append(f'"{sent_folder}"', '\\Seen', None, raw)
                    self._safe_exec(account, _append)
            except Exception as e:
                log.warning(f'Could not save to Sent: {e}')

            return {'ok': True}
        except Exception as e:
            log.error(f'send_email error: {e}')
            return {'ok': False, 'error': str(e)}