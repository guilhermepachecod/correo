"""
Testa GET /api/emails?view=... e GET /api/views/counts (Fase 5.1 — visões
unificadas no frontend, backend já existia desde a Fase 2.9).
"""
from app import app
from database import db, Account, Email, FolderState


def _seed():
    db.session.query(Email).delete()
    db.session.query(FolderState).delete()
    db.session.query(Account).delete()
    db.session.commit()

    acc1 = Account(name='a1', email='views1@example.com', imap_host='h', smtp_host='s',
                   username='views1@example.com', password_enc='x')
    acc2 = Account(name='a2', email='views2@example.com', imap_host='h', smtp_host='s',
                   username='views2@example.com', password_enc='x')
    db.session.add_all([acc1, acc2])
    db.session.commit()

    db.session.add_all([
        FolderState(account_id=acc1.id, folder='INBOX', role='inbox'),
        FolderState(account_id=acc1.id, folder='INBOX.Trash', role='trash'),
        FolderState(account_id=acc2.id, folder='INBOX', role='inbox'),
    ])
    db.session.commit()

    emails = [
        Email(account_id=acc1.id, folder='INBOX', uid=1, subject='a1 unread',
              from_addr='x@y.com', is_read=False),
        Email(account_id=acc1.id, folder='INBOX', uid=2, subject='a1 read starred',
              from_addr='x@y.com', is_read=True, is_starred=True),
        Email(account_id=acc1.id, folder='INBOX.Trash', uid=3, subject='a1 trashed',
              from_addr='x@y.com', is_read=True),
        Email(account_id=acc2.id, folder='INBOX', uid=1, subject='a2 unread',
              from_addr='x@y.com', is_read=False),
    ]
    db.session.add_all(emails)
    db.session.commit()
    return acc1, acc2


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def test_view_unread_aggregates_all_accounts():
    with app.app_context():
        _seed()
        client = app.test_client()
        _login(client)

        r = client.get('/api/emails?view=unread')
        assert r.status_code == 200
        subjects = {e['subject'] for e in r.get_json()['emails']}
        assert subjects == {'a1 unread', 'a2 unread'}


def test_view_starred_ignores_role_crosses_folders():
    with app.app_context():
        _seed()
        client = app.test_client()
        _login(client)

        r = client.get('/api/emails?view=starred')
        subjects = {e['subject'] for e in r.get_json()['emails']}
        assert subjects == {'a1 read starred'}


def test_view_all_is_inbox_role_only_not_trash():
    with app.app_context():
        _seed()
        client = app.test_client()
        _login(client)

        r = client.get('/api/emails?view=all')
        subjects = {e['subject'] for e in r.get_json()['emails']}
        assert subjects == {'a1 unread', 'a1 read starred', 'a2 unread'}


def test_view_scoped_to_single_account_when_account_id_given():
    with app.app_context():
        acc1, _acc2 = _seed()
        client = app.test_client()
        _login(client)

        r = client.get(f'/api/emails?view=unread&account_id={acc1.id}')
        subjects = {e['subject'] for e in r.get_json()['emails']}
        assert subjects == {'a1 unread'}


def test_invalid_view_returns_400():
    with app.app_context():
        _seed()
        client = app.test_client()
        _login(client)

        r = client.get('/api/emails?view=nope')
        assert r.status_code == 400


def test_view_counts_endpoint():
    with app.app_context():
        _seed()
        client = app.test_client()
        _login(client)

        r = client.get('/api/views/counts')
        assert r.status_code == 200
        data = r.get_json()
        assert data['all']['total'] == 3       # a1 unread, a1 read starred, a2 unread (não o da lixeira)
        assert data['unread']['total'] == 2
        assert data['starred']['total'] == 1
