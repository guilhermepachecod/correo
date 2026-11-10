"""
Testa o lote de UIDs em bulk_action (routes/emails.py) — o bug real
reportado: 'excluir todos' numa Lixeira com ~3000 mensagens não fazia
nada. Suspeita confirmada por inspeção: um único comando IMAP com
milhares de UIDs é frágil (linha de comando longa demais pro servidor) —
agora todo comando sai em lotes de BULK_IMAP_BATCH UIDs, e um lote que
falha não aborta os outros.
"""
from app import app
from database import db, Account, Email
from routes.emails import BULK_IMAP_BATCH


class BatchRecordingClient:
    def __init__(self, fail_on_uid=None):
        self.calls = []
        self.fail_on_uid = fail_on_uid

    def select_folder(self, folder, readonly=False):
        self.calls.append(('select_folder', folder))
        return {}

    def add_flags(self, uids, flags):
        if self.fail_on_uid and self.fail_on_uid in uids:
            raise RuntimeError('servidor recusou o comando (lote grande demais)')
        self.calls.append(('add_flags', tuple(uids), tuple(flags)))

    def uid_expunge(self, uids):
        self.calls.append(('uid_expunge', tuple(uids)))

    def expunge(self):
        raise AssertionError('expunge() genérico chamado — regressão do bug B7')


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def _seed_many(n=520):
    db.session.query(Email).delete()
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


def test_large_delete_is_split_into_multiple_imap_batches(monkeypatch):
    """~520 mensagens já na Lixeira: nenhum comando IMAP deve levar mais
    que BULK_IMAP_BATCH UIDs de uma vez, e todas devem ser removidas."""
    import extensions

    with app.app_context():
        acc = _seed_many(520)
        account_id = acc.id

        fake_client = BatchRecordingClient()
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)

    client = app.test_client()
    _login(client)

    r = client.post('/api/emails/bulk', json={'action': 'delete', 'ids': [], 'folder': 'INBOX.Trash',
                                               'account_id': account_id},
                    headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200
    data = r.get_json()
    assert data['count'] == 520
    assert data['errors'] == []

    add_flags_calls = [c for c in fake_client.calls if c[0] == 'add_flags']
    uid_expunge_calls = [c for c in fake_client.calls if c[0] == 'uid_expunge']
    assert all(len(c[1]) <= BULK_IMAP_BATCH for c in add_flags_calls)
    assert all(len(c[1]) <= BULK_IMAP_BATCH for c in uid_expunge_calls)
    assert len(add_flags_calls) == 3  # 520 / 200 arredondado pra cima

    with app.app_context():
        assert Email.query.filter_by(account_id=account_id).count() == 0


def test_one_failed_batch_does_not_abort_the_others(monkeypatch):
    """Lote 2 falha (simulando o servidor recusando) — os outros 2 lotes
    ainda são processados e removidos; o erro do lote 2 é reportado, mas
    não trava a exclusão inteira."""
    import extensions

    with app.app_context():
        acc = _seed_many(520)
        account_id = acc.id

        # uid 201 cai no segundo lote (200-399) — força aquele lote a falhar
        fake_client = BatchRecordingClient(fail_on_uid=201)
        monkeypatch.setattr(extensions.mail_service, '_safe_exec', lambda account, fn: fn(fake_client))
        monkeypatch.setattr(extensions.mail_service, 'role_folder', lambda account, role: None)

    client = app.test_client()
    _login(client)

    r = client.post('/api/emails/bulk', json={'action': 'delete', 'ids': [], 'folder': 'INBOX.Trash',
                                               'account_id': account_id},
                    headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200
    data = r.get_json()
    assert data['count'] == 320        # 520 - 200 (lote que falhou)
    assert len(data['errors']) == 1

    with app.app_context():
        remaining = Email.query.filter_by(account_id=account_id).count()
        assert remaining == 200         # só o lote que falhou continua lá
