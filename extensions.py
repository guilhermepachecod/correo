"""
extensions.py
Instâncias compartilhadas inicializadas aqui e importadas pelo resto da app.
Evita imports circulares entre app.py ↔ routes ↔ services.
"""
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO

db       = SQLAlchemy()
socketio = SocketIO()

# Importado depois que db e socketio existem
mail_service = None  # atribuído em app.py após create_app()
idle_manager = None  # atribuído em app.py após o app subir
