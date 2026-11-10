#!/usr/bin/env python3
"""
scripts/import_conf.py
Mesma função usada pelo botão "Ler arquivos .conf" da UI (POST
/api/accounts/sync-conf), como comando — útil para automatizar o
provisionamento de contas sem passar pela interface.

Uso (dentro do container, ou localmente com as variáveis de ambiente corretas):
    CORREO_SKIP_IDLE=1 python scripts/import_conf.py
"""
import os
os.environ.setdefault('CORREO_SKIP_IDLE', '1')

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import app
from database import db, Account, MAX_ACCOUNTS
from services.conf_manager import sync_conf_to_db


def main() -> None:
    with app.app_context():
        result = sync_conf_to_db(
            accounts_dir=Path(app.config['ACCOUNTS_DIR']),
            db=db,
            Account=Account,
            MAX_ACCOUNTS=MAX_ACCOUNTS,
            idle_manager=None,  # script roda fora do processo da app; sem IDLE manager pra (re)iniciar
        )

    for key in ('created', 'updated', 'deleted'):
        for email in result[key]:
            print(f'  [{key.upper()}] {email}')
    for err in result['errors']:
        print(f'  [ERRO] {err}')

    print(
        f"\nResumo: {len(result['created'])} criada(s), {len(result['updated'])} "
        f"atualizada(s), {len(result['deleted'])} removida(s), {len(result['errors'])} erro(s)."
    )
    if result['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
