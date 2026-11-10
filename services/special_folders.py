"""
services/special_folders.py
Detecção do "papel" de uma pasta IMAP (inbox/sent/trash/junk/drafts/archive),
usada pelas visões unificadas (Fase 5) e pelas ações servidor-primeiro
(Fase 2.6 — excluir = mover para a pasta role=trash etc.).

Função pura, sem IMAP: recebe o que `imapclient.IMAPClient.list_folders()`
já devolve (lista de `(flags, delimiter, name)`), sem abrir conexão nenhuma
— por isso é testável sem servidor real (ver tests/test_special_folders.py).
"""
import re

# Atributos SPECIAL-USE (RFC 6154). Vêm como bytes ou str dependendo da lib/
# servidor — normalizamos tudo para str antes de comparar.
_SPECIAL_USE_ROLE = {
    r'\trash':   'trash',
    r'\sent':    'sent',
    r'\junk':    'junk',
    r'\drafts':  'drafts',
    r'\archive': 'archive',
}

# Fallback por nome quando o servidor não anuncia SPECIAL-USE (Dovecot sem
# a extensão habilitada, por exemplo). Comparação case-insensitive.
_NAME_ROLE_PATTERNS = [
    (re.compile(r'^\[gmail\]/lixeira$', re.I), 'trash'),
    (re.compile(r'^\[gmail\]/trash$', re.I), 'trash'),
    (re.compile(r'^\[gmail\]/sent mail$', re.I), 'sent'),
    (re.compile(r'^\[gmail\]/spam$', re.I), 'junk'),
    (re.compile(r'^\[gmail\]/all mail$', re.I), 'archive'),
    (re.compile(r'^(inbox[./])?(trash|lixeira|deleted( items)?)$', re.I), 'trash'),
    (re.compile(r'^(inbox[./])?(sent( items| mail)?|enviados|e-mails enviados)$', re.I), 'sent'),
    (re.compile(r'^(inbox[./])?(spam|junk)$', re.I), 'junk'),
    (re.compile(r'^(inbox[./])?(drafts|rascunhos)$', re.I), 'drafts'),
    (re.compile(r'^(inbox[./])?(archive|arquivo)$', re.I), 'archive'),
    (re.compile(r'^inbox$', re.I), 'inbox'),
]


def _normalize_flag(flag) -> str:
    if isinstance(flag, bytes):
        flag = flag.decode('ascii', errors='ignore')
    return flag.lower()


def detect_role(name: str, flags=()) -> str | None:
    """Retorna o role ('inbox'|'sent'|'trash'|'junk'|'drafts'|'archive') ou
    None se não for possível determinar. SPECIAL-USE tem prioridade sobre o
    nome, já que é o jeito correto (RFC 6154) e funciona mesmo com pastas
    renomeadas/traduzidas pelo usuário."""
    for flag in flags or ():
        role = _SPECIAL_USE_ROLE.get(_normalize_flag(flag))
        if role:
            return role

    if name:
        for pattern, role in _NAME_ROLE_PATTERNS:
            if pattern.match(name.strip()):
                return role

    return None
