"""
Testa o campo `total` de GET /api/emails — pedido real do usuário: o número
mostrado na lista (e usado pra "Mostrando 50 de X e-mails") tem que refletir
o total real do servidor (FolderState.server_total/server_unread, grava do
por sync_folder_incremental), não só quantas linhas já sincronizamos local-
mente. Cai pra contagem local (pag.total) quando ainda não sincronizou essa
contagem, ou quando é busca por texto (o servidor não entra nessa conta).
"""
from app import app
from database import db, Account, Email, FolderState


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def _seed():
    db.session.query(Email).delete()
    db.session.query(FolderState).delete()
    db.session.query(Account).delete()
    db.session.commit()
    acc = Account(name='t', email='total@example.com', imap_host='h', smtp_host='s',
                  username='total@example.com', password_enc='x')
    db.session.add(acc)
    db.session.commit()
    for uid in range(1, 4):   # só 3 sincronizadas localmente
        db.session.add(Email(account_id=acc.id, folder='INBOX', uid=uid, subject=f'm{uid}',
                             from_addr='a@b.com', is_read=(uid != 1), body_text=f'corpo {uid}'))
    db.session.commit()
    return acc


def test_folder_total_uses_server_count_when_available():
    with app.app_context():
        acc = _seed()
        fs = FolderState(account_id=acc.id, folder='INBOX', role='inbox', server_total=1000)
        db.session.add(fs)
        db.session.commit()

        client = app.test_client()
        _login(client)
        r = client.get(f'/api/emails?folder=INBOX&account_id={acc.id}')
        data = r.get_json()

        assert len(data['emails']) == 3   # só o que tem local pra listar
        assert data['total'] == 1000      # mas o total mostrado é o do servidor


def test_folder_total_falls_back_to_local_count_without_folder_state():
    with app.app_context():
        acc = _seed()
        client = app.test_client()
        _login(client)
        r = client.get(f'/api/emails?folder=INBOX&account_id={acc.id}')
        data = r.get_json()

        assert data['total'] == 3   # nunca sincronizou server_total — cai pro local


def test_search_never_uses_server_total():
    """Busca por texto é sempre sobre o que já foi sincronizado localmente —
    mesmo com server_total gravado, o total mostrado tem que ser o de
    resultados da busca, não o da pasta inteira."""
    with app.app_context():
        acc = _seed()
        fs = FolderState(account_id=acc.id, folder='INBOX', role='inbox', server_total=1000)
        db.session.add(fs)
        db.session.commit()

        client = app.test_client()
        _login(client)
        r = client.get(f'/api/emails?folder=INBOX&account_id={acc.id}&search=corpo+2')
        data = r.get_json()

        assert data['total'] == 1   # só a mensagem 2 bate a busca


def test_unread_view_sums_server_unread_across_inbox_role_folders():
    with app.app_context():
        acc = _seed()
        fs = FolderState(account_id=acc.id, folder='INBOX', role='inbox',
                         server_total=1000, server_unread=37)
        db.session.add(fs)
        db.session.commit()

        client = app.test_client()
        _login(client)
        r = client.get(f'/api/emails?view=unread&account_id={acc.id}')
        data = r.get_json()

        assert data['total'] == 37   # unread usa server_unread, não server_total


def test_starred_view_never_uses_server_total():
    """Estrela é cross-folder — sem total de pasta único no servidor pra
    isso, então sempre cai pro total local."""
    with app.app_context():
        acc = _seed()
        fs = FolderState(account_id=acc.id, folder='INBOX', role='inbox', server_total=1000)
        db.session.add(fs)
        email = Email.query.filter_by(account_id=acc.id, uid=1).first()
        email.is_starred = True
        db.session.commit()

        client = app.test_client()
        _login(client)
        r = client.get(f'/api/emails?view=starred&account_id={acc.id}')
        data = r.get_json()

        assert data['total'] == 1
