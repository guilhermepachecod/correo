// socket.js — handlers de eventos Socket.IO

const socket = io();

// A sidebar (contagens + árvore de contas/pastas) só re-renderiza no máximo
// 1x por janela de 600ms, mesmo que vários eventos de sync cheguem em
// rajada (IDLE + poll de várias contas/pastas) — sincroniza tudo primeiro,
// atualiza a tela depois, em vez de piscar uma vez por evento. O estado
// (syncingIds, syncErrors etc.) é sempre atualizado na hora; só o render
// é agrupado. refreshFolderCounts() já busca as contagens do banco (nunca
// do IMAP) e chama renderAccountTree() no final.
const _scheduleSidebarRefresh = debounce(() => { refreshFolderCounts(); }, 600);

socket.on('sync_start', d => {
  state.syncingIds.add(d.account_id);
  _scheduleSidebarRefresh();
});

socket.on('sync_done', d => {
  state.syncingIds.delete(d.account_id);
  state.syncErrors[d.account_id] = null;
  state.lastSyncTime[d.account_id] = Date.now();

  const isCurrentAccount = !state.selectedAccountId || state.selectedAccountId === d.account_id;
  const hasNew   = d.new   && d.new   > 0;
  const hasFlags = d.flag_changes && d.flag_changes > 0;

  if (isCurrentAccount && (hasNew || hasFlags)) {
    loadEmails(false, true);  // silencioso — IDLE/poll em background, sem piscar a lista
  }
  _scheduleSidebarRefresh();

  if (hasNew)
    showSyncNotification(`${d.new} nova${d.new > 1 ? 's' : ''} mensagem${d.new > 1 ? 'ns' : ''} em ${d.email}`);
});

socket.on('sync_error', d => {
  state.syncingIds.delete(d.account_id);
  state.syncErrors[d.account_id] = d.error;
  if (d.auth_failed) {
    state.authFailed.add(d.account_id);
    const acc = state.accounts.find(a => a.id === d.account_id);
    showSyncNotification(`Senha inválida: ${acc?.email || 'conta'} — clique em ↻ para tentar novamente`, 'error');
  } else {
    showSyncNotification('Erro ao sincronizar: verifique a conta', 'error');
  }
  _scheduleSidebarRefresh();
});

socket.on('new_email', d => {
  // Só recarrega se estiver vendo INBOX desta conta (ou sem conta selecionada)
  const isCurrentAccount = !state.selectedAccountId || state.selectedAccountId === d.account_id;
  const isInbox = !state.selectedFolderReals ||
    state.selectedFolderReals.some(f => /^inbox$/i.test(f));

  if (isCurrentAccount && isInbox) {
    loadEmails(false, true);  // silencioso — chegou via IDLE, sem piscar a lista
  }
  _scheduleSidebarRefresh();

  if (d.email) _sendBrowserNotif(d.email.subject, d.email.from_name || d.email.from_addr, d.email);
});

socket.on('flags_updated', d => {
  const matchesCurrentScope = state.currentView
    ? true   // visão unificada agrega todas as contas
    : (!state.selectedAccountId || d.account_id === state.selectedAccountId);
  if (matchesCurrentScope) {
    let changed = false;
    for (const upd of (d.updates || [])) {
      const email = state.emails.find(e => e.id === upd.id);
      if (!email) continue;
      // Visão "Não lidos": se outro cliente marcou como lido, some da lista.
      if (state.currentView === 'unread' && 'is_read' in upd && upd.is_read) {
        _dropFromList(upd.id);
        changed = true;
        continue;
      }
      // upd só traz os campos que de fato mudaram — nunca aplicar uma
      // chave ausente (senão vira undefined e corrompe o estado local)
      const patch = {};
      if ('is_read' in upd && email.is_read !== upd.is_read) {
        email.is_read = upd.is_read; patch.is_read = upd.is_read;
      }
      if ('is_starred' in upd && email.is_starred !== upd.is_starred) {
        email.is_starred = upd.is_starred; patch.is_starred = upd.is_starred;
      }
      if (Object.keys(patch).length) { _patchEmailItem(upd.id, patch); changed = true; }
    }
    if (changed) _scheduleSidebarRefresh();
  }
});

socket.on('emails_removed', d => {
  if (!state.selectedAccountId || d.account_id === state.selectedAccountId) {
    const idSet = new Set(d.ids || []);
    if (!idSet.size) return;
    const hadSelected = state.selectedEmailId && idSet.has(state.selectedEmailId);
    state.emails = state.emails.filter(e => !idSet.has(e.id));
    renderEmailList();
    _scheduleSidebarRefresh();
    if (hadSelected) {
      hideEmail();
      state.selectedEmailId = null;
      toast('Mensagem removida (excluída em outro cliente)', 'info');
    }
  }
});

socket.on('imap_search_start', d => {
  if (d.account_id === state.selectedAccountId)
    showSyncNotification('Buscando no servidor IMAP...', 'info');
});

socket.on('imap_search_done', d => {
  state.imapSearchActive = false;
  _setImapSearchUI(false);
  if (d.error) {
    showSyncNotification(`Erro na busca: ${d.error}`, 'error');
  } else {
    showSyncNotification(
      `Busca: ${d.found} resultado(s) no servidor${d.new ? `, ${d.new} novo(s)` : ''}`, 'info');
    if (d.new > 0) loadEmails(false, true);
  }
});

socket.on('idle_status', () => {});
