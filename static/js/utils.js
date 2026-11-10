// utils.js — funções puras, zero dependências externas

// ── API (fetch único) ──────────────────────────────────────────────────────
// TODA chamada a /api/* no frontend DEVE passar por api(), nunca por fetch()
// direto — é o único lugar que injeta o header X-Correo-CSRF em métodos
// mutáveis e trata sessão expirada (401 → /login).
// tests/test_no_raw_fetch.py falha o build se aparecer fetch( fora daqui.

let _csrfToken = null;

function setCsrfToken(token) { _csrfToken = token; }

async function api(url, opts = {}) {
  const init = Object.assign({}, opts);
  const method = (init.method || 'GET').toUpperCase();
  const isMutating = ['POST', 'PUT', 'DELETE', 'PATCH'].includes(method);

  if (isMutating && url.startsWith('/api/') && url !== '/api/login' && _csrfToken) {
    const headers = new Headers(init.headers || {});
    headers.set('X-Correo-CSRF', _csrfToken);
    init.headers = headers;
  }

  const res = await fetch(url, init);

  if (res.status === 401 && url !== '/api/login' && url !== '/api/me') {
    window.location.href = '/login';
  }
  return res;
}

// Busca o token CSRF da sessão atual. Chamado uma vez no início de init()
// (app.js), antes de qualquer chamada mutável.
async function initAuth() {
  try {
    const res = await api('/api/me');
    if (res.ok) {
      const data = await res.json();
      setCsrfToken(data.csrf || null);
    }
  } catch (e) { /* offline — próximas chamadas tratam o erro via 401 */ }
}

function esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function _parseDate(iso) {
  if (!iso) return null;
  // Banco salva em UTC sem Z — adiciona Z para browser interpretar como UTC e converter para local
  const s = iso.endsWith('Z') || iso.includes('+') || iso.includes('-', 10) ? iso : iso + 'Z';
  return new Date(s);
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = _parseDate(iso), now = new Date();
  if (!d || isNaN(d)) return '';
  if (d.toDateString() === now.toDateString())
    return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
  if (now - d < 7 * 86400000)
    return d.toLocaleDateString('pt-BR', { weekday: 'short', day: '2-digit', month: '2-digit' });
  return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit', year: '2-digit' });
}

function fmtDateFull(iso) {
  if (!iso) return '';
  const d = _parseDate(iso);
  if (!d || isNaN(d)) return '';
  return d.toLocaleString('pt-BR', {
    day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function fmtSize(b) {
  if (!b) return '';
  if (b < 1024) return `${b}B`;
  if (b < 1048576) return `${(b / 1024).toFixed(1)}KB`;
  return `${(b / 1048576).toFixed(1)}MB`;
}

let _toastTimer;
function toast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.className = '', 3500);
}

let _notifTimer;
function showSyncNotification(msg, type = 'info') {
  const el = document.getElementById('sync-notif');
  if (!el) return;
  el.textContent = msg;
  el.className = `sync-notif visible ${type}`;
  clearTimeout(_notifTimer);
  _notifTimer = setTimeout(() => el.classList.remove('visible'), 5000);
}