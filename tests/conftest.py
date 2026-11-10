"""
Configura o ambiente mínimo para importar app.py/database.py em testes,
sem precisar de Docker nem de um .env real. Roda num banco sqlite temporário
isolado por sessão de teste.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = Path(tempfile.mkdtemp(prefix='correo-test-'))

os.environ.setdefault('CORREO_SECRET_KEY', 'test-secret-key-for-pytest')
os.environ.setdefault('CORREO_FERNET_KEY', 'XO14J94Qq9P428E3Cs2uBEfr0sUK6OKcg1abo81P6eA=')
os.environ.setdefault('CORREO_USER', 'admin')
os.environ.setdefault(
    'CORREO_PASSWORD_HASH',
    'scrypt:32768:8:1$QTfyc2sCTPqgAZFl$6b3911de762279b101e05ad247cdae937c70b69bf9826becbf82ad32318571'
    'eda658514b7cf023f29cd69e63219eb9227002e84554a18607153b9e71bee03718',
)
os.environ.setdefault('CORREO_DATABASE_URI', f'sqlite:///{_tmp / "test.db"}')
os.environ.setdefault('ACCOUNTS_DIR', str(_tmp / 'accounts'))
os.environ.setdefault('ATTACHMENTS_DIR', str(_tmp / 'attachments'))
os.environ.setdefault('CORREO_SKIP_IDLE', '1')
