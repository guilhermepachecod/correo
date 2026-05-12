// compose.js — modal de composição, editor HTML rico, anexos, envio

let composeMode = 'plain';

// ── Abrir / fechar ────────────────────────────────────────────────────────────

function openCompose(opts = {}) {
  state.replyToId  = opts.replyToId || null;
  state.attachments = [];

  document.getElementById('compose-to').value      = opts.to || '';
  document.getElementById('compose-cc').value      = opts.cc || '';
  document.getElementById('compose-subject').value = opts.subject || '';
  document.getElementById('compose-body').value    = opts.body || '';
  document.getElementById('compose-attach-list').innerHTML = '';
  document.getElementById('compose-title').textContent    = opts.title || 'Nova mensagem';

  // Reset para modo texto
  composeMode = 'plain';
  document.getElementById('compose-body-wrap').style.display = '';
  document.getElementById('compose-html-wrap').style.display = 'none';
  document.getElementById('tab-plain').classList.add('active');
  document.getElementById('tab-html').classList.remove('active');
  document.getElementById('compose-editor').innerHTML = '';

  document.getElementById('compose-modal').classList.add('open');
  setTimeout(() => document.getElementById('compose-to').focus(), 50);
}

function closeCompose() {
  document.getElementById('compose-modal').classList.remove('open');
  state.replyToId   = null;
  state.attachments = [];
}

// ── Modo HTML / Plain ─────────────────────────────────────────────────────────

function setComposeMode(mode) {
  composeMode = mode;
  const plainWrap = document.getElementById('compose-body-wrap');
  const htmlWrap  = document.getElementById('compose-html-wrap');
  const tabPlain  = document.getElementById('tab-plain');
  const tabHtml   = document.getElementById('tab-html');

  if (mode === 'html') {
    const text = document.getElementById('compose-body').value;
    if (text && !document.getElementById('compose-editor').innerHTML)
      document.getElementById('compose-editor').innerText = text;
    plainWrap.style.display = 'none';
    htmlWrap.style.display  = 'flex';
    tabPlain.classList.remove('active');
    tabHtml.classList.add('active');
    setTimeout(() => document.getElementById('compose-editor').focus(), 50);
  } else {
    const html = document.getElementById('compose-editor').innerHTML;
    if (html) {
      const tmp = document.createElement('div');
      tmp.innerHTML = html;
      document.getElementById('compose-body').value = tmp.innerText;
    }
    plainWrap.style.display = '';
    htmlWrap.style.display  = 'none';
    tabPlain.classList.add('active');
    tabHtml.classList.remove('active');
  }
}

function execCmd(cmd, value = null) {
  document.getElementById('compose-editor').focus();
  document.execCommand(cmd, false, value);
}

function insertLink() {
  const url = prompt('URL do link:');
  if (url) execCmd('createLink', url);
}

// ── Anexos ────────────────────────────────────────────────────────────────────

function addAttachments(input) {
  for (const f of input.files) state.attachments.push(f);
  renderAttachList();
  input.value = '';
}

function renderAttachList() {
  document.getElementById('compose-attach-list').innerHTML =
    state.attachments.map((f, i) =>
      `<span class="compose-att">${esc(f.name)} <span class="compose-att-remove" onclick="removeAtt(${i})">×</span></span>`
    ).join('');
}

function removeAtt(i) { state.attachments.splice(i, 1); renderAttachList(); }

// ── Envio ─────────────────────────────────────────────────────────────────────

async function sendEmail() {
  const btn = document.getElementById('compose-send-btn');
  btn.disabled = true; btn.textContent = 'Enviando...';

  const fd = new FormData();
  fd.append('account_id', document.getElementById('compose-account-select').value);
  fd.append('to',      document.getElementById('compose-to').value);
  fd.append('cc',      document.getElementById('compose-cc').value);
  fd.append('subject', document.getElementById('compose-subject').value);

  if (composeMode === 'html') {
    const html = document.getElementById('compose-editor').innerHTML;
    const tmp  = document.createElement('div'); tmp.innerHTML = html;
    fd.append('body', tmp.innerText);
    fd.append('body_html', html);
  } else {
    fd.append('body', document.getElementById('compose-body').value);
  }

  if (state.replyToId) fd.append('reply_to_id', state.replyToId);
  for (const f of state.attachments) fd.append('attachments', f);

  try {
    const data = await (await fetch('/api/emails/send', { method: 'POST', body: fd })).json();
    if (data.ok) { toast('E-mail enviado!', 'success'); closeCompose(); }
    else toast(`Erro: ${data.error}`, 'error');
  } catch(e) {
    toast('Erro ao enviar', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Enviar';
  }
}
