// auth-client.js — carregado ANTES dos demais scripts.
// Busca o token CSRF da sessão e intercepta globalmente fetch() para:
//   1) anexar o header X-Correo-CSRF em toda chamada mutável para /api/*
//   2) redirecionar para /login em qualquer resposta 401 (sessão expirada)

(function () {
  let csrfToken = null;

  async function fetchMe() {
    try {
      const res = await fetch('/api/me', { credentials: 'same-origin' });
      if (res.ok) {
        const data = await res.json();
        csrfToken = data.csrf || null;
      } else if (res.status === 401) {
        window.location.href = '/login';
      }
    } catch (e) { /* offline — próximas chamadas tratam o erro */ }
  }

  const _origFetch = window.fetch.bind(window);

  window.fetch = async function (input, init) {
    init = init ? Object.assign({}, init) : {};
    const method = (init.method || 'GET').toUpperCase();
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const isMutating = ['POST', 'PUT', 'DELETE', 'PATCH'].includes(method);

    if (isMutating && url.startsWith('/api/') && url !== '/api/login' && csrfToken) {
      const headers = new Headers(init.headers || {});
      headers.set('X-Correo-CSRF', csrfToken);
      init.headers = headers;
    }

    const res = await _origFetch(input, init);

    if (res.status === 401 && url !== '/api/login' && url !== '/api/me') {
      window.location.href = '/login';
    }
    return res;
  };

  document.addEventListener('DOMContentLoaded', fetchMe);
})();
