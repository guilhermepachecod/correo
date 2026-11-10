#!/usr/bin/env python3
"""
scripts/gen_keys.py
Gera uma CORREO_SECRET_KEY e uma CORREO_FERNET_KEY novas, prontas para
colar no .env. Não lê nem escreve nenhum arquivo — só imprime.
"""
import secrets
from cryptography.fernet import Fernet

if __name__ == '__main__':
    print(f'CORREO_SECRET_KEY={secrets.token_hex(32)}')
    print(f'CORREO_FERNET_KEY={Fernet.generate_key().decode()}')
