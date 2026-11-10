"""
routes/contacts.py
Autocomplete de destinatários (Fase 5.2) — deriva contatos dos endereços já
vistos em from/to/cc dos e-mails, sem tabela própria. Só lê o banco.
"""
from email.utils import getaddresses

from flask import Blueprint, request, jsonify

from database import db, Account, Email

bp = Blueprint('contacts', __name__)

SCAN_LIMIT = 1000  # mensagens recentes examinadas, não total de contatos devolvidos


def _collect_addresses(limit: int = SCAN_LIMIT):
    rows = (db.session.query(Email.from_addr, Email.from_name, Email.to_addr, Email.cc_addr)
                      .order_by(Email.date.desc()).limit(limit).all())
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for from_addr, from_name, to_addr, cc_addr in rows:
        pairs = []
        if from_addr:
            pairs.append((from_name or '', from_addr))
        if to_addr:
            pairs += getaddresses([to_addr])
        if cc_addr:
            pairs += getaddresses([cc_addr])
        for name, addr in pairs:
            addr = (addr or '').strip().lower()
            if not addr or '@' not in addr:
                continue
            counts[addr] = counts.get(addr, 0) + 1
            if name and not names.get(addr):
                names[addr] = name
    return counts, names


@bp.get('/api/contacts')
def contacts():
    query = request.args.get('q', '').strip().lower()
    counts, names = _collect_addresses()
    own_emails = {a.email.lower() for a in Account.query.all()}

    result = []
    for addr, count in counts.items():
        if addr in own_emails:
            continue  # não sugere o próprio endereço das contas cadastradas
        if query and query not in addr and query not in names.get(addr, '').lower():
            continue
        result.append({'email': addr, 'name': names.get(addr, ''), 'count': count})

    result.sort(key=lambda r: -r['count'])
    return jsonify(result[:20])
