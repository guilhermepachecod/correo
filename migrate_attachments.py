#!/usr/bin/env python3
"""
migrate_attachments.py
Migra anexos armazenados como blob no banco (campo `data`)
para arquivos em disco (campo `file_path`).

Uso:
  docker exec correo python /app/migrate_attachments.py
  ou
  python migrate_attachments.py
"""
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# Importa app para ter contexto Flask + banco
sys.path.insert(0, '/app')
from app import app
from database import db, Attachment, ATTACHMENTS_DIR


def migrate():
    with app.app_context():
        # Conta quantos ainda estão como blob
        total = db.session.query(db.func.count(Attachment.id))\
                          .filter(Attachment.data != None,
                                  Attachment.file_path == None).scalar() or 0

        if total == 0:
            log.info('Nenhum anexo para migrar. Banco já está limpo.')
            return

        log.info(f'{total} anexo(s) para migrar para disco ({ATTACHMENTS_DIR})')
        os.makedirs(ATTACHMENTS_DIR, exist_ok=True)

        migrated = 0
        errors   = 0
        batch_size = 50

        while True:
            atts = db.session.query(Attachment)\
                             .filter(Attachment.data != None,
                                     Attachment.file_path == None)\
                             .limit(batch_size).all()
            if not atts:
                break

            for att in atts:
                try:
                    raw = att.data
                    if not raw:
                        att.data = None
                        continue

                    # Salva em disco usando o método do modelo
                    att.save_to_disk(raw)
                    att.data = None  # libera blob
                    migrated += 1

                    if migrated % 100 == 0:
                        log.info(f'  {migrated}/{total} migrados...')

                except Exception as e:
                    log.error(f'Erro ao migrar attachment id={att.id}: {e}')
                    errors += 1

            db.session.commit()

        log.info(f'Migração concluída: {migrated} migrados, {errors} erros.')


if __name__ == '__main__':
    migrate()