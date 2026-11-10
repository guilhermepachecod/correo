// viewer.js — visualização de e-mail individual e threads

// ── Abrir e-mail ──────────────────────────────────────────────────────────────

async function openEmail(id) {
  state.selectedEmailId = id;
  state.threadOpen      = false;
  document.querySelectorAll('.email-item').forEach(el =>
    el.classList.toggle('active', parseInt(el.dataset.id) === id));

  // Marca como lido na lista imediatamente
  const le = state.emails.find(e => e.id === id);
  if (le) { le.is_read = true; _patchEmailItem(id, { is_read: true }); }
  // Na visão "Não lidos", ler uma mensagem a remove da lista (ela continua
  // aberta no painel de leitura, só sai da lista à esquerda)
  if (state.currentView === 'unread') _dropFromList(id);

  // Mostra painel imediatamente com dados da lista (que já temos)
  document.getElementById('reading-empty').style.display = 'none';
  document.getElementById('email-view').style.display    = 'flex';

  // Mobile: desliza para o viewer
  if (window.innerWidth < 768) {
    document.getElementById('reading-pane').classList.add('active-mobile');
    document.getElementById('email-list-panel').classList.add('hidden-mobile');
    // Preenche título no header mobile
    const mvt = document.getElementById('mobile-viewer-title');
    if (mvt && le) mvt.textContent = le.subject || '';
  }

  if (le) {
    document.getElementById('ev-subject').textContent   = le.subject;
    document.getElementById('ev-avatar').textContent    = (le.from_name || le.from_addr || '?')[0].toUpperCase();
    document.getElementById('ev-from-name').textContent = le.from_name || le.from_addr;
    document.getElementById('ev-from-addr').textContent = `<${le.from_addr}>`;
    document.getElementById('ev-to-line').textContent   = le.to_addr ? `Para: ${le.to_addr}` : '';
    document.getElementById('ev-date-meta').textContent = fmtDateFull(le.date);
    document.getElementById('ev-attachments').style.display = 'none';
    document.getElementById('ev-junk-banner').classList.remove('visible');
    // Skeleton no corpo enquanto carrega
    const bodyHtml = document.getElementById('ev-body-html');
    const bodyText = document.getElementById('ev-body-text');
    bodyHtml.style.display = 'block'; bodyText.style.display = 'none';
    bodyHtml.innerHTML = '<div class="body-skeleton"><div class="skel-line w80"></div><div class="skel-line w60"></div><div class="skel-line w90"></div><div class="skel-line w50"></div><div class="skel-line w70"></div></div>';
  }

  // Busca dados completos em background
  const email = await (await api(`/api/emails/${id}`)).json();

  // Se o usuário trocou de e-mail enquanto carregava, ignora
  if (state.selectedEmailId !== id) return;

  state.currentEmail = email;
  state.emailShowImages = settings.showImagesByDefault;
  // Usa modo padrão das configurações, mas só se o e-mail tiver HTML
  const preferredMode = settings.defaultEmailViewMode || 'html';
  state.emailViewMode = (preferredMode === 'html' && email.body_html) ? 'html' : 'text';

  document.getElementById('ev-subject').textContent   = email.subject;
  document.getElementById('ev-avatar').textContent    = (email.from_name || email.from_addr || '?')[0].toUpperCase();
  document.getElementById('ev-from-name').textContent = email.from_name || email.from_addr;
  document.getElementById('ev-from-addr').textContent = `<${email.from_addr}>`;
  document.getElementById('ev-to-line').textContent   = email.to_addr ? `Para: ${email.to_addr}` : '';
  document.getElementById('ev-date-meta').textContent = fmtDateFull(email.date);

  const starBtn = document.getElementById('ev-star-btn');
  starBtn.textContent = email.is_starred ? '★ Marcado' : '☆ Marcar';
  starBtn.style.color = email.is_starred ? 'var(--amber)' : '';

  const isSpam  = /spam|junk/i.test(email.folder || '');
  const spamBtn = document.getElementById('ev-spam-btn');
  spamBtn.textContent    = isSpam ? '✓ Não spam' : '⚠ Spam';
  spamBtn.dataset.isSpam = isSpam ? '1' : '0';
  document.getElementById('ev-junk-banner').classList.toggle('visible', isSpam);

  const attDiv = document.getElementById('ev-attachments');
  if (email.attachments?.length) {
    attDiv.style.display = 'flex';
    attDiv.innerHTML = email.attachments.map(a =>
      `<a class="att-chip" href="/api/attachments/${a.id}" target="_blank">📎 ${esc(a.filename)} <span class="att-size">${fmtSize(a.size)}</span></a>`
    ).join('');
  } else {
    attDiv.style.display = 'none';
    attDiv.innerHTML = '';
  }

  const threadBtn = document.getElementById('ev-thread-btn');
  if (threadBtn) {
    if (email.thread_id) {
      threadBtn.style.display = '';
      threadBtn.onclick = () => openThread(email);
    } else {
      threadBtn.style.display = 'none';
    }
  }

  _renderEmailBody();
  _updateViewButtons();
}

// ── Thread ────────────────────────────────────────────────────────────────────

async function openThread(email) {
  if (!email.thread_id) { openEmail(email.id); return; }
  const params = new URLSearchParams({ account_id: state.selectedAccountId });
  const data   = await (await api(`/api/threads/${encodeURIComponent(email.thread_id)}?${params}`)).json();
  if (!data.length)  { openEmail(email.id); return; }
  if (data.length === 1) { openEmail(data[0].id); return; }

  state.currentThread = data;
  state.threadOpen    = true;

  document.getElementById('reading-empty').style.display = 'none';
  document.getElementById('email-view').style.display    = 'flex';

  document.getElementById('ev-subject').textContent   = `💬 ${data[0].subject} (${data.length})`;
  document.getElementById('ev-avatar').textContent    = '💬';
  document.getElementById('ev-from-name').textContent = `${data.length} mensagens nesta conversa`;
  document.getElementById('ev-from-addr').textContent = '';
  document.getElementById('ev-to-line').textContent   = '';
  document.getElementById('ev-date-meta').textContent = fmtDateFull(data[data.length - 1].date);
  document.getElementById('ev-junk-banner').classList.remove('visible');
  document.getElementById('ev-attachments').style.display = 'none';

  const bodyHtml = document.getElementById('ev-body-html');
  const bodyText = document.getElementById('ev-body-text');
  bodyText.style.display = 'none';
  bodyHtml.style.display = 'block';
  bodyHtml.innerHTML = data.map((e, i) => `
    <div class="thread-msg ${i === data.length - 1 ? 'thread-msg-last' : ''}">
      <div class="thread-msg-header" onclick="this.parentElement.classList.toggle('collapsed')">
        <span class="thread-msg-avatar">${(e.from_name || e.from_addr || '?')[0].toUpperCase()}</span>
        <span class="thread-msg-from">${esc(e.from_name || e.from_addr)}</span>
        <span class="thread-msg-date">${fmtDateFull(e.date)}</span>
        <span class="thread-msg-toggle">▾</span>
      </div>
      <div class="thread-msg-body">
        ${e.body_html
          ? e.body_html.replace(/<script[\s\S]*?<\/script>/gi, '').replace(/on\w+\s*=\s*["'][^"']*["']/gi, '')
          : `<pre style="white-space:pre-wrap;font-size:12px">${esc(e.body_text || '')}</pre>`}
      </div>
    </div>
  `).join('');

  for (const e of data) {
    if (!e.is_read) {
      api(`/api/emails/${e.id}/read`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ read: true }),
      });
    }
  }
}

// ── Render body ───────────────────────────────────────────────────────────────

function _renderEmailBody() {
  const email    = state.currentEmail; if (!email) return;
  const bodyHtml = document.getElementById('ev-body-html');
  const bodyText = document.getElementById('ev-body-text');

  if (state.emailViewMode === 'html' && email.body_html) {
    bodyHtml.style.display = 'block'; bodyText.style.display = 'none';
    // body_html já vem sanitizado do servidor (nh3) — aqui só aplicamos o
    // bloqueio de imagens remotas antes de renderizar num iframe isolado
    // (sandbox sem allow-scripts/allow-same-origin: nada do e-mail executa).
    let html = email.body_html;
    if (!state.emailShowImages) {
      html = html.replace(/<img([^>]*?)\ssrc=(["'])(?!data:)([^"']+)\2/gi,
        (m, attrs, q, src) => `<img${attrs} data-src="${src}" src="" style="display:none" title="🖼 Imagem bloqueada">`);
    }
    const srcdoc = `<!DOCTYPE html><html><head><meta charset="UTF-8"><base target="_blank">
      <style>body{font-family:sans-serif;color:#111;background:#fff;margin:0;padding:12px;
      word-wrap:break-word;overflow-wrap:break-word}img{max-width:100%}</style></head>
      <body>${html}</body></html>`;
    let iframe = document.getElementById('ev-body-iframe');
    if (!iframe) {
      iframe = document.createElement('iframe');
      iframe.id = 'ev-body-iframe';
      iframe.setAttribute('sandbox', 'allow-popups');
      iframe.style.cssText = 'width:100%;border:0;display:block;height:70vh;background:#fff;border-radius:var(--radius);';
      bodyHtml.innerHTML = '';
      bodyHtml.appendChild(iframe);
    }
    iframe.srcdoc = srcdoc;
  } else {
    bodyHtml.style.display = 'none'; bodyText.style.display = 'block';
    bodyText.textContent = email.body_text || '';
  }
}

function _updateViewButtons() {
  const imgBtn  = document.getElementById('ev-img-btn');
  const htmlBtn = document.getElementById('ev-html-btn');
  const txtBtn  = document.getElementById('ev-txt-btn');
  if (imgBtn) imgBtn.textContent = state.emailShowImages ? '🖼 Imgs: ON' : '🖼 Imgs: OFF';
  imgBtn?.classList.toggle('active-toggle', state.emailShowImages);
  htmlBtn?.classList.toggle('active-toggle', state.emailViewMode === 'html');
  txtBtn?.classList.toggle('active-toggle',  state.emailViewMode === 'text');
}

function toggleEmailImages() { state.emailShowImages = !state.emailShowImages; _renderEmailBody(); _updateViewButtons(); }
function setEmailViewMode(m) { state.emailViewMode = m; _renderEmailBody(); _updateViewButtons(); }

// ── Ações no viewer ───────────────────────────────────────────────────────────

function hideEmail() {
  document.getElementById('reading-empty').style.display = 'flex';
  document.getElementById('email-view').style.display    = 'none';
  document.getElementById('ev-junk-banner').classList.remove('visible');
  state.currentEmail = null;
  state.threadOpen   = false;
}

function closeMobileViewer() {
  document.getElementById('reading-pane').classList.remove('active-mobile');
  document.getElementById('email-list-panel').classList.remove('hidden-mobile');
  hideEmail();
  state.selectedEmailId = null;
}

async function toggleStar() {
  const id = state.selectedEmailId; if (!id) return;
  const res = await api(`/api/emails/${id}/star`, { method: 'POST' });
  if (!res.ok) { toast('Erro ao atualizar estrela no servidor', 'error'); return; }
  const data = await res.json();
  const btn  = document.getElementById('ev-star-btn');
  btn.textContent = data.starred ? '★ Marcado' : '☆ Marcar';
  btn.style.color = data.starred ? 'var(--amber)' : '';
  const le = state.emails.find(e => e.id === id);
  if (le) { le.is_starred = data.starred; _patchEmailItem(id, { is_starred: data.starred }); }
}

// Ações servidor-primeiro (Fase 2.6): aplica otimista, mas desfaz se a API
// devolver erro — nunca confia que a chamada em background deu certo.

async function markUnread() {
  const id = state.selectedEmailId; if (!id) return;
  const le = state.emails.find(e => e.id === id);
  const wasRead = le ? le.is_read : undefined;
  if (le) { le.is_read = false; _patchEmailItem(id, { is_read: false }); }
  hideEmail(); state.selectedEmailId = null;
  refreshFolderCounts();
  try {
    const res = await api(`/api/emails/${id}/read`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ read: false }),
    });
    if (!res.ok) throw new Error('servidor rejeitou');
    toast('Marcado como não lido', 'success');
  } catch (e) {
    if (le) { le.is_read = wasRead; _patchEmailItem(id, { is_read: wasRead }); }
    refreshFolderCounts();
    toast('Erro ao marcar como não lido no servidor — desfeito', 'error');
  }
}

async function toggleSpam() {
  const id = state.selectedEmailId; if (!id) return;
  const btn      = document.getElementById('ev-spam-btn');
  const isSpam   = btn.dataset.isSpam === '1';
  const le       = state.emails.find(e => e.id === id);
  const snapshot = le ? { ...le } : null;
  state.emails = state.emails.filter(e => e.id !== id);
  state.selectedEmailId = null;
  hideEmail(); renderEmailList(); refreshFolderCounts();
  try {
    const res = await api(`/api/emails/${id}/spam`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ spam: !isSpam }),
    });
    if (!res.ok) throw new Error('servidor rejeitou');
    toast(isSpam ? 'Removido do spam' : 'Marcado como spam', 'success');
  } catch (e) {
    if (snapshot) { state.emails.push(snapshot); renderEmailList(); refreshFolderCounts(); }
    toast('Erro ao atualizar no servidor — desfeito', 'error');
  }
}

async function deleteEmail() {
  if (!state.selectedEmailId) return;
  if (!confirm('Excluir este e-mail?')) return;
  const id = state.selectedEmailId;
  try {
    const res  = await api(`/api/emails/${id}`, { method: 'DELETE' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { toast(data.error || 'Erro ao excluir', 'error'); return; }
    state.emails = state.emails.filter(e => e.id !== id);
    state.selectedEmailId = null;
    hideEmail(); renderEmailList(); refreshFolderCounts();
    toast(data.warning || 'E-mail excluído', data.warning ? 'info' : 'success');
  } catch(e) {
    toast('Erro ao excluir', 'error');
  }
}

// ── Ações de composição a partir do viewer ────────────────────────────────────

function _quoteBody(e) {
  return `\n\n--- Mensagem original ---\nDe: ${e.from_addr}\nData: ${fmtDateFull(e.date)}\n\n${e.body_text || ''}`;
}

function replyEmail() {
  const e = state.currentEmail; if (!e) return;
  openCompose({
    to: e.reply_to || e.from_addr,
    subject: e.subject.startsWith('Re:') ? e.subject : `Re: ${e.subject}`,
    body: _quoteBody(e), replyToId: e.id, title: 'Responder',
  });
}

function replyAll() {
  const e = state.currentEmail; if (!e) return;
  openCompose({
    to: e.reply_to || e.from_addr,
    cc: [e.to_addr, e.cc_addr].filter(Boolean).join(', '),
    subject: e.subject.startsWith('Re:') ? e.subject : `Re: ${e.subject}`,
    body: _quoteBody(e), replyToId: e.id, title: 'Responder a todos',
  });
}

function forwardEmail() {
  const e = state.currentEmail; if (!e) return;
  openCompose({
    subject: e.subject.startsWith('Fwd:') ? e.subject : `Fwd: ${e.subject}`,
    body: `\n\n--- Mensagem encaminhada ---\nDe: ${e.from_addr}\nPara: ${e.to_addr}\nData: ${fmtDateFull(e.date)}\nAssunto: ${e.subject}\n\n${e.body_text || ''}`,
    title: 'Encaminhar',
  });
}

function forwardAsAttachment() {
  const e = state.currentEmail; if (!e) return;
  openCompose({
    subject: `Fwd: ${e.subject}`,
    body: `E-mail encaminhado.\n\nOriginal de: ${e.from_addr}\nAssunto: ${e.subject}`,
    title: 'Encaminhar como anexo',
  });
  toast('Anexe o .eml manualmente se necessário', 'info');
}

function editAsNew() {
  const e = state.currentEmail; if (!e) return;
  openCompose({ to: e.to_addr, cc: e.cc_addr, subject: e.subject, body: e.body_text || '', title: 'Editar como nova mensagem' });
}

function printEmail() {
  const e = state.currentEmail; if (!e) return;
  const win  = window.open('', '_blank');
  const body = e.body_html
    ? e.body_html.replace(/<script[\s\S]*?<\/script>/gi, '')
    : `<pre style="white-space:pre-wrap">${esc(e.body_text || '')}</pre>`;
  win.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>${esc(e.subject)}</title>
    <style>body{font-family:sans-serif;max-width:800px;margin:20px auto}.h{border-bottom:1px solid #ccc;margin-bottom:16px;padding-bottom:12px}h1{font-size:16px}.m{font-size:12px;color:#555}@media print{body{margin:0}}</style>
    </head><body><div class="h"><h1>${esc(e.subject)}</h1><div class="m">De: ${esc(e.from_name||e.from_addr)}</div><div class="m">Para: ${esc(e.to_addr)}</div><div class="m">Data: ${fmtDateFull(e.date)}</div></div>${body}</body></html>`);
  win.document.close(); win.focus(); setTimeout(() => win.print(), 400);
}