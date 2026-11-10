"""
Testa ação em lote (POST /api/emails/bulk) com mensagens de contas
diferentes no mesmo lote — o cenário real quando o usuário seleciona
várias mensagens numa visão unificada (Fase 5.1 ressalva: "não tinha teste
dedicado pra isso", mas a lógica de agrupar por conta/pasta já existia
desde a Fase 2.6).
"""
from app import app
from database import db, Account, Email


class RecordingClient:
    def __init__(self):
        self.calls = []

    def select_folder(self, folder, readonly=False):
        self.calls.append(('select_folder', folder))
        return {}

    def add_flags(self, uids, flags):
        self.calls.append(('add_flags', tuple(uids), tuple(flags)))


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def test_bulk_mark_read_groups_by_account_and_folder(monkeypatch):
    import extensions

    with app.app_context():
        db.session.query(Email).delete()
        db.session.query(Account).delete()
        db.session.commit()

        acc1 = Account(name='a1', email='bulk1@example.com', imap_host='h', smtp_host='s',
                       username='bulk1@example.com', password_enc='x')
        acc2 = Account(name='a2', email='bulk2@example.com', imap_host='h', smtp_host='s',
                       username='bulk2@example.com', password_enc='x')
        db.session.add_all([acc1, acc2])
        db.session.commit()

        e1 = Email(account_id=acc1.id, folder='INBOX', uid=1, subject='x',
                   from_addr='a@b.com', is_read=False)
        e2 = Email(account_id=acc2.id, folder='INBOX', uid=1, subject='y',
                   from_addr='a@b.com', is_read=False)
        db.session.add_all([e1, e2])
        db.session.commit()
        ids = [e1.id, e2.id]

        clients_by_account = {}

        def fake_safe_exec(account, fn):
            client = clients_by_account.setdefault(account.id, RecordingClient())
            return fn(client)

        monkeypatch.setattr(extensions.mail_service, '_safe_exec', fake_safe_exec)
        monkeypatch.setattr(extensions.mail_service, '_capabilities', lambda account: set())

    client = app.test_client()
    _login(client)

    # Lote com e-mails de DUAS contas diferentes, como a visão "Não lidos" faria
    r = client.post('/api/emails/bulk', json={'action': 'mark_read', 'ids': ids},
                    headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200
    data = r.get_json()
    assert data['count'] == 2

    with app.app_context():
        assert Email.query.get(e1.id).is_read is True
        assert Email.query.get(e2.id).is_read is True

    # cada conta recebeu seu próprio comando de flags — nunca misturado
    assert len(clients_by_account) == 2
    for acc_id, fake_client in clients_by_account.items():
        assert ('add_flags', (1,), ('\\Seen',)) in fake_client.calls
