# correo — Fase 5: funcionalidades

Complemento do `docs/CORRECOES.md`. Fase 2 (sincronização) está entregue —
`folder_state.role` e `GET /api/emails?view=...` já existem e as visões
unificadas (seção 1 abaixo) podem começar. **Ressalva importante**: a Fase 2
só foi testada com `imapclient.IMAPClient` mockado, nunca contra um servidor
IMAP real (sem acesso a um neste ambiente de trabalho) — vale validar isso
com uma conta real antes ou em paralelo a começar a Fase 5.

Mesmas regras gerais do `CORRECOES.md`: commits pequenos (`fase5: ...`), não quebrar a
API existente, autenticação e CSRF em todas as rotas novas, testes junto com cada item.

---

## 0. Pendências da Fase 1 encontradas no deploy (corrigir antes)

1. **Container roda como root.** `docker exec correo id -u` retorna `0`. Adicionar usuário
   não-root no `Dockerfile` (`USER correo`, UID fixo 1000) e documentar no README o
   `chown -R 1000:1000` dos volumes `database/`, `accounts/`, `attachments/`.
2. **Gunicorn sem log de acesso.** Adicionar `--access-logfile -` ao `CMD`.
3. **CSRF faltando em chamadas do frontend.** O botão "sincronizar .conf"
   (`static/js/accounts.js`, `fetch('/api/accounts/sync-conf', { method: 'POST' })`) não envia
   `X-Correo-CSRF`. Criar um wrapper único `api(url, opts)` em `static/js/utils.js` que
   sempre envia o header em métodos mutáveis e trata `401`, e substituir **todos** os
   `fetch` diretos por ele. Adicionar teste que varre `static/js/` e falha se existir
   `fetch(` fora do wrapper.
4. Expor a sincronização de `.conf` também como comando:
   `python scripts/import_conf.py` (mesma função `sync_conf_to_db`, imprime created/updated/deleted/errors).

**Status:** item 1, 2, 3 e 4 entregues — ver commits `fase1 (pendência deploy): ...`
em `main`. `docker exec correo id -u` agora deve retornar `1000`; o botão "sincronizar
.conf" e todo outro `fetch` do frontend passam por `api()` (CSRF garantido por
`tests/test_no_raw_fetch.py`); `scripts/import_conf.py` existe e foi testado localmente.

---

## 1. Menu de visões unificadas

Barra lateral, acima da lista de contas, uma seção **"Todas as contas"** com:

| Item | Conteúdo | Contador |
|------|----------|----------|
| **Todos** | INBOX de todas as contas | total |
| **Não lidos** | INBOX de todas as contas com `is_read=false` | não lidos |
| **Com estrela** | `is_starred=true` em qualquer pasta, todas as contas | total |
| **Enviados** | pastas `role=sent` de todas as contas | — |
| **Lixeira** | pastas `role=trash` de todas as contas | — |

Requisitos:
- Backend: `/api/emails?view=all|unread|starred|sent|trash` (usar `folder_state.role`, não nome de pasta).
- Cada linha da lista mostra um **marcador da conta** (cor fixa por conta + e-mail abreviado).
- Configuração de cor por conta na tela de contas (default gerado do e-mail).
- **Não lidos**: ao abrir uma mensagem, ela continua na lista até sair da visão
  (não sumir sob o cursor); some no próximo refresh/navegação.
- Contadores atualizados em tempo real pelos eventos Socket.IO existentes.
- Ações em lote funcionam na visão unificada (mensagens de contas diferentes no mesmo lote:
  o backend agrupa por conta e pasta).
- Visão padrão ao abrir o app: **Não lidos** (configurável em Configurações).
- Atalho de teclado: `g a` (todos), `g u` (não lidos), `g s` (estrela).

> **Entregue** (sidebar, `selectUnifiedView`, marcador de conta, badges via
> `/api/views/counts`) — ver commits `fase5.1: ...`. Ressalvas, por
> decisão explícita do usuário ou por ainda não ter sido feito:
> - **"Não lidos" remove a mensagem da lista no momento em que é marcada
>   como lida** (não "continua até sair da visão") — mudança pedida pelo
>   usuário em relação ao spec original.
> - Cor do marcador é gerada por hash do e-mail (`_accountColor` em
>   `emails.js`), sem UI de configuração por conta — ainda não feito.
> - Ações em lote na visão unificada ainda não testadas especificamente
>   (a lógica de agrupar por conta/pasta já existe desde a Fase 2.6 e
>   deveria funcionar, mas não tem teste dedicado pra isso).
> - Visão padrão ainda é a última pasta/conta selecionada, não "Não
>   lidos" — não mudei o comportamento de abertura do app.
> - Atalhos de teclado `g a`/`g u`/`g s` não implementados.

---

## 2. Compose em tela grande

Substituir o compose atual por um **modal centralizado e grande**:

- Tamanho padrão: `min(1100px, 92vw)` × `min(860px, 90vh)`, centralizado, com fundo escurecido.
- Botão **maximizar** (ocupa a viewport inteira) e **minimizar** (vira uma barra no canto
  inferior, permitindo ler outros e-mails enquanto escreve). Estado lembrado em `localStorage`.
- Mobile (largura < 768px): sempre tela cheia.
- Campos: **De** (select com todas as contas; default = conta da mensagem que está sendo
  respondida, ou a conta padrão), Para, Cc, Cco (Cc/Cco recolhidos atrás de um link), Assunto.
- Autocomplete de destinatários a partir dos endereços já vistos no banco (from/to/cc), ordenado por frequência.
- Editor: área de texto grande; formatação básica (negrito, itálico, lista, link) com HTML
  simples sanitizado; envio multipart (texto + HTML).
- Anexos: arrastar e soltar na área inteira do modal + botão; mostrar tamanho; respeitar
  `MAX_CONTENT_LENGTH`.
- Responder / Responder a todos / Encaminhar preenchem destinatários, assunto (`Re:`/`Fwd:`),
  citação e cabeçalhos `In-Reply-To`/`References`.
- **Rascunho automático** a cada 10 s e ao fechar: salvar localmente no banco; se a conta
  tiver `role=drafts`, também gravar no servidor via `APPEND` (substituindo o rascunho anterior).
- Fechar com conteúdo não enviado pede confirmação.
- Após enviar: `APPEND` na pasta `role=sent` da conta quando o servidor não fizer isso
  sozinho (Gmail faz; Dovecot/Postfix não). Configurável por conta: "Salvar cópia em Enviados".
- Atalhos: `c` abre novo, `Ctrl+Enter` envia, `Esc` minimiza.

> **Entregue** — ver commits `fase5.2: ...`. Ressalvas, por decisão
> explícita (risco) ou por ainda não ter sido feito:
> - **Rascunho automático só grava no `localStorage`, nunca no IMAP/banco.**
>   Decisão deliberada: gravar via `APPEND` a cada 10s bombardearia contas
>   em `mail.soft.dev.br` (fail2ban agressivo) com escrita repetida. O
>   critério de aceite #5 ("rascunho sobrevive a fechar a aba") está coberto
>   assim mesmo — ao reabrir o app, o rascunho é oferecido de volta
>   automaticamente. Persistência de verdade no servidor (`role=drafts`,
>   só ao fechar/minimizar, substituindo o rascunho anterior) ficou de fora.
> - "Salvar cópia em Enviados" configurável por conta não existe — hoje é
>   sempre tentado (`role=sent`), já era o comportamento desde a Fase 2.6.
> - Atalho `c` para abrir novo compose **não foi restaurado** — já tinha
>   sido removido antes por conflitar com Ctrl+C; não revisitei esse
>   conflito nesta rodada. `Esc` continua **fechando** (com a confirmação
>   de rascunho), não minimizando — mudar isso tocaria o handler
>   compartilhado com os outros modais (contas, filtros) e não parecia
>   valer o risco agora.
> - Corrigi de brinde um bug preexistente: digitar `s`/`e`/`j`/`k` dentro
>   do editor HTML (contenteditable) disparava os atalhos de teclado da
>   lista de e-mails (estrela, excluir, navegar) por engano.

---

## 3. Feeds (RSS/Atom)

- Dependência: `feedparser`.
- Modelos: `Feed` (id, nome, url, site_url, intervalo_min default 30, ativo, último erro,
  último fetch, etag, last_modified) e `FeedItem` (feed_id, guid único por feed, título,
  link, autor, publicado_em, resumo sanitizado, lido, com estrela).
- Coleta em thread de background respeitando `ETag`/`Last-Modified`; timeout 15 s;
  backoff em erro; nunca bloquear o sync de e-mail.
- Retenção: manter itens por `FEED_RETENTION_DAYS` (default 60), exceto os com estrela.
- UI: seção **Feeds** na barra lateral (abaixo das contas) com "Todos os feeds" e um
  item por feed, contador de não lidos.
- Leitura: título, origem, data, resumo sanitizado (mesmo `nh3` + iframe sandbox do e-mail),
  botão "Abrir original" (`target=_blank rel=noopener noreferrer`).
- Ações: marcar lido/não lido, estrela, marcar todos como lidos.
- Gerenciamento: adicionar por URL do site ou do feed (descobrir `<link rel="alternate">`
  quando for URL de site), editar, remover, **importar/exportar OPML**.
- **Feeds NÃO entram nas visões de e-mail** (Todos/Não lidos). Opção em Configurações
  "Mostrar itens de feed em Não lidos" (default **desligado**), gravada no banco.
- Proteção SSRF: recusar URLs que resolvam para IPs privados/loopback/link-local, exceto se
  `FEEDS_ALLOW_PRIVATE=true`.

---

## 4. Calendário

Escopo inicial: **calendário local** (o servidor de e-mail não tem CalDAV).
Arquitetura preparada para CalDAV futuro (camada `CalendarBackend` com implementação `local`).

- Modelos: `Calendar` (nome, cor) e `Event` (calendar_id, título, descrição, local,
  início, fim, dia_inteiro, timezone, recorrência RRULE opcional, uid ICS, lembrete_min).
- Timezone padrão `America/Sao_Paulo` (env `TZ`); gravar em UTC no banco.
- UI: item **Calendário** na barra lateral. Visões **mês**, **semana** e **agenda** (lista dos
  próximos 30 dias). Criar evento clicando no dia/horário; editar/excluir em modal.
- Recorrência: diária, semanal (dias da semana), mensal, anual, com fim por data ou contagem
  (`python-dateutil` rrule para expandir na consulta).
- **Convites em e-mail**: ao abrir uma mensagem com anexo `text/calendar` / `.ics`, mostrar
  cartão com título, data/hora e botões **Adicionar ao calendário** (e, se `METHOD:REQUEST`,
  **Aceitar / Recusar**, enviando a resposta iCalendar `METHOD:REPLY` pela conta que recebeu).
- Importar/exportar `.ics` por calendário.
- Lembretes: notificação no navegador (Notification API, com permissão) quando o app estiver
  aberto, disparada pelo backend via Socket.IO no horário do lembrete.
- Visão "Hoje" pequena no topo da barra lateral: próximos 3 eventos.

---

## 5. Critérios de aceite

1. Com 8 contas, "Não lidos" mostra os não lidos de todas, cada um com o marcador da conta, e o contador bate com a soma das contas.
2. Marcar como lido em "Não lidos" reflete no servidor e no contador sem recarregar a página.
3. Compose abre centralizado e grande; maximiza, minimiza e restaura; no celular abre em tela cheia.
4. Responder usa a conta correta em "De", e a resposta aparece em Enviados da conta (servidor).
5. Rascunho sobrevive a fechar a aba e reabrir o app.
6. Feed adicionado pela URL do site é descoberto e coletado; itens não aparecem em "Não lidos" com a opção desligada.
7. OPML exportado e reimportado em banco limpo recria os mesmos feeds.
8. Evento semanal recorrente aparece corretamente nas visões mês/semana/agenda.
9. E-mail com convite `.ics` mostra o cartão e "Adicionar ao calendário" cria o evento com horário correto (fuso).
10. Nenhum `fetch(` fora do wrapper `api()` no frontend (teste automatizado).

---

## 6. Ordem de entrega

1. Pendências da seção 0.
2. Menu de visões unificadas (seção 1).
3. Compose (seção 2).
4. Feeds (seção 3).
5. Calendário (seção 4).
