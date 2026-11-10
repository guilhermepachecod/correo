from services.special_folders import detect_role


def test_special_use_flag_wins_over_name():
    assert detect_role('Qualquer Nome', [b'\\HasNoChildren', b'\\Trash']) == 'trash'
    assert detect_role('Qualquer Nome', (r'\Sent',)) == 'sent'
    assert detect_role('x', (r'\Junk',)) == 'junk'
    assert detect_role('x', (r'\Drafts',)) == 'drafts'
    assert detect_role('x', (r'\Archive',)) == 'archive'


def test_fallback_by_name_dovecot_style():
    assert detect_role('INBOX') == 'inbox'
    assert detect_role('INBOX.Trash') == 'trash'
    assert detect_role('Lixeira') == 'trash'
    assert detect_role('INBOX.Sent') == 'sent'
    assert detect_role('Enviados') == 'sent'
    assert detect_role('E-mails Enviados') == 'sent'
    assert detect_role('INBOX.Spam') == 'junk'
    assert detect_role('Rascunhos') == 'drafts'
    assert detect_role('Archive') == 'archive'


def test_fallback_by_name_gmail_style():
    assert detect_role('[Gmail]/Lixeira') == 'trash'
    assert detect_role('[Gmail]/Trash') == 'trash'
    assert detect_role('[Gmail]/Sent Mail') == 'sent'
    assert detect_role('[Gmail]/Spam') == 'junk'
    assert detect_role('[Gmail]/All Mail') == 'archive'


def test_unknown_folder_returns_none():
    assert detect_role('Projetos') is None
    assert detect_role('Clientes.2024') is None


def test_no_flags_and_no_name_match_is_none():
    assert detect_role('x', ()) is None
    assert detect_role('', ()) is None
