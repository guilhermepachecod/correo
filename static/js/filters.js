// filters.js — modal de filtros/regras por conta

let _editFilterId = null;

// ── Abrir / fechar ────────────────────────────────────────────────────────────

function openFiltersModal() {
  const sel = document.getElementById('filter-account-select');
  sel.innerHTML = state.accounts.map(a =>
    `<option value="${a.id}">${esc(a.name)} &lt;${esc(a.email)}&gt;</option>`
  ).join('');
  if (state.selectedAccountId) sel.value = state.selectedAccountId;
  closeFilterForm();
  loadFilters();
  document.getElementById('filters-modal').classList.add('open');
}

function closeFiltersModal() {
  document.getElementById('filters-modal').classList.remove('open');
  closeFilterForm();
}

// ── Listar ────────────────────────────────────────────────────────────────────

async function loadFilters() {
  const accountId = document.getElementById('filter-account-select').value;
  if (!accountId) return;
  _loadFolderOptionsForFilter(accountId);
  try {
    const filters = await (await api(`/api/accounts/${accountId}/filters`)).json();
    _renderFilterList(filters, accountId);
  } catch(e) { toast('Erro ao carregar filtros', 'error'); }
}

function _loadFolderOptionsForFilter(accountId) {
  const sel     = document.getElementById('ff-action-folder');
  const counts  = folderCountsData[accountId] || {};
  const folders = Object.keys(counts);
  sel.innerHTML = '<option value="">— não mover —</option>' +
    folders.map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
}

function _renderFilterList(filters, accountId) {
  const list = document.getElementById('filter-list');
  if (!filters.length) {
    list.innerHTML = '<div class="filter-empty">Nenhum filtro criado. Clique em "+ Novo filtro" para começar.</div>';
    return;
  }
  list.innerHTML = filters.map(f => {
    const conds = [
      f.cond_from    && `De: ${f.cond_from}`,
      f.cond_to      && `Para: ${f.cond_to}`,
      f.cond_subject && `Assunto: ${f.cond_subject}`,
      f.cond_has_attach && 'Com anexo',
    ].filter(Boolean).join(' · ') || 'Sem condições';
    const actions = [
      f.action_move_to   && `→ ${f.action_move_to}`,
      f.action_mark_read && '✓ Lido',
      f.action_star      && '★ Estrela',
      f.action_delete    && '🗑 Excluir',
    ].filter(Boolean).join(' · ') || 'Sem ações';
    return `
      <div class="filter-item">
        <div class="filter-item-info">
          <div class="filter-item-name">${esc(f.name)}</div>
          <div class="filter-item-desc">${esc(conds)} → ${esc(actions)}</div>
        </div>
        <button class="filter-toggle ${f.enabled ? 'on' : ''}"
          title="${f.enabled ? 'Desativar' : 'Ativar'}"
          onclick="toggleFilter(${f.id}, ${!f.enabled}, this)"></button>
        <button class="btn btn-ghost" style="padding:3px 8px;font-size:11px"
          onclick="openFilterForm(${JSON.stringify(f).replace(/"/g, '&quot;')})">Editar</button>
        <button class="btn btn-danger" style="padding:3px 8px;font-size:11px"
          onclick="deleteFilter(${f.id})">Remover</button>
      </div>`;
  }).join('');
}

async function toggleFilter(id, enabled, btn) {
  await api(`/api/filters/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
  btn.classList.toggle('on', enabled);
  btn.title = enabled ? 'Desativar' : 'Ativar';
  toast(enabled ? 'Filtro ativado' : 'Filtro desativado', 'success');
}

// ── Formulário ────────────────────────────────────────────────────────────────

function openFilterForm(filter = null) {
  _editFilterId = filter?.id || null;
  document.getElementById('ff-name').value            = filter?.name || '';
  document.getElementById('ff-cond-from').value       = filter?.cond_from || '';
  document.getElementById('ff-cond-to').value         = filter?.cond_to || '';
  document.getElementById('ff-cond-subject').value    = filter?.cond_subject || '';
  document.getElementById('ff-cond-attach').checked   = !!filter?.cond_has_attach;
  document.getElementById('ff-action-read').checked   = !!filter?.action_mark_read;
  document.getElementById('ff-action-star').checked   = !!filter?.action_star;
  document.getElementById('ff-action-delete').checked = !!filter?.action_delete;
  document.getElementById('ff-enabled').checked       = filter?.enabled !== false;
  document.getElementById('ff-priority').value        = filter?.priority || 0;
  const folderSel = document.getElementById('ff-action-folder');
  folderSel.value = filter?.action_move_to || '';
  document.getElementById('filter-form-title').textContent = filter ? 'Editar filtro' : 'Novo filtro';
  document.getElementById('filter-form').style.display          = '';
  document.getElementById('filter-form-divider').style.display  = '';
  document.getElementById('filters-box-footer').style.display   = 'flex';
  document.getElementById('filter-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeFilterForm() {
  _editFilterId = null;
  document.getElementById('filter-form').style.display         = 'none';
  document.getElementById('filter-form-divider').style.display = 'none';
  document.getElementById('filters-box-footer').style.display  = 'none';
}

async function saveFilter() {
  const name = document.getElementById('ff-name').value.trim();
  if (!name) { toast('Nome do filtro obrigatório', 'error'); return; }
  const accountId = document.getElementById('filter-account-select').value;
  const payload = {
    name,
    enabled:          document.getElementById('ff-enabled').checked,
    priority:         parseInt(document.getElementById('ff-priority').value) || 0,
    cond_from:        document.getElementById('ff-cond-from').value.trim() || null,
    cond_to:          document.getElementById('ff-cond-to').value.trim()   || null,
    cond_subject:     document.getElementById('ff-cond-subject').value.trim() || null,
    cond_has_attach:  document.getElementById('ff-cond-attach').checked || null,
    action_move_to:   document.getElementById('ff-action-folder').value || null,
    action_mark_read: document.getElementById('ff-action-read').checked,
    action_star:      document.getElementById('ff-action-star').checked,
    action_delete:    document.getElementById('ff-action-delete').checked,
  };
  try {
    const res = _editFilterId
      ? await api(`/api/filters/${_editFilterId}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload) })
      : await api(`/api/accounts/${accountId}/filters`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload) });
    if (res.ok) { toast('Filtro salvo!', 'success'); closeFilterForm(); loadFilters(); }
    else toast((await res.json()).error || 'Erro ao salvar filtro', 'error');
  } catch(e) { toast('Erro ao salvar filtro', 'error'); }
}

async function deleteFilter(id) {
  if (!confirm('Remover este filtro?')) return;
  await api(`/api/filters/${id}`, { method: 'DELETE' });
  toast('Filtro removido', 'success');
  loadFilters();
}
