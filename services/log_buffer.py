"""
services/log_buffer.py
Buffer circular em memória com as últimas linhas de log da aplicação —
existe só pra permitir ver o log ao vivo via HTTP (GET /api/debug/logs em
app.py), sem precisar de acesso SSH ao host pra rodar `docker logs`. Nunca
grava nada em disco; some ao reiniciar o container, igual ao `docker logs`
normal.
"""
import logging
from collections import deque

MAXLEN = 5000
_buffer = deque(maxlen=MAXLEN)


class RingBufferHandler(logging.Handler):
    def emit(self, record):
        try:
            _buffer.append(self.format(record))
        except Exception:
            pass


def install(level=logging.INFO) -> None:
    """Anexa o handler no logger raiz — captura tudo que já aparece no
    `docker logs` (mesmo formato: LEVEL:logger.name:mensagem)."""
    handler = RingBufferHandler()
    handler.setFormatter(logging.Formatter('%(levelname)s:%(name)s:%(message)s'))
    handler.setLevel(level)
    logging.getLogger().addHandler(handler)


def tail(n: int = 200, grep: str | None = None) -> list[str]:
    lines = list(_buffer)
    if grep:
        needle = grep.lower()
        lines = [l for l in lines if needle in l.lower()]
    return lines[-n:]
