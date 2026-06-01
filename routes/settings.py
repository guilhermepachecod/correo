"""
routes/settings.py
Preferências globais da aplicação — persistidas no banco.
"""
from flask import Blueprint, request, jsonify
from database import db, AppSettings

bp = Blueprint('settings', __name__)

ALLOWED_KEYS = {
    'theme', 'layout', 'showImagesByDefault',
    'defaultEmailViewMode', 'splitSize', 'autoSyncMin',
    'browserNotifications',
}


@bp.get('/api/settings')
def get_settings():
    return jsonify(AppSettings.get_all())


@bp.post('/api/settings')
def save_settings():
    data = request.json or {}
    for key, value in data.items():
        if key in ALLOWED_KEYS:
            AppSettings.set(key, str(value))
    return jsonify({'ok': True})