"""
services/conf_manager.py
Leitura, escrita e sincronização de arquivos .conf de contas de e-mail.
Formato: chave=valor, um por linha. Linhas com # são comentários.
"""
import re
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CONF_KEY_ORDER = ['email', 'user', 'pass',
                  'imap_host', 'imap_port', 'imap_ssl',
                  'smtp_host', 'smtp_port', 'smtp_ssl', 'smtp_tls']


def safe_filename(name: str) -> str:
    return re.sub(r'[^\w\-]', '_', name).strip('_') or 'conta'


def read_conf(path: Path) -> dict:
    data = {}
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, _, v = line.partition('=')
                data[k.strip().lower()] = v.strip()
    except Exception as e:
        log.error(f'Erro ao ler {path}: {e}')
    return data


def write_conf(path: Path, data: dict) -> None:
    lines = [f'{k}={data[k]}' for k in CONF_KEY_ORDER if k in data]
    lines += [f'{k}={v}' for k, v in data.items() if k not in CONF_KEY_ORDER]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def account_to_conf_data(account) -> dict:
    return {
        'email':     account.email,
        'user':      account.username,
        'pass':      f'enc:{account.password_enc}',
        'imap_host': account.imap_host,
        'imap_port': str(account.imap_port),
        'imap_ssl':  'true' if account.imap_ssl else 'false',
        'smtp_host': account.smtp_host,
        'smtp_port': str(account.smtp_port),
        'smtp_ssl':  'true' if account.smtp_ssl else 'false',
        'smtp_tls':  'true' if account.smtp_tls else 'false',
    }


def sync_conf_to_db(accounts_dir: Path, db, Account, MAX_ACCOUNTS: int,
                    idle_manager=None) -> dict:
    """
    Lê todos os *.conf em accounts_dir e sincroniza com o banco.
    Retorna dict com listas: created, updated, deleted, errors.
    """
    result      = {'created': [], 'updated': [], 'deleted': [], 'errors': []}
    conf_emails = set()

    for conf_path in accounts_dir.glob('*.conf'):
        name = conf_path.stem
        data = read_conf(conf_path)
        email = data.get('email', '').strip()
        if not email:
            result['errors'].append(f'{conf_path.name}: campo "email" ausente')
            continue

        conf_emails.add(email.lower())
        username  = data.get('user', email)
        raw_pass  = data.get('pass', '')
        imap_host = data.get('imap_host', '')
        imap_port = int(data.get('imap_port', 993))
        imap_ssl  = data.get('imap_ssl', 'true').lower() in ('true', '1', 'yes')
        smtp_host = data.get('smtp_host', '')
        smtp_port = int(data.get('smtp_port', 587))
        smtp_ssl  = data.get('smtp_ssl', 'false').lower() in ('true', '1', 'yes')
        smtp_tls  = data.get('smtp_tls', 'true').lower() in ('true', '1', 'yes')

        if not imap_host or not smtp_host:
            result['errors'].append(f'{conf_path.name}: imap_host ou smtp_host ausente')
            continue

        account = Account.query.filter_by(email=email).first()
        if account:
            account.name      = name
            account.username  = username
            account.imap_host = imap_host; account.imap_port = imap_port; account.imap_ssl = imap_ssl
            account.smtp_host = smtp_host; account.smtp_port = smtp_port
            account.smtp_ssl  = smtp_ssl;  account.smtp_tls  = smtp_tls
            account.conf_file = conf_path.name
            if raw_pass and not raw_pass.startswith('enc:'):
                account.set_password(raw_pass)
            db.session.commit()
            result['updated'].append(email)
            if idle_manager:
                idle_manager.restart_account(account.id)
        else:
            if Account.query.count() >= MAX_ACCOUNTS:
                result['errors'].append(f'{conf_path.name}: limite de {MAX_ACCOUNTS} contas atingido')
                continue
            if not raw_pass:
                result['errors'].append(f'{conf_path.name}: campo "pass" ausente para nova conta')
                continue
            account = Account(
                name=name, email=email, username=username,
                imap_host=imap_host, imap_port=imap_port, imap_ssl=imap_ssl,
                smtp_host=smtp_host, smtp_port=smtp_port,
                smtp_ssl=smtp_ssl, smtp_tls=smtp_tls, conf_file=conf_path.name,
            )
            account.set_password(raw_pass)
            db.session.add(account)
            db.session.commit()
            result['created'].append(email)
            if idle_manager:
                idle_manager.start_account(account.id)

        # Encripta a senha no .conf se estava em plain text
        if raw_pass and not raw_pass.startswith('enc:'):
            data['pass'] = f'enc:{account.password_enc}'
            write_conf(conf_path, data)

    # Remove do banco contas cujo .conf foi deletado
    for account in Account.query.all():
        if account.conf_file and account.email.lower() not in conf_emails:
            if not (accounts_dir / account.conf_file).exists():
                if idle_manager:
                    idle_manager.stop_account(account.id)
                db.session.delete(account)
                db.session.commit()
                result['deleted'].append(account.email)

    return result
