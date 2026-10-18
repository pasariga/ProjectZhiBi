"""
登录鉴权相关接口
"""
import os
import sqlite3
import time
from flask import request, jsonify
from werkzeug.utils import secure_filename
from utils.db_utils import (
    get_db_connection, now_str, get_user_profile, update_user_profile, change_user_password
)


def configure_auth_routes(app):
    @app.route('/login', methods=['POST'])
    def login():
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip()
        password = (data.get('password') or '').strip()
        if not username or not password:
            return jsonify({'status': 'error', 'message': '请填写账号和密码'}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT username, password, role,
                   COALESCE(display_name, username) AS display_name,
                   COALESCE(avatar_url, '') AS avatar_url
            FROM users
            WHERE username = ?
        """, (username,))
        user = cur.fetchone()
        conn.close()

        if user is None or user['password'] != password:
            return jsonify({'status': 'error', 'message': '账号或密码不正确'}), 401
        return jsonify({
            'status': 'success',
            'message': '欢迎回来，继续你的写作练习',
            'username': user['username'],
            'role': user['role'],
            'display_name': user['display_name'],
            'avatar_url': user['avatar_url']
        })

    @app.route('/register', methods=['POST'])
    def register():
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip()
        password = (data.get('password') or '').strip()

        if not username:
            return jsonify({'status': 'error', 'message': '请填写账号'}), 400
        if len(username) < 2:
            return jsonify({'status': 'error', 'message': '账号至少需要 2 个字符'}), 400
        if not password:
            return jsonify({'status': 'error', 'message': '请填写密码'}), 400
        if len(password) < 6:
            return jsonify({'status': 'error', 'message': '密码至少需要 6 位'}), 400

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute('SELECT 1 FROM users WHERE username = ?', (username,))
            if cur.fetchone() is not None:
                return jsonify({'status': 'error', 'message': '这个账号已存在，请换一个试试'}), 409
            cur.execute(
                'INSERT INTO users (username, password, role, display_name, created_at) VALUES (?, ?, ?, ?, ?)',
                (username, password, 'student', username, now_str())
            )
            conn.commit()
            return jsonify({
                'status': 'success',
                'message': '账号创建成功，开始你的写作之旅',
                'username': username,
                'role': 'student',
                'display_name': username,
                'avatar_url': ''
            })
        except sqlite3.Error:
            conn.rollback()
            return jsonify({'status': 'error', 'message': '账号创建失败，请稍后重试'}), 500
        finally:
            conn.close()

    @app.route('/profile', methods=['GET'])
    def profile_detail():
        username = (request.args.get('username') or '').strip()
        if not username:
            return jsonify({'status': 'error', 'message': '请先登录账号'}), 400
        profile = get_user_profile(username)
        if profile is None:
            return jsonify({'status': 'error', 'message': '没有找到这个账号'}), 404
        return jsonify({'status': 'success', 'profile': profile})

    @app.route('/profile/update', methods=['POST'])
    def profile_update():
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip()
        display_name = (data.get('display_name') or '').strip()
        avatar_url = (data.get('avatar_url') or '').strip()
        if not username:
            return jsonify({'status': 'error', 'message': '请先登录账号'}), 400
        if not display_name:
            return jsonify({'status': 'error', 'message': '请填写显示名称'}), 400
        if len(display_name) > 18:
            return jsonify({'status': 'error', 'message': '显示名称不能超过 18 个字符'}), 400
        profile = update_user_profile(username, display_name=display_name, avatar_url=avatar_url)
        if profile is None:
            return jsonify({'status': 'error', 'message': '没有找到这个账号'}), 404
        return jsonify({'status': 'success', 'message': '个人资料已保存', 'profile': profile})

    @app.route('/profile/password', methods=['POST'])
    def profile_password():
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip()
        old_password = (data.get('old_password') or '').strip()
        new_password = (data.get('new_password') or '').strip()
        if not username or not old_password or not new_password:
            return jsonify({'status': 'error', 'message': '请完整填写原密码和新密码'}), 400
        if len(new_password) < 6:
            return jsonify({'status': 'error', 'message': '新密码至少需要 6 位'}), 400
        ok, message = change_user_password(username, old_password, new_password)
        if not ok:
            return jsonify({'status': 'error', 'message': message}), 400
        return jsonify({'status': 'success', 'message': message})

    @app.route('/profile/avatar', methods=['POST'])
    def profile_avatar():
        username = (request.form.get('username') or '').strip()
        file = request.files.get('avatar')
        if not username:
            return jsonify({'status': 'error', 'message': '请先登录账号'}), 400
        if file is None or file.filename == '':
            return jsonify({'status': 'error', 'message': '请选择头像图片'}), 400
        ext = os.path.splitext(secure_filename(file.filename))[1].lower()
        if ext not in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
            return jsonify({'status': 'error', 'message': '头像仅支持 png / jpg / webp / gif'}), 400
        upload_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'uploads', 'avatars')
        os.makedirs(upload_dir, exist_ok=True)
        safe_name = secure_filename(username) or 'user'
        filename = f'{safe_name}_{int(time.time())}{ext}'
        save_path = os.path.join(upload_dir, filename)
        file.save(save_path)
        avatar_url = f'/uploads/avatars/{filename}'
        profile = update_user_profile(username, avatar_url=avatar_url)
        if profile is None:
            return jsonify({'status': 'error', 'message': '没有找到这个账号'}), 404
        return jsonify({'status': 'success', 'message': '头像已更新，写作空间更有你的样子了', 'avatar_url': avatar_url, 'profile': profile})

    @app.route('/guest_login', methods=['POST'])
    def guest_login():
        data = request.get_json(silent=True) or {}
        guest_name = (data.get('guest_name') or '游客').strip()
        return jsonify({'status': 'success', 'message': '已进入游客体验，可以先试着写一篇', 'username': f'guest_{guest_name}', 'role': 'guest'})
