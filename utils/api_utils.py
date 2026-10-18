"""
第三方 API 工具函数
"""
import json
import time
import requests
from urllib.parse import quote_plus
from config.settings import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_API_URL,
    DEEPSEEK_MODEL,
    BAIDU_OCR_API_KEY,
    BAIDU_OCR_SECRET_KEY,
    OCR_TOKEN_URL,
    OCR_RECOGNIZE_URL,
    OCR_REQUEST_INTERVAL,
    ENABLE_ONLINE_RESOURCE_SEARCH,
    SEARCH_PROVIDER,
    SEARCH_API_KEY,
    SEARCH_API_URL,
    SEARCH_RESULT_LIMIT,
)

ocr_last_request_time = 0


def has_deepseek_key():
    return bool(DEEPSEEK_API_KEY)


def call_deepseek(messages, temperature=0.7, max_tokens=1500):
    if not has_deepseek_key():
        raise ValueError('DeepSeek API Key 未配置')
    headers = {
        'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
        'Content-Type': 'application/json'
    }
    payload = {
        'model': DEEPSEEK_MODEL,
        'messages': messages,
        'temperature': temperature,
        'stream': False,
        'max_tokens': max_tokens
    }
    response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    return data['choices'][0]['message']['content'].strip()


def stream_deepseek(messages, temperature=0.7, max_tokens=1500, timeout=(8, 45)):
    if not has_deepseek_key():
        raise ValueError('DeepSeek API Key 未配置')
    headers = {
        'Authorization': f'Bearer {DEEPSEEK_API_KEY}',
        'Content-Type': 'application/json'
    }
    payload = {
        'model': DEEPSEEK_MODEL,
        'messages': messages,
        'temperature': temperature,
        'stream': True,
        'max_tokens': max_tokens
    }
    response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, stream=True, timeout=timeout)
    response.raise_for_status()
    return response


def parse_stream_chunks(response):
    for line in response.iter_lines():
        if not line:
            continue
        line = line.decode('utf-8', errors='ignore').strip()
        if not line.startswith('data: '):
            continue
        data_str = line[6:]
        if data_str == '[DONE]':
            break
        try:
            stream_data = json.loads(data_str)
            delta = stream_data.get('choices', [{}])[0].get('delta', {})
            content_chunk = delta.get('content', '')
            if content_chunk:
                yield content_chunk
        except json.JSONDecodeError:
            continue


def get_baidu_ocr_token():
    if not BAIDU_OCR_API_KEY or not BAIDU_OCR_SECRET_KEY:
        return None, 'OCR API 密钥未配置，请在环境变量中设置 BAIDU_OCR_API_KEY 和 BAIDU_OCR_SECRET_KEY'
    try:
        params = {
            'grant_type': 'client_credentials',
            'client_id': BAIDU_OCR_API_KEY,
            'client_secret': BAIDU_OCR_SECRET_KEY
        }
        response = requests.post(OCR_TOKEN_URL, params=params, timeout=20)
        response.raise_for_status()
        data = response.json()
        if 'access_token' in data:
            return data['access_token'], None
        return None, data.get('error_description') or data.get('error') or '获取 OCR Token 失败'
    except requests.exceptions.RequestException as e:
        return None, f'网络请求失败：{str(e)}'


def recognize_image_with_baidu(image_base64):
    global ocr_last_request_time
    current_time = time.time()
    if current_time - ocr_last_request_time < OCR_REQUEST_INTERVAL:
        time.sleep(OCR_REQUEST_INTERVAL - (current_time - ocr_last_request_time))
    ocr_last_request_time = time.time()

    access_token, error = get_baidu_ocr_token()
    if error:
        return None, error

    try:
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        data = {
            'image': image_base64,
            'detect_direction': 'true',
            'language_type': 'CHN_ENG',
            'paragraph': 'true',
            'probability': 'true'
        }
        response = requests.post(
            f'{OCR_RECOGNIZE_URL}?access_token={access_token}',
            headers=headers,
            data=data,
            timeout=30
        )
        response.raise_for_status()
        result = response.json()
        if 'words_result' in result:
            return result['words_result'], None
        if 'error_code' in result:
            return None, f"OCR 返回错误（{result.get('error_code')}）：{result.get('error_msg', '未知错误')}"
        return None, f'OCR 返回未知错误：{result}'
    except requests.exceptions.RequestException as e:
        return None, f'网络请求失败：{str(e)}'
    except Exception as e:
        return None, f'OCR 识别异常：{str(e)}'


# -----------------------------
# 国内公开学习资源推荐 / 精选资源库
# 说明：比赛与正式部署默认面向中国大陆网络环境，推荐资源只使用大陆常见可访问站点或站内搜索入口。
# 不搬运第三方全文，不下载视频；仅展示标题、摘要、来源、推荐理由和原始链接。
# -----------------------------

RESOURCE_THEME_KEYWORDS = {
    '成长': ['成长', '改变', '进步', '青春', '经历'],
    '坚持': ['坚持', '努力', '挫折', '失败', '毅力', '奋斗'],
    '责任': ['责任', '担当', '使命', '奉献'],
    '亲情': ['妈妈', '爸爸', '父母', '家人', '亲情', '陪伴'],
    '友情': ['朋友', '同学', '友谊', '帮助'],
    '梦想': ['梦想', '理想', '目标', '未来'],
    '读书': ['读书', '阅读', '书籍', '知识'],
    '自然': ['自然', '风景', '春天', '秋天', '校园'],
}

RESOURCE_WEAKNESS_RULES = [
    ('结尾升华不足', ['结尾', '点题', '升华', '收束']),
    ('素材单一', ['素材', '事例', '论据', '名人', '引用']),
    ('结构层次不清', ['结构', '层次', '段落', '过渡', '衔接']),
    ('语言表达偏平', ['语言', '表达', '句式', '生动', '细节']),
    ('修辞不足', ['修辞', '比喻', '排比', '拟人']),
    ('错别字与规范', ['错别字', '标点', '规范', '语病']),
]

# 只使用国内常见可访问站点或国内搜索入口。URL 多采用站内搜索页，避免硬编码失效文章链接。
def _baidu_search_url(query):
    return 'https://www.baidu.com/s?wd=' + quote_plus(query)


def _bilibili_search_url(query):
    return 'https://search.bilibili.com/all?keyword=' + quote_plus(query)


CURATED_RESOURCE_BANK = [
    {
        'kind': 'article',
        'title': '成长类作文素材：人民网站内检索',
        'source': '人民网',
        'summary': '检索成长、青春、奋斗相关的新闻人物、时代故事和真实事例，适合补充作文素材。',
        'tags': ['成长', '坚持', '素材单一'],
        'url': _baidu_search_url('site:people.com.cn 成长 奋斗 青春 作文素材'),
    },
    {
        'kind': 'article',
        'title': '责任与担当素材：新华网站内检索',
        'source': '新华网',
        'summary': '围绕责任、担当、奉献等主题检索权威素材，适合用于议论和记叙文升华。',
        'tags': ['责任', '梦想', '素材单一'],
        'url': _baidu_search_url('site:xinhuanet.com 责任 担当 青年 奋斗'),
    },
    {
        'kind': 'article',
        'title': '作文结尾升华方法：光明网站内检索',
        'source': '光明网',
        'summary': '查找点题、升华、首尾呼应等写作方法，适合解决作文结尾收束较弱的问题。',
        'tags': ['结尾升华不足', '结构层次不清'],
        'url': _baidu_search_url('site:gmw.cn 作文 结尾 升华 点题'),
    },
    {
        'kind': 'article',
        'title': '细节描写与语言表达：百度检索',
        'source': '百度搜索',
        'summary': '检索动作、神态、心理、环境描写等写作技巧，帮助把笼统表达改得更具体。',
        'tags': ['语言表达偏平', '修辞不足'],
        'url': _baidu_search_url('中学生作文 细节描写 动作 神态 心理 环境'),
    },
    {
        'kind': 'article',
        'title': '阅读与写作积累：中国作家网站内检索',
        'source': '中国作家网',
        'summary': '从优秀作家文章和文学评论中积累语言表达、主题立意和素材角度。',
        'tags': ['读书', '语言表达偏平', '修辞不足'],
        'url': _baidu_search_url('site:chinawriter.com.cn 阅读 写作 语言 表达'),
    },
    {
        'kind': 'article',
        'title': '中小学写作资源：国家中小学智慧教育平台',
        'source': '国家中小学智慧教育平台',
        'summary': '面向中小学学习场景的官方教育资源入口，可用于语文写作拓展学习。',
        'tags': ['写作结构', '结构层次不清', '错别字与规范'],
        'url': 'https://www.zxx.edu.cn/',
    },
    {
        'kind': 'video',
        'title': '作文结尾如何升华：哔哩哔哩检索',
        'source': '哔哩哔哩',
        'summary': '检索结尾点题、主题升华、首尾呼应相关公开视频，适合针对结尾薄弱点训练。',
        'tags': ['结尾升华不足', '结构层次不清'],
        'url': _bilibili_search_url('作文 结尾 升华 点题'),
        'embed_url': '',
        'allow_embed': False,
    },
    {
        'kind': 'video',
        'title': '作文结构与段落层次：哔哩哔哩检索',
        'source': '哔哩哔哩',
        'summary': '检索作文结构、段落安排、过渡衔接相关微课，帮助文章层次更清楚。',
        'tags': ['结构层次不清', '结尾升华不足'],
        'url': _bilibili_search_url('作文 结构 段落 层次 过渡'),
        'embed_url': '',
        'allow_embed': False,
    },
    {
        'kind': 'video',
        'title': '作文语言润色与修辞：哔哩哔哩检索',
        'source': '哔哩哔哩',
        'summary': '检索语言润色、句式变化、比喻排比等修辞微课，适合提升表达表现力。',
        'tags': ['语言表达偏平', '修辞不足'],
        'url': _bilibili_search_url('作文 语言润色 修辞 比喻 排比'),
        'embed_url': '',
        'allow_embed': False,
    },
    {
        'kind': 'video',
        'title': '作文素材积累方法：哔哩哔哩检索',
        'source': '哔哩哔哩',
        'summary': '检索作文素材积累、素材分类和素材运用方法，帮助解决素材单一问题。',
        'tags': ['素材单一', '成长', '坚持', '责任'],
        'url': _bilibili_search_url('作文素材 积累 方法 中学生'),
        'embed_url': '',
        'allow_embed': False,
    },
]

RESOURCE_THEME_KEYWORDS = {
    '梦想': ['梦想', '理想', '目标', '未来'],
    '成长': ['成长', '改变', '进步', '挫折', '突破', '青春'],
    '亲情': ['亲情', '父母', '妈妈', '爸爸', '家人', '陪伴'],
    '校园': ['校园', '老师', '同学', '班级', '课堂'],
    '责任': ['责任', '担当', '奉献', '承诺'],
    '传统文化': ['传统文化', '文化', '节日', '家国', '中国'],
    '科技': ['科技', '人工智能', 'AI', '创新', '未来'],
}

RESOURCE_WEAKNESS_RULES = [
    ('错别字', ['错别字', '错字', '别字', '标点', '规范']),
    ('病句 / 语句不通', ['病句', '语句不通', '不通顺', '搭配不当', '语序']),
    ('表达空泛', ['空泛', '笼统', '不具体', '内容单薄']),
    ('描写不足', ['描写不足', '细节不足', '动作描写', '心理描写', '环境描写', '画面感']),
    ('结构混乱', ['结构混乱', '层次不清', '段落', '过渡', '衔接']),
    ('主题偏离', ['偏题', '离题', '主题偏离', '中心不明', '立意']),
    ('素材不足', ['素材不足', '素材单一', '事例不足', '缺少事例']),
    ('语言不够生动', ['语言平淡', '不够生动', '缺少修辞', '文采不足']),
    ('结尾薄弱', ['结尾薄弱', '结尾仓促', '升华不足', '点题']),
    ('开头吸引力不足', ['开头平淡', '开头吸引力', '起笔']),
    ('文体不匹配', ['文体不匹配', '文体', '记叙文', '议论文', '说明文']),
    ('字数不足', ['字数不足', '篇幅偏短', '内容太少']),
]

CURATED_RESOURCE_BANK = [
    {
        'kind': 'article',
        'title': '人物动作描写与细节扩写方法',
        'source': '百度站内检索',
        'summary': '聚合动作、心理、环境等细节描写方法，适合解决描写不足、表达空泛的问题。',
        'tags': ['描写不足', '表达空泛', '语言不够生动', '成长', '校园', '记叙文', '初中', '中小学'],
        'url': _baidu_search_url('中学生作文 动作描写 心理描写 环境描写 方法'),
    },
    {
        'kind': 'article',
        'title': '作文结构与段落衔接训练',
        'source': '国家中小学智慧教育平台',
        'summary': '学习开头、主体、结尾之间的层次安排和过渡句写法，适合结构混乱、结尾薄弱的问题。',
        'tags': ['结构混乱', '结尾薄弱', '开头吸引力不足', '记叙文', '议论文', '初中', '中小学'],
        'url': 'https://basic.smartedu.cn/',
    },
    {
        'kind': 'article',
        'title': '主题素材积累：成长、责任与梦想',
        'source': '央视网教育',
        'summary': '从人物故事、时代新闻和生活细节中提炼作文素材，适合素材不足、表达空泛的问题。',
        'tags': ['素材不足', '表达空泛', '成长', '责任', '梦想', '记叙文', '议论文', '中小学'],
        'url': 'https://edu.cctv.com/',
    },
    {
        'kind': 'article',
        'title': '错别字、标点与病句自查清单',
        'source': '国家中小学智慧教育平台',
        'summary': '围绕错别字、标点、病句和基本格式建立写后检查清单，适合规范基础薄弱的同学。',
        'tags': ['错别字', '病句 / 语句不通', '文体不匹配', '记叙文', '议论文', '说明文', '小学', '初中', '中小学'],
        'url': 'https://basic.smartedu.cn/',
    },
    {
        'kind': 'article',
        'title': '开头结尾写法：点题与升华',
        'source': '光明网教育',
        'summary': '查找开头引入、结尾点题、主题升华等写作方法，适合开头平淡和结尾仓促的问题。',
        'tags': ['开头吸引力不足', '结尾薄弱', '主题偏离', '成长', '责任', '初中', '高中', '中小学'],
        'url': _baidu_search_url('site:gmw.cn 作文 开头 结尾 点题 升华 方法'),
    },
    {
        'kind': 'video',
        'title': '细节描写微课检索',
        'source': '哔哩哔哩公开课程',
        'summary': '检索动作、心理、环境描写相关公开微课，帮助把笼统表达改得更具体。',
        'tags': ['描写不足', '语言不够生动', '表达空泛', '记叙文', '小学', '初中', '中小学'],
        'url': _bilibili_search_url('作文 细节描写 动作 心理 环境 微课'),
        'embed_url': '',
        'allow_embed': False,
    },
    {
        'kind': 'video',
        'title': '作文结构与段落层次微课检索',
        'source': '哔哩哔哩公开课程',
        'summary': '检索作文结构、段落安排和过渡衔接相关微课，适合先解决结构混乱。',
        'tags': ['结构混乱', '结尾薄弱', '开头吸引力不足', '记叙文', '议论文', '初中', '高中', '中小学'],
        'url': _bilibili_search_url('作文 结构 段落 层次 过渡 微课'),
        'embed_url': '',
        'allow_embed': False,
    },
    {
        'kind': 'video',
        'title': '作文素材积累方法微课检索',
        'source': '哔哩哔哩公开课程',
        'summary': '检索素材分类、人物事例和主题素材积累方法，适合素材不足时使用。',
        'tags': ['素材不足', '表达空泛', '成长', '责任', '梦想', '记叙文', '议论文', '中小学'],
        'url': _bilibili_search_url('作文 素材 积累 人物事例 主题素材 微课'),
        'embed_url': '',
        'allow_embed': False,
    },
]

DOMESTIC_RESOURCE_DOMAINS = (
    'baidu.com', 'bilibili.com', 'people.com.cn', 'xinhuanet.com', 'gmw.cn',
    'cctv.com', 'cntv.cn', 'cnr.cn', 'moe.gov.cn', 'zxx.edu.cn',
    'chinawriter.com.cn', 'jyb.cn', 'edu.cn', 'cnki.net', 'xuexi.cn'
)


def _safe_text(value, max_len=240):
    value = (value or '').strip()
    if len(value) > max_len:
        return value[:max_len].rstrip() + '…'
    return value


def extract_resource_profile(title='', content='', correction=''):
    full_text = f'{title}\n{content}\n{correction}'
    theme_scores = []
    for tag, words in RESOURCE_THEME_KEYWORDS.items():
        score = sum(full_text.count(word) for word in words)
        if score > 0:
            theme_scores.append((tag, score))
    theme_scores.sort(key=lambda item: item[1], reverse=True)
    theme_tags = [tag for tag, _ in theme_scores[:4]] or ['成长', '坚持']

    weakness_tags = []
    for label, words in RESOURCE_WEAKNESS_RULES:
        if any(word in full_text for word in words):
            weakness_tags.append(label)
    if not weakness_tags:
        compact_len = len((content or '').replace(' ', '').replace('\n', ''))
        if compact_len < 350:
            weakness_tags.append('素材单一')
        if not any(word in full_text for word in ['像', '仿佛', '如同', '好像', '排比', '比喻', '拟人']):
            weakness_tags.append('语言表达偏平')
        weakness_tags.append('结尾升华不足')
    weakness_tags = weakness_tags[:4]

    query_keywords = []
    for tag in theme_tags[:2]:
        query_keywords.append(f'{tag} 作文素材')
    for weakness in weakness_tags[:2]:
        if '结尾' in weakness:
            query_keywords.append('作文结尾升华方法')
        elif '结构' in weakness:
            query_keywords.append('作文结构 段落层次 微课')
        elif '语言' in weakness or '修辞' in weakness:
            query_keywords.append('作文语言润色 修辞 微课')
        elif '素材' in weakness:
            query_keywords.append('中学生作文素材 事例')
    return {
        'theme_tags': theme_tags,
        'weakness_tags': weakness_tags,
        'query_keywords': query_keywords[:5],
    }


def extract_resource_profile(title='', content='', correction=''):
    full_text = f'{title}\n{content}\n{correction}'
    theme_scores = []
    for tag, words in RESOURCE_THEME_KEYWORDS.items():
        score = sum(full_text.count(word) for word in words)
        if score > 0:
            theme_scores.append((tag, score))
    theme_scores.sort(key=lambda item: item[1], reverse=True)
    theme_tags = [tag for tag, _ in theme_scores[:4]] or ['成长', '梦想']

    weakness_tags = []
    for label, words in RESOURCE_WEAKNESS_RULES:
        if any(word in full_text for word in words):
            weakness_tags.append(label)
    if not weakness_tags:
        compact_len = len((content or '').replace(' ', '').replace('\n', ''))
        if compact_len < 350:
            weakness_tags.append('字数不足')
        if not any(word in full_text for word in ['像', '仿佛', '如同', '好像', '排比', '比喻', '拟人']):
            weakness_tags.append('语言不够生动')
        weakness_tags.append('结尾薄弱')
    weakness_tags = list(dict.fromkeys(weakness_tags))[:4]

    if any(word in full_text for word in ['观点', '论证', '因此', '由此可见', '首先', '其次']):
        genre_tags = ['议论文']
    elif any(word in full_text for word in ['说明', '介绍', '特点', '用途', '原理']):
        genre_tags = ['说明文']
    elif any(word in full_text for word in ['通知', '书信', '倡议书', '演讲稿']):
        genre_tags = ['应用文']
    else:
        genre_tags = ['记叙文']

    query_keywords = []
    for tag in theme_tags[:2]:
        query_keywords.append(f'{tag} 作文素材')
    for weakness in weakness_tags[:2]:
        if '结尾' in weakness or '开头' in weakness:
            query_keywords.append('作文开头结尾 点题升华 方法')
        elif '结构' in weakness:
            query_keywords.append('作文结构 段落层次 过渡 微课')
        elif '语言' in weakness or '病句' in weakness:
            query_keywords.append('作文语言润色 病句修改 微课')
        elif '描写' in weakness:
            query_keywords.append('作文细节描写 动作心理环境 方法')
        elif '素材' in weakness:
            query_keywords.append('中学生作文素材 人物事例')
    return {
        'theme_tags': theme_tags,
        'weakness_tags': weakness_tags,
        'genre_tags': genre_tags,
        'grade_tags': ['中小学'],
        'query_keywords': query_keywords[:5],
    }


def _resource_reason(resource, profile):
    title = resource.get('title', '')
    tags = resource.get('tags') or []
    matched = [tag for tag in (profile['theme_tags'] + profile['weakness_tags']) if tag in tags or tag in title]
    if matched:
        return f"与你当前作文的“{'、'.join(matched[:2])}”方向匹配，可作为后续修改和积累材料。"
    if resource.get('kind') == 'video':
        return '适合用于针对薄弱点进行微课学习，帮助完成“诊断后再练习”。'
    return '适合补充作文素材和写作角度，建议阅读摘要后再打开原文。'


def _format_search_item(item, kind, profile, source_hint='国内公开资源'):
    title = item.get('title') or item.get('name') or '学习资源'
    url = item.get('link') or item.get('url') or item.get('displayLink') or ''
    if url and not any(domain in url for domain in DOMESTIC_RESOURCE_DOMAINS):
        return None
    summary = item.get('snippet') or item.get('description') or item.get('summary') or ''
    source = item.get('source') or item.get('displayLink') or source_hint
    resource = {
        'kind': kind,
        'title': _safe_text(title, 80),
        'source': _safe_text(source, 60),
        'summary': _safe_text(summary or '该资源来自国内公开学习资源检索结果，建议打开原网页查看完整内容。', 180),
        'reason': '',
        'url': url,
        'embed_url': item.get('embed_url') or '',
        'allow_embed': bool(item.get('allow_embed')),
        'preview_mode': 'summary_only',
        'copyright_note': '仅展示标题、摘要、来源与原始链接；完整内容请前往原网站查看。默认面向中国大陆网络环境。',
    }
    resource['reason'] = _resource_reason(resource, profile)
    return resource


def _resource_reason(resource, profile):
    title = resource.get('title', '')
    tags = resource.get('tags') or []
    weakness_match = [tag for tag in profile.get('weakness_tags', []) if tag in tags or tag in title]
    theme_match = [tag for tag in profile.get('theme_tags', []) if tag in tags or tag in title]
    if weakness_match:
        return f"你最近的作文中“{weakness_match[0]}”较明显，因此推荐这项资源帮助你针对性练习。"
    if theme_match:
        return f"这项资源与当前作文的“{theme_match[0]}”主题相关，适合补充素材和写作角度。"
    if resource.get('kind') == 'video':
        return '适合用来针对薄弱点做微课练习，完成“诊断后再训练”的闭环。'
    return '适合补充作文素材和写作方法，建议先阅读摘要，再打开原网页查看。'


def _call_serper_search(query, limit):
    url = SEARCH_API_URL or ''
    headers = {'X-API-KEY': SEARCH_API_KEY, 'Content-Type': 'application/json'}
    if not url:
        return []
    response = requests.post(url, headers=headers, json={'q': query, 'num': limit}, timeout=12)
    response.raise_for_status()
    data = response.json()
    return data.get('organic') or data.get('videos') or []


def _call_bing_search(query, limit):
    url = SEARCH_API_URL or ''
    headers = {'Ocp-Apim-Subscription-Key': SEARCH_API_KEY}
    if not url:
        return []
    response = requests.get(url, headers=headers, params={'q': query, 'count': limit, 'mkt': 'zh-CN'}, timeout=12)
    response.raise_for_status()
    data = response.json()
    return (data.get('webPages') or {}).get('value') or []


def _search_web(query, limit=5):
    if not ENABLE_ONLINE_RESOURCE_SEARCH or not SEARCH_API_KEY or not SEARCH_API_URL:
        return []
    # 正式版不默认连接海外搜索服务；只有显式配置国内/内网可用搜索接口时才调用。
    if SEARCH_PROVIDER == 'bing':
        return _call_bing_search(query, limit)
    return _call_serper_search(query, limit)


def _count_tag_matches(target, tags):
    tag_set = set(tags or [])
    return len([tag for tag in target or [] if tag in tag_set])


def _resource_match_score(resource, profile):
    tags = resource.get('tags') or []
    weakness = _count_tag_matches(profile.get('weakness_tags', []), tags)
    theme = _count_tag_matches(profile.get('theme_tags', []), tags)
    genre = _count_tag_matches(profile.get('genre_tags', []), tags)
    grade = _count_tag_matches(profile.get('grade_tags', []), tags)
    return weakness * 45 + theme * 20 + genre * 15 + grade * 10


def _fallback_resources(profile):
    selected = []
    wanted = set(profile['theme_tags'] + profile['weakness_tags'] + profile.get('genre_tags', []) + profile.get('grade_tags', []))
    for item in CURATED_RESOURCE_BANK:
        tags = set(item.get('tags') or [])
        if wanted & tags:
            selected.append(item)
    if len(selected) < 4:
        selected.extend([item for item in CURATED_RESOURCE_BANK if item not in selected])
    selected = sorted(selected, key=lambda item: _resource_match_score(item, profile), reverse=True)
    articles, videos = [], []
    for item in selected:
        resource = dict(item)
        resource.setdefault('embed_url', '')
        resource.setdefault('allow_embed', False)
        resource['reason'] = _resource_reason(resource, profile)
        resource['match_score'] = _resource_match_score(resource, profile)
        resource['preview_mode'] = 'summary_only'
        resource['copyright_note'] = '该卡片只提供检索入口、摘要和推荐理由，不搬运第三方全文或视频。'
        if resource.get('kind') == 'video':
            videos.append(resource)
        else:
            articles.append(resource)
    return articles[:3], videos[:3]


def recommend_online_resources(title='', content='', correction='', limit=4):
    profile = extract_resource_profile(title, content, correction)
    articles, videos = [], []
    mode = 'curated_fallback'
    errors = []

    if ENABLE_ONLINE_RESOURCE_SEARCH and SEARCH_API_KEY:
        queries = profile['query_keywords'] or ['中学生作文素材', '作文写作技巧 微课']
        for query in queries[:3]:
            try:
                kind = 'video' if ('微课' in query or '视频' in query) else 'article'
                results = _search_web(query, min(SEARCH_RESULT_LIMIT, limit))
                formatted = [_format_search_item(item, kind, profile) for item in results if (item.get('link') or item.get('url'))]
                formatted = [item for item in formatted if item]
                if kind == 'video':
                    videos.extend(formatted)
                else:
                    articles.extend(formatted)
                if formatted:
                    mode = 'domestic_online_search'
            except Exception as exc:
                errors.append(str(exc))

    if len(articles) < 2 or len(videos) < 1:
        fallback_articles, fallback_videos = _fallback_resources(profile)
        existing_urls = {item.get('url') for item in articles + videos}
        articles.extend([item for item in fallback_articles if item.get('url') not in existing_urls])
        videos.extend([item for item in fallback_videos if item.get('url') not in existing_urls])

    return {
        'mode': mode,
        'search_enabled': bool(ENABLE_ONLINE_RESOURCE_SEARCH and SEARCH_API_KEY and SEARCH_API_URL),
        'theme_tags': profile['theme_tags'],
        'weakness_tags': profile['weakness_tags'],
        'genre_tags': profile.get('genre_tags', []),
        'grade_tags': profile.get('grade_tags', []),
        'query_keywords': profile['query_keywords'],
        'articles': articles[:limit],
        'videos': videos[:limit],
        'errors': errors[:2],
        'compliance_notice': '智笔仅推荐中国大陆网络环境下常见可访问的公开学习资源；只展示标题、来源、摘要、推荐理由和原始链接，不抓取第三方全文，不下载或搬运视频。支持合法嵌入的公开视频才会站内播放，否则跳转原网站。',
    }
