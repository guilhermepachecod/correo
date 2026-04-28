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
  attachments: [], // files for compose
};

// ── Socket.IO ──────────────────────────────────────────────────────────────────
const socket = io();
socket.on('sync_start', d => toast(`↺ Sincronizando ${d.email}...`, 'info'));
socket.on('sync_done',  d => { toast(`✓ ${d.email} sincronizado`, 'success'); loadEmails(); updateFolderCounts(); });
socket.on('sync_error', d => toast(`Erro: ${d.error}`, 'error'));
socket.on('new_email',  d => { if (d.account_id === state.selectedAccountId || !state.selectedAccountId) { loadEmails(); updateFolderCounts(); } });

// ── Init ───────────────────────────────────────────────────────────────────────
async function init() {
  await loadAccounts();
  await loadEmails();
  updateFolderCounts();
}

// ── Accounts ───────────────────────────────────────────────────────────────────
async function loadAccounts() {
  const res = await fetch('/api/accounts');
  state.accounts = await res.json();
  renderAccountSelect();
  renderComposeAccountSelect();
  renderAccountList();
}

function renderAccountSelect() {
  const sel = document.getElementById('account-select');
  const cur = sel.value;
  sel.innerHTML = '<option value="">Todas as contas</option>';
  for (const a of state.accounts) {
    const opt = document.createElement('option');
    opt.value = a.id;
    opt.textContent = a.email;
    sel.appendChild(opt);
  }
  sel.value = cur;
}

function renderComposeAccountSelect() {
  const sel = document.getElementById('compose-account-select');
  sel.innerHTML = '';
  for (const a of state.accounts) {
    const opt = document.createElement('option');
    opt.value = a.id;
    opt.textContent = `${a.name} <${a.email}>`;
    sel.appendChild(opt);
  }
}

function onAccountChange() {
  const v = document.getElementById('account-select').value;
  state.selectedAccountId = v ? parseInt(v) : null;
  state.selectedFolder = 'INBOX';
  state.selectedEmailId = null;
  state.currentPage = 1;
  loadEmails();
  updateFolderCounts();
  renderFolders();
  hideEmail();
}

// ── Folders ────────────────────────────────────────────────────────────────────
const FOLDER_ICONS = {
  inbox: '📥', sent: '📤', drafts: '📝', trash: '🗑', junk: '🚫', spam: '🚫', starred: '⭐',
};
function folderIcon(name) {
  const key = name.toLowerCase();
  for (const [k, v] of Object.entries(FOLDER_ICONS)) {
    if (key.includes(k)) return v;
  }
  return '📁';
}

let folderCounts = {};
async function updateFolderCounts() {
  const params = state.selectedAccountId ? `?account_id=${state.selectedAccountId}` : '';
  const res = await fetch(`/api/folders/counts${params}`);
  folderCounts = await res.json();
  renderFolders();
}

const COMMON_FOLDERS = ['INBOX', 'Sent', 'Drafts', 'Trash', 'Junk', 'Spam'];
async function renderFolders() {
  const container = document.getElementById('folders');
  let folders = [...COMMON_FOLDERS];

  for (const f of Object.keys(folderCounts)) {
    if (!folders.includes(f)) folders.push(f);
  }

  container.innerHTML = '';
  for (const f of folders) {
    const counts = folderCounts[f] || { total: 0, unread: 0 };
    if (counts.total === 0 && !COMMON_FOLDERS.includes(f)) continue;
    const item = document.createElement('div');
    item.className = 'folder-item' + (state.selectedFolder === f ? ' active' : '');
    item.innerHTML = `
      <span class="folder-icon">${folderIcon(f)}</span>
      <span class="folder-name">${f}</span>
      ${counts.unread > 0 ? `<span class="folder-badge">${counts.unread}</span>` : ''}
    `;
    item.onclick = () => selectFolder(f);
    container.appendChild(item);
  }
}

function selectFolder(folder) {
  state.selectedFolder = folder;
  state.currentPage = 1;
  state.selectedEmailId = null;
  hideEmail();
  loadEmails();
  renderFolders();
}

// ── Email list ─────────────────────────────────────────────────────────────────
async function loadEmails() {
  const loading = document.getElementById('list-loading');
  const empty = document.getElementById('list-empty');
  loading.style.display = 'flex';
  empty.style.display = 'none';

  const params = new URLSearchParams({
    folder: state.selectedFolder,
    page: state.currentPage,
    per_page: 50,
  });
  if (state.selectedAccountId) params.set('account_id', state.selectedAccountId);
  if (state.searchQuery) params.set('search', state.searchQuery);

  try {
    const res = await fetch(`/api/emails?${params}`);
    const data = await res.json();
    state.emails = data.emails;
    state.totalPages = data.pages;
    renderEmailList();
  } catch (e) {
    toast('Erro ao carregar e-mails', 'error');
  } finally {
    loading.style.display = 'none';
  }
}

function renderEmailList() {
  const container = document.getElementById('email-list');
  const empty = document.getElementById('list-empty');
  container.querySelectorAll('.email-item').forEach(e => e.remove());

  if (state.emails.length === 0) {
    empty.style.display = 'flex';
    return;
  }
  empty.style.display = 'none';

  for (const email of state.emails) {
    const item = document.createElement('div');
    item.className = 'email-item' + (email.is_read ? '' : ' unread') + (email.id === state.selectedEmailId ? ' active' : '');
    item.dataset.id = email.id;
    item.innerHTML = `
      <div class="ei-row1">
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
    item.onclick = () => openEmail(email.id);
    container.appendChild(item);
  }
}

// ── Email view ─────────────────────────────────────────────────────────────────
async function openEmail(id) {
  state.selectedEmailId = id;
  document.querySelectorAll('.email-item').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset.id) === id);
  });

  const res = await fetch(`/api/emails/${id}`);
  const email = await res.json();

  const listEmail = state.emails.find(e => e.id === id);
  if (listEmail) { listEmail.is_read = true; renderEmailList(); }

  document.getElementById('reading-empty').style.display = 'none';
  const view = document.getElementById('email-view');
  view.style.display = 'flex';

  document.getElementById('ev-subject').textContent = email.subject;
  const initials = (email.from_name || email.from_addr || '?').slice(0, 1).toUpperCase();
  document.getElementById('ev-avatar').textContent = initials;
  document.getElementById('ev-from-name').textContent = email.from_name || email.from_addr;
  document.getElementById('ev-from-addr').textContent = `<${email.from_addr}>`;
  document.getElementById('ev-to-line').textContent = email.to_addr ? `Para: ${email.to_addr}` : '';
  document.getElementById('ev-date').textContent = fmtDateFull(email.date);

  const starBtn = document.getElementById('ev-star-btn');
  starBtn.textContent = email.is_starred ? '★ Marcado' : '☆ Marcar';
  starBtn.style.color = email.is_starred ? 'var(--amber)' : '';
  starBtn._emailId = id;
  starBtn._starred = email.is_starred;

  const attDiv = document.getElementById('ev-attachments');
  if (email.attachments && email.attachments.length > 0) {
    attDiv.style.display = 'flex';
    attDiv.innerHTML = email.attachments.map(a =>
      `<a class="att-chip" href="/api/attachments/${a.id}" target="_blank">
        <span class="att-icon">📎</span>
        <span>${esc(a.filename)}</span>
        <span class="att-size">${fmtSize(a.size)}</span>
       </a>`
    ).join('');
  } else {
    attDiv.style.display = 'none';
    attDiv.innerHTML = '';
  }

  const bodyHtml = document.getElementById('ev-body-html');
  const bodyText = document.getElementById('ev-body-text');
  if (email.body_html) {
    bodyHtml.style.display = 'block';
    bodyText.style.display = 'none';
    const clean = email.body_html.replace(/<script[\s\S]*?<\/script>/gi, '')
                                  .replace(/on\w+="[^"]*"/gi, '');
    bodyHtml.innerHTML = clean;
  } else {
    bodyHtml.style.display = 'none';
    bodyText.style.display = 'block';
    bodyText.textContent = email.body_text || '';
  }

  view._email = email;
}

function hideEmail() {
  document.getElementById('reading-empty').style.display = 'flex';
  document.getElementById('email-view').style.display = 'none';
}

async function toggleStar() {
  const btn = document.getElementById('ev-star-btn');
  const id = btn._emailId || state.selectedEmailId;
  if (!id) return;
  const res = await fetch(`/api/emails/${id}/star`, { method: 'POST' });
  const data = await res.json();
  btn._starred = data.starred;
  btn.textContent = data.starred ? '★ Marcado' : '☆ Marcar';
  btn.style.color = data.starred ? 'var(--amber)' : '';
  const listEmail = state.emails.find(e => e.id === id);
  if (listEmail) { listEmail.is_starred = data.starred; renderEmailList(); }
}

async function deleteEmail() {
  if (!state.selectedEmailId) return;
  if (!confirm('Excluir este e-mail?')) return;
  await fetch(`/api/emails/${state.selectedEmailId}`, { method: 'DELETE' });
  state.emails = state.emails.filter(e => e.id !== state.selectedEmailId);
  state.selectedEmailId = null;
  hideEmail();
  renderEmailList();
  updateFolderCounts();
  toast('E-mail excluído', 'success');
}

// ── Compose ────────────────────────────────────────────────────────────────────
function openCompose(opts = {}) {
  state.replyToId = opts.replyToId || null;
  state.attachments = [];
  document.getElementById('compose-to').value = opts.to || '';
  document.getElementById('compose-cc').value = opts.cc || '';
  document.getElementById('compose-subject').value = opts.subject || '';
  document.getElementById('compose-body').value = opts.body || '';
  document.getElementById('compose-attach-list').innerHTML = '';
  document.getElementById('compose-title').textContent = opts.title || 'Nova mensagem';
  document.getElementById('compose-modal').classList.add('open');
  setTimeout(() => document.getElementById('compose-to').focus(), 50);
}

function closeCompose() {
  document.getElementById('compose-modal').classList.remove('open');
  state.replyToId = null;
  state.attachments = [];
}

function replyEmail() {
  const view = document.getElementById('email-view');
  const email = view._email;
  if (!email) return;
  const replyTo = email.reply_to || email.from_addr;
  const subject = email.subject.startsWith('Re:') ? email.subject : `Re: ${email.subject}`;
  const quote = `\n\n--- Mensagem original ---\nDe: ${email.from_addr}\nData: ${fmtDateFull(email.date)}\n\n${email.body_text || ''}`;
  openCompose({ to: replyTo, subject, body: quote, replyToId: email.id, title: 'Responder' });
}

function addAttachments(input) {
  for (const file of input.files) {
    state.attachments.push(file);
  }
  renderAttachList();
  input.value = '';
}

function renderAttachList() {
  const list = document.getElementById('compose-attach-list');
  list.innerHTML = state.attachments.map((f, i) =>
    `<span class="compose-att">${esc(f.name)} <span class="compose-att-remove" onclick="removeAtt(${i})">×</span></span>`
  ).join('');
}

function removeAtt(i) {
  state.attachments.splice(i, 1);
  renderAttachList();
}

async function sendEmail() {
  const btn = document.getElementById('compose-send-btn');
  btn.disabled = true;
  btn.textContent = 'Enviando...';

  const fd = new FormData();
  fd.append('account_id', document.getElementById('compose-account-select').value);
  fd.append('to', document.getElementById('compose-to').value);
  fd.append('cc', document.getElementById('compose-cc').value);
  fd.append('subject', document.getElementById('compose-subject').value);
  fd.append('body', document.getElementById('compose-body').value);
  if (state.replyToId) fd.append('reply_to_id', state.replyToId);
  for (const file of state.attachments) fd.append('attachments', file);

  try {
    const res = await fetch('/api/emails/send', { method: 'POST', body: fd });
    const data = await res.json();
    if (data.ok) {
      toast('E-mail enviado!', 'success');
      closeCompose();
    } else {
      toast(`Erro: ${data.error}`, 'error');
    }
  } catch (e) {
    toast('Erro ao enviar', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Enviar';
  }
}

// ── Sync ───────────────────────────────────────────────────────────────────────
async function syncAll() {
  const btn = document.getElementById('sync-btn');
  btn.classList.add('spinning');
  await fetch('/api/sync/all', { method: 'POST' });
  setTimeout(() => btn.classList.remove('spinning'), 2000);
}

async function syncCurrent() {
  const btn = document.getElementById('sync-btn');
  btn.classList.add('spinning');
  if (state.selectedAccountId) {
    await fetch(`/api/accounts/${state.selectedAccountId}/sync`, { method: 'POST' });
  } else {
    await fetch('/api/sync/all', { method: 'POST' });
  }
  setTimeout(() => btn.classList.remove('spinning'), 2000);
}

// ── Sync .conf ─────────────────────────────────────────────────────────────────
async function syncConf() {
  const btn = document.getElementById('sync-conf-btn');
  btn.disabled = true;
  btn.textContent = '⟳ Sincronizando...';
  try {
    const res = await fetch('/api/accounts/sync-conf', { method: 'POST' });
    const data = await res.json();
    const parts = [];
    if (data.created.length) parts.push(`${data.created.length} criada(s)`);
    if (data.updated.length) parts.push(`${data.updated.length} atualizada(s)`);
    if (data.deleted.length) parts.push(`${data.deleted.length} removida(s)`);
    if (data.errors.length) {
      toast(`Erros: ${data.errors.join(' | ')}`, 'error');
    } else {
      toast(parts.length ? parts.join(', ') : 'Nenhuma alteração', 'success');
    }
    await loadAccounts();
  } catch (e) {
    toast('Erro ao sincronizar contas', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '⟳ Ler arquivos .conf';
  }
}

// ── Search ─────────────────────────────────────────────────────────────────────
let searchTimer;
function debounceSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.searchQuery = document.getElementById('search-input').value.trim();
    state.currentPage = 1;
    loadEmails();
  }, 400);
}

// ── Account modal ──────────────────────────────────────────────────────────────
function openAccountModal() {
  state.editAccountId = null;
  clearAccountForm();
  document.getElementById('account-form-title').textContent = 'Adicionar conta';
  renderAccountList();
  document.getElementById('account-modal').classList.add('open');
}

function closeAccountModal() {
  document.getElementById('account-modal').classList.remove('open');
}

function renderAccountList() {
  const list = document.getElementById('account-list');
  if (!state.accounts.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:13px;margin-bottom:12px;">Nenhuma conta cadastrada</div>';
    return;
  }
  list.innerHTML = state.accounts.map(a => `
    <div class="account-list-item">
      <div class="account-list-info">
        <div class="account-list-name">${esc(a.name)}</div>
        <div class="account-list-email">${esc(a.email)}</div>
        ${a.conf_file ? `<div class="account-list-conf">📄 ${esc(a.conf_file)}</div>` : ''}
      </div>
      <button class="btn btn-ghost" style="padding:5px 10px;font-size:12px;" onclick="editAccount(${a.id})">Editar</button>
      <button class="btn btn-danger" style="padding:5px 10px;font-size:12px;" onclick="deleteAccount(${a.id})">Remover</button>
      <button class="btn btn-purge" style="padding:5px 10px;font-size:12px;" onclick="purgeAccount(${a.id})" title="Remove conta e todos os e-mails">🗑 Limpar</button>
    </div>
  `).join('');
}

function editAccount(id) {
  const a = state.accounts.find(x => x.id === id);
  if (!a) return;
  state.editAccountId = id;
  document.getElementById('af-name').value = a.name;
  document.getElementById('af-email').value = a.email;
  document.getElementById('af-username').value = a.username;
  document.getElementById('af-password').value = '';
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
  if (state.selectedAccountId === id) {
    state.selectedAccountId = null;
    document.getElementById('account-select').value = '';
  }
  loadEmails();
  toast('Conta removida', 'success');
}

async function purgeAccount(id) {
  const a = state.accounts.find(x => x.id === id);
  if (!confirm(`Limpar "${a?.name}"? Isso removerá a conta E TODOS OS E-MAILS permanentemente.`)) return;
  await fetch(`/api/accounts/${id}/purge`, { method: 'DELETE' });
  await loadAccounts();
  if (state.selectedAccountId === id) {
    state.selectedAccountId = null;
    document.getElementById('account-select').value = '';
    hideEmail();
  }
  loadEmails();
  updateFolderCounts();
  toast('Conta e e-mails removidos', 'success');
}

function clearAccountForm() {
  ['af-name','af-email','af-username','af-password','af-imap-host','af-imap-port','af-smtp-host','af-smtp-port'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('af-imap-ssl').checked = true;
  document.getElementById('af-smtp-ssl').checked = false;
  document.getElementById('af-smtp-tls').checked = true;
  document.getElementById('af-imap-port').value = 993;
  document.getElementById('af-smtp-port').value = 587;
}

async function saveAccount() {
  const data = {
    name: document.getElementById('af-name').value.trim(),
    email: document.getElementById('af-email').value.trim(),
    username: document.getElementById('af-username').value.trim(),
    password: document.getElementById('af-password').value,
    imap_host: document.getElementById('af-imap-host').value.trim(),
    imap_port: parseInt(document.getElementById('af-imap-port').value),
    imap_ssl: document.getElementById('af-imap-ssl').checked,
    smtp_host: document.getElementById('af-smtp-host').value.trim(),
    smtp_port: parseInt(document.getElementById('af-smtp-port').value),
    smtp_ssl: document.getElementById('af-smtp-ssl').checked,
    smtp_tls: document.getElementById('af-smtp-tls').checked,
  };
  if (!data.name || !data.email || !data.imap_host || !data.smtp_host) {
    toast('Preencha todos os campos obrigatórios', 'error'); return;
  }
  let res;
  if (state.editAccountId) {
    res = await fetch(`/api/accounts/${state.editAccountId}`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) });
  } else {
    if (!data.password) { toast('Senha obrigatória para nova conta', 'error'); return; }
    res = await fetch('/api/accounts', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) });
  }
  if (res.ok) {
    toast('Conta salva!', 'success');
    await loadAccounts();
    clearAccountForm();
    state.editAccountId = null;
    document.getElementById('account-form-title').textContent = 'Adicionar conta';
    renderAccountList();
  } else {
    const err = await res.json();
    toast(err.error || 'Erro ao salvar', 'error');
  }
}

async function testAccount() {
  const btn = document.getElementById('af-test-btn');
  btn.textContent = 'Testando...';
  btn.disabled = true;

  const data = {
    name: document.getElementById('af-name').value.trim(),
    email: document.getElementById('af-email').value.trim(),
    username: document.getElementById('af-username').value.trim(),
    password: document.getElementById('af-password').value,
    imap_host: document.getElementById('af-imap-host').value.trim(),
    imap_port: parseInt(document.getElementById('af-imap-port').value),
    imap_ssl: document.getElementById('af-imap-ssl').checked,
    smtp_host: document.getElementById('af-smtp-host').value.trim(),
    smtp_port: parseInt(document.getElementById('af-smtp-port').value),
    smtp_ssl: document.getElementById('af-smtp-ssl').checked,
    smtp_tls: document.getElementById('af-smtp-tls').checked,
  };

  if (state.editAccountId) {
    const res = await fetch(`/api/accounts/${state.editAccountId}/test`, { method: 'POST' });
    const result = await res.json();
    showTestResult(result);
  } else {
    const createRes = await fetch('/api/accounts', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) });
    if (!createRes.ok) { toast('Preencha todos os campos para testar', 'error'); btn.textContent = '🔌 Testar'; btn.disabled = false; return; }
    const acc = await createRes.json();
    const testRes = await fetch(`/api/accounts/${acc.id}/test`, { method: 'POST' });
    const result = await testRes.json();
    await fetch(`/api/accounts/${acc.id}`, { method: 'DELETE' });
    showTestResult(result);
    await loadAccounts();
  }
  btn.textContent = '🔌 Testar';
  btn.disabled = false;
}

function showTestResult(result) {
  if (result.ok) {
    toast('✓ Conexão bem-sucedida!', 'success');
  } else {
    toast(result.errors.join(' | '), 'error');
  }
}

// ── Utilities ──────────────────────────────────────────────────────────────────
function esc(str) {
  if (!str) return '';
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  }
  if (now - d < 7 * 86400000) {
    return d.toLocaleDateString('pt-BR', { weekday: 'short' });
  }
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
}

function fmtDateFull(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString('pt-BR', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function fmtSize(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes/1024).toFixed(1)}KB`;
  return `${(bytes/1024/1024).toFixed(1)}MB`;
}

let toastTimer;
function toast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = '', 3500);
}

// close modals on backdrop click
document.getElementById('compose-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('compose-modal')) closeCompose();
});
document.getElementById('account-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('account-modal')) closeAccountModal();
});

// keyboard shortcuts
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeCompose(); closeAccountModal(); }
  if ((e.ctrlKey || e.metaKey) && e.key === 'n') { e.preventDefault(); openCompose(); }
  if ((e.ctrlKey || e.metaKey) && e.key === 'r' && state.selectedEmailId) { e.preventDefault(); replyEmail(); }
});

// ── Start ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);