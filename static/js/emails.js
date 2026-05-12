// emails.js — lista de e-mails, sort, diff animado, bulk, scroll infinito

// ── Sort ──────────────────────────────────────────────────────────────────────

function setSortBy(col) {
  if (state.sortBy === col) state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
  else { state.sortBy = col; state.sortDir = col === 'date' ? 'desc' : 'asc'; }
  updateSortArrows();
  state.emails = sortEmails(state.emails);
  renderEmailList();
}

function updateSortArrows() {
  ['from', 'subject', 'date'].forEach(col => {
    const el  = document.getElementById(`sort-${col}-arrow`);
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
      va = new Date(a.date).getTime(); vb = new Date(b.date).getTime();
    } else if (state.sortBy === 'from') {
      va = (a.from_name || a.from_addr || '').toLowerCase();
      vb = (b.from_name || b.from_addr || '').toLowerCase();
    } else {
      va = (a.subject || '').toLowerCase();
      vb = (b.subject || '').toLowerCase();
    }
    if (va < vb) return state.sortDir === 'asc' ? -1 : 1;
    if (va > vb) return state.sortDir === 'asc' ?  1 : -1;
    return 0;
  });
}

// ── Carregamento ──────────────────────────────────────────────────────────────

function _initScrollInfinite() {
  const list = document.getElementById('email-list');
  if (!list) return;
  list.addEventListener('scroll', () => {
    if (!state.hasMore || state.isLoadingMore) return;
    if (list.scrollHeight - list.scrollTop - list.clientHeight < 120) {
      state.currentPage++;
      loadEmails(true);
    }
  });
}

async function loadEmails(append = false) {
  const loading = document.getElementById('list-loading');
  const empty   = document.getElementById('list-empty');

  if (!append) {
    state.currentPage = 1;
    state.hasMore = false;
    loading.style.display = 'flex';
    empty.style.display = 'none';
  } else {
    if (state.isLoadingMore || !state.hasMore) return;
    state.isLoadingMore = true;
    _showBottomSpinner(true);
  }

  const accountId = state.selectedAccountId;
  const folders   = state.selectedFolderReals?.length ? state.selectedFolderReals : ['INBOX'];
  const PER_PAGE  = 50;

  try {
    let all = [], anyHasMore = false;
    for (const folder of folders) {
      const params = new URLSearchParams({ folder, page: state.currentPage, per_page: PER_PAGE });
      if (accountId) params.set('account_id', String(accountId));
      if (state.searchQuery) params.set('search', state.searchQuery);
      const data = await (await fetch(`/api/emails?${params}`)).json();
      all = all.concat(data.emails);
      if (data.page < data.pages) anyHasMore = true;
    }
    state.hasMore = anyHasMore;
    const sorted  = sortEmails(all);

    if (append) {
      const existingIds = new Set(state.emails.map(e => e.id));
      const fresh = sorted.filter(e => !existingIds.has(e.id));
      state.emails = state.emails.concat(fresh);
      _appendEmailItems(fresh);
    } else {
      state.totalEmails = sorted.length;
      _applyEmailDiff(sorted);
    }
    _updateEmailCountBar();
  } catch(e) {
    toast('Erro ao carregar e-mails', 'error');
  } finally {
    loading.style.display = 'none';
    state.isLoadingMore = false;
    _showBottomSpinner(false);
  }
}

function _updateEmailCountBar() {
  const bar = document.getElementById('email-count-bar');
  if (!bar) return;
  if (!state.emails.length) { bar.textContent = ''; return; }
  bar.textContent = state.hasMore
    ? `Mostrando ${state.emails.length} e-mails — role para carregar mais`
    : `${state.emails.length} e-mail${state.emails.length !== 1 ? 's' : ''}`;
}

function _showBottomSpinner(show) {
  let el = document.getElementById('list-bottom-spinner');
  if (!el) {
    el = document.createElement('div');
    el.id = 'list-bottom-spinner';
    el.style.cssText = 'display:none;justify-content:center;padding:10px;color:var(--muted);font-size:12px;gap:8px;align-items:center;';
    el.innerHTML = '<div class="spinner"></div> Carregando mais...';
    document.getElementById('email-list').appendChild(el);
  }
  el.style.display = show ? 'flex' : 'none';
}

// ── Diff animado ──────────────────────────────────────────────────────────────

function _applyEmailDiff(newEmails) {
  const oldMap    = new Map(state.emails.map(e => [e.id, e]));
  const newMap    = new Map(newEmails.map(e => [e.id, e]));
  state.emails    = newEmails;
  const container = document.getElementById('email-list');
  const empty     = document.getElementById('list-empty');

  if (!newEmails.length) {
    container.querySelectorAll('.email-item').forEach(el => el.remove());
    empty.style.display = 'flex';
    renderBulkBar();
    return;
  }
  empty.style.display = 'none';

  // Remove os que saíram
  container.querySelectorAll('.email-item').forEach(el => {
    if (!newMap.has(parseInt(el.dataset.id))) {
      el.classList.add('removing');
      setTimeout(() => el.remove(), 200);
    }
  });

  // Insere/atualiza na ordem correta
  let prevEl = null;
  for (const email of newEmails) {
    let item = container.querySelector(`.email-item[data-id="${email.id}"]`);
    if (!item) {
      item = _buildEmailItem(email);
      item.classList.add('appearing');
      if (prevEl) prevEl.insertAdjacentElement('afterend', item);
      else container.prepend(item);
      requestAnimationFrame(() => requestAnimationFrame(() => item.classList.remove('appearing')));
    } else {
      const old = oldMap.get(email.id);
      if (old) {
        if (old.is_read !== email.is_read) {
          item.classList.toggle('unread', !email.is_read);
          const dot = item.querySelector('.dot-unread, .dot-read');
          if (dot) dot.className = email.is_read ? 'dot-read' : 'dot-unread';
        }
        if (old.is_starred !== email.is_starred) {
          const star = item.querySelector('.icon-star');
          if (star) star.classList.toggle('starred', email.is_starred);
        }
      }
      if (prevEl && prevEl.nextElementSibling !== item)
        prevEl.insertAdjacentElement('afterend', item);
    }
    item.classList.toggle('active',   email.id === state.selectedEmailId);
    item.classList.toggle('selected', state.selectedIds.has(email.id));
    prevEl = item;
  }
  renderBulkBar();
}

function _appendEmailItems(emails) {
  const container = document.getElementById('email-list');
  const spinner   = document.getElementById('list-bottom-spinner');
  for (const email of emails) {
    const item = _buildEmailItem(email);
    if (spinner) container.insertBefore(item, spinner);
    else container.appendChild(item);
  }
  renderBulkBar();
}

// ── Construção de item ────────────────────────────────────────────────────────

function _buildEmailItem(email) {
  const hasThread = !!(email.thread_id);
  const item = document.createElement('div');
  item.className = 'email-item'
    + (email.is_read ? '' : ' unread')
    + (email.id === state.selectedEmailId ? ' active' : '')
    + (state.selectedIds.has(email.id) ? ' selected' : '');
  item.dataset.id = email.id;
  if (email.thread_id) item.dataset.threadId = email.thread_id;
  item.innerHTML = `
    <div class="ei-check-col">
      <input type="checkbox" class="ei-checkbox" ${state.selectedIds.has(email.id) ? 'checked' : ''}
        onclick="toggleSelect(event,${email.id})">
    </div>
    <div class="ei-icons-col">
      ${email.is_read ? '<span class="dot-read"></span>' : '<span class="dot-unread"></span>'}
      <span class="icon-star ${email.is_starred ? 'starred' : ''}" onclick="quickStar(event,${email.id})">★</span>
      ${email.has_attachments ? '<span class="icon-attach">📎</span>' : ''}
    </div>
    <div class="ei-from">${esc(email.from_name || email.from_addr)}</div>
    <div class="ei-subject">
      ${esc(email.subject)}
      ${hasThread ? '<span class="thread-indicator" title="Ver conversa">💬</span>' : ''}
    </div>
    <div class="ei-date">${fmtDate(email.date)}</div>
  `;
  item.onclick = e => {
    if (e.target.classList.contains('ei-checkbox') ||
        e.target.classList.contains('icon-star')) return;
    if (e.target.classList.contains('thread-indicator')) { openThread(email); return; }
    openEmail(email.id);
  };
  return item;
}

function renderEmailList() {
  const container = document.getElementById('email-list');
  const empty     = document.getElementById('list-empty');
  container.querySelectorAll('.email-item').forEach(e => e.remove());
  if (!state.emails.length) { empty.style.display = 'flex'; return; }
  empty.style.display = 'none';
  for (const email of state.emails) container.appendChild(_buildEmailItem(email));
  renderBulkBar();
}

// ── Patch cirúrgico ───────────────────────────────────────────────────────────

function _patchEmailItem(emailId, changes) {
  const item = document.querySelector(`.email-item[data-id="${emailId}"]`);
  if (!item) return;
  if ('is_read' in changes) {
    item.classList.toggle('unread', !changes.is_read);
    const dot = item.querySelector('.dot-unread, .dot-read');
    if (dot) dot.className = changes.is_read ? 'dot-read' : 'dot-unread';
  }
  if ('is_starred' in changes) {
    const star = item.querySelector('.icon-star');
    if (star) star.classList.toggle('starred', changes.is_starred);
  }
}

// ── Quick star ────────────────────────────────────────────────────────────────

async function quickStar(e, id) {
  e.stopPropagation();
  const data = await (await fetch(`/api/emails/${id}/star`, { method: 'POST' })).json();
  const le = state.emails.find(x => x.id === id);
  if (le) { le.is_starred = data.starred; _patchEmailItem(id, { is_starred: data.starred }); }
  if (state.selectedEmailId === id) {
    const btn = document.getElementById('ev-star-btn');
    if (btn) { btn.textContent = data.starred ? '★ Marcado' : '☆ Marcar'; btn.style.color = data.starred ? 'var(--amber)' : ''; }
  }
}

// ── Seleção / Bulk ────────────────────────────────────────────────────────────

function toggleSelect(e, id) {
  e.stopPropagation();
  if (state.selectedIds.has(id)) state.selectedIds.delete(id);
  else state.selectedIds.add(id);
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

async function bulkAction(action) {
  const ids = [...state.selectedIds]; if (!ids.length) return;
  const L = { delete: 'Excluir', mark_spam: 'Marcar como spam',
              mark_read: 'Marcar como lido', mark_unread: 'Marcar como não lido',
              mark_not_spam: 'Desmarcar spam' };
  if (['delete', 'mark_spam'].includes(action) && !confirm(`${L[action]} ${ids.length} e-mail(s)?`)) return;

  const idSet = new Set(ids);
  if (['delete', 'mark_spam'].includes(action)) {
    state.emails = state.emails.filter(e => !idSet.has(e.id));
    if (idSet.has(state.selectedEmailId)) { hideEmail(); state.selectedEmailId = null; }
  } else if (action === 'mark_read') {
    state.emails.filter(e => idSet.has(e.id)).forEach(e => { e.is_read = true; _patchEmailItem(e.id, { is_read: true }); });
  } else if (action === 'mark_unread') {
    state.emails.filter(e => idSet.has(e.id)).forEach(e => { e.is_read = false; _patchEmailItem(e.id, { is_read: false }); });
  }
  state.selectedIds.clear();
  renderEmailList();
  refreshFolderCounts();
  toast(`${L[action]}: ${ids.length} e-mail(s)`, 'success');
  fetch('/api/emails/bulk', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, ids, folder: state.selectedFolder, account_id: state.selectedAccountId }),
  }).catch(() => {});
}

async function bulkActionAll(action) {
  if (!state.selectedAccountId) { toast('Selecione uma conta primeiro', 'error'); return; }
  const L = { mark_read: 'Marcar todos como lido', delete: 'Excluir todos' };
  if (!confirm(`${L[action]} na pasta atual?`)) return;
  const folders = state.selectedFolderReals?.length ? state.selectedFolderReals : ['INBOX'];
  const count = state.emails.length;
  if (action === 'delete') {
    state.emails = []; hideEmail(); state.selectedEmailId = null; renderEmailList();
  } else if (action === 'mark_read') {
    state.emails.forEach(e => { e.is_read = true; _patchEmailItem(e.id, { is_read: true }); });
  }
  state.selectedIds.clear();
  refreshFolderCounts();
  toast(`${L[action]}: ${count} e-mail(s)`, 'success');
  for (const folder of folders) {
    fetch('/api/emails/bulk', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, ids: [], folder, account_id: state.selectedAccountId }),
    }).catch(() => {});
  }
}

// ── Busca IMAP no servidor ────────────────────────────────────────────────────

let searchTimer;
function debounceSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.searchQuery = document.getElementById('search-input').value.trim();
    state.currentPage = 1;
    loadEmails();
  }, 400);
}

async function searchOnServer() {
  if (!state.selectedAccountId || !state.searchQuery) {
    toast('Selecione uma conta e digite um termo', 'error'); return;
  }
  const folders = state.selectedFolderReals || ['INBOX'];
  state.imapSearchActive = true;
  _setImapSearchUI(true);
  for (const folder of folders) {
    await fetch(`/api/accounts/${state.selectedAccountId}/search-imap`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: state.searchQuery, folder }),
    });
  }
}

function _setImapSearchUI(loading) {
  const btn = document.getElementById('imap-search-btn');
  if (!btn) return;
  btn.disabled = loading;
  btn.textContent = loading ? '⟳' : '🔍 Servidor';
  btn.classList.toggle('spinning', loading);
}

// ── Sync ──────────────────────────────────────────────────────────────────────

async function syncAll() {
  const btn = document.getElementById('sync-all-btn');
  btn.classList.add('spinning');
  for (const acc of state.accounts) {
    await fetch(`/api/accounts/${acc.id}/idle-restart`, { method: 'POST' });
  }
  setTimeout(() => { loadEmails(); refreshFolderCounts(); btn.classList.remove('spinning'); }, 4000);
}

async function syncCurrent() {
  const btn = document.getElementById('sync-btn');
  btn.classList.add('spinning');
  if (state.selectedAccountId) {
    await fetch(`/api/accounts/${state.selectedAccountId}/idle-restart`, { method: 'POST' });
    setTimeout(() => { loadEmails(); refreshFolderCounts(); btn.classList.remove('spinning'); }, 3000);
  } else {
    setTimeout(() => btn.classList.remove('spinning'), 2000);
  }
}

// ── Navegação j/k ─────────────────────────────────────────────────────────────

function _navigateEmail(dir) {
  if (!state.emails.length) return;
  const idx = state.emails.findIndex(e => e.id === state.selectedEmailId);
  let next;
  if (idx === -1) next = dir === 'next' ? 0 : state.emails.length - 1;
  else next = dir === 'next' ? Math.min(idx + 1, state.emails.length - 1) : Math.max(idx - 1, 0);
  const email = state.emails[next];
  openEmail(email.id);
  document.querySelector(`.email-item[data-id="${email.id}"]`)
    ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}
