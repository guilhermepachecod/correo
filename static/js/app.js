// app.js — orquestrador: init, tema, layout, resize, notificações, atalhos

// ── Sessão ────────────────────────────────────────────────────────────────────

async function logout() {
  try { await api('/api/logout', { method: 'POST' }); } catch (e) {}
  window.location.href = '/login';
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  await initAuth();      // busca o token CSRF antes de qualquer chamada mutável
  await loadSettings();  // carrega do banco antes de qualquer outra coisa
  updateSettingsUI();
  _initScrollInfinite();
  _setupComposeDragDrop();
  _setupComposeAutocomplete();
  await loadAccounts();
  if (settings.startupView && settings.startupView !== 'last') {
    selectUnifiedView(settings.startupView);
  } else {
    await loadEmails();
  }
  resumeDraftIfAny();
}

function setStartupView(view) {
  settings.startupView = view;
  saveSettings();
}

// ── Tema ──────────────────────────────────────────────────────────────────────

function applyTheme() {
  document.documentElement.setAttribute('data-theme', settings.theme);
  const btn = document.getElementById('toggle-theme');
  if (btn) btn.textContent = settings.theme === 'dark' ? '☀ Claro' : '🌙 Escuro';
}

function toggleTheme() {
  settings.theme = settings.theme === 'dark' ? 'light' : 'dark';
  saveSettings(); applyTheme();
}

// ── Layout ────────────────────────────────────────────────────────────────────

function applyLayout() {
  document.getElementById('main-panel').setAttribute('data-layout', settings.layout);
  applySplit();
  const btn = document.getElementById('pref-layout-btn');
  if (btn) btn.textContent = settings.layout === 'vertical' ? '⇔ Horizontal' : '⇕ Vertical';
}

function toggleLayout() {
  // Salva o splitSize atual para o layout que está saindo
  const key = settings.layout === 'vertical' ? 'splitSizeV' : 'splitSizeH';
  settings[key] = settings.splitSize;
  // Troca o layout e restaura o splitSize do novo layout (ou usa default)
  settings.layout = settings.layout === 'vertical' ? 'horizontal' : 'vertical';
  const newKey = settings.layout === 'vertical' ? 'splitSizeV' : 'splitSizeH';
  settings.splitSize = settings[newKey] || (settings.layout === 'vertical' ? 320 : 260);
  saveSettings();
  applyLayout();
}

// ── Resize ────────────────────────────────────────────────────────────────────

function applySplit() {
  const panel = document.getElementById('main-panel');
  if (settings.layout === 'vertical') {
    const w = Math.max(200, Math.min(settings.splitSize, window.innerWidth - 500));
    panel.style.gridTemplateColumns = `${w}px 4px 1fr`;
    panel.style.gridTemplateRows    = '';
  } else {
    const h = Math.max(120, Math.min(settings.splitSize, window.innerHeight - 200));
    panel.style.gridTemplateRows    = `1fr 4px ${h}px`;
    panel.style.gridTemplateColumns = '';
  }
}

let resizing = false, resizeStart = 0, resizeStartSize = 0;

function startResize(e) {
  resizing = true;
  resizeStart     = settings.layout === 'vertical' ? e.clientX : e.clientY;
  resizeStartSize = settings.splitSize;
  document.getElementById('resize-handle').classList.add('dragging');
  document.body.style.cursor     = settings.layout === 'vertical' ? 'col-resize' : 'row-resize';
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
  document.body.style.cursor = document.body.style.userSelect = '';
  saveSettings();
});

window.addEventListener('resize', applySplit);

// ── Settings UI ───────────────────────────────────────────────────────────────

function updateSettingsUI() {
  applyTheme(); applyLayout();
  _updateNotifButton();
  const startupSel = document.getElementById('startup-view-select');
  if (startupSel) startupSel.value = settings.startupView || 'last';

  // Atualiza toggles de preferências de e-mail
  const imgBtn  = document.getElementById('pref-images-btn');
  const modeBtn = document.getElementById('pref-mode-btn');
  const layBtn  = document.getElementById('pref-layout-btn');
  if (imgBtn)  imgBtn.textContent  = settings.showImagesByDefault ? '🖼 Imgs: ON' : '🖼 Imgs: OFF';
  if (modeBtn) modeBtn.textContent = (settings.defaultEmailViewMode || 'html') === 'html' ? 'HTML' : 'Texto';
  if (layBtn)  layBtn.textContent  = settings.layout === 'vertical' ? '⇕ Vertical' : '⇔ Horizontal';
}

function toggleDefaultImages() {
  settings.showImagesByDefault = !settings.showImagesByDefault;
  saveSettings(); updateSettingsUI();
}

function toggleDefaultViewMode() {
  settings.defaultEmailViewMode = (settings.defaultEmailViewMode || 'html') === 'html' ? 'text' : 'html';
  saveSettings(); updateSettingsUI();
}

// ── Notificações do navegador ─────────────────────────────────────────────────

async function toggleBrowserNotifications() {
  if (!('Notification' in window)) { toast('Navegador não suporta notificações', 'error'); return; }
  if (settings.browserNotifications) {
    settings.browserNotifications = false;
    saveSettings(); _updateNotifButton();
    toast('Notificações desativadas', 'info'); return;
  }
  const perm = await Notification.requestPermission();
  if (perm === 'granted') {
    settings.browserNotifications = true;
    saveSettings(); _updateNotifButton();
    toast('Notificações ativadas!', 'success');
    new Notification('Correo', { body: 'Notificações ativadas ✓', icon: '/static/icon.png' });
  } else {
    toast('Permissão negada pelo navegador', 'error');
  }
}

function _updateNotifButton() {
  const btn = document.getElementById('toggle-notif');
  if (!btn) return;
  btn.textContent = settings.browserNotifications ? '🔔 Notif: ON' : '🔕 Notif: OFF';
  btn.classList.toggle('active-toggle', settings.browserNotifications);
}

function _sendBrowserNotif(subject, from, accountEmail) {
  if (!settings.browserNotifications) return;
  if (Notification.permission !== 'granted') return;
  if (document.visibilityState === 'visible') return;
  new Notification(`Novo e-mail — ${accountEmail}`, {
    body: `De: ${from}\n${subject}`,
    icon: '/static/icon.png',
    tag:  'correo-new-email',
  });
}

// ── Atalhos de teclado ────────────────────────────────────────────────────────

let _gChordPending = false, _gChordTimer = null;

document.addEventListener('keydown', e => {
  const inInput = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)
    || document.activeElement?.isContentEditable;
  const composeOpen = document.getElementById('compose-modal').classList.contains('open');

  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && composeOpen) {
    e.preventDefault(); sendEmail(); return;
  }
  if (e.key === 'Escape') {
    if (composeOpen && !document.getElementById('compose-modal').classList.contains('minimized')
        && _hasDraftContent()) {
      minimizeCompose();
    } else {
      closeCompose();
    }
    closeAccountModal(); closeFiltersModal(); closeSource();
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 'n') { e.preventDefault(); openCompose(); }
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key === 'r' && state.selectedEmailId) { e.preventDefault(); replyEmail(); }
  if ((e.ctrlKey || e.metaKey) && e.shiftKey  && e.key === 'R' && state.selectedEmailId) { e.preventDefault(); replyAll(); }
  if ((e.ctrlKey || e.metaKey) && e.key === 'l' && state.selectedEmailId) { e.preventDefault(); forwardEmail(); }
  if ((e.ctrlKey || e.metaKey) && e.key === 'p' && state.selectedEmailId) { e.preventDefault(); printEmail(); }
  if (!inInput) {
    // Atalho composto "g" + (a|u|s) — visões unificadas (Fase 5.1)
    if (_gChordPending) {
      _gChordPending = false; clearTimeout(_gChordTimer);
      if (e.key === 'a') { e.preventDefault(); selectUnifiedView('all');     return; }
      if (e.key === 'u') { e.preventDefault(); selectUnifiedView('unread');  return; }
      if (e.key === 's') { e.preventDefault(); selectUnifiedView('starred'); return; }
      // qualquer outra tecla cancela o chord e segue o fluxo normal abaixo
    }
    if (e.key === 'g' && !e.ctrlKey && !e.metaKey) {
      _gChordPending = true;
      clearTimeout(_gChordTimer);
      _gChordTimer = setTimeout(() => { _gChordPending = false; }, 600);
      return;
    }
    if (e.key === 'c' && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); openCompose(); }
    if (e.key === 'j') _navigateEmail('next');
    if (e.key === 'k') _navigateEmail('prev');
    if (e.key === 'u') { hideEmail(); state.selectedEmailId = null; }
    if (e.key === 'e' && state.selectedEmailId) deleteEmail();
    if (e.key === 's' && state.selectedEmailId) toggleStar();
    if (e.key === '!' && state.selectedEmailId) toggleSpam();
    // 'c' removido — conflitava com Ctrl+C
    if (e.key === '/') { e.preventDefault(); document.getElementById('search-input')?.focus(); }
  }
});

// ── Fechar modais ao clicar fora ──────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('compose-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('compose-modal')) closeCompose();
  });
  document.getElementById('account-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('account-modal')) closeAccountModal();
  });
  document.getElementById('filters-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('filters-modal')) closeFiltersModal();
  });
  document.getElementById('source-box').addEventListener('click', e => {
    if (e.target === document.getElementById('source-box')) closeSource();
  });
  init();
});