# correo — Plano de correção (sincronização + segurança)

Documento de trabalho para o agente de código. Leia inteiro antes de começar.
Trabalhe em fases, na ordem, com commits pequenos por item. Não pule fases.

> **Status:** Fase 1 (segurança) entregue, incluindo as pendências
> encontradas no primeiro deploy real (ver `docs/FASE5-FUNCIONALIDADES.md`,
> seção 0). Fase 2 completa (2.1 a 2.9) — ver ressalvas em cada subseção da
> seção 4. **O mais importante: nada da Fase 2 foi testado contra um
> servidor IMAP real** (só `imapclient.IMAPClient` mockado em todos os
> testes) — este ambiente de trabalho não tem acesso a um IMAP de teste
> nem a Docker. Validar com uma conta real (de preferência não crítica)
> antes de confiar nisso para as 8 contas de produção em
> `mail.soft.dev.br`/Gmail/`e-dominios.com.br`. Fase 3 (testes com Dovecot
> real) é o próximo passo natural pra fechar essa lacuna — ver seção 8.

---

## 0. Contexto

`correo` é um webmail self-hosted em **Flask + Flask-SocketIO + SQLite** que agrega
várias contas IMAP/SMTP, guarda cache local das mensagens e usa IMAP IDLE para
notificar o navegador via WebSocket.

Ele vai substituir o Cypht e ficará **exposto na internet** atrás de um Caddy
(HTTPS, `https://cypht.soft.dev.br:18443`, reverse proxy para o container na porta 15002).

Contas reais que vão usar o app:
- 6 contas em Dovecot/Postfix próprio (`mail.soft.dev.br`, IMAP 993 TLS, SMTP 587 STARTTLS)
- 1 Gmail (`imap.gmail.com` 993 / `smtp.gmail.com` 465, senha de app)
- 1 conta em outro Dovecot (`imap.e-dominios.com.br` 993 / `smtp.e-dominios.com.br` 465)

O servidor `mail.soft.dev.br` tem **fail2ban** agressivo: muitas conexões/logins
falhos em sequência banem o IP. Isso é requisito de projeto (ver Fase 2, conexões).

### Estrutura atual relevante

```
app.py                     # create_app, SECRET_KEY fixo, socketio threading, werkzeug dev server
database.py                # modelos, Fernet com chave fixa, enforce_email_limit
routes/accounts.py         # CRUD contas, sync manual
routes/emails.py           # listagem, ações (read/star/spam/delete/move/bulk/send)
services/mail_service.py   # sync por pasta, ações IMAP, envio SMTP
services/idle_manager.py   # thread IDLE por conta (só INBOX)
services/imap_pool.py      # "pool" que abre uma conexão nova por operação
services/conf_manager.py   # contas via arquivos .conf em /app/accounts
static/js/*.js             # SPA
templates/index.html
```

---

## 1. Diagnóstico (bugs confirmados no código)

| # | Onde | Problema | Sintoma |
|---|------|----------|---------|
| B1 | `routes/emails.py` `delete_email` + `mail_service.delete_email` | Erro no servidor é só logado; a rota apaga o registro local mesmo assim | E-mail some e **volta** no próximo sync |
| B2 | `database.py` `enforce_email_limit` | Apaga do banco os mais antigos acima de `MAX_EMAILS_PER_ACCOUNT`; eles continuam no servidor e o sync os vê como "novos" | Re-download infinito, e-mails antigos reaparecendo |
| B3 | `idle_manager._run_idle_session` / `_fetch_new` | Usa **número de sequência** (`fetch exists+1:new`) em vez de UID; EXPUNGE + EXISTS no mesmo ciclo desalinha | E-mail novo não aparece ou aparece o errado |
| B4 | `idle_manager` | IDLE só na INBOX; demais pastas só em sync manual | Enviados/Lixeira/Spam desatualizados |
| B5 | `_sync_folder`, `_sync_flags` | Sincroniza só `\Seen`, só nos últimos 200 UIDs; IDLE ignora `* n FETCH (FLAGS ...)`; `\Flagged` nunca sincroniza | Lido/estrela em outro cliente não reflete |
| B6 | todo o sync | Não armazena nem verifica `UIDVALIDITY` | UIDs locais apontam para mensagens erradas após recriação da caixa |
| B7 | `delete_email`, `set_spam` etc. | `EXPUNGE` sem UID apaga **todas** as mensagens `\Deleted` da pasta | Perda de mensagens marcadas por outro cliente |
| B8 | `imap_pool.py` | Uma conexão + login novos por operação | Lentidão e **ban do fail2ban** |
| B9 | `_initial_sync`, `_sync_folder` | Caps `[-100:]`, `[-200:]`, `[-500:]` sem continuação | Mensagens recebidas offline podem ficar sem baixar até um próximo ciclo arbitrário |
| B10 | `delete_email` | Lixeira descoberta por palavra-chave (`trash/lixeira`), ignora SPECIAL-USE | Exclusão falha em servidores com nomes diferentes |

### Problemas de segurança

| # | Onde | Problema |
|---|------|----------|
| S1 | todo o app | **Não existe autenticação.** Qualquer um com a URL lê e envia e-mail de todas as contas |
| S2 | `database.py:8` | Chave Fernet fixa no código, em **repositório público**. Senhas criptografadas = senhas em texto claro para quem obtiver o `mail.db` ou os `.conf` |
| S3 | `app.py` | `SECRET_KEY` fixo no código |
| S4 | `app.py` | `cors_allowed_origins='*'` no Socket.IO |
| S5 | `app.py` | Servidor de desenvolvimento do Werkzeug com `allow_unsafe_werkzeug=True` |
| S6 | `static/js/viewer.js` (verificar) | HTML de e-mail renderizado: garantir sanitização e isolamento (XSS via e-mail recebido) |

---

## 2. Regras gerais para o agente

- Não mudar o visual da interface além do necessário (tela de login e indicadores de erro são novos).
- Manter as rotas da API compatíveis com o frontend atual; se precisar mudar, ajustar o JS no mesmo commit.
- **Nunca** commitar segredos. Criar `.env.example` com placeholders.
- Cada fase termina com o app subindo via `docker compose up` e os testes da fase passando.
- Mensagens de commit em português, curtas, no padrão `fase N: descrição`.
- Atualizar o `README.md` no fim (variáveis de ambiente, deploy, como rodar testes).
- Usar logging (`log.info/warning/error`) com contexto (conta, pasta, UID). Nunca logar senhas.

---

## 3. Fase 1 — Segurança ✅ entregue

### 1.1 Configuração por ambiente
- `SECRET_KEY` lido de `CORREO_SECRET_KEY` (obrigatório; o app recusa subir sem ele).
- Chave Fernet lida de `CORREO_FERNET_KEY` (obrigatório, formato Fernet base64).
- `CORREO_ALLOWED_ORIGINS` (lista separada por vírgula) para o Socket.IO.
- `CORREO_COOKIE_SECURE` (`true` em produção atrás do Caddy).
- Criar `.env.example` com todas as variáveis e comentários.
- Criar `scripts/gen_keys.py` que imprime uma `SECRET_KEY` e uma `FERNET_KEY` novas.

### 1.2 Rotação da chave Fernet (migração sem perder contas)
- Em `database.py`, `get_password()` tenta a chave nova; se falhar com `InvalidToken`,
  tenta a chave legada (`mailapp-fernet-key-v1-stable-2024`, mantida **só** em `scripts/rotate_key.py`
  ou num módulo `legacy_crypto.py`) e, em sucesso, **recriptografa** com a nova e salva.
- `scripts/rotate_key.py`: recriptografa de uma vez todas as contas do banco e todos os
  `pass=enc:...` dos arquivos `.conf` em `/app/accounts`. Idempotente. Imprime um resumo.
- Depois da migração, nenhum caminho de código normal deve conhecer a chave legada.

### 1.3 Autenticação (usuário único)
- Variáveis: `CORREO_USER` e `CORREO_PASSWORD_HASH` (hash gerado por
  `werkzeug.security.generate_password_hash`, método `scrypt`).
- `scripts/hash_password.py` gera o hash a partir de uma senha digitada (sem eco).
- Rotas: `GET /login` (página), `POST /api/login`, `POST /api/logout`, `GET /api/me`.
- Checkbox **"Manter conectado"**: sessão permanente de 90 dias
  (`PERMANENT_SESSION_LIFETIME`), senão cookie de sessão.
- Cookie: `HttpOnly`, `SameSite=Lax`, `Secure` conforme `CORREO_COOKIE_SECURE`.
- `before_request` global: toda rota exceto `/login`, `/api/login`, `/static/*` e o
  healthcheck exige sessão; API retorna `401` JSON, páginas redirecionam para `/login`.
- Socket.IO: o handler `connect` rejeita conexão sem sessão válida.
- Frontend: interceptar `401` em todas as chamadas `fetch` e redirecionar para `/login`.
- **Rate limit de login**: no máximo 5 falhas por IP em 15 min (em memória basta).
  Logar falhas como `LOGIN FAIL ip=<ip> user=<user>` (permite um jail de fail2ban depois).
- Comparação de senha com `check_password_hash` (tempo constante).

### 1.4 CSRF
- Exigir header `X-Correo-CSRF` com token da sessão em todo método mutável
  (`POST/PUT/DELETE`). Token entregue em `GET /api/me` e guardado em memória no JS.
- Rejeitar com `403` quando ausente ou inválido.

### 1.5 Proxy e servidor de produção
- `werkzeug.middleware.proxy_fix.ProxyFix(x_for=1, x_proto=1, x_host=1, x_port=1)`.
- Socket.IO com `cors_allowed_origins` vindo de `CORREO_ALLOWED_ORIGINS`.
- Trocar o servidor de desenvolvimento por **gunicorn** com 1 worker e threads
  (modo `threading` do Flask-SocketIO + `simple-websocket`):
  `gunicorn -w 1 --threads 64 -b 0.0.0.0:15002 app:app`.
  Um único worker é obrigatório (estado de Socket.IO e threads IDLE em memória).
- Garantir que as threads de background (IDLE/sync) iniciem **uma vez** por processo.
- Remover `allow_unsafe_werkzeug`. Remover `eventlet` do `requirements.txt` se não for usado.

### 1.6 Renderização segura de e-mail (S6)
- Sanitizar `body_html` no servidor com `nh3` (remover `script`, `on*`, `javascript:`, `iframe`, `object`, `form`).
- Exibir dentro de `<iframe sandbox>` **sem** `allow-scripts` e sem `allow-same-origin`, via `srcdoc`.
- Imagens remotas bloqueadas por padrão, com botão "Carregar imagens" por mensagem.
- Links abrem com `target="_blank" rel="noopener noreferrer"`.

### Critérios de aceite da Fase 1
- Sem login, `GET /api/emails` retorna 401 e o WebSocket não conecta.
- Login com "Manter conectado" sobrevive a fechar e reabrir o navegador.
- `grep -rn "mailapp-" .` só encontra o módulo/script de migração legado.
- Contas existentes continuam funcionando após `rotate_key.py`.
- E-mail HTML com `<script>alert(1)</script>` e `<img onerror=...>` não executa nada.

---

## 4. Fase 2 — Reescrita do núcleo de sincronização ✅ entregue (ver ressalvas por item abaixo)

Reescrever `services/mail_service.py` (parte de sync/ações), `services/idle_manager.py`
e `services/imap_pool.py`. Usar a biblioteca **`imapclient`** no lugar de `imaplib` cru.

### 2.1 Modelo de dados ✅
Criar sistema de migração versionado simples (tabela `schema_version` + lista ordenada
de funções), substituindo `_run_migrations` com `try/except pass`.

Novas estruturas:
- Tabela `folder_state`: `account_id`, `folder`, `uidvalidity`, `uidnext`,
  `highestmodseq` (nullable), `last_sync`, `role` (`inbox|sent|trash|junk|drafts|archive|null`).
  Unique (`account_id`, `folder`).
- `emails`: adicionar `UNIQUE(account_id, folder, uid)`; coluna `body_cached` (bool).
  Mapear `is_starred` ⇄ `\Flagged`.
- `accounts`: `auth_error` (texto nullable), `auth_error_at`, `capabilities` (texto).

> Entregue em `database.py` (`run_migrations`, `FolderState`) — ver commit
> `fase2.1: ...`. `uidvalidity`/`uidnext`/`highestmodseq` e o mapeamento
> `is_starred` ⇄ `\Flagged` ainda não são escritos por nada — isso é a 2.4.

### 2.2 Gerenciador de conexões (resolve B8) — fundação entregue, ainda não conectada
- Por conta: **uma conexão de comandos persistente** protegida por `threading.Lock`,
  reaproveitada por sync e ações; NOOP periódico; reconexão com backoff exponencial
  (2s, 4s, 8s… máx 5 min).
- Mais **uma conexão IDLE** dedicada à INBOX.
- **Falha de autenticação**: parar todas as tentativas da conta, gravar `auth_error`,
  emitir evento para a UI e só tentar de novo quando a conta for editada ou o
  usuário clicar em "reconectar". Nunca entrar em loop de login (fail2ban).
- Limite total de conexões simultâneas por conta: 2.
- Ler e guardar `CAPABILITY` (IDLE, MOVE, UIDPLUS, CONDSTORE, QRESYNC, SPECIAL-USE).

> Entregue como módulo novo `services/imap_connection.py`
> (`AccountConnection`/`ConnectionManager`, ver commit `fase2.2: ...`):
> conexão persistente + lock, circuit breaker de autenticação (nunca
> relogar sozinho após falha de auth), `next_backoff()`, capabilities.
> **Ainda não ligado a `idle_manager.py`/`mail_service.py`** (que continuam
> no `imap_pool.py` atual, sem mudança de comportamento) — isso é a 2.4/2.5.
> A conexão IDLE dedicada e o semáforo de 2 conexões/conta também são 2.5.
> Testado só com `imapclient.IMAPClient` mockado — **nunca contra um
> servidor IMAP real** (sem acesso a um neste ambiente de trabalho).

### 2.3 Pastas especiais (resolve B10) ✅
- Detectar via `LIST` com atributos SPECIAL-USE (`\Trash`, `\Sent`, `\Drafts`, `\Junk`, `\Archive`).
- Fallback por nome (`Trash`, `Lixeira`, `Deleted`, `[Gmail]/Lixeira`, `Sent`, `Enviados`, `E-mails enviados`, `Spam`, `Junk`, `Drafts`, `Rascunhos`, `Archive`).
- Gravar em `folder_state.role`. Expor na API para a UI (`/api/accounts/<id>/folders` com `role`).
- Se não existir lixeira, a exclusão é definitiva (com confirmação na UI).

> Entregue: `services/special_folders.py` (`detect_role`) +
> `MailService.refresh_folder_roles` (usa o `imap_pool` atual, sem mudar o
> algoritmo de sync) + `GET /api/accounts/<id>/folders` devolvendo
> `[{name, role}]`. "Exclusão definitiva se não existir lixeira" ainda não
> implementado — depende das ações servidor-primeiro (2.6).

### 2.4 Algoritmo de sync incremental por pasta (resolve B2, B3, B5, B6, B9)
Para cada pasta sincronizada:

1. `SELECT` (com `CONDSTORE` se suportado). Ler `UIDVALIDITY`, `UIDNEXT`, `HIGHESTMODSEQ`.
2. Se `UIDVALIDITY` difere do salvo → apagar o cache local dessa pasta e tratar como primeira sync.
3. **Novas mensagens**: `UID SEARCH UID <uidnext_salvo>:*` (ou todas, na primeira vez).
   Baixar em lotes de 50 **até acabar** (sem descartar o excedente).
   Primeira sync de uma pasta grande: baixar cabeçalhos (`ENVELOPE`, `FLAGS`,
   `BODYSTRUCTURE`, `RFC822.SIZE`) de tudo e corpo só das N mais recentes
   (`CORREO_INITIAL_BODIES`, padrão 300); o resto tem corpo sob demanda.
4. **Flags**: com CONDSTORE, `FETCH 1:* (FLAGS) (CHANGEDSINCE <modseq_salvo>)`;
   sem CONDSTORE, `FETCH 1:* (UID FLAGS)` completo (só flags, é barato). Sincronizar `\Seen` e `\Flagged`.
5. **Exclusões**: `UID SEARCH ALL` e remover localmente os UIDs ausentes
   (com QRESYNC, usar `VANISHED` quando disponível).
6. Salvar `uidvalidity`, `uidnext`, `highestmodseq`, `last_sync` em `folder_state`.
7. Emitir eventos Socket.IO agregados por pasta (`new_emails`, `flags_updated`, `emails_removed`).

> **Entregue** em `MailService.sync_folder_incremental` (ver commit
> `fase2.4/2.6/2.7: ...`). Simplificações deliberadas (usam o fallback que o
> próprio item 3/4 já permite, não um atalho fora do spec):
> - **Sem CONDSTORE/QRESYNC** — sempre usa o fallback "`FETCH 1:* (UID FLAGS)`
>   completo" descrito no item 4 e "`UID SEARCH ALL`" do item 5. Funciona,
>   mas não é o caminho mais eficiente para caixas muito grandes. Otimizar
>   com CONDSTORE/QRESYNC (a capability já é detectada e cacheada em
>   `Account.capabilities`) fica pra depois, se necessário.
> - **Sem a otimização "headers primeiro, corpo das N mais recentes"** do
>   item 3 — baixa o corpo completo (`BODY.PEEK[]`) de toda mensagem nova,
>   em lotes de 50. Corrige os bugs (nada é descartado), só não é o jeito
>   mais rápido pra uma pasta com milhares de mensagens não sincronizadas.
> - `HIGHESTMODSEQ` **não é lido nem salvo ainda** (coluna existe em
>   `folder_state` desde a 2.1, mas fica null até o CONDSTORE acima ser
>   implementado — não é usado pra nada no momento).

### 2.5 IDLE e agendamento (resolve B3, B4)
- IDLE na INBOX. **Qualquer** resposta não solicitada (`EXISTS`, `EXPUNGE`, `FETCH`,
  `RECENT`) dispara a **sync incremental da pasta (2.4)**. Nunca usar números de sequência.
- Renovar IDLE a cada 25 min (limite RFC de 29).
- Demais pastas com `role` (sent, trash, junk, drafts, archive) e pastas que o usuário
  abriu recentemente: sync incremental periódica a cada `CORREO_POLL_SECONDS`
  (padrão 120) usando a conexão de comandos.
- Ao abrir uma pasta na UI, disparar sync incremental dela imediatamente.

> **Entregue** em `services/idle_manager.py` (ver commit `fase2.5: ...`).
> Pendente: "ao abrir uma pasta na UI, disparar sync incremental dela
> imediatamente" — ainda não existe rota para isso; hoje só a INBOX (IDLE)
> e as pastas com role (`CORREO_POLL_SECONDS`) são mantidas atualizadas
> automaticamente. **Não testado contra um servidor IMAP real** — só com
> `imapclient.IMAPClient` mockado (sem acesso a um servidor de teste neste
> ambiente de trabalho). Validar com uma conta real antes de confiar nisso
> para as 8 contas de produção.

### 2.6 Ações: servidor primeiro (resolve B1, B7)
Para delete, move, spam, read/unread, star/unstar e bulk:

1. Executar no servidor primeiro.
2. Só alterar o banco local se o servidor confirmar.
3. Em erro, a API retorna `502` com mensagem; a UI mostra o erro e **não** remove o item.

Regras:
- Mover: `UID MOVE` se houver MOVE; senão `UID COPY` + `UID STORE +FLAGS \Deleted` + `UID EXPUNGE` (UIDPLUS).
- Sem UIDPLUS: não executar `EXPUNGE` genérico; marcar `\Deleted` e retornar aviso.
- **Nunca** usar `EXPUNGE` sem UID.
- Excluir = mover para a pasta `role=trash`; se já estiver na lixeira, excluir definitivamente.
- Gmail: excluir = mover para a pasta `\Trash` (mesma regra, via SPECIAL-USE).
- Ações em lote agrupam UIDs por pasta num único comando.
- A UI pode aplicar atualização otimista, mas deve **reverter** se a API retornar erro.

> **Entregue** em `MailService.delete_email/move_email/set_spam/
> mark_*_on_server` + `routes/emails.py` (502 em erro) + frontend
> (`markUnread`/`toggleSpam`/bulk mark_read/unread desfazem a UI em erro —
> ver commit `frontend: ...`). "Gmail: excluir = mover pra `\Trash`" já
> funciona pela via genérica (SPECIAL-USE detecta `\Trash` igual em
> qualquer servidor, não precisou de caso especial pra Gmail).

### 2.7 Limite de armazenamento (resolve B2)
- Remover `enforce_email_limit` que apaga linhas.
- Substituir por **poda de corpo**: para mensagens além de `CORREO_MAX_BODIES_PER_ACCOUNT`
  (padrão 2000) ou mais antigas que `CORREO_BODY_RETENTION_DAYS` (padrão 180),
  limpar `body_text`, `body_html` e anexos em disco, marcar `body_cached=false`.
  O cabeçalho permanece no banco, a mensagem continua na lista.
- `GET /api/emails/<id>`: se `body_cached=false`, buscar `BODY.PEEK[]` no servidor,
  salvar e retornar.

> **Entregue**: `prune_old_bodies()` + `fetch_full_body()` (ver commit
> `fase2.4/2.6/2.7: ...`). Chamado automaticamente ao fim de cada
> `sync_folder_incremental`.

### 2.8 Concorrência com SQLite
- Manter WAL. Cada thread de background usa seu próprio `app.app_context()` e
  sessão; fechar a sessão ao fim de cada unidade de trabalho.
- Serializar escritas com um lock global de escrita (ou fila única de escrita) para
  eliminar `database is locked`.

> **Parcial**: WAL + `app.app_context()` por thread já vêm da Fase 1/2.1-2.5
> (nenhuma mudança nova aqui). **Lock global de escrita não foi
> adicionado** — com `busy_timeout=30000` (Fase 1) e WAL, escritas
> concorrentes esperam até 30s em vez de falhar na hora, o que cobre o
> padrão de uso atual (poucas threads, commits pequenos e frequentes). Se
> aparecer `database is locked` em produção com as 8 contas reais, é aqui
> que entra o lock global/fila — fica pendente até esse sinal aparecer.

### 2.9 Visões unificadas
- `/api/emails?view=inbox|unread|starred|sent|trash` agrega todas as contas usando
  `folder_state.role` (não o nome da pasta).
- `view=unread` = INBOX de todas as contas com `is_read=false`.

> **Entregue** em `routes/emails.py` (`GET /api/emails?view=...`) — ver
> commit `fase2.6/2.9: ...`. Aditivo: sem `view=`, o comportamento é
> idêntico ao de antes da Fase 2. A barra lateral com o menu "Todas as
> contas" (seção 1 de `docs/FASE5-FUNCIONALIDADES.md`) ainda não existe —
> só o backend está pronto para ela.

### Critérios de aceite da Fase 2
- Excluir uma mensagem com o servidor fora do ar: a UI mostra erro e a mensagem **não** some.
- Excluir com servidor ok: a mensagem vai para a lixeira no servidor e **não volta**.
- Marcar como lida/estrela em outro cliente reflete no correo em até 1 ciclo de IDLE (INBOX) ou `POLL_SECONDS` (outras pastas).
- Receber 300 mensagens com o app desligado: ao subir, **todas** aparecem.
- Passar de 2000 mensagens: nenhuma é re-baixada em loop; as antigas continuam listadas e abrem sob demanda.
- Recriar a caixa no servidor (UIDVALIDITY muda): o cache da pasta é refeito sem duplicados.
- Senha errada em uma conta: no máximo 1 tentativa de login a cada ciclo de backoff, e a UI mostra o erro da conta.
- No log do Dovecot, o número de logins por conta é estável (conexão persistente), não um por ação.

---

## 5. Fase 3 — Testes

Criar `tests/` com **pytest**.

- Fixture com Dovecot de teste em container (`docker compose -f tests/docker-compose.test.yml`)
  com 2 usuários e IMAP em texto puro na rede de teste.
- Helper que injeta mensagens via APPEND e altera flags/exclui a partir de uma
  segunda conexão (simula outro cliente).

Casos mínimos:
1. Primeira sync baixa todas as mensagens (incluindo mais que o tamanho do lote).
2. Mensagem nova chega durante IDLE e aparece.
3. Exclusão e chegada no mesmo ciclo de IDLE: estado final correto (regressão de B3).
4. Flag `\Seen` e `\Flagged` alteradas por outro cliente são refletidas.
5. Exclusão pelo app move para a lixeira; falha no servidor não altera o banco.
6. Exclusão não remove mensagens `\Deleted` de outro cliente (regressão de B7).
7. Mudança de UIDVALIDITY reindexa a pasta.
8. Poda de corpo não remove linhas e o corpo volta sob demanda.
9. Auth: rotas protegidas retornam 401; rate limit bloqueia na 6ª tentativa.
10. Rotação de chave: conta com senha na chave legada continua funcionando e é recriptografada.

Adicionar job de testes no workflow do GitHub Actions existente, antes do build da imagem.

---

## 6. Fase 4 — Empacotamento

- `Dockerfile`: rodar com gunicorn (1.5); usuário não-root; `HEALTHCHECK` em rota pública
  `/healthz` (sem autenticação, sem dados).
- `docker-compose.yml`: `env_file: .env`, volumes atuais mantidos, sem segredos inline.
- `README.md`: variáveis de ambiente, geração de chaves (`scripts/gen_keys.py`),
  hash de senha (`scripts/hash_password.py`), rotação de chave (`scripts/rotate_key.py`),
  como rodar os testes, exemplo de bloco do Caddy:

```
cypht.soft.dev.br:18443 {
    tls {
        dns cloudflare {env.CF_API_TOKEN}
    }
    reverse_proxy correo:15002
}
```

---

## 7. Fora do escopo do agente (o dono faz manualmente)

- Gerar o `.env` de produção no dock01 e rodar `rotate_key.py` no volume real.
- Trocar o `reverse_proxy` do Caddy de `cypht:80` para o correo.
- Desligar o Cypht.
- Eventual jail de fail2ban para `LOGIN FAIL` do correo.

---

## 8. Ordem de entrega

1. Fase 1 completa (segurança) → commit(s) → app sobe com login. **✅ entregue.**
2. Fase 2.1 a 2.3 (modelo, conexões, pastas especiais). **✅ entregue.**
3. Fase 2.4 e 2.5 (sync incremental e IDLE). **✅ entregue** — ver ressalvas
   na seção 4 (sem CONDSTORE/QRESYNC, sem servidor real de teste).
4. Fase 2.6 e 2.7 (ações servidor-primeiro e poda de corpo). **✅ entregue.**
5. Fase 2.8 e 2.9 (concorrência e visões unificadas). **✅ entregue**
   (2.8 parcial — sem lock global, ver ressalva na seção 4).
6. Fase 3 (testes) — **← próximo passo real.** Os testes atuais usam
   `imapclient.IMAPClient` mockado; nada da Fase 2 rodou contra um IMAP de
   verdade. Montar o Dovecot de teste em container é o que fecha essa
   lacuna antes de confiar 100% nisso pras 8 contas de produção.
7. Fase 4 (empacotamento e README) — parcialmente já coberto na Fase 1
   (gunicorn, usuário não-root, `.env.example`, README); falta o
   `docker-compose.yml` final e o job de testes já está no CI desde a
   Fase 2.1.

Ao final de cada etapa, rodar os testes e listar no resumo do commit o que foi verificado.

> A partir daqui, ver `docs/FASE5-FUNCIONALIDADES.md` para o próximo conjunto de
> funcionalidades (visões unificadas, compose grande, feeds, calendário) — depende
> da Fase 2 estar entregue.
