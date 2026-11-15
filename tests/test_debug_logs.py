"""
Testa GET /api/debug/logs — pedido do usuário: ver o log ao vivo via curl
direto, sem precisar de SSH no host. Só existe de verdade com
CORREO_DEBUG_LOG_TOKEN definida; por padrão (sem a env var) a rota segue as
regras normais de sessão/CSRF como qualquer outra, nunca fica exposta sem
querer.
"""
import logging

import pytest

import app as app_module
from app import app


@pytest.fixture(autouse=True)
def _ensure_app_logger_emits_info():
    # O nível efetivo do logger 'app' depende de quando logging.basicConfig
    # rodou em relação aos handlers que o pytest já pode ter anexado no
    # logger raiz (basicConfig não faz nada se já existe handler) — fixa
    # explicitamente aqui pra esses testes não dependerem dessa ordem.
    logger = logging.getLogger('app')
    old_level = logger.level
    logger.setLevel(logging.INFO)
    yield
    logger.setLevel(old_level)


def _login(client):
    with client.session_transaction() as sess:
        sess['authenticated'] = True
        sess['user'] = 'admin'
        sess['csrf'] = 'tok'


def test_disabled_by_default_requires_normal_session(monkeypatch):
    monkeypatch.setattr(app_module, '_DEBUG_LOG_TOKEN', None)

    client = app.test_client()
    r = client.get('/api/debug/logs?token=qualquercoisa')
    assert r.status_code == 401   # sem sessão, igual qualquer rota normal


def test_wrong_token_is_404_even_when_enabled(monkeypatch):
    monkeypatch.setattr(app_module, '_DEBUG_LOG_TOKEN', 'segredo-forte')

    client = app.test_client()
    _login(client)
    r = client.get('/api/debug/logs?token=errado')
    assert r.status_code == 404


def test_correct_token_returns_matching_lines(monkeypatch):
    monkeypatch.setattr(app_module, '_DEBUG_LOG_TOKEN', 'segredo-forte')

    logging.getLogger('app').info('marca-exclusiva-xyz789 e outra coisa irrelevante')
    logging.getLogger('app').info('linha sem relação nenhuma')

    client = app.test_client()
    _login(client)
    r = client.get('/api/debug/logs?token=segredo-forte&grep=marca-exclusiva-xyz789')
    assert r.status_code == 200
    assert r.mimetype == 'text/plain'
    body = r.get_data(as_text=True)
    assert 'marca-exclusiva-xyz789' in body
    assert 'linha sem relação nenhuma' not in body


def test_tail_limits_number_of_lines(monkeypatch):
    monkeypatch.setattr(app_module, '_DEBUG_LOG_TOKEN', 'segredo-forte')

    log = logging.getLogger('app')
    for i in range(20):
        log.info(f'marca-tail-test linha {i}')

    client = app.test_client()
    _login(client)
    r = client.get('/api/debug/logs?token=segredo-forte&grep=marca-tail-test&tail=3')
    assert r.status_code == 200
    lines = [l for l in r.get_data(as_text=True).splitlines() if l]
    assert len(lines) == 3
    # as 3 últimas, não as 3 primeiras
    assert 'linha 19' in lines[-1]
