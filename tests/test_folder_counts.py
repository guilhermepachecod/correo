"""
Testa GET /api/folders/counts — nunca toca o IMAP, e toda pasta conhecida
via folder_state aparece mesmo com 0 e-mails em cache. Antes disso, uma
pasta como a Lixeira "desaparecia" da sidebar sempre que o cache local
esvaziava e reaparecia no próximo sync — era isso que piscava.
"""
from app import app
from database import db, Account, Email, FolderState


def _seed():
    db.session.query(Email).delete()
    db.session.query(FolderState).delete()
    db.session.query(Account).delete()
    db.session.commit()

    acc = Account(name='t', email='counts@example.com', imap_host='h', smtp_host='s',
                  username='counts@example.com', password_enc='x')
    db.session.add(acc)
    db.session.commit()

    db.session.add_all([
        FolderState(account_id=acc.id, folder='INBOX', role='inbox'),
        FolderState(account_id=acc.id, folder='INBOX.Trash', role='trash'),  # vazia
    ])
    db.session.add(Email(account_id=acc.id, folder='INBOX', uid=1, subject='x',
                         from_addr='a@b.com', is_read=False))
    db.session.commit()
    return acc


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def test_empty_known_folder_still_appears_with_zero_count():
    with app.app_context():
        acc = _seed()
        client = app.test_client()
        _login(client)

        r = client.get(f'/api/folders/counts?account_id={acc.id}')
        data = r.get_json()

        assert 'INBOX.Trash' in data
        assert data['INBOX.Trash'] == {'total': 0, 'unread': 0}
        assert data['INBOX'] == {'total': 1, 'unread': 1}


def test_unknown_folder_without_folder_state_is_not_invented():
    with app.app_context():
        acc = _seed()
        client = app.test_client()
        _login(client)

        r = client.get(f'/api/folders/counts?account_id={acc.id}')
        data = r.get_json()

        assert 'INBOX.Spam' not in data  # nunca existiu em folder_state nem em emails
