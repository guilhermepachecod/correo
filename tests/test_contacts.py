"""
Testa GET /api/contacts (Fase 5.2 — autocomplete de destinatários no
compose). Deriva contatos de from/to/cc já vistos, sem tabela própria.
"""
from app import app
from database import db, Account, Email


def _seed():
    db.session.query(Email).delete()
    db.session.query(Account).delete()
    db.session.commit()

    acc = Account(name='t', email='contacts@example.com', imap_host='h', smtp_host='s',
                  username='contacts@example.com', password_enc='x')
    db.session.add(acc)
    db.session.commit()

    db.session.add_all([
        Email(account_id=acc.id, folder='INBOX', uid=1, subject='a',
              from_addr='maria@example.com', from_name='Maria Silva',
              to_addr='contacts@example.com'),
        Email(account_id=acc.id, folder='INBOX', uid=2, subject='b',
              from_addr='maria@example.com', from_name='Maria Silva',
              to_addr='contacts@example.com, joao@example.com'),
        Email(account_id=acc.id, folder='Sent', uid=3, subject='c',
              from_addr='contacts@example.com',
              to_addr='joao@example.com', cc_addr='pedro@example.com'),
    ])
    db.session.commit()


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def test_contacts_ranked_by_frequency():
    with app.app_context():
        _seed()
        client = app.test_client()
        _login(client)

        r = client.get('/api/contacts')
        assert r.status_code == 200
        emails = [c['email'] for c in r.get_json()]
        # maria aparece 2x (from), joao 2x (to em duas mensagens), devem vir antes de pedro (1x)
        assert emails[0] in ('maria@example.com', 'joao@example.com')
        assert 'pedro@example.com' in emails


def test_contacts_excludes_own_configured_accounts():
    """O próprio e-mail da conta (contacts@example.com) nunca é sugerido —
    não faz sentido autocompletar o remetente como destinatário dele mesmo."""
    with app.app_context():
        _seed()
        client = app.test_client()
        _login(client)

        r = client.get('/api/contacts')
        emails = [c['email'] for c in r.get_json()]
        assert 'contacts@example.com' not in emails


def test_contacts_filtered_by_query():
    with app.app_context():
        _seed()
        client = app.test_client()
        _login(client)

        r = client.get('/api/contacts?q=mar')
        data = r.get_json()
        assert all('mar' in c['email'] or 'mar' in c['name'].lower() for c in data)
        assert any(c['email'] == 'maria@example.com' for c in data)


def test_contacts_includes_display_name():
    with app.app_context():
        _seed()
        client = app.test_client()
        _login(client)

        r = client.get('/api/contacts?q=maria')
        data = r.get_json()
        maria = next(c for c in data if c['email'] == 'maria@example.com')
        assert maria['name'] == 'Maria Silva'
