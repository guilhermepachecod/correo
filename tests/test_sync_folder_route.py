"""
Testa POST /api/accounts/<id>/folders/<folder>/sync (Fase 2.5 pendente:
abrir uma pasta na UI dispara sync incremental dela, sem esperar o IDLE
— que só cobre a INBOX — nem o poll de 120s).
"""
import time

from app import app
from database import db, Account


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def test_sync_folder_runs_in_background_and_emits_events(monkeypatch):
    import extensions
    import services.mail_service as mail_service_module

    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = Account(name='t', email='syncfolder@example.com', imap_host='h', smtp_host='s',
                      username='syncfolder@example.com', password_enc='x')
        db.session.add(acc)
        db.session.commit()
        account_id = acc.id

    events = []
    monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: events.append((ev, data)))
    monkeypatch.setattr(extensions.mail_service, 'sync_folder_incremental',
                        lambda account, folder: {'ok': True, 'new': 2, 'flag_changes': 1, 'removed': 0})

    client = app.test_client()
    _login(client)

    r = client.post(f'/api/accounts/{account_id}/folders/INBOX.Trash/sync', headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200
    assert r.get_json() == {'ok': True}

    for _ in range(20):
        if any(e[0] == 'sync_done' for e in events):
            break
        time.sleep(0.05)

    assert ('sync_start', {'account_id': account_id, 'email': 'syncfolder@example.com'}) in events
    done = next(e for e in events if e[0] == 'sync_done')
    assert done[1]['new'] == 2
    assert done[1]['flag_changes'] == 1


def test_sync_folder_emits_error_on_failure(monkeypatch):
    import extensions

    with app.app_context():
        db.session.query(Account).delete()
        db.session.commit()
        acc = Account(name='t', email='syncfolder2@example.com', imap_host='h', smtp_host='s',
                      username='syncfolder2@example.com', password_enc='x')
        db.session.add(acc)
        db.session.commit()
        account_id = acc.id

    events = []
    monkeypatch.setattr(extensions.mail_service, '_emit', lambda ev, data: events.append((ev, data)))
    monkeypatch.setattr(extensions.mail_service, 'sync_folder_incremental',
                        lambda account, folder: {'ok': False, 'error': 'conexão recusada'})

    client = app.test_client()
    _login(client)

    r = client.post(f'/api/accounts/{account_id}/folders/INBOX/sync', headers={'X-Correo-CSRF': 'tok'})
    assert r.status_code == 200

    for _ in range(20):
        if any(e[0] == 'sync_error' for e in events):
            break
        time.sleep(0.05)

    assert any(e[0] == 'sync_error' for e in events)
