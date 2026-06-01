// state.js — estado global da aplicação e settings persistidos em localStorage

const state = {
  accounts:           [],
  selectedAccountId:  null,
  selectedFolder:     'INBOX',
  selectedFolderReals: null,
  emails:             [],
  selectedEmailId:    null,
  currentPage:        1,
  hasMore:            false,
  isLoadingMore:      false,
  totalEmails:        0,
  searchQuery:        '',
  imapSearchActive:   false,
  replyToId:          null,
  editAccountId:      null,
  attachments:        [],
  selectedIds:        new Set(),
  emailShowImages:    false,
  emailViewMode:      'html',
  currentEmail:       null,
  currentThread:      [],
  threadOpen:         false,
  sortBy:             'date',
  sortDir:            'desc',
  syncErrors:         {},
  syncingIds:         new Set(),
  lastSyncTime:       {},
  authFailed:         new Set(),
};

const MIN_SYNC_GAP_MS = 5 * 60 * 1000;

// ── Settings ──────────────────────────────────────────────────────────────────

const settings = {
  theme:               'dark',
  layout:              'vertical',
  showImagesByDefault: false,
  defaultEmailViewMode: 'html',   // 'html' ou 'text'
  splitSize:           320,
  collapsedAccounts:   {},
  autoSyncMin:         0,
  browserNotifications: false,
};

async function loadSettings() {
  // Carrega do banco (servidor) — sincroniza entre dispositivos
  try {
    const res = await fetch('/api/settings');
    if (res.ok) {
      const data = await res.json();
      // Converte valores string de volta para os tipos corretos
      if (data.theme)                settings.theme                = data.theme;
      if (data.layout)               settings.layout               = data.layout;
      if (data.showImagesByDefault !== undefined)
        settings.showImagesByDefault  = data.showImagesByDefault.toLowerCase() === 'true';
      if (data.defaultEmailViewMode) settings.defaultEmailViewMode = data.defaultEmailViewMode;
      if (data.splitSize)            settings.splitSize            = parseInt(data.splitSize) || 320;
      if (data.autoSyncMin !== undefined)
        settings.autoSyncMin          = parseInt(data.autoSyncMin) || 0;
      if (data.browserNotifications !== undefined)
        settings.browserNotifications = data.browserNotifications.toLowerCase() === 'true';
    }
  } catch(e) {}
  // Fallback: localStorage para preferências locais (collapsedAccounts etc)
  try {
    const local = JSON.parse(localStorage.getItem('correo_settings') || '{}');
    if (local.collapsedAccounts) settings.collapsedAccounts = local.collapsedAccounts;
  } catch(e) {}
}

function saveSettings() {
  // Salva no localStorage (collapsedAccounts — específico por dispositivo)
  localStorage.setItem('correo_settings', JSON.stringify({ collapsedAccounts: settings.collapsedAccounts }));
  // Salva no banco (preferências globais)
  fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      theme:                settings.theme,
      layout:               settings.layout,
      showImagesByDefault:  settings.showImagesByDefault,
      defaultEmailViewMode: settings.defaultEmailViewMode || 'html',
      splitSize:            settings.splitSize,
      autoSyncMin:          settings.autoSyncMin,
      browserNotifications: settings.browserNotifications,
    }),
  }).catch(() => {});
}