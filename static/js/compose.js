// compose.js — modal de composição grande (Fase 5.2): maximizar/minimizar,
// Cc/Cco recolhidos, autocomplete de destinatários, drag-drop de anexos,
// rascunho automático local (localStorage — ver nota no commit sobre por
// que não grava no IMAP a cada 10s).

let composeMode = 'plain';
const MOBILE_BREAKPOINT = 768;
const MAX_ATTACH_BYTES  = 25 * 1024 * 1024; // mesmo limite de app.py (MAX_CONTENT_LENGTH)
const DRAFT_STORAGE_KEY = 'correo_draft';
const DRAFT_AUTOSAVE_MS = 10000;

let _draftTimer = null;
let _suppressDraftOnClose = false; // true depois de enviar com sucesso

// ── Abrir / fechar ────────────────────────────────────────────────────────────

function openCompose(opts = {}) {
  state.replyToId  = opts.replyToId || null;
  state.attachments = [];
  _suppressDraftOnClose = false;

  document.getElementById('compose-to').value      = opts.to || '';
  document.getElementById('compose-cc').value      = opts.cc || '';
  document.getElementById('compose-bcc').value     = opts.bcc || '';
  document.getElementById('compose-subject').value = opts.subject || '';
  document.getElementById('compose-body').value    = opts.body || '';
  document.getElementById('compose-attach-list').innerHTML = '';
  document.getElementById('compose-title').textContent    = opts.title || 'Nova mensagem';

  // Reset para modo texto (quem chama com body_html usa setComposeMode depois)
  composeMode = 'plain';
  document.getElementById('compose-body-wrap').style.display = '';
  document.getElementById('compose-html-wrap').style.display = 'none';
  document.getElementById('tab-plain').classList.add('active');
  document.getElementById('tab-html').classList.remove('active');
  document.getElementById('compose-editor').innerHTML = '';

  // Cc/Cco: só mostra recolhido se não vier nada pra lá
  _setCcBccVisible(!!(opts.cc || opts.bcc));

  // De: conta da mensagem original (responder/encaminhar) ou a primeira
  const sel = document.getElementById('compose-account-select');
  if (opts.accountId && [...sel.options].some(o => o.value === String(opts.accountId))) {
    sel.value = String(opts.accountId);
  } else if (sel.options.length) {
    sel.selectedIndex = 0;
  }

  const modal = document.getElementById('compose-modal');
  modal.classList.remove('minimized');
  modal.classList.add('open');
  document.getElementById('compose-box').classList.toggle(
    'maximized', localStorage.getItem('correo_compose_maximized') === '1');
  _updateMaximizeButton();
  setTimeout(() => document.getElementById('compose-to').focus(), 50);

  _startDraftAutosave();
}

function closeCompose() {
  if (!document.getElementById('compose-modal').classList.contains('open')) return;
  if (!_suppressDraftOnClose && _hasDraftContent()) {
    if (!confirm('Fechar sem enviar? O rascunho fica salvo e será recuperado na próxima vez que você abrir o correo.')) {
      return;
    }
    _saveDraftLocally();
  } else if (!_suppressDraftOnClose) {
    _clearLocalDraft();
  }
  _stopDraftAutosave();
  const modal = document.getElementById('compose-modal');
  modal.classList.remove('open', 'minimized');
  state.replyToId   = null;
  state.attachments = [];
}

// Descarta o rascunho de propósito — diferente de closeCompose(), nunca
// pergunta nem regrava no localStorage (é o único jeito de um rascunho de
// teste parar de voltar sozinho a cada abertura do correo via resumeDraftIfAny()).
function discardCompose() {
  if (_hasDraftContent() && !confirm('Descartar este rascunho? Isso não pode ser desfeito.')) {
    return;
  }
  _suppressDraftOnClose = true;
  _clearLocalDraft();
  closeCompose();
}

// ── Maximizar / minimizar ──────────────────────────────────────────────────────

function toggleComposeMaximize() {
  const modal = document.getElementById('compose-modal');
  if (modal.classList.contains('minimized')) {
    // Minimizado, os botões de maximizar/minimizar só servem pra "trazer de
    // volta" — não faz sentido também trocar o tamanho que tava antes de
    // minimizar (bug real: clicar em maximizar minimizado não tinha efeito
    // nenhum, porque a regra CSS .minimized tem mais especificidade que
    // .maximized e ignorava a troca de classe).
    restoreComposeIfMinimized();
    return;
  }
  const box = document.getElementById('compose-box');
  const maximized = box.classList.toggle('maximized');
  try { localStorage.setItem('correo_compose_maximized', maximized ? '1' : '0'); } catch (e) {}
  _updateMaximizeButton();
}

function _updateMaximizeButton() {
  const btn = document.getElementById('compose-maximize-btn');
  if (!btn) return;
  const maximized = document.getElementById('compose-box').classList.contains('maximized');
  btn.textContent = maximized ? '🗗' : '🗖';
  btn.title = maximized ? 'Restaurar' : 'Maximizar';
}

function minimizeCompose() {
  if (window.innerWidth < MOBILE_BREAKPOINT) return; // mobile é sempre tela cheia
  document.getElementById('compose-modal').classList.add('minimized');
}

function restoreComposeIfMinimized() {
  const modal = document.getElementById('compose-modal');
  if (modal.classList.contains('minimized')) modal.classList.remove('minimized');
}

// ── Cc / Cco ───────────────────────────────────────────────────────────────────

function toggleCcBcc() {
  const wrap = document.getElementById('compose-ccbcc-wrap');
  _setCcBccVisible(wrap.style.display === 'none');
}

function _setCcBccVisible(visible) {
  document.getElementById('compose-ccbcc-wrap').style.display = visible ? 'flex' : 'none';
}

// ── Modo HTML / Plain ─────────────────────────────────────────────────────────

function setComposeMode(mode) {
  composeMode = mode;
  const plainWrap = document.getElementById('compose-body-wrap');
  const htmlWrap  = document.getElementById('compose-html-wrap');
  const tabPlain  = document.getElementById('tab-plain');
  const tabHtml   = document.getElementById('tab-html');

  if (mode === 'html') {
    const text = document.getElementById('compose-body').value;
    if (text && !document.getElementById('compose-editor').innerHTML)
      document.getElementById('compose-editor').innerText = text;
    plainWrap.style.display = 'none';
    htmlWrap.style.display  = 'flex';
    tabPlain.classList.remove('active');
    tabHtml.classList.add('active');
    setTimeout(() => document.getElementById('compose-editor').focus(), 50);
  } else {
    const html = document.getElementById('compose-editor').innerHTML;
    if (html) {
      const tmp = document.createElement('div');
      tmp.innerHTML = html;
      document.getElementById('compose-body').value = tmp.innerText;
    }
    plainWrap.style.display = '';
    htmlWrap.style.display  = 'none';
    tabPlain.classList.add('active');
    tabHtml.classList.remove('active');
  }
}

function execCmd(cmd, value = null) {
  document.getElementById('compose-editor').focus();
  document.execCommand(cmd, false, value);
}

function insertLink() {
  const url = prompt('URL do link:');
  if (url) execCmd('createLink', url);
}

// ── Anexos (botão + arrastar e soltar) ─────────────────────────────────────────

function addAttachments(input) {
  _addFiles(input.files);
  input.value = '';
}

function _addFiles(fileList) {
  for (const f of fileList) state.attachments.push(f);
  renderAttachList();
}

function _totalAttachBytes() {
  return state.attachments.reduce((s, f) => s + f.size, 0);
}

function renderAttachList() {
  document.getElementById('compose-attach-list').innerHTML =
    state.attachments.map((f, i) =>
      `<span class="compose-att">${esc(f.name)} <span class="compose-att-size">${fmtSize(f.size)}</span><span class="compose-att-remove" onclick="removeAtt(${i})">×</span></span>`
    ).join('');
}

function removeAtt(i) { state.attachments.splice(i, 1); renderAttachList(); }

function _setupComposeDragDrop() {
  const box = document.getElementById('compose-box');
  if (!box || box.dataset.dndReady) return;
  box.dataset.dndReady = '1';
  ['dragenter', 'dragover'].forEach(evt => box.addEventListener(evt, e => {
    e.preventDefault(); e.stopPropagation(); box.classList.add('drag-over');
  }));
  ['dragleave', 'drop'].forEach(evt => box.addEventListener(evt, e => {
    e.preventDefault(); e.stopPropagation(); box.classList.remove('drag-over');
  }));
  box.addEventListener('drop', e => {
    if (e.dataTransfer?.files?.length) _addFiles(e.dataTransfer.files);
  });
}

// ── Autocomplete de destinatários ──────────────────────────────────────────────

function _attachRecipientAutocomplete(input) {
  if (!input || input.dataset.acReady) return;
  input.dataset.acReady = '1';
  let box = null, items = [], activeIndex = -1, searchTimer = null;

  function closeBox() { if (box) { box.remove(); box = null; } }

  function currentToken() {
    const upToCursor = input.value.slice(0, input.selectionStart);
    const parts = upToCursor.split(',');
    return parts[parts.length - 1].trim();
  }

  function replaceToken(email) {
    const val = input.value;
    const cursor = input.selectionStart;
    const before = val.slice(0, cursor);
    const after  = val.slice(cursor);
    const lastComma = before.lastIndexOf(',');
    const newBefore = (lastComma >= 0 ? before.slice(0, lastComma + 1) + ' ' : '') + email + ', ';
    input.value = newBefore + after.replace(/^\s*/, '');
    const pos = newBefore.length;
    input.setSelectionRange(pos, pos);
  }

  function renderBox() {
    closeBox();
    if (!items.length) return;
    box = document.createElement('div');
    box.className = 'recipient-suggest';
    const rect = input.getBoundingClientRect();
    box.style.position = 'fixed';
    box.style.left  = `${rect.left}px`;
    box.style.top   = `${rect.bottom + 2}px`;
    box.style.width = `${Math.max(rect.width, 220)}px`;
    box.innerHTML = items.map((it, i) =>
      `<div class="recipient-suggest-item" data-i="${i}">${esc(it.name ? `${it.name} <${it.email}>` : it.email)}</div>`
    ).join('');
    box.querySelectorAll('.recipient-suggest-item').forEach(el => {
      el.onmousedown = e => {
        e.preventDefault();
        replaceToken(items[+el.dataset.i].email);
        closeBox(); input.focus();
      };
    });
    document.body.appendChild(box);
    activeIndex = -1;
  }

  function highlight() {
    if (!box) return;
    box.querySelectorAll('.recipient-suggest-item').forEach((el, i) =>
      el.classList.toggle('active', i === activeIndex));
  }

  async function search() {
    const token = currentToken();
    if (token.length < 2) { closeBox(); return; }
    try {
      const res = await api(`/api/contacts?q=${encodeURIComponent(token)}`);
      items = await res.json();
    } catch (e) { items = []; }
    renderBox();
  }

  input.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(search, 150);
  });
  input.addEventListener('blur', () => setTimeout(closeBox, 150));
  input.addEventListener('keydown', e => {
    if (!box) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); activeIndex = Math.min(activeIndex + 1, items.length - 1); highlight(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); activeIndex = Math.max(activeIndex - 1, 0); highlight(); }
    else if (e.key === 'Enter' && activeIndex >= 0) { e.preventDefault(); replaceToken(items[activeIndex].email); closeBox(); }
    else if (e.key === 'Escape') closeBox();
  });
}

function _setupComposeAutocomplete() {
  ['compose-to', 'compose-cc', 'compose-bcc'].forEach(id =>
    _attachRecipientAutocomplete(document.getElementById(id)));
}

// ── Rascunho automático local ──────────────────────────────────────────────────
// Salva no localStorage (não no IMAP) a cada 10s e ao fechar sem enviar.
// Decisão deliberada: gravar via APPEND no servidor a cada 10s bombardearia
// contas no mail.soft.dev.br (fail2ban agressivo) com escrita repetida —
// fica pra uma versão futura que faça isso só ao fechar, não em intervalo.

function _hasDraftContent() {
  return !!(
    document.getElementById('compose-to').value.trim() ||
    document.getElementById('compose-subject').value.trim() ||
    document.getElementById('compose-body').value.trim() ||
    document.getElementById('compose-editor').innerHTML.trim()
  );
}

function _saveDraftLocally() {
  if (!_hasDraftContent()) return;
  const draft = {
    to:        document.getElementById('compose-to').value,
    cc:        document.getElementById('compose-cc').value,
    bcc:       document.getElementById('compose-bcc').value,
    subject:   document.getElementById('compose-subject').value,
    mode:      composeMode,
    body:      composeMode === 'html'
      ? document.getElementById('compose-editor').innerHTML
      : document.getElementById('compose-body').value,
    accountId: document.getElementById('compose-account-select').value,
    replyToId: state.replyToId,
    title:     document.getElementById('compose-title').textContent,
    savedAt:   Date.now(),
  };
  try { localStorage.setItem(DRAFT_STORAGE_KEY, JSON.stringify(draft)); } catch (e) {}
}

function _clearLocalDraft() {
  try { localStorage.removeItem(DRAFT_STORAGE_KEY); } catch (e) {}
}

function _startDraftAutosave() {
  _stopDraftAutosave();
  _draftTimer = setInterval(_saveDraftLocally, DRAFT_AUTOSAVE_MS);
}

function _stopDraftAutosave() {
  if (_draftTimer) { clearInterval(_draftTimer); _draftTimer = null; }
}

function resumeDraftIfAny() {
  let draft;
  try { draft = JSON.parse(localStorage.getItem(DRAFT_STORAGE_KEY) || 'null'); } catch (e) { return; }
  if (!draft) return;

  openCompose({
    to: draft.to, cc: draft.cc, bcc: draft.bcc, subject: draft.subject,
    body: draft.mode === 'html' ? '' : draft.body,
    replyToId: draft.replyToId, accountId: draft.accountId,
    title: draft.title || 'Rascunho recuperado',
  });
  if (draft.mode === 'html') {
    setComposeMode('html');
    document.getElementById('compose-editor').innerHTML = draft.body || '';
  }
  toast('Rascunho recuperado — continue de onde parou', 'info');
}

// ── Envio ─────────────────────────────────────────────────────────────────────

async function sendEmail() {
  if (_totalAttachBytes() > MAX_ATTACH_BYTES) {
    toast(`Anexos somam ${fmtSize(_totalAttachBytes())} — o limite é ${fmtSize(MAX_ATTACH_BYTES)}`, 'error');
    return;
  }

  const btn = document.getElementById('compose-send-btn');
  btn.disabled = true; btn.textContent = 'Enviando...';

  const fd = new FormData();
  fd.append('account_id', document.getElementById('compose-account-select').value);
  fd.append('to',      document.getElementById('compose-to').value);
  fd.append('cc',      document.getElementById('compose-cc').value);
  fd.append('bcc',     document.getElementById('compose-bcc').value);
  fd.append('subject', document.getElementById('compose-subject').value);

  if (composeMode === 'html') {
    const html = document.getElementById('compose-editor').innerHTML;
    const tmp  = document.createElement('div'); tmp.innerHTML = html;
    fd.append('body', tmp.innerText);
    fd.append('body_html', html);
  } else {
    fd.append('body', document.getElementById('compose-body').value);
  }

  if (state.replyToId) fd.append('reply_to_id', state.replyToId);
  for (const f of state.attachments) fd.append('attachments', f);

  try {
    const data = await (await api('/api/emails/send', { method: 'POST', body: fd })).json();
    if (data.ok) {
      toast('E-mail enviado!', 'success');
      _clearLocalDraft();
      _suppressDraftOnClose = true;
      closeCompose();
    } else {
      toast(`Erro: ${data.error}`, 'error');
    }
  } catch(e) {
    toast('Erro ao enviar', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Enviar';
  }
}
