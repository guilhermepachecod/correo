// socket.js — handlers de eventos Socket.IO

const socket = io();

socket.on('sync_start', d => {
  state.syncingIds.add(d.account_id);
  renderAccountTree();
});

socket.on('sync_done', d => {
  state.syncingIds.delete(d.account_id);
  state.syncErrors[d.account_id] = null;
  state.lastSyncTime[d.account_id] = Date.now();
  renderAccountTree();
  // Só recarrega lista/contagens se houve e-mails novos ou flag_changes
  const hasChanges = (d.new && d.new > 0) || (d.flag_changes && d.flag_changes > 0);
  if (hasChanges && (!state.selectedAccountId || state.selectedAccountId === d.account_id)) {
    loadEmails();
    refreshFolderCounts();
  } else if (hasChanges) {
    // Outra conta teve mudanças — atualiza só as contagens da sidebar
    refreshFolderCounts();
  }
  if (d.new && d.new > 0)
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
  renderAccountTree();
});

socket.on('new_email', d => {
  if (!state.selectedAccountId || d.account_id === state.selectedAccountId) {
    loadEmails();
    refreshFolderCounts();
  }
  if (d.email) _sendBrowserNotif(d.email.subject, d.email.from_name || d.email.from_addr, d.email);
});

socket.on('flags_updated', d => {
  if (!state.selectedAccountId || d.account_id === state.selectedAccountId) {
    let changed = false;
    for (const upd of (d.updates || [])) {
      const email = state.emails.find(e => e.id === upd.id);
      if (email && email.is_read !== upd.is_read) {
        email.is_read = upd.is_read;
        changed = true;
        _patchEmailItem(upd.id, { is_read: upd.is_read });
      }
    }
    if (changed) refreshFolderCounts();
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
    if (d.new > 0) loadEmails();
  }
});

socket.on('idle_status', d => {
  // Pode ser usado futuramente para indicadores de conexão por conta
});