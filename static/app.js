// ── State ──────────────────────────────────────────────────────────────────────
const state = {
  accounts: [],
  selectedAccountId: null,
  selectedFolder: 'INBOX',
  selectedFolderReal: null, // pasta real no servidor
  emails: [],
  selectedEmailId: null,
  currentPage: 1,
  totalPages: 1,
  searchQuery: '',
  replyToId: null,
  editAccountId: null,
  attachments: [],
  selectedIds: new Set(),
  emailShowImages: false,
  emailViewMode: 'html',
  currentEmail: null,
  sortBy: 'date',
  sortDir: 'desc',
};

// ── Settings ───────────────────────────────────────────────────────────────────
const settings = {
  theme: 'dark',
  layout: 'vertical',
  showImagesByDefault: false,
  autoSync: 0,
  splitSize: 320,
  collapsedAccounts: {}, // { accountId: true/false }
};

function loadSettings() {
  try { Object.assign(settings, JSON.parse(localStorage.getItem('correo_settings') || '{}')); } catch(e) {}
  applyTheme();
  applyLayout();
  applySplit();
  updateSettingsUI();
  if (settings.autoSync > 0) startAutoSync();
}
function saveSettings() { localStorage.setItem('correo_settings', JSON.stringify(settings)); }

// ── Theme ──────────────────────────────────────────────────────────────────────
function applyTheme() {
  document.documentElement.setAttribute('data-theme', settings.theme);
  const btn = document.getElementById('toggle-theme');
  if (btn) btn.textContent = settings.theme === 'dark' ? '☀ Claro' : '🌙 Escuro';
}
function toggleTheme() { settings.theme = settings.theme === 'dark' ? 'light' : 'dark'; saveSettings(); applyTheme(); }

// ── Layout ─────────────────────────────────────────────────────────────────────
function applyLayout() {
  const panel = document.getElementById('main-panel');
  panel.setAttribute('data-layout', settings.layout);
  applySplit();
  const btn = document.getElementById('toggle-layout');
  if (btn) btn.textContent = settings.layout === 'vertical' ? '⇔ Horizontal' : '⇕ Vertical';
}
function toggleLayout() {
  settings.layout = settings.layout === 'vertical' ? 'horizontal' : 'vertical';
  saveSettings(); applyLayout();
}

// ── Split resize ───────────────────────────────────────────────────────────────
function applySplit() {
  const panel = document.getElementById('main-panel');
  if (settings.layout === 'vertical') {
    const w = Math.max(200, Math.min(settings.splitSize, window.innerWidth - 500));
    panel.style.gridTemplateColumns = `${w}px 4px 1fr`;
    panel.style.gridTemplateRows = '';
  } else {
    const h = Math.max(120, Math.min(settings.splitSize, window.innerHeight - 200));
    panel.style.gridTemplateRows = `1fr 4px ${h}px`;
    panel.style.gridTemplateColumns = '';
  }
}

let resizing = false, resizeStart = 0, resizeStartSize = 0;
function startResize(e) {
  resizing = true;
  resizeStart = settings.layout === 'vertical' ? e.clientX : e.clientY;
  resizeStartSize = settings.splitSize;
  document.getElementById('resize-handle').classList.add('dragging');
  document.body.style.cursor = settings.layout === 'vertical' ? 'col-resize' : 'row-resize';
  document.body.style.userSelect = 'none';
}
document.addEventListener('mousemove', e => {
  if (!resizing) return;
  settings.splitSize = settings.layout === 'vertical'
    ? resizeStartSize + (e.clientX - resizeStart)
    : resizeStartSize + (resizeStart - e.clientY);
  applySplit();
});
document.addEventListener('mouseup', () => {
  if (!resizing) return;
  resizing = false;
  document.getElementById('resize-handle').classList.remove('dragging');
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  saveSettings();
});
window.addEventListener('resize', applySplit);

// ── Auto-sync ──────────────────────────────────────────────────────────────────
let autoSyncTimer = null;
function startAutoSync() {
  stopAutoSync();
  if (settings.autoSync <= 0) return;
  autoSyncTimer = setInterval(() => fetch('/api/sync/all', { method: 'POST' }), settings.autoSync * 60 * 1000);
}
function stopAutoSync() { if (autoSyncTimer) { clearInterval(autoSyncTimer); autoSyncTimer = null; } }
function setAutoSync(val) {
  settings.autoSync = parseInt(val); saveSettings();
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
socket.on('sync_done',  d => { toast(`✓ ${d.email} sincronizado${d.new ? ` (${d.new} novos)` : ''}`, 'success'); loadEmails(); renderAccountTree(); });
socket.on('sync_error', d => toast(`Erro: ${d.error}`, 'error'));
socket.on('new_email',  d => { if (d.account_id === state.selectedAccountId || !state.selectedAccountId) { loadEmails(); renderAccountTree(); } });

// ── Init ───────────────────────────────────────────────────────────────────────
async function init() {
  loadSettings();
  await loadAccounts();
  await loadEmails();
}

// ── Accounts ───────────────────────────────────────────────────────────────────
let folderCountsData = {}; // { accountId: { folderName: { total, unread } } }

async function loadAccounts() {
  const res = await fetch('/api/accounts');
  state.accounts = await res.json();
  renderComposeAccountSelect();
  renderAccountList();
  await loadAllFolderCounts();
  renderAccountTree();
}

async function loadAllFolderCounts() {
  folderCountsData = {};
  for (const acc of state.accounts) {
    try {
      const res = await fetch(`/api/folders/counts?account_id=${acc.id}`);
      folderCountsData[acc.id] = await res.json();
    } catch(e) {
      folderCountsData[acc.id] = {};
    }
  }
}

function renderComposeAccountSelect() {
  const sel = document.getElementById('compose-account-select');
  sel.innerHTML = '';
  for (const a of state.accounts) {
    const opt = document.createElement('option');
    opt.value = a.id; opt.textContent = `${a.name} <${a.email}>`; sel.appendChild(opt);
  }
}

// ── Account tree (sidebar Thunderbird-style) ───────────────────────────────────

const FOLDER_DEFS = [
  { key: 'inbox',  label: 'Entrada',    icon: '📥', keywords: ['inbox'] },
  { key: 'drafts', label: 'Rascunhos',  icon: '📝', keywords: ['draft'] },
  { key: 'sent',   label: 'Enviados',   icon: '📤', keywords: ['sent'] },
  { key: 'spam',   label: 'Spam',       icon: '🔥', keywords: ['spam', 'junk'] },
  { key: 'trash',  label: 'Lixeira',    icon: '🗑',  keywords: ['trash'] },
  { key: 'archive',label: 'Arquivo',    icon: '📦', keywords: ['archive'] },
];

function matchFolderDef(realName) {
  const k = realName.toLowerCase();
  return FOLDER_DEFS.find(d => d.keywords.some(kw => k.includes(kw)));
}

function getFolderCounts(accountId, realFolder) {
  const data = folderCountsData[accountId] || {};
  return data[realFolder] || { total: 0, unread: 0 };
}

function getAccountUnread(accountId) {
  const data = folderCountsData[accountId] || {};
  return Object.values(data).reduce((sum, c) => sum + (c.unread || 0), 0);
}

function renderAccountTree() {
  const container = document.getElementById('account-tree');
  container.innerHTML = '';

  for (const acc of state.accounts) {
    const counts = folderCountsData[acc.id] || {};
    const totalUnread = getAccountUnread(acc.id);
    const isCollapsed = settings.collapsedAccounts[acc.id] || false;

    const node = document.createElement('div');
    node.className = 'account-node' + (isCollapsed ? ' collapsed' : '');
    node.dataset.accId = acc.id;

    // Header da conta
    const header = document.createElement('div');
    header.className = 'account-header';
    header.innerHTML = `
      <span class="account-toggle">▾</span>
      <span class="account-icon">✉</span>
      <span class="account-email" title="${esc(acc.email)}">${esc(acc.email)}</span>
      ${totalUnread > 0 ? `<span class="account-badge">${totalUnread}</span>` : ''}
    `;
    header.onclick = () => toggleAccountCollapse(acc.id, node);

    // Pastas
    const foldersDiv = document.createElement('div');
    foldersDiv.className = 'account-folders';

    // Monta lista de pastas reais agrupadas em virtuais
    const seen = new Set();
    const orderedDefs = [...FOLDER_DEFS];

    // Primeiro adiciona as pastas que temos contagens
    const realFolders = Object.keys(counts);

    for (const def of orderedDefs) {
      // Encontra pastas reais que correspondem a essa def
      const matching = realFolders.filter(f => def.keywords.some(kw => f.toLowerCase().includes(kw)));
      if (matching.length === 0) continue;

      const totalC = matching.reduce((s, f) => s + (counts[f]?.total || 0), 0);
      const unreadC = matching.reduce((s, f) => s + (counts[f]?.unread || 0), 0);
      if (totalC === 0 && def.key !== 'inbox') { matching.forEach(f => seen.add(f)); continue; }

      const primaryReal = matching[0];
      seen.add(...matching);

      const folderEl = document.createElement('div');
      folderEl.className = 'folder-item' + (
        state.selectedAccountId === acc.id && state.selectedFolder === def.key ? ' active' : ''
      );
      folderEl.innerHTML = `
        <span class="folder-icon">${def.icon}</span>
        <span class="folder-name">${def.label}</span>
        ${unreadC > 0 ? `<span class="folder-badge">${unreadC}</span>` : ''}
      `;
      folderEl.onclick = () => selectFolder(acc.id, def.key, matching);
      foldersDiv.appendChild(folderEl);
    }

    // Pastas extras não mapeadas
    for (const real of realFolders) {
      if (seen.has(real)) continue;
      const c = counts[real] || { total: 0, unread: 0 };
      if (c.total === 0) continue;
      const folderEl = document.createElement('div');
      folderEl.className = 'folder-item' + (
        state.selectedAccountId === acc.id && state.selectedFolder === real ? ' active' : ''
      );
      folderEl.innerHTML = `
        <span class="folder-icon">📁</span>
        <span class="folder-name">${esc(real)}</span>
        ${c.unread > 0 ? `<span class="folder-badge">${c.unread}</span>` : ''}
      `;
      folderEl.onclick = () => selectFolder(acc.id, real, [real]);
      foldersDiv.appendChild(folderEl);
    }

    // Garante pelo menos INBOX mesmo sem contagens
    if (foldersDiv.children.length === 0) {
      const def = FOLDER_DEFS[0];
      const folderEl = document.createElement('div');
      folderEl.className = 'folder-item' + (state.selectedAccountId === acc.id && state.selectedFolder === 'inbox' ? ' active' : '');
      folderEl.innerHTML = `<span class="folder-icon">${def.icon}</span><span class="folder-name">${def.label}</span>`;
      folderEl.onclick = () => selectFolder(acc.id, def.key, ['INBOX']);
      foldersDiv.appendChild(folderEl);
    }

    // Altura para animação
    if (!isCollapsed) {
      foldersDiv.style.maxHeight = '500px';
    }

    node.appendChild(header);
    node.appendChild(foldersDiv);
    container.appendChild(node);
  }

  if (!state.accounts.length) {
    container.innerHTML = '<div style="padding:16px;color:var(--muted);font-size:12px;">Nenhuma conta. Clique em ⚙ Gerenciar contas.</div>';
  }
}

function toggleAccountCollapse(accId, node) {
  const isCollapsed = !settings.collapsedAccounts[accId];
  settings.collapsedAccounts[accId] = isCollapsed;
  saveSettings();
  node.classList.toggle('collapsed', isCollapsed);
  const foldersDiv = node.querySelector('.account-folders');
  foldersDiv.style.maxHeight = isCollapsed ? '0' : '500px';
}

// ── Folder selection ───────────────────────────────────────────────────────────
function selectFolder(accountId, folderKey, realFolders) {
  state.selectedAccountId = accountId;
  state.selectedFolder = folderKey;
  state.selectedFolderReals = realFolders;
  state.selectedEmailId = null;
  state.selectedIds.clear();
  state.currentPage = 1;
  hideEmail();
  renderAccountTree();
  loadEmails();
}

function realFoldersFor(folderKey) {
  if (state.selectedFolderReals && state.selectedFolderReals.length) {
    return state.selectedFolderReals;
  }
  // Fallback: tenta encontrar pastas reais que correspondem
  if (!state.selectedAccountId) return [folderKey];
  const counts = folderCountsData[state.selectedAccountId] || {};
  const def = FOLDER_DEFS.find(d => d.key === folderKey);
  if (!def) return [folderKey];
  const matching = Object.keys(counts).filter(f => def.keywords.some(kw => f.toLowerCase().includes(kw)));
  return matching.length ? matching : [folderKey === 'inbox' ? 'INBOX' : folderKey];
}

// ── Sort ───────────────────────────────────────────────────────────────────────
function setSortBy(col) {
  if (state.sortBy === col) {
    state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
  } else {
    state.sortBy = col;
    state.sortDir = col === 'date' ? 'desc' : 'asc';
  }
  updateSortArrows();
  sortAndRender();
}

function updateSortArrows() {
  ['from', 'subject', 'date'].forEach(col => {
    const el = document.getElementById(`sort-${col}-arrow`);
    const hdr = document.getElementById(`sort-${col}`);
    if (!el || !hdr) return;
    if (state.sortBy === col) {
      el.textContent = state.sortDir === 'asc' ? '▲' : '▼';
      hdr.classList.add('sorted');
    } else {
      el.textContent = '';
      hdr.classList.remove('sorted');
    }
  });
}

function sortEmails(emails) {
  return [...emails].sort((a, b) => {
    let va, vb;
    if (state.sortBy === 'date') {
      va = new Date(a.date).getTime();
      vb = new Date(b.date).getTime();
    } else if (state.sortBy === 'from') {
      va = (a.from_name || a.from_addr || '').toLowerCase();
      vb = (b.from_name || b.from_addr || '').toLowerCase();
    } else {
      va = (a.subject || '').toLowerCase();
      vb = (b.subject || '').toLowerCase();
    }
    if (va < vb) return state.sortDir === 'asc' ? -1 : 1;
    if (va > vb) return state.sortDir === 'asc' ? 1 : -1;
    return 0;
  });
}

function sortAndRender() {
  state.emails = sortEmails(state.emails);
  renderEmailList();
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
    state.emails = sortEmails(all);
    renderEmailList();
  } catch(e) { toast('Erro ao carregar e-mails', 'error'); }
  finally { loading.style.display = 'none'; }
}

function renderEmailList() {
  const container = document.getElementById('email-list');
  const empty     = document.getElementById('list-empty');
  container.querySelectorAll('.email-item').forEach(e => e.remove());

  if (!state.emails.length) { empty.style.display = 'flex'; return; }
  empty.style.display = 'none';

  for (const email of state.emails) {
    const isSel    = state.selectedIds.has(email.id);
    const isActive = email.id === state.selectedEmailId;
    const item = document.createElement('div');
    item.className = 'email-item'
      + (email.is_read ? '' : ' unread')
      + (isActive ? ' active' : '')
      + (isSel ? ' selected' : '');
    item.dataset.id = email.id;

    item.innerHTML = `
      <div class="ei-check-col">
        <input type="checkbox" class="ei-checkbox" ${isSel ? 'checked' : ''} onclick="toggleSelect(event,${email.id})">
      </div>
      <div class="ei-icons-col">
        ${email.is_read ? '<span class="dot-read"></span>' : '<span class="dot-unread"></span>'}
        <span class="icon-star ${email.is_starred ? 'starred' : ''}" onclick="quickStar(event,${email.id})">★</span>
        ${email.has_attachments ? '<span class="icon-attach">📎</span>' : ''}
      </div>
      <div class="ei-from">${esc(email.from_name || email.from_addr)}</div>
      <div class="ei-subject">${esc(email.subject)}</div>
      <div class="ei-date">${fmtDate(email.date)}</div>
    `;
    item.onclick = e => { if (e.target.classList.contains('ei-checkbox') || e.target.classList.contains('icon-star')) return; openEmail(email.id); };
    container.appendChild(item);
  }
  renderBulkBar();
}

// ── Quick star (sem abrir email) ───────────────────────────────────────────────
async function quickStar(e, id) {
  e.stopPropagation();
  const data = await (await fetch(`/api/emails/${id}/star`, { method: 'POST' })).json();
  const le = state.emails.find(x => x.id === id);
  if (le) { le.is_starred = data.starred; renderEmailList(); }
  if (state.selectedEmailId === id) {
    const btn = document.getElementById('ev-star-btn');
    if (btn) { btn.textContent = data.starred ? '★ Marcado' : '☆ Marcar'; btn.style.color = data.starred ? 'var(--amber)' : ''; }
  }
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
  const L = { delete: 'Excluir', mark_spam: 'Marcar como spam', mark_read: 'Marcar como lido', mark_unread: 'Marcar como não lido', mark_not_spam: 'Desmarcar spam' };
  if (['delete', 'mark_spam'].includes(action) && !confirm(`${L[action]} ${ids.length} e-mail(s)?`)) return;
  const data = await (await fetch('/api/emails/bulk', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, ids, folder: state.selectedFolder, account_id: state.selectedAccountId }),
  })).json();
  if (data.ok) {
    toast(`${L[action]}: ${data.count} e-mail(s)`, 'success');
    state.selectedIds.clear(); await loadEmails(); await refreshFolderCounts();
    if (['delete', 'mark_spam'].includes(action) && ids.includes(state.selectedEmailId)) { hideEmail(); state.selectedEmailId = null; }
  } else toast('Erro na operação', 'error');
}

async function bulkActionAll(action) {
  const L = { mark_read: 'Marcar todos como lido', delete: 'Excluir todos' };
  if (!confirm(`${L[action]} na pasta atual?`)) return;
  for (const folder of realFoldersFor(state.selectedFolder)) {
    await fetch('/api/emails/bulk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, ids: [], folder, account_id: state.selectedAccountId }),
    });
  }
  toast('Operação concluída', 'success'); await loadEmails(); await refreshFolderCounts();
  if (action === 'delete') { hideEmail(); state.selectedEmailId = null; }
}

async function refreshFolderCounts() {
  await loadAllFolderCounts();
  renderAccountTree();
}

// ── Email view ─────────────────────────────────────────────────────────────────
async function openEmail(id) {
  state.selectedEmailId = id;
  document.querySelectorAll('.email-item').forEach(el => el.classList.toggle('active', parseInt(el.dataset.id) === id));

  const email = await (await fetch(`/api/emails/${id}`)).json();
  state.currentEmail = email;

  const le = state.emails.find(e => e.id === id);
  if (le) { le.is_read = true; renderEmailList(); }

  state.emailShowImages = settings.showImagesByDefault;
  state.emailViewMode   = email.body_html ? 'html' : 'text';

  document.getElementById('reading-empty').style.display = 'none';
  document.getElementById('email-view').style.display    = 'flex';

  // Header
  document.getElementById('ev-subject').textContent   = email.subject;
  document.getElementById('ev-avatar').textContent    = (email.from_name || email.from_addr || '?').slice(0, 1).toUpperCase();
  document.getElementById('ev-from-name').textContent = email.from_name || email.from_addr;
  document.getElementById('ev-from-addr').textContent = `<${email.from_addr}>`;
  document.getElementById('ev-to-line').textContent   = email.to_addr ? `Para: ${email.to_addr}` : '';
  document.getElementById('ev-date-meta').textContent = fmtDateFull(email.date);

  // Star
  const starBtn = document.getElementById('ev-star-btn');
  starBtn.textContent = email.is_starred ? '★ Marcado' : '☆ Marcar';
  starBtn.style.color = email.is_starred ? 'var(--amber)' : '';

  // Spam
  const isSpam  = isSpamFolder(email.folder || '');
  const spamBtn = document.getElementById('ev-spam-btn');
  spamBtn.textContent    = isSpam ? '✓ Não spam' : '⚠ Spam';
  spamBtn.dataset.isSpam = isSpam ? '1' : '0';

  // Banner de junk
  const banner = document.getElementById('ev-junk-banner');
  banner.classList.toggle('visible', isSpam);

  // Attachments
  const attDiv = document.getElementById('ev-attachments');
  if (email.attachments && email.attachments.length) {
    attDiv.style.display = 'flex';
    attDiv.innerHTML = email.attachments.map(a =>
      `<a class="att-chip" href="/api/attachments/${a.id}" target="_blank">
        📎 ${esc(a.filename)} <span class="att-size">${fmtSize(a.size)}</span></a>`
    ).join('');
  } else { attDiv.style.display = 'none'; attDiv.innerHTML = ''; }

  _renderEmailBody();
  _updateViewButtons();
}

function isSpamFolder(name) {
  const k = (name || '').toLowerCase();
  return k.includes('spam') || k.includes('junk');
}

function _renderEmailBody() {
  const email    = state.currentEmail; if (!email) return;
  const bodyHtml = document.getElementById('ev-body-html');
  const bodyText = document.getElementById('ev-body-text');

  if (state.emailViewMode === 'html' && email.body_html) {
    bodyHtml.style.display = 'block'; bodyText.style.display = 'none';
    let clean = email.body_html
      .replace(/<script[\s\S]*?<\/script>/gi, '')
      .replace(/on\w+\s*=\s*["'][^"']*["']/gi, '');
    if (!state.emailShowImages) {
      clean = clean.replace(/<img([^>]*?)\ssrc=(["'])(?!data:)([^"']+)\2/gi,
        (m, attrs, q, src) => `<img${attrs} data-src="${src}" src="" style="display:none" title="🖼 Imagem bloqueada">`
      );
    }
    bodyHtml.innerHTML = clean;
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

function hideEmail() {
  document.getElementById('reading-empty').style.display = 'flex';
  document.getElementById('email-view').style.display    = 'none';
  document.getElementById('ev-junk-banner').classList.remove('visible');
  state.currentEmail = null;
}

async function toggleStar() {
  const id = state.selectedEmailId; if (!id) return;
  const data = await (await fetch(`/api/emails/${id}/star`, { method: 'POST' })).json();
  const btn  = document.getElementById('ev-star-btn');
  btn.textContent = data.starred ? '★ Marcado' : '☆ Marcar';
  btn.style.color = data.starred ? 'var(--amber)' : '';
  const le = state.emails.find(e => e.id === id);
  if (le) { le.is_starred = data.starred; renderEmailList(); }
}

async function markUnread() {
  const id = state.selectedEmailId; if (!id) return;
  await fetch(`/api/emails/${id}/read`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ read: false }) });
  const le = state.emails.find(e => e.id === id);
  if (le) { le.is_read = false; renderEmailList(); }
  hideEmail(); state.selectedEmailId = null;
  toast('Marcado como não lido', 'success');
}

async function toggleSpam() {
  const id = state.selectedEmailId; if (!id) return;
  const btn    = document.getElementById('ev-spam-btn');
  const isSpam = btn.dataset.isSpam === '1';
  const data   = await (await fetch(`/api/emails/${id}/spam`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ spam: !isSpam }),
  })).json();
  if (data.ok) {
    toast(isSpam ? 'Removido do spam' : 'Marcado como spam', 'success');
    state.emails = state.emails.filter(e => e.id !== id);
    state.selectedEmailId = null; hideEmail(); renderEmailList(); await refreshFolderCounts();
  } else toast('Erro ao mover e-mail', 'error');
}

async function deleteEmail() {
  if (!state.selectedEmailId) return;
  if (!confirm('Excluir este e-mail?')) return;
  await fetch(`/api/emails/${state.selectedEmailId}`, { method: 'DELETE' });
  state.emails = state.emails.filter(e => e.id !== state.selectedEmailId);
  state.selectedEmailId = null; hideEmail(); renderEmailList(); await refreshFolderCounts();
  toast('E-mail excluído', 'success');
}

// ── Compose / Reply / Forward ──────────────────────────────────────────────────
function openCompose(opts = {}) {
  state.replyToId = opts.replyToId || null; state.attachments = [];
  document.getElementById('compose-to').value      = opts.to || '';
  document.getElementById('compose-cc').value      = opts.cc || '';
  document.getElementById('compose-subject').value = opts.subject || '';
  document.getElementById('compose-body').value    = opts.body || '';
  document.getElementById('compose-attach-list').innerHTML = '';
  document.getElementById('compose-title').textContent = opts.title || 'Nova mensagem';
  document.getElementById('compose-modal').classList.add('open');
  setTimeout(() => document.getElementById('compose-to').focus(), 50);
}
function closeCompose() { document.getElementById('compose-modal').classList.remove('open'); state.replyToId = null; state.attachments = []; }

function _quoteBody(e) { return `\n\n--- Mensagem original ---\nDe: ${e.from_addr}\nData: ${fmtDateFull(e.date)}\n\n${e.body_text || ''}`; }

function replyEmail() {
  const e = state.currentEmail; if (!e) return;
  openCompose({ to: e.reply_to || e.from_addr, subject: e.subject.startsWith('Re:') ? e.subject : `Re: ${e.subject}`, body: _quoteBody(e), replyToId: e.id, title: 'Responder' });
}
function replyAll() {
  const e = state.currentEmail; if (!e) return;
  openCompose({ to: e.reply_to || e.from_addr, cc: [e.to_addr, e.cc_addr].filter(Boolean).join(', '), subject: e.subject.startsWith('Re:') ? e.subject : `Re: ${e.subject}`, body: _quoteBody(e), replyToId: e.id, title: 'Responder a todos' });
}
function forwardEmail() {
  const e = state.currentEmail; if (!e) return;
  openCompose({ subject: e.subject.startsWith('Fwd:') ? e.subject : `Fwd: ${e.subject}`, body: `\n\n--- Mensagem encaminhada ---\nDe: ${e.from_addr}\nPara: ${e.to_addr}\nData: ${fmtDateFull(e.date)}\nAssunto: ${e.subject}\n\n${e.body_text || ''}`, title: 'Encaminhar' });
}
function forwardAsAttachment() {
  const e = state.currentEmail; if (!e) return;
  openCompose({ subject: `Fwd: ${e.subject}`, body: `E-mail encaminhado.\n\nOriginal de: ${e.from_addr}\nAssunto: ${e.subject}`, title: 'Encaminhar como anexo' });
  toast('Anexe o .eml manualmente se necessário', 'info');
}
function editAsNew() {
  const e = state.currentEmail; if (!e) return;
  openCompose({ to: e.to_addr, cc: e.cc_addr, subject: e.subject, body: e.body_text || '', title: 'Editar como nova mensagem' });
}
function printEmail() {
  const e = state.currentEmail; if (!e) return;
  const win  = window.open('', '_blank');
  const body = e.body_html ? e.body_html.replace(/<script[\s\S]*?<\/script>/gi, '') : `<pre style="white-space:pre-wrap">${esc(e.body_text || '')}</pre>`;
  win.document.write(`<!DOCTYPE html><html><head><meta charset="UTF-8"><title>${esc(e.subject)}</title><style>body{font-family:sans-serif;max-width:800px;margin:20px auto}.h{border-bottom:1px solid #ccc;margin-bottom:16px;padding-bottom:12px}h1{font-size:16px}.m{font-size:12px;color:#555}@media print{body{margin:0}}</style></head><body><div class="h"><h1>${esc(e.subject)}</h1><div class="m">De: ${esc(e.from_name||e.from_addr)}</div><div class="m">Para: ${esc(e.to_addr)}</div><div class="m">Data: ${fmtDateFull(e.date)}</div></div>${body}</body></html>`);
  win.document.close(); win.focus(); setTimeout(() => win.print(), 400);
}

// ── Attachments ────────────────────────────────────────────────────────────────
function addAttachments(input) { for (const f of input.files) state.attachments.push(f); renderAttachList(); input.value = ''; }
function renderAttachList() { document.getElementById('compose-attach-list').innerHTML = state.attachments.map((f, i) => `<span class="compose-att">${esc(f.name)} <span class="compose-att-remove" onclick="removeAtt(${i})">×</span></span>`).join(''); }
function removeAtt(i) { state.attachments.splice(i, 1); renderAttachList(); }

async function sendEmail() {
  const btn = document.getElementById('compose-send-btn');
  btn.disabled = true; btn.textContent = 'Enviando...';
  const fd = new FormData();
  fd.append('account_id', document.getElementById('compose-account-select').value);
  fd.append('to', document.getElementById('compose-to').value);
  fd.append('cc', document.getElementById('compose-cc').value);
  fd.append('subject', document.getElementById('compose-subject').value);
  fd.append('body', document.getElementById('compose-body').value);
  if (state.replyToId) fd.append('reply_to_id', state.replyToId);
  for (const f of state.attachments) fd.append('attachments', f);
  try {
    const data = await (await fetch('/api/emails/send', { method: 'POST', body: fd })).json();
    if (data.ok) { toast('E-mail enviado!', 'success'); closeCompose(); }
    else toast(`Erro: ${data.error}`, 'error');
  } catch(e) { toast('Erro ao enviar', 'error'); }
  finally { btn.disabled = false; btn.textContent = 'Enviar'; }
}

// ── Sync ───────────────────────────────────────────────────────────────────────
async function syncAll() {
  const btn = document.getElementById('sync-all-btn');
  btn.classList.add('spinning');
  await fetch('/api/sync/all', { method: 'POST' });
  setTimeout(() => btn.classList.remove('spinning'), 2000);
}
async function syncCurrent() {
  const btn = document.getElementById('sync-btn');
  btn.classList.add('spinning');
  const url = state.selectedAccountId ? `/api/accounts/${state.selectedAccountId}/sync` : '/api/sync/all';
  await fetch(url, { method: 'POST' });
  setTimeout(() => btn.classList.remove('spinning'), 2000);
}
async function syncConf() {
  const btn = document.getElementById('sync-conf-btn');
  btn.disabled = true; btn.textContent = '⟳ Sincronizando...';
  try {
    const data = await (await fetch('/api/accounts/sync-conf', { method: 'POST' })).json();
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
  if (!state.accounts.length) { list.innerHTML = '<div style="color:var(--muted);font-size:12px;margin-bottom:12px;">Nenhuma conta cadastrada</div>'; return; }
  list.innerHTML = state.accounts.map(a => `
    <div class="account-list-item">
      <div class="account-list-info">
        <div class="account-list-name">${esc(a.name)}</div>
        <div class="account-list-email">${esc(a.email)}</div>
        ${a.conf_file ? `<div class="account-list-conf">📄 ${esc(a.conf_file)}</div>` : ''}
      </div>
      <button class="btn btn-ghost" style="padding:4px 9px;font-size:11px" onclick="editAccount(${a.id})">Editar</button>
      <button class="btn btn-danger" style="padding:4px 9px;font-size:11px" onclick="deleteAccount(${a.id})">Remover</button>
      <button class="btn btn-purge" style="padding:4px 9px;font-size:11px" onclick="purgeAccount(${a.id})">🗑 Limpar</button>
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
  document.getElementById('af-imap-ssl').checked = a.imap_ssl;
  document.getElementById('af-smtp-host').value = a.smtp_host;
  document.getElementById('af-smtp-port').value = a.smtp_port;
  document.getElementById('af-smtp-ssl').checked = a.smtp_ssl;
  document.getElementById('af-smtp-tls').checked = a.smtp_tls;
  document.getElementById('account-form-title').textContent = 'Editar conta';
}

async function deleteAccount(id) {
  if (!confirm('Remover esta conta? Os e-mails serão mantidos.')) return;
  await fetch(`/api/accounts/${id}`, { method: 'DELETE' });
  await loadAccounts();
  if (state.selectedAccountId === id) { state.selectedAccountId = null; }
  loadEmails(); toast('Conta removida', 'success');
}

async function purgeAccount(id) {
  const a = state.accounts.find(x => x.id === id);
  if (!confirm(`Limpar "${a?.name}"? Remove conta E TODOS OS E-MAILS permanentemente.`)) return;
  await fetch(`/api/accounts/${id}/purge`, { method: 'DELETE' });
  await loadAccounts();
  if (state.selectedAccountId === id) { state.selectedAccountId = null; hideEmail(); }
  loadEmails(); toast('Conta e e-mails removidos', 'success');
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
  if (state.editAccountId) {
    res = await fetch(`/api/accounts/${state.editAccountId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  } else {
    if (!data.password) { toast('Senha obrigatória para nova conta', 'error'); return; }
    res = await fetch('/api/accounts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  }
  if (res.ok) {
    toast('Conta salva!', 'success'); await loadAccounts(); clearAccountForm();
    state.editAccountId = null; document.getElementById('account-form-title').textContent = 'Adicionar conta'; renderAccountList();
  } else { toast((await res.json()).error || 'Erro ao salvar', 'error'); }
}

async function testAccount() {
  const btn = document.getElementById('af-test-btn');
  btn.textContent = 'Testando...'; btn.disabled = true;
  const data = { name: document.getElementById('af-name').value.trim(), email: document.getElementById('af-email').value.trim(), username: document.getElementById('af-username').value.trim(), password: document.getElementById('af-password').value, imap_host: document.getElementById('af-imap-host').value.trim(), imap_port: parseInt(document.getElementById('af-imap-port').value), imap_ssl: document.getElementById('af-imap-ssl').checked, smtp_host: document.getElementById('af-smtp-host').value.trim(), smtp_port: parseInt(document.getElementById('af-smtp-port').value), smtp_ssl: document.getElementById('af-smtp-ssl').checked, smtp_tls: document.getElementById('af-smtp-tls').checked };
  if (state.editAccountId) {
    showTestResult(await (await fetch(`/api/accounts/${state.editAccountId}/test`, { method: 'POST' })).json());
  } else {
    const cr = await fetch('/api/accounts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    if (!cr.ok) { toast('Preencha todos os campos para testar', 'error'); btn.textContent = '🔌 Testar'; btn.disabled = false; return; }
    const acc = await cr.json();
    showTestResult(await (await fetch(`/api/accounts/${acc.id}/test`, { method: 'POST' })).json());
    await fetch(`/api/accounts/${acc.id}`, { method: 'DELETE' }); await loadAccounts();
  }
  btn.textContent = '🔌 Testar'; btn.disabled = false;
}
function showTestResult(r) { if (r.ok) toast('✓ Conexão bem-sucedida!', 'success'); else toast(r.errors.join(' | '), 'error'); }

// ── Utilities ──────────────────────────────────────────────────────────────────
function esc(str) { if (!str) return ''; return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  if (now - d < 7 * 86400000) return d.toLocaleDateString('pt-BR', { weekday: 'short', day: '2-digit', month: '2-digit' });
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: '2-digit' });
}

function fmtDateFull(iso) { if (!iso) return ''; return new Date(iso).toLocaleString('pt-BR', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }); }
function fmtSize(b) { if (!b) return ''; if (b < 1024) return `${b}B`; if (b < 1048576) return `${(b/1024).toFixed(1)}KB`; return `${(b/1048576).toFixed(1)}MB`; }

let toastTimer;
function toast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = `show ${type}`;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.className = '', 3500);
}

// ── Events ─────────────────────────────────────────────────────────────────────
document.getElementById('compose-modal').addEventListener('click', e => { if (e.target === document.getElementById('compose-modal')) closeCompose(); });
document.getElementById('account-modal').addEventListener('click', e => { if (e.target === document.getElementById('account-modal')) closeAccountModal(); });

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeCompose(); closeAccountModal(); }
  if ((e.ctrlKey || e.metaKey) && e.key === 'n') { e.preventDefault(); openCompose(); }
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key === 'r' && state.selectedEmailId) { e.preventDefault(); replyEmail(); }
  if ((e.ctrlKey || e.metaKey) && e.shiftKey  && e.key === 'R' && state.selectedEmailId) { e.preventDefault(); replyAll(); }
  if ((e.ctrlKey || e.metaKey) && e.key === 'l' && state.selectedEmailId) { e.preventDefault(); forwardEmail(); }
  if ((e.ctrlKey || e.metaKey) && e.key === 'p' && state.selectedEmailId) { e.preventDefault(); printEmail(); }
});

document.addEventListener('DOMContentLoaded', init);