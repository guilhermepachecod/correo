// app.js COMPLETO

// ─────────────────────────────────────────────────────────────
// STATE
// ─────────────────────────────────────────────────────────────
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
  attachments: [],
  selectedIds: new Set(),

  emailShowImages: false,
  emailViewMode: 'html',

  cacheEmail: new Map(),

  horizontalSplit: 52
};

// ─────────────────────────────────────────────────────────────
// SETTINGS
// ─────────────────────────────────────────────────────────────
const settings = {
  theme: 'dark',
  layout: 'vertical',
  autoSync: 0,

  showImagesByDefault: false,
  defaultEmailMode: 'html',

  splitHorizontal: 52
};

function loadSettings() {
  try {
    Object.assign(
      settings,
      JSON.parse(localStorage.getItem('correo_settings') || '{}')
    );
  } catch (e) {}

  state.horizontalSplit = settings.splitHorizontal || 52;

  applyTheme();
  applyLayout();
  applyReaderDefaults();
  updateSettingsUI();

  if (settings.autoSync > 0) startAutoSync();
}

function saveSettings() {
  localStorage.setItem(
    'correo_settings',
    JSON.stringify(settings)
  );
}

function applyReaderDefaults() {
  state.emailShowImages = settings.showImagesByDefault;
  state.emailViewMode = settings.defaultEmailMode;
}

// ─────────────────────────────────────────────────────────────
// THEME
// ─────────────────────────────────────────────────────────────
function applyTheme() {
  document.documentElement.setAttribute(
    'data-theme',
    settings.theme
  );

  const btn = document.getElementById('toggle-theme');
  if (btn) {
    btn.textContent =
      settings.theme === 'dark'
        ? '☀ Claro'
        : '🌙 Escuro';
  }
}

function toggleTheme() {
  settings.theme =
    settings.theme === 'dark'
      ? 'light'
      : 'dark';

  saveSettings();
  applyTheme();
}

// ─────────────────────────────────────────────────────────────
// LAYOUT
// ─────────────────────────────────────────────────────────────
function applyLayout() {
  const app = document.getElementById('app');
  if (!app) return;

  app.setAttribute(
    'data-layout',
    settings.layout
  );

  const btn = document.getElementById('toggle-layout');
  if (btn) {
    btn.textContent =
      settings.layout === 'vertical'
        ? '⇔ Horizontal'
        : '⇕ Vertical';
  }

  requestAnimationFrame(() => {
    applyHorizontalSplit();
  });
}

function toggleLayout() {
  settings.layout =
    settings.layout === 'vertical'
      ? 'horizontal'
      : 'vertical';

  saveSettings();
  applyLayout();

  if (state.selectedEmailId) {
    openEmail(state.selectedEmailId);
  }
}

function applyHorizontalSplit() {
  if (settings.layout !== 'horizontal') return;

  const left = document.getElementById('left-pane');
  const right = document.getElementById('right-pane');

  if (!left || !right) return;

  const top = Math.max(
    20,
    Math.min(80, state.horizontalSplit)
  );

  left.style.height = top + '%';
  right.style.height = (100 - top) + '%';
}

function initSplitter() {
  const bar = document.getElementById('splitter');
  const wrap = document.getElementById('main-content');

  if (!bar || !wrap) return;

  let active = false;

  bar.addEventListener('mousedown', () => {
    active = true;
    document.body.classList.add('resizing');
  });

  window.addEventListener('mouseup', () => {
    active = false;
    document.body.classList.remove('resizing');
  });

  window.addEventListener('mousemove', e => {
    if (!active) return;
    if (settings.layout !== 'horizontal') return;

    const rect = wrap.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const pct = (y / rect.height) * 100;

    state.horizontalSplit = Math.max(
      20,
      Math.min(80, pct)
    );

    settings.splitHorizontal =
      state.horizontalSplit;

    saveSettings();
    applyHorizontalSplit();
  });
}

// ─────────────────────────────────────────────────────────────
// AUTOSYNC
// ─────────────────────────────────────────────────────────────
let autoSyncTimer = null;

function startAutoSync() {
  stopAutoSync();

  autoSyncTimer = setInterval(() => {
    fetch('/api/sync/all', {
      method: 'POST'
    });
  }, settings.autoSync * 60000);
}

function stopAutoSync() {
  if (autoSyncTimer) {
    clearInterval(autoSyncTimer);
    autoSyncTimer = null;
  }
}

function setAutoSync(v) {
  settings.autoSync = parseInt(v) || 0;

  saveSettings();

  if (settings.autoSync > 0) {
    startAutoSync();
    toast(
      `Auto-sync ${settings.autoSync} min`,
      'success'
    );
  } else {
    stopAutoSync();
    toast('Auto-sync OFF');
  }
}

// ─────────────────────────────────────────────────────────────
// EXTRA SETTINGS
// ─────────────────────────────────────────────────────────────
function toggleDefaultImages() {
  settings.showImagesByDefault =
    !settings.showImagesByDefault;

  saveSettings();
  updateSettingsUI();

  toast(
    settings.showImagesByDefault
      ? 'Imagens ON'
      : 'Imagens OFF'
  );
}

function toggleDefaultMode() {
  settings.defaultEmailMode =
    settings.defaultEmailMode === 'html'
      ? 'text'
      : 'html';

  saveSettings();
  updateSettingsUI();

  toast(
    settings.defaultEmailMode === 'html'
      ? 'Modo HTML'
      : 'Modo Texto'
  );
}

function updateSettingsUI() {
  const sel = document.getElementById(
    'autosync-select'
  );

  if (sel) sel.value = settings.autoSync;

  const img = document.getElementById(
    'toggle-images-default'
  );

  if (img) {
    img.textContent =
      settings.showImagesByDefault
        ? '🖼 Imagens ON'
        : '🖼 Imagens OFF';
  }

  const mode = document.getElementById(
    'toggle-email-mode'
  );

  if (mode) {
    mode.textContent =
      settings.defaultEmailMode === 'html'
        ? '📄 HTML'
        : '📄 Texto';
  }
}

// ─────────────────────────────────────────────────────────────
// EMAILS
// ─────────────────────────────────────────────────────────────
async function loadEmails() {
  const folders = [state.selectedFolder];
  let all = [];

  for (const folder of folders) {
    const params = new URLSearchParams({
      folder,
      page: state.currentPage,
      per_page: 50
    });

    if (state.selectedAccountId) {
      params.set(
        'account_id',
        state.selectedAccountId
      );
    }

    if (state.searchQuery) {
      params.set(
        'search',
        state.searchQuery
      );
    }

    const r = await fetch(
      '/api/emails?' + params.toString()
    );

    const data = await r.json();

    all = all.concat(data.emails || []);
  }

  state.emails = all.sort(
    (a, b) =>
      new Date(b.date) - new Date(a.date)
  );

  renderEmailList();
}

function renderEmailList() {
  const el =
    document.getElementById('email-list');

  if (!el) return;

  const html = [];

  for (const email of state.emails) {
    html.push(`
<div class="email-item ${
      email.id === state.selectedEmailId
        ? 'active'
        : ''
    }"
data-id="${email.id}"
onclick="openEmail(${email.id})">

<div class="row1">
<span>${esc(
      email.from_name ||
        email.from_addr
    )}</span>

<span>${fmtDate(email.date)}</span>
</div>

<div class="subj">
${esc(email.subject)}
</div>

<div class="prev">
${esc(email.preview || '')}
</div>

</div>
`);
  }

  el.innerHTML = html.join('');
}

// ─────────────────────────────────────────────────────────────
// OPEN EMAIL
// ─────────────────────────────────────────────────────────────
async function openEmail(id) {
  state.selectedEmailId = id;

  let email;

  if (state.cacheEmail.has(id)) {
    email = state.cacheEmail.get(id);
  } else {
    const r = await fetch(
      '/api/emails/' + id
    );
    email = await r.json();
    state.cacheEmail.set(id, email);
  }

  state.emailShowImages =
    settings.showImagesByDefault;

  state.emailViewMode =
    settings.defaultEmailMode;

  fillEmail(email);
  renderEmailList();
}

function fillEmail(email) {
  const subj =
    document.getElementById('ev-subject');

  const body =
    document.getElementById('ev-body');

  if (subj) subj.textContent = email.subject;

  if (!body) return;

  if (
    state.emailViewMode === 'html' &&
    email.body_html
  ) {
    let html = email.body_html.replace(
      /<script[\s\S]*?<\/script>/gi,
      ''
    );

    if (!state.emailShowImages) {
      html = html.replace(
        /<img/gi,
        '<img style="display:none"'
      );
    }

    body.innerHTML = html;
  } else {
    body.textContent =
      email.body_text || '';
  }
}

// ─────────────────────────────────────────────────────────────
// DELETE ALL
// ─────────────────────────────────────────────────────────────
async function deleteAllCurrentFolder() {
  if (
    !confirm(
      'Apagar TODOS os emails desta pasta?'
    )
  )
    return;

  const r = await fetch(
    '/api/emails/bulk',
    {
      method: 'POST',
      headers: {
        'Content-Type':
          'application/json'
      },
      body: JSON.stringify({
        action: 'delete',
        ids: [],
        folder:
          state.selectedFolder,
        account_id:
          state.selectedAccountId
      })
    }
  );

  const data = await r.json();

  if (data.ok) {
    state.selectedEmailId = null;
    state.emails = [];

    renderEmailList();

    toast(
      'Todos apagados',
      'success'
    );
  } else {
    toast(
      'Erro ao apagar',
      'error'
    );
  }
}

// ─────────────────────────────────────────────────────────────
// SEARCH
// ─────────────────────────────────────────────────────────────
let searchTimer = null;

function debounceSearch() {
  clearTimeout(searchTimer);

  searchTimer = setTimeout(() => {
    state.searchQuery =
      document
        .getElementById(
          'search-input'
        )
        .value.trim();

    loadEmails();
  }, 250);
}

// ─────────────────────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────────────────────
function esc(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function fmtDate(v) {
  if (!v) return '';

  return new Date(v).toLocaleDateString(
    'pt-BR'
  );
}

let toastTimer;

function toast(
  msg,
  type = 'info'
) {
  const el =
    document.getElementById(
      'toast'
    );

  if (!el) return;

  el.textContent = msg;
  el.className =
    'show ' + type;

  clearTimeout(toastTimer);

  toastTimer = setTimeout(() => {
    el.className = '';
  }, 2500);
}

// ─────────────────────────────────────────────────────────────
// INIT
// ─────────────────────────────────────────────────────────────
async function init() {
  loadSettings();
  initSplitter();
  await loadEmails();
}

document.addEventListener(
  'DOMContentLoaded',
  init
);