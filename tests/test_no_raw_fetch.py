"""
Garante que nenhum arquivo em static/js/ chama fetch() diretamente, exceto
utils.js (onde vive o wrapper único api()). Toda chamada a /api/* precisa
passar por api() para receber o header X-Correo-CSRF e o tratamento de 401 —
ver pendência registrada em docs/FASE5-FUNCIONALIDADES.md (seção 0, item 3).
"""
import re
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent.parent / 'static' / 'js'
ALLOWED_RAW_FETCH_FILES = {'utils.js'}
_FETCH_CALL_RE = re.compile(r'(?<![\w.$])fetch\s*\(')


def test_no_raw_fetch_outside_wrapper():
    violations = []
    for js_file in sorted(JS_DIR.glob('*.js')):
        if js_file.name in ALLOWED_RAW_FETCH_FILES:
            continue
        text = js_file.read_text(encoding='utf-8')
        for m in _FETCH_CALL_RE.finditer(text):
            line_no = text.count('\n', 0, m.start()) + 1
            violations.append(f'{js_file.name}:{line_no}')
    assert not violations, (
        'fetch() direto fora do wrapper api() (static/js/utils.js): ' + ', '.join(violations)
    )
