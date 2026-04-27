#!/bin/sh
set -eu

# ── Configuração ───────────────────────────────────────────────────────────────
DIR="/opt/correo"
BASE_URL="http://10.0.0.12/python/correo"
CONTAINER="correo"
IMAGE="correo"
PORT=15002

# ── Output ─────────────────────────────────────────────────────────────────────
info()    { echo "==> $*"; }
success() { echo "[OK] $*"; }
warn()    { echo "[WARN] $*"; }
error()   { echo "[ERRO] $*"; }

# ── Verificar Docker ───────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    error "Docker não encontrado. Instale o Docker antes de continuar."
    exit 1
fi

info "=== Deploy: Correo Webmail ==="

# ── Criar estrutura de diretórios ──────────────────────────────────────────────
info "Criando diretórios..."
mkdir -p "$DIR/templates" "$DIR/static" "$DIR/database"
cd "$DIR" || { error "Falha ao entrar em $DIR"; exit 1; }
success "Diretório: $DIR"

# ── Função de download ─────────────────────────────────────────────────────────
download() {
    dl_file="$1"
    dl_dest="${2:-$1}"
    info "Baixando $dl_file..."
    if ! wget -q -O "$dl_dest" "$BASE_URL/$dl_file"; then
        error "Falha ao baixar $dl_file"
        exit 1
    fi
}

# ── Baixar arquivos principais ─────────────────────────────────────────────────
info "Baixando arquivos da aplicação..."

download "app.py"
download "database.py"
download "mail_service.py"
download "requirements.txt"
download "Dockerfile"
download "docker-compose.yml"

# ── Baixar templates ───────────────────────────────────────────────────────────
download "index.html" "templates/index.html"

# ── Baixar estáticos ───────────────────────────────────────────────────────────
download "app.js" "static/app.js"

success "Todos os arquivos baixados"

# ── Parar e remover container antigo ──────────────────────────────────────────
info "Removendo container antigo..."
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${CONTAINER}$"; then
    docker rm -f "$CONTAINER" && success "Container $CONTAINER removido"
else
    warn "Container $CONTAINER não encontrado (ok)"
fi

# ── Remover imagem antiga ──────────────────────────────────────────────────────
info "Removendo imagem antiga..."
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker rmi -f "$IMAGE" && success "Imagem $IMAGE removida"
else
    warn "Imagem $IMAGE não encontrada (ok)"
fi

# ── Build ──────────────────────────────────────────────────────────────────────
info "Buildando imagem Docker (sem cache)..."
if docker build --no-cache -t "$IMAGE" "$DIR"; then
    success "Imagem buildada com sucesso"
else
    error "Falha no docker build"
    exit 1
fi

# ── Iniciar container ──────────────────────────────────────────────────────────
info "Iniciando container..."
docker run -d \
    --name "$CONTAINER" \
    --restart on-failure:5 \
    -p "${PORT}:${PORT}" \
    -v "${DIR}/database:/app/database" \
    "$IMAGE"

success "Container iniciado"

# ── Aguardar aplicação ─────────────────────────────────────────────────────────
info "Aguardando aplicação inicializar..."
waited=0
max_wait=30
while [ "$waited" -lt "$max_wait" ]; do
    if wget -q -O /dev/null "http://localhost:${PORT}/" 2>/dev/null; then
        success "Aplicação respondendo em http://localhost:${PORT}"
        break
    fi
    sleep 2
    waited=$((waited + 2))
done

if [ "$waited" -ge "$max_wait" ]; then
    warn "Aplicação ainda não respondeu após ${max_wait}s. Verifique: docker logs $CONTAINER"
fi

# ── Status final ───────────────────────────────────────────────────────────────
echo ""
success "=== Deploy concluído ==="
echo "  Container : $CONTAINER"
echo "  Porta     : $PORT"
echo "  Dados     : ${DIR}/database  (persistente, nao apaga no redeploy)"
echo "  Logs      : docker logs -f $CONTAINER"
echo ""
docker ps --filter "name=${CONTAINER}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"