// ── State ──────────────────────────────────────────────────────────────────────
const state = {
  accounts: [],
  selectedAccountId: null,
  selectedFolder: 'INBOX',
  emails: [],
  selectedEmailId: null,
  currentPage: 1,
  totalPages: 1,
  searchQuery: '',
  replyToId: null,
  editAccountId: null,
  attachments: [],
  selectedIds: new Set(),
  // per-email view state
  emailShowImages: false,
  emailViewMode: 'html', // 'html' | 'text'
};

// ── Settings (persistidas no localStorage) ─────────────────────────────────────
const settings = {
  theme: 'dark',
  layout: 'vertical',
  showImagesByDefault: false,
  autoSync: 0,
};

function loadSettings() {
  try { Object.assign(settings, JSON.parse(localStorage.getItem('correo_settings') || '{}')); } catch(e) {}
  applyTheme();
  applyLayout();
  updateSettingsUI();
  if (settings.autoSync > 0) startAutoSync();
}

function saveSettings() {
  localStorage.setItem('correo_settings', JSON.stringify(settings));
}

// ── Theme ──────────────────────────────────────────────────────────────────────
function applyTheme() {
  document.documentElement.setAttribute('data-theme', settings.theme);
  const btn = document.getElementById('toggle-theme');
  if (btn) btn.textContent = settings.theme === 'dark' ? '☀ Claro' : '🌙 Escuro';
}

function toggleTheme() {
  settings.theme = settings.theme === 'dark' ? 'light' : 'dark';
  saveSettings(); applyTheme();
}

// ── Layout ─────────────────────────────────────────────────────────────────────
function applyLayout() {
  const app = document.getElementById('app');
  app.setAttribute('data-layout', settings.layout);
  const btn = document.getElementById('toggle-layout');
  if (btn) btn.textContent = settings.layout === 'vertical' ? '⇔ Horizontal' : '⇕ Vertical';
}

function toggleLayout() {
  settings.layout = settings.layout === 'vertical' ? 'horizontal' : 'vertical';
  saveSettings(); applyLayout();
  // Re-abre email no painel correto se houver um aberto
  if (state.selectedEmailId) openEmail(state.selectedEmailId);
}

// ── Auto-sync ──────────────────────────────────────────────────────────────────
let autoSyncTimer = null;

function startAutoSync() {
  stopAutoSync();
  if (settings.autoSync <= 0) return;
  autoSyncTimer = setInterval(() => {
    fetch('/api/sync/all', { method: 'POST' });
  }, settings.autoSync * 60 * 1000);
}

function stopAutoSync() {
  if (autoSyncTimer) { clearInterval(autoSyncTimer); autoSyncTimer = null; }
}

function setAutoSync(val) {
  settings.autoSync = parseInt(val);
  saveSettings();
  if (settings.autoSync > 0) { startAutoSync(); toast(`Auto-sync a cada ${settings.autoSync} min`, 'success'); }
  else { stopAutoSync(); toast('Auto-sync desativado', 'info'); }
}

function updateSettingsUI() {
  applyTheme(); applyLayout();
  const sel = document.getElementById('autosync-select');
  if (sel) sel.value = settings.autoSync;
}

// ── Socket.IO ──────────────────────────────────────────────────────────────────
const socket = io();
socket.on('sync_start', d => toast(`↺ Sincronizando ${d.email}...`, 'info'));
socket.on('sync_done',  d => { toast(`✓ ${d.email} sincronizado`, 'success'); loadEmails(); updateFolderCounts(); });
socket.on('sync_error', d => toast(`Erro: ${d.error}`, 'error'));
socket.on('new_email',  d => { if (d.account_id === state.selectedAccountId || !state.selectedAccountId) { loadEmails(); updateFolderCounts(); } });

// ── Init ───────────────────────────────────────────────────────────────────────
async function init() {
  loadSettings();
  await loadAccounts();
  await loadEmails();
  updateFolderCounts();
}

// ── Accounts ───────────────────────────────────────────────────────────────────
async function loadAccounts() {
  const res = await fetch('/api/accounts');
  state.accounts = await res.json();
  renderAccountSelect(); renderComposeAccountSelect(); renderAccountList();
}

function renderAccountSelect() {
  const sel = document.getElementById('account-select');
  const cur = sel.value;
  sel.innerHTML = '<option value="">Todas as contas</option>';
  for (const a of state.accounts) {
    const opt = document.createElement('option');
    opt.value = a.id; opt.textContent = a.email; sel.appendChild(opt);
  }
  sel.value = cur;
}

function renderComposeAccountSelect() {
  const sel = document.getElementById('compose-account-select');
  sel.innerHTML = '';
  for (const a of state.accounts) {
    const opt = document.createElement('option');
    opt.value = a.id; opt.textContent = `${a.name} <${a.email}>`; sel.appendChild(opt);
  }
}

function onAccountChange() {
  const v = document.getElementById('account-select').value;
  state.selectedAccountId = v ? parseInt(v) : null;
  state.selectedFolder = 'INBOX';
  state.selectedEmailId = null;
  state.currentPage = 1;
  state.selectedIds.clear();
  loadEmails(); updateFolderCounts(); renderFolders(); hideEmail();
}

// ── Folders ────────────────────────────────────────────────────────────────────
function isSpamFolder(name) {
  const k = (name || '').toLowerCase();
  return k.includes('spam') || k.includes('junk');
}

const FOLDER_LABELS   = { INBOX: 'Entrada', Sent: 'Enviados', Drafts: 'Rascunhos', Spam: 'Spam', Trash: 'Lixeira' };
const FOLDER_ICONS_MAP = { INBOX: '📥', Sent: '📤', Drafts: '📝', Spam: '🚫', Trash: '🗑' };
const VIRTUAL_ORDER   = ['INBOX', 'Sent', 'Drafts', 'Spam', 'Trash'];

function folderVirtualName(real) {
  const k = real.toLowerCase();
  if (k === 'inbox') return 'INBOX';
  if (k.includes('sent')) return 'Sent';
  if (k.includes('draft')) return 'Drafts';
  if (isSpamFolder(k)) return 'Spam';
  if (k.includes('trash')) return 'Trash';
  return real;
}

function folderDisplayLabel(vname) { return FOLDER_LABELS[vname] || vname; }
function folderDisplayIcon(vname) {
  if (FOLDER_ICONS_MAP[vname]) return FOLDER_ICONS_MAP[vname];
  if ((vname||'').toLowerCase().includes('archive')) return '📦';
  return '📁';
}

let folderCounts = {};

async function updateFolderCounts() {
  const params = state.selectedAccountId ? `?account_id=${state.selectedAccountId}` : '';
  const res = await fetch(`/api/folders/counts${params}`);
  folderCounts = await res.json();
  renderFolders();
}

function renderFolders() {
  const container = document.getElementById('folders');
  const virtual = {};
  for (const [real, counts] of Object.entries(folderCounts)) {
    const vf = folderVirtualName(real);
    if (!virtual[vf]) virtual[vf] = { total: 0, unread: 0, reals: [] };
    virtual[vf].total += counts.total;
    virtual[vf].unread += counts.unread;
    virtual[vf].reals.push(real);
  }
  for (const vf of VIRTUAL_ORDER) {
    if (!virtual[vf]) virtual[vf] = { total: 0, unread: 0, reals: [] };
  }
  const ordered = [...VIRTUAL_ORDER];
  for (const vf of Object.keys(virtual)) {
    if (!ordered.includes(vf)) ordered.push(vf);
  }
  container.innerHTML = '';
  for (const vf of ordered) {
    const counts = virtual[vf];
    if (counts.total === 0 && !VIRTUAL_ORDER.includes(vf)) continue;
    const item = document.createElement('div');
    item.className = 'folder-item' + (state.selectedFolder === vf ? ' active' : '');
    item.innerHTML = `
      <span class="folder-icon">${folderDisplayIcon(vf)}</span>
      <span class="folder-name">${folderDisplayLabel(vf)}</span>
      ${counts.unread > 0 ? `<span class="folder-badge">${counts.unread}</span>` : ''}
    `;
    item.onclick = () => selectFolder(vf);
    container.appendChild(item);
  }
}

function realFoldersFor(virtualName) {
  const reals = Object.keys(folderCounts).filter(r => folderVirtualName(r) === virtualName);
  return reals.length ? reals : [virtualName];
}

function selectFolder(vf) {
  state.selectedFolder = vf;
  state.currentPage = 1;
  state.selectedEmailId = null;
  state.selectedIds.clear();
  hideEmail(); loadEmails(); renderFolders(); renderBulkBar();
}

// ── Email list ─────────────────────────────────────────────────────────────────
async function loadEmails() {
  const loading = document.getElementById('list-loading');
  const empty   = document.getElementById('list-empty');
  loading.style.display = 'flex'; empty.style.display = 'none';
  const folders = realFoldersFor(state.selectedFolder);
  try {
    let all = [];
    for (const folder of folders) {
      const params = new URLSearchParams({ folder, page: state.currentPage, per_page: 50 });
      if (state.selectedAccountId) params.set('account_id', state.selectedAccountId);
      if (state.searchQuery) params.set('search', state.searchQuery);
      const data = await (await fetch(`/api/emails?${params}`)).json();
      all = all.concat(data.emails);
    }
    all.sort((a, b) => new Date(b.date) - new Date(a.date));
    state.emails = all;
    renderEmailList();
  } catch(e) { toast('Erro ao carregar e-mails', 'error'); }
  finally { loading.style.display = 'none'; }
}

function renderEmailList() {
  const container = document.getElementById('email-list');
  const empty     = document.getElementById('list-empty');
  container.querySelectorAll('.email-item').forEach(e => e.remove());
  if (state.emails.length === 0) { empty.style.display = 'flex'; return; }
  empty.style.display = 'none';
  for (const email of state.emails) {
    const isSel = state.selectedIds.has(email.id);
    const item  = document.createElement('div');
    item.className = 'email-item'
      + (email.is_read ? '' : ' unread')
      + (email.id === state.selectedEmailId ? ' active' : '')
      + (isSel ? ' selected' : '');
    item.dataset.id = email.id;
    item.innerHTML = `
      <div class="ei-row1">
        <input type="checkbox" class="ei-checkbox" ${isSel?'checked':''} onclick="toggleSelect(event,${email.id})">
        <span class="ei-from">${esc(email.from_name || email.from_addr)}</span>
        <span class="ei-date">${fmtDate(email.date)}</span>
        <div class="ei-indicators">
          ${!email.is_read ? '<span class="dot-unread"></span>' : ''}
          ${email.is_starred ? '<span class="icon-star">★</span>' : ''}
          ${email.has_attachments ? '<span class="icon-attach">📎</span>' : ''}
        </div>
      </div>
      <div class="ei-subject">${esc(email.subject)}</div>
      <div class="ei-preview">${esc(email.preview)}</div>
    `;
    item.onclick = e => { if (e.target.classList.contains('ei-checkbox')) return; openEmail(email.id); };
    container.appendChild(item);
  }
  renderBulkBar();
}

// ── Selection ──────────────────────────────────────────────────────────────────
function toggleSelect(e, id) {
  e.stopPropagation();
  if (state.selectedIds.has(id)) state.selectedIds.delete(id); else state.selectedIds.add(id);
  document.querySelector(`.email-item[data-id="${id}"]`)?.classList.toggle('selected', state.selectedIds.has(id));
  renderBulkBar();
}
function selectAll()   { state.emails.forEach(e => state.selectedIds.add(e.id)); renderEmailList(); }
function deselectAll() { state.selectedIds.clear(); renderEmailList(); }
function renderBulkBar() {
  const bar = document.getElementById('bulk-bar');
  const n   = state.selectedIds.size;
  bar.style.display = n ? 'flex' : 'none';
  if (n) document.getElementById('bulk-count').textContent = `${n} selecionado(s)`;
}

// ── Bulk actions ───────────────────────────────────────────────────────────────
async function bulkAction(action) {
  const ids = [...state.selectedIds]; if (!ids.length) return;
  const L = { delete:'Excluir', mark_spam:'Marcar como spam', mark_read:'Marcar como lido', mark_unread:'Marcar como não lido', mark_not_spam:'Desmarcar spam' };
  if (['delete','mark_spam'].includes(action) && !confirm(`${L[action]} ${ids.length} e-mail(s)?`)) return;
  const data = await (await fetch('/api/emails/bulk', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ action, ids, folder: state.selectedFolder, account_id: state.selectedAccountId }),
  })).json();
  if (data.ok) {
    toast(`${L[action]}: ${data.count} e-mail(s)`, 'success');
    state.selectedIds.clear(); await loadEmails(); updateFolderCounts();
    if (['delete','mark_spam'].includes(action) && ids.includes(state.selectedEmailId)) { hideEmail(); state.selectedEmailId = null; }
  } else toast('Erro na operação', 'error');
}

async function bulkActionAll(action) {
  const L = { mark_read:'Marcar todos como lido', delete:'Excluir todos' };
  if (!confirm(`${L[action]} na pasta "${folderDisplayLabel(state.selectedFolder)}"?`)) return;
  for (const folder of realFoldersFor(state.selectedFolder)) {
    await fetch('/api/emails/bulk', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ action, ids:[], folder, account_id: state.selectedAccountId }) });
  }
  toast('Operação concluída', 'success');
  await loadEmails(); updateFolderCounts();
  if (action === 'delete') { hideEmail(); state.selectedEmailId = null; }
}

// ── Email view ─────────────────────────────────────────────────────────────────
function _getEmailContainer() {
  // No layout horizontal, usa o painel inline; no vertical, usa o painel lateral
  if (settings.layout === 'horizontal') {
    return {
      empty: document.getElementById('reading-empty-inline'),
      view:  document.getElementById('email-view-inline'),
    };
  }
  return {
    empty: document.getElementById('reading-empty'),
    view:  document.getElementById('email-view'),
  };
}

async function openEmail(id) {
  state.selectedEmailId = id;
  document.querySelectorAll('.email-item').forEach(el => el.classList.toggle('active', parseInt(el.dataset.id) === id));

  const email = await (await fetch(`/api/emails/${id}`)).json();

  // Marca lido na lista
  const le = state.emails.find(e => e.id === id);
  if (le) { le.is_read = true; renderEmailList(); }

  // Estado de visualização por email
  state.emailShowImages = settings.showImagesByDefault;
  state.emailViewMode   = email.body_html ? 'html' : 'text';

  // Esconde empty, mostra view — em ambos os contêineres (só o visível importa)
  document.getElementById('reading-empty').style.display = 'none';
  document.getElementById('email-view').style.display = 'flex';
  document.getElementById('reading-empty-inline').style.display = 'none';
  document.getElementById('email-view-inline').style.display = 'flex';

  // Preenche em ambos os contêineres (eles compartilham os mesmos IDs via função)
  _fillEmailView(email);

  // Guarda referência
  document.getElementById('email-view')._email = email;
  document.getElementById('email-view-inline')._email = email;
}

function _fillEmailView(email) {
  // Preenche todos os elementos de ambas as views (vertical e horizontal compartilham mesmos IDs via prefixo)
  ['', '-inline'].forEach(suffix => {
    const s = suffix;
    _setEl(`ev-subject${s}`, email.subject, 'text');
    _setEl(`ev-avatar${s}`, (email.from_name || email.from_addr || '?').slice(0,1).toUpperCase(), 'text');
    _setEl(`ev-from-name${s}`, email.from_name || email.from_addr, 'text');
    _setEl(`ev-from-addr${s}`, `<${email.from_addr}>`, 'text');
    _setEl(`ev-to-line${s}`, email.to_addr ? `Para: ${email.to_addr}` : '', 'text');
    _setEl(`ev-date${s}`, fmtDateFull(email.date), 'text');

    // Star
    const starBtn = document.getElementById(`ev-star-btn${s}`);
    if (starBtn) { starBtn.textContent = email.is_starred ? '★ Marcado' : '☆ Marcar'; starBtn.style.color = email.is_starred ? 'var(--amber)' : ''; }

    // Spam
    const spamBtn = document.getElementById(`ev-spam-btn${s}`);
    const isSpam  = isSpamFolder(email.folder || '');
    if (spamBtn) { spamBtn.textContent = isSpam ? '✓ Não spam' : '⚠ Spam'; spamBtn.dataset.isSpam = isSpam ? '1' : '0'; }

    // Botões de view
    _updateViewButtons(s);

    // Attachments
    const attDiv = document.getElementById(`ev-attachments${s}`);
    if (attDiv) {
      if (email.attachments && email.attachments.length > 0) {
        attDiv.style.display = 'flex';
        attDiv.innerHTML = email.attachments.map(a =>
          `<a class="att-chip" href="/api/attachments/${a.id}" target="_blank">
            <span class="att-icon">📎</span><span>${esc(a.filename)}</span>
            <span class="att-size">${fmtSize(a.size)}</span></a>`
        ).join('');
      } else { attDiv.style.display = 'none'; attDiv.innerHTML = ''; }
    }

    // Body
    _renderBody(email, s);
  });
}

function _setEl(id, val, type) {
  const el = document.getElementById(id);
  if (!el) return;
  if (type === 'text') el.textContent = val;
  else el.innerHTML = val;
}

function _updateViewButtons(suffix) {
  const imgBtn  = document.getElementById(`ev-img-btn${suffix}`);
  const htmlBtn = document.getElementById(`ev-html-btn${suffix}`);
  const txtBtn  = document.getElementById(`ev-txt-btn${suffix}`);
  if (imgBtn)  imgBtn.textContent  = state.emailShowImages ? '🖼 Imagens: ON' : '🖼 Imagens: OFF';
  if (imgBtn)  imgBtn.classList.toggle('active-toggle', state.emailShowImages);
  if (htmlBtn) htmlBtn.classList.toggle('active-toggle', state.emailViewMode === 'html');
  if (txtBtn)  txtBtn.classList.toggle('active-toggle',  state.emailViewMode === 'text');
}

function _renderBody(email, suffix) {
  const bodyHtml = document.getElementById(`ev-body-html${suffix}`);
  const bodyText = document.getElementById(`ev-body-text${suffix}`);
  if (!bodyHtml || !bodyText) return;

  if (state.emailViewMode === 'html' && email.body_html) {
    bodyHtml.style.display = 'block'; bodyText.style.display = 'none';
    let clean = email.body_html.replace(/<script[\s\S]*?<\/script>/gi,'').replace(/on\w+="[^"]*"/gi,'');
    if (!state.emailShowImages) {
      clean = clean.replace(/<img([^>]*?)\ssrc=(["'])(?!data:)([^"']+)\2/gi, (m, attrs, q, src) =>
        `<img${attrs} data-src="${src}" src="" style="display:none" title="🖼 Imagem bloqueada">`
      );
    }
    bodyHtml.innerHTML = clean;
  } else {
    bodyHtml.style.display = 'none'; bodyText.style.display = 'block';
    bodyText.textContent = email.body_text || '';
  }
}

function toggleEmailImages() {
  state.emailShowImages = !state.emailShowImages;
  const email = document.getElementById('email-view')._email || document.getElementById('email-view-inline')._email;
  if (!email) return;
  ['', '-inline'].forEach(s => { _updateViewButtons(s); _renderBody(email, s); });
}

function setEmailViewMode(mode) {
  state.emailViewMode = mode;
  const email = document.getElementById('email-view')._email || document.getElementById('email-view-inline')._email;
  if (!email) return;
  ['', '-inline'].forEach(s => { _updateViewButtons(s); _renderBody(email, s); });
}

function hideEmail() {
  document.getElementById('reading-empty').style.display = 'flex';
  document.getElementById('email-view').style.display = 'none';
  document.getElementById('reading-empty-inline').style.display = 'flex';
  document.getElementById('email-view-inline').style.display = 'none';
}

async function toggleStar() {
  const id = state.selectedEmailId; if (!id) return;
  const data = await (await fetch(`/api/emails/${id}/star`, { method:'POST' })).json();
  ['', '-inline'].forEach(s => {
    const btn = document.getElementById(`ev-star-btn${s}`);
    if (btn) { btn.textContent = data.starred ? '★ Marcado' : '☆ Marcar'; btn.style.color = data.starred ? 'var(--amber)' : ''; }
  });
  const le = state.emails.find(e => e.id === id);
  if (le) { le.is_starred = data.starred; renderEmailList(); }
}

async function markUnread() {
  const id = state.selectedEmailId; if (!id) return;
  await fetch(`/api/emails/${id}/read`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({read:false}) });
  const le = state.emails.find(e => e.id === id);
  if (le) { le.is_read = false; renderEmailList(); }
  hideEmail(); state.selectedEmailId = null;
  toast('Marcado como não lido', 'success');
}

async function toggleSpam() {
  const id = state.selectedEmailId; if (!id) return;
  const btn = document.getElementById('ev-spam-btn') || document.getElementById('ev-spam-btn-inline');
  const isSpam = btn?.dataset.isSpam === '1';
  const data = await (await fetch(`/api/emails/${id}/spam`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ spam: !isSpam }) })).json();
  if (data.ok) {
    toast(isSpam ? 'Removido do spam' : 'Marcado como spam', 'success');
    state.emails = state.emails.filter(e => e.id !== id);
    state.selectedEmailId = null; hideEmail(); renderEmailList(); updateFolderCounts();
  } else toast('Erro ao mover e-mail', 'error');
}

async function deleteEmail() {
  if (!state.selectedEmailId) return;
  if (!confirm('Excluir este e-mail?')) return;
  await fetch(`/api/emails/${state.selectedEmailId}`, { method:'DELETE' });
  state.emails = state.emails.filter(e => e.id !== state.selectedEmailId);
  state.selectedEmailId = null; hideEmail(); renderEmailList(); updateFolderCounts();
  toast('E-mail excluído', 'success');
}

// ── Compose / Reply / Forward ──────────────────────────────────────────────────
function _currentEmail() {
  return document.getElementById('email-view')._email || document.getElementById('email-view-inline')._email;
}

function openCompose(opts = {}) {
  state.replyToId = opts.replyToId || null; state.attachments = [];
  document.getElementById('compose-to').value      = opts.to || '';
  document.getElementById('compose-cc').value      = opts.cc || '';
  document.getElementById('compose-subject').value = opts.subject || '';
  document.getElementById('compose-body').value    = opts.body || '';
  document.getElementById('compose-attach-list').innerHTML = '';
  document.getElementById('compose-title').textContent    = opts.title || 'Nova mensagem';
  document.getElementById('compose-modal').classList.add('open');
  setTimeout(() => document.getElementById('compose-to').focus(), 50);
}

function closeCompose() { document.getElementById('compose-modal').classList.remove('open'); state.replyToId = null; state.attachments = []; }

function _quoteBody(email) { return `\n\n--- Mensagem original ---\nDe: ${email.from_addr}\nData: ${fmtDateFull(email.date)}\n\n${email.body_text || ''}`; }

function replyEmail()   { const e = _currentEmail(); if (!e) return; openCompose({ to: e.reply_to || e.from_addr, subject: e.subject.startsWith('Re:') ? e.subject : `Re: ${e.subject}`, body: _quoteBody(e), replyToId: e.id, title: 'Responder' }); }
function replyAll()     { const e = _currentEmail(); if (!e) return; openCompose({ to: e.reply_to || e.from_addr, cc: [e.to_addr, e.cc_addr].filter(Boolean).join(', '), subject: e.subject.startsWith('Re:') ? e.subject : `Re: ${e.subject}`, body: _quoteBody(e), replyToId: e.id, title: 'Responder a todos' }); }
function forwardEmail() { const e = _currentEmail(); if (!e) return; openCompose({ subject: e.subject.startsWith('Fwd:') ? e.subject : `Fwd: ${e.subject}`, body: `\n\n--- Mensagem encaminhada ---\nDe: ${e.from_addr}\nPara: ${e.to_addr}\nData: ${fmtDateFull(e.date)}\nAssunto: ${e.subject}\n\n${e.body_text || ''}`, title: 'Encaminhar' }); }
function forwardAsAttachment() { const e = _currentEmail(); if (!e) return; openCompose({ subject: `Fwd: ${e.subject}`, body: `E-mail encaminhado.\n\nOriginal de: ${e.from_addr}\nAssunto: ${e.subject}`, title: 'Encaminhar como anexo' }); toast('Anexe o .eml manualmente se necessário', 'info'); }
function editAsNew()    { const e = _currentEmail(); if (!e) return; openCompose({ to: e.to_addr, cc: e.cc_addr, subject: e.subject, body: e.body_text || '', title: 'Editar como nova mensagem' }); }

function printEmail() {
  const e = _currentEmail(); if (!e) return;
  const win = window.open('', '_blank');
  const body = e.body_html ? e.body_html.replace(/<script[\s\S]*?<\/script>/gi,'') : `<pre style="white-space:pre-wrap">${esc(e.body_text||'')}</pre>`;
  win.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>${esc(e.subject)}</title><style>body{font-family:sans-serif;max-width:800px;margin:20px auto}.header{border-bottom:1px solid #ccc;margin-bottom:16px;padding-bottom:12px}h1{font-size:18px}.meta{font-size:13px;color:#555}@media print{body{margin:0}}</style></head><body><div class="header"><h1>${esc(e.subject)}</h1><div class="meta">De: ${esc(e.from_name||e.from_addr)}</div><div class="meta">Para: ${esc(e.to_addr)}</div><div class="meta">Data: ${fmtDateFull(e.date)}</div></div>${body}</body></html>`);
  win.document.close(); win.focus(); setTimeout(() => win.print(), 400);
}

// ── Attachments ────────────────────────────────────────────────────────────────
function addAttachments(input) { for (const f of input.files) state.attachments.push(f); renderAttachList(); input.value=''; }
function renderAttachList() { document.getElementById('compose-attach-list').innerHTML = state.attachments.map((f,i) => `<span class="compose-att">${esc(f.name)} <span class="compose-att-remove" onclick="removeAtt(${i})">×</span></span>`).join(''); }
function removeAtt(i) { state.attachments.splice(i,1); renderAttachList(); }

async function sendEmail() {
  const btn = document.getElementById('compose-send-btn');
  btn.disabled = true; btn.textContent = 'Enviando...';
  const fd = new FormData();
  fd.append('account_id', document.getElementById('compose-account-select').value);
  fd.append('to',      document.getElementById('compose-to').value);
  fd.append('cc',      document.getElementById('compose-cc').value);
  fd.append('subject', document.getElementById('compose-subject').value);
  fd.append('body',    document.getElementById('compose-body').value);
  if (state.replyToId) fd.append('reply_to_id', state.replyToId);
  for (const f of state.attachments) fd.append('attachments', f);
  try {
    const data = await (await fetch('/api/emails/send', { method:'POST', body: fd })).json();
    if (data.ok) { toast('E-mail enviado!', 'success'); closeCompose(); }
    else toast(`Erro: ${data.error}`, 'error');
  } catch(e) { toast('Erro ao enviar', 'error'); }
  finally { btn.disabled = false; btn.textContent = 'Enviar'; }
}

// ── Sync ───────────────────────────────────────────────────────────────────────
async function syncAll() {
  document.getElementById('sync-btn').classList.add('spinning');
  await fetch('/api/sync/all', { method:'POST' });
  setTimeout(() => document.getElementById('sync-btn').classList.remove('spinning'), 2000);
}
async function syncCurrent() {
  document.getElementById('sync-btn').classList.add('spinning');
  await fetch(state.selectedAccountId ? `/api/accounts/${state.selectedAccountId}/sync` : '/api/sync/all', { method:'POST' });
  setTimeout(() => document.getElementById('sync-btn').classList.remove('spinning'), 2000);
}

async function syncConf() {
  const btn = document.getElementById('sync-conf-btn');
  btn.disabled = true; btn.textContent = '⟳ Sincronizando...';
  try {
    const data = await (await fetch('/api/accounts/sync-conf', { method:'POST' })).json();
    const parts = [];
    if (data.created.length) parts.push(`${data.created.length} criada(s)`);
    if (data.updated.length) parts.push(`${data.updated.length} atualizada(s)`);
    if (data.deleted.length) parts.push(`${data.deleted.length} removida(s)`);
    if (data.errors.length) toast(`Erros: ${data.errors.join(' | ')}`, 'error');
    else toast(parts.length ? parts.join(', ') : 'Nenhuma alteração', 'success');
    await loadAccounts();
  } catch(e) { toast('Erro ao sincronizar contas', 'error'); }
  finally { btn.disabled = false; btn.textContent = '⟳ Ler arquivos .conf'; }
}

// ── Search ─────────────────────────────────────────────────────────────────────
let searchTimer;
function debounceSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.searchQuery = document.getElementById('search-input').value.trim(); state.currentPage = 1; loadEmails(); }, 400);
}

// ── Account modal ──────────────────────────────────────────────────────────────
function openAccountModal() {
  state.editAccountId = null; clearAccountForm();
  document.getElementById('account-form-title').textContent = 'Adicionar conta';
  renderAccountList(); document.getElementById('account-modal').classList.add('open');
}
function closeAccountModal() { document.getElementById('account-modal').classList.remove('open'); }

function renderAccountList() {
  const list = document.getElementById('account-list');
  if (!state.accounts.length) { list.innerHTML = '<div style="color:var(--muted);font-size:13px;margin-bottom:12px;">Nenhuma conta cadastrada</div>'; return; }
  list.innerHTML = state.accounts.map(a => `
    <div class="account-list-item">
      <div class="account-list-info">
        <div class="account-list-name">${esc(a.name)}</div>
        <div class="account-list-email">${esc(a.email)}</div>
        ${a.conf_file ? `<div class="account-list-conf">📄 ${esc(a.conf_file)}</div>` : ''}
      </div>
      <button class="btn btn-ghost" style="padding:5px 10px;font-size:12px" onclick="editAccount(${a.id})">Editar</button>
      <button class="btn btn-danger" style="padding:5px 10px;font-size:12px" onclick="deleteAccount(${a.id})">Remover</button>
      <button class="btn btn-purge" style="padding:5px 10px;font-size:12px" onclick="purgeAccount(${a.id})">🗑 Limpar</button>
    </div>`).join('');
}

function editAccount(id) {
  const a = state.accounts.find(x => x.id === id); if (!a) return;
  state.editAccountId = id;
  document.getElementById('af-name').value      = a.name;
  document.getElementById('af-email').value     = a.email;
  document.getElementById('af-username').value  = a.username;
  document.getElementById('af-password').value  = '';
  document.getElementById('af-imap-host').value = a.imap_host;
  document.getElementById('af-imap-port').value = a.imap_port;
  document.getElementById('af-imap-ssl').checked= a.imap_ssl;
  document.getElementById('af-smtp-host').value = a.smtp_host;
  document.getElementById('af-smtp-port').value = a.smtp_port;
  document.getElementById('af-smtp-ssl').checked= a.smtp_ssl;
  document.getElementById('af-smtp-tls').checked= a.smtp_tls;
  document.getElementById('account-form-title').textContent = 'Editar conta';
}

async function deleteAccount(id) {
  if (!confirm('Remover esta conta? Os e-mails serão mantidos.')) return;
  await fetch(`/api/accounts/${id}`, { method:'DELETE' });
  await loadAccounts();
  if (state.selectedAccountId === id) { state.selectedAccountId = null; document.getElementById('account-select').value = ''; }
  loadEmails(); toast('Conta removida', 'success');
}

async function purgeAccount(id) {
  const a = state.accounts.find(x => x.id === id);
  if (!confirm(`Limpar "${a?.name}"? Remove conta E TODOS OS E-MAILS permanentemente.`)) return;
  await fetch(`/api/accounts/${id}/purge`, { method:'DELETE' });
  await loadAccounts();
  if (state.selectedAccountId === id) { state.selectedAccountId = null; document.getElementById('account-select').value = ''; hideEmail(); }
  loadEmails(); updateFolderCounts(); toast('Conta e e-mails removidos', 'success');
}

function clearAccountForm() {
  ['af-name','af-email','af-username','af-password','af-imap-host','af-imap-port','af-smtp-host','af-smtp-port'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('af-imap-ssl').checked = true;
  document.getElementById('af-smtp-ssl').checked = false;
  document.getElementById('af-smtp-tls').checked = true;
  document.getElementById('af-imap-port').value  = 993;
  document.getElementById('af-smtp-port').value  = 587;
}

async function saveAccount() {
  const data = {
    name: document.getElementById('af-name').value.trim(), email: document.getElementById('af-email').value.trim(),
    username: document.getElementById('af-username').value.trim(), password: document.getElementById('af-password').value,
    imap_host: document.getElementById('af-imap-host').value.trim(), imap_port: parseInt(document.getElementById('af-imap-port').value),
    imap_ssl: document.getElementById('af-imap-ssl').checked, smtp_host: document.getElementById('af-smtp-host').value.trim(),
    smtp_port: parseInt(document.getElementById('af-smtp-port').value), smtp_ssl: document.getElementById('af-smtp-ssl').checked,
    smtp_tls: document.getElementById('af-smtp-tls').checked,
  };
  if (!data.name || !data.email || !data.imap_host || !data.smtp_host) { toast('Preencha todos os campos obrigatórios', 'error'); return; }
  let res;
  if (state.editAccountId) res = await fetch(`/api/accounts/${state.editAccountId}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data) });
  else { if (!data.password) { toast('Senha obrigatória para nova conta', 'error'); return; } res = await fetch('/api/accounts', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data) }); }
  if (res.ok) { toast('Conta salva!', 'success'); await loadAccounts(); clearAccountForm(); state.editAccountId = null; document.getElementById('account-form-title').textContent = 'Adicionar conta'; renderAccountList(); }
  else { toast((await res.json()).error || 'Erro ao salvar', 'error'); }
}

async function testAccount() {
  const btn = document.getElementById('af-test-btn');
  btn.textContent = 'Testando...'; btn.disabled = true;
  const data = { name: document.getElementById('af-name').value.trim(), email: document.getElementById('af-email').value.trim(), username: document.getElementById('af-username').value.trim(), password: document.getElementById('af-password').value, imap_host: document.getElementById('af-imap-host').value.trim(), imap_port: parseInt(document.getElementById('af-imap-port').value), imap_ssl: document.getElementById('af-imap-ssl').checked, smtp_host: document.getElementById('af-smtp-host').value.trim(), smtp_port: parseInt(document.getElementById('af-smtp-port').value), smtp_ssl: document.getElementById('af-smtp-ssl').checked, smtp_tls: document.getElementById('af-smtp-tls').checked };
  if (state.editAccountId) {
    showTestResult(await (await fetch(`/api/accounts/${state.editAccountId}/test`, { method:'POST' })).json());
  } else {
    const cr = await fetch('/api/accounts', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data) });
    if (!cr.ok) { toast('Preencha todos os campos para testar', 'error'); btn.textContent='🔌 Testar'; btn.disabled=false; return; }
    const acc = await cr.json();
    showTestResult(await (await fetch(`/api/accounts/${acc.id}/test`, { method:'POST' })).json());
    await fetch(`/api/accounts/${acc.id}`, { method:'DELETE' }); await loadAccounts();
  }
  btn.textContent = '🔌 Testar'; btn.disabled = false;
}

function showTestResult(r) { if (r.ok) toast('✓ Conexão bem-sucedida!', 'success'); else toast(r.errors.join(' | '), 'error'); }

// ── Utilities ──────────────────────────────────────────────────────────────────
function esc(str) { if (!str) return ''; return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'});
  if (now-d < 7*86400000) return d.toLocaleDateString('pt-BR',{weekday:'short'});
  return d.toLocaleDateString('pt-BR',{day:'2-digit',month:'2-digit'});
}
function fmtDateFull(iso) { if (!iso) return ''; return new Date(iso).toLocaleString('pt-BR',{day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}); }
function fmtSize(b) { if (!b) return ''; if (b<1024) return `${b}B`; if (b<1048576) return `${(b/1024).toFixed(1)}KB`; return `${(b/1048576).toFixed(1)}MB`; }

let toastTimer;
function toast(msg, type='info') {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = `show ${type}`;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.className='', 3500);
}

document.getElementById('compose-modal').addEventListener('click', e => { if (e.target===document.getElementById('compose-modal')) closeCompose(); });
document.getElementById('account-modal').addEventListener('click', e => { if (e.target===document.getElementById('account-modal')) closeAccountModal(); });

document.addEventListener('keydown', e => {
  if (e.key==='Escape') { closeCompose(); closeAccountModal(); }
  if ((e.ctrlKey||e.metaKey) && e.key==='n') { e.preventDefault(); openCompose(); }
  if ((e.ctrlKey||e.metaKey) && !e.shiftKey && e.key==='r' && state.selectedEmailId) { e.preventDefault(); replyEmail(); }
  if ((e.ctrlKey||e.metaKey) && e.shiftKey  && e.key==='R' && state.selectedEmailId) { e.preventDefault(); replyAll(); }
  if ((e.ctrlKey||e.metaKey) && e.key==='l' && state.selectedEmailId) { e.preventDefault(); forwardEmail(); }
  if ((e.ctrlKey||e.metaKey) && e.key==='p' && state.selectedEmailId) { e.preventDefault(); printEmail(); }
});

document.addEventListener('DOMContentLoaded', init);