// accounts.js — árvore sidebar, seleção de pasta, modal de contas

let folderCountsData = {};

// ── Folder defs ───────────────────────────────────────────────────────────────

const FOLDER_DEFS = [
  { key: 'inbox',   label: 'Entrada',   icon: '📥', match: f => /^inbox$/i.test(f) },
  { key: 'drafts',  label: 'Rascunhos', icon: '📝', match: f => /draft/i.test(f) },
  { key: 'sent',    label: 'Enviados',  icon: '📤', match: f => /^sent$|sent (mail|items|messages)|\.(sent)$/i.test(f) || /^\[gmail\]\/sent/i.test(f) },
  { key: 'spam',    label: 'Spam',      icon: '🔥', match: f => /spam|junk/i.test(f) },
  { key: 'trash',   label: 'Lixeira',   icon: '🗑',  match: f => /trash|deleted|lixeira/i.test(f) || /^\[gmail\]\/trash/i.test(f) },
  { key: 'archive', label: 'Arquivo',   icon: '📦', match: f => /archive|all mail/i.test(f) || /^\[gmail\]\/all/i.test(f) },
];

function matchFolderDef(folderName) {
  return FOLDER_DEFS.find(d => d.match(folderName)) || null;
}

// ── Carregamento ──────────────────────────────────────────────────────────────

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
  await Promise.all(state.accounts.map(async acc => {
    try {
      const res = await fetch(`/api/folders/counts?account_id=${acc.id}`);
      folderCountsData[acc.id] = await res.json();
    } catch(e) { folderCountsData[acc.id] = {}; }
  }));
}

async function refreshFolderCounts() {
  await loadAllFolderCounts();
  renderAccountTree();
}

// ── Árvore de contas ──────────────────────────────────────────────────────────

function getAccountUnread(accountId) {
  const data = folderCountsData[accountId] || {};
  return Object.entries(data).reduce((s, [folder, c]) => {
    const f = folder.toLowerCase();
    if (['sent', 'draft'].some(kw => f.includes(kw))) return s;
    return s + (c.unread || 0);
  }, 0);
}

function renderAccountTree() {
  const container = document.getElementById('account-tree');
  container.innerHTML = '';

  for (const acc of state.accounts) {
    const counts      = folderCountsData[acc.id] || {};
    const totalUnread = getAccountUnread(acc.id);
    const isCollapsed = settings.collapsedAccounts[acc.id] || false;
    const hasAuthFail = state.authFailed.has(acc.id);
    const hasError    = !!state.syncErrors[acc.id];
    const isSyncing   = state.syncingIds.has(acc.id);
    const forceCollapsed = hasAuthFail || isCollapsed;

    const node = document.createElement('div');
    node.className = 'account-node' + (forceCollapsed ? ' collapsed' : '');
    node.dataset.accId = acc.id;

    // Header
    const header = document.createElement('div');
    header.className = 'account-header'
      + (hasAuthFail ? ' has-auth-error' : hasError ? ' has-error' : '')
      + (isSyncing ? ' is-syncing' : '');
    header.title = hasAuthFail
      ? `Autenticação falhou: ${state.syncErrors[acc.id]}`
      : hasError ? `Erro: ${state.syncErrors[acc.id]}` : '';
    const icon = isSyncing ? '<span class="mini-spinner"></span>'
      : hasAuthFail ? '🔐' : hasError ? '⚠' : '✉';
    header.innerHTML = `
      <span class="account-toggle">▾</span>
      <span class="account-icon">${icon}</span>
      <span class="account-email" title="${esc(acc.email)}">${esc(acc.email)}</span>
      ${totalUnread > 0 && !hasAuthFail ? `<span class="account-badge">${totalUnread}</span>` : ''}
      ${hasAuthFail ? '<span class="auth-fail-badge" title="Clique em ↻ para tentar novamente">senha inválida</span>' : ''}
    `;
    header.onclick = () => toggleAccountCollapse(acc.id, node);

    // Retry button
    if (hasError || hasAuthFail) {
      const retryBtn = document.createElement('button');
      retryBtn.className = 'account-retry-btn';
      retryBtn.textContent = '↻';
      retryBtn.title = hasAuthFail ? 'Tentar novamente com nova senha' : 'Tentar novamente';
      retryBtn.onclick = e => {
        e.stopPropagation();
        state.authFailed.delete(acc.id);
        state.syncErrors[acc.id] = null;
        renderAccountTree();
        fetch(`/api/accounts/${acc.id}/idle-restart`, { method: 'POST' });
      };
      header.appendChild(retryBtn);
    }

    // Pastas
    const foldersDiv = document.createElement('div');
    foldersDiv.className = 'account-folders';
    if (!forceCollapsed) foldersDiv.style.maxHeight = '500px';

    const seen = new Set();
    const realFolderKeys = Object.keys(counts);

    for (const def of FOLDER_DEFS) {
      const matching = realFolderKeys.filter(f => matchFolderDef(f)?.key === def.key);
      matching.forEach(f => seen.add(f));
      const totalC  = matching.reduce((s, f) => s + (counts[f]?.total  || 0), 0);
      const unreadC = matching.reduce((s, f) => s + (counts[f]?.unread || 0), 0);
      if (totalC === 0 && def.key !== 'inbox') continue;
      const realList = matching.length ? matching : (def.key === 'inbox' ? ['INBOX'] : []);
      if (!realList.length) continue;
      const isActive = state.selectedAccountId === acc.id && state.selectedFolder === def.key;
      const folderEl = document.createElement('div');
      folderEl.className = 'folder-item' + (isActive ? ' active' : '');
      folderEl.innerHTML = `
        <span class="folder-icon">${def.icon}</span>
        <span class="folder-name">${def.label}</span>
        ${unreadC > 0 ? `<span class="folder-badge">${unreadC}</span>` : ''}
      `;
      folderEl.onclick = () => selectFolder(acc.id, def.key, realList);
      foldersDiv.appendChild(folderEl);
    }

    for (const real of realFolderKeys) {
      if (seen.has(real)) continue;
      const c = counts[real] || { total: 0, unread: 0 };
      if (c.total === 0) continue;
      const isActive = state.selectedAccountId === acc.id && state.selectedFolder === real;
      const folderEl = document.createElement('div');
      folderEl.className = 'folder-item' + (isActive ? ' active' : '');
      folderEl.innerHTML = `
        <span class="folder-icon">📁</span>
        <span class="folder-name">${esc(real)}</span>
        ${c.unread > 0 ? `<span class="folder-badge">${c.unread}</span>` : ''}
      `;
      folderEl.onclick = () => selectFolder(acc.id, real, [real]);
      foldersDiv.appendChild(folderEl);
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
  settings.collapsedAccounts[accId] = !settings.collapsedAccounts[accId];
  saveSettings();
  node.classList.toggle('collapsed', settings.collapsedAccounts[accId]);
  const foldersDiv = node.querySelector('.account-folders');
  foldersDiv.style.maxHeight = settings.collapsedAccounts[accId] ? '0' : '500px';
}

// ── Seleção de pasta ──────────────────────────────────────────────────────────

function selectFolder(accountId, folderKey, realFolders) {
  state.selectedAccountId   = accountId;
  state.selectedFolder      = folderKey;
  state.selectedFolderReals = realFolders;
  state.selectedEmailId     = null;
  state.selectedIds.clear();
  state.currentPage   = 1;
  state.searchQuery   = '';
  state.imapSearchActive = false;
  const si = document.getElementById('search-input');
  if (si) si.value = '';
  hideEmail();
  renderAccountTree();
  loadEmails();

  if (!state.syncErrors[accountId] && !state.syncingIds.has(accountId)) {
    const elapsed = Date.now() - (state.lastSyncTime[accountId] || 0);
    if (elapsed >= MIN_SYNC_GAP_MS)
      fetch(`/api/accounts/${accountId}/sync`, { method: 'POST' });
  }
}

// ── Modal de contas ───────────────────────────────────────────────────────────

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

function renderComposeAccountSelect() {
  const sel = document.getElementById('compose-account-select');
  sel.innerHTML = state.accounts.map(a =>
    `<option value="${a.id}">${esc(a.name)} &lt;${esc(a.email)}&gt;</option>`
  ).join('');
}

function renderAccountList() {
  const list = document.getElementById('account-list');
  if (!state.accounts.length) {
    list.innerHTML = '<div style="color:var(--muted);font-size:12px;margin-bottom:12px;">Nenhuma conta cadastrada</div>';
    return;
  }
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
  document.getElementById('af-name').value       = a.name;
  document.getElementById('af-email').value      = a.email;
  document.getElementById('af-username').value   = a.username;
  document.getElementById('af-password').value   = '';
  document.getElementById('af-imap-host').value  = a.imap_host;
  document.getElementById('af-imap-port').value  = a.imap_port;
  document.getElementById('af-imap-ssl').checked = a.imap_ssl;
  document.getElementById('af-smtp-host').value  = a.smtp_host;
  document.getElementById('af-smtp-port').value  = a.smtp_port;
  document.getElementById('af-smtp-ssl').checked = a.smtp_ssl;
  document.getElementById('af-smtp-tls').checked = a.smtp_tls;
  document.getElementById('account-form-title').textContent = 'Editar conta';
}

async function deleteAccount(id) {
  if (!confirm('Remover esta conta? Os e-mails serão mantidos.')) return;
  await fetch(`/api/accounts/${id}`, { method: 'DELETE' });
  if (state.selectedAccountId === id) state.selectedAccountId = null;
  await loadAccounts(); loadEmails(); toast('Conta removida', 'success');
}

async function purgeAccount(id) {
  const a = state.accounts.find(x => x.id === id);
  if (!confirm(`Limpar "${a?.name}"? Remove conta E TODOS OS E-MAILS permanentemente.`)) return;
  await fetch(`/api/accounts/${id}/purge`, { method: 'DELETE' });
  if (state.selectedAccountId === id) { state.selectedAccountId = null; hideEmail(); }
  await loadAccounts(); loadEmails(); toast('Conta e e-mails removidos', 'success');
}

function clearAccountForm() {
  ['af-name','af-email','af-username','af-password',
   'af-imap-host','af-imap-port','af-smtp-host','af-smtp-port'].forEach(id =>
    document.getElementById(id).value = '');
  document.getElementById('af-imap-ssl').checked = true;
  document.getElementById('af-smtp-ssl').checked = false;
  document.getElementById('af-smtp-tls').checked = true;
  document.getElementById('af-imap-port').value  = 993;
  document.getElementById('af-smtp-port').value  = 587;
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
  const res = state.editAccountId
    ? await fetch(`/api/accounts/${state.editAccountId}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data) })
    : (() => {
        if (!data.password) { toast('Senha obrigatória para nova conta', 'error'); return null; }
        return fetch('/api/accounts', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data) });
      })();
  if (!res) return;
  if (res.ok) {
    toast('Conta salva!', 'success');
    await loadAccounts(); clearAccountForm();
    state.editAccountId = null;
    document.getElementById('account-form-title').textContent = 'Adicionar conta';
    renderAccountList();
  } else {
    toast((await res.json()).error || 'Erro ao salvar', 'error');
  }
}

async function testAccount() {
  const btn = document.getElementById('af-test-btn');
  btn.textContent = 'Testando...'; btn.disabled = true;
  try {
    if (state.editAccountId) {
      const r = await (await fetch(`/api/accounts/${state.editAccountId}/test`, { method: 'POST' })).json();
      r.ok ? toast('✓ Conexão bem-sucedida!', 'success') : toast(r.errors.join(' | '), 'error');
    } else {
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
      const cr = await fetch('/api/accounts', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data) });
      if (!cr.ok) { toast('Preencha todos os campos para testar', 'error'); return; }
      const acc = await cr.json();
      const r = await (await fetch(`/api/accounts/${acc.id}/test`, { method: 'POST' })).json();
      r.ok ? toast('✓ Conexão bem-sucedida!', 'success') : toast(r.errors.join(' | '), 'error');
      await fetch(`/api/accounts/${acc.id}`, { method: 'DELETE' });
      await loadAccounts();
    }
  } finally {
    btn.textContent = '🔌 Testar'; btn.disabled = false;
  }
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
