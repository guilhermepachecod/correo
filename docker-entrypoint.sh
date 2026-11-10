#!/bin/sh
# Garante que os diretórios montados como volume (podem vir com dono "root"
# do host) sejam graváveis pelo usuário não-root "correo" antes de soltar
# privilégios e executar o comando real (gunicorn).
set -e

mkdir -p /app/database /app/accounts /app/database/attachments
chown -R correo:correo /app/database /app/accounts 2>/dev/null || true

exec su -s /bin/sh correo -c "exec $*"
