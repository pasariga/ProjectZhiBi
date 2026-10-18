"""
普通业务接口：素材、微课、历史记录、个性化推荐
"""
import json
from flask import request, jsonify
from utils.db_utils import (
    get_db_connection,
    get_personalized_videos,
    normalize_history_record,
    record_user_behavior,
)
from utils.api_utils import recommend_online_resources


def configure_api_routes(app):
    def positive_int(value, default, maximum=None):
        try:
            result = max(1, int(value))
        except (TypeError, ValueError):
            result = default
        return min(result, maximum) if maximum else result

    @app.route('/api/history', methods=['GET'])
    def history_list():
        username = (request.args.get('username') or '').strip()
        if not username:
            return jsonify({'status': 'error', 'message': '缺少账号信息'}), 400
        page = positive_int(request.args.get('page'), 1)
        page_size = positive_int(request.args.get('page_size'), 8, 50)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS total FROM history WHERE username = ? AND action_type = 'essay_corrected'",
            (username,)
        )
        total = cur.fetchone()['total'] or 0
        cur.execute(
            """
            SELECT id, title, content, result, action_type, created_at
            FROM history
            WHERE username = ? AND action_type = 'essay_corrected'
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (username, page_size, (page - 1) * page_size)
        )
        records = [normalize_history_record(row) for row in cur.fetchall()]
        conn.close()
        return jsonify({
            'status': 'success',
            'records': records,
            'total': total,
            'page': page,
            'page_size': page_size,
            'has_more': page * page_size < total,
        })

    @app.route('/api/history/<int:history_id>', methods=['GET', 'DELETE'])
    def history_detail(history_id):
        data = request.get_json(silent=True) or {}
        username = (request.args.get('username') or data.get('username') or '').strip()
        if not username:
            return jsonify({'status': 'error', 'message': '缺少账号信息'}), 400
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, content, result, action_type, created_at
            FROM history
            WHERE id = ? AND username = ? AND action_type = 'essay_corrected'
            """,
            (history_id, username)
        )
        row = cur.fetchone()
        if row is None:
            conn.close()
            return jsonify({'status': 'error', 'message': '历史记录不存在或无权访问'}), 404
        if request.method == 'DELETE':
            cur.execute(
                "DELETE FROM history WHERE id = ? AND username = ? AND action_type = 'essay_corrected'",
                (history_id, username)
            )
            conn.commit()
            conn.close()
            return jsonify({'status': 'success', 'message': '历史记录已删除', 'history_id': history_id})
        record = normalize_history_record(row)
        conn.close()
        return jsonify({'status': 'success', 'record': record})

    @app.route('/api/history/import', methods=['POST'])
    def import_legacy_history():
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or '').strip()
        records = data.get('records') if isinstance(data.get('records'), list) else []
        if not username:
            return jsonify({'status': 'error', 'message': '缺少账号信息'}), 400
        conn = get_db_connection()
        cur = conn.cursor()
        imported = 0
        for item in records[:50]:
            if not isinstance(item, dict):
                continue
            title = str(item.get('title') or '').strip()
            content = str(item.get('content') or '').strip()
            result = item.get('result') or item.get('correction') or ''
            if not content or not result:
                continue
            if isinstance(result, (dict, list)):
                result = json.dumps(result, ensure_ascii=False)
            cur.execute(
                """
                SELECT 1 FROM history
                WHERE username = ? AND action_type = 'essay_corrected'
                  AND COALESCE(title, '') = ? AND COALESCE(content, '') = ? AND COALESCE(result, '') = ?
                LIMIT 1
                """,
                (username, title, content, str(result))
            )
            if cur.fetchone():
                continue
            created_at = str(item.get('created_at') or item.get('time') or '').strip()
            cur.execute(
                """
                INSERT INTO history (username, title, content, result, action_type, created_at)
                VALUES (?, ?, ?, ?, 'essay_corrected', COALESCE(NULLIF(?, ''), datetime('now', 'localtime')))
                """,
                (username, title, content, str(result), created_at)
            )
            imported += 1
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'imported': imported})

    @app.route('/api/hot_materials', methods=['GET'])
    def hot_materials():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT title, tag, content, source FROM hot_materials ORDER BY id ASC')
        materials = [dict(row) for row in cur.fetchall()]
        conn.close()
        return jsonify({'status': 'success', 'materials': materials})

    @app.route('/api/video', methods=['GET'])
    def videos():
        username = (request.args.get('username') or 'guest_user').strip()
        limit = positive_int(request.args.get('limit'), 5, 50)
        return jsonify({'status': 'success', 'videos': get_personalized_videos(username, limit)})

    @app.route('/api/record_material_action', methods=['POST'])
    def record_material_action():
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or 'guest_user').strip()
        title = (data.get('title') or '').strip()
        content = data.get('content') or ''
        action_type = (data.get('action_type') or 'material_used').strip()
        if not title:
            return jsonify({'status': 'error', 'message': '请先选择要记录的内容'}), 400
        behavior_id = record_user_behavior(
            username,
            action_type,
            target_type='learning_resource',
            target_title=title,
            metadata={'content': str(content)[:1000]},
        )
        return jsonify({'status': 'success', 'message': '学习行为已记录', 'behavior_id': behavior_id})

    @app.route('/api/user-behavior', methods=['POST'])
    def user_behavior():
        data = request.get_json(silent=True) or {}
        username = (data.get('username') or 'guest_user').strip() or 'guest_user'
        event_type = (data.get('event_type') or '').strip()
        if not event_type:
            return jsonify({'status': 'error', 'message': '缺少行为类型'}), 400
        metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}
        behavior_id = record_user_behavior(
            username=username,
            event_type=event_type,
            target_type=(data.get('target_type') or '').strip(),
            target_id=data.get('target_id') or '',
            target_title=(data.get('target_title') or '').strip(),
            metadata=metadata,
        )
        return jsonify({'status': 'success', 'behavior_id': behavior_id})

    @app.route('/api/learning_progress', methods=['GET'])
    def learning_progress():
        username = (request.args.get('username') or 'guest_user').strip()
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM videos")
        total = cur.fetchone()['total'] or 1
        cur.execute("""
            SELECT COUNT(DISTINCT target_title) AS watched
            FROM user_behaviors
            WHERE username = ? AND event_type IN ('video_watched', 'video_viewed', 'video_started', 'microcourse_opened')
        """, (username,))
        watched = cur.fetchone()['watched'] or 0
        if not watched:
            cur.execute("""
                SELECT COUNT(DISTINCT title) AS watched
                FROM history
                WHERE username = ? AND action_type IN ('video_watched', 'video_viewed', 'video_started', 'microcourse_opened')
            """, (username,))
            watched = cur.fetchone()['watched'] or 0
        conn.close()
        return jsonify({'status': 'success', 'watched_count': watched, 'total_count': total})


    @app.route('/api/recommend_resources', methods=['POST'])
    def recommend_resources():
        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        content = (data.get('content') or '').strip()
        correction = (data.get('correction') or '').strip()
        limit = int(data.get('limit') or 4)
        if not title and not content:
            return jsonify({'status': 'error', 'message': '请先写下作文题目或正文内容'}), 400
        result = recommend_online_resources(title=title, content=content, correction=correction, limit=limit)
        return jsonify({'status': 'success', **result})
