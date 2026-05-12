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
  splitSize:           320,
  collapsedAccounts:   {},
  autoSyncMin:         0,
  browserNotifications: false,
};

function loadSettings() {
  try {
    Object.assign(settings, JSON.parse(localStorage.getItem('correo_settings') || '{}'));
  } catch(e) {}
}

function saveSettings() {
  localStorage.setItem('correo_settings', JSON.stringify(settings));
}
