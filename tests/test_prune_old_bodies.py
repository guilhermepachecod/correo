"""
Testa prune_old_bodies (Fase 2.7) — substitui enforce_email_limit, que
apagava linhas (bug B2: causava re-download infinito). Poda só o corpo,
nunca a linha.
"""
from datetime import datetime, timedelta

from app import app
from database import db, Account, Email
import services.mail_service as mail_service
from services.mail_service import prune_old_bodies


def _make_account(email):
    acc = Account(name='t', email=email, imap_host='h', smtp_host='s',
                  username=email, password_enc='x')
    db.session.add(acc)
    db.session.commit()
    return acc


def _make_email(account_id, days_old=0, uid=None):
    e = Email(account_id=account_id, folder='INBOX', uid=uid, subject='x',
              from_addr='a@b.com', body_text='corpo', body_html='<p>corpo</p>',
              date=datetime.utcnow() - timedelta(days=days_old), body_cached=True)
    db.session.add(e)
    db.session.commit()
    return e


def test_prune_never_deletes_rows_only_clears_body(monkeypatch):
    with app.app_context():
        monkeypatch.setattr(mail_service, 'MAX_BODIES_PER_ACCOUNT', 2)
        monkeypatch.setattr(mail_service, 'BODY_RETENTION_DAYS', 9999)

        db.session.query(Email).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('prune1@example.com')

        e1 = _make_email(acc.id, days_old=5, uid=1)
        e2 = _make_email(acc.id, days_old=3, uid=2)
        e3 = _make_email(acc.id, days_old=1, uid=3)

        pruned = prune_old_bodies(acc.id)

        assert pruned == 1  # só 1 mensagem além do limite de 2
        assert Email.query.filter_by(account_id=acc.id).count() == 3  # nenhuma linha removida

        e1 = Email.query.get(e1.id)
        assert e1.body_cached is False
        assert e1.body_text == ''
        assert e1.body_html == ''

        e3 = Email.query.get(e3.id)
        assert e3.body_cached is True
        assert e3.body_text == 'corpo'


def test_prune_also_catches_old_messages_beyond_retention(monkeypatch):
    with app.app_context():
        monkeypatch.setattr(mail_service, 'MAX_BODIES_PER_ACCOUNT', 1000)
        monkeypatch.setattr(mail_service, 'BODY_RETENTION_DAYS', 30)

        db.session.query(Email).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('prune2@example.com')

        old = _make_email(acc.id, days_old=90, uid=1)
        recent = _make_email(acc.id, days_old=1, uid=2)

        pruned = prune_old_bodies(acc.id)

        assert pruned == 1
        assert Email.query.filter_by(account_id=acc.id).count() == 2
        assert Email.query.get(old.id).body_cached is False
        assert Email.query.get(recent.id).body_cached is True


def test_prune_is_noop_when_under_limit_and_retention(monkeypatch):
    with app.app_context():
        monkeypatch.setattr(mail_service, 'MAX_BODIES_PER_ACCOUNT', 1000)
        monkeypatch.setattr(mail_service, 'BODY_RETENTION_DAYS', 180)

        db.session.query(Email).delete()
        db.session.query(Account).delete()
        db.session.commit()
        acc = _make_account('prune3@example.com')
        _make_email(acc.id, days_old=1, uid=1)

        assert prune_old_bodies(acc.id) == 0
