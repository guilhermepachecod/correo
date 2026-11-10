# correo

Webmail self-hosted (Flask + Flask-SocketIO + SQLite) que agrega várias
contas IMAP/SMTP, mantém cache local das mensagens e usa IMAP IDLE para
notificar o navegador em tempo real via WebSocket.

> Este README cobre o que já existe (Fase 1 — segurança). As próximas fases
> (motor de sincronização, testes automatizados, empacotamento final) vão
> ampliar este documento.

## Configuração (.env)

Copie `.env.example` para `.env` e preencha:

```bash
cp .env.example .env
```

### Gerar as chaves obrigatórias

```bash
python scripts/gen_keys.py
```

Cole a saída (`CORREO_SECRET_KEY` e `CORREO_FERNET_KEY`) no `.env`.

### Gerar o hash da senha de login

```bash
python scripts/hash_password.py
```

Cole o valor impresso em `CORREO_PASSWORD_HASH` e defina `CORREO_USER` com o
usuário desejado.

**Atenção:** o hash scrypt contém caracteres `$` (ex.:
`scrypt:32768:8:1$salt$hash`). O `docker-compose` interpola `$VAR` dentro do
`.env` — um `$` literal precisa ser escrito como `$$`, senão o container
recebe o hash corrompido e o login nunca funciona. `scripts/hash_password.py`
já imprime a versão com `$$` escapado quando necessário; use essa.

### Variáveis obrigatórias

| Variável | Descrição |
|---|---|
| `CORREO_SECRET_KEY` | Chave de sessão do Flask. O app recusa subir sem ela. |
| `CORREO_FERNET_KEY` | Chave de criptografia das senhas das contas de e-mail. |
| `CORREO_USER` | Usuário único de login da aplicação. |
| `CORREO_PASSWORD_HASH` | Hash (scrypt) da senha de login. |

### Variáveis opcionais

| Variável | Default | Descrição |
|---|---|---|
| `CORREO_ALLOWED_ORIGINS` | (nenhuma) | Origens permitidas no Socket.IO, separadas por vírgula. Defina com a URL pública (ex.: `https://cypht.soft.dev.br:18443`) em produção. |
| `CORREO_COOKIE_SECURE` | `false` | `true` quando atrás de HTTPS (Caddy) em produção. |
| `MAX_EMAILS_PER_ACCOUNT` | `2000` | Limite de mensagens em cache por conta. |
| `MAX_ACCOUNTS` | `20` | Limite de contas de e-mail cadastradas. |
| `ATTACHMENTS_DIR` | `/app/database/attachments` | Diretório dos anexos salvos em disco. |
| `ACCOUNTS_DIR` | `/app/accounts` | Diretório dos arquivos `.conf` de contas. |
| `CORREO_DATABASE_URI` | `sqlite:////app/database/mail.db` | URI do banco (útil para rodar/testar fora do Docker). |

## Rodando

O container roda como usuário não-root `correo` (UID/GID fixos em **1000**).
Os volumes montados (`database/`, `accounts/`, `attachments/`) precisam ser
graváveis por esse UID **antes** do primeiro `up`:

```bash
mkdir -p database accounts attachments
chown -R 1000:1000 database accounts attachments
docker compose up --build
```

Sem isso, o gunicorn sobe mas falha ao gravar no SQLite/anexos/`.conf`
(erro de permissão nos logs do container).

Acesse `http://localhost:15002` — a primeira tela é o login
(`CORREO_USER` / senha correspondente a `CORREO_PASSWORD_HASH`).

Healthcheck público (sem autenticação, sem dados): `GET /healthz`.

## Rotação da chave Fernet legada

Contas criadas antes da Fase 1 tinham a senha criptografada com uma chave
fixa no código-fonte (comprometida, já que o repositório é público). A
migração para a chave nova (`CORREO_FERNET_KEY`) é automática e transparente
no primeiro uso de cada conta — mas para migrar tudo de uma vez (banco +
arquivos `.conf`), rode dentro do container:

```bash
docker compose exec correo python scripts/rotate_key.py
```

Idempotente: pode rodar de novo sem efeito colateral em contas já migradas.

## Deploy atrás do Caddy (exemplo)

```
cypht.soft.dev.br:18443 {
    tls {
        dns cloudflare {env.CF_API_TOKEN}
    }
    reverse_proxy correo:15002
}
```
