#!/usr/bin/env python3
"""
scripts/rotate_key.py
Força a recriptografia de todas as contas (banco + arquivos .conf) da chave
Fernet legada (mailapp-fernet-key-v1-stable-2024) para a chave definida em
CORREO_FERNET_KEY. Idempotente — pode rodar várias vezes sem efeito em
contas já migradas (database.Account.get_password() já migra sozinho no
primeiro uso; este script força isso de uma vez para todas as contas).

Uso (dentro do container, com as variáveis de ambiente de produção carregadas):
    CORREO_SKIP_IDLE=1 python scripts/rotate_key.py
"""
import os
os.environ.setdefault('CORREO_SKIP_IDLE', '1')

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app
from database import Account
from services.conf_manager import write_conf, account_to_conf_data


def main() -> None:
    migrated, already_ok, errors = 0, 0, 0
    with app.app_context():
        accounts_dir = Path(app.config['ACCOUNTS_DIR'])
        for account in Account.query.all():
            enc_before = account.password_enc
            try:
                account.get_password()  # migra e salva sozinho se estava na chave legada
            except Exception as e:
                print(f'  [ERRO] {account.email}: {e}')
                errors += 1
                continue
            if account.password_enc != enc_before:
                migrated += 1
                if account.conf_file:
                    conf_path = accounts_dir / account.conf_file
                    if conf_path.exists():
                        write_conf(conf_path, account_to_conf_data(account))
                print(f'  [OK] {account.email}: recriptografada com a chave nova')
            else:
                already_ok += 1

    print(f'\nResumo: {migrated} migrada(s), {already_ok} já na chave nova, {errors} erro(s).')


if __name__ == '__main__':
    main()
