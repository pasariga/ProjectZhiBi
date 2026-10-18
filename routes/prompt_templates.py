import re
import secrets


DEFAULT_TARGET_WORD_COUNT = 800
FULL_TEXT_MIN_WORD_COUNT = 800
FULL_TEXT_MAX_WORD_COUNT = 900
FULL_TEXT_IDEAL_WORD_COUNT = 850


COMMON_TEACHER_ROLE = (
    '你是一名经验丰富的中小学作文指导老师，擅长作文构思、素材积累、语言润色、'
    '结构优化和逐句批改。你的反馈要适合学生理解，表达清楚、具体、有帮助，'
    '避免空泛评价。不要出现“作为一个 AI”“以下是我为你生成的”等废话；'
    '不要生成过度成人化、过度华丽、脱离学生水平的内容；不要输出无关内容；'
    '不要输出 Markdown 代码块；不要出现测试、demo、mock、debug 等字样。'
)

TEXT_RESULT_SYSTEM_PROMPT = (
    COMMON_TEACHER_ROLE
    + '文本类结果可以用清楚的中文小标题组织，但要少解释、多给可直接使用的内容。'
)

JSON_RESULT_SYSTEM_PROMPT = (
    COMMON_TEACHER_ROLE
    + '结构化功能必须只返回严格 JSON，不要混入解释文字，不要使用 Markdown 包裹。'
)

NDJSON_RESULT_SYSTEM_PROMPT = (
    COMMON_TEACHER_ROLE
    + '你只能逐行输出合法 JSON 事件；每一行都是一个独立 JSON 对象，不要输出 Markdown 或解释文字。'
)


def _value(value, fallback='未填写'):
    text = (value or '').strip()
    return text if text else fallback


def _input_block(title='', content='', tool='', grade='', theme='', requirement=''):
    return (
        f'当前功能：{_value(tool)}\n'
        f'用户年级：{_value(grade, "中小学")}\n'
        f'写作主题：{_value(theme, "请根据题目和正文判断")}\n'
        f'写作要求：{_value(requirement, "按普通作文处理")}\n'
        f'作文题目：{_value(title)}\n'
        f'作文正文：{_value(content)}'
    )


def _essay_char_count(content=''):
    compact = re.sub(r'\s+', '', content or '')
    chinese_count = len(re.findall(r'[\u4e00-\u9fff]', compact))
    return chinese_count or len(compact)


def build_outline_prompt(title='', content='', grade='', theme='', requirement=''):
    return f'''{_input_block(title, content, '生成提纲', grade, theme, requirement)}

任务：生成一份清晰、可直接用于写作的作文提纲。

请严格按下面结构输出：
标题建议：
中心立意：
开头思路：
正文安排：
1. 第一段：
2. 第二段：
3. 第三段：
结尾思路：
可用素材：
写作提醒：

生成要求：
1. 提纲要围绕作文题目；如果题目为空但正文存在，请根据正文判断主题。
2. 中心立意要明确，段落安排要清楚。
3. 不要生成完整作文。
4. 每一点要具体，不要写“内容要充实”“语言要优美”这类空话。
5. 提纲应能支撑一篇约 {DEFAULT_TARGET_WORD_COUNT} 字的完整作文，主体层次和素材安排不能过于单薄。
6. 适合学生照着写。'''


def build_opening_prompt(title='', content='', grade='', theme='', requirement=''):
    return f'''{_input_block(title, content, '生成开头', grade, theme, requirement)}

任务：生成 2 到 3 个作文开头，供学生选择。

请严格按下面结构输出：
开头一：
特点：

开头二：
特点：

开头三：
特点：

生成要求：
1. 每个开头 80 到 120 字左右。
2. 不要太夸张，不要堆砌辞藻。
3. 开头要自然引出主题。
4. 三个开头风格尽量不同，例如叙事式、描写式、点题式。
5. 不要生成完整作文。
6. 开头要适合承接一篇约 {DEFAULT_TARGET_WORD_COUNT} 字的完整作文，不要在开头提前写完全部内容。
7. 不要写成模板化万能开头。'''


def build_detail_expand_prompt(title='', content='', grade='', theme='', requirement=''):
    return f'''{_input_block(title, content, '细节扩写', grade, theme, requirement)}

任务：从现有正文中找出最值得展开的一处关键场景，把概括叙述扩写成具体、自然、可直接放回作文的片段。

输出限制：
只输出扩写后的正文片段，不要输出标题、分析、说明、修改理由或“扩写结果”等提示语。

生成要求：
1. 严格围绕当前题目、主题、文体和写作要求，不得改换中心或另编无关事件。
2. 保留原文中的人物关系、时间、地点和事件逻辑；原文没有提供的关键事实不要随意杜撰。
3. 优先补充动作、神态、对话、环境和真实心理变化，让读者看见事情怎样发生。
4. 扩写必须推动情节或支撑中心，不能堆砌形容词，也不能为了变长而重复原意。
5. 保留学生原有语气和年龄感，不要写得过度成人化或过度华丽。
6. 输出 180 到 320 字左右的一个或两个自然段，并与原文上下文自然衔接。
7. 如果正文中有多处可以扩写，只选择最能支撑中心的一处。'''


def build_full_text_prompt(title='', content='', grade='', theme='', requirement=''):
    variation_hint = secrets.choice([
        '优先从校园任务、同伴协作或公共规则中选择具体事件。',
        '优先从一次亲手解决问题、完成实践或承担后果的经历切入。',
        '优先选择有明确时间压力、选择冲突和行动变化的真实小事。',
        '优先从劳动、运动、社团、课堂实践或社区生活中寻找情节。',
        '采用不同于常见亲情回忆文的场景，以人物行动推动主题。',
    ])
    input_block = _input_block(title, content, '生成全文', grade, theme, requirement).replace(
        '作文正文：',
        '参考正文（仅在与当前题目一致时沿用；冲突时视为旧稿并忽略）：'
    )
    if requirement:
        variation_hint = '严格执行用户填写的主题、文体与写作要求，不擅自更换题材、中心或表达方式。'
    return f'''{input_block}

任务：根据题目和要求生成一篇完整作文。

本次构思方向：{variation_hint}

输出限制：
只输出作文正文，不要输出解释，不要输出“以下是作文”。

生成要求：
1. 文章要有标题；用户填写了作文题目时，输出第一行必须原样使用该题目，不得改题、另拟题目或添加副标题。
2. 内容完整，有开头、正文和结尾。
3. 中心明确，结构清楚。
4. 语言自然，适合学生作文。
5. 不要过度华丽，不要像套模板。
6. 作文题目、写作主题、指定文体和写作要求属于最高优先级约束。生成前必须逐项核对，正文必须直接回应题目中的关键词，并使用指定文体；不得把“责任、亲情、校园、科技”等主题替换成泛泛的“成长”。
7. 生成全文统一以 {FULL_TEXT_IDEAL_WORD_COUNT} 字左右为目标。正文（不含标题）不得少于 {FULL_TEXT_MIN_WORD_COUNT} 字，原则上不得超过 {FULL_TEXT_MAX_WORD_COUNT} 字；即使用户没有填写额外要求，也必须遵守这一篇幅，不得只生成四五百字的短文。
8. 写作前先在内部构思至少 3 个不同的核心事件，比较后选择最贴合题目且最不套路的一个；不要输出构思过程。
9. 题目是“责任、梦想、成长、坚持”等抽象主题时，必须用一个清楚的具体事件承载主题，写明起因、困难或冲突、关键行动、结果与认识，不能只堆道理。
10. 除非题目、正文或写作要求明确出现外婆、奶奶、爷爷、菜园、浇水、病倒等信息，否则禁止主动使用这些人物和情节，也不要默认写长辈用一句话教育“我”。
11. 避免高频模板：不要使用“小时候我觉得这个词很遥远，直到那件事发生”“长辈的话像一颗种子种在心里”“从那以后我每天照顾菜园”等现成叙事骨架。
12. 同一题目重新生成时，应该更换核心事件、主要场景、关键人物关系、矛盾与转折方式，不能只替换少量词语或重复相同段落结构。
13. 只有当正文与当前题目、主题或要求一致时，才优先沿用用户明确提供的人物和事件；两者冲突时不得被旧正文带偏，也不得继续复用外婆、菜园、考试失利等旧稿情节。
14. 开头直接进入具体场景或行动，结尾从本次事件自然得出认识；少用万能抒情句和空泛升华。
15. 细节必须服务当前事件，包括可观察的动作、对话、环境变化和真实心理转折；不要为了凑字数重复议论。
16. 输出前在内部检查正文篇幅：不足 {FULL_TEXT_MIN_WORD_COUNT} 字时补充与核心事件直接相关的过程、动作、对话、心理和环境细节；超过 {FULL_TEXT_MAX_WORD_COUNT} 字时删去重复议论和无效铺陈；不要输出检查过程或字数说明。
17. 输出前再做一次主题一致性检查：标题、中心事件、每个主体段和结尾必须共同服务用户填写的主题；若发现偏离，必须先在内部重写后再输出。
18. 严格执行文体结构：记叙文写清人物、事件发展和心理变化；议论文提出中心论点并分层论证；说明文围绕说明对象写清概念、特点、原理、作用和注意事项；应用文必须符合书信、通知、倡议书、演讲稿等对应格式与交际目的。不得把所有文体都写成成长故事。
19. 把“写作要求”逐项视为硬约束。若要求使用第一或第三人称、加入对话/动作/心理/环境描写、使用比喻或排比、列出分论点、举出具体事例、首尾呼应，正文中必须出现可核验的对应内容；不得只在开头口头回应要求。
20. 输出前在内部建立“题目—主题—文体—具体要求”核对表，逐项检查通过后再输出正文。不要输出核对表；任一项不满足时先重写相关段落，不能用与题目无关的通用段落补足字数。'''


def build_full_text_extension_prompt(title='', draft='', grade='', theme='', requirement=''):
    current_count = len(re.findall(r'[\u4e00-\u9fff]', draft or '')) or len(re.sub(r'\s+', '', draft or ''))
    return f'''{_input_block(title, draft, '补足完整作文篇幅', grade, theme, requirement)}

任务：当前完整作文只有约 {current_count} 字，请在不改变题目、主题、文体、人物关系和核心事件的前提下，补写与原文自然衔接的正文段落，使全文达到 {FULL_TEXT_MIN_WORD_COUNT} 到 {FULL_TEXT_MAX_WORD_COUNT} 字。

要求：
1. 只输出需要追加的自然段，不要重复标题，不要重写或复述已有段落。
2. 优先补充事件过程、动作、对话、心理变化和环境细节，让新增内容推动情节或深化中心。
3. 新增内容必须承接原文最后一段，并保持学生原有语言水平。
4. 不要使用“补充内容”“续写如下”等说明，不要输出字数统计。
5. 补足后全文应不少于 {FULL_TEXT_MIN_WORD_COUNT} 字，并尽量控制在 {FULL_TEXT_IDEAL_WORD_COUNT} 字左右，绝不超过 {FULL_TEXT_MAX_WORD_COUNT} 字。
6. 按原文文体补写：记叙文补事件细节与心理转折；议论文补论据、分析或反方辨析；说明文补特点、原理、例子或注意事项；应用文补必要事项、行动要求或规范结语。禁止用通用成长感悟给其他文体凑字数。'''


def build_polish_prompt(title='', content='', grade='', theme='', requirement=''):
    return f'''{_input_block(title, content, '语句润色', grade, theme, requirement)}

任务：在不改变原意的基础上，优化语言表达。

请严格按下面结构输出：
润色结果：
修改说明：
保留优点：

生成要求：
1. 不改变文章中心和主要情节。
2. 保留学生原本文风，不要改得过度成熟。
3. 优化病句、重复、啰嗦和表达不自然的地方。
4. 适当提升文采，但不要堆砌辞藻。
5. 如果只润色部分内容，也要保持上下文自然。
6. 不要为了润色而大幅删减内容；原文接近 {DEFAULT_TARGET_WORD_COUNT} 字时，润色后应保持完整篇幅，避免压缩成短文。
7. 输出内容要方便用户复制或替换正文。'''


def build_structure_prompt(title='', content='', grade='', theme='', requirement=''):
    return f'''{_input_block(title, content, '结构梳理', grade, theme, requirement)}

任务：提炼这篇作文的结构框架，帮助学生看懂文章是怎样组织的。

必须只返回严格 JSON，不要返回普通文本，不要 Markdown，不要代码块。

返回格式必须为：
{{
  "structure_type": "递进式记叙结构",
  "theme": "责任",
  "genre": "记叙文",
  "nodes": [
    {{"label": "开头", "summary": "点明责任由陌生到理解"}},
    {{"label": "发展", "summary": "雨天放学等待，引出事件背景"}},
    {{"label": "转折", "summary": "看见同学保护鸟窝"}},
    {{"label": "深化", "summary": "理解责任也可以是守护小事"}},
    {{"label": "行动", "summary": "主动承担，学会照顾与帮助"}},
    {{"label": "结尾", "summary": "回扣责任主题，完成成长升华"}}
  ],
  "comment": "本文采用递进式记叙结构，从不了解责任写到看见责任，再写到主动承担责任，层层推进，适合表现成长主题。"
}}

生成要求：
1. 必须结合用户指定文体和正文自动判断结构类型：记叙文使用“开头—发展—转折—深化—行动—结尾”；议论文使用“引论—分层论证—结论”；说明文使用“引入对象—概念—特点—原理—应用—总结”；应用文使用“标题称谓—背景目的—主体事项—行动要求—结语落款”。
2. 只能提炼结构作用，不能复制原文段落，不能把原文截断塞进节点。
3. nodes 每项 summary 控制在 10 到 24 个汉字左右，必须像提纲节点。
4. 记叙文节点优先使用：开头、发展、转折、深化、行动、结尾。
5. 议论文节点优先使用：引论、本论一、本论二、本论三、结论。
6. comment 控制在 60 到 90 字，说明文章如何推进和升华。
7. 按约 {DEFAULT_TARGET_WORD_COUNT} 字完整作文的标准审视结构，指出哪些主体层次需要补足，但节点仍要保持简短。
8. 如果主题不明确，请从题目和正文推断一个简短主题。
9. genre 必须返回实际采用的文体；nodes 必须与该文体的结构规则一致，不能把说明文或应用文套成记叙文。'''


def build_continue_prompt(title='', content='', grade='', theme='', requirement=''):
    current_count = _essay_char_count(content)
    return f'''{_input_block(title, content, '续写作文', grade, theme, requirement)}

任务：承接已有正文继续往下写。

生成要求：
1. 不要重复已有正文。
2. 延续原文中心、人物和语气。
3. 补充具体情节、动作或心理变化。
4. 当前正文约 {current_count} 字，默认完整作文目标为 {DEFAULT_TARGET_WORD_COUNT} 字。
5. 如果当前不足 {DEFAULT_TARGET_WORD_COUNT} 字，续写篇幅应根据当前差距动态决定，重点补充情节、动作、心理和结尾，使全文自然接近目标；不要机械凑字。
6. 如果当前已经达到目标，只补充必要的收束内容，不重复扩写。
7. 只输出续写内容。'''


def build_ending_prompt(title='', content='', grade='', theme='', requirement=''):
    return f'''{_input_block(title, content, '生成结尾', grade, theme, requirement)}

任务：生成 1 个能承接全文的作文结尾。

生成要求：
1. 80 到 150 字左右。
2. 要自然收束全文，回扣题目或中心。
3. 不要喊口号，不要过度拔高。
4. 结尾应适合约 {DEFAULT_TARGET_WORD_COUNT} 字完整作文的收束节奏，避免过于仓促。
5. 只输出结尾正文。'''


def build_correction_prompt(title='', content='', grade='', theme='', requirement=''):
    return f'''{_input_block(title, content, '智能批改', grade, theme, requirement)}

任务：对作文进行综合批改，必须返回严格 JSON，不要返回普通文本。

返回格式必须为：
{{
  "type": "correction",
  "scores": {{
    "content": 0,
    "structure": 0,
    "language": 0,
    "rhetoric": 0,
    "standard": 0
  }},
  "summary": "",
  "suggestions": [
    {{
      "type": "",
      "original": "",
      "advice": "",
      "reason": ""
    }}
  ],
  "optimized_text": ""
}}

评分要求：
1. 五个维度分数范围 0 到 100。
2. content：是否切题、主题是否明确、内容是否充实，具体事例和细节能否支撑中心。
3. structure：段落是否清楚、过渡是否自然、结尾是否点题。
4. language：句子是否通顺、表达是否自然、是否啰嗦。
5. rhetoric：是否有恰当修辞、描写是否生动。
6. standard：错别字、标点、格式、书面表达。
7. 具体事例、生活细节和内容饱满度统一计入 content，不单独设置素材分。
8. 只返回五个维度分，不要返回总分，不要返回 material 字段。
9. 分数必须结合原文真实质量判断，不能给保险分、平均分、模板分，也不要照抄示例数字。
10. 五个维度应有自然差异，不要全部集中在 80 到 85。
11. 默认作文目标为 {DEFAULT_TARGET_WORD_COUNT} 字：低于 400 字视为内容偏短；400-749 字已有基本内容但仍需扩写；750-850 字视为接近目标且完成度较高；850 字以上不因篇幅扣分，但要关注详略和表达精炼。

评分区间参考：
- 90-100：主题鲜明，内容充实，结构完整，语言生动，几乎无明显问题。
- 80-89：整体较好，但细节、语言或结构仍有可优化处。
- 70-79：基本完成作文要求，但内容偏薄、结构一般或语言较平。
- 60-69：能看出主题，但内容空泛、结构松散、语病较多。
- 60 以下：明显偏题、字数严重不足、结构混乱或表达不完整。

建议要求：
1. suggestions 至少返回 3 条，最多 6 条。
2. 每条建议必须具体。
3. original 要尽量引用原文中的真实句子。
4. advice 要给出可执行的修改方式。
5. reason 要说明为什么这样改。
6. 不要只写“语言需要优化”“结构需要加强”这种空话。'''


def build_correction_stream_prompt(title='', content='', grade='', theme='', requirement=''):
    return f'''{_input_block(title, content, '智能批改', grade, theme, requirement)}

任务：批改下面这篇作文，并按 NDJSON 格式逐行输出事件。

输出要求：
1. 每一行必须是一个独立 JSON 对象，不要 Markdown，不要代码块，不要解释。
2. 先依次输出五个维度分数事件，value 必须是根据实际作文质量判断出的 0-100 整数，下面只展示字段格式，不代表推荐分数，禁止照抄示例值：
{{"type":"score","key":"content","label":"内容","value":"根据实际作文质量给出0-100整数"}}
{{"type":"score","key":"structure","label":"结构","value":"根据实际作文质量给出0-100整数"}}
{{"type":"score","key":"language","label":"语言","value":"根据实际作文质量给出0-100整数"}}
{{"type":"score","key":"rhetoric","label":"修辞","value":"根据实际作文质量给出0-100整数"}}
{{"type":"score","key":"standard","label":"规范","value":"根据实际作文质量给出0-100整数"}}
3. 再输出一条总评事件：
{{"type":"summary","content":"文章主题明确，情感真实，但部分语句还可以更具体。"}}
4. 再输出 3 到 6 条修改建议事件：
{{"type":"suggestion","data":{{"type":"语言问题","original":"原句","advice":"修改建议","reason":"修改理由"}}}}
5. 本功能只需要诊断和简短建议，不要重写整篇作文，也不要输出 optimized_text 事件。
6. 最后一行输出：
{{"type":"final","message":"批改完成"}}
7. 不要输出总分，系统会根据五个维度分数按固定公式计算总分。
8. 不要输出 inline_comments，逐句旁批只属于智能旁批功能。
9. 不要给保险分、平均分、模板分；如果作文很短、空泛或结构混乱，应明显低于 80。
10. 五个维度必须有自然差异，不要全部集中在 80-85，也不要五项完全相同；不要输出 material 字段。
11. 默认作文目标为 {DEFAULT_TARGET_WORD_COUNT} 字：低于 400 字视为内容偏短；400-749 字已有基本内容但仍需扩写；750-850 字视为接近目标且完成度较高；850 字以上不因篇幅扣分，但要关注详略和表达精炼。

评分区间参考：
- 90-100：主题鲜明，内容充实，结构完整，语言生动，几乎无明显问题。
- 80-89：整体较好，但细节、语言或结构仍有可优化处。
- 70-79：基本完成作文要求，但内容偏薄、结构一般或语言较平。
- 60-69：能看出主题，但内容空泛、结构松散、语病较多。
- 60 以下：明显偏题、字数严重不足、结构混乱或表达不完整。

评分维度：
内容：是否切题、主题是否明确、内容是否充实，具体事例和细节能否支撑中心。
结构：段落是否清楚、过渡是否自然、结尾是否点题。
语言：句子是否通顺、表达是否自然、是否啰嗦。
修辞：是否有恰当修辞、描写是否生动。
规范：错别字、标点、格式、书面表达。'''


def build_inline_comment_prompt(title='', content='', grade='', theme='', requirement=''):
    return f'''{_input_block(title, content, '智能旁批', grade, theme, requirement)}

任务：生成作文批改报告式智能旁批，必须返回严格 JSON，方便前端定位句子、编号标注和渲染报告。

返回格式必须为：
{{
  "type": "annotation_report",
  "title": "作文题目",
  "word_count": 0,
  "summary": {{
    "total": 0,
    "highlight": 0,
    "suggestion": 0,
    "correction": 0,
    "structure": 0,
    "rewrite": 0
  }},
  "annotations": [
    {{
      "id": 1,
      "type": "highlight",
      "type_name": "亮点句子",
      "sentence_index": 1,
      "quote": "",
      "problem": "",
      "comment": "",
      "suggestion": "",
      "rewrite": "",
      "dimension": "语言表达",
      "severity": "low"
    }}
  ],
  "inline_comments": [],
  "overall": {{
    "strengths": "",
    "main_issues": "",
    "next_steps": "",
    "upgraded_example": ""
  }}
}}

要求：
1. 只返回 JSON，不要 Markdown，不要代码块，不要解释。
2. 每条 annotations 都必须定位到原文中的具体句子，sentence_index 使用从 1 开始的句子编号。
3. quote 必须尽量和原文句子一致；如果只点评短语，也要保留所在原句。
4. type 只能使用：highlight、suggestion、correction、structure、rewrite。
5. type_name 对应为：亮点句子、建议优化、字词纠错、结构提醒、升格改写。
6. 每条旁批必须包含：原句 quote、类型 type/type_name、点评 comment、问题说明 problem、修改建议 suggestion、示范改写 rewrite。
7. 批注数量必须充实，不要只给 3-4 条批注。默认完整作文目标为 {DEFAULT_TARGET_WORD_COUNT} 字，数量范围：
   - 100 字以下：2-4 条；
   - 100-400 字：4-7 条；
   - 400-749 字：8-12 条；
   - 750-850 字：12-16 条；
   - 超过 850 字：14-20 条。
   接近 {DEFAULT_TARGET_WORD_COUNT} 字的完整作文建议生成 12-16 条批注。
8. 类型分布要求：亮点句子至少 2 条，建议优化至少 3 条，结构提醒至少 1 条，升格改写至少 1 条；没有明显错别字时，字词纠错可以为 0。
9. 优先选择：开头句、主题句、关键情节句、有细节描写的句子、表达重复或空泛的句子、转折句、结尾升华句、明显语病或错别字位置、可以升格的句子。
10. 禁止空泛评价，不要只写“写得不错”“可以更生动”，必须说明为什么好、哪里有问题、怎么改。
11. 必须先按原文换行逐段检查，再生成批注；每个主要自然段至少返回 1 条有效批注，不能只检查开头和结尾。
12. 批注位置必须覆盖开头、中段和结尾。完整作文至少一半批注应落在中间主体段，且中段应尽量同时包含 highlight、suggestion、structure、rewrite 四类批注。
13. 相邻批注应优先选择不同段落、不同句子和不同问题；除非确有两个完全不同的问题，否则不要对同一句重复批注，也不要连续点评同一段。
14. 输出前自行检查 sentence_index 的分布：若中段数量不足、主要段落遗漏或位置重复，先补齐并去重后再返回。
15. inline_comments 可以复制 annotations，用于兼容旧前端字段。'''


def build_messages(prompt, system_prompt=TEXT_RESULT_SYSTEM_PROMPT):
    return [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': prompt},
    ]
