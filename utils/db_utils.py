"""
数据库工具函数
"""
import json
import sqlite3
from datetime import datetime
from config.settings import DATABASE


def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def ensure_column(cur, table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row['name'] for row in cur.fetchall()}
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_user_profile(username):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT username, role, COALESCE(display_name, username) AS display_name, COALESCE(avatar_url, '') AS avatar_url
        FROM users
        WHERE username = ?
    """, (username,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def update_user_profile(username, display_name=None, avatar_url=None):
    conn = get_db_connection()
    cur = conn.cursor()
    fields = []
    params = []
    if display_name is not None:
        fields.append('display_name = ?')
        params.append(display_name)
    if avatar_url is not None:
        fields.append('avatar_url = ?')
        params.append(avatar_url)
    if not fields:
        conn.close()
        return get_user_profile(username)
    params.append(username)
    cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE username = ?", params)
    conn.commit()
    conn.close()
    return get_user_profile(username)


def change_user_password(username, old_password, new_password):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('SELECT password FROM users WHERE username = ?', (username,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return False, '用户不存在'
    if row['password'] != old_password:
        conn.close()
        return False, '原密码不正确'
    cur.execute('UPDATE users SET password = ? WHERE username = ?', (new_password, username))
    conn.commit()
    conn.close()
    return True, '密码已更新，请记好新的登录密码'



def save_history(username, title, content, result, action_type):
    conn = get_db_connection()
    cur = conn.cursor()
    if action_type == 'essay_corrected':
        cur.execute(
            '''
            SELECT id
            FROM history
            WHERE username = ? AND title = ? AND content = ? AND result = ? AND action_type = 'essay_corrected'
              AND ABS((julianday('now', 'localtime') - julianday(created_at)) * 86400) <= 120
            ORDER BY id DESC
            LIMIT 1
            ''',
            (username, title, content, result)
        )
        duplicate = cur.fetchone()
        if duplicate is not None:
            history_id = duplicate['id']
            conn.close()
            return history_id
    cur.execute(
        '''
        INSERT INTO history (username, title, content, result, action_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (username, title, content, result, action_type, now_str())
    )
    history_id = cur.lastrowid
    conn.commit()
    conn.close()
    return history_id


def record_user_behavior(username, event_type, target_type='', target_id='', target_title='', metadata=None):
    conn = get_db_connection()
    cur = conn.cursor()
    normalized_username = (username or 'guest_user').strip() or 'guest_user'
    normalized_event_type = (event_type or 'unknown').strip() or 'unknown'
    normalized_target_id = str(target_id or '').strip()
    if normalized_event_type == 'recommendation_viewed' and normalized_target_id:
        cur.execute(
            '''
            SELECT id FROM user_behaviors
            WHERE username = ? AND event_type = ? AND target_id = ?
            ORDER BY id DESC LIMIT 1
            ''',
            (normalized_username, normalized_event_type, normalized_target_id)
        )
        existing = cur.fetchone()
        if existing is not None:
            behavior_id = existing['id']
            conn.close()
            return behavior_id
    metadata_text = json.dumps(metadata or {}, ensure_ascii=False)
    cur.execute(
        '''
        INSERT INTO user_behaviors (
            username, event_type, target_type, target_id, target_title, metadata, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            normalized_username,
            normalized_event_type,
            (target_type or '').strip(),
            normalized_target_id,
            (target_title or '').strip(),
            metadata_text,
            now_str(),
        )
    )
    behavior_id = cur.lastrowid
    conn.commit()
    conn.close()
    return behavior_id


def get_recent_user_behaviors(username, limit=80):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        '''
        SELECT id, event_type, target_type, target_id, target_title, metadata, created_at
        FROM user_behaviors
        WHERE username = ?
        ORDER BY id DESC
        LIMIT ?
        ''',
        ((username or 'guest_user').strip() or 'guest_user', max(1, min(int(limit or 80), 200)))
    )
    rows = []
    for row in cur.fetchall():
        item = dict(row)
        try:
            item['metadata'] = json.loads(item.get('metadata') or '{}')
        except (TypeError, ValueError, json.JSONDecodeError):
            item['metadata'] = {}
        rows.append(item)
    conn.close()
    return rows


def save_personalized_recommendation(username, context_signature, payload, source):
    conn = get_db_connection()
    cur = conn.cursor()
    result_text = json.dumps(payload or {}, ensure_ascii=False)
    cur.execute(
        '''
        INSERT INTO personalized_recommendations (
            username, context_signature, result, source, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        ''',
        (
            (username or 'guest_user').strip() or 'guest_user',
            str(context_signature or '').strip(),
            result_text,
            str(source or 'local_fallback').strip(),
            now_str(),
        )
    )
    recommendation_id = cur.lastrowid
    cur.execute(
        '''
        DELETE FROM personalized_recommendations
        WHERE username = ? AND id NOT IN (
            SELECT id FROM personalized_recommendations
            WHERE username = ? ORDER BY id DESC LIMIT 20
        )
        ''',
        ((username or 'guest_user').strip() or 'guest_user', (username or 'guest_user').strip() or 'guest_user')
    )
    conn.commit()
    conn.close()
    return recommendation_id


def get_personalized_recommendation(username, context_signature=None):
    conn = get_db_connection()
    cur = conn.cursor()
    params = [(username or 'guest_user').strip() or 'guest_user']
    where = 'username = ?'
    if context_signature:
        where += ' AND context_signature = ?'
        params.append(str(context_signature).strip())
    cur.execute(
        f'''
        SELECT id, context_signature, result, source, created_at
        FROM personalized_recommendations
        WHERE {where}
        ORDER BY id DESC
        LIMIT 1
        ''',
        params
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    item = dict(row)
    try:
        item['result'] = json.loads(item.get('result') or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return item


def normalize_history_record(row):
    """把历史表中的新 JSON、旧 JSON 和旧纯文本统一成前端可安全消费的结构。"""
    record = dict(row) if row is not None else {}
    raw_result = record.get('result') or ''
    parsed_result = None
    if isinstance(raw_result, dict):
        parsed_result = raw_result
        raw_result = json.dumps(raw_result, ensure_ascii=False)
    elif isinstance(raw_result, str) and raw_result.strip():
        try:
            candidate = json.loads(raw_result)
            if isinstance(candidate, dict):
                parsed_result = candidate
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_result = None

    score_source = parsed_result or {}
    overall_score = (
        score_source.get('overall_score')
        or score_source.get('final_score')
        or score_source.get('score')
    )
    dimension_scores = score_source.get('dimension_scores')
    if not isinstance(dimension_scores, dict):
        dimension_scores = score_source.get('radar_scores')
    if not isinstance(dimension_scores, dict):
        dimension_scores = score_source.get('scores')
    if not isinstance(dimension_scores, dict):
        dimension_scores = {}

    record.update({
        'history_id': record.get('id'),
        'title': (record.get('title') or '').strip(),
        'content': record.get('content') or '',
        'result': parsed_result if parsed_result is not None else raw_result,
        'raw_result': raw_result,
        'result_format': 'json' if parsed_result is not None else 'text',
        'overall_score': overall_score,
        'dimension_scores': dimension_scores,
    })
    return record


def get_user_preferred_tags(username, limit=3):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        '''
        SELECT content
        FROM history
        WHERE username = ? AND action_type = 'essay_corrected'
        ORDER BY id DESC
        LIMIT 30
        ''',
        (username,)
    )
    tag_counter = {}
    rules = {
        '理想 / 拼搏': ('理想', '拼搏', '梦想'),
        '坚韧 / 毅力': ('坚韧', '毅力', '坚持'),
        '胸怀 / 修养': ('胸怀', '修养'),
        '语言润色': ('语言', '润色', '表达'),
        '写作结构': ('写作', '结构', '层次'),
        '结尾升华': ('结尾', '点题'),
    }
    for row in cur.fetchall():
        content = row['content'] or ''
        for tag, keywords in rules.items():
            if any(keyword in content for keyword in keywords):
                tag_counter[tag] = tag_counter.get(tag, 0) + 1
    conn.close()
    return [tag for tag, _ in sorted(tag_counter.items(), key=lambda item: item[1], reverse=True)[:limit]]


def get_personalized_videos(username, limit=5):
    conn = get_db_connection()
    cur = conn.cursor()
    preferred_tags = get_user_preferred_tags(username)
    videos = []
    select_sql = '''
        SELECT id,
               title,
               tag,
               COALESCE(category, tag, '') AS category,
               video_url AS link,
               video_url AS url,
               local_video AS video,
               cover_url AS cover,
               cover_url AS coverUrl,
               cover_color AS coverColor,
               COALESCE(duration, '') AS duration,
               COALESCE(keywords, '') AS keywords,
               COALESCE(learning_goal, '') AS learning_goal,
               COALESCE(practice, '') AS practice,
               COALESCE(source, '') AS source,
               COALESCE(description, '') AS desc
        FROM videos
    '''
    if preferred_tags:
        placeholders = ','.join(['?'] * len(preferred_tags))
        cur.execute(
            f'{select_sql} WHERE tag IN ({placeholders}) ORDER BY id ASC LIMIT ?',
            preferred_tags + [limit]
        )
        videos = [dict(row) for row in cur.fetchall()]
    if len(videos) < limit:
        cur.execute(f'{select_sql} ORDER BY id ASC LIMIT ?', (limit,))
        seen = {video['title'] for video in videos}
        for row in cur.fetchall():
            item = dict(row)
            if item['title'] not in seen:
                videos.append(item)
            if len(videos) >= limit:
                break
    conn.close()
    return videos


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'student',
            created_at TEXT
        )
    ''')
    ensure_column(cur, 'users', 'display_name', 'TEXT')
    ensure_column(cur, 'users', 'avatar_url', 'TEXT')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            tag TEXT,
            video_url TEXT,
            cover_url TEXT,
            cover_color TEXT,
            created_at TEXT
        )
    ''')
    ensure_column(cur, 'videos', 'category', 'TEXT')
    ensure_column(cur, 'videos', 'duration', 'TEXT')
    ensure_column(cur, 'videos', 'keywords', 'TEXT')
    ensure_column(cur, 'videos', 'local_video', 'TEXT')
    ensure_column(cur, 'videos', 'learning_goal', 'TEXT')
    ensure_column(cur, 'videos', 'practice', 'TEXT')
    ensure_column(cur, 'videos', 'source', 'TEXT')
    ensure_column(cur, 'videos', 'description', 'TEXT')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS hot_materials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            tag TEXT,
            content TEXT,
            source TEXT,
            created_at TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            title TEXT,
            content TEXT,
            result TEXT,
            action_type TEXT,
            created_at TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS user_behaviors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            event_type TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            target_title TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_user_behaviors_user_time ON user_behaviors (username, id DESC)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_user_behaviors_event ON user_behaviors (username, event_type)')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS personalized_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            context_signature TEXT NOT NULL,
            result TEXT NOT NULL,
            source TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_personalized_recommendations_context ON personalized_recommendations (username, context_signature, id DESC)')

    default_videos = [
        ('人物事例与细节描写', '人物事例', '内容充实', 'https://b23.tv/9BCvfX2', '/covers/material-preview-1.png', 'green', '8 分钟', '人物事例,内容充实,细节描写', '/videos/material-preview-1.mp4', '学会把人物事例写具体，让内容能支撑中心。', '从身边人物中选择一个细节，写出动作、语言和心理。', '本地微课 / B站公开课程', '围绕人物事例、生活细节和主题表达进行写作训练。'),
        ('生活细节观察积累', '生活细节', '内容充实', 'https://b23.tv/Xmv6dmo', '/covers/23201279238dc88043919a7c31123ab8.png', 'green', '7 分钟', '生活细节,真实感,观察积累', '/videos/23201279238dc88043919a7c31123ab8.mp4', '学会把日常小事写成有画面、有情绪的文章内容。', '记录今天看到的一处动作或场景，并补充自己的心理感受。', '本地微课 / B站公开课程', '从日常观察中提炼动作、场景和心理细节。'),
        ('名言运用与主题表达', '主题表达', '内容充实', 'https://b23.tv/JELlcBT', '/covers/4a99c3713106903cdb1daca9022ee0ca.png', 'green', '6 分钟', '名言运用,主题表达,内容充实', '/videos/4a99c3713106903cdb1daca9022ee0ca.mp4', '学会让名言服务主题，而不是机械堆砌。', '为成长、责任、坚持各整理一句可使用的主题表达。', '本地微课 / B站公开课程', '围绕常见作文主题积累名言和表达。'),
        ('润色语句与表达提升', '润色语句', '语言提升', 'https://b23.tv/RZquPZx', '/covers/dc30a466d7d727038324e1d28b94d190.png', 'blue', '9 分钟', '润色语句,表达自然,语言提升', '/videos/dc30a466d7d727038324e1d28b94d190.mp4', '学会把重复、笼统和不自然的句子改得顺畅。', '从自己的作文中挑 3 个句子，分别做一次精简和一次扩写。', '本地微课 / B站公开课程', '把笼统句、重复句改得更自然。'),
        ('修辞方法与高级表达', '修辞表达', '语言提升', 'https://b23.tv/E8j7ajV', '/covers/92af1c7cdaebdbcf8ab1beac32994fe0.png', 'blue', '8 分钟', '修辞表达,比喻,排比', '/videos/92af1c7cdaebdbcf8ab1beac32994fe0.mp4', '掌握比喻、排比等常见修辞的自然使用方法。', '把一句普通描写改成含比喻的句子，再改成含排比的句子。', '本地微课 / B站公开课程', '学习常用修辞，让句子更有表现力。'),
        ('病句修改与通顺表达', '病句修改', '语言提升', 'https://b23.tv/DiwgKZ1', '/covers/e174197542322236e1ca9a5876e2d862.png', 'blue', '7 分钟', '病句修改,表达通顺,语言规范', '/videos/e174197542322236e1ca9a5876e2d862.mp4', '识别搭配不当、成分残缺和语序不顺等常见问题。', '检查作文中 3 个长句，看是否能拆成更清楚的表达。', '本地微课 / B站公开课程', '识别常见病句和不通顺表达。'),
        ('作文开头与结尾技巧', '开头结尾', '结构与修改', 'https://b23.tv/VqlyH6O', '/covers/054f6d07cd04835fdec6821985f51d8e.png', 'purple', '8 分钟', '开头结尾,点题,呼应', '/videos/054f6d07cd04835fdec6821985f51d8e.mp4', '学会自然点题、首尾呼应和结尾升华。', '给同一个题目写 2 个不同开头，再写 1 个点题结尾。', '本地微课 / B站公开课程', '掌握自然开头、结尾点题和首尾呼应的方法。'),
        ('文章结构与段落安排', '文章结构', '结构与修改', 'https://b23.tv/mij1fMa', '/covers/8acf18311583b09eb4c9dc0e4145a247.png', 'purple', '9 分钟', '文章结构,段落层次,结构训练', '/videos/8acf18311583b09eb4c9dc0e4145a247.mp4', '看懂开头、主体、转折和结尾之间的层次关系。', '为当前作文列出“开头、发展、转折、结尾”四步提纲。', '本地微课 / B站公开课程', '学习安排段落层次和过渡句。'),
        ('作文自改与批改复盘', '批改复盘', '结构与修改', 'https://b23.tv/x8Bftvs', '/covers/8852d00175938056843a4b9236d95d04.png', 'purple', '8 分钟', '作文自改,批改复盘,修改方法', '/videos/8852d00175938056843a4b9236d95d04.mp4', '学会根据批改结果确定修改顺序。', '把一篇作文的问题分成“先改结构、再改句子、最后查规范”。', '本地微课 / B站公开课程', '根据作文诊断复盘问题，形成稳定的自我修改方法。'),
    ]
    cur.execute("SELECT COUNT(*) AS bad FROM videos WHERE video_url LIKE '%youtube%' OR cover_url LIKE '%via.placeholder%' OR video_url LIKE 'https://search.bilibili.com/%'")
    bad_video_count = cur.fetchone()['bad']
    cur.execute('SELECT COUNT(*) AS total FROM videos')
    video_total = cur.fetchone()['total']
    cur.execute("SELECT COUNT(*) AS b23_total FROM videos WHERE video_url LIKE 'https://b23.tv/%'")
    b23_total = cur.fetchone()['b23_total'] or 0
    if bad_video_count or video_total < len(default_videos) or b23_total < len(default_videos):
        cur.execute('DELETE FROM videos')
        for item in default_videos:
            cur.execute(
                '''
                INSERT INTO videos (
                    title, tag, category, video_url, cover_url, cover_color, duration,
                    keywords, local_video, learning_goal, practice, source, description, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (*item, now_str())
            )

    cur.execute('SELECT COUNT(*) AS total FROM hot_materials')
    if cur.fetchone()['total'] == 0:
        default_materials = [
            ('长风破浪会有时，直挂云帆济沧海。', '理想 / 拼搏', '表达虽处困境但依旧相信未来、坚定向前的精神，适合用于成长、理想、坚持类作文。', '李白《行路难》'),
            ('千磨万击还坚劲，任尔东西南北风。', '坚韧 / 毅力', '借竹子坚韧不拔的形象，适合用于逆境、意志、自我要求等主题。', '郑燮《竹石》'),
            ('海纳百川，有容乃大；壁立千仞，无欲则刚。', '胸怀 / 修养', '适合写品格、修养、心态、格局等主题，表达宽广胸怀与内在力量。', '林则徐'),
            ('不积跬步，无以至千里；不积小流，无以成江海。', '坚韧 / 毅力', '强调积累的重要性，适合用于学习、成长、目标实现等主题。', '荀子《劝学》'),
            ('纸上得来终觉浅，绝知此事要躬行。', '实践 / 真知', '强调实践的重要性，适合用于理论与实践相结合的主题。', '陆游《冬夜读书示子聿》'),
            ('读书破万卷，下笔如有神。', '写作结构', '强调广泛阅读对写作能力的提升作用。', '杜甫《奉赠韦左丞丈二十二韵》'),
            ('清水出芙蓉，天然去雕饰。', '语言润色', '形容语言或风格自然清新，不加雕琢的美。', '李白'),
            ('文章合为时而著，歌诗合为事而作。', '写作结构', '强调写作应关注现实、表达真实感受，适合用于责任、时代、青年主题。', '白居易'),
        ]
        for item in default_materials:
            cur.execute(
                'INSERT INTO hot_materials (title, tag, content, source, created_at) VALUES (?, ?, ?, ?, ?)',
                (*item, now_str())
            )

    conn.commit()
    conn.close()
