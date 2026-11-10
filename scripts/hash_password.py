#!/usr/bin/env python3
"""
scripts/hash_password.py
Pede uma senha (sem eco no terminal) e imprime o hash para colar em
CORREO_PASSWORD_HASH no .env.
"""
import getpass
from werkzeug.security import generate_password_hash

if __name__ == '__main__':
    pw1 = getpass.getpass('Senha: ')
    pw2 = getpass.getpass('Confirme a senha: ')
    if pw1 != pw2:
        print('As senhas não coincidem.')
        raise SystemExit(1)
    if len(pw1) < 8:
        print('Use uma senha com pelo menos 8 caracteres.')
        raise SystemExit(1)
    print(f'CORREO_PASSWORD_HASH={generate_password_hash(pw1, method="scrypt")}')
