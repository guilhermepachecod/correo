"""
services/mail_service.py
Operações de e-mail: sync de pastas (incremental, por UID — Fase 2.4),
ações servidor-primeiro (Fase 2.6), envio SMTP, pastas especiais (2.3).

Depende de: imap_connection (conexão persistente por conta), email_parser,
database. O IMAPIdleManager é injetado externamente (evita import circular).

Fase 2.4-2.7: substitui o antigo services/imap_pool.py (uma conexão nova por
operação — bug B8/fail2ban) por services/imap_connection.py (conexão
persistente, imapclient). Sync por pasta agora é por UID com UIDVALIDITY
(bugs B2/B3/B5/B6/B9) e as ações (delete/move/spam) só tocam o banco local
depois que o servidor confirma, e nunca fazem EXPUNGE genérico (bugs B1/B7).
"""
import json
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from imapclient import IMAPClient

from database import db, Email, Attachment, EmailFilter, Account, FolderState, \
    resolve_thread_id, BODY_RETENTION_DAYS, MAX_BODIES_PER_ACCOUNT
from services.imap_connection import manager as conn_manager, AuthCircuitOpen, get_capabilities
from services.email_parser import parse_email_from_raw
from services.special_folders import detect_role

log = logging.getLogger(__name__)

BATCH_SIZE = 50         # mensagens completas (corpo) por lote de fetch
FLAGS_BATCH_SIZE = 200  # só FLAGS é bem mais leve — lote maior sem problema


def _flag_str(flag) -> str:
    return flag.decode('ascii', errors='ignore') if isinstance(flag, bytes) else str(flag)


def _has_flag(flags, name: str) -> bool:
    return any(_flag_str(f) == name for f in (flags or ()))


class MailService:

    def __init__(self, socketio):
        self.socketio      = socketio
        self._folder_cache = {}
        self.idle_manager  = None  # injetado por app.py após subir

    # ── Emit ──────────────────────────────────────────────────────────────────

    def _emit(self, event: str, data: dict) -> None:
        try:
            self.socketio.emit(event, data)
        except Exception:
            pass

    # ── Conexão (Fase 2.2/2.4) ───────────────────────────────────────────────

    def _get_connection(self, account: Account):
        return conn_manager.register(
            account.id, account.imap_host, account.imap_port, account.imap_ssl,
            account.username, account.get_password,
            on_auth_error=self._on_auth_error, on_capabilities=self._on_capabilities,
        )

    def _safe_exec(self, account: Account, fn):
        """Executa fn(client) na conexão persistente da conta (imapclient) —
        substitui o imap_pool antigo (uma conexão/login por operação)."""
        return self._get_connection(account).execute(fn)

    def _on_auth_error(self, account_id: int, message: str) -> None:
        account = Account.query.get(account_id)
        if not account:
            return
        account.auth_error    = message
        account.auth_error_at = datetime.utcnow()
        db.session.commit()
        log.error(f'Erro de autenticação permanente account_id={account_id}: {message}')
        self._emit('sync_error', {
            'account_id': account_id,
            'error': f'Erro de autenticação: verifique usuário/senha ({message})',
            'auth_failed': True,
        })

    def _on_capabilities(self, account_id: int, caps: set) -> None:
        account = Account.query.get(account_id)
        if not account:
            return
        account.capabilities = json.dumps(sorted(caps))
        db.session.commit()

    def reset_auth_error(self, account_id: int) -> None:
        """Chamado quando a conta é editada ou o usuário pede retry manual —
        único jeito de uma conta bloqueada por auth voltar a tentar logar."""
        conn_manager.reset_auth_error(account_id)
        account = Account.query.get(account_id)
        if account and account.auth_error:
            account.auth_error    = None
            account.auth_error_at = None
            db.session.commit()

    def _capabilities(self, account: Account) -> set:
        """Lê a CAPABILITY cacheada; se nunca foi cacheada (ou o cache veio
        vazio), busca na conexão persistente já aberta — sem login novo —
        em vez de assumir 'sem UIDPLUS' e silenciosamente nunca expurgar
        nada. Era essa suposição errada que fazia 'excluir' parar de
        funcionar (ficava só flagando \\Deleted sem nunca confirmar) em
        contas cuja capability ainda não tinha sido cacheada."""
        if account.capabilities:
            try:
                cached = set(json.loads(account.capabilities))
                if cached:
                    return cached
            except Exception:
                pass
        try:
            caps = self._safe_exec(account, lambda c: get_capabilities(c))
        except Exception as e:
            log.warning(f'_capabilities fetch error {account.email}: {e}')
            return set()
        self._on_capabilities(account.id, caps)
        return caps

    # ── Test ──────────────────────────────────────────────────────────────────

    def test_connection(self, account: Account) -> dict:
        """Teste de conectividade avulso — usa uma conexão própria, não a
        persistente da conta (não deve interferir no estado do IDLE/sync)."""
        errors = []
        try:
            client = IMAPClient(account.imap_host, port=account.imap_port,
                                ssl=account.imap_ssl, timeout=10)
            try:
                client.login(account.username, account.get_password())
                client.noop()
            finally:
                try: client.shutdown()
                except Exception: pass
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

    # ── Pastas (Fase 2.3) ────────────────────────────────────────────────────

    def get_folders(self, account: Account) -> list[str]:
        if account.id in self._folder_cache:
            return self._folder_cache[account.id]
        try:
            raw = self._safe_exec(account, lambda c: c.list_folders())
            folders = [name for _flags, _delim, name in raw]
            self._folder_cache[account.id] = folders
            return folders
        except Exception as e:
            log.error(f'get_folders error {account.email}: {e}')
            return ['INBOX']

    def refresh_folder_roles(self, account: Account) -> dict[str, str | None]:
        """Detecta o role de cada pasta (SPECIAL-USE ou nome) e grava em
        FolderState — base das ações servidor-primeiro (2.6) e das visões
        unificadas (2.9)."""
        result: dict[str, str | None] = {}
        try:
            raw = self._safe_exec(account, lambda c: c.list_folders())
        except Exception as e:
            log.warning(f'refresh_folder_roles error {account.email}: {e}')
            return result

        existing = {fs.folder: fs for fs in
                    FolderState.query.filter_by(account_id=account.id).all()}
        for flags, _delim, name in raw:
            role = detect_role(name, flags)
            result[name] = role
            fs = existing.get(name)
            if fs:
                fs.role = role
            else:
                fs = FolderState(account_id=account.id, folder=name, role=role)
                db.session.add(fs)
                existing[name] = fs
        db.session.commit()
        return result

    def list_folder_uids(self, account: Account, folder: str) -> list[int]:
        """UIDs ao vivo no servidor pra essa pasta — usado pelas ações 'toda
        a pasta' (ids vazio, ex. 'apagar todos'), que não podem ficar
        limitadas ao que o sync local já alcançou: numa pasta grande
        (ex.: Lixeira com milhares de mensagens) isso fazia 'apagar todos'
        parecer que processou tudo mas só afetava as primeiras centenas
        já sincronizadas."""
        self._safe_exec(account, lambda c: c.select_folder(folder, readonly=True))
        return sorted(self._safe_exec(account, lambda c: c.search('ALL')))

    def role_folder(self, account: Account, role: str) -> str | None:
        """Pasta com o role dado (ex. 'trash', 'sent', 'junk'). Fallback por
        palavra-chave no nome se refresh_folder_roles ainda não rodou pra
        essa conta."""
        fs = FolderState.query.filter_by(account_id=account.id, role=role).first()
        if fs:
            return fs.folder
        keywords = {
            'trash': ['trash', 'deleted', 'lixeira'],
            'junk':  ['spam', 'junk'],
            'sent':  ['sent', 'enviados'],
            'drafts': ['drafts', 'rascunhos'],
            'archive': ['archive', 'arquivo'],
            'inbox': ['inbox'],
        }.get(role, [role])
        return self._find_folder_by_keyword(account, keywords)

    def _find_folder_by_keyword(self, account: Account, keywords: list[str]) -> str | None:
        folders = self.get_folders(account)
        for kw in keywords:
            for f in folders:
                if f.lower() == kw:
                    return f
        for kw in keywords:
            for f in folders:
                if kw in f.lower():
                    return f
        return None

    def sync_folders_to_track(self, account: Account) -> list[str]:
        """Pastas que entram no ciclo de poll periódico (role conhecido) —
        IDLE cobre a INBOX, isso aqui cobre o resto (2.5)."""
        rows = FolderState.query.filter_by(account_id=account.id).filter(
            FolderState.role.isnot(None), FolderState.role != 'inbox').all()
        return [r.folder for r in rows]

    # ── Sync incremental por pasta (Fase 2.4) ────────────────────────────────

    def sync_folder_incremental(self, account: Account, folder: str) -> dict:
        """UID + UIDVALIDITY, sem cap que descarta mensagem, sem EXPUNGE,
        sem número de sequência — resolve B2, B3, B5, B6, B9."""
        try:
            select_info = self._safe_exec(account, lambda c: c.select_folder(folder, readonly=False))
        except AuthCircuitOpen:
            return {'ok': False, 'error': 'auth_blocked'}
        except Exception as e:
            log.warning(f'sync_folder_incremental select error {account.email}/{folder}: {e}')
            return {'ok': False, 'error': str(e)}

        uidvalidity = select_info.get(b'UIDVALIDITY')
        uidnext_srv = select_info.get(b'UIDNEXT')

        fs = FolderState.query.filter_by(account_id=account.id, folder=folder).first()
        if not fs:
            fs = FolderState(account_id=account.id, folder=folder)
            db.session.add(fs)
            db.session.flush()

        if fs.uidvalidity is not None and uidvalidity is not None and fs.uidvalidity != uidvalidity:
            log.warning(f'UIDVALIDITY mudou {account.email}/{folder}: '
                        f'{fs.uidvalidity} -> {uidvalidity}; reindexando pasta')
            removed = Email.query.filter_by(account_id=account.id, folder=folder).all()
            for e in removed:
                for att in e.attachments:
                    att.delete_from_disk()
            Email.query.filter_by(account_id=account.id, folder=folder).delete(synchronize_session=False)
            db.session.commit()
            fs.uidvalidity = None

        try:
            all_uids = set(self._safe_exec(account, lambda c: c.search('ALL')))
        except Exception as e:
            log.warning(f'sync_folder_incremental search error {account.email}/{folder}: {e}')
            return {'ok': False, 'error': str(e)}

        existing_rows = {r.uid: r for r in
                         Email.query.filter_by(account_id=account.id, folder=folder).all()}
        existing_uids = set(existing_rows)

        new_uids = sorted(all_uids - existing_uids)
        new_count = 0
        for i in range(0, len(new_uids), BATCH_SIZE):
            batch = new_uids[i:i + BATCH_SIZE]
            try:
                data = self._safe_exec(account, lambda c: c.fetch(batch, ['FLAGS', 'BODY.PEEK[]']))
            except Exception as e:
                # Um lote falhando (ex.: pasta com milhares de mensagens,
                # comando longo demais pro servidor) não pode travar o
                # resto — continua tentando os próximos lotes.
                log.warning(f'sync_folder_incremental fetch error {account.email}/{folder}: {e}')
                continue
            new_count += self._save_fetched(account, folder, data)

        flag_changes = 0
        check_uids = sorted(existing_uids & all_uids)
        for i in range(0, len(check_uids), FLAGS_BATCH_SIZE):
            batch = check_uids[i:i + FLAGS_BATCH_SIZE]
            try:
                flag_data = self._safe_exec(account, lambda c: c.fetch(batch, ['FLAGS']))
                flag_changes += self._apply_flag_updates(account, folder, flag_data, existing_rows)
            except Exception as e:
                log.warning(f'sync_folder_incremental flags error {account.email}/{folder}: {e}')

        removed_rows = [existing_rows[u] for u in (existing_uids - all_uids)]
        if removed_rows:
            removed_ids = [r.id for r in removed_rows]
            for r in removed_rows:
                for att in r.attachments:
                    att.delete_from_disk()
            Email.query.filter(Email.id.in_(removed_ids)).delete(synchronize_session=False)
            db.session.commit()
            self._emit('emails_removed', {'account_id': account.id, 'folder': folder, 'ids': removed_ids})

        fs.uidvalidity = uidvalidity
        fs.uidnext     = uidnext_srv
        fs.last_sync   = datetime.utcnow()
        db.session.commit()

        prune_old_bodies(account.id)

        return {'ok': True, 'new': new_count, 'flag_changes': flag_changes, 'removed': len(removed_rows)}

    def _save_fetched(self, account: Account, folder: str, data: dict) -> int:
        saved = 0
        filters = list(EmailFilter.query.filter_by(account_id=account.id, enabled=True)
                                        .order_by(EmailFilter.priority.asc()).all())
        for uid, item in data.items():
            raw = item.get(b'BODY[]') or item.get(b'RFC822')
            if not raw:
                continue
            flags      = item.get(b'FLAGS', ())
            is_read    = _has_flag(flags, '\\Seen')
            is_starred = _has_flag(flags, '\\Flagged')
            try:
                email_obj, att_data = parse_email_from_raw(
                    raw, uid, folder, account.id, is_read, resolve_thread_id)
                email_obj.is_starred = is_starred
                db.session.add(email_obj)
                db.session.flush()
                for a in att_data:
                    att = Attachment(email_id=email_obj.id, filename=a['filename'],
                                     content_type=a['content_type'], size=a['size'])
                    try: att.save_to_disk(a['data'])
                    except Exception: att.data = a['data']
                    db.session.add(att)
                db.session.commit()
                self._apply_filters(email_obj, filters)
                self._emit('new_email', {
                    'account_id': account.id, 'folder': email_obj.folder,
                    'email': email_obj.to_list_dict(),
                })
                saved += 1
            except Exception as e:
                log.warning(f'parse error account={account.id} uid={uid}: {e}')
                try: db.session.rollback()
                except Exception: pass
        return saved

    def _apply_flag_updates(self, account: Account, folder: str, flag_data: dict,
                            existing_rows: dict) -> int:
        updates = []
        for uid, item in flag_data.items():
            row = existing_rows.get(uid)
            if not row:
                continue
            flags          = item.get(b'FLAGS', ())
            server_read    = _has_flag(flags, '\\Seen')
            server_starred = _has_flag(flags, '\\Flagged')
            changed = {}
            if row.is_read != server_read:
                changed['is_read'] = server_read
            if row.is_starred != server_starred:
                changed['is_starred'] = server_starred
            if changed:
                updates.append((row.id, changed))
        if not updates:
            return 0
        for email_id, changed in updates:
            Email.query.filter_by(id=email_id).update(changed, synchronize_session=False)
        db.session.commit()
        self._emit('flags_updated', {
            'account_id': account.id, 'folder': folder,
            'updates': [{'id': eid, **changed} for eid, changed in updates],
        })
        return len(updates)

    def fetch_raw_source(self, email_obj: Email) -> str | None:
        """Fonte bruta (headers + corpo, como armazenada no servidor) pra
        diagnóstico — ex.: checar se o 'Received' vaza o IP real do servidor
        de e-mail. BODY.PEEK[] não marca \\Seen; nada é gravado no banco."""
        account = email_obj.account
        try:
            self._safe_exec(account, lambda c: c.select_folder(email_obj.folder, readonly=True))
            data = self._safe_exec(account, lambda c: c.fetch([email_obj.uid], ['BODY.PEEK[]']))
        except Exception as e:
            log.warning(f'fetch_raw_source error email_id={email_obj.id}: {e}')
            return None
        item = data.get(email_obj.uid)
        if not item:
            return None
        raw = item.get(b'BODY[]') or item.get(b'RFC822')
        if not raw:
            return None
        return raw.decode('utf-8', errors='replace')

    def fetch_full_body(self, email_obj: Email) -> bool:
        """Poda de corpo (2.7): quando body_cached=false, busca o corpo
        completo sob demanda e regrava no banco (Email volta a body_cached=true)."""
        account = email_obj.account
        try:
            self._safe_exec(account, lambda c: c.select_folder(email_obj.folder, readonly=True))
            data = self._safe_exec(account, lambda c: c.fetch([email_obj.uid], ['BODY.PEEK[]']))
        except Exception as e:
            log.warning(f'fetch_full_body error email_id={email_obj.id}: {e}')
            return False
        item = data.get(email_obj.uid)
        if not item:
            return False
        raw = item.get(b'BODY[]') or item.get(b'RFC822')
        if not raw:
            return False
        try:
            parsed, att_data = parse_email_from_raw(
                raw, email_obj.uid, email_obj.folder, account.id, email_obj.is_read, resolve_thread_id)
        except Exception as e:
            log.warning(f'fetch_full_body parse error email_id={email_obj.id}: {e}')
            return False

        for att in list(email_obj.attachments):
            att.delete_from_disk()
            db.session.delete(att)
        email_obj.body_text      = parsed.body_text
        email_obj.body_html      = parsed.body_html
        email_obj.has_attachments = bool(att_data)
        email_obj.body_cached    = True
        db.session.flush()
        for a in att_data:
            att = Attachment(email_id=email_obj.id, filename=a['filename'],
                             content_type=a['content_type'], size=a['size'])
            try: att.save_to_disk(a['data'])
            except Exception: att.data = a['data']
            db.session.add(att)
        db.session.commit()
        return True

    # ── Sync manual (botão "sincronizar") ────────────────────────────────────

    def sync_account(self, account_id: int) -> None:
        account = Account.query.get(account_id)
        if not account:
            return
        log.info(f'Sync manual {account.email}')
        self._emit('sync_start', {'account_id': account_id, 'email': account.email})
        self._folder_cache.pop(account_id, None)
        try:
            self.refresh_folder_roles(account)
            folders = self.get_folders(account)
            total_new, total_flags = 0, 0
            for folder in folders:
                result = self.sync_folder_incremental(account, folder)
                if result.get('ok'):
                    total_new   += result.get('new', 0)
                    total_flags += result.get('flag_changes', 0)
            account.last_sync = datetime.utcnow()
            db.session.commit()
            self._emit('sync_done', {
                'account_id': account_id, 'email': account.email,
                'new': total_new, 'flag_changes': total_flags,
            })
        except AuthCircuitOpen:
            pass  # _on_auth_error já emitiu sync_error
        except Exception as e:
            log.error(f'sync_account error {account.email}: {e}')
            self._emit('sync_error', {'account_id': account_id, 'error': str(e)})

    # ── IMAP Search ───────────────────────────────────────────────────────────

    def search_imap(self, account_id: int, folder: str, query: str) -> None:
        account = Account.query.get(account_id)
        if not account:
            return
        self._emit('imap_search_start', {'account_id': account_id, 'query': query})
        try:
            self._safe_exec(account, lambda c: c.select_folder(folder, readonly=True))
            uids = self._safe_exec(account, lambda c: c.search(['TEXT', query]))
            if not uids:
                self._emit('imap_search_done', {'account_id': account_id, 'found': 0})
                return
            existing = {r[0] for r in db.session.query(Email.uid)
                                                 .filter_by(account_id=account_id, folder=folder).all()}
            new_uids = [u for u in uids if u not in existing]
            saved = 0
            for i in range(0, min(len(new_uids), 100), BATCH_SIZE):
                batch = new_uids[i:i + BATCH_SIZE]
                data = self._safe_exec(account, lambda c: c.fetch(batch, ['FLAGS', 'BODY.PEEK[]']))
                saved += self._save_fetched(account, folder, data)
            self._emit('imap_search_done', {
                'account_id': account_id, 'found': len(uids),
                'new': saved, 'folder': folder, 'query': query,
            })
        except Exception as e:
            log.error(f'search_imap error {account.email}: {e}')
            self._emit('imap_search_done', {'account_id': account_id, 'found': 0, 'error': str(e)})

    # ── Filtros automáticos ───────────────────────────────────────────────────

    def _apply_filters(self, email_obj: Email, filters: list) -> None:
        for f in filters:
            if not f.matches(email_obj):
                continue
            changed = False
            if f.action_mark_read and not email_obj.is_read:
                email_obj.is_read = True; changed = True
            if f.action_star and not email_obj.is_starred:
                email_obj.is_starred = True; changed = True
            if f.action_delete:
                self.delete_email(email_obj)
                return
            if f.action_move_to and email_obj.folder != f.action_move_to:
                self.move_email(email_obj, f.action_move_to); changed = True
            if changed:
                try: db.session.commit()
                except Exception: db.session.rollback()
            break

    # ── Ações servidor-primeiro (Fase 2.6) ───────────────────────────────────
    # Todas devolvem {'ok': bool, ...}. O banco local só muda depois que o
    # servidor confirma. EXPUNGE sempre com UID (uid_expunge, UIDPLUS) —
    # nunca o EXPUNGE genérico que apagaria \Deleted de outro cliente (B7).

    def mark_read_on_server(self, email_obj: Email, account: Account = None) -> bool:
        acc = account or email_obj.account
        try:
            self._safe_exec(acc, lambda c: (
                c.select_folder(email_obj.folder, readonly=False),
                c.add_flags([email_obj.uid], ['\\Seen']),
            ))
            return True
        except Exception as e:
            log.warning(f'mark_read_on_server error email_id={email_obj.id}: {e}')
            return False

    def mark_unread_on_server(self, email_obj: Email, account: Account = None) -> bool:
        acc = account or email_obj.account
        try:
            self._safe_exec(acc, lambda c: (
                c.select_folder(email_obj.folder, readonly=False),
                c.remove_flags([email_obj.uid], ['\\Seen']),
            ))
            return True
        except Exception as e:
            log.warning(f'mark_unread_on_server error email_id={email_obj.id}: {e}')
            return False

    def mark_star_on_server(self, email_obj: Email, starred: bool, account: Account = None) -> bool:
        acc = account or email_obj.account
        flag_fn = 'add_flags' if starred else 'remove_flags'
        try:
            self._safe_exec(acc, lambda c: (
                c.select_folder(email_obj.folder, readonly=False),
                getattr(c, flag_fn)([email_obj.uid], ['\\Flagged']),
            ))
            return True
        except Exception as e:
            log.warning(f'mark_star_on_server error email_id={email_obj.id}: {e}')
            return False

    def _move_or_copy_delete(self, conn_exec, uid: int, source: str, target: str, caps: set) -> None:
        """fn interna: seleciona source, move pra target (UID MOVE se
        suportado, senão COPY + flag \\Deleted + UID EXPUNGE se UIDPLUS)."""
        has_move    = 'MOVE' in caps
        has_uidplus = 'UIDPLUS' in caps

        def _op(c):
            c.select_folder(source, readonly=False)
            if has_move:
                c.move([uid], target)
                return
            c.copy([uid], target)
            c.add_flags([uid], ['\\Deleted'])
            if has_uidplus:
                c.uid_expunge([uid])
            # sem UIDPLUS: mensagem fica duplicada (copiada + flagada no
            # original) até algum cliente expurgar — nunca chamamos
            # EXPUNGE genérico aqui (apagaria \Deleted de outro cliente).
        conn_exec(_op)

    def move_email(self, email_obj: Email, target_folder: str, account: Account = None) -> dict:
        acc = account or email_obj.account
        if not email_obj.uid:
            return {'ok': False, 'error': 'uid ausente'}
        caps = self._capabilities(acc)
        try:
            self._move_or_copy_delete(
                lambda fn: self._safe_exec(acc, fn),
                email_obj.uid, email_obj.folder, target_folder, caps)
        except Exception as e:
            log.warning(f'move_email error email_id={email_obj.id}: {e}')
            return {'ok': False, 'error': str(e)}
        email_obj.folder = target_folder
        db.session.commit()
        return {'ok': True}

    def set_spam(self, email_obj: Email, mark_as_spam: bool, account: Account = None) -> dict:
        acc = account or email_obj.account
        target = self.role_folder(acc, 'junk') if mark_as_spam else (self.role_folder(acc, 'inbox') or 'INBOX')
        if not target:
            return {'ok': False, 'error': 'pasta de spam/inbox não encontrada'}
        if email_obj.folder == target:
            return {'ok': True, 'folder': email_obj.folder}
        return self.move_email(email_obj, target, acc)

    def delete_email(self, email_obj: Email, account: Account = None) -> dict:
        acc = account or email_obj.account
        uid = email_obj.uid
        folder = email_obj.folder
        if not uid:
            return {'ok': False, 'error': 'uid ausente'}

        trash = self.role_folder(acc, 'trash')
        caps  = self._capabilities(acc)
        has_uidplus = 'UIDPLUS' in caps

        try:
            if trash and folder != trash:
                self._move_or_copy_delete(lambda fn: self._safe_exec(acc, fn), uid, folder, trash, caps)
                email_obj.folder = trash
                db.session.commit()
                return {'ok': True, 'permanent': False}

            # já está na lixeira (ou não existe lixeira) — exclusão definitiva
            def _purge(c):
                c.select_folder(folder, readonly=False)
                c.add_flags([uid], ['\\Deleted'])
                if has_uidplus:
                    c.uid_expunge([uid])
            self._safe_exec(acc, _purge)

            if not has_uidplus:
                return {'ok': True, 'permanent': False,
                        'warning': 'Servidor sem UIDPLUS: mensagem marcada para exclusão, '
                                   'mas não removida definitivamente ainda.'}

            for att in email_obj.attachments:
                att.delete_from_disk()
            db.session.delete(email_obj)
            db.session.commit()
            return {'ok': True, 'permanent': True}
        except Exception as e:
            log.error(f'delete_email error (folder={folder} uid={uid}): {e}')
            return {'ok': False, 'error': str(e)}

    # ── Send ──────────────────────────────────────────────────────────────────

    def send_email(self, account: Account, to: str, cc: str, bcc: str,
                   subject: str, body: str, body_html: str = '',
                   reply_to_id=None, attachments=None) -> dict:
        try:
            if body_html:
                msg = MIMEMultipart('mixed')
                alt = MIMEMultipart('alternative')
                alt.attach(MIMEText(body or '', 'plain', 'utf-8'))
                alt.attach(MIMEText(body_html, 'html', 'utf-8'))
                msg.attach(alt)
            else:
                msg = MIMEMultipart('mixed')
                msg.attach(MIMEText(body or '', 'plain', 'utf-8'))

            msg['From']    = f'{account.name} <{account.email}>'
            msg['To']      = to
            msg['Subject'] = subject
            if cc: msg['Cc'] = cc

            if reply_to_id:
                orig = Email.query.get(reply_to_id)
                if orig and orig.message_id:
                    msg['In-Reply-To'] = orig.message_id
                    msg['References']  = orig.message_id

            for f in (attachments or []):
                if not f or not f.filename: continue
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{f.filename}"')
                msg.attach(part)

            recipients = [r.strip() for r in to.split(',') if r.strip()]
            if cc:  recipients += [r.strip() for r in cc.split(',') if r.strip()]
            if bcc: recipients += [r.strip() for r in bcc.split(',') if r.strip()]

            if account.smtp_ssl:
                s = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=30)
            else:
                s = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30)
                if account.smtp_tls: s.starttls()
            s.login(account.username, account.get_password())
            s.sendmail(account.email, recipients, msg.as_string())
            s.quit()

            if account.save_sent_copy:
                try:
                    sent_folder = self.role_folder(account, 'sent')
                    if sent_folder:
                        raw = msg.as_bytes()
                        self._safe_exec(account, lambda c: c.append(sent_folder, raw, flags=['\\Seen']))
                except Exception as e:
                    log.warning(f'Could not save to Sent: {e}')

            return {'ok': True}
        except Exception as e:
            log.error(f'send_email error: {e}')
            return {'ok': False, 'error': str(e)}


# ── Poda de armazenamento (Fase 2.7, resolve B2) ─────────────────────────────

def prune_old_bodies(account_id: int) -> int:
    """Substitui enforce_email_limit (que apagava linhas → re-download
    infinito). Poda corpo/anexos de mensagens além do limite configurado ou
    mais antigas que a retenção, marcando body_cached=false. O cabeçalho
    continua no banco e a mensagem continua na lista; o corpo volta sob
    demanda (MailService.fetch_full_body)."""
    cutoff = datetime.utcnow() - timedelta(days=BODY_RETENTION_DAYS)
    total = db.session.query(db.func.count(Email.id)).filter_by(account_id=account_id).scalar() or 0

    to_prune = {}
    if total > MAX_BODIES_PER_ACCOUNT:
        over_limit = (Email.query.filter_by(account_id=account_id, body_cached=True)
                                 .order_by(Email.date.asc())
                                 .limit(total - MAX_BODIES_PER_ACCOUNT).all())
        for e in over_limit:
            to_prune[e.id] = e

    old_ones = (Email.query.filter_by(account_id=account_id, body_cached=True)
                           .filter(Email.date < cutoff).all())
    for e in old_ones:
        to_prune[e.id] = e

    for email_obj in to_prune.values():
        for att in list(email_obj.attachments):
            att.delete_from_disk()
            db.session.delete(att)
        email_obj.body_text   = ''
        email_obj.body_html   = ''
        email_obj.body_cached = False

    if to_prune:
        db.session.commit()
    return len(to_prune)
