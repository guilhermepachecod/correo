"""
legacy_crypto.py
Chave Fernet antiga, fixa no código (pré-CORREO_FERNET_KEY).
Usada APENAS como fallback de leitura em database.Account.get_password()
e pelo scripts/rotate_key.py, para migrar senhas antigas para a chave nova.
Nenhum outro módulo deve importar isto.
"""
import base64
import hashlib
from cryptography.fernet import Fernet

_LEGACY_SECRET = b'mailapp-fernet-key-v1-stable-2024'
_LEGACY_KEY    = base64.urlsafe_b64encode(hashlib.sha256(_LEGACY_SECRET).digest())
legacy_fernet  = Fernet(_LEGACY_KEY)
