"""
Testa o lote de UIDs em bulk_action (routes/emails.py) — o bug real
reportado: 'excluir todos' numa Lixeira com ~3000 mensagens não fazia
nada. Suspeita confirmada por inspeção: um único comando IMAP com
milhares de UIDs é frágil (linha de comando longa demais pro servidor) —
agora todo comando sai em lotes de BULK_IMAP_BATCH UIDs, e um lote que
falha não aborta os outros.

Também cobre o bug seguinte: com ids=[] (ação na pasta inteira), os UIDs
têm que vir ao vivo do servidor (c.search('ALL')), não só do que o sync
local já alcançou — senão numa pasta grande "apagar todos" só processava
as primeiras mensagens já sincronizadas e parava silenciosamente.

E o terceiro: "pasta inteira" roda em background (thread + evento
bulk_done via Socket.IO), não mais bloqueando a requisição HTTP — uma
exclusão de milhares de mensagens, rodando de forma síncrona dentro do
request, podia passar do timeout do gunicorn/proxy: o servidor terminava
a exclusão certinho, mas o navegador recebia erro de conexão (parecia
"0 processado" mesmo com tudo apagado de verdade).
"""
import time

import pytest

from app import app
from database import db, Account, Email, FolderState
from routes.emails import BULK_IMAP_BATCH
import routes.emails as emails_module


@pytest.fixture(autouse=True)
def _reset_whole_folder_lock():
    # Trava global de routes/emails.py (_bulk_whole_folder_inflight) — sem
    # limpar entre testes, uma chave (account_id, folder) de um teste
    # anterior pode "sobreviver" se o account_id for reaproveitado (SQLite
    # reusa rowid depois de Account.query.delete()), fazendo um teste
    # seguinte ver a pasta como "já em andamento" por engano.
    emails_module._bulk_whole_folder_inflight.clear()
    yield
    emails_module._bulk_whole_folder_inflight.clear()


class BatchRecordingClient:
    def __init__(self, fail_on_uid=None, all_uids=None):
        self.calls = []
        self.fail_on_uid = fail_on_uid
        self.all_uids = all_uids or []

    def select_folder(self, folder, readonly=False):
        self.calls.append(('select_folder', folder))
        return {}

    def search(self, criteria):
        self.calls.append(('search', criteria))
        return list(self.all_uids)

    def add_flags(self, uids, flags):
        if self.fail_on_uid and self.fail_on_uid in uids:
            raise RuntimeError('servidor recusou o comando (lote grande demais)')
        self.calls.append(('add_flags', tuple(uids), tuple(flags)))

    def uid_expunge(self, uids):
        self.calls.append(('uid_expunge', tuple(uids)))
        # Reflete a exclusão de verdade — sem isso, um SEARCH ALL depois do
        # expunge continuaria achando os UIDs já apagados (ver teste de
        # re-sync pós-ação em bulk_action).
        self.all_uids = [u for u in self.all_uids if u not in uids]

    def expunge(self):
        raise AssertionError('expunge() genérico chamado — regressão do bug B7')


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def _seed_many(n=520):
    db.session.query(Email).delete()
    db.session.query(FolderState).delete()
    db.session.query(Account).delete()
    db.session.commit()
    acc = Account(name='t', email='bigtrash@example.com', imap_host='h', smtp_host='s',
                  username='bigtrash@example.com', password_enc='x', capabilities='["UIDPLUS"]')
    db.session.add(acc)
    db.session.commit()
    for uid in range(1, n + 1):
        db.session.add(Email(account_id=acc.id, folder='INBOX.Trash', uid=uid,
                             subject=f'msg {uid}', from_addr='a@b.com', is_read=True))
    db.session.commit()
    return acc


def _wait_for_bulk_done(events, timeout=10):
    for _ in range(int(timeout / 0.05)):
        done = [e for e in events if e[0] == 'bulk_done']
        if done:
            return done[-1][1]
        time.sleep(0.05)
    raise AssertionError('bulk_done nunca chegou — a thread de background travou ou não emitiu')


def test_whole_folder_action_responds_immediately_async(monkeypatch):
    """A rota não pode mais bloquear a requisição HTTP numa ação de pasta
    inteira — devolve na hora, o resultado real vem pelo evento."""
    import extensions

    with app.app_context():
        acc = _seed_many(10)
        account_id = acc.id
        fake_client = BatchRecordingClient(all_uids=list(range(1, 11)))
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)
        monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: None)

    client = app.test_client()
    _login(client)

    r = client.post('/api/emails/bulk', json={'action': 'delete', 'ids': [], 'folder': 'INBOX.Trash',
                                               'account_id': account_id},
                    headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200
    assert r.get_json() == {'ok': True, 'async': True}


def test_second_whole_folder_request_on_same_folder_is_rejected_while_first_runs(monkeypatch):
    """Bug real em produção: dois cliques (ou duas abas) disparando 'apagar
    todos' na mesma pasta ao mesmo tempo rodavam duas threads em paralelo,
    cada uma reprocessando/reexpurgando os mesmos UIDs. A rota recusa (409)
    uma segunda ação de pasta inteira enquanto a primeira ainda não terminou
    pra essa mesma (account_id, folder).

    Testado direto na trava (_bulk_whole_folder_inflight), sem threading
    real nem Flask test client concorrente — duas tentativas anteriores
    (time.sleep fixo, depois threading.Event) deram resultados diferentes
    e flakeys entre o sandbox local e o runner do GitHub Actions (timing
    de thread real não é portável entre ambientes). Simular o estado "já
    em andamento" preenchendo a mesma estrutura que a rota consulta prova
    exatamente a mesma lógica, de forma 100% determinística."""
    import extensions
    import routes.emails as emails_mod

    with app.app_context():
        acc = _seed_many(5)
        account_id = acc.id

        fake_client = BatchRecordingClient(all_uids=[1, 2, 3, 4, 5])
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)
        events = []
        monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: events.append((ev, data)))

    client = app.test_client()
    _login(client)
    body = {'action': 'delete', 'ids': [], 'folder': 'INBOX.Trash', 'account_id': account_id}

    key = (account_id, 'INBOX.Trash')
    emails_mod._bulk_whole_folder_inflight.add(key)   # simula "já em andamento"
    try:
        r = client.post('/api/emails/bulk', json=body, headers={'X-Correo-CSRF': 'tok'})
        assert r.status_code == 409
    finally:
        emails_mod._bulk_whole_folder_inflight.discard(key)

    # liberada a trava, a mesma ação volta a ser aceita normalmente
    r2 = client.post('/api/emails/bulk', json=body, headers={'X-Correo-CSRF': 'tok'})
    assert r2.status_code == 200
    _wait_for_bulk_done(events)   # espera terminar antes de sair do teste


def test_large_delete_is_split_into_multiple_imap_batches(monkeypatch):
    """~520 mensagens já na Lixeira: nenhum comando IMAP deve levar mais
    que BULK_IMAP_BATCH UIDs de uma vez, e todas devem ser removidas."""
    import extensions

    with app.app_context():
        acc = _seed_many(520)
        account_id = acc.id

        fake_client = BatchRecordingClient(all_uids=list(range(1, 521)))
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)
        events = []
        monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: events.append((ev, data)))

    client = app.test_client()
    _login(client)

    r = client.post('/api/emails/bulk', json={'action': 'delete', 'ids': [], 'folder': 'INBOX.Trash',
                                               'account_id': account_id},
                    headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200
    assert r.get_json() == {'ok': True, 'async': True}

    result = _wait_for_bulk_done(events)
    assert result['count'] == 520
    assert result['errors'] == []
    assert result['ok'] is True

    add_flags_calls = [c for c in fake_client.calls if c[0] == 'add_flags']
    uid_expunge_calls = [c for c in fake_client.calls if c[0] == 'uid_expunge']
    assert all(len(c[1]) <= BULK_IMAP_BATCH for c in add_flags_calls)
    assert all(len(c[1]) <= BULK_IMAP_BATCH for c in uid_expunge_calls)
    assert len(add_flags_calls) == 3  # 520 / 200 arredondado pra cima

    # Pedido do usuário: progresso visível na tela durante a ação, não só
    # um evento no início e um no fim — um lote concluído emite bulk_progress
    # com done/total crescendo até bater com o total da pasta.
    progress = [e[1] for e in events if e[0] == 'bulk_progress']
    assert [p['done'] for p in progress] == [200, 400, 520]
    assert all(p['total'] == 520 for p in progress)
    assert all(p['folder'] == 'INBOX.Trash' for p in progress)

    with app.app_context():
        assert Email.query.filter_by(account_id=account_id).count() == 0


def test_whole_folder_delete_refreshes_server_total_badge_immediately(monkeypatch):
    """Bug real reportado: depois de 'apagar todos', o badge/contador
    continuava mostrando o número de antes por até ~120s (até o próximo
    poll periódico recalcular FolderState.server_total/server_unread) —
    parecia que a exclusão não tinha feito nada, mesmo já tendo apagado
    tudo no servidor. bulk_action agora re-sincroniza a pasta antes de
    emitir bulk_done."""
    import extensions

    with app.app_context():
        acc = _seed_many(50)
        account_id = acc.id

        fake_client = BatchRecordingClient(all_uids=list(range(1, 51)))
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)
        events = []
        monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: events.append((ev, data)))

        # Badge mostrava isso antes da exclusão — tem que deixar de mostrar.
        db.session.add(FolderState(account_id=account_id, folder='INBOX.Trash',
                                   role='trash', server_total=50, server_unread=0))
        db.session.commit()

    client = app.test_client()
    _login(client)

    r = client.post('/api/emails/bulk', json={'action': 'delete', 'ids': [], 'folder': 'INBOX.Trash',
                                               'account_id': account_id},
                    headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200

    result = _wait_for_bulk_done(events)
    assert result['count'] == 50

    with app.app_context():
        fs = FolderState.query.filter_by(account_id=account_id, folder='INBOX.Trash').first()
        assert fs.server_total == 0   # não ficou parado em 50


def test_one_failed_batch_does_not_abort_the_others(monkeypatch):
    """Lote 2 falha (simulando o servidor recusando) — os outros 2 lotes
    ainda são processados e removidos; o erro do lote 2 é reportado, mas
    não trava a exclusão inteira."""
    import extensions

    with app.app_context():
        acc = _seed_many(520)
        account_id = acc.id

        # uid 201 cai no segundo lote (200-399) — força aquele lote a falhar
        fake_client = BatchRecordingClient(fail_on_uid=201, all_uids=list(range(1, 521)))
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)
        events = []
        monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: events.append((ev, data)))

    client = app.test_client()
    _login(client)

    r = client.post('/api/emails/bulk', json={'action': 'delete', 'ids': [], 'folder': 'INBOX.Trash',
                                               'account_id': account_id},
                    headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200

    result = _wait_for_bulk_done(events)
    assert result['count'] == 320        # 520 - 200 (lote que falhou)
    assert len(result['errors']) == 1

    with app.app_context():
        remaining = Email.query.filter_by(account_id=account_id).count()
        assert remaining == 200         # só o lote que falhou continua lá


def test_delete_all_uses_live_server_uids_not_just_synced_ones(monkeypatch):
    """O bug relatado: Lixeira com milhares de mensagens no servidor, mas
    só uma fração sincronizada localmente — 'apagar todos' tem que mandar
    apagar TUDO que o servidor reporta (c.search('ALL')), não só as que já
    existem na tabela local."""
    import extensions

    with app.app_context():
        acc = _seed_many(100)  # só 100 sincronizadas localmente
        account_id = acc.id

        # servidor tem 300 UIDs de verdade na pasta — 200 delas nunca
        # chegaram a ser sincronizadas localmente
        fake_client = BatchRecordingClient(all_uids=list(range(1, 301)))
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)
        events = []
        monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: events.append((ev, data)))

    client = app.test_client()
    _login(client)

    r = client.post('/api/emails/bulk', json={'action': 'delete', 'ids': [], 'folder': 'INBOX.Trash',
                                               'account_id': account_id},
                    headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200

    result = _wait_for_bulk_done(events)
    assert result['count'] == 300        # as 300 do servidor, não só as 100 locais
    assert result['errors'] == []

    add_flags_calls = [c for c in fake_client.calls if c[0] == 'add_flags']
    all_flagged_uids = {u for c in add_flags_calls for u in c[1]}
    assert all_flagged_uids == set(range(1, 301))

    with app.app_context():
        assert Email.query.filter_by(account_id=account_id).count() == 0


def test_explicit_selection_still_runs_synchronously(monkeypatch):
    """Seleção explícita (ids=[...], o que o usuário marcou na tela) não
    precisa virar background — é sempre pequena e o resultado já vem
    pronto na resposta HTTP, sem precisar esperar evento nenhum."""
    import extensions

    with app.app_context():
        acc = _seed_many(5)
        account_id = acc.id
        email_ids = [e.id for e in Email.query.filter_by(account_id=account_id).all()]

        fake_client = BatchRecordingClient(all_uids=list(range(1, 6)))
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)

    client = app.test_client()
    _login(client)

    r = client.post('/api/emails/bulk', json={'action': 'delete', 'ids': email_ids},
                    headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200
    data = r.get_json()
    assert data == {'ok': True, 'count': 5, 'errors': []}

    with app.app_context():
        assert Email.query.filter_by(account_id=account_id).count() == 0


def test_whole_folder_delete_outside_trash_junk_is_rejected_without_flag(monkeypatch):
    """Incidente real em produção: um clique acabou disparando 'apagar
    todos' contra '[Gmail]/Todos os e-mails' (All Mail) em vez da Lixeira —
    role desconhecido (nem folder_state, nem o detector por nome reconhece
    essa variante em português). Fora de Lixeira/Spam, a rota recusa sem
    o flag explícito confirm_dangerous."""
    import extensions

    with app.app_context():
        db.session.query(Email).delete()
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = Account(name='t', email='alldangerous@example.com', imap_host='h', smtp_host='s',
                     username='alldangerous@example.com', password_enc='x', capabilities='["UIDPLUS"]')
        db.session.add(acc)
        db.session.commit()
        account_id = acc.id
        db.session.add(Email(account_id=account_id, folder='[Gmail]/Todos os e-mails', uid=1,
                             subject='m1', from_addr='a@b.com', is_read=True))
        db.session.commit()

        fake_client = BatchRecordingClient(all_uids=[1])
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)
        events = []
        monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: events.append((ev, data)))

    client = app.test_client()
    _login(client)

    body = {'action': 'delete', 'ids': [], 'folder': '[Gmail]/Todos os e-mails', 'account_id': account_id}

    r_no_flag = client.post('/api/emails/bulk', json=body, headers={'X-Correo-CSRF': 'tok'})
    assert r_no_flag.status_code == 400
    assert r_no_flag.get_json()['ok'] is False

    with app.app_context():
        assert Email.query.filter_by(account_id=account_id).count() == 1   # nada foi tocado

    r_with_flag = client.post('/api/emails/bulk', json={**body, 'confirm_dangerous': True},
                              headers={'X-Correo-CSRF': 'tok'})
    assert r_with_flag.status_code == 200
    assert r_with_flag.get_json() == {'ok': True, 'async': True}

    result = _wait_for_bulk_done(events)
    assert result['count'] == 1


def test_whole_folder_delete_in_known_trash_role_needs_no_flag(monkeypatch):
    """Lixeira/Spam continuam sem essa fricção extra — só a confirmação
    normal do frontend já basta, igual sempre funcionou."""
    import extensions

    with app.app_context():
        db.session.query(Email).delete()
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = Account(name='t', email='safetrash@example.com', imap_host='h', smtp_host='s',
                     username='safetrash@example.com', password_enc='x', capabilities='["UIDPLUS"]')
        db.session.add(acc)
        db.session.commit()
        account_id = acc.id
        db.session.add(FolderState(account_id=account_id, folder='INBOX.Junk', role='junk'))
        db.session.add(Email(account_id=account_id, folder='INBOX.Junk', uid=1,
                             subject='spam', from_addr='a@b.com', is_read=True))
        db.session.commit()

        fake_client = BatchRecordingClient(all_uids=[1])
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)
        events = []
        monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: events.append((ev, data)))

    client = app.test_client()
    _login(client)

    body = {'action': 'delete', 'ids': [], 'folder': 'INBOX.Junk', 'account_id': account_id}
    r = client.post('/api/emails/bulk', json=body, headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200
    assert r.get_json() == {'ok': True, 'async': True}

    result = _wait_for_bulk_done(events)
    assert result['count'] == 1


def test_whole_folder_delete_purges_even_when_another_folder_also_has_trash_role(monkeypatch):
    """Mesmo bug do test_mail_service_actions.py, mas pelo caminho de
    'apagar todos' (routes/emails.py._process_bulk_groups): conta com DUAS
    pastas role=trash (Gmail: 'Trash' e '[Gmail]/Lixeira') — excluir de
    dentro de UMA delas não pode "mover" pra outra só porque
    role_folder('trash') escolheu a outra."""
    import extensions

    with app.app_context():
        db.session.query(Email).delete()
        db.session.query(FolderState).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = Account(name='t', email='doubletrash@example.com', imap_host='h', smtp_host='s',
                     username='doubletrash@example.com', password_enc='x', capabilities='["UIDPLUS","MOVE"]')
        db.session.add(acc)
        db.session.commit()
        account_id = acc.id
        # role_folder('trash') vai devolver 'Trash' (inserida primeiro) —
        # a exclusão abaixo é feita de dentro de '[Gmail]/Lixeira', a outra.
        db.session.add(FolderState(account_id=account_id, folder='Trash', role='trash'))
        db.session.add(FolderState(account_id=account_id, folder='[Gmail]/Lixeira', role='trash'))
        for uid in range(1, 11):
            db.session.add(Email(account_id=account_id, folder='[Gmail]/Lixeira', uid=uid,
                                 subject=f'm{uid}', from_addr='a@b.com', is_read=True))
        db.session.commit()

        fake_client = BatchRecordingClient(all_uids=list(range(1, 11)))
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        events = []
        monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: events.append((ev, data)))

    client = app.test_client()
    _login(client)

    body = {'action': 'delete', 'ids': [], 'folder': '[Gmail]/Lixeira', 'account_id': account_id}
    r = client.post('/api/emails/bulk', json=body, headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200

    result = _wait_for_bulk_done(events)
    assert result['count'] == 10
    assert result['errors'] == []

    uid_expunge_calls = [c for c in fake_client.calls if c[0] == 'uid_expunge']
    assert len(uid_expunge_calls) == 1   # exclusão definitiva, não "move" pra Trash

    with app.app_context():
        assert Email.query.filter_by(account_id=account_id).count() == 0
