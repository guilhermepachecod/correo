"""
routes/auth.py
Login de usuário único (CORREO_USER / CORREO_PASSWORD_HASH), logout,
sessão e rate limit de tentativas de login por IP.
"""
import os
import time
import secrets
import logging
import threading
from flask import Blueprint, request, jsonify, render_template, session, redirect
from werkzeug.security import check_password_hash

log = logging.getLogger(__name__)

bp = Blueprint('auth', __name__)

CORREO_USER          = os.environ.get('CORREO_USER')
CORREO_PASSWORD_HASH = os.environ.get('CORREO_PASSWORD_HASH')

if not CORREO_USER or not CORREO_PASSWORD_HASH:
    raise RuntimeError(
        'CORREO_USER e CORREO_PASSWORD_HASH são obrigatórias. '
        'Gere o hash com scripts/hash_password.py e defina as duas no .env.'
    )

RATE_LIMIT_WINDOW = 15 * 60   # 15 min
RATE_LIMIT_MAX    = 5         # tentativas falhas permitidas na janela

_fail_lock: threading.Lock = threading.Lock()
_fail_log: dict[str, list[float]] = {}


def _client_ip() -> str:
    return request.remote_addr or 'unknown'


def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    with _fail_lock:
        attempts = [t for t in _fail_log.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
        _fail_log[ip] = attempts
        return len(attempts) >= RATE_LIMIT_MAX


def _record_fail(ip: str) -> None:
    with _fail_lock:
        _fail_log.setdefault(ip, []).append(time.time())


@bp.get('/login')
def login_page():
    if session.get('authenticated'):
        return redirect('/')
    return render_template('login.html')


@bp.post('/api/login')
def api_login():
    ip = _client_ip()
    if _is_rate_limited(ip):
        log.warning(f'LOGIN BLOCKED ip={ip} (rate limit)')
        return jsonify({'error': 'Muitas tentativas. Aguarde alguns minutos.'}), 429

    data     = request.json or {}
    user     = (data.get('user') or '').strip()
    password = data.get('password') or ''
    remember = bool(data.get('remember'))

    valid = (user == CORREO_USER and check_password_hash(CORREO_PASSWORD_HASH, password))
    if not valid:
        _record_fail(ip)
        log.warning(f'LOGIN FAIL ip={ip} user={user!r}')
        return jsonify({'error': 'Usuário ou senha inválidos'}), 401

    session.clear()
    session['authenticated'] = True
    session['user']          = user
    session['csrf']          = secrets.token_urlsafe(32)
    session.permanent        = remember
    return jsonify({'ok': True, 'user': user, 'csrf': session['csrf']})


@bp.post('/api/logout')
def api_logout():
    session.clear()
    return jsonify({'ok': True})


@bp.get('/api/me')
def api_me():
    if not session.get('authenticated'):
        return jsonify({'authenticated': False}), 401
    if not session.get('csrf'):
        session['csrf'] = secrets.token_urlsafe(32)
    return jsonify({'authenticated': True, 'user': session.get('user'), 'csrf': session['csrf']})
