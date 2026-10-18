"""
AI 与 OCR 相关接口
支持 DeepSeek；未配置 Key 时使用基础规则结果，保证核心流程可用。
"""
import base64
import html
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from flask import request, jsonify, Response, stream_with_context, send_from_directory, current_app
from config.settings import ENABLE_LOCAL_FALLBACK
from utils.api_utils import call_deepseek, stream_deepseek, parse_stream_chunks, recognize_image_with_baidu, has_deepseek_key
from utils.db_utils import (
    get_db_connection,
    save_history,
    get_recent_user_behaviors,
    save_personalized_recommendation,
    get_personalized_recommendation,
)
from routes.prompt_templates import (
    DEFAULT_TARGET_WORD_COUNT,
    FULL_TEXT_MIN_WORD_COUNT,
    FULL_TEXT_MAX_WORD_COUNT,
    FULL_TEXT_IDEAL_WORD_COUNT,
    TEXT_RESULT_SYSTEM_PROMPT,
    JSON_RESULT_SYSTEM_PROMPT,
    NDJSON_RESULT_SYSTEM_PROMPT,
    build_messages,
    build_outline_prompt,
    build_opening_prompt,
    build_detail_expand_prompt,
    build_full_text_prompt,
    build_full_text_extension_prompt,
    build_correction_prompt,
    build_correction_stream_prompt,
    build_polish_prompt,
    build_structure_prompt,
    build_inline_comment_prompt,
    build_continue_prompt,
    build_ending_prompt,
)


def sse_payload(payload):
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_response(generator):
    response = Response(stream_with_context(generator), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response


def sse_error(message):
    return sse_response(iter([sse_payload({'error': message})]))


def stream_text(text, delay=0.01, source='local_fallback', message='已为你整理基础建议'):
    def generate():
        full = ''
        yield sse_payload({'type': 'start', 'source': source, 'message': message})
        chunks = re.split(r'(。|！|？|\n)', text)
        buffer = ''
        for part in chunks:
            if part in {'。', '！', '？', '\n'}:
                buffer += part
                if buffer:
                    full += buffer
                    yield sse_payload({'content': buffer})
                    buffer = ''
                    time.sleep(delay)
            else:
                buffer += part
        if buffer:
            full += buffer
            yield sse_payload({'content': buffer})
        yield sse_payload({
            'done': True,
            'full_result': full,
            'source': source,
            'message': message,
        })
    return sse_response(generate())


def split_sentences(text):
    parts = re.findall(r'[^。！？!?]+[。！？!?]?', text or '')
    return [p.strip() for p in parts if p.strip()]


def count_essay_chars(content=''):
    compact = re.sub(r'\s+', '', content or '')
    chinese_count = len(re.findall(r'[\u4e00-\u9fff]', compact))
    return chinese_count or len(compact)


def get_word_count_guidance(length):
    if length < 400:
        return '内容偏短，需要补充具体事件和细节。'
    if length < 750:
        return f'已有基本内容，建议继续扩写，使全文接近 {DEFAULT_TARGET_WORD_COUNT} 字。'
    if length <= 850:
        return f'已接近 {DEFAULT_TARGET_WORD_COUNT} 字目标，完成度较高。'
    return '篇幅较充足，请注意详略安排和表达精炼。'


def clean_inline_text(value=''):
    text = str(value or '')
    text = text.replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    text = re.sub(r'^[\s　•\-、，。]+', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


WRITING_ASSISTANT_SYSTEM_PROMPT = TEXT_RESULT_SYSTEM_PROMPT


def clean_writing_output(text):
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return ''
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group(0).replace('```', ''), text)
    text = re.sub(r'^\s*以下是[^。\n]*[：:]\s*', '', text)
    text = re.sub(r'^\s*(我为你|为你|给你)[^。\n]*[：:]\s*', '', text)
    text = re.sub(r'^\s*(作文开头|开头|作文结尾|结尾|续写|润色后|优化后作文|优化后正文|完整作文|正文)\s*[：:]\s*', '', text)
    text = text.replace('**', '').replace('__', '').replace('`', '')
    lines = []
    cut_markers = re.compile(r'^\s*(写作小贴士|小贴士|修改建议|写作建议|说明|分析|点评|可选结尾|注意事项)\s*(?:[:：].*)?$')
    label_markers = re.compile(r'^\s*(作文开头|开头|作文结尾|结尾|续写|润色后|优化后作文|优化后正文|完整作文|正文)\s*[：:]?\s*$')
    label_prefix = re.compile(r'^\s*(作文开头|开头|作文结尾|结尾|续写|润色后|优化后作文|优化后正文|完整作文|正文)\s*[：:]\s*')
    for raw_line in text.split('\n'):
        line = raw_line.strip()
        if not line:
            lines.append('')
            continue
        if re.fullmatch(r'[-*_—]{3,}', line):
            continue
        line = re.sub(r'^\s*#{1,6}\s*', '', line)
        line = re.sub(r'^\s*>\s?', '', line)
        line = re.sub(r'^\s*[-*+]\s+', '', line)
        line = re.sub(r'^\s*\d+[\.、]\s+', '', line)
        if label_markers.match(line):
            continue
        line = label_prefix.sub('', line).strip()
        if not line:
            continue
        if cut_markers.match(line):
            break
        if re.match(r'^\s*(以下是|我为你|建议你|可以这样写)', line):
            continue
        lines.append(line)
    cleaned = '\n'.join(lines)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


def normalize_genre_name(genre='', title='', requirement='', content=''):
    explicit = str(genre or '').strip()
    if '说明' in explicit:
        return '说明文'
    if '议论' in explicit:
        return '议论文'
    if re.search(r'应用|书信|倡议|通知|演讲|发言', explicit):
        return '应用文'
    if '记叙' in explicit:
        return '记叙文'
    context = f'{genre} {title} {requirement} {content[:180]}'.lower()
    if re.search(r'说明文|介绍|说明|科普|原理|特点', context):
        return '说明文'
    if re.search(r'议论文|议论|论点|论证|观点|演讲稿', context):
        return '议论文' if '演讲稿' not in context else '应用文'
    if re.search(r'应用文|书信|倡议书|通知|演讲稿|申请书|感谢信|发言稿', context):
        return '应用文'
    return '记叙文'


def get_full_text_requirement_flags(requirement=''):
    """Extract only requirements that can be checked reliably after generation."""
    text = clean_inline_text(requirement)
    return {
        'first_person': bool(re.search(r'第一人称|以[“\"]?我[”\"]?的口吻', text)),
        'third_person': bool(re.search(r'第三人称|以(?:他|她|人物)为主人公', text)),
        'dialogue': bool(re.search(r'对话|语言描写', text)),
        'action': bool(re.search(r'动作描写|动作细节', text)),
        'psychology': bool(re.search(r'心理描写|心理活动', text)),
        'environment': bool(re.search(r'环境描写|景物描写', text)),
        'metaphor': bool(re.search(r'比喻', text)),
        'parallelism': bool(re.search(r'排比', text)),
        'comparison': bool(re.search(r'作比较|对比说明|进行比较', text)),
        'echo': bool(re.search(r'首尾呼应|照应开头|照应题目', text)),
        'example': bool(re.search(r'具体事例|举例|例子|事实论据', text)),
    }


def full_text_requirement_issues(text, requirement='', genre='', title='', theme=''):
    """Return unmet explicit requirements without treating vague prose as hard rules."""
    flags = get_full_text_requirement_flags(requirement)
    if not any(flags.values()):
        return []
    cleaned = clean_writing_output(text)
    body_lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    body = '\n'.join(body_lines[1:]) if len(body_lines) > 1 else cleaned
    issues = []
    normalized_genre = normalize_genre_name(genre, title, requirement, body)
    if flags['first_person'] and len(re.findall(r'我|我们', body)) < 3:
        issues.append('第一人称')
    if flags['third_person'] and normalized_genre == '记叙文':
        third_person_count = len(re.findall(r'他|她|小林|少年|女孩|男孩|同学|主人公', body))
        if third_person_count < 3 or len(re.findall(r'我|我们', body)) > third_person_count:
            issues.append('第三人称')
    feature_patterns = {
        'dialogue': r'[“\"]([^”\"]{2,})[”\"]|说道|问道|回答|喊道',
        'action': r'抬头|低头|伸手|握住|放下|走到|跑向|转身|停下|拿起|推开|整理|检查',
        'psychology': r'心里|心想|想到|犹豫|紧张|担心|后悔|明白|意识到|感到',
        'environment': r'阳光|月光|灯光|天空|窗外|雨|风|树影|操场|教室|走廊|暮色',
        'metaphor': r'像|仿佛|如同|好似|宛如',
        'parallelism': r'(?:不是[^，。；]{2,}[，；]){2}|(?:有的[^，。；]{2,}[，；]){2}|(?:一边[^，。；]{2,}[，；]){2}|不仅[^，。；]+(?:而且|还)',
        'comparison': r'相比|比较|不同于|而[^。；]{2,}(?:则|却|更)|一方面[^。；]+另一方面',
        'example': r'例如|比如|一次|那天|有一回|事实|经历|事例',
    }
    labels = {
        'dialogue': '对话描写', 'action': '动作描写', 'psychology': '心理描写',
        'environment': '环境描写', 'metaphor': '比喻', 'parallelism': '排比',
        'comparison': '作比较', 'example': '具体事例',
    }
    for key, pattern in feature_patterns.items():
        if flags[key] and not re.search(pattern, body):
            issues.append(labels[key])
    if flags['echo']:
        subject = clean_inline_text(theme or title).strip('《》“” ')
        first_part = ''.join(body_lines[1:3]) if len(body_lines) > 2 else body[:180]
        last_part = body_lines[-1] if body_lines else body[-180:]
        if subject and (subject not in first_part or subject not in last_part):
            issues.append('首尾呼应')
    return issues


def apply_local_requirement_details(text, subject, genre='', requirement='', title=''):
    """Make the local fallback honor explicit, checkable writing requirements."""
    result = str(text or '').strip()
    normalized_genre = normalize_genre_name(genre, title, requirement, result)
    flags = get_full_text_requirement_flags(requirement)
    if normalized_genre == '记叙文' and flags['third_person']:
        result = result.replace('我们', '他们').replace('我的', '小林的').replace('我会', '小林会').replace('我', '小林')
    narrator = '小林' if normalized_genre == '记叙文' and flags['third_person'] else '我'
    partner = '同伴' if narrator == '我' else '同学'
    additions = []
    if flags['dialogue'] and not re.search(r'[“\"]([^”\"]{2,})[”\"]', result):
        additions.append(f'{partner}看着{narrator}，认真地说：“先别急，我们把与{subject}有关的问题一项项列出来。”{narrator}点点头，把刚才遗漏的步骤重新写好，也把自己的想法清楚地告诉了大家。')
    if flags['environment'] and not re.search(r'阳光|月光|灯光|天空|窗外|雨|风|树影|操场|教室|走廊', result):
        additions.append(f'窗外的光慢慢移过桌角，走廊里传来轻轻的脚步声。这样的安静让{narrator}重新看清眼前的任务，也让关于{subject}的选择显得更加具体。')
    if flags['metaphor'] and not re.search(r'像|仿佛|如同|好似|宛如', result):
        additions.append(f'这次经历像一面清楚的镜子，让{narrator}看见面对{subject}时的犹豫，也看见认真行动之后真实的改变。')
    if flags['parallelism'] and not re.search(r'不是[^，。；]+[，；].*不是|有的[^，。；]+[，；].*有的|不仅[^，。；]+(?:而且|还)', result):
        additions.append(f'{subject}不是一句随口说出的保证，不是遇到困难时的短暂冲动，也不是只看结果的自我安慰；它是看清问题后的选择，是承担任务时的坚持，也是完成之后诚实的反思。')
    if flags['comparison'] and normalized_genre == '说明文' and not re.search(r'相比|比较|不同于|而[^。；]{2,}(?:则|却|更)', result):
        additions.append(f'与只凭表面印象判断相比，按照条件记录并比较{subject}的不同表现，能够更准确地发现规律；前者容易忽略变化原因，后者则能把特点、条件和结果联系起来。')
    if flags['example'] and normalized_genre == '议论文' and not re.search(r'例如|比如|事实|事例', result):
        additions.append(f'例如，在一次小组任务中，主动核对分工、发现遗漏后及时补救，比单纯强调{subject}更能说明观点。这个普通事例表明，抽象认识只有进入具体行动，才具有真正的说服力。')
    if additions:
        paragraphs = [part.strip() for part in result.split('\n\n') if part.strip()]
        insert_at = max(1, len(paragraphs) - 1)
        paragraphs[insert_at:insert_at] = additions
        result = '\n\n'.join(paragraphs)
    return result


def build_genre_expansion_paragraphs(subject, genre='', requirement='', title=''):
    normalized_genre = normalize_genre_name(genre, title, requirement)
    requirement_hint = clean_inline_text(requirement)
    if normalized_genre == '议论文':
        return [
            f'进一步看，{subject}并不只影响一次选择，还会影响一个人长期形成的判断方式。面对复杂情况时，先辨明目标，再比较不同做法的后果，最后为自己的选择负责，才能让关于{subject}的认识从口号变成稳定的行动准则。',
            f'有人担心强调{subject}会增加负担，其实真正有效的做法不是盲目坚持，而是依据事实及时调整。方向正确时保持耐心，发现错误时主动修正，能力不足时寻求帮助，这些都体现了对{subject}更完整、更理性的理解。',
            f'在日常学习和集体生活中，检验{subject}最可靠的标准是具体行动。认真完成一次约定、及时补上一个疏漏、在意见不同时说明理由并尊重他人，这些普通小事比空泛表态更有说服力，也能让中心观点得到现实支撑。',
            f'因此，讨论{subject}不能停留在“它很重要”的层面。只有把认识落实为清楚的目标、持续的行动和诚实的反思，才能真正体现它的价值。{requirement_hint}' if requirement_hint else f'因此，讨论{subject}不能停留在“它很重要”的层面。只有把认识落实为清楚的目标、持续的行动和诚实的反思，才能真正体现它的价值。',
        ]
    if normalized_genre == '说明文':
        return [
            f'理解{subject}，还要注意它在不同场景中的具体表现。观察对象、记录现象、比较差异，再分析形成原因，是认识这一主题的基本方法。这样得到的结论有事实依据，也能避免只凭印象作判断。',
            f'从实际应用看，{subject}往往与我们的学习和生活密切相关。使用相关方法时，应先明确条件和步骤，再关注效果及可能出现的问题。只有把原理、过程和结果联系起来，说明才会清楚完整。',
            f'此外，介绍{subject}不能只罗列特点，还应说明这些特点为什么形成、彼此有什么联系。通过分类说明、举例说明和作比较，可以让抽象内容变得具体，使读者更容易把握重点。',
            f'总的来说，认识{subject}既需要准确概念，也需要真实例子和规范步骤。沿着“是什么、为什么、怎样做、要注意什么”的顺序展开，能够更全面地呈现这一主题。{requirement_hint}' if requirement_hint else f'总的来说，认识{subject}既需要准确概念，也需要真实例子和规范步骤。沿着“是什么、为什么、怎样做、要注意什么”的顺序展开，能够更全面地呈现这一主题。',
        ]
    if normalized_genre == '应用文':
        return [
            f'为了让有关{subject}的安排真正落实，参与者应先了解目标和具体步骤，按照约定时间完成各自任务；遇到变化要及时说明，发现遗漏要主动补充，确保信息传达准确、行动衔接顺畅。',
            f'实施过程中，我们还应关注安全、秩序和实际效果。既要认真完成个人负责的部分，也要尊重他人意见、保持必要沟通，让每一项关于{subject}的行动都清楚、可执行、可检查。',
            f'本次关于{subject}的行动面向所有相关同学和老师。各班或各小组可结合实际情况细化分工，但不得改变核心目标；需要提交材料时，应确保内容真实、时间明确、责任到人，避免只写口号却没有具体安排。',
            f'活动或任务结束后，请及时整理过程记录，并从参与情况、实际效果和存在问题三个方面进行检查。对已经完成的事项予以确认，对暂未解决的问题列出后续计划，使{subject}相关工作能够形成完整闭环。',
            f'如果在执行中遇到困难，可以向老师、负责人或同伴反馈。反馈时应说明具体情况、已经尝试的方法和需要的帮助，以便及时协调资源、调整步骤，共同保证关于{subject}的计划有序推进。',
            f'希望大家从现在开始，以具体行动回应{subject}这一主题。让一次倡议或安排不只停留在纸面，而是成为共同参与、互相提醒和持续改进的过程。{requirement_hint}' if requirement_hint else f'希望大家从现在开始，以具体行动回应{subject}这一主题，让计划真正落实。',
        ]
    return [
        f'事情最关键的时刻，我重新想起这次经历与{subject}的联系。我没有急着给自己找理由，而是把眼前的问题一项项看清：哪里出了差错，接下来能做什么，又该怎样向相关的人说明。这样的思考让我从慌乱中慢慢安定下来。',
        f'我先完成最需要处理的一步，又回到现场检查遗漏。周围的声音、人物的动作和当时的细节依然清楚，它们共同推动着这件关于{subject}的事情向前发展，也让我看见自己的态度正在发生变化。',
        f'当问题终于得到解决，我没有只关注结果，而是回想自己从犹豫到行动的过程。真正让我理解{subject}的，正是那个必须作出选择的瞬间，以及选择之后愿意承担、补救和坚持的每一个动作。',
        f'回望全文，{subject}不再是一个抽象词语，而是这次具体经历留给我的认识。{requirement_hint}' if requirement_hint else f'回望全文，{subject}不再是一个抽象词语，而是这次具体经历留给我的认识。以后面对相似情境，我会记得这次选择，并用更成熟的行动回应。',
    ]


def ensure_local_full_length(text, subject, genre='', requirement='', title=''):
    additions = build_genre_expansion_paragraphs(subject, genre, requirement, title)
    result = str(text or '').strip()
    for paragraph in additions:
        if count_essay_chars(result) >= FULL_TEXT_IDEAL_WORD_COUNT:
            break
        result += f'\n\n{paragraph}'
    return trim_full_text_to_limit(result)


def trim_full_text_to_limit(text, minimum=FULL_TEXT_MIN_WORD_COUNT, maximum=FULL_TEXT_MAX_WORD_COUNT):
    """Keep a generated essay within the target range and finish on a complete sentence."""
    cleaned = clean_writing_output(text)
    if count_essay_chars(cleaned) <= maximum:
        return cleaned

    count_chinese_only = bool(re.search(r'[\u4e00-\u9fff]', cleaned))
    chinese_count = 0
    maximum_index = len(cleaned)
    sentence_end_index = None
    for index, char in enumerate(cleaned):
        if ('\u4e00' <= char <= '\u9fff') if count_chinese_only else (not char.isspace()):
            chinese_count += 1
        if chinese_count >= minimum and char in '。！？':
            sentence_end_index = index + 1
        if chinese_count >= maximum:
            maximum_index = index + 1
            break

    cut_index = sentence_end_index or maximum_index
    result = cleaned[:cut_index].rstrip('，、；：,;: \n')
    if result and result[-1] not in '。！？':
        result += '。'
    return result


def full_text_matches_context(text, title='', theme='', genre='', requirement=''):
    cleaned = clean_writing_output(text)
    if not cleaned:
        return False
    nonempty_lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    first_line = re.sub(r'[《》“”"\s]+', '', nonempty_lines[0] if nonempty_lines else '')
    expected_title = re.sub(r'[《》“”"\s]+', '', title or '')
    if expected_title and expected_title not in first_line and first_line not in expected_title:
        return False

    body = '\n'.join(nonempty_lines[1:]) if len(nonempty_lines) > 1 else cleaned
    context = f'{theme} {title}'
    known_topics = [
        word for word in (
            '责任', '负责', '成长', '坚持', '梦想', '亲情', '友情', '友谊', '诚信', '勇气',
            '担当', '感恩', '环保', '自然', '科技', '人工智能', '劳动', '读书', '家乡', '传统文化'
        ) if word in context
    ]
    clean_theme = re.sub(r'[《》“”"\s，。！？、：；]+', '', theme or '')
    if 2 <= len(clean_theme) <= 10 and clean_theme not in known_topics:
        known_topics.append(clean_theme)
    if known_topics and not any(topic in body for topic in known_topics):
        return False

    normalized_genre = normalize_genre_name(genre, title, requirement, body)
    body_paragraphs = [part.strip() for part in re.split(r'\n+', body) if part.strip()]
    if normalized_genre == '议论文' and len(body_paragraphs) < 4 and not re.search(
        r'观点|首先|其次|因此|所以|可见|由此|诚然|一方面|另一方面|事实|例如|比如|论证|'
        r'我们应|应当|应该|这说明|这表明|不仅|然而|重要|意义',
        body,
    ):
        return False
    if normalized_genre == '说明文' and len(body_paragraphs) < 4 and not re.search(
        r'是指|属于|包括|特点|原因|作用|分类|例如|比如|说明|原理|条件|过程|方法|功能|影响|注意',
        body,
    ):
        return False
    if normalized_genre == '应用文' and not re.search(r'亲爱的|尊敬的|通知|倡议|大家|此致|谢谢', cleaned):
        return False
    if full_text_requirement_issues(cleaned, requirement, normalized_genre, title, theme):
        return False
    return True


def normalize_full_text_length(text, title='', theme='', genre='', requirement=''):
    """Normalize every full-essay result before it reaches the front end."""
    cleaned = clean_writing_output(text)
    subject = (theme or title or '认真完成一件事').strip('《》“” ') or '认真完成一件事'
    if cleaned and not full_text_matches_context(cleaned, title, theme, genre, requirement):
        cleaned = build_local_full_essay(title, theme, genre, requirement)
    if count_essay_chars(cleaned) < FULL_TEXT_MIN_WORD_COUNT:
        cleaned = ensure_local_full_length(cleaned, subject, genre, requirement, title)
    if count_essay_chars(cleaned) < FULL_TEXT_MIN_WORD_COUNT:
        local_text = local_generate_text('full', title, '', theme, genre, requirement)
        if count_essay_chars(local_text) > count_essay_chars(cleaned):
            cleaned = local_text
    return trim_full_text_to_limit(cleaned)


def build_local_full_essay(title='', theme='', genre='', requirement=''):
    subject = (theme or title or '认真完成一件事').strip('《》“” ') or '认真完成一件事'
    essay_title = (title or subject).strip('《》')
    normalized_genre = normalize_genre_name(genre, title, requirement)
    requirement_focus = re.sub(r'(?:^|；)指定文体[：:][^；]+', '', requirement or '').strip('； ')

    if normalized_genre == '议论文':
        draft = f'''{essay_title}

{subject}不是一句用来装饰文章的口号，而是需要通过认识、选择和行动来证明的价值。面对学习与生活中的问题，一个人怎样判断、怎样行动、怎样承担结果，最能说明他是否真正理解{subject}。因此，我们既要看到它的意义，也要思考它怎样进入现实生活。

首先，{subject}能帮助我们建立清楚的目标。没有目标，努力容易变成忙乱；只有知道为什么出发，才能在遇到困难时判断哪些事情应该坚持，哪些方法需要调整。目标不是僵硬的命令，而是提醒我们把有限时间用在真正重要的事情上。

其次，{subject}必须落实为持续行动。一次热情可以让人迈出第一步，却不能保证事情真正完成。按计划推进、出现问题及时修正、无人提醒时仍认真做好本分，这些看似普通的行动，才让{subject}拥有可信的分量。

再次，{subject}还意味着愿意面对结果。成功时不过分夸大个人作用，失败时不急着推卸责任，而是回到过程寻找原因，这种态度能够把一次得失转化为下一次进步的基础。真正成熟的选择，往往就体现在这一点上。

有人认为，只要结果不错，过程是否认真并不重要。其实，偶然得到的好结果不能代替可靠的能力。如果忽视规则、合作和反思，即使暂时顺利，也很难获得长久信任。相反，重视过程的人能够不断修正方法，让行动越来越稳定。

对中小学生来说，{subject}并不遥远。认真完成一次分工，发现错误后主动订正，答应别人的事情按时做到，在集体需要时补上空缺，都是它的具体表现。小事虽然没有惊人的声势，却最能检验一个人的态度。

因此，我们不能只说{subject}很重要，更要让它成为每天可以执行的选择。明确目标、踏实行动、诚实复盘，再把认识带到下一次实践中，才能让{subject}真正转化为成长的力量。'''
    elif normalized_genre == '说明文':
        draft = f'''{essay_title}

{subject}是我们认识世界时需要了解的一个重要主题。要把它说明清楚，不能只给出一句简单判断，而应从概念、特点、形成原因、实际作用和注意事项等方面逐层展开，让读者既知道“是什么”，也明白“为什么”和“怎样运用”。

首先，可以从基本含义认识{subject}。它通常不是孤立存在的，而是由多个因素共同形成。观察时要明确对象和范围，避免把相似现象混为一谈；表达时则要使用准确、通俗的语言，使概念边界清楚。

其次，{subject}具有可以观察和比较的特点。我们可以记录它在不同时间、不同环境或不同条件下的表现，再寻找其中稳定的规律。通过分类说明，原本零散的信息会形成清楚层次，读者也更容易把握重点。

再次，理解{subject}还要分析形成原因。一个现象往往同时受到环境、条件和使用方式的影响，不能只根据表面结果下结论。把原因与结果对应起来，并用真实例子加以说明，能够使文章更严谨、更有说服力。

在实际生活中，{subject}与我们的学习、选择和行动都有联系。正确运用相关知识，可以帮助我们提高效率、减少误解，也能提醒我们注意可能出现的问题。说明实际作用时，应交代具体场景，而不是只罗列抽象好处。

当然，认识{subject}也要注意适用条件。任何方法都有边界，不同对象和环境需要不同处理方式。使用前明确目标，过程中观察变化，结束后检查效果，才能避免机械照搬，让知识真正解决问题。

总的来说，说明{subject}应按照“概念—特点—原因—作用—注意事项”的顺序展开，并综合使用举例子、作比较和分类别等方法。这样既能保证信息准确，也能让读者获得完整、清楚的认识。'''
    elif normalized_genre == '应用文':
        is_letter = bool(re.search(r'书信|感谢信|一封信', f'{title}{requirement}'))
        is_notice = bool(re.search(r'通知', f'{title}{requirement}'))
        is_speech = bool(re.search(r'演讲稿|发言稿', f'{title}{requirement}'))
        if is_letter:
            draft = f'''{essay_title}

亲爱的朋友：

你好！我想和你认真谈谈{subject}。最近的学习和生活让我发现，它并不是课本里一个遥远的词，而是藏在每天的选择中：怎样完成答应的事情，怎样面对出现的问题，又怎样在结果不理想时重新调整。

我曾经以为，只要有一个好的想法就足够了。真正开始行动后才明白，目标需要拆成步骤，过程需要耐心检查，遇到变化还要及时沟通。正是这些具体环节，让我对{subject}有了更真实的认识。

我也希望和你一起把这种认识落实下来。我们可以先确定一件能够完成的小事，约定时间和分工；过程中互相提醒，遇到困难不急着放弃；结束后再看看哪些地方做得好，哪些地方需要改进。

如果一次没有做到，也不必用一句“我不行”否定自己。找到原因、补上遗漏、重新开始，本身就是负责任的表现。真正的进步不是从不犯错，而是每次都愿意把错误变成下一步行动的依据。

希望我们都能在普通日子里认真理解{subject}，把想法变成行动，把一次行动变成稳定习惯。也期待听到你的想法，看看我们还能怎样做得更具体、更踏实。

祝
学习进步！

你的朋友'''
        elif is_notice:
            draft = f'''关于开展“{subject}”主题活动的通知

各班同学：

为帮助大家深入理解{subject}，并把相关认识落实到学习和校园生活中，学校拟开展“{subject}”主题实践活动。现将活动目标、参与方式和注意事项通知如下，请各班认真阅读并做好准备。

一、活动目标。围绕{subject}开展观察、记录和实践，通过真实任务理解主题含义，形成清楚表达，并在合作中提高沟通、执行和反思能力。

二、活动内容。各小组结合校园生活确定一个具体问题，先调查实际情况，再制定可执行方案。过程材料应包括分工表、观察记录、行动照片或文字说明，以及活动后的总结。

三、实施步骤。第一阶段完成选题和分工；第二阶段按照计划开展实践并记录变化；第三阶段整理材料，说明采取了什么行动、遇到什么困难、怎样调整以及最终效果。

四、成果要求。每组提交一份结构清楚的文字报告，并准备简短交流。报告应紧扣{subject}，不得只抄资料或空喊口号，要用事实、数据或具体经历说明问题。

五、注意事项。活动中要遵守校园规则，注意安全，尊重他人，不影响正常教学秩序。若计划发生变化，应及时与指导老师沟通，不得隐瞒问题或编造记录。

请各班按要求组织实施，让本次活动真正成为认识{subject}、实践{subject}的一次学习过程。

校学生发展中心'''
        else:
            greeting = '尊敬的老师、亲爱的同学们：' if is_speech else '亲爱的同学们：'
            draft = f'''{essay_title}

{greeting}

今天我想围绕{subject}发出一份真诚倡议。它看起来是一个熟悉的词，但真正落实时，需要我们从具体问题出发，用清楚的目标和持续行动作出回答。

我们常常愿意表达赞同，却容易忽略最普通的细节。一次按时完成的约定、一次发现问题后的主动补救、一次合作中的认真倾听，都能让{subject}从口号变成看得见的行动。

为此，我倡议大家从三方面开始。第一，明确自己能够承担的一项任务，不把责任停留在别人提醒之后；第二，记录行动过程，遇到困难及时沟通并调整；第三，完成后认真复盘，用事实检查是否真正解决了问题。

同时，我们要避免追求表面热闹。有关{subject}的行动不在于形式多么复杂，而在于是否贴近实际、是否能够坚持、是否给自己和集体带来真实改变。每个人都从一件小事做起，就能形成可靠的共同力量。

希望大家从今天开始作出一个具体选择，并认真完成。让我们用行动理解{subject}，用坚持守住承诺，也用每一次改进证明自己正在成长。

谢谢大家！'''
    else:
        context = f'{title}{theme}{requirement}'
        if re.search(r'亲情|外婆|奶奶|爷爷|爸爸|妈妈|家人', context):
            scene = '一次与家人共同完成家务并坦诚交流的经历'
        elif re.search(r'环保|自然|垃圾|绿色', context):
            scene = '一次校园环保值日中发现并解决问题的经历'
        elif re.search(r'科技|人工智能|实验|发明', context):
            scene = '一次科技实践中反复调试作品的经历'
        elif re.search(r'责任|担当|集体', context):
            scene = '一次承担班级任务并处理突发问题的经历'
        elif re.search(r'梦想|坚持|努力', context):
            scene = '一次为目标训练并克服中途困难的经历'
        else:
            scene = f'一次让我真正理解{subject}的具体经历'
        draft = f'''{essay_title}

那天发生的事，让我第一次认真思考{subject}。这不是一个突然出现的答案，而是从{scene}开始，在犹豫、行动和反思中一点点变得清楚。现在回想起来，许多当时没有留意的细节，都在悄悄推动我的认识发生变化。

事情开始时，我并没有把任务想得很复杂。我按照原来的习惯准备，觉得只要完成表面要求就可以。真正动手后，意外接连出现：原定步骤无法继续，有人提出不同意见，我负责的部分也暴露出疏漏。周围的声音一下子变得清楚，我握紧手里的材料，心里既着急又有些退缩。

短暂沉默后，我没有把问题推给别人，而是重新核对目标。先处理最紧急的一项，再说明当前情况，接着邀请伙伴一起寻找遗漏。我们把混乱的任务重新排序，一项项确认。过程中，我几次想用“差不多”结束，可想到{subject}真正要求的并不是应付，便停下来继续检查。

最难的部分出现在临近完成的时候。一个细节如果不改，之前的努力就会留下明显缺口；重新处理却要花费更多时间。我站在原地权衡片刻，最终选择返工。手上的动作比刚开始更慢，却也更坚定。正是在这个决定里，我开始理解{subject}与自己的关系。

伙伴没有责怪我的疏漏，而是和我一起补救。有人负责重新整理，有人核对记录，我则把问题产生的原因写清楚。大家的配合让我明白，{subject}不是一个人逞强，也不是把所有事情揽到自己身上，而是在共同任务中做好本分、听取意见，并对结果保持诚实。

任务完成时，结果并不完美，但每一步都有了清楚交代。我看着整理好的成果，心里没有想象中的兴奋，反而多了一份踏实。因为我知道，真正重要的变化不是事情终于结束，而是我在遇到麻烦时没有逃开。

这次经历让我看到，{subject}藏在具体选择里：是发现问题后主动说明，是承诺之后认真完成，也是结果不理想时愿意承担并改进。它没有响亮的声音，却能让一个人的行动变得可靠。

以后再面对相似情况，我仍可能紧张，也可能犯错，但我会记得这次经历。先看清问题，再迈出能够完成的一步；既尊重共同目标，也认真对待自己的责任。这样，{subject}就不再只是题目中的词，而会成为我继续成长时可以依靠的力量。'''

    draft = apply_local_requirement_details(draft, subject, normalized_genre, requirement_focus, essay_title)
    return ensure_local_full_length(draft, subject, normalized_genre, requirement_focus, essay_title)


def local_generate_text(kind, title, content, theme='', genre='', requirement=''):
    if kind == 'opening':
        return f'每当想起{title or "那段经历"}，我总会想起那个让我悄悄改变的瞬间。它并不轰轰烈烈，却像一束温柔的光，照亮了我原本犹豫的心，也让我明白，真正的成长常常藏在一次认真面对、一点主动改变之中。'
    if kind == 'ending':
        return f'回望这段经历，我终于懂得，{title or "成长"}并不是一句空泛的口号，而是在一次次选择中学会坚持，在一次次不足中愿意改变。以后再遇到困难，我也会带着这份勇气继续向前，因为每一步认真走过的路，都会把我带向更好的自己。'
    if kind == 'continue':
        remaining = max(100, DEFAULT_TARGET_WORD_COUNT - count_essay_chars(content))
        continuation_paragraphs = [
            '接下来的日子里，我没有再把困难推到一边，而是试着把它们一点点拆开。遇到不会的题，我先找出卡住的地方；写不好的段落，我就反复读、反复改。这样的过程并不轻松，可每当我发现自己比昨天更清楚一点、更坚定一点，心里就多了一份继续走下去的力量。',
            '有一次，我又在同样的问题上停了很久。窗外的天色渐渐暗下来，草稿纸上留下了许多修改的痕迹。我本想合上本子，却想起自己已经答应过要认真完成。于是，我重新梳理思路，把最难的部分分成几个小步骤，再从第一步慢慢做起。',
            '真正的改变也从这些小事里出现。以前遇到不顺利，我总盼着别人直接告诉我答案；现在，我会先独立思考，再带着具体问题去请教。老师的提醒、同学的建议和自己的反复尝试，让我渐渐明白，进步并不来自一时冲动，而来自每天都愿意多做一点。',
            '当我再次面对相似的任务时，心里仍然会紧张，却不再只想着逃避。我先列出计划，再按顺序完成，还会在结束后检查遗漏。那些曾经让我手足无措的问题，终于变成了可以一步步解决的事情。',
            '回头看这段经历，我发现困难并没有突然消失，改变的是我面对它的方式。我开始愿意承担自己的选择，也愿意为结果付出耐心。这样的成长没有响亮的口号，却真实地藏在每一次坚持和修改之中。',
            '以后再遇到新的挑战，我也许仍会犯错，但我不会轻易否定自己。我会记住这次经历带来的勇气，认真走好眼前的每一步，让每一次尝试都成为继续向前的力量。',
            '我也终于明白，真正值得珍惜的并不是某一次顺利的结果，而是自己在犹豫时仍愿意迈出一步，在失败后仍愿意重新整理。正是这些看似普通的选择，让我慢慢成为更坚定的自己。',
        ]
        selected = []
        for paragraph in continuation_paragraphs:
            selected.append(paragraph)
            if count_essay_chars('\n\n'.join(selected)) >= remaining:
                break
        return '\n\n'.join(selected)
    if kind == 'polish':
        return (content or '').replace('很', '格外').replace('非常', '格外') or '请先写下需要润色的正文。'
    if kind == 'detail_expand':
        compact = re.sub(r'\s+', '', content or '')
        seed = compact[:90] or '我站在原地，心里有些紧张。'
        return (
            f'{seed}\n\n'
            '我没有急着把这段经历一笔带过，而是停下来仔细回想当时的情景。'
            '周围细小的声音一下子清楚起来，我的手指不自觉地收紧，心里反复衡量着下一步该怎么做。'
            '短暂的犹豫之后，我深吸一口气，先完成眼前最具体的一步，再根据结果调整。'
            '这个动作看起来并不起眼，却让原本模糊的想法有了落点，也让这件事与文章的中心联系得更紧。'
        )
    if kind == 'full':
        return build_local_full_essay(title, theme, genre, requirement)
    return content or ''


def local_optimize_structure(title, content, genre='', theme='', requirement=''):
    paragraphs = [p.strip() for p in re.split(r'\n+', content or '') if p.strip()]
    if not paragraphs:
        return ''
    text = content or ''

    def infer_theme():
        clean_title = re.sub(r'[《》“”"\'\s]+', '', title or '')
        if clean_title and len(clean_title) <= 12:
            return clean_title
        theme_keywords = ['责任', '成长', '坚持', '梦想', '亲情', '友谊', '诚信', '勇气', '担当', '感恩']
        for keyword in theme_keywords:
            if keyword in text or keyword in (title or ''):
                return keyword
        return clean_title[:12] if clean_title else '成长'

    theme = (theme or infer_theme()).strip('《》“” ') or infer_theme()
    normalized_genre = normalize_genre_name(genre, title, requirement, text)
    if normalized_genre == '议论文':
        structure_type = '议论文递进论证结构'
        nodes = [
            {'label': '引论', 'summary': f'提出关于{theme}的中心观点'},
            {'label': '本论一', 'summary': f'解释{theme}的内涵与意义'},
            {'label': '本论二', 'summary': '结合事实或事例展开论证'},
            {'label': '本论三', 'summary': '辨析相反观点并联系现实'},
            {'label': '结论', 'summary': f'总结观点，回扣{theme}主题'},
        ]
        comment = f'全文按照“提出观点—解释观点—举例论证—联系现实—总结提升”的逻辑展开，各层共同服务关于{theme}的中心论点。'
    elif normalized_genre == '说明文':
        structure_type = '说明文逻辑顺序结构'
        nodes = [
            {'label': '引入对象', 'summary': f'明确本文说明对象{theme}'},
            {'label': '概念说明', 'summary': f'解释{theme}的基本含义'},
            {'label': '特点分类', 'summary': '分类介绍主要特点与表现'},
            {'label': '原理原因', 'summary': '说明形成原因和内在联系'},
            {'label': '应用影响', 'summary': '结合生活说明作用与注意事项'},
            {'label': '总结', 'summary': f'归纳{theme}的核心认识'},
        ]
        comment = f'全文采用逻辑顺序说明{theme}，从概念进入，再展开特点、原因和应用，最后归纳重点，符合说明文由浅入深的组织方式。'
    elif normalized_genre == '应用文':
        subtype = '书信' if re.search(r'书信|感谢信|一封信', f'{title}{requirement}') else '通知' if '通知' in f'{title}{requirement}' else '倡议或演讲'
        structure_type = f'{subtype}规范结构'
        nodes = [
            {'label': '标题称谓', 'summary': '明确对象并使用规范称谓'},
            {'label': '背景目的', 'summary': f'交代围绕{theme}写作的缘由'},
            {'label': '主体事项', 'summary': '分层写清主要信息或观点'},
            {'label': '行动要求', 'summary': '提出具体可执行的做法'},
            {'label': '结语落款', 'summary': '礼貌收束并补全应用文格式'},
        ]
        comment = f'全文按照{subtype}的交际目的组织内容，先明确对象和背景，再写核心事项与行动要求，最后以规范结语完成收束。'
    else:
        if re.search(r'相比|不同|过去|现在|一边|另一边', text):
            structure_type = '对照式记叙结构'
        elif re.search(r'可是|但是|然而|后来|从此|终于|原来|明白|懂得', text):
            structure_type = '递进式记叙结构'
        else:
            structure_type = '记叙文发展式结构'

        if '责任' in theme or '责任' in text:
            intro = '点明责任由陌生到理解'
            deepen = '理解责任也可以是守护小事'
            action = '主动承担，学会照顾与帮助'
        elif '成长' in theme or '长大' in text or '成长' in text:
            intro = '借具体经历引出成长主题'
            deepen = '明白失败不是成长终点'
            action = '调整做法，开始主动改变'
        else:
            intro = f'借具体经历引出{theme}主题'
            deepen = '写出心理变化和认识加深'
            action = '主动行动，完成自我改变'

        if '雨' in text and '放学' in text:
            development = '雨天放学等待，引出事件背景'
        elif '考试' in text or '试卷' in text:
            development = '考试失利，引出内心低落'
        elif '妈妈' in text or '父母' in text:
            development = '家人陪伴，铺垫反思契机'
        else:
            development = '交代事件背景，铺垫矛盾'

        if '鸟窝' in text or '小鸟' in text:
            turn = '看见同学保护鸟窝'
        elif '妈妈' in text or '父母' in text:
            turn = '家人引导，重新整理思路'
        elif '老师' in text:
            turn = '老师提醒，引发态度转变'
        else:
            turn = '关键事件出现，推动转变'

        nodes = [
            {'label': '开头', 'summary': intro},
            {'label': '发展', 'summary': development},
            {'label': '转折', 'summary': turn},
            {'label': '深化', 'summary': deepen},
            {'label': '行动', 'summary': action},
            {'label': '结尾', 'summary': f'回扣{theme}主题，完成成长升华'},
        ]
        comment = f'本文采用{structure_type}，先引出{theme}主题，再通过事件发展、关键转折和心理变化层层推进，最后回扣主题完成升华。'

    return json.dumps({
        'structure_type': structure_type,
        'theme': theme,
        'genre': normalized_genre,
        'nodes': nodes,
        'comment': comment,
    }, ensure_ascii=False)


def normalize_structure_payload(raw, fallback):
    if isinstance(fallback, str):
        try:
            fallback_payload = json.loads(fallback)
        except (TypeError, ValueError, json.JSONDecodeError):
            fallback_payload = {}
    else:
        fallback_payload = fallback if isinstance(fallback, dict) else {}
    if isinstance(raw, dict):
        payload = raw
    else:
        payload = extract_json_payload(str(raw or ''), fallback_payload)
    if not isinstance(payload, dict):
        payload = fallback_payload

    fallback_nodes = fallback_payload.get('nodes') if isinstance(fallback_payload.get('nodes'), list) else []
    raw_nodes = payload.get('nodes') if isinstance(payload.get('nodes'), list) else fallback_nodes
    nodes = []
    for index, item in enumerate(raw_nodes[:7]):
        if not isinstance(item, dict):
            continue
        label = clean_inline_text(item.get('label') or item.get('name') or f'层次{index + 1}')[:8]
        summary = clean_inline_text(item.get('summary') or item.get('content') or item.get('text'))[:28]
        if label and summary:
            nodes.append({'label': label, 'summary': summary})
    if not nodes:
        nodes = fallback_nodes
    expected_genre = clean_inline_text(fallback_payload.get('genre') or '')
    candidate_genre = clean_inline_text(payload.get('genre') or expected_genre or '记叙文')
    label_text = ' '.join(item.get('label', '') for item in nodes)
    genre_label_patterns = {
        '记叙文': r'开头|发展|转折|深化|行动|结尾',
        '议论文': r'引论|本论|论点|论证|结论',
        '说明文': r'引入|概念|特点|分类|原理|原因|应用|总结',
        '应用文': r'标题|称谓|背景|目的|主体|事项|行动|要求|结语|落款',
    }
    expected_pattern = genre_label_patterns.get(expected_genre)
    genre_mismatch = bool(expected_genre and candidate_genre != expected_genre)
    label_matches = len(re.findall(expected_pattern, label_text)) if expected_pattern else len(nodes)
    if genre_mismatch or (expected_pattern and label_matches < 2):
        nodes = fallback_nodes
        payload = fallback_payload
        candidate_genre = expected_genre or candidate_genre
    return {
        'structure_type': clean_inline_text(payload.get('structure_type') or payload.get('type') or fallback_payload.get('structure_type') or '文章层级结构')[:18],
        'theme': clean_inline_text(payload.get('theme') or payload.get('center') or fallback_payload.get('theme') or '中心主题')[:14],
        'genre': candidate_genre[:10],
        'nodes': nodes,
        'comment': clean_inline_text(payload.get('comment') or payload.get('summary') or fallback_payload.get('comment') or '')[:120],
    }


def local_correction(title, content):
    length = len(re.sub(r'\s+', '', content or ''))
    paragraphs = len([p for p in re.split(r'\n+', content or '') if p.strip()])
    return f'''一、总体评价\n这篇《{title or '未命名作文'}》主题较明确，能够围绕题目展开表达。当前正文约 {length} 字，段落数量为 {paragraphs}。{get_word_count_guidance(length)}\n\n二、亮点分析\n1. 选材贴近学习生活，容易让读者理解。\n2. 文章有一定情感表达，能体现作者的思考。\n3. 结尾具备点题意识，可以继续强化升华。\n\n三、主要问题\n1. 局部句子还可以更具体，建议补充动作、心理或环境细节。\n2. 段落之间的过渡可以更自然，避免只是简单罗列。\n3. 如果能加入一两个更鲜明的细节，文章感染力会更强。\n\n四、修改建议\n建议按照“事件经过—心理变化—成长感悟”的顺序再调整一遍。重点修改表达较笼统的句子，把“很开心、很难过、非常努力”等词换成具体画面。'''


ANNOTATION_TYPE_META = {
    'highlight': ('亮点句子', '语言表达', 'low'),
    'suggestion': ('建议优化', '语言表达', 'medium'),
    'correction': ('字词纠错', '书写规范', 'medium'),
    'structure': ('结构提醒', '结构安排', 'medium'),
    'rewrite': ('升格改写', '内容细节', 'low'),
}


def normalize_annotation_type(raw_type=''):
    text = str(raw_type or '').strip().lower()
    if text in ANNOTATION_TYPE_META:
        return text
    if re.search(r'亮点|优点|好句|精彩|highlight|good', text, re.I):
        return 'highlight'
    if re.search(r'纠错|错别字|字词|标点|规范|correction|standard', text, re.I):
        return 'correction'
    if re.search(r'结构|过渡|开头|结尾|层次|structure', text, re.I):
        return 'structure'
    if re.search(r'升格|改写|rewrite|优化表达|示范', text, re.I):
        return 'rewrite'
    return 'suggestion'


def get_expected_annotation_range(content=''):
    length = count_essay_chars(content)
    if length < 100:
        return 2, 4
    if length < 400:
        return 4, 7
    if length < 750:
        return 8, 12
    if length <= 850:
        return 12, 16
    return 14, 20


def local_annotations(content):
    sentences = [s for s in split_sentences(content) if len(re.sub(r'\s+', '', s)) >= 8]
    if not sentences:
        compact = re.sub(r'\s+', '', content or '')
        quote = (compact[:48] + '…') if len(compact) > 48 else compact
        if not quote:
            return []
        return [{
            'id': 1,
            'type': 'suggestion',
            'type_name': '建议优化',
            'sentence_index': 1,
            'quote': quote,
            'original_sentence': quote,
            'comment_type': 'suggestion',
            'problem': '当前正文还比较短，信息量不足。',
            'comment': '已经有了基本表达，但还需要补充具体事件和细节。',
            'suggestion': '建议继续写清事情的起因、经过和感受，再补充动作或心理描写。',
            'rewrite': '',
            'dimension': '内容细节',
            'severity': 'medium',
        }]
    min_count, max_count = get_expected_annotation_range(content)

    def make_item(item_id, item_type, sentence_index, quote, comment, suggestion, problem='', rewrite='', dimension='', severity=''):
        normalized_type = normalize_annotation_type(item_type)
        type_name, default_dimension, default_severity = ANNOTATION_TYPE_META[normalized_type]
        return {
            'id': item_id,
            'type': normalized_type,
            'type_name': type_name,
            'sentence_index': sentence_index,
            'quote': quote,
            'original_sentence': quote,
            'comment_type': normalized_type,
            'problem': problem,
            'comment': comment,
            'suggestion': suggestion,
            'rewrite': rewrite,
            'dimension': dimension or default_dimension,
            'severity': severity or default_severity,
        }

    def pick_sentence(position):
        idx = max(0, min(len(sentences) - 1, position))
        return idx + 1, sentences[idx]

    selected = []
    first_index, first_sentence = pick_sentence(0)
    selected.append(make_item(
        1,
        'highlight',
        first_index,
        first_sentence,
        '开头能够较快进入主题，读者容易进入作文情境。',
        '可以继续补充场景或心理，让开头更有代入感。',
        '',
        '',
        '语言表达',
        'low'
    ))
    if len(sentences) > 1:
        second_index, second_sentence = pick_sentence(1)
        selected.append(make_item(
            len(selected) + 1,
            'suggestion',
            second_index,
            second_sentence,
            '这里已经交代了事件或情绪，但表达方式还可以更具体。',
            '建议补充人物动作、神态或心理变化，让情绪更有画面感。',
            '表达略显概括，缺少具体动作或心理细节。',
            '我低着头，脚步慢了下来，心里像压着一块小石头。',
            '语言表达',
            'medium'
        ))
    if len(sentences) > 2:
        mid_index = max(2, min(len(sentences), len(sentences) // 2 + 1))
        selected.append(make_item(
            len(selected) + 1,
            'structure',
            mid_index,
            sentences[mid_index - 1],
            '这一处位于文章中段，适合承担承上启下的作用。',
            '可加入一句过渡或原因说明，使前后层次更清楚。',
            '段落推进还可以更自然。',
            '',
            '结构安排',
            'medium'
        ))
    if len(sentences) > 3:
        selected.append(make_item(
            len(selected) + 1,
            'rewrite',
            len(sentences),
            sentences[-1],
            '结尾已经有收束意识，可以进一步升华主题。',
            '建议回扣题目或中心词，让文章结尾更有力量。',
            '结尾点题还可以更明确。',
            '这次经历让我明白，真正的成长不是突然变强，而是在一次次认真面对中学会承担。',
            '内容细节',
            'low'
        ))
    elif sentences[-1] != sentences[0]:
        selected.append(make_item(
            len(selected) + 1,
            'structure',
            len(sentences),
            sentences[-1],
            '结尾有收束作用，但还可以更明确地回扣主题。',
            '建议用一句简洁的话点明收获，让文章完成升华。',
            '结尾升华略显不足。',
            '',
            '结构安排',
            'medium'
        ))

    templates = [
        ('highlight', '这句话能体现文章的主题方向，是值得保留的表达。', '可以围绕这个中心继续补充具体细节，让主题更鲜明。', '', '', '内容主题', 'low'),
        ('suggestion', '这句话表达了事情发展，但画面感还可以再加强。', '建议补充一个动作、神态或环境细节，让读者更容易想象当时的情景。', '叙述偏概括，缺少可感知的细节。', '我停下脚步，手心微微出汗，心里反复想着刚才发生的一幕。', '细节描写', 'medium'),
        ('suggestion', '这里有情绪或想法，但还可以写得更具体。', '建议把“我”的心理变化拆成一两个具体感受，避免一句话带过。', '心理描写略显单薄。', '', '心理描写', 'medium'),
        ('structure', '这一句位于段落推进处，适合承担衔接作用。', '可加入表示时间、原因或转折的过渡词，让前后内容连接更自然。', '段落之间的过渡还可以更清楚。', '', '结构安排', 'medium'),
        ('rewrite', '这句话有升格空间，适合改得更有表现力。', '可以保留原意，适当加入比喻或动作描写，使表达更生动。', '语言还可以更有画面感。', '那一刻，我像突然看见了一束光，心里原本模糊的答案慢慢清楚起来。', '语言表达', 'low'),
        ('highlight', '这句话能帮助读者理解人物状态，具有一定表现力。', '建议保留，并在前后补充原因或结果，让表达更完整。', '', '', '语言表达', 'low'),
        ('suggestion', '这里交代了事件结果，但还可以补充“为什么”。', '建议说明这件事带来的影响或自己的收获，让内容更有层次。', '结果写到了，但认识变化还不够充分。', '', '内容深化', 'medium'),
        ('structure', '这句话接近文章收束位置，可以承担点题作用。', '建议回扣题目中的关键词，用一句简洁的话完成升华。', '结尾点题意识还可以更明确。', '这件事让我明白，成长不是突然变得完美，而是学会认真面对每一次选择。', '结构安排', 'medium'),
    ]

    cursor = 0
    while len(selected) < min_count and cursor < max(min_count * 3, 12):
        sentence_index, quote = pick_sentence(cursor % len(sentences))
        tpl = templates[cursor % len(templates)]
        selected.append(make_item(len(selected) + 1, tpl[0], sentence_index, quote, tpl[1], tpl[2], tpl[3], tpl[4], tpl[5], tpl[6]))
        cursor += 1

    result = []
    seen = set()
    for item in selected:
        excerpt = item.get('quote') or item.get('original_sentence') or ''
        signature = (excerpt, item.get('type'), item.get('problem') or item.get('comment'))
        if signature in seen:
            continue
        seen.add(signature)
        item['id'] = len(result) + 1
        result.append(item)
    return result[:max_count]


def ai_or_fallback_stream(messages, fallback_text, temperature=0.7, max_tokens=1500, clean_output=False):
    if has_deepseek_key():
        try:
            response = stream_deepseek(messages, temperature=temperature, max_tokens=max_tokens)
            if clean_output:
                def generate_clean_stream():
                    full = ''
                    for chunk in parse_stream_chunks(response):
                        full += chunk
                        yield sse_payload({'content': chunk})
                    cleaned_text = clean_writing_output(full) or clean_writing_output(fallback_text)
                    yield sse_payload({
                        'done': True,
                        'full_result': cleaned_text,
                        'source': 'deepseek',
                        'message': '建议已生成',
                    })
                return sse_response(generate_clean_stream())

            def generate():
                full = ''
                for chunk in parse_stream_chunks(response):
                    full += chunk
                    yield sse_payload({'content': chunk})
                yield sse_payload({
                    'done': True,
                    'full_result': full,
                    'source': 'deepseek',
                    'message': '建议已生成',
                })
            return sse_response(generate())
        except Exception:
            current_app.logger.exception('DeepSeek 流式请求建立失败，改用本地兜底')
            if not ENABLE_LOCAL_FALLBACK:
                return sse_error('当前 AI 服务暂时不可用，请稍后重试。')
    return stream_text(
        clean_writing_output(fallback_text) if clean_output else fallback_text,
        source='local_fallback',
        message='当前写作服务暂时不可用，已为你整理基础建议',
    )


def complete_full_text_result(raw_text, title='', grade='', theme='', genre='', requirement=''):
    """Complete a non-streaming DeepSeek draft, then enforce the 800-900 range."""
    cleaned = clean_writing_output(raw_text)
    if cleaned and count_essay_chars(cleaned) < FULL_TEXT_MIN_WORD_COUNT and has_deepseek_key():
        try:
            extension = call_deepseek(
                build_messages(build_full_text_extension_prompt(title, cleaned, grade, theme, requirement)),
                temperature=0.65,
                max_tokens=900,
            )
            extension = clean_writing_output(extension)
            if extension:
                cleaned = f'{cleaned}\n\n{extension}'
        except Exception:
            current_app.logger.exception('DeepSeek 全文补写失败，使用本地规则补足篇幅')
    return normalize_full_text_length(cleaned, title, theme, genre, requirement)


def stream_full_text_result(messages, fallback_text, title='', grade='', theme='', genre='', requirement='', temperature=0.75):
    """Stream a full essay and send a validated 800-900 character final result."""
    fallback_text = normalize_full_text_length(fallback_text, title, theme, genre, requirement)
    if not has_deepseek_key():
        return stream_text(
            fallback_text,
            source='local_fallback',
            message='当前写作服务暂时不可用，已为你生成完整初稿',
        )

    try:
        response = stream_deepseek(messages, temperature=temperature, max_tokens=2300)
    except Exception:
        current_app.logger.exception('DeepSeek 全文流式请求建立失败，改用本地完整初稿')
        if not ENABLE_LOCAL_FALLBACK:
            return sse_error('当前 AI 服务暂时不可用，请稍后重试。')
        return stream_text(
            fallback_text,
            source='local_fallback',
            message='当前写作服务暂时不可用，已为你生成完整初稿',
        )

    def generate():
        full = ''
        source = 'deepseek'
        message = '作文初稿已生成'
        yield sse_payload({'type': 'start', 'source': source, 'message': '正在围绕主题生成完整作文...'})
        try:
            for chunk in parse_stream_chunks(response):
                full += chunk
                yield sse_payload({'content': chunk})
        except Exception:
            current_app.logger.exception('DeepSeek 全文流读取失败，使用本地规则完成初稿')

        cleaned = clean_writing_output(full)
        if cleaned and count_essay_chars(cleaned) < FULL_TEXT_MIN_WORD_COUNT:
            try:
                yield sse_payload({
                    'type': 'status',
                    'message': f'正在补充细节，使全文达到 {FULL_TEXT_MIN_WORD_COUNT} 字以上...',
                })
                extension_response = stream_deepseek(
                    build_messages(build_full_text_extension_prompt(title, cleaned, grade, theme, requirement)),
                    temperature=0.65,
                    max_tokens=900,
                )
                extension = ''
                for chunk in parse_stream_chunks(extension_response):
                    extension += chunk
                    yield sse_payload({'content': chunk})
                extension = clean_writing_output(extension)
                if extension:
                    cleaned = f'{cleaned}\n\n{extension}'
            except Exception:
                current_app.logger.exception('DeepSeek 全文流式补写失败，使用本地规则补足篇幅')
                source = 'deepseek_with_local_completion'
                message = '作文初稿已生成，并完成篇幅校验'

        if not cleaned:
            cleaned = fallback_text
            source = 'local_fallback'
            message = '当前写作服务暂时不可用，已为你生成完整初稿'

        before_normalize = cleaned
        context_matches = full_text_matches_context(cleaned, title, theme, genre, requirement)
        final_text = normalize_full_text_length(cleaned, title, theme, genre, requirement)
        if not context_matches:
            source = 'local_fallback'
            message = '模型初稿与题目要求不一致，已由后端生成匹配初稿'
        elif final_text.startswith(before_normalize) and len(final_text) > len(before_normalize):
            yield sse_payload({'content': final_text[len(before_normalize):]})
            source = 'deepseek_with_local_completion'
            message = '作文初稿已生成，并完成篇幅校验'
        yield sse_payload({
            'done': True,
            'full_result': final_text,
            'word_count': count_essay_chars(final_text),
            'source': source,
            'message': message,
        })

    return sse_response(generate())


def stream_structure_result(messages, fallback_text, temperature=0.45):
    fallback_payload = normalize_structure_payload('', fallback_text)
    fallback_json = json.dumps(fallback_payload, ensure_ascii=False)
    if not has_deepseek_key():
        return stream_text(
            fallback_json,
            source='local_fallback',
            message='当前写作服务暂时不可用，已按本文文体整理结构图',
        )
    try:
        response = stream_deepseek(messages, temperature=temperature, max_tokens=1600)
    except Exception:
        current_app.logger.exception('DeepSeek 结构梳理请求建立失败，改用后端本地结构图')
        if not ENABLE_LOCAL_FALLBACK:
            return sse_error('当前 AI 服务暂时不可用，请稍后重试。')
        return stream_text(
            fallback_json,
            source='local_fallback',
            message='当前写作服务暂时不可用，已按本文文体整理结构图',
        )

    def generate():
        full = ''
        yield sse_payload({'type': 'start', 'source': 'deepseek', 'message': '正在识别文体并提炼结构层级...'})
        try:
            for chunk in parse_stream_chunks(response):
                full += chunk
                yield sse_payload({'content': chunk})
            payload = normalize_structure_payload(full, fallback_payload)
            final_text = json.dumps(payload, ensure_ascii=False)
            yield sse_payload({
                'done': True,
                'full_result': final_text,
                'result': payload,
                'source': 'deepseek',
                'message': '结构图已生成',
            })
        except Exception:
            current_app.logger.exception('DeepSeek 结构梳理流读取失败，改用后端本地结构图')
            yield sse_payload({
                'done': True,
                'full_result': fallback_json,
                'result': fallback_payload,
                'source': 'local_fallback',
                'message': '当前写作服务暂时不可用，已按本文文体整理结构图',
            })

    return sse_response(generate())


def build_writing_user_prompt(kind, title, content, grade='', theme='', requirement=''):
    if kind == 'opening':
        return build_opening_prompt(title, content, grade, theme, requirement)
    if kind == 'ending':
        return build_ending_prompt(title, content, grade, theme, requirement)
    if kind == 'continue':
        return build_continue_prompt(title, content, grade, theme, requirement)
    if kind == 'polish':
        return build_polish_prompt(title, content, grade, theme, requirement)
    if kind == 'detail_expand':
        return build_detail_expand_prompt(title, content, grade, theme, requirement)
    if kind == 'full':
        return build_full_text_prompt(title, content, grade, theme, requirement)
    if kind == 'outline':
        return build_outline_prompt(title, content, grade, theme, requirement)
    if kind == 'structure':
        return build_structure_prompt(title, content, grade, theme, requirement)
    return build_full_text_prompt(title, content, grade, theme, requirement)


DIMENSION_LABELS = ['内容', '结构', '语言', '修辞', '规范']
CORRECTION_DIMENSIONS = ['内容', '结构', '语言', '修辞', '规范']
CORRECTION_SCORE_KEYS = {
    '内容': 'content',
    '结构': 'structure',
    '语言': 'language',
    '修辞': 'rhetoric',
    '规范': 'standard',
}
CORRECTION_DIMENSION_ALIASES = {
    '内容': ['内容', 'content'],
    '结构': ['结构', 'structure'],
    '语言': ['语言', 'language'],
    '修辞': ['修辞', 'rhetoric'],
    '规范': ['规范', 'standard', 'norm'],
}


def calculate_final_score(scores):
    content = float(scores.get('content', scores.get('内容', 0)) or 0)
    structure = float(scores.get('structure', scores.get('结构', 0)) or 0)
    language = float(scores.get('language', scores.get('语言', 0)) or 0)
    rhetoric = float(scores.get('rhetoric', scores.get('修辞', 0)) or 0)
    standard = float(scores.get('standard', scores.get('规范', 0)) or 0)
    final_score = (
        content * 0.30 +
        structure * 0.20 +
        language * 0.20 +
        rhetoric * 0.15 +
        standard * 0.15
    )
    return max(0, min(100, round(final_score)))


def cap_final_score(score, content, radar_scores):
    compact = re.sub(r'\s+', '', content or '')
    chinese_length = len(re.findall(r'[\u4e00-\u9fff]', compact))
    length = chinese_length or len(compact)
    if not length:
        return 0
    capped = score
    if length < 80:
        capped = min(capped, 55)
    elif length < 150:
        capped = min(capped, 65)
    elif length < 400:
        capped = min(capped, 78)
    title_like_words = len(set(re.findall(r'[\u4e00-\u9fa5]{2,}', compact[:80])))
    if title_like_words == 0 and length > 0:
        capped = min(capped, 70)
    if radar_scores.get('规范', 100) < 60 or radar_scores.get('语言', 100) < 60:
        capped = min(capped, 78)
    return max(0, min(100, round(capped)))


def normalize_tool_name(tool):
    tool_map = {
        'full': 'full_text',
        'full_text': 'full_text',
        'correct': 'correction',
        'correction': 'correction',
        'annotation': 'inline_comment',
        'inline_comment': 'inline_comment',
        'continue': 'continue',
        'structure': 'structure',
        'polish': 'polish',
        'outline': 'outline',
        'opening': 'opening',
        'ending': 'ending',
        'detail': 'detail_expand',
        'detail_expand': 'detail_expand',
    }
    return tool_map.get((tool or '').strip(), (tool or '').strip())


def get_request_writing_context(data, title=''):
    grade = (data.get('grade') or '').strip()
    theme = (data.get('theme') or '').strip() or (title or '').strip()
    genre = (data.get('genre') or '').strip()
    requirement = (data.get('requirement') or '').strip()
    requirement_parts = []
    if genre:
        requirement_parts.append(f'指定文体：{genre}')
    if requirement:
        requirement_parts.append(requirement)
    return grade, theme, genre, '；'.join(requirement_parts)


SUPPORTED_AI_TOOLS = {
    'full_text', 'correction', 'inline_comment', 'continue', 'structure',
    'polish', 'outline', 'opening', 'ending', 'detail_expand'
}


def clamp_score(value, default=76):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(0, min(100, number))


def pick_dimension_score(source, label, default=76):
    if not isinstance(source, dict):
        return clamp_score(None, default)
    for alias in CORRECTION_DIMENSION_ALIASES.get(label, [label]):
        if alias in source:
            return clamp_score(source.get(alias), default)
    return clamp_score(None, default)


def extract_json_payload(text, fallback):
    raw = (text or '').strip()
    if not raw:
        return fallback
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.I).strip()
    raw = re.sub(r'\s*```$', '', raw).strip()
    candidates = [raw]
    obj_match = re.search(r'\{[\s\S]*\}', raw)
    if obj_match:
        candidates.append(obj_match.group(0))
    arr_match = re.search(r'\[[\s\S]*\]', raw)
    if arr_match:
        candidates.append(arr_match.group(0))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return fallback


def analyze_essay_features(title='', content=''):
    raw = content or ''
    compact = re.sub(r'\s+', '', raw)
    paragraphs = [p.strip() for p in re.split(r'\n+', raw) if p.strip()]
    sentences = split_sentences(raw)
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', compact)
    punctuation = re.findall(r'[。！？；，、,.!?;:：]', raw)
    title_terms = [term for term in re.findall(r'[\u4e00-\u9fff]{2,}', title or '') if term]
    topic_match = bool(title_terms and any(term in raw for term in title_terms)) or not title_terms
    narrative_hits = len(re.findall(r'那一次|后来|从那以后|老师|妈妈|爸爸|同学|经历|事情|发现|明白|放学|考试|回家|小时候', raw))
    argument_hits = len(re.findall(r'例如|比如|事例|材料|首先|其次|再次|因此|所以|一方面|另一方面|观点|证明', raw))
    detail_hits = len(re.findall(r'看见|听见|想到|心里|眼泪|台灯|雨|风|声音|脚步|低头|抬头|笑|哭|手|书包|试卷|鸟窝|花草', raw))
    transition_hits = len(re.findall(r'后来|从那以后|可是|但是|然而|于是|终于|原来|因此|所以|最后|总之', raw))
    rhetoric_hits = len(re.findall(r'像|仿佛|好像|如同|似的|不仅.*?也|一边.*?一边|既.*?又|排比|比喻', raw))
    colloquial_hits = len(re.findall(r'然后|就是|非常非常|我觉得|这个|那个|挺|蛮|啥|超级', raw))
    slogan_hits = len(re.findall(r'我要努力|我要坚持|很重要|非常重要|我很开心|我很难过|好好学习|天天向上', raw))
    repeat_patterns = re.findall(r'([\u4e00-\u9fff]{2,6})\1{2,}', compact)
    repeated_punctuation = len(re.findall(r'[!！?？。]{3,}', raw))
    abnormal_chars = len(re.findall(r'[^\u4e00-\u9fffA-Za-z0-9\s，。！？；：、,.!?;:《》“”‘’（）()—…-]', raw))
    avg_sentence_len = round(len(compact) / max(1, len(sentences)), 1)
    long_sentence_count = len([s for s in sentences if len(re.sub(r'\s+', '', s)) > 60])
    has_intro = bool(sentences and len(re.sub(r'\s+', '', sentences[0])) >= 8)
    has_conclusion = bool(sentences and re.search(r'明白|懂得|收获|成长|责任|坚持|以后|从此|意义|启示', sentences[-1]))
    return {
        'length': len(chinese_chars),
        'paragraph_count': len(paragraphs),
        'sentence_count': len(sentences),
        'punctuation_count': len(punctuation),
        'topic_match': topic_match,
        'narrative_hits': narrative_hits,
        'argument_hits': argument_hits,
        'detail_hits': detail_hits,
        'transition_hits': transition_hits,
        'rhetoric_hits': rhetoric_hits,
        'colloquial_hits': colloquial_hits,
        'slogan_hits': slogan_hits,
        'repeat_count': len(repeat_patterns),
        'repeated_punctuation': repeated_punctuation,
        'abnormal_chars': abnormal_chars,
        'avg_sentence_len': avg_sentence_len,
        'long_sentence_count': long_sentence_count,
        'has_intro': has_intro,
        'has_conclusion': has_conclusion,
    }


def estimate_radar_scores(content, title=''):
    features = analyze_essay_features(title, content)
    length = features['length']
    if length == 0:
        return {'内容': 0, '结构': 0, '语言': 0, '修辞': 0, '规范': 0}

    length_delta = 0
    if length < 80:
        length_delta = -28
    elif length < 150:
        length_delta = -18
    elif length < 400:
        length_delta = -10
    elif length < 750:
        length_delta = 0
    elif length <= 850:
        length_delta = 5
    else:
        length_delta = 5

    content_support_bonus = min(10, (features['narrative_hits'] + features['argument_hits']) * 2)
    detail_bonus = min(8, features['detail_hits'])
    repetition_penalty = min(16, features['repeat_count'] * 6 + features['slogan_hits'] * 3)
    topic_penalty = 0 if features['topic_match'] else 10

    content_score = 68 + length_delta + content_support_bonus + detail_bonus - repetition_penalty - topic_penalty

    paragraph_count = features['paragraph_count']
    if paragraph_count <= 1:
        paragraph_delta = -14 if length >= 180 else -8
    elif paragraph_count == 2:
        paragraph_delta = -5
    elif paragraph_count >= 4:
        paragraph_delta = 7
    else:
        paragraph_delta = 5
    structure_score = (
        66 + paragraph_delta +
        min(6, features['transition_hits'] * 2) +
        (3 if features['has_intro'] else -4) +
        (4 if features['has_conclusion'] else -5)
    )

    avg_len = features['avg_sentence_len']
    sentence_penalty = 0
    if avg_len > 65:
        sentence_penalty += 14
    elif avg_len > 45:
        sentence_penalty += 8
    if features['long_sentence_count']:
        sentence_penalty += min(8, features['long_sentence_count'] * 3)
    language_score = (
        74 + min(7, features['detail_hits'] // 2) -
        sentence_penalty -
        min(8, features['colloquial_hits']) -
        min(10, features['repeat_count'] * 4)
    )

    rhetoric_score = 68 + min(14, features['rhetoric_hits'] * 4) + min(6, features['detail_hits'] // 2)
    if features['rhetoric_hits'] == 0:
        rhetoric_score -= 4
    if features['rhetoric_hits'] > 8:
        rhetoric_score -= 3
    if length < 150:
        rhetoric_score -= 8

    standard_score = 76
    if features['punctuation_count'] == 0:
        standard_score -= 22
    elif features['punctuation_count'] < max(1, features['sentence_count'] // 2):
        standard_score -= 8
    if paragraph_count >= 3:
        standard_score += 5
    elif paragraph_count == 1 and length > 180:
        standard_score -= 8
    standard_score -= min(12, features['abnormal_chars'] * 2)
    standard_score -= min(10, features['repeated_punctuation'] * 4)
    standard_score -= min(6, features['repeat_count'] * 2)

    if length >= 650 and paragraph_count >= 3 and features['detail_hits'] >= 6 and features['has_conclusion']:
        content_score += 2
        language_score += 2
        rhetoric_score += 2
        standard_score += 2
    if length >= DEFAULT_TARGET_WORD_COUNT and paragraph_count >= 4 and features['detail_hits'] >= 8:
        content_score += 2
        structure_score += 2

    return {
        '内容': clamp_score(content_score),
        '结构': clamp_score(structure_score),
        '语言': clamp_score(language_score),
        '修辞': clamp_score(rhetoric_score),
        '规范': clamp_score(standard_score),
    }


def local_correction_payload(title, content):
    estimated_scores = estimate_radar_scores(content, title)
    radar_scores = {label: estimated_scores.get(label, 76) for label in CORRECTION_DIMENSIONS}
    score_key_scores = {CORRECTION_SCORE_KEYS[label]: radar_scores[label] for label in CORRECTION_DIMENSIONS}
    score = cap_final_score(calculate_final_score(score_key_scores), content, radar_scores)
    sentences = split_sentences(content)
    return {
        'type': 'correction',
        'score': score,
        'final_score': score,
        'scores': {CORRECTION_SCORE_KEYS[label]: radar_scores[label] for label in CORRECTION_DIMENSIONS},
        'radar_scores': radar_scores,
        'scoring_source': '本地兜底评分',
        'summary': f'《{title or "未命名作文"}》主题基本明确，能够围绕中心展开表达。{get_word_count_guidance(count_essay_chars(content))}后续可以继续加强结构衔接、内容细节和语言表现力。',
        'highlights': ['主题比较清楚', '能围绕生活经历展开', '结尾具备一定点题意识'],
        'issues': ['段落之间的过渡还可以更自然', '内容细节略显单薄', '部分句子可以更具体生动'],
        'suggestions': ['增加具体事例，让内容更有支撑', '在段落开头补充承接句', '适当使用比喻、排比等修辞提升表现力'],
        'issue_cards': [
            {
                'type': '结构',
                'original_sentence': sentences[0] if sentences else '',
                'suggestion': '补充承接句或过渡句，让段落之间更自然。',
                'reason': '结构清晰会让文章层次更稳定，读者更容易理解成长过程。'
            },
            {
                'type': '语言',
                'original_sentence': sentences[1] if len(sentences) > 1 else (sentences[0] if sentences else ''),
                'suggestion': '把笼统感受改成动作、心理或场景描写。',
                'reason': '具体描写比抽象评价更有画面感，也更能表现真实情感。'
            },
            {
                'type': '内容',
                'original_sentence': sentences[2] if len(sentences) > 2 else (sentences[-1] if sentences else ''),
                'suggestion': '补充一个更具体的生活细节，例如人物动作、对话或当时看到的场景。',
                'reason': '具体细节能支撑中心立意，让文章内容更充实，不会停留在简单叙述。'
            }
        ],
    }


def correction_payload_to_text(payload):
    radar_scores = payload.get('radar_scores') or {}
    lines = [
        f"总评分：{clamp_score(payload.get('score'))}/100",
        '',
        '维度评分：',
    ]
    for label in CORRECTION_DIMENSIONS:
        lines.append(f"{label}：{clamp_score(radar_scores.get(label))}")
    lines.extend([
        '',
        '总评：',
        str(payload.get('summary') or '作文整体较完整，可以继续围绕薄弱维度进行修改。'),
        '',
        '修改建议：',
    ])
    suggestions = payload.get('suggestions') or []
    for index, item in enumerate(suggestions[:5], 1):
        lines.append(f'{index}. {item}')
    return '\n'.join(lines)


def correction_payload_to_stream_events(payload):
    radar_scores = payload.get('radar_scores') or {}
    for label in CORRECTION_DIMENSIONS:
        yield {
            'type': 'score',
            'key': CORRECTION_SCORE_KEYS[label],
            'label': label,
            'value': clamp_score(radar_scores.get(label)),
        }
    yield {
        'type': 'summary',
        'content': payload.get('summary') or '作文整体较完整，可以继续围绕薄弱维度进行修改。',
    }
    issue_cards = payload.get('issue_cards') or []
    if issue_cards:
        for item in issue_cards[:6]:
            yield {
                'type': 'suggestion',
                'data': {
                    'type': item.get('type') or '建议',
                    'original': item.get('original_sentence') or item.get('original') or '',
                    'advice': item.get('suggestion') or item.get('advice') or '',
                    'reason': item.get('reason') or '',
                }
            }
    else:
        for index, item in enumerate((payload.get('suggestions') or [])[:6]):
            yield {
                'type': 'suggestion',
                'data': {
                    'type': (payload.get('issues') or [])[index] if index < len(payload.get('issues') or []) else '建议',
                    'original': '',
                    'advice': item,
                    'reason': '这项修改有助于提升文章表达质量。',
                }
            }
    if payload.get('optimized_text'):
        yield {'type': 'optimized_text', 'content': payload.get('optimized_text')}


def build_correction_stream_messages(title, content, grade='', theme='', requirement=''):
    return build_messages(
        build_correction_stream_prompt(title, content, grade, theme, requirement),
        NDJSON_RESULT_SYSTEM_PROMPT
    )


def save_correction_history(logger, username, title, content, payload, source):
    saved_payload = dict(payload or {})
    saved_payload['source'] = source
    try:
        return save_history(
            username,
            title,
            content,
            json.dumps(saved_payload, ensure_ascii=False),
            'essay_corrected'
        )
    except Exception:
        logger.exception('保存作文批改历史失败：username=%s title=%s', username, title)
        return None


def stream_correction_fallback(payload, username, title, content, logger, message='当前写作服务暂时不可用，已为你整理基础诊断建议'):
    def generate():
        yield sse_payload({'type': 'start', 'message': '正在细读你的作文...'})
        time.sleep(0.08)
        for event in correction_payload_to_stream_events(payload):
            yield sse_payload(event)
            time.sleep(0.08)
        history_id = save_correction_history(logger, username, title, content, payload, 'local_fallback')
        yield sse_payload({
            'type': 'final',
            'message': '作文诊断完成',
            'result': payload,
            'text': correction_payload_to_text(payload),
            'source': 'local_fallback',
            'notice': message,
            'history_id': history_id,
            'done': True,
        })
    return sse_response(generate())


def normalize_correction_stream_event(event):
    if not isinstance(event, dict):
        return None
    event_type = event.get('type')
    if event_type == 'score':
        key = event.get('key')
        label = event.get('label')
        key_alias = {
            '内容': 'content',
            '结构': 'structure',
            '语言': 'language',
            '修辞': 'rhetoric',
            '规范': 'standard',
        }
        key = key_alias.get(key, key)
        if key not in {'content', 'structure', 'language', 'rhetoric', 'standard'}:
            key = key_alias.get(label)
        if not key:
            return None
        label_map = {
            'content': '内容',
            'structure': '结构',
            'language': '语言',
            'rhetoric': '修辞',
            'standard': '规范',
        }
        return {
            'type': 'score',
            'key': key,
            'label': label or label_map[key],
            'value': clamp_score(event.get('value') or event.get('score')),
        }
    if event_type == 'summary':
        content = event.get('content') or event.get('summary') or ''
        return {'type': 'summary', 'content': str(content).strip()} if content else None
    if event_type == 'suggestion':
        data = event.get('data') if isinstance(event.get('data'), dict) else event
        return {
            'type': 'suggestion',
            'data': {
                'type': data.get('type') or data.get('issue_type') or '建议',
                'original': data.get('original') or data.get('original_sentence') or data.get('sentence') or '',
                'advice': data.get('advice') or data.get('suggestion') or '',
                'reason': data.get('reason') or '',
            }
        }
    if event_type == 'optimized_text':
        content = event.get('content') or event.get('optimized_text') or ''
        return {'type': 'optimized_text', 'content': str(content).strip()} if content else None
    if event_type == 'final':
        return {'type': 'final', 'message': event.get('message') or '作文诊断完成'}
    return None


def is_suspicious_ai_scores(radar_scores, title='', content=''):
    scores = [clamp_score(radar_scores.get(label), 0) for label in CORRECTION_DIMENSIONS]
    compact = re.sub(r'\s+', '', content or '')
    length = len(re.findall(r'[\u4e00-\u9fff]', compact))
    features = analyze_essay_features(title, content)
    if not any(scores):
        return True
    if scores == [88, 85, 82, 80, 90]:
        return True
    if len(set(scores)) == 1:
        return True
    if all(80 <= score <= 85 for score in scores) and (max(scores) - min(scores) <= 2):
        return True
    if length < 80 and max(scores) >= 70:
        return True
    if length < 150 and max(scores) >= 82:
        return True
    if length < 400 and sum(scores) / len(scores) >= 86:
        return True
    if features.get('punctuation_count', 0) <= 1 and clamp_score(radar_scores.get('规范'), 0) >= 86:
        return True
    if (
        features.get('detail_hits', 0) < 2
        and (features.get('narrative_hits', 0) + features.get('argument_hits', 0)) < 2
        and clamp_score(radar_scores.get('内容'), 0) >= 88
    ):
        return True
    return False


def blend_ai_scores_with_local(ai_scores, local_scores, ai_weight=0.4):
    blended = {}
    for label in CORRECTION_DIMENSIONS:
        ai_value = clamp_score(ai_scores.get(label), local_scores.get(label, 0))
        local_value = clamp_score(local_scores.get(label), ai_value)
        blended[label] = clamp_score(round(ai_value * ai_weight + local_value * (1 - ai_weight)))
    return blended


def normalize_correction_payload(payload, title, content):
    if not isinstance(payload, dict):
        payload = {}
    fallback = local_correction_payload(title, content)
    payload_text = json.dumps(payload, ensure_ascii=False) if payload else ''
    if content.strip() and any(token in payload_text for token in ['作文正文为空', '未提供作文正文', '没有提供作文正文', '占位符', '无法批改', '请提供实际作文内容', '????????']):
        payload = {}
    radar = payload.get('radar_scores') if isinstance(payload.get('radar_scores'), dict) else {}
    if not radar and isinstance(payload.get('scores'), dict):
        radar = payload.get('scores') or {}
    normalized_radar = {}
    for label in CORRECTION_DIMENSIONS:
        normalized_radar[label] = pick_dimension_score(radar, label, fallback['radar_scores'][label])
    if content.strip() and all(score == 0 for score in normalized_radar.values()):
        normalized_radar = dict(fallback['radar_scores'])
    suspicious_scores = is_suspicious_ai_scores(normalized_radar, title, content)
    if content.strip():
        normalized_radar = blend_ai_scores_with_local(
            normalized_radar,
            fallback['radar_scores'],
            ai_weight=0.4 if suspicious_scores else 0.7
        )
    scoring_source = '本地规则校准' if suspicious_scores else 'DeepSeek 智能评分 · 本地规则校准'
    weighted_scores = {CORRECTION_SCORE_KEYS[label]: normalized_radar[label] for label in CORRECTION_DIMENSIONS}
    final_score = cap_final_score(calculate_final_score(weighted_scores), content, normalized_radar)
    raw_issue_cards = payload.get('issue_cards') if isinstance(payload.get('issue_cards'), list) else []
    if not raw_issue_cards and isinstance(payload.get('suggestions'), list):
        raw_issue_cards = payload.get('suggestions') or []
    issue_cards = []
    for item in raw_issue_cards[:6]:
        if not isinstance(item, dict):
            continue
        issue_cards.append({
            'type': clean_inline_text(item.get('type') or item.get('issue_type') or '建议'),
            'original_sentence': clean_inline_text(
                item.get('original')
                or item.get('originalText')
                or item.get('original_sentence')
                or item.get('sentence')
                or item.get('sourceText')
                or item.get('excerpt')
                or ''
            ),
            'suggestion': clean_inline_text(
                item.get('suggestion')
                or item.get('improved')
                or item.get('revised')
                or item.get('advice')
                or item.get('revision')
                or ''
            ),
            'reason': clean_inline_text(
                item.get('reason')
                or item.get('explanation')
                or item.get('why')
                or item.get('diagnosis')
                or ''
            ),
        })
    if not issue_cards:
        issue_cards = fallback.get('issue_cards', [])
    elif len(issue_cards) < 3:
        seen_originals = {item.get('original_sentence', '') for item in issue_cards}
        for fallback_item in fallback.get('issue_cards', []):
            if len(issue_cards) >= 3:
                break
            if fallback_item.get('original_sentence', '') in seen_originals and fallback_item.get('original_sentence'):
                continue
            issue_cards.append(fallback_item)

    summary = payload.get('summary') or fallback['summary']
    if content.strip() and (
        final_score == 0
        or '未提供' in str(summary)
        or '没有提供' in str(summary)
    ):
        summary = fallback['summary']
    highlights = payload.get('highlights') if isinstance(payload.get('highlights'), list) and payload.get('highlights') else fallback['highlights']
    issues = payload.get('issues') if isinstance(payload.get('issues'), list) and payload.get('issues') else fallback['issues']
    if isinstance(payload.get('suggestions'), list) and payload.get('suggestions'):
        suggestions = [
            item.get('advice') or item.get('suggestion') or item.get('reason') or str(item)
            if isinstance(item, dict) else str(item)
            for item in payload.get('suggestions')
        ]
    else:
        suggestions = fallback['suggestions']
    normalized = {
        'type': 'correction',
        'score': final_score,
        'final_score': final_score,
        'scores': {CORRECTION_SCORE_KEYS[label]: normalized_radar[label] for label in CORRECTION_DIMENSIONS},
        'radar_scores': normalized_radar,
        'scoring_source': scoring_source,
        'summary': summary,
        'highlights': highlights,
        'issues': issues,
        'suggestions': suggestions,
        'issue_cards': issue_cards,
        'optimized_text': payload.get('optimized_text') or payload.get('revised_text') or '',
    }
    return normalized


def build_ai_tool_messages(tool, title, content, grade='', theme='', requirement=''):
    if tool == 'outline':
        return build_messages(build_outline_prompt(title, content, grade, theme, requirement))
    if tool in {'opening', 'ending', 'continue', 'polish', 'full_text', 'detail_expand'}:
        kind = 'full' if tool == 'full_text' else tool
        return build_messages(build_writing_user_prompt(kind, title, content, grade, theme, requirement))
    if tool == 'structure':
        return build_messages(
            build_structure_prompt(title, content, grade, theme, requirement),
            JSON_RESULT_SYSTEM_PROMPT
        )
    if tool == 'correction':
        return build_messages(
            build_correction_prompt(title, content, grade, theme, requirement),
            JSON_RESULT_SYSTEM_PROMPT
        )
    if tool == 'inline_comment':
        return build_messages(
            build_inline_comment_prompt(title, content, grade, theme, requirement),
            JSON_RESULT_SYSTEM_PROMPT
        )
    return build_messages(build_full_text_prompt(title, content, grade, theme, requirement))


def get_essay_word_count(content=''):
    return count_essay_chars(content)


def normalize_annotation_item(item, index=0):
    if not isinstance(item, dict):
        item = {}
    raw_type = item.get('type') or item.get('comment_type') or item.get('issue_type') or 'suggestion'
    annotation_type = normalize_annotation_type(raw_type)
    type_name, default_dimension, default_severity = ANNOTATION_TYPE_META[annotation_type]
    quote = (
        item.get('quote')
        or item.get('original_sentence')
        or item.get('sentence')
        or item.get('excerpt')
        or item.get('original')
        or ''
    )
    sentence_index = item.get('sentence_index') if item.get('sentence_index') is not None else item.get('index')
    try:
        sentence_index = int(sentence_index)
    except (TypeError, ValueError):
        sentence_index = index + 1
    if sentence_index <= 0:
        sentence_index = index + 1
    try:
        annotation_id = int(item.get('id') or index + 1)
    except (TypeError, ValueError):
        annotation_id = index + 1
    return {
        'id': annotation_id,
        'type': annotation_type,
        'type_name': item.get('type_name') or type_name,
        'sentence_index': sentence_index,
        'quote': quote,
        'original_sentence': quote,
        'comment_type': annotation_type,
        'problem': item.get('problem') or item.get('issue') or item.get('diagnosis') or '',
        'comment': item.get('comment') or item.get('feedback') or item.get('reason') or '',
        'suggestion': item.get('suggestion') or item.get('advice') or item.get('result') or '',
        'rewrite': item.get('rewrite') or item.get('revised') or item.get('improved') or '',
        'dimension': item.get('dimension') or default_dimension,
        'severity': item.get('severity') or default_severity,
    }


def build_annotation_sentence_map(content=''):
    paragraphs = [part.strip() for part in re.split(r'\n+', content or '') if part.strip()]
    if not paragraphs and (content or '').strip():
        paragraphs = [(content or '').strip()]
    entries = []
    sentence_cursor = 0
    paragraph_count = len(paragraphs)
    for paragraph_index, paragraph in enumerate(paragraphs):
        paragraph_sentences = split_sentences(paragraph) or [paragraph]
        for sentence_in_paragraph, sentence in enumerate(paragraph_sentences):
            sentence = sentence.strip()
            if not sentence:
                continue
            sentence_cursor += 1
            if paragraph_count >= 3:
                zone = 'opening' if paragraph_index == 0 else 'ending' if paragraph_index == paragraph_count - 1 else 'middle'
            else:
                zone = 'middle'
            entries.append({
                'sentence_index': sentence_cursor,
                'sentence': sentence,
                'paragraph_index': paragraph_index,
                'paragraph_number': paragraph_index + 1,
                'paragraph_count': paragraph_count,
                'sentence_in_paragraph': sentence_in_paragraph,
                'zone': zone,
            })
    if paragraph_count < 3 and len(entries) >= 3:
        last_index = len(entries) - 1
        for index, entry in enumerate(entries):
            if index == 0:
                entry['zone'] = 'opening'
            elif index == last_index:
                entry['zone'] = 'ending'
            else:
                entry['zone'] = 'middle'
    return entries


def normalize_annotation_match_text(value=''):
    return re.sub(r'[\s　，。！？；：、,.!?;:“”"‘’（）()《》—…-]+', '', str(value or ''))


def align_annotation_to_source(item, entries):
    if not entries:
        return item, None
    quote_key = normalize_annotation_match_text(item.get('quote') or item.get('original_sentence'))
    best_entry = None
    best_ratio = 0.0
    if quote_key:
        for entry in entries:
            sentence_key = normalize_annotation_match_text(entry['sentence'])
            if not sentence_key:
                continue
            if quote_key in sentence_key or sentence_key in quote_key:
                ratio = min(len(quote_key), len(sentence_key)) / max(len(quote_key), len(sentence_key))
            else:
                ratio = SequenceMatcher(None, quote_key, sentence_key).ratio()
            if ratio > best_ratio:
                best_entry = entry
                best_ratio = ratio
    try:
        requested_index = int(item.get('sentence_index') or 0)
    except (TypeError, ValueError):
        requested_index = 0
    indexed_entry = next((entry for entry in entries if entry['sentence_index'] == requested_index), None)
    if best_entry is None or best_ratio < 0.48:
        best_entry = indexed_entry
    if best_entry is None:
        best_entry = entries[min(max(requested_index - 1, 0), len(entries) - 1)]
    aligned = dict(item)
    aligned['sentence_index'] = best_entry['sentence_index']
    aligned['quote'] = best_entry['sentence']
    aligned['original_sentence'] = best_entry['sentence']
    aligned['_paragraph_index'] = best_entry['paragraph_index']
    aligned['_zone'] = best_entry['zone']
    return aligned, best_entry


def annotation_text_similarity(first, second):
    first_text = normalize_annotation_match_text(
        f"{first.get('problem', '')}{first.get('comment', '')}{first.get('suggestion', '')}"
    )
    second_text = normalize_annotation_match_text(
        f"{second.get('problem', '')}{second.get('comment', '')}{second.get('suggestion', '')}"
    )
    if not first_text or not second_text:
        return 0.0
    return SequenceMatcher(None, first_text, second_text).ratio()


def build_coverage_annotation(entry, annotation_type):
    quote = entry['sentence']
    paragraph_number = entry['paragraph_number']
    zone = entry['zone']
    if annotation_type == 'highlight':
        if re.search(r'像|仿佛|好像|如同|看见|听见|心里|手|眼', quote):
            comment = '这句话包含可感知的动作、心理或画面，能够帮助读者进入情境。'
        else:
            comment = f'第 {paragraph_number} 段这句话承担了清楚的信息表达，能够推动主体内容向前发展。'
        return {
            'type': 'highlight', 'problem': '', 'comment': comment,
            'suggestion': '建议保留这处有效表达，并让前后句继续围绕同一重点展开。', 'rewrite': '',
            'dimension': '语言表达', 'severity': 'low'
        }
    if annotation_type == 'structure':
        if zone == 'middle':
            problem = f'第 {paragraph_number} 段与前后内容的推进关系还可以交代得更清楚。'
            suggestion = '可补充时间、因果或转折过渡句，说明这一层与上一层的联系。'
        elif zone == 'opening':
            problem = '开头已经引出内容，但中心方向还可以更快显现。'
            suggestion = '可在本句后补一句与题目关键词直接相关的点题句。'
        else:
            problem = '结尾已有收束作用，但与前文关键事件的呼应还可以加强。'
            suggestion = '可回扣主体中的关键行动或变化，再点明最终认识。'
        return {
            'type': 'structure', 'problem': problem, 'comment': '这句话所在位置承担着文章层次衔接的重要作用。',
            'suggestion': suggestion, 'rewrite': '', 'dimension': '结构安排', 'severity': 'medium'
        }
    if annotation_type == 'rewrite':
        base = quote.rstrip('。！？!?')
        rewrite = f'{base}，我把当时的动作、周围的变化和心里的想法一一写清，眼前的情景也随之变得具体起来。'
        return {
            'type': 'rewrite', 'problem': '原意清楚，但关键细节还可以进一步展开。',
            'comment': f'第 {paragraph_number} 段这句话适合作为细节扩写的落点。',
            'suggestion': '保留原意，补充一个动作、一处环境变化和当时的心理反应。',
            'rewrite': rewrite, 'dimension': '内容细节', 'severity': 'low'
        }
    return {
        'type': 'suggestion', 'problem': '这句话表达较概括，读者还不容易看见具体过程。',
        'comment': f'第 {paragraph_number} 段已经写到主要内容，但还可以增加可观察的细节。',
        'suggestion': '建议补充人物动作、对话、心理或场景中的一个具体变化。',
        'rewrite': '', 'dimension': '内容细节', 'severity': 'medium'
    }


def make_coverage_annotation(entry, annotation_type, index=0):
    payload = build_coverage_annotation(entry, annotation_type)
    payload.update({
        'id': index + 1,
        'sentence_index': entry['sentence_index'],
        'quote': entry['sentence'],
        'original_sentence': entry['sentence'],
    })
    normalized = normalize_annotation_item(payload, index)
    normalized['_paragraph_index'] = entry['paragraph_index']
    normalized['_zone'] = entry['zone']
    return normalized


def validate_annotation_coverage(content='', annotations=None):
    entries = build_annotation_sentence_map(content)
    annotations = annotations or []
    entry_by_sentence = {entry['sentence_index']: entry for entry in entries}
    covered_paragraphs = {
        entry_by_sentence[item.get('sentence_index')]['paragraph_index']
        for item in annotations
        if item.get('sentence_index') in entry_by_sentence
    }
    paragraph_text = {}
    for entry in entries:
        paragraph_text.setdefault(entry['paragraph_index'], '')
        paragraph_text[entry['paragraph_index']] += entry['sentence']
    major_paragraphs = {
        paragraph_index for paragraph_index, text in paragraph_text.items()
        if count_essay_chars(text) >= 12
    }
    zones = {zone: 0 for zone in ['opening', 'middle', 'ending']}
    middle_types = set()
    sentence_hits = {}
    for item in annotations:
        entry = entry_by_sentence.get(item.get('sentence_index'))
        if not entry:
            continue
        zones[entry['zone']] = zones.get(entry['zone'], 0) + 1
        sentence_hits[entry['sentence_index']] = sentence_hits.get(entry['sentence_index'], 0) + 1
        if entry['zone'] == 'middle':
            middle_types.add(item.get('type'))
    middle_required = min(4, len([entry for entry in entries if entry['zone'] == 'middle']))
    return {
        'covered_paragraphs': covered_paragraphs,
        'missing_paragraphs': sorted(major_paragraphs - covered_paragraphs),
        'zones': zones,
        'middle_types': middle_types,
        'middle_required': middle_required,
        'duplicate_positions': [index for index, count in sentence_hits.items() if count > 1],
    }


def ensure_annotation_coverage(content='', annotations=None, minimum=0, limit=20):
    entries = build_annotation_sentence_map(content)
    if not entries:
        return []
    aligned = []
    for index, raw_item in enumerate(annotations or []):
        normalized = normalize_annotation_item(raw_item, index)
        normalized, _ = align_annotation_to_source(normalized, entries)
        duplicate = next((existing for existing in aligned if existing.get('sentence_index') == normalized.get('sentence_index')), None)
        if duplicate and (duplicate.get('type') == normalized.get('type') or annotation_text_similarity(duplicate, normalized) >= 0.72):
            continue
        if duplicate:
            continue
        aligned.append(normalized)

    entry_by_paragraph = {}
    for entry in entries:
        entry_by_paragraph.setdefault(entry['paragraph_index'], []).append(entry)
    major_paragraphs = [
        paragraph_index for paragraph_index, paragraph_entries in entry_by_paragraph.items()
        if count_essay_chars(''.join(item['sentence'] for item in paragraph_entries)) >= 12
    ]
    if len(major_paragraphs) > limit:
        step = (len(major_paragraphs) - 1) / max(1, limit - 1)
        major_paragraphs = sorted({major_paragraphs[round(index * step)] for index in range(limit)})

    used_sentences = {item.get('sentence_index') for item in aligned}

    def make_room_for_supplement():
        if len(aligned) < limit:
            return True
        paragraph_counts = {}
        for item in aligned:
            paragraph_index = item.get('_paragraph_index')
            paragraph_counts[paragraph_index] = paragraph_counts.get(paragraph_index, 0) + 1
        for index in range(len(aligned) - 1, -1, -1):
            paragraph_index = aligned[index].get('_paragraph_index')
            if paragraph_counts.get(paragraph_index, 0) <= 1:
                continue
            removed = aligned.pop(index)
            used_sentences.discard(removed.get('sentence_index'))
            return True
        return False

    def add_for_entry(entry, annotation_type):
        if not entry or entry['sentence_index'] in used_sentences:
            return False
        if len(aligned) >= limit and not make_room_for_supplement():
            return False
        aligned.append(make_coverage_annotation(entry, annotation_type, len(aligned)))
        used_sentences.add(entry['sentence_index'])
        return True

    coverage_types = ['highlight', 'suggestion', 'structure', 'rewrite']
    for order, paragraph_index in enumerate(major_paragraphs):
        if any(item.get('_paragraph_index') == paragraph_index for item in aligned):
            continue
        candidates = entry_by_paragraph.get(paragraph_index, [])
        candidate = next((entry for entry in candidates if entry['sentence_index'] not in used_sentences), None)
        add_for_entry(candidate, coverage_types[order % len(coverage_types)])

    middle_entries = [entry for entry in entries if entry['zone'] == 'middle']
    middle_types = {
        item.get('type') for item in aligned
        if item.get('_zone') == 'middle'
    }
    for annotation_type in coverage_types:
        if annotation_type in middle_types:
            continue
        candidate = next((entry for entry in middle_entries if entry['sentence_index'] not in used_sentences), None)
        if add_for_entry(candidate, annotation_type):
            middle_types.add(annotation_type)

    target_middle_count = min(len(middle_entries), max(1, minimum // 2)) if middle_entries else 0
    while len([item for item in aligned if item.get('_zone') == 'middle']) < target_middle_count and len(aligned) < limit:
        candidate = next((entry for entry in middle_entries if entry['sentence_index'] not in used_sentences), None)
        if not candidate:
            break
        add_for_entry(candidate, coverage_types[len(aligned) % len(coverage_types)])

    for entry in entries:
        if len(aligned) >= minimum or len(aligned) >= limit:
            break
        add_for_entry(entry, coverage_types[len(aligned) % len(coverage_types)])

    while len(aligned) > limit:
        paragraph_counts = {}
        zone_counts = {}
        middle_type_counts = {}
        for item in aligned:
            paragraph_index = item.get('_paragraph_index')
            zone = item.get('_zone')
            paragraph_counts[paragraph_index] = paragraph_counts.get(paragraph_index, 0) + 1
            zone_counts[zone] = zone_counts.get(zone, 0) + 1
            if zone == 'middle':
                annotation_type = item.get('type')
                middle_type_counts[annotation_type] = middle_type_counts.get(annotation_type, 0) + 1
        remove_index = None
        for index in range(len(aligned) - 1, -1, -1):
            item = aligned[index]
            paragraph_index = item.get('_paragraph_index')
            zone = item.get('_zone')
            if paragraph_counts.get(paragraph_index, 0) <= 1:
                continue
            if zone in {'opening', 'ending'} and zone_counts.get(zone, 0) <= 1:
                continue
            if zone == 'middle' and item.get('type') in coverage_types and middle_type_counts.get(item.get('type'), 0) <= 1:
                continue
            remove_index = index
            break
        if remove_index is None:
            remove_index = len(aligned) - 1
        aligned.pop(remove_index)

    aligned.sort(key=lambda item: (item.get('sentence_index') or 0, item.get('type') or ''))
    for index, item in enumerate(aligned[:limit], 1):
        item['id'] = index
        item.pop('_paragraph_index', None)
        item.pop('_zone', None)
    return aligned[:limit]


def annotation_limit_for_content(content=''):
    return get_expected_annotation_range(content)[1]


def build_annotation_overall(title='', content='', annotations=None):
    annotations = annotations or []
    issue_count = sum(1 for item in annotations if item.get('type') in {'suggestion', 'correction', 'structure'})
    if get_essay_word_count(content) < 120:
        return {
            'strengths': '当前作文内容较短，系统仅提供基础旁批建议。',
            'main_issues': '文章篇幅还不够充分，细节、过程和结尾升华都有继续展开的空间。',
            'next_steps': '建议先补充一件具体事情，再加入动作、心理或环境描写。',
            'upgraded_example': ''
        }
    return {
        'strengths': '本文能够围绕题目展开，已经具备基本的叙述线索和表达基础。',
        'main_issues': '部分语句还偏概括，关键情节的动作、心理和段落衔接可以继续加强。',
        'next_steps': '建议先修改右侧标出的重点句子，再回到全文检查开头点题、过程展开和结尾升华。',
        'upgraded_example': '可选择一处关键经历，按照“动作 + 心理 + 结果”的方式扩写成更有画面感的段落。' if issue_count else ''
    }


def build_annotation_report(title='', content='', annotations=None, overall=None):
    min_count, limit = get_expected_annotation_range(content)
    annotations = ensure_annotation_coverage(content, annotations, min_count, limit)
    for index, item in enumerate(annotations, 1):
        item['id'] = index
    summary = {key: 0 for key in ['highlight', 'suggestion', 'correction', 'structure', 'rewrite']}
    for item in annotations:
        summary[item.get('type') or 'suggestion'] = summary.get(item.get('type') or 'suggestion', 0) + 1
    summary['total'] = len(annotations)
    return {
        'type': 'annotation_report',
        'title': title or '未命名作文',
        'word_count': get_essay_word_count(content),
        'summary': summary,
        'annotations': annotations,
        'inline_comments': annotations,
        'overall': overall if isinstance(overall, dict) else build_annotation_overall(title, content, annotations),
    }


def normalize_inline_comment_payload(payload, fallback, title='', content=''):
    fallback_report = fallback if isinstance(fallback, dict) and isinstance(fallback.get('annotations'), list) else build_annotation_report(title, content, fallback if isinstance(fallback, list) else [])
    if isinstance(payload, dict):
        items = payload.get('annotations') if isinstance(payload.get('annotations'), list) else None
        if items is None:
            items = payload.get('inline_comments') if isinstance(payload.get('inline_comments'), list) else None
        if items is None and isinstance(payload.get('result'), list):
            items = payload.get('result')
        overall = payload.get('overall') if isinstance(payload.get('overall'), dict) else None
        report_title = payload.get('title') or title or fallback_report.get('title')
        if items:
            return build_annotation_report(report_title, content, items, overall)
        return fallback_report
    if isinstance(payload, list):
        return build_annotation_report(title or fallback_report.get('title'), content, payload)
    return fallback_report


def annotation_report_to_text(report):
    annotations = report.get('annotations') if isinstance(report, dict) else []
    lines = [f"智能旁批报告：{report.get('title', '未命名作文') if isinstance(report, dict) else '未命名作文'}"]
    for item in annotations or []:
        block = [
            f"{item.get('id')}. {item.get('type_name') or item.get('type')}｜第{item.get('sentence_index')}句：{item.get('quote') or item.get('original_sentence')}",
            f"点评：{item.get('comment', '')}",
        ]
        if item.get('suggestion'):
            block.append(f"建议：{item.get('suggestion')}")
        if item.get('rewrite'):
            block.append(f"示范：{item.get('rewrite')}")
        lines.append('\n'.join(block))
    overall = report.get('overall') if isinstance(report, dict) else {}
    if overall:
        lines.extend(['', '总体修改建议：', overall.get('next_steps') or '建议结合旁批逐句修改。'])
    return '\n\n'.join(lines)


def sanitize_download_filename(name='智能旁批报告'):
    safe = re.sub(r'[\\/:*?"<>|\s]+', '_', str(name or '智能旁批报告')).strip('_')
    return safe[:48] or '智能旁批报告'


def build_annotation_pdf_file(title='', content='', report=None, output_path=''):
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    except Exception as exc:
        raise RuntimeError('PDF 组件暂未安装，请先安装 reportlab。') from exc

    try:
        pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
        font_name = 'STSong-Light'
    except Exception:
        font_name = 'Helvetica'

    report = report if isinstance(report, dict) else build_annotation_report(title, content, [])
    annotations = report.get('annotations') or []
    summary = report.get('summary') or {}
    generated_at = datetime.now().strftime('%Y-%m-%d %H:%M')
    title_text = report.get('title') or title or '未命名作文'

    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        'ZhibiBase',
        parent=styles['Normal'],
        fontName=font_name,
        fontSize=10.5,
        leading=17,
        textColor=colors.HexColor('#27364a'),
        spaceAfter=5,
    )
    heading = ParagraphStyle(
        'ZhibiHeading',
        parent=base,
        fontSize=18,
        leading=24,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#12345b'),
        spaceAfter=10,
    )
    section = ParagraphStyle(
        'ZhibiSection',
        parent=base,
        fontSize=13,
        leading=18,
        textColor=colors.HexColor('#1d4ed8'),
        spaceBefore=8,
        spaceAfter=6,
    )
    small = ParagraphStyle('ZhibiSmall', parent=base, fontSize=9.5, leading=15, textColor=colors.HexColor('#52657c'))

    def p(text='', style=base, raw=False):
        value = str(text or '')
        return Paragraph(value if raw else html.escape(value).replace('\n', '<br/>'), style)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f'智能旁批报告_{title_text}',
    )
    story = [
        p('智能旁批报告', heading),
        p(f'题目：{title_text}', base),
        p(f'字数：{report.get("word_count") or get_essay_word_count(content)} 字　批注总数：{summary.get("total", len(annotations))}　生成时间：{generated_at}', small),
        Spacer(1, 5),
        p('批注图例', section),
        p('蓝色：亮点句子　橙色：建议优化　红色：字词纠错　紫色：结构提醒　绿色：升格改写', small),
        p('作文原文', section),
    ]

    annotation_map = {}
    for item in annotations:
        idx = int(item.get('sentence_index') or 0)
        if idx:
            annotation_map.setdefault(idx, []).append(str(item.get('id') or ''))

    sentence_cursor = 0
    paragraph_rows = []
    for paragraph in [part.strip() for part in re.split(r'\n+', content or '') if part.strip()]:
        rendered_sentences = []
        for sentence in split_sentences(paragraph) or [paragraph]:
            sentence_cursor += 1
            markers = ''.join(f'[{mark}]' for mark in annotation_map.get(sentence_cursor, []) if mark)
            rendered_sentences.append(f'{sentence}{markers}')
        paragraph_rows.append('　　' + ''.join(rendered_sentences))
    story.extend([p(row, base) for row in paragraph_rows] or [p('暂无作文正文。', small)])

    story.append(p('批注详情', section))
    for item in annotations:
        meta = ANNOTATION_TYPE_META.get(normalize_annotation_type(item.get('type')), ('建议优化', '写作表达', 'medium'))
        lines = [
            f"{item.get('id') or '-'}. {item.get('type_name') or meta[0]}｜{item.get('dimension') or meta[1]}",
            f"原句：{item.get('quote') or item.get('original_sentence') or '未定位到原句'}",
            f"点评：{item.get('comment') or '这句话值得重点关注。'}",
        ]
        if item.get('problem'):
            lines.append(f"问题：{item.get('problem')}")
        if item.get('suggestion'):
            lines.append(f"建议：{item.get('suggestion')}")
        if item.get('rewrite'):
            lines.append(f"示范：{item.get('rewrite')}")
        table = Table([[p('<br/>'.join(html.escape(line) for line in lines), base, raw=True)]], colWidths=[170 * mm])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fbff')),
            ('BOX', (0, 0), (-1, -1), 0.6, colors.HexColor('#dce8f7')),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ]))
        story.extend([table, Spacer(1, 6)])

    doc.build(story)


def local_ai_tool_result(tool, title, content, theme='', genre='', requirement=''):
    if tool == 'outline':
        safe_title = title or '我的成长故事'
        return f'《{safe_title}》\n\n写作目标：围绕中心完成约 {DEFAULT_TARGET_WORD_COUNT} 字。\n\n开头：用一个具体场景引出主题，点明这次经历带来的成长。\n\n主体一：写清事情的起因，交代遇到的困难或困惑。\n\n主体二：重点描写解决问题的过程，补充动作、心理和细节。\n\n主体三：写出转折和变化，让读者看到“我”的成长。\n\n结尾：回扣题目，升华主题，表达继续向前的态度。'
    if tool in {'opening', 'ending', 'continue'}:
        return local_generate_text(tool, title, content, theme, genre, requirement)
    if tool == 'full_text':
        return local_generate_text('full', title, content, theme, genre, requirement)
    if tool == 'polish':
        return local_generate_text('polish', title, content, theme, genre, requirement)
    if tool == 'detail_expand':
        compact = re.sub(r'\s+', '', content or '')
        seed = compact[:90] or '我站在原地，心里有些紧张。'
        return (
            f'{seed}\n\n'
            '我没有急着把这段经历一笔带过，而是停下来仔细回想当时的情景。'
            '周围细小的声音一下子清楚起来，我的手指不自觉地收紧，心里反复衡量着下一步该怎么做。'
            '短暂的犹豫之后，我深吸一口气，先完成眼前最具体的一步，再根据结果调整。'
            '这个动作看起来并不起眼，却让原本模糊的想法有了落点，也让我真正感受到这件事带来的变化。'
        )
    if tool == 'structure':
        return local_optimize_structure(title, content, genre, theme, requirement) or '请先写下作文正文，结构梳理建议会显示在这里。'
    if tool == 'inline_comment':
        return build_annotation_report(title, content, local_annotations(content))
    return local_generate_text('full', title, content)


def stream_inline_comment_events(title, content, grade='', theme='', requirement=''):
    fallback = local_ai_tool_result('inline_comment', title, content)
    prompt = f'''题目：{title or '未命名作文'}
年级：{grade or '中小学'}
主题：{theme or '根据题目和正文判断'}
要求：{requirement or '普通作文'}

正文：
{content}

任务：生成作文批改报告式逐句旁批。请按 SSE 友好的 NDJSON 方式输出，每一行必须是一个独立 JSON 对象，不要 Markdown，不要代码块，不要解释。

输出格式：
{{"type":"inline_comment","data":{{"id":1,"type":"highlight","type_name":"亮点句子","sentence_index":1,"quote":"原文句子","problem":"","comment":"具体点评","suggestion":"修改建议","rewrite":"示范改写","dimension":"语言表达","severity":"low"}}}}
{{"type":"inline_comment","data":{{"id":2,"type":"suggestion","type_name":"建议优化","sentence_index":2,"quote":"原文句子","problem":"问题说明","comment":"具体点评","suggestion":"修改建议","rewrite":"示范改写","dimension":"语言表达","severity":"medium"}}}}
{{"type":"final","message":"逐句旁批完成"}}

要求：
1. type 只能使用 highlight、suggestion、correction、structure、rewrite。
2. sentence_index 使用从 1 开始的句子编号。
3. quote 必须尽量和原文一致。
4. 每条都要包含 comment、problem、suggestion、rewrite、dimension、severity。
5. 每条 comment 和 suggestion 都要具体，禁止空泛评价。
6. 批注数量必须充实，不要只给 3-4 条：100 字以下 2-4 条，100-400 字 4-7 条，400-749 字 8-12 条，750-850 字 12-16 条，超过 850 字 14-20 条。接近 {DEFAULT_TARGET_WORD_COUNT} 字的完整作文建议 12-16 条。
7. 类型分布：亮点句子至少 2 条，建议优化至少 3 条，结构提醒至少 1 条，升格改写至少 1 条；没有明显错别字时，字词纠错可以为 0。
8. 必须先按换行逐段检查；每个主要自然段至少输出 1 条批注，不能只检查开头和结尾。
9. 批注必须覆盖开头、中段和结尾，完整作文至少一半批注放在中间主体段；中段应尽量同时包含 highlight、suggestion、structure、rewrite。
10. 相邻批注优先选择不同段落、不同句子和不同问题；不要对同一句重复生成相似批注，也不要连续集中点评同一段。
11. 优先选择开头句、主题句、关键情节句、细节描写句、表达重复或空泛的句子、转折句、结尾升华句、明显语病或错别字位置、可以升格的句子。
12. 输出 final 前检查 sentence_index 分布；中段不足、主要段落遗漏或位置重复时，先补齐并去重。'''

    def emit_payload(payload, source='local_fallback', message='逐句旁批已生成'):
        yield sse_payload({'type': 'start', 'message': '正在生成逐句旁批...'})
        report = normalize_inline_comment_payload(payload, fallback, title, content)
        annotations = report.get('annotations', [])
        for item in annotations:
            yield sse_payload({'type': 'inline_comment', 'data': item})
            time.sleep(0.02)
        yield sse_payload({
            'done': True,
            'type': 'final',
            'result': report,
            'full_result': json.dumps(report, ensure_ascii=False),
            'source': source,
            'message': message,
        })

    if has_deepseek_key():
        try:
            response = stream_deepseek(
                build_messages(prompt, NDJSON_RESULT_SYSTEM_PROMPT),
                temperature=0.25,
                max_tokens=2800
            )

            def generate():
                yield sse_payload({'type': 'start', 'message': '正在生成逐句旁批...'})
                full = ''
                line_buffer = ''
                streamed_items = []
                for chunk in parse_stream_chunks(response):
                    full += chunk
                    line_buffer += chunk
                    lines = line_buffer.split('\n')
                    line_buffer = lines.pop() if lines else ''
                    for raw_line in lines:
                        raw_line = raw_line.strip().strip(',')
                        if not raw_line:
                            continue
                        try:
                            event = json.loads(raw_line)
                        except Exception:
                            continue
                        if event.get('type') == 'inline_comment':
                            item = event.get('data') or {}
                            streamed_items.append(item)
                            yield sse_payload({'type': 'inline_comment', 'data': item})
                        elif event.get('type') == 'final':
                            yield sse_payload({'type': 'status', 'message': event.get('message') or '逐句旁批完成'})
                tail = line_buffer.strip().strip(',')
                if tail:
                    try:
                        event = json.loads(tail)
                        if event.get('type') == 'inline_comment':
                            item = event.get('data') or {}
                            streamed_items.append(item)
                            yield sse_payload({'type': 'inline_comment', 'data': item})
                    except Exception:
                        pass
                payload = normalize_inline_comment_payload(streamed_items or extract_json_payload(full, fallback), fallback, title, content)
                payload_items = payload.get('annotations', [])
                streamed_signatures = {
                    (int(item.get('sentence_index') or 0), normalize_annotation_type(item.get('type')))
                    for item in streamed_items if isinstance(item, dict)
                }
                for item in payload_items:
                    signature = (int(item.get('sentence_index') or 0), normalize_annotation_type(item.get('type')))
                    if signature in streamed_signatures:
                        continue
                    yield sse_payload({'type': 'inline_comment', 'data': item})
                    streamed_signatures.add(signature)
                    time.sleep(0.01)
                yield sse_payload({
                    'done': True,
                    'type': 'final',
                    'result': payload,
                    'full_result': json.dumps(payload, ensure_ascii=False),
                    'source': 'deepseek',
                    'message': '逐句旁批已生成',
                })
            return sse_response(generate())
        except Exception:
            pass

    return sse_response(emit_payload(fallback, message='已为你生成基础旁批建议'))


def get_recent_history(username, limit=8):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        '''
        SELECT id, title, content, result, action_type, created_at
        FROM history
        WHERE username = ? AND action_type = 'essay_corrected'
        ORDER BY id DESC
        LIMIT ?
        ''',
        (username, limit)
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def get_legacy_user_behaviors(username, limit=80):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        '''
        SELECT id, action_type, title, content, created_at
        FROM history
        WHERE username = ? AND action_type <> 'essay_corrected'
        ORDER BY id DESC
        LIMIT ?
        ''',
        (username, max(1, min(int(limit or 80), 200)))
    )
    rows = [{
        'id': f"legacy-{row['id']}",
        'event_type': row['action_type'] or 'unknown',
        'target_type': 'legacy_history',
        'target_id': '',
        'target_title': row['title'] or '',
        'metadata': {'content': row['content'] or ''},
        'created_at': row['created_at'] or '',
    } for row in cur.fetchall()]
    conn.close()
    return rows


def summarize_user_behaviors(items):
    event_counts = {}
    tool_usage = []
    viewed_titles = []
    for item in items or []:
        event_type = str(item.get('event_type') or '').strip()
        target_title = str(item.get('target_title') or '').strip()
        metadata = item.get('metadata') if isinstance(item.get('metadata'), dict) else {}
        if event_type:
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
        if event_type.startswith('tool_'):
            tool_usage.append({
                'tool': target_title or metadata.get('tool') or event_type[5:],
                'actionKey': metadata.get('action_key') or event_type[5:],
                'time': item.get('created_at') or '',
            })
        recommendation_titles = metadata.get('recommendation_titles')
        if event_type == 'recommendation_viewed' and isinstance(recommendation_titles, list):
            viewed_titles.extend(
                str(title).strip() for title in recommendation_titles
                if str(title or '').strip()
            )
        if event_type in {
            'recommendation_viewed', 'recommendation_clicked', 'recommendation_started',
            'resource_opened', 'material_viewed', 'material_used',
            'video_viewed', 'video_started', 'video_watched', 'microcourse_opened',
        } and target_title and item.get('target_type') != 'personalized_recommendation_set':
            viewed_titles.append(target_title)
    return {
        'event_counts': event_counts,
        'tool_usage': tool_usage[:40],
        'viewed_titles': unique_compact(viewed_titles, 40),
        'recent_events': [{
            'event_type': item.get('event_type') or '',
            'target_type': item.get('target_type') or '',
            'target_title': item.get('target_title') or '',
            'created_at': item.get('created_at') or '',
        } for item in (items or [])[:30]],
    }


def parse_history_scores(item):
    result = item.get('result') or ''
    parsed = extract_json_payload(result, None)
    if isinstance(parsed, dict) and (isinstance(parsed.get('radar_scores'), dict) or isinstance(parsed.get('scores'), dict)):
        score_source = dict(parsed.get('radar_scores') or {})
        if isinstance(parsed.get('scores'), dict):
            score_source = {**parsed.get('scores'), **score_source}
        estimated = estimate_radar_scores(item.get('content') or '', item.get('title') or '')
        scores = {label: pick_dimension_score(score_source, label, estimated.get(label, 76)) for label in DIMENSION_LABELS}
        weighted = {CORRECTION_SCORE_KEYS[label]: scores[label] for label in DIMENSION_LABELS}
        computed = calculate_final_score(weighted)
        return scores, cap_final_score(computed, item.get('content') or '', scores) if item.get('content') else computed
    scores = {}
    for label in DIMENSION_LABELS:
        estimated = estimate_radar_scores(item.get('content') or '', item.get('title') or '')
        match = re.search(rf'{label}\s*[：:]\s*(\d{{1,3}})', result)
        scores[label] = clamp_score(match.group(1) if match else None, estimated.get(label, 76))
    weighted = {CORRECTION_SCORE_KEYS[label]: scores[label] for label in DIMENSION_LABELS}
    computed = calculate_final_score(weighted)
    overall = cap_final_score(computed, item.get('content') or '', scores) if item.get('content') else computed
    return scores, overall


WRITING_WEAKNESS_RULES = [
    ('错别字', '规范', r'错别字|别字|错字|书写错误|用字不准'),
    ('病句 / 语句不通', '语言', r'病句|语句不通|不通顺|句子不顺|表达不顺|搭配不当|语序'),
    ('表达空泛', '内容', r'空泛|笼统|不具体|泛泛|缺少细节|内容单薄'),
    ('描写不足', '内容', r'描写不足|细节不足|细节描写|动作描写|心理描写|环境描写|画面感不足'),
    ('结构混乱', '结构', r'结构混乱|层次不清|段落不清|衔接不自然|过渡|结构|段落'),
    ('主题偏离', '内容', r'偏题|离题|主题偏离|中心不明|中心不突出|立意不清'),
    ('素材不足', '内容', r'素材不足|素材单一|事例不足|例子不足|内容不充实|缺少事例'),
    ('语言不够生动', '语言', r'语言平淡|不够生动|缺少修辞|表达平淡|文采不足|修辞不足'),
    ('结尾薄弱', '结构', r'结尾薄弱|结尾仓促|结尾|升华不足|点题不够'),
    ('开头吸引力不足', '结构', r'开头吸引力不足|开头平淡|开头|起笔|引入不自然'),
    ('文体不匹配', '内容', r'文体不匹配|文体|记叙文|议论文|说明文|格式不符'),
    ('字数不足', '内容', r'字数不足|篇幅偏短|内容太少|字数偏少'),
]


def unique_compact(items, limit=8):
    result = []
    for item in items or []:
        if item and item not in result:
            result.append(item)
        if len(result) >= limit:
            break
    return result


def extract_writing_weakness_tags(text='', content=''):
    source = f'{text or ""}\n{content or ""}'
    tags = [label for label, _, pattern in WRITING_WEAKNESS_RULES if re.search(pattern, source)]
    compact_len = len(re.sub(r'\s+', '', content or ''))
    if compact_len and compact_len < 400:
        tags.append('字数不足')
    if compact_len and '“' not in (content or '') and re.search(r'说|告诉|回答', content or ''):
        tags.append('描写不足')
    return unique_compact(tags)


def rank_weakness_tags(current_tags, history, weakness_profile=None):
    scores = {}
    for tag in current_tags or []:
        scores[tag] = scores.get(tag, 0) + 5
    for index, item in enumerate((history or [])[:8]):
        weight = max(1, 4 - index // 2)
        tags = item.get('weakness_tags') or extract_writing_weakness_tags(item.get('result') or '', item.get('content') or '')
        for tag in tags:
            scores[tag] = scores.get(tag, 0) + weight
    counts = (weakness_profile or {}).get('counts') if isinstance(weakness_profile, dict) else {}
    if isinstance(counts, dict):
        for tag, count in counts.items():
            scores[tag] = scores.get(tag, 0) + min(6, int(count or 0))
    return [tag for tag, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:6]]


def infer_genre_tags(title='', content=''):
    source = f'{title} {content}'
    if re.search(r'议论|观点|论证|证明|因此|由此可见|首先|其次', source):
        return ['议论文']
    if re.search(r'说明|介绍|特点|用途|原理|方法', source):
        return ['说明文']
    if re.search(r'书信|通知|倡议书|演讲稿|申请', source):
        return ['应用文']
    return ['记叙文']


def infer_theme_tags(title='', content=''):
    source = f'{title} {content}'
    rules = [
        ('成长', r'成长|坚持|挫折|改变|突破|努力'),
        ('责任', r'责任|担当|奉献|承诺|守护'),
        ('亲情', r'亲情|父亲|母亲|爸爸|妈妈|外婆|奶奶'),
        ('校园', r'校园|同学|老师|课堂|考试|班级'),
        ('梦想', r'梦想|理想|未来|目标'),
        ('传统文化', r'传统文化|非遗|节日|诗词|汉字'),
        ('科技', r'科技|人工智能|机器人|创新'),
    ]
    matches = [label for label, pattern in rules if re.search(pattern, source)]
    return unique_compact(matches or ['成长'], 3)


def normalize_client_history(items):
    normalized = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        normalized.append({
            'title': item.get('title') or '',
            'content': item.get('content') or '',
            'result': item.get('result') or item.get('correction') or '',
            'action_type': item.get('action_type') or 'client_history',
            'created_at': item.get('time') or item.get('created_at') or '',
            'dimension_scores': item.get('dimension_scores') or {},
            'overall_score': item.get('overall_score') or item.get('overall') or None,
            'weakness_tags': item.get('weakness_tags') if isinstance(item.get('weakness_tags'), list) else [],
        })
    return normalized


def build_profile_from_history(history, tool_usage, title='', content='', weakness_profile=None):
    is_new_user = not bool(history)
    tool_counts = {}
    for item in tool_usage or []:
        tool = item.get('tool') or item.get('actionKey') if isinstance(item, dict) else ''
        if tool:
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
    frequent_tools = [tool for tool, _ in sorted(tool_counts.items(), key=lambda item: item[1], reverse=True)[:3]]
    if is_new_user:
        return {
            'essay_count': 0,
            'average_score': 0,
            'weakest_dimensions': [],
            'weakest_dimension': '',
            'dimension_scores': {},
            'frequent_tools': frequent_tools,
            'weakness_tags': [],
            'current_weakness_tags': [],
            'theme_tags': infer_theme_tags(title, content) if (title or content) else [],
            'genre_tags': infer_genre_tags(title, content) if (title or content) else [],
            'grade_tags': ['中小学'],
            'temporary': bool(title or content),
            'has_data': False,
            'is_new_user': True,
            'summary': '还没有作文诊断记录，先完成一次写作和诊断，再逐步建立你的写作画像。',
        }
    source = history[:8]
    temporary = False
    if not source and (title or content):
        source = [{'title': title, 'content': content, 'result': ''}]
        temporary = True
    totals = {label: 0 for label in DIMENSION_LABELS}
    counts = {label: 0 for label in DIMENSION_LABELS}
    score_total = 0
    score_count = 0
    for item in source:
        if item.get('dimension_scores'):
            estimated = estimate_radar_scores(item.get('content') or '', item.get('title') or '')
            scores = {label: pick_dimension_score(item.get('dimension_scores') or {}, label, estimated.get(label, 76)) for label in DIMENSION_LABELS}
            weighted = {CORRECTION_SCORE_KEYS[label]: scores[label] for label in DIMENSION_LABELS}
            computed = calculate_final_score(weighted)
            overall = cap_final_score(computed, item.get('content') or '', scores) if item.get('content') else computed
        else:
            scores, overall = parse_history_scores(item)
        for label in DIMENSION_LABELS:
            totals[label] += scores[label]
            counts[label] += 1
        score_total += overall
        score_count += 1
    if not source:
        defaults = {'内容': 78, '结构': 74, '语言': 76, '修辞': 72, '规范': 82}
        for label in DIMENSION_LABELS:
            totals[label] = defaults[label]
            counts[label] = 1
        score_total = 76
        score_count = 1
    averages = {label: clamp_score(round(totals[label] / max(1, counts[label]))) for label in DIMENSION_LABELS}
    ranked = sorted(averages.items(), key=lambda item: item[1])
    current_result = history[0].get('result') if history else ''
    current_weakness_tags = extract_writing_weakness_tags(current_result or '', content or (history[0].get('content') if history else ''))
    weakness_tags = rank_weakness_tags(current_weakness_tags, history, weakness_profile)
    weakness_dimension = next((dimension for label, dimension, _ in WRITING_WEAKNESS_RULES if label == (weakness_tags[0] if weakness_tags else '')), ranked[0][0])
    second_dimension = next((dimension for label, dimension, _ in WRITING_WEAKNESS_RULES if label == (weakness_tags[1] if len(weakness_tags) > 1 else '')), ranked[1][0])
    return {
        'essay_count': len(history),
        'average_score': clamp_score(round(score_total / max(1, score_count))),
        'weakest_dimensions': unique_compact([weakness_dimension, second_dimension] + [label for label, _ in ranked[:2]], 3),
        'weakest_dimension': weakness_dimension,
        'dimension_scores': averages,
        'frequent_tools': frequent_tools,
        'weakness_tags': weakness_tags,
        'current_weakness_tags': current_weakness_tags,
        'theme_tags': infer_theme_tags(
            title or (history[0].get('title') if history else ''),
            content or (history[0].get('content') if history else ''),
        ),
        'genre_tags': infer_genre_tags(title, content),
        'grade_tags': ['中小学'],
        'temporary': temporary,
        'has_data': bool(history) or temporary,
        'is_new_user': False,
        'summary': '',
    }


def local_personalized_payload(profile):
    if profile.get('is_new_user') or not profile.get('essay_count'):
        recommendations = [
            {
                'title': '完成第一次完整写作',
                'type': '入门练习',
                'reason': '目前还没有作文记录，先完成一篇文章，系统才能了解你的真实写作情况。',
                'action': f'选择一个熟悉的主题，完成一篇约 {DEFAULT_TARGET_WORD_COUNT} 字的作文。',
            },
            {
                'title': '进行第一次作文诊断',
                'type': '入门诊断',
                'reason': '首次诊断会从内容、结构、语言、修辞和规范五个方面整理反馈。',
                'action': '写完后使用作文诊断，查看整体评价和简短修改建议。',
            },
            {
                'title': '从一条建议开始修改',
                'type': '入门修改',
                'reason': '先完成一次小范围修改，比同时处理很多问题更容易看到变化。',
                'action': '从诊断结果中选择一条建议，修改对应段落后再读一遍。',
            },
            {
                'title': '逐步建立写作画像',
                'type': '成长记录',
                'reason': '积累真实作文和诊断记录后，推荐内容会逐渐贴合你的学习情况。',
                'action': '保留每次写作与修改记录，完成几次练习后再查看成长方向。',
            },
        ]
        for index, item in enumerate(recommendations):
            item['id'] = hashlib.sha1(f"new-user:{item['title']}:{index}".encode('utf-8')).hexdigest()[:12]
        return {
            'profile': profile,
            'recommendations': recommendations,
            'learning_path': [
                {'step': '01', 'title': '首次写作', 'description': f'选择熟悉主题，完成一篇约 {DEFAULT_TARGET_WORD_COUNT} 字的作文'},
                {'step': '02', 'title': '首次诊断', 'description': '查看五维反馈，了解文章当前表现'},
                {'step': '03', 'title': '修改一处', 'description': '选择一条具体建议，完成一次针对性修改'},
                {'step': '04', 'title': '建立画像', 'description': '积累真实记录后获得更贴合的成长建议'},
            ],
            'next_task': {
                'id': hashlib.sha1(b'new-user:first-writing').hexdigest()[:12],
                'title': '完成第一次写作练习',
                'description': f'先写一篇约 {DEFAULT_TARGET_WORD_COUNT} 字的完整作文，再使用作文诊断建立第一份写作画像。',
                'duration': '30 分钟',
            },
        }
    weak = profile.get('weakest_dimension') or '结构'
    weak = '内容' if weak in {'素材', 'material'} else weak
    second = (profile.get('weakest_dimensions') or ['结构', '内容'])[1 if len(profile.get('weakest_dimensions') or []) > 1 else 0]
    second = '内容' if second in {'素材', 'material'} else second
    weakness_tags = profile.get('current_weakness_tags') or profile.get('weakness_tags') or []
    focus_weakness = weakness_tags[0] if weakness_tags else weak
    advice_map = {
        '结构': ('加强段落衔接训练', '练习建议', f'你最近作文中“{focus_weakness}”较明显，段落之间的过渡还可以更自然。', '下一篇作文尝试使用“首先、其次、由此可见”等衔接方式。'),
        '语言': ('润色语句专项训练', '修改建议', f'你最近作文中“{focus_weakness}”较明显，部分句子可以更具体、更顺畅。', '修改时把笼统情绪词换成动作、心理和场景描写。'),
        '修辞': ('修辞表达仿写训练', '练习建议', '你的修辞表现力还可以提升，文章语言可以更有画面感。', '围绕一个主题写 1 个比喻句、1 个排比句和 1 个拟人句。'),
        '规范': ('标点与错别字自查', '修改建议', '你的规范维度需要稳定，建议先减少错别字和标点问题。', '提交前通读一遍，重点检查标点、错别字和病句。'),
        '内容': ('中心与细节补充训练', '练习建议', '你的内容支撑还可以加强，中心需要更明确的细节。', '每段至少补充一个动作、心理或环境细节。'),
    }
    main = advice_map.get(weak, advice_map['结构'])
    extra = advice_map.get(second, advice_map['内容'])
    profile['summary'] = profile.get('summary') or f'你最近作文整体表达较完整，建议优先处理“{focus_weakness}”，同时关注{second}。'
    recommendations = [
        {'title': main[0], 'type': main[1], 'reason': main[2], 'action': main[3]},
        {'title': extra[0], 'type': extra[1], 'reason': extra[2], 'action': extra[3]},
        {'title': '建立作文修改清单', 'type': '修改方向', 'reason': '把作文诊断转化为可执行的小任务，能更稳定提升写作质量。', 'action': '每次诊断后只选 2 个问题重点修改，避免一次改太多。'},
        {'title': '整理主题素材本', 'type': '素材推荐', 'reason': '高频主题素材能减少写作时“有观点没例子”的问题。', 'action': '按成长、责任、坚持、家国情怀四类各整理 1 条素材。'},
    ]
    for index, item in enumerate(recommendations):
        item['id'] = hashlib.sha1(f"{item['title']}:{index}".encode('utf-8')).hexdigest()[:12]
    viewed_titles = set((profile.get('behavior_summary') or {}).get('viewed_titles') or [])
    # Python sort is stable: keep the intended recommendation priority while
    # moving resources the user has already viewed behind fresh suggestions.
    recommendations.sort(key=lambda item: item['title'] in viewed_titles)
    return {
        'profile': profile,
        'recommendations': recommendations,
        'learning_path': [
            {'step': '01', 'title': '观察积累', 'description': f'围绕“{focus_weakness}”留心人物事例和生活细节'},
            {'step': '02', 'title': '完成练笔', 'description': f'完成一篇约 {DEFAULT_TARGET_WORD_COUNT} 字的针对性作文'},
            {'step': '03', 'title': '作文诊断', 'description': f'重点检查{weak}与{second}是否得到改善'},
            {'step': '04', 'title': '复盘优化', 'description': '根据诊断和逐句点评完成二次修改'},
        ],
        'next_task': {
            'id': hashlib.sha1(f'next-task:{weak}'.encode('utf-8')).hexdigest()[:12],
            'title': f'完成一次“{weak}”专项修改',
            'description': f'以约 {DEFAULT_TARGET_WORD_COUNT} 字完整作文为目标，选择最近一篇作文，围绕{weak}补充或修改关键段落，再检查是否解决主要问题。'
        }
    }


def normalize_personalized_payload(payload, fallback):
    if not isinstance(payload, dict):
        return fallback
    generated_profile = payload.get('profile') if isinstance(payload.get('profile'), dict) else {}
    # Counts, scores and weakness tags come from persisted correction history.
    # The model may improve the prose summary, but it must not rewrite facts.
    profile = {
        **fallback['profile'],
        'summary': str(generated_profile.get('summary') or fallback['profile'].get('summary') or '').strip(),
    }
    recommendations = payload.get('recommendations') if isinstance(payload.get('recommendations'), list) else fallback['recommendations']
    clean_recommendations = []
    for index, item in enumerate(recommendations):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get('type') or '练习建议')
        if '微课' in item_type or '课程' in item_type:
            continue
        title = str(item.get('title') or '').strip()
        if not title:
            continue
        clean_recommendations.append({
            'id': str(item.get('id') or hashlib.sha1(f'{title}:{index}'.encode('utf-8')).hexdigest()[:12]),
            'title': title,
            'type': item_type,
            'reason': str(item.get('reason') or '根据你的最近写作表现推荐。'),
            'action': str(item.get('action') or '完成一次针对性修改训练。'),
            'weakness_tags': item.get('weakness_tags') if isinstance(item.get('weakness_tags'), list) else [],
            'theme_tags': item.get('theme_tags') if isinstance(item.get('theme_tags'), list) else [],
            'genre_tags': item.get('genre_tags') if isinstance(item.get('genre_tags'), list) else [],
            'grade_tags': item.get('grade_tags') if isinstance(item.get('grade_tags'), list) else [],
        })
    if not clean_recommendations:
        clean_recommendations = fallback['recommendations']
    raw_learning_path = payload.get('learning_path') if isinstance(payload.get('learning_path'), list) else []
    clean_learning_path = []
    for index, item in enumerate(raw_learning_path[:4]):
        if not isinstance(item, dict):
            continue
        title = str(item.get('title') or '').strip()
        description = str(item.get('description') or item.get('summary') or '').strip()
        if not title or not description:
            continue
        clean_learning_path.append({
            'step': str(item.get('step') or f'{index + 1:02d}').strip(),
            'title': title,
            'description': description,
        })
    if len(clean_learning_path) != 4:
        clean_learning_path = fallback['learning_path']
    next_task = payload.get('next_task') if isinstance(payload.get('next_task'), dict) else fallback['next_task']
    raw_dimensions = profile.get('weakest_dimensions') or fallback['profile'].get('weakest_dimensions') or []
    clean_dimensions = unique_compact([
        '内容' if str(item) in {'素材', 'material'} else str(item)
        for item in raw_dimensions
        if str(item) in set(DIMENSION_LABELS) | {'素材', 'material'}
    ], 3)
    weakest_dimension = profile.get('weakest_dimension') or (clean_dimensions[0] if clean_dimensions else '结构')
    weakest_dimension = '内容' if str(weakest_dimension) in {'素材', 'material'} else weakest_dimension
    if weakest_dimension not in DIMENSION_LABELS:
        weakest_dimension = clean_dimensions[0] if clean_dimensions else '结构'
    second_dimension = profile.get('second_dimension') or (clean_dimensions[1] if len(clean_dimensions) > 1 else '语言')
    second_dimension = '内容' if str(second_dimension) in {'素材', 'material'} else second_dimension
    if second_dimension not in DIMENSION_LABELS:
        second_dimension = '语言'
    raw_dimension_scores = profile.get('dimension_scores') if isinstance(profile.get('dimension_scores'), dict) else {}
    clean_dimension_scores = {
        label: pick_dimension_score(raw_dimension_scores, label, fallback['profile'].get('dimension_scores', {}).get(label, 76))
        for label in DIMENSION_LABELS
    }
    next_task_title = str(next_task.get('title') or fallback['next_task']['title']).replace('素材专项', '内容充实专项').replace('“素材”', '“内容”')
    next_task_description = str(next_task.get('description') or fallback['next_task']['description']).replace('围绕素材', '围绕内容充实')
    return {
        'profile': {
            **fallback['profile'],
            **profile,
            'weakest_dimensions': clean_dimensions,
            'weakest_dimension': weakest_dimension,
            'second_dimension': second_dimension,
            'dimension_scores': clean_dimension_scores,
            'ranked_dimensions': sorted(clean_dimension_scores.items(), key=lambda item: item[1]),
            'weakness_tags': profile.get('weakness_tags') or fallback['profile'].get('weakness_tags') or [],
            'current_weakness_tags': profile.get('current_weakness_tags') or fallback['profile'].get('current_weakness_tags') or [],
            'theme_tags': profile.get('theme_tags') or fallback['profile'].get('theme_tags') or [],
            'genre_tags': profile.get('genre_tags') or fallback['profile'].get('genre_tags') or [],
            'grade_tags': profile.get('grade_tags') or fallback['profile'].get('grade_tags') or ['中小学'],
        },
        'recommendations': clean_recommendations[:6],
        'learning_path': clean_learning_path,
        'next_task': {
            'id': str(next_task.get('id') or fallback['next_task'].get('id') or hashlib.sha1(next_task_title.encode('utf-8')).hexdigest()[:12]),
            'title': next_task_title,
            'description': next_task_description,
            'duration': str(next_task.get('duration') or next_task.get('time') or '15 分钟'),
        }
    }


def prepare_personalized_context(data):
    username = (data.get('username') or request.args.get('username') or 'guest_user').strip()
    title = (data.get('title') or request.args.get('title') or '').strip()
    content = (data.get('content') or request.args.get('content') or '').strip()
    weakness_profile = data.get('weakness_profile') if isinstance(data.get('weakness_profile'), dict) else {}
    client_history = normalize_client_history(data.get('history') if isinstance(data.get('history'), list) else [])
    db_history = get_recent_history(username, 8) if username and not username.startswith('guest') else []
    merged_history = (db_history or client_history)[:8]

    stored_behaviors = get_recent_user_behaviors(username, 80) if username else []
    legacy_behaviors = get_legacy_user_behaviors(username, 80) if username and not username.startswith('guest') else []
    behavior_items = (stored_behaviors + legacy_behaviors)[:120]
    behavior_summary = summarize_user_behaviors(behavior_items)
    client_tool_usage = data.get('tool_usage') if isinstance(data.get('tool_usage'), list) else []
    tool_usage = (behavior_summary['tool_usage'] + client_tool_usage)[:40]
    profile = build_profile_from_history(merged_history, tool_usage, title, content, weakness_profile)
    profile['behavior_summary'] = behavior_summary
    profile['frequent_tools'] = unique_compact(
        profile.get('frequent_tools', []) + [item.get('tool') for item in behavior_summary['tool_usage']],
        4,
    )
    fallback = local_personalized_payload(profile)
    signature_payload = {
        'recommendation_schema': 'new-user-neutral-v2',
        'username': username,
        'title': title,
        'content': content,
        'history': [
            [item.get('id'), item.get('created_at'), item.get('title'), item.get('overall_score')]
            for item in merged_history
        ],
        'scores': profile.get('dimension_scores'),
        'weakness_tags': profile.get('weakness_tags'),
        # Use the normalized title set instead of raw passive-view row IDs.
        # The first view can refresh future recommendations, while repeatedly
        # opening the same recommendation set keeps the cache signature stable.
        'viewed_titles': sorted(behavior_summary.get('viewed_titles') or []),
        'behaviors': [
            [item.get('id'), item.get('event_type'), item.get('target_title'), item.get('created_at')]
            for item in behavior_items[:40]
            if item.get('event_type') != 'recommendation_viewed'
        ],
    }
    context_signature = hashlib.sha256(
        json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')
    ).hexdigest()[:32]
    return {
        'username': username,
        'title': title,
        'content': content,
        'tool_usage': tool_usage,
        'merged_history': merged_history,
        'behavior_summary': behavior_summary,
        'profile': profile,
        'fallback': fallback,
        'context_signature': context_signature,
        'force': bool(data.get('force')),
    }


def build_personalized_prompt(context):
    profile = context['profile']
    merged_history = context['merged_history']
    tool_usage = context['tool_usage']
    title = context['title']
    content = context['content']
    behavior_summary = context.get('behavior_summary') or {}
    return f'''请根据下面的写作画像生成个性化推荐，只返回合法 JSON，不要 Markdown。
不要生成微课、课程、视频内容，因为微课学习已经是独立页面。
推荐必须优先依据用户薄弱项，而不是随机推荐或只按主题推荐。排序权重：薄弱项匹配 45%，当前作文主题 20%，文体 15%，年级/学段 10%，最近是否重复看过 10%。
如果 current_weakness_tags 不为空，必须优先参考本次作文暴露的问题；否则使用 weakness_tags 和历史记录。
当 is_new_user 为 true 或 essay_count 为 0 时，禁止虚构薄弱项、低分维度、常见问题或“最近作文表现”；只能推荐首次写作、首次诊断、完成一次修改和逐步建立画像。
所有完整作文练习默认以约 {DEFAULT_TARGET_WORD_COUNT} 字为目标；低于 400 字时优先推荐补充具体事件和细节，400-749 字时推荐继续扩写，750-850 字视为完成度较高，850 字以上提醒关注详略和精炼。
    作文篇数、平均分、五维分数和薄弱项已经由后端从数据库计算，禁止自行改写或臆测这些数据。
    返回格式：
    {{
      "recommendations": [
        {{"title": "...", "type": "素材推荐/练习建议/修改方向", "reason": "推荐理由必须说明命中的薄弱项", "action": "...", "weakness_tags": ["描写不足"], "theme_tags": ["成长"], "genre_tags": ["记叙文"], "grade_tags": ["初中"]}}
      ],
      "learning_path": [
        {{"step": "01", "title": "...", "description": "结合薄弱项给出简短行动"}}
      ],
      "next_task": {{"title": "...", "description": "..."}}
    }}
learning_path 必须恰好返回 4 步，形成“积累或准备 → 完成练习 → 作文诊断 → 复盘修改”的连续路径，每一步都要结合当前薄弱项，不能返回空泛占位内容。
用户画像：{json.dumps(profile, ensure_ascii=False)}
最近作文记录摘要：{json.dumps(merged_history[:5], ensure_ascii=False)[:4000]}
常用功能：{json.dumps(tool_usage[:12], ensure_ascii=False)}
近期使用行为：{json.dumps(behavior_summary, ensure_ascii=False)[:2500]}
当前作文题目：{title}
当前作文正文：{content[:1200]}'''


def personalized_messages(context):
    return [
        {'role': 'system', 'content': '你是中小学作文学习平台的个性化推荐引擎。只返回合法 JSON，不要推荐微课。'},
        {'role': 'user', 'content': build_personalized_prompt(context)}
    ]


def persist_personalized_payload(context, payload, source):
    recommendation_id = save_personalized_recommendation(
        context['username'],
        context['context_signature'],
        payload,
        source,
    )
    return {
        **payload,
        'recommendation_record_id': recommendation_id,
        'context_signature': context['context_signature'],
        'source': source,
    }


def stream_personalized_payload(payload, source, message, context=None, recommendation_id=None):
    if context is not None:
        payload = persist_personalized_payload(context, payload, source)
    elif recommendation_id:
        payload = {
            **payload,
            'recommendation_record_id': recommendation_id,
            'source': source,
        }

    def generate():
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        yield sse_payload({'type': 'start', 'source': source, 'message': message})
        for index in range(0, len(text), 80):
            yield sse_payload({'content': text[index:index + 80]})
            time.sleep(0.01)
        yield sse_payload({
            'done': True,
            'full_result': text,
            'result': payload,
            'source': source,
            'message': message,
        })
    return sse_response(generate())


def stream_personalized_fallback(context, payload, message='还没有足够记录生成专属建议，请先完成一次写作练习。'):
    return stream_personalized_payload(payload, 'local_fallback', message, context=context)


def normalize_ocr_line(line):
    text = (line or '').strip()
    if not text:
        return ''
    text = re.sub(r'\s+', '', text)
    text = text.translate(str.maketrans({
        ',': '，',
        '.': '。',
        '?': '？',
        '!': '！',
        ';': '；',
        ':': '：',
        '(': '（',
        ')': '）',
    }))
    text = re.sub(r'[，。！？；：、]{2,}', lambda m: m.group(0)[0], text)
    text = text.replace('她强够', '勉强够').replace('她強夠', '勉强够')
    text = re.sub(r'^[·•\-\—_=~*#]+$', '', text)
    return text.strip()


def looks_like_ocr_noise(line):
    if not line:
        return True
    compact = re.sub(r'\s+', '', line)
    if not compact:
        return True
    if re.fullmatch(r'[\W_]+', compact):
        return True
    return len(re.findall(r'[\u4e00-\u9fffA-Za-z0-9]', compact)) == 0


def is_likely_paragraph_break(current_line, next_line=''):
    current = current_line or ''
    next_text = next_line or ''
    if not current:
        return False
    if re.search(r'[。！？；]$', current) and len(current) >= 24:
        if not next_text:
            return True
        if re.match(r'^(后来|从此|于是|可是|但是|然而|最后|总之|回想|每当|第二天|有一次)', next_text):
            return True
    if len(current) <= 12 and re.search(r'[。！？]$', current) and re.match(r'^(后来|从此|最后|总之|回想)', next_text):
        return True
    return False


def normalize_ocr_text(lines):
    cleaned_lines = []
    for line in lines or []:
        normalized = normalize_ocr_line(line)
        if normalized and not looks_like_ocr_noise(normalized):
            cleaned_lines.append(normalized)

    paragraphs = []
    current = ''
    for index, line in enumerate(cleaned_lines):
        next_line = cleaned_lines[index + 1] if index + 1 < len(cleaned_lines) else ''
        if not current:
            current = line
        else:
            if re.search(r'[，、（“《]$', current) or not re.search(r'[。！？；：]$', current):
                current += line
            else:
                current += line
        if is_likely_paragraph_break(line, next_line):
            paragraphs.append(current.strip())
            current = ''
    if current.strip():
        paragraphs.append(current.strip())

    if not paragraphs and cleaned_lines:
        paragraphs = [''.join(cleaned_lines).strip()]
    formatted = '\n\n'.join(paragraph for paragraph in paragraphs if paragraph)
    formatted = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', formatted)
    formatted = re.sub(r'\n{3,}', '\n\n', formatted)
    return formatted.strip(), cleaned_lines


def handle_ocr_upload():
    if 'image' not in request.files:
        return jsonify({'success': False, 'status': 'error', 'text': '', 'message': '请上传图片文件'}), 400
    file = request.files['image']
    filename = (file.filename or '').lower()
    if not filename.endswith(('.jpg', '.jpeg', '.png')):
        return jsonify({'success': False, 'status': 'error', 'text': '', 'message': '仅支持 jpg、jpeg、png 图片'}), 400
    image_bytes = file.read()
    if not image_bytes:
        return jsonify({'success': False, 'status': 'error', 'text': '', 'message': '图片文件为空'}), 400
    if len(image_bytes) > 5 * 1024 * 1024:
        return jsonify({'success': False, 'status': 'error', 'text': '', 'message': '图片过大，请上传 5MB 以内的清晰图片'}), 400
    image_base64 = base64.b64encode(image_bytes).decode('utf-8')
    words_result, error = recognize_image_with_baidu(image_base64)
    if error:
        return jsonify({'success': False, 'status': 'error', 'text': '', 'message': '图片识别失败，请检查图片清晰度或 OCR 配置'}), 500
    lines = [item.get('words', '') for item in words_result or [] if item.get('words')]
    raw_text = '\n'.join(line.strip() for line in lines if line and line.strip()).strip()
    formatted_text, normalized_lines = normalize_ocr_text(lines)
    text = formatted_text or raw_text
    return jsonify({
        'success': True,
        'status': 'success',
        'text': text,
        'result': text,
        'raw_text': raw_text,
        'formatted_text': text,
        'lines': normalized_lines,
        'message': '图片识别完成，已整理为可编辑的作文内容'
    })


def configure_ai_routes(app):
    @app.route('/downloads/<path:filename>', methods=['GET'])
    def download_generated_file(filename):
        downloads_dir = os.path.join(app.root_path, 'downloads')
        file_path = os.path.join(downloads_dir, filename)
        if not os.path.isfile(file_path):
            return jsonify({'success': False, 'message': 'PDF 文件不存在，请重新生成。'}), 404
        return send_from_directory(downloads_dir, filename, as_attachment=True)

    @app.route('/api/export_annotation_pdf', methods=['GET', 'POST'])
    def export_annotation_pdf():
        if request.method != 'POST':
            return jsonify({'success': False, 'message': '请在智能旁批报告中点击“导出 PDF”。'}), 405
        try:
            data = request.get_json(silent=True) or {}
            title = (data.get('title') or '').strip()
            content = (data.get('content') or '').strip()
            annotations = data.get('annotations') if isinstance(data.get('annotations'), list) else []
            payload = {
                'title': title,
                'annotations': annotations,
                'overall': data.get('overall') if isinstance(data.get('overall'), dict) else None,
            }
            report = normalize_inline_comment_payload(payload, build_annotation_report(title, content, annotations), title, content)
            downloads_dir = os.path.join(app.root_path, 'downloads')
            os.makedirs(downloads_dir, exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            safe_title = sanitize_download_filename(title or report.get('title') or '未命名作文')
            internal_filename = f'annotation_report_{timestamp}_{uuid.uuid4().hex[:8]}.pdf'
            display_filename = f'智能旁批报告_{safe_title}_{timestamp}.pdf'
            output_path = os.path.join(downloads_dir, internal_filename)
            build_annotation_pdf_file(title, content, report, output_path)
        except Exception:
            app.logger.exception('导出智能旁批 PDF 失败')
            return jsonify({'success': False, 'message': 'PDF 生成失败，请稍后重试。'}), 500
        return jsonify({
            'success': True,
            'download_url': f'/downloads/{internal_filename}',
            'filename': display_filename
        })

    @app.route('/api/ai-tool', methods=['POST'])
    def ai_tool():
        data = request.get_json(silent=True) or {}
        tool = normalize_tool_name(data.get('tool'))
        if tool not in SUPPORTED_AI_TOOLS:
            return jsonify({'success': False, 'message': '不支持的写作工具'}), 400
        title = (data.get('title') or '').strip()
        content = (data.get('content') or '').strip()
        username = (data.get('username') or 'guest_user').strip()
        grade, theme, genre, requirement = get_request_writing_context(data, title)

        if tool in {'continue', 'polish', 'structure', 'correction', 'inline_comment', 'detail_expand'} and not content:
            return jsonify({'success': False, 'message': '请先写下作文正文内容'}), 400
        if tool in {'opening', 'ending', 'full_text', 'outline'} and not (title or theme or content):
            return jsonify({'success': False, 'message': '请先写下作文题目、主题或正文内容'}), 400

        source = 'deepseek'
        message = '建议已生成'
        try:
            if tool == 'correction':
                fallback = local_correction_payload(title, content)
                raw = ''
                if has_deepseek_key():
                    raw = call_deepseek(
                        build_ai_tool_messages(tool, title, content, grade, theme, requirement),
                        temperature=0.25,
                        max_tokens=1800
                    )
                else:
                    source = 'local_fallback'
                    message = '当前写作服务暂时不可用，已为你整理基础诊断建议'
                payload = normalize_correction_payload(extract_json_payload(raw, fallback), title, content) if raw else fallback
                text = correction_payload_to_text(payload)
                history_id = save_correction_history(app.logger, username, title, content, payload, source)
                return jsonify({'success': True, 'type': tool, 'result': payload, 'text': text, 'source': source, 'message': message, 'history_id': history_id})

            if tool == 'inline_comment':
                fallback = local_ai_tool_result(tool, title, content, theme, genre, requirement)
                raw = ''
                if has_deepseek_key():
                    raw = call_deepseek(
                        build_ai_tool_messages(tool, title, content, grade, theme, requirement),
                        temperature=0.25,
                        max_tokens=2800
                    )
                else:
                    source = 'local_fallback'
                    message = '当前写作服务暂时不可用，已为你整理基础旁批建议'
                payload = normalize_inline_comment_payload(extract_json_payload(raw, fallback), fallback, title, content) if raw else fallback
                text = annotation_report_to_text(payload)
                return jsonify({
                    'success': True,
                    'type': tool,
                    'result': payload,
                    'annotations': payload.get('annotations', []),
                    'inline_comments': payload.get('inline_comments', []),
                    'text': text,
                    'source': source,
                    'message': message
                })

            if tool == 'structure':
                fallback_text = local_ai_tool_result(tool, title, content, theme, genre, requirement)
                raw = ''
                if has_deepseek_key():
                    raw = call_deepseek(
                        build_ai_tool_messages(tool, title, content, grade, theme, requirement),
                        temperature=0.45,
                        max_tokens=1600,
                    )
                else:
                    source = 'local_fallback'
                    message = '当前写作服务暂时不可用，已按本文文体整理结构图'
                payload = normalize_structure_payload(raw, fallback_text)
                text = json.dumps(payload, ensure_ascii=False)
                return jsonify({
                    'success': True,
                    'type': tool,
                    'result': payload,
                    'text': text,
                    'source': source,
                    'message': message,
                })

            fallback_text = local_ai_tool_result(tool, title, content, theme, genre, requirement)
            if has_deepseek_key():
                raw = call_deepseek(
                    build_ai_tool_messages(tool, title, content, grade, theme, requirement),
                    temperature=0.82 if tool == 'full_text' else 0.6,
                    max_tokens=2300 if tool == 'full_text' else (600 if tool == 'detail_expand' else 1800)
                )
                if tool == 'full_text':
                    context_matches = full_text_matches_context(raw, title, theme, genre, requirement)
                    result = complete_full_text_result(raw, title, grade, theme, genre, requirement)
                    if not context_matches:
                        source = 'local_fallback'
                        message = '模型初稿与题目要求不一致，已由后端生成匹配初稿'
                else:
                    result = clean_writing_output(raw) if tool in {'opening', 'ending', 'continue', 'polish', 'detail_expand'} else raw.strip()
                if not result:
                    result = fallback_text
            else:
                source = 'local_fallback'
                message = '当前写作服务暂时不可用，已为你整理基础建议'
                result = fallback_text
            return jsonify({'success': True, 'type': tool, 'result': result, 'text': result, 'source': source, 'message': message})
        except Exception:
            if not ENABLE_LOCAL_FALLBACK:
                return jsonify({'success': False, 'message': '当前写作服务暂时不可用，请稍后重试。'}), 500
            fallback = local_ai_tool_result(tool, title, content, theme, genre, requirement)
            if tool == 'correction':
                text = correction_payload_to_text(fallback)
                return jsonify({'success': True, 'type': tool, 'result': fallback, 'text': text, 'source': 'local_fallback', 'message': '当前写作服务暂时不可用，已为你整理基础建议'})
            if tool == 'inline_comment':
                text = annotation_report_to_text(fallback)
                return jsonify({
                    'success': True,
                    'type': tool,
                    'result': fallback,
                    'annotations': fallback.get('annotations', []) if isinstance(fallback, dict) else [],
                    'inline_comments': fallback.get('inline_comments', []) if isinstance(fallback, dict) else [],
                    'text': text,
                    'source': 'local_fallback',
                    'message': '当前写作服务暂时不可用，已为你整理基础旁批建议'
                })
            return jsonify({'success': True, 'type': tool, 'result': fallback, 'text': fallback if isinstance(fallback, str) else json.dumps(fallback, ensure_ascii=False), 'source': 'local_fallback', 'message': '当前写作服务暂时不可用，已为你整理基础建议'})

    @app.route('/api/ai-tool/stream', methods=['POST'])
    def ai_tool_stream():
        data = request.get_json(silent=True) or {}
        tool = normalize_tool_name(data.get('tool'))
        if tool not in SUPPORTED_AI_TOOLS:
            return sse_error('不支持的写作工具')
        title = (data.get('title') or '').strip()
        content = (data.get('content') or '').strip()
        grade, theme, genre, requirement = get_request_writing_context(data, title)
        if tool == 'correction':
            return sse_error('作文诊断请使用专用流式接口')
        if tool in {'continue', 'polish', 'structure', 'inline_comment', 'detail_expand'} and not content:
            return sse_error('请先写下作文正文内容')
        if tool in {'opening', 'ending', 'full_text', 'outline'} and not (title or theme or content):
            return sse_error('请先写下作文题目、主题或正文内容')
        if tool == 'inline_comment':
            return stream_inline_comment_events(title, content, grade, theme, requirement)
        fallback = local_ai_tool_result(tool, title, content, theme, genre, requirement)
        messages = build_ai_tool_messages(tool, title, content, grade, theme, requirement)
        if tool == 'structure':
            return stream_structure_result(
                messages,
                fallback if isinstance(fallback, str) else json.dumps(fallback, ensure_ascii=False),
                temperature=0.45,
            )
        if tool == 'full_text':
            return stream_full_text_result(
                messages,
                fallback if isinstance(fallback, str) else json.dumps(fallback, ensure_ascii=False),
                title=title,
                grade=grade,
                theme=theme,
                genre=genre,
                requirement=requirement,
                temperature=0.82,
            )
        clean_output = tool in {'opening', 'ending', 'continue', 'polish', 'full_text', 'detail_expand'}
        return ai_or_fallback_stream(
            messages,
            fallback if isinstance(fallback, str) else json.dumps(fallback, ensure_ascii=False),
            temperature=0.82 if tool == 'full_text' else 0.6,
            max_tokens=600 if tool == 'detail_expand' else 1800,
            clean_output=clean_output
        )

    @app.route('/api/correct_stream', methods=['POST'])
    def correct_stream():
        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        content = (data.get('content') or '').strip()
        username = (data.get('username') or 'guest_user').strip()
        grade, theme, genre, requirement = get_request_writing_context(data, title)
        if not content:
            return sse_error('请先写下作文正文内容')

        fallback = local_correction_payload(title, content)
        if not has_deepseek_key():
            return stream_correction_fallback(fallback, username, title, content, app.logger, '当前写作服务暂时不可用，已为你整理基础诊断建议')

        def generate():
            yield sse_payload({'type': 'start', 'message': '正在细读你的作文...'})
            full = ''
            line_buffer = ''
            scores = {}
            summary = ''
            issue_cards = []
            optimized_text = ''
            emitted_any = False
            fallback_stream_scores = {
                CORRECTION_SCORE_KEYS[label]: fallback['radar_scores'][label]
                for label in CORRECTION_DIMENSIONS
            }
            label_by_key = {CORRECTION_SCORE_KEYS[label]: label for label in CORRECTION_DIMENSIONS}

            def missing_score_events():
                for key, value in fallback_stream_scores.items():
                    if key not in scores:
                        scores[key] = value
                        yield {
                            'type': 'score',
                            'key': key,
                            'label': label_by_key[key],
                            'value': value,
                        }

            try:
                response = stream_deepseek(
                    build_correction_stream_messages(title, content, grade, theme, requirement),
                    temperature=0.25,
                    max_tokens=1300,
                    timeout=(6, 35)
                )
                for chunk in parse_stream_chunks(response):
                    full += chunk
                    line_buffer += chunk
                    lines = line_buffer.split('\n')
                    line_buffer = lines.pop() if lines else ''
                    for raw_line in lines:
                        raw_line = raw_line.strip().strip(',')
                        if not raw_line:
                            continue
                        try:
                            event = normalize_correction_stream_event(json.loads(raw_line))
                        except Exception:
                            continue
                        if not event:
                            continue
                        emitted_any = True
                        if event['type'] == 'score':
                            scores[event['key']] = event['value']
                        elif event['type'] == 'summary':
                            for score_event in missing_score_events():
                                yield sse_payload(score_event)
                            summary = event.get('content', '')
                        elif event['type'] == 'suggestion':
                            for score_event in missing_score_events():
                                yield sse_payload(score_event)
                            data = event.get('data') or {}
                            issue_cards.append({
                                'type': data.get('type') or '建议',
                                'original_sentence': data.get('original') or '',
                                'suggestion': data.get('advice') or '',
                                'reason': data.get('reason') or '',
                            })
                        elif event['type'] == 'optimized_text':
                            for score_event in missing_score_events():
                                yield sse_payload(score_event)
                            optimized_text = event.get('content', '')
                        elif event['type'] == 'final':
                            for score_event in missing_score_events():
                                yield sse_payload(score_event)
                        if event['type'] != 'final':
                            yield sse_payload(event)

                tail = line_buffer.strip().strip(',')
                if tail:
                    try:
                        event = normalize_correction_stream_event(json.loads(tail))
                    except Exception:
                        event = None
                    if event:
                        emitted_any = True
                        if event['type'] == 'score':
                            scores[event['key']] = event['value']
                        elif event['type'] == 'summary':
                            for score_event in missing_score_events():
                                yield sse_payload(score_event)
                            summary = event.get('content', '')
                        elif event['type'] == 'suggestion':
                            for score_event in missing_score_events():
                                yield sse_payload(score_event)
                            data = event.get('data') or {}
                            issue_cards.append({
                                'type': data.get('type') or '建议',
                                'original_sentence': data.get('original') or '',
                                'suggestion': data.get('advice') or '',
                                'reason': data.get('reason') or '',
                            })
                        elif event['type'] == 'optimized_text':
                            for score_event in missing_score_events():
                                yield sse_payload(score_event)
                            optimized_text = event.get('content', '')
                        elif event['type'] == 'final':
                            for score_event in missing_score_events():
                                yield sse_payload(score_event)
                        if event['type'] != 'final':
                            yield sse_payload(event)

                if not emitted_any:
                    payload = normalize_correction_payload(extract_json_payload(full, fallback), title, content)
                    for event in correction_payload_to_stream_events(payload):
                        yield sse_payload(event)
                else:
                    payload = normalize_correction_payload({
                        'radar_scores': {
                            '内容': scores.get('content'),
                            '结构': scores.get('structure'),
                            '语言': scores.get('language'),
                            '修辞': scores.get('rhetoric'),
                            '规范': scores.get('standard'),
                        },
                        'summary': summary,
                        'issue_cards': issue_cards,
                        'optimized_text': optimized_text,
                    }, title, content)

                history_id = save_correction_history(app.logger, username, title, content, payload, 'deepseek')
                yield sse_payload({
                    'type': 'final',
                    'message': '作文诊断完成',
                    'result': payload,
                    'text': correction_payload_to_text(payload),
                    'source': 'deepseek',
                    'history_id': history_id,
                    'done': True,
                })
            except Exception:
                app.logger.exception('流式作文诊断异常，使用本地兜底：username=%s title=%s', username, title)
                fallback_payload = local_correction_payload(title, content)
                for event in correction_payload_to_stream_events(fallback_payload):
                    yield sse_payload(event)
                history_id = save_correction_history(app.logger, username, title, content, fallback_payload, 'local_fallback')
                yield sse_payload({
                    'type': 'final',
                    'message': '当前写作服务暂时不可用，已为你生成基础诊断',
                    'result': fallback_payload,
                    'text': correction_payload_to_text(fallback_payload),
                    'source': 'local_fallback',
                    'notice': '本次使用本地兜底评分',
                    'history_id': history_id,
                    'done': True,
                })

        return sse_response(generate())

    @app.route('/api/personalized-recommendations', methods=['GET', 'POST'])
    def personalized_recommendations():
        data = (request.get_json(silent=True) or {}) if request.method == 'POST' else {}
        context = prepare_personalized_context(data)
        fallback = context['fallback']
        cached = None if context['force'] else get_personalized_recommendation(
            context['username'], context['context_signature']
        )
        if cached:
            return jsonify({
                'success': True,
                'status': 'success',
                **cached['result'],
                'source': 'database_cache',
                'original_source': cached.get('source') or '',
                'message': '已加载最近生成的写作建议',
                'recommendation_record_id': cached['id'],
                'context_signature': cached['context_signature'],
            })
        if context['profile'].get('is_new_user'):
            payload = persist_personalized_payload(context, fallback, 'onboarding')
            return jsonify({
                'success': True,
                'status': 'success',
                'message': '已为你整理首次写作与画像建立建议',
                **payload,
            })
        source = 'deepseek'
        message = '写作建议已整理'
        if has_deepseek_key():
            try:
                raw = call_deepseek(personalized_messages(context), temperature=0.35, max_tokens=1600)
                payload = normalize_personalized_payload(extract_json_payload(raw, fallback), fallback)
            except Exception:
                app.logger.exception('个性化推荐生成失败，改用后端本地兜底：username=%s', context['username'])
                payload = fallback
                source = 'local_fallback'
                message = '当前写作服务暂时不可用，已根据写作记录整理建议'
        else:
            payload = fallback
            source = 'local_fallback'
            message = (
                '已根据作文记录和使用行为整理基础建议'
                if context['profile'].get('essay_count')
                else '已为你整理入门写作建议'
            )
        payload = persist_personalized_payload(context, payload, source)
        return jsonify({'success': True, 'status': 'success', 'message': message, **payload})

    @app.route('/api/personalized-recommendations/stream', methods=['POST'])
    def personalized_recommendations_stream():
        data = request.get_json(silent=True) or {}
        context = prepare_personalized_context(data)
        fallback = context['fallback']
        cached = None if context['force'] else get_personalized_recommendation(
            context['username'], context['context_signature']
        )
        if cached:
            return stream_personalized_payload(
                {**cached['result'], 'context_signature': cached['context_signature']},
                'database_cache',
                '已加载最近生成的写作建议',
                recommendation_id=cached['id'],
            )
        if context['profile'].get('is_new_user'):
            return stream_personalized_payload(
                fallback,
                'onboarding',
                '已为你整理首次写作与画像建立建议',
                context=context,
            )
        if not has_deepseek_key():
            fallback_message = (
                '已根据作文记录和使用行为整理基础建议'
                if context['profile'].get('essay_count')
                else '已为你整理入门写作建议'
            )
            return stream_personalized_fallback(context, fallback, fallback_message)
        try:
            response = stream_deepseek(personalized_messages(context), temperature=0.35, max_tokens=1600)
        except Exception:
            app.logger.exception('个性化推荐流建立失败，改用后端本地兜底：username=%s', context['username'])
            return stream_personalized_fallback(context, fallback, '当前写作服务暂时不可用，已根据写作记录整理建议')

        def generate():
            full = ''
            try:
                for chunk in parse_stream_chunks(response):
                    full += chunk
                    yield sse_payload({'content': chunk})
                payload = normalize_personalized_payload(extract_json_payload(full, fallback), fallback)
                payload = persist_personalized_payload(context, payload, 'deepseek')
                yield sse_payload({
                    'done': True,
                    'full_result': full,
                    'result': payload,
                    'source': 'deepseek',
                    'message': '写作建议已整理',
                })
            except Exception:
                app.logger.exception('个性化推荐流读取失败，改用后端本地兜底：username=%s', context['username'])
                fallback_payload = persist_personalized_payload(context, fallback, 'local_fallback')
                text = json.dumps(fallback_payload, ensure_ascii=False, indent=2)
                yield sse_payload({
                    'done': True,
                    'full_result': text,
                    'result': fallback_payload,
                    'source': 'local_fallback',
                    'message': '当前写作服务暂时不可用，已根据写作记录整理建议',
                })
        return sse_response(generate())

    @app.route('/api/ocr', methods=['POST'])
    def api_ocr():
        return handle_ocr_upload()

    @app.route('/generate', methods=['POST'])
    def generate():
        data = request.get_json(silent=True) or {}
        kind = data.get('type') or 'full'
        if kind == 'detail':
            kind = 'detail_expand'
        title = (data.get('title') or '').strip()
        content = (data.get('content') or '').strip()
        grade, theme, genre, requirement = get_request_writing_context(data, title)
        if kind == 'structure':
            fallback = local_optimize_structure(title, content, genre, theme, requirement)
            messages = build_messages(
                build_structure_prompt(title, content, grade, theme, requirement),
                JSON_RESULT_SYSTEM_PROMPT
            )
            return stream_structure_result(messages, fallback, temperature=0.45)
        fallback = local_generate_text(kind, title, content, theme, genre, requirement)
        max_tokens_map = {
            'opening': 320,
            'ending': 320,
            'continue': 520,
            'polish': 1500,
            'full': 2300,
            'detail_expand': 600,
        }
        messages = build_messages(build_writing_user_prompt(kind, title, content, grade, theme, requirement))
        if kind == 'full':
            return stream_full_text_result(
                messages,
                fallback,
                title=title,
                grade=grade,
                theme=theme,
                genre=genre,
                requirement=requirement,
                temperature=0.65,
            )
        return ai_or_fallback_stream(
            messages,
            fallback,
            temperature=0.65,
            max_tokens=max_tokens_map.get(kind, 1000),
            clean_output=True
        )

    @app.route('/optimize_structure_stream', methods=['POST'])
    def optimize_structure_stream():
        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        content = (data.get('content') or '').strip()
        grade, theme, genre, requirement = get_request_writing_context(data, title)
        fallback = local_optimize_structure(title, content, genre, theme, requirement)
        messages = build_messages(
            build_structure_prompt(title, content, grade, theme, requirement),
            JSON_RESULT_SYSTEM_PROMPT
        )
        return stream_structure_result(messages, fallback, temperature=0.45)

    @app.route('/correct', methods=['POST'])
    def correct():
        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        content = (data.get('content') or '').strip()
        username = (data.get('username') or 'guest_user').strip()
        grade, theme, genre, requirement = get_request_writing_context(data, title)
        if not title or not content:
            return jsonify({'status': 'error', 'message': '请先写下作文题目和正文内容'}), 400
        fallback_payload = local_correction_payload(title, content)
        source = 'local_fallback'
        payload = fallback_payload
        if has_deepseek_key():
            try:
                raw = call_deepseek(
                    build_messages(build_correction_prompt(title, content, grade, theme, requirement), JSON_RESULT_SYSTEM_PROMPT),
                    temperature=0.25,
                    max_tokens=1800
                )
                payload = normalize_correction_payload(extract_json_payload(raw, fallback_payload), title, content)
                source = 'deepseek'
            except Exception:
                app.logger.exception('旧版作文批改异常，使用本地兜底：username=%s title=%s', username, title)
        text = correction_payload_to_text(payload)
        history_id = save_correction_history(app.logger, username, title, content, payload, source)

        def generate_legacy_correction():
            for chunk in re.split(r'(?<=[。！？\n])', text):
                if chunk:
                    yield sse_payload({'content': chunk})
            yield sse_payload({
                'done': True,
                'full_result': text,
                'result': payload,
                'source': source,
                'history_id': history_id,
            })

        return sse_response(generate_legacy_correction())

    @app.route('/smart_annotation', methods=['POST'])
    def smart_annotation():
        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        content = (data.get('content') or '').strip()
        grade, theme, genre, requirement = get_request_writing_context(data, title)
        if not content:
            return jsonify({'status': 'error', 'message': '请先写下作文正文'}), 400
        if has_deepseek_key():
            try:
                fallback = local_ai_tool_result('inline_comment', title, content)
                raw = call_deepseek([
                    {'role': 'system', 'content': JSON_RESULT_SYSTEM_PROMPT},
                    {'role': 'user', 'content': build_inline_comment_prompt(title, content, grade, theme, requirement)}
                ], temperature=0.3, max_tokens=2800)
                report = normalize_inline_comment_payload(extract_json_payload(raw, fallback), fallback, title, content)
                if report.get('annotations'):
                    return jsonify({'status': 'success', 'report': report, 'annotations': report.get('annotations', [])})
            except Exception:
                pass
        fallback = local_ai_tool_result('inline_comment', title, content)
        return jsonify({'status': 'success', 'report': fallback, 'annotations': fallback.get('annotations', [])})

    @app.route('/ocr', methods=['POST'])
    def ocr():
        return handle_ocr_upload()
