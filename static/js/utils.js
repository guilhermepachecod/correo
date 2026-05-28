// utils.js — funções puras, zero dependências externas

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