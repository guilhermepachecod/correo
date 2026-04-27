#!/bin/bash
set -euo pipefail

# ── Configuração ───────────────────────────────────────────────────────────────
DIR="/opt/correo"
BASE_URL="http://10.0.0.12/python/correo"
CONTAINER="correo"
IMAGE="correo"
PORT=15002

# ── Cores para output ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}➜  $*${NC}"; }
success() { echo -e "${GREEN}✓  $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠  $*${NC}"; }
error()   { echo -e "${RED}✗  $*${NC}"; }

# ── Verificar Docker ───────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    error "Docker não encontrado. Instale o Docker antes de continuar."
    exit 1
fi

info "=== Deploy: Correo Webmail ==="
echo ""

# ── Criar estrutura de diretórios ──────────────────────────────────────────────
info "Criando diretórios..."
mkdir -p "$DIR/templates" "$DIR/static" "$DIR/database"
cd "$DIR" || { error "Falha ao entrar em $DIR"; exit 1; }
success "Diretório: $DIR"

# ── Função de download ─────────────────────────────────────────────────────────
download() {
    local file="$1"
    local dest="${2:-$file}"
    info "Baixando $file..."
    if ! wget -q -O "$dest" "$BASE_URL/$file"; then
        error "Falha ao baixar $file de $BASE_URL/$file"
        exit 1
    fi
}

# ── Baixar arquivos principais ─────────────────────────────────────────────────
info "Baixando arquivos da aplicação..."

MAIN_FILES=(
    "app.py"
    "database.py"
    "mail_service.py"
    "requirements.txt"
    "Dockerfile"
    "docker-compose.yml"
)

for f in "${MAIN_FILES[@]}"; do
    download "$f"
done

# ── Baixar templates ───────────────────────────────────────────────────────────
TEMPLATES=("index.html")
for t in "${TEMPLATES[@]}"; do
    download "$t" "templates/$t"
done

# ── Baixar arquivos estáticos ──────────────────────────────────────────────────
STATIC_FILES=("app.js")
for s in "${STATIC_FILES[@]}"; do
    download "$s" "static/$s"
done

success "Todos os arquivos baixados"

# ── Parar e remover container antigo ──────────────────────────────────────────
echo ""
info "Removendo container antigo..."
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    docker rm -f "$CONTAINER" && success "Container $CONTAINER removido" || warn "Falha ao remover container"
else
    warn "Container $CONTAINER não encontrado (ok)"
fi

# ── Remover imagem antiga ──────────────────────────────────────────────────────
info "Removendo imagem antiga..."
if docker image inspect "$IMAGE" &>/dev/null; then
    docker rmi -f "$IMAGE" && success "Imagem $IMAGE removida" || warn "Falha ao remover imagem"
else
    warn "Imagem $IMAGE não encontrada (ok)"
fi

# ── Build ──────────────────────────────────────────────────────────────────────
echo ""
info "Buildando imagem Docker (sem cache)..."
if docker build --no-cache -t "$IMAGE" "$DIR"; then
    success "Imagem buildada com sucesso"
else
    error "Falha no docker build"
    exit 1
fi

# ── Iniciar container ──────────────────────────────────────────────────────────
echo ""
info "Iniciando container..."
docker run -d \
    --name "$CONTAINER" \
    --restart on-failure:5 \
    -p "${PORT}:${PORT}" \
    -v "${DIR}/database:/app/database" \
    "$IMAGE"

success "Container iniciado"

# ── Verificar se subiu ─────────────────────────────────────────────────────────
echo ""
info "Aguardando aplicação inicializar..."
MAX_WAIT=30
WAITED=0
while [ $WAITED -lt $MAX_WAIT ]; do
    if curl -sf "http://localhost:${PORT}/" &>/dev/null; then
        success "Aplicação respondendo em http://localhost:${PORT}"
        break
    fi
    sleep 2
    WAITED=$((WAITED + 2))
done

if [ $WAITED -ge $MAX_WAIT ]; then
    warn "Aplicação ainda não respondeu após ${MAX_WAIT}s. Verifique os logs:"
    echo "  docker logs $CONTAINER"
fi

# ── Status final ───────────────────────────────────────────────────────────────
echo ""
success "=== Deploy concluído ==="
echo ""
echo "  Container : $CONTAINER"
echo "  Porta     : $PORT"
echo "  Dados     : ${DIR}/database  (persistente)"
echo "  Logs      : docker logs -f $CONTAINER"
echo ""
docker ps --filter "name=${CONTAINER}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"