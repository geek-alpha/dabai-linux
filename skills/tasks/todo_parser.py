"""自然语言任务解析器 —— 把用户的复杂任务描述解析成可执行的子任务计划。

能力（纯规则实现，零外部依赖，便于扩展）：
1. 分段：按序数/序号/序列词（先…再…然后…最后…）切出「关键任务 + 子任务」；
2. 优先级：识别 加急/紧急/重要/尽快 → 高；不急/顺便/有空 → 低；默认中；
3. 截止时间：解析 今天/明天/后天/周X/下周X/X月X日/月底/明天18点/YYYY-MM-DD 等；
4. 提醒：识别 在X点提醒/每天X点提醒/每周X提醒/每隔N小时(N天) 等；
5. 依赖：序数顺序 + 先…后…/等…完成…后/依赖 等措辞，生成依赖边并拓扑排序。

模块入口：decompose(text) → 计划字典（可直接交给 service.import_plan 落库）。
"""
from __future__ import annotations

import re
import time
import datetime as _dt

# ---------------- 优先级关键词 ----------------

_HIGH_PRIORITY_WORDS = (
    '加急', '紧急', '重要', '优先', '必须', '马上', '立刻', '尽快',
    '高优', '火速', 'critical', 'urgent', 'high',
)
_LOW_PRIORITY_WORDS = (
    '不急', '顺便', '有空', '低优', '不着急', '慢慢来', '不太重要',
    '有时间再说', 'low', 'later',
)

# ---------------- 序列 / 序数关键词 ----------------

_SEQUENCE_WORDS = ('首先', '第一', '先', '再者', '接着', '其次', '然后',
                   '再', '随后', '接下来', '之后', '最后')

_NUMBERED_ITEM_RE = re.compile(
    r'^\s*(?:(\d+)[\.\、)）]|([一二三四五六七八九十]+)[、\.\)]|(第一步|步骤[一二三四五六七八九十\d]+|第\d步))\s*')

# 行内编号分隔符（用于把 "1. 整理需求 2. 画架构图" 拆成多条子任务）
_NUMBERED_LINE_RE = re.compile(r'(?<!\d)(?:(\d+)[\.\、)）]|([一二三四五六七八九十]+)[、\.\)])(?!\d)')

_SEQUENCE_SPLIT_RE = re.compile(
    r'(先(?:要|去|做|把|整理|安排|准备|收集|准备好|准备好)?|首先|接着|其次|然后|再(?:来|去|做)?|随后|接下来|之后|最后)')


def _split_numbered_lines(sent: str) -> list:
    """把一句内连续编号的文本切成多条（"1. A 2. B 3. C" → ['1. A', '2. B', '3. C']）。

    编号与其后续文本合并为一项（编号前缀交给 _NUMBERED_ITEM_RE / _strip_marker 解析）。
    带 (?:数字.数字) 的版本号、小数不受影响（前后数字断言保护）。
    """
    items = []
    current = []
    pos = 0
    for m in _NUMBERED_LINE_RE.finditer(sent):
        head = sent[pos:m.start()]
        if current:
            current.append(head)
            items.append(' '.join(current))
            current = []
        current.append(sent[m.start():m.end()])
        pos = m.end()
    tail = sent[pos:]
    if current:
        if tail.strip():
            current.append(tail)
        items.append(' '.join(current))
    elif tail.strip():
        items.append(tail)
    return [i.strip() for i in items if i.strip()]

# ---------------- 截止时间表达 ----------------

_WEEKDAYS = {'周一': 0, '星期一': 0, '礼拜一': 0,
             '周二': 1, '星期二': 1, '礼拜二': 1,
             '周三': 2, '星期三': 2, '礼拜三': 2,
             '周四': 3, '星期四': 3, '礼拜四': 3,
             '周五': 4, '星期五': 4, '礼拜五': 4,
             '周六': 5, '星期六': 5, '礼拜六': 5,
             '周日': 6, '星期日': 6, '星期天': 6, '礼拜天': 6,
             '周天': 6}

_TIME_RE_1 = re.compile(r'(\d{1,2})[:：](\d{1,2})')
_TIME_RE_2 = re.compile(r'(上午|中午|下午|晚上|傍晚|凌晨)?\s*(\d{1,2})\s*[点时](?:(\d{1,2})\s*分|半)?')
_MONTH_DAY_RE = re.compile(r'(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]')
_ISO_DATE_RE = re.compile(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日]?')
_MMDD_DATE_RE = re.compile(r'(?<!\d)(\d{1,2})[-/](\d{1,2})(?!\d)')

_DEADLINE_WORDS = ('截止', '前完成', '之前', '前', 'deadline', 'due', '前交付', '前提交', '最后期限')

_DEFAULT_HOUR = 18
_DEFAULT_MINUTE = 0

# ---------------- 提醒表达 ----------------

_DAILY_REMIND_RE = re.compile(r'(每天|每日)\s*(?:早上|上午|中午|下午|晚上)?\s*(?:(\d{1,2})[:：](\d{1,2})|(\d{1,2})\s*点(?:半|(\d{1,2})\s*分)?)\s*(?:提醒|叫|通知)')
_WEEKLY_REMIND_RE = re.compile(r'每周(?:的)?(?:(周|星期|礼拜)([一二三四五六日天])|([一二三四五六日天]))?\s*(?:上午|中午|下午|晚上)?\s*(\d{1,2})\s*[点时]\s*(?:提醒|叫|通知)')
_INTERVAL_REMIND_RE = re.compile(r'每隔\s*(\d+)\s*(小时|分钟|天|周)\s*(?:提醒|叫|通知)')
_ONCE_REMIND_RE = re.compile(r'提醒\s*我?\s*(?:在|于|到时间)?\s*((?:今天|明天|后天|下周|周[一二三四五六日天]|星期[一二三四五六日天]|\d{1,2}月\d{1,2}[日号])?\s*(?:上午|中午|下午|晚上)?\s*\d{1,2}\s*[点时](?:\d{1,2}\s*分)?)')

_REMIND_REPEAT_WORDS = ('每天', '每日', '每周', '每周', '每星期', '每小时')


def parse_priority(text) -> str | None:
    """从文本中识别优先级关键词，返回 'high'/'medium'/'low'，未命中返回 None。"""
    low = str(text or '').lower()
    if any(w in low for w in _HIGH_PRIORITY_WORDS):
        return 'high'
    if any(w in low for w in _LOW_PRIORITY_WORDS):
        return 'low'
    return None


# ---------------- 时间解析 ----------------

def parse_hour_minute(text) -> dict | None:
    """从文本中解析一个时刻，返回 {'hour', 'minute'}（可选 'next_day'）；解析不了返回 None。

    修复说明：
    - 旧实现引用了 _TIME_RE_2 中不存在的第 4 个捕获组（'半' 不是捕获组），
      所有 "X点/X点半" 表达都会 IndexError 崩溃 —— 现已改为检查匹配串本身；
    - 时制语义：晚上/傍晚 12 点 = 次日凌晨 0 点（附 next_day 标记），凌晨 12 点 = 0 点。
    """
    m = _TIME_RE_1.search(text)
    if m:
        return {'hour': _clamp(int(m.group(1)), 0, 23), 'minute': _clamp(int(m.group(2)), 0, 59)}
    m = _TIME_RE_2.search(text)
    if m:
        hour = int(m.group(2))
        minute = _clamp(int(m.group(3)), 0, 59) if m.group(3) else (30 if '半' in m.group(0) else 0)
        period = m.group(1)
        next_day = False
        if period == '下午' or period == '晚上' or period == '傍晚':
            if hour < 12:
                hour += 12
            elif hour == 12 and period in ('晚上', '傍晚'):
                hour, minute, next_day = 0, 0, True
        elif period == '中午' and hour < 12:
            hour += 12
        elif period == '凌晨' and hour == 12:
            hour, minute = 0, 0
        out = {'hour': _clamp(hour, 0, 23), 'minute': _clamp(minute, 0, 59)}
        if next_day:
            out['next_day'] = True
        return out
    return None


def _clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _today() -> tuple:
    lt = time.localtime()
    return (lt.tm_year, lt.tm_mon, lt.tm_mday)


def _ts(y, m, d, hour=_DEFAULT_HOUR, minute=_DEFAULT_MINUTE) -> float:
    return time.mktime((y, m, d, hour, minute, 0, 0, 0, -1))


def _parse_date_expr(text) -> tuple | None:
    """解析日期表达，返回 (年, 月, 日)；解析不了返回 None。"""
    t = str(text or '').strip()
    now = _dt.date.today()
    # 今天/明天/后天/大后天/下周
    if '大后天' in t:
        return tuple((now + _dt.timedelta(days=3)).timetuple()[:3])
    if '后天' in t:
        return tuple((now + _dt.timedelta(days=2)).timetuple()[:3])
    if '明天' in t or '次日' in t:
        return tuple((now + _dt.timedelta(days=1)).timetuple()[:3])
    if '今天' in t or '今日' in t or '今晚' in t or '当天' in t:
        return tuple(now.timetuple()[:3])
    if '月底' in t or '月末' in t:
        import calendar
        return (now.year, now.month, calendar.monthrange(now.year, now.month)[1])
    # 周X / 星期X（含下周）：目标星期 == 今天时一律取 7 天后
    # （修复：旧实现 "下周六" 在今天正好周六时返回今天）
    for wd_name, target in _WEEKDAYS.items():
        if wd_name in t:
            delta = (target - now.weekday()) % 7
            if delta == 0:
                delta = 7
            d = now + _dt.timedelta(days=delta)
            return (d.year, d.month, d.day)
    # X月X日
    m = _MONTH_DAY_RE.search(t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year
        if month < now.month or (month == now.month and day < now.day):
            year += 1
        return (year, month, day)
    # YYYY-MM-DD / YYYY年M月D日
    m = _ISO_DATE_RE.search(t)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # MM-DD
    m = _MMDD_DATE_RE.search(t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            year = now.year
            if month < now.month or (month == now.month and day < now.day):
                year += 1
            return (year, month, day)
    return None


def _merge_date_time(date_tuple, time_dict):
    if date_tuple is None:
        date_tuple = _today()
    hour = time_dict['hour'] if time_dict else _DEFAULT_HOUR
    minute = time_dict['minute'] if time_dict else _DEFAULT_MINUTE
    return (date_tuple[0], date_tuple[1], date_tuple[2], hour, minute)


def parse_datetime(text) -> dict | None:
    """从文本解析出一个「日期+时刻」，返回 {'text', 'ts'}。

    只含时间时：默认是今天，若已过则顺延到明天。
    只含日期时：默认为当天 18:00。
    """
    t = str(text or '').strip()
    if not t:
        return None
    date_tuple = _parse_date_expr(t)
    time_dict = parse_hour_minute(t)
    if date_tuple is None and time_dict is None:
        return None
    y, m, d, hour, minute = _merge_date_time(date_tuple, time_dict)
    ts = _ts(y, m, d, hour, minute)
    # 晚上/傍晚 12 点 = 次日凌晨 0 点（如 "明天晚上12点" → 后天 0 点）
    if time_dict and time_dict.get('next_day'):
        ts += 86400
    now = time.time()
    # 只写了时间（今天已过）→ 顺延明天
    if date_tuple is None and ts < now:
        tomo = _dt.date.today() + _dt.timedelta(days=1)
        ts = _ts(tomo.year, tomo.month, tomo.day, hour, minute)
    display = time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))
    return {'text': display, 'ts': ts}


def find_deadline(text) -> str | None:
    """识别文本中的截止时间表达（含 截止/之前 关键词），返回 ISO 显示串。

    修复说明：仅在截止关键词**之前**的窗口内解析时间，避免把
    "每天9点提醒"这类从句里的时间误作截止时刻（"下周一前完成，每天9点提醒"
    的截止应是下周一 18:00 而不是 09:00）。
    """
    t = str(text or '').strip()
    idx = -1
    for w in _DEADLINE_WORDS:
        i = t.lower().find(w)
        if i >= 0 and (idx < 0 or i < idx):
            idx = i
    if idx < 0:
        return None
    window = t[max(0, idx - 40):idx + 1]
    r = parse_datetime(window)
    return r['text'] if r else None


# ---------------- 提醒解析 ----------------

def parse_reminders(text) -> list:
    """从文本解析提醒信息，返回提醒字典列表（每条含 at/type/repeat/repeat_every）。"""
    t = str(text or '').strip()
    if not t:
        return []
    out = []
    m = _INTERVAL_REMIND_RE.search(t)
    if m:
        out.append(_interval_reminder(int(m.group(1)), m.group(2), t))
        return out
    m = _DAILY_REMIND_RE.search(t)
    if m:
        hm = _daily_hourmin(m)
        if hm:
            out.append(_repeat_reminder_at(hm, 'daily'))
            return out
    m = _WEEKLY_REMIND_RE.search(t)
    if m:
        day_char = m.group(2) or m.group(3)
        if day_char:
            hm = {'hour': _clamp(int(m.group(4)), 0, 23), 'minute': 0}
            out.append(_weekly_reminder(day_char, hm))
            return out
    m = _ONCE_REMIND_RE.search(t)
    if m:
        dt = parse_datetime(m.group(1))
        if dt:
            out.append({'at': dt['text'], 'type': 'once', 'repeat': 'none',
                        'repeat_every': 1})
            return out
    # 兜底：存在提醒词 + 可解析时刻 → 一次性（含 每天/每周/每小时 等重复词时升级为重复）
    if '提醒' in t or '叫' in t or '记得' in t or '通知' in t:
        hm = parse_hour_minute(t)
        if hm:
            repeat = 'none'
            if any(w in t for w in ('每天', '每日')):
                repeat = 'daily'
            elif any(w in t for w in ('每周', '每星期')):
                repeat = 'weekly'
            if repeat != 'none':
                out.append(_repeat_reminder_at(hm, repeat))
            else:
                date_tuple = _parse_date_expr(t)
                y, m, d, hour, minute = _merge_date_time(date_tuple, hm)
                nxt = _ts(y, m, d, hour, minute)
                if nxt < time.time() and date_tuple is None:
                    tomo = _dt.date.today() + _dt.timedelta(days=1)
                    nxt = _ts(tomo.year, tomo.month, tomo.day, hour, minute)
                out.append({'at': time.strftime('%Y-%m-%d %H:%M', time.localtime(nxt)),
                            'type': 'once', 'repeat': 'none', 'repeat_every': 1})
        elif '每小时' in t or '每1小时' in t:
            # 无具体时刻的"每小时提醒" → 从 1 小时后开始重复
            nxt = _dt.datetime.now() + _dt.timedelta(hours=1)
            out.append({'at': time.strftime('%Y-%m-%d %H:%M', time.localtime(nxt.timestamp())),
                        'type': 'repeat', 'repeat': 'hourly', 'repeat_every': 1})
    return out


def _interval_reminder(n: int, unit: str, text: str) -> dict:
    """每隔 N 分钟/小时/天/周 → 重复提醒字典。

    修复说明：分钟级周期此前被 round 到不存在的颗粒度（"每隔30分钟"→每1小时、
    "每隔90分钟"→每2小时），现改为原生 minutely 周期（service 已支持）。
    """
    hm = parse_hour_minute(text)
    if unit == '分钟':
        repeat, every = 'minutely', n
    elif unit == '小时':
        repeat, every = 'hourly', n
    elif unit == '周':
        repeat, every = 'weekly', n
    else:
        repeat, every = 'daily', n
    ts_ref = _dt.datetime.now().replace(second=0, microsecond=0) + _dt.timedelta(hours=1)
    if hm:
        now = _dt.datetime.now()
        nxt = now.replace(hour=hm['hour'], minute=hm['minute'], second=0, microsecond=0)
        if nxt <= now:
            nxt = nxt + _dt.timedelta(days=1)
        ts_ref = nxt
    return {'at': time.strftime('%Y-%m-%d %H:%M', time.localtime(ts_ref.timestamp())),
            'type': 'repeat', 'repeat': repeat, 'repeat_every': every}


def _daily_hourmin(m) -> dict | None:
    """从『每天8:30』/『每天8点』正则匹配中取时分。"""
    hour = int(m.group(2)) if m.group(2) else int(m.group(4))
    minute = int(m.group(3) or 0) if m.group(2) else 0
    return {'hour': _clamp(hour, 0, 23), 'minute': _clamp(minute, 0, 59)}


_DAY_CHAR_TO_IDX = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4,
                     '六': 5, '日': 6, '天': 6}


def _weekly_reminder(day_char: str, hm: dict) -> dict:
    """『每周一9点提醒』 → 下一周同一时刻的重复提醒。"""
    target = _DAY_CHAR_TO_IDX.get(day_char)
    if target is None:
        target = 0
    today = _dt.date.today()
    delta = (target - today.weekday()) % 7
    if delta == 0:
        delta = 7
    nxt = today + _dt.timedelta(days=delta)
    return {'at': time.strftime('%Y-%m-%d %H:%M',
                                time.localtime(_ts(nxt.year, nxt.month, nxt.day,
                                                   hm['hour'], hm['minute']))),
            'type': 'repeat', 'repeat': 'weekly', 'repeat_every': 1}


def _repeat_reminder_at(hm: dict, repeat: str) -> dict:
    now = _dt.datetime.now()
    nxt = now.replace(hour=hm['hour'], minute=hm['minute'], second=0, microsecond=0)
    if nxt <= now:
        nxt = nxt + _dt.timedelta(days=1)
    return {'at': time.strftime('%Y-%m-%d %H:%M', time.localtime(nxt.timestamp())),
            'type': 'repeat', 'repeat': repeat, 'repeat_every': 1}


# ---------------- 提醒状语剥离 ----------------

_STRIP_REMINDER_PATTERNS = (
    _DAILY_REMIND_RE, _WEEKLY_REMIND_RE, _INTERVAL_REMIND_RE, _ONCE_REMIND_RE,
)

# 日期/时间短语（用于识别"纯截止状语"段，如 "下周一前完成"）
_DATE_PHRASE_RE = re.compile(
    r'(?:[今明后大]天|今晚|今日|当天|次日|'
    r'(?:大下周|下周|本周|上?周)[一二三四五六日天]?|'
    r'星期[一二三四五六日天]|礼拜[一二三四五六日天]|月底|月末|'
    r'\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?|\d{1,2}月\d{1,2}[日号]|\d{1,2}[-/]\d{1,2})'
    r'(?:上午|中午|下午|晚上|傍晚|凌晨)?\s*'
    r'(?:\d{1,2}[:：]\d{1,2}|\d{1,2}\s*点(?:\d{1,2}\s*分|半)?)?')


def _is_pure_deadline_clause(text) -> bool:
    """判断文本是否为"纯时间状语"（下周一前完成 / 明天18点前交付 / 明天18点）。

    若剥离日期时间短语与 前完成/之前/截止 等词后没有剩余实质内容 → True。
    这类段不构成子任务（截止/提醒语义由 find_deadline、parse_reminders 单独落到任务/子任务）。
    """
    t = str(text or '').strip()
    if not t:
        return True
    stripped = _DATE_PHRASE_RE.sub('', t)
    for kw in _DEADLINE_WORDS + ('前', '完成', '搞定', '交付', '提交', '每', '我'):
        stripped = stripped.replace(kw, '')
    rest = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', stripped)
    return not rest


def _strip_reminder_clause(text) -> str:
    """从任务描述中剔除提醒状语（"每天9点提醒" 等），仅保留任务本体。

    提醒语义已由 parse_reminders 单独提取，剥离后避免"每天9点提醒"
    被误切成一条子任务。返回空串表示整句都是提醒/时间状语。
    """
    t = str(text or '').strip()
    if not t:
        return t
    changed = True
    while changed:
        changed = False
        for pat in _STRIP_REMINDER_PATTERNS:
            m = pat.search(t)
            if m:
                t = (t[:m.start()] + ' ' + t[m.end():]).strip(' ，,、;；')
                changed = True
    # 兜底1：句尾孤立提醒词（"记得/提醒/通知" 且其后无内容，可带连词）
    m = re.search(r'(?:并|和|再|然后|接着)?\s*(?:提醒|叫|记得|通知)(?:我)?\s*(?:一下|我)?$', t)
    if m:
        t = t[:m.start()].strip(' ，,、;；')
    # 兜底2：行内 "X点提醒/提醒X点"（未命中上述正则的变体）
    m = re.search(r'(?:在|于)?\s*\d{1,2}\s*[点时](?:\d{1,2}\s*分|半)?\s*(?:提醒|叫|通知)(?:我)?', t)
    if m:
        t = (t[:m.start()] + ' ' + t[m.end():]).strip(' ，,、;；')
    return t


# ---------------- 分段（关键任务 + 子任务） ----------------

def _split_segments(text) -> list:
    """把复杂任务描述切成「主任务 + 有序子任务」。

    返回 [{'text': str, 'order': int | None}]，order 表示有序序号（从 1 开始）。
    切法优先级：
      1) 行/句号分号切句；
      2) 句子内含「先…然后…再…最后…」时按标点与序列词再切；
      3) 有序号（1. 一、 第一步）时按其归组并赋顺序；
      4) 多句且含序列词/多步骤 → 按自然顺序赋序（此时第一个无序号短句视为总目标）。
    """
    t = str(text or '').strip()
    if not t:
        return []
    # 按行/句号/分号切句
    rough = re.split(r'[。；;\n]', t)
    sentences = [s.strip() for s in rough if s.strip()]
    # 句子内含序列词的，用逗号再细分（先…再…然后…）；
    # 其余句子再按行内编号切分（"1. A 2. B" → 多条子任务）
    expanded = []
    for sent in sentences:
        if any(w in sent for w in _SEQUENCE_WORDS) and '，' in sent:
            pieces = re.split(r'[，,]', sent)
            expanded.extend(p.strip() for p in pieces if p.strip())
        else:
            expanded.extend(_split_numbered_lines(sent))
    # 识别每句序号：编号 / 序数 / 第N步 / 序列词开头
    numbered = []
    seq_index = 1
    has_markers = False
    for sent in expanded:
        m = _NUMBERED_ITEM_RE.search(sent)
        if m:
            num = int(m.group(1)) if m.group(1) else _cn_to_idx(m.group(2))
            numbered.append((sent, num))
            has_markers = True
            continue
        if any(sent.startswith(w) for w in ('首先', '第一', '先')):
            numbered.append((sent, 1))
            has_markers = True
            continue
        if any(sent.startswith(w) for w in ('接着', '其次', '然后', '再', '随后', '接下来', '之后')):
            numbered.append((sent, seq_index))
            seq_index += 1
            has_markers = True
            continue
        if any(sent.startswith(w) for w in ('最后', '总结', '回顾')):
            numbered.append((sent, 999))
            has_markers = True
            continue
        numbered.append((sent, None))

    if len(expanded) == 1:
        return [{'text': expanded[0], 'order': None}]
    if has_markers:
        # 有序归组：维护递增序号
        items = []
        last_order = 0
        first = numbered[0]
        if first[1] is None and len(numbered) > 1:
            items.append({'text': first[0], 'order': None})
            numbered = numbered[1:]
        for sent, num in numbered:
            if num is not None:
                last_order = num if num != 999 else last_order + 1
            else:
                last_order += 1
            items.append({'text': sent, 'order': last_order})
        return items
    # 多句无标记：整段都是子步骤（顺序执行）
    return [{'text': sent, 'order': i + 1} for i, sent in enumerate(expanded)]


def _cn_to_idx(cn) -> int:
    base = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
            '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    text = str(cn or '') or '1'
    if text in base:
        return base[text]
    if text.startswith('十'):
        return 10 + base.get(text[1:], 0)
    if text.endswith('十'):
        return base.get(text[0], 1) * 10
    return 1


# ---------------- 依赖识别 ----------------

def _detect_dependency(sent) -> str | None:
    """识别句子中的「依赖前置」表达，返回被依赖的子任务标题关键词。

    修复说明：旧正则把裸 '在' 当作依赖触发器（"现在..."、"住在..." 均可能误判），
    已移除，仅保留 等/完成/搞定/做完…(之后/后) 及显式 "在…之后" 结构。
    """
    m2 = re.search(r'(?:完成|搞定|做完)\s*(.*?)\s*(?:之后|以后|后)', sent)
    if m2 and m2.group(1):
        return m2.group(1).strip()
    m = re.search(r'(?:等|等完成|完成|等.*?完成后)\s*(.*?)\s*(?:之后|后再|完成后|后)?\s*(?:再|接着|然后|才能|即可|才可以|再去|继续|再继续)?', sent)
    if m and m.group(1):
        return m.group(1).strip()
    m3 = re.search(r'在\s*(.*?)\s*之后', sent)
    if m3 and m3.group(1):
        return m3.group(1).strip()
    return None


# ---------------- 主入口 ----------------

def decompose(text: str) -> dict:
    """把一句/一段自然语言任务描述解析成执行计划字典。

    返回结构：
        {
          'title': 主任务标题,
          'goal':  原始完整描述,
          'priority': 'high'|'medium'|'low',
          'due_date': ISO 截止（找到才给）,
          'reminders': 全局提醒列表,
          'subtasks': [ {'title','description','priority','due_date',
                         'reminder', 'depends_on':[索引]} ],
          'execution_order': [子任务索引的有序序列],
        }
    """
    raw = str(text or '').strip()
    if not raw:
        return {'title': '', 'goal': '', 'priority': 'medium', 'due_date': None,
                'reminders': [], 'subtasks': [], 'execution_order': []}

    segments = _split_segments(raw)
    goal_seg = None
    work_segs = []
    for seg in segments:
        if seg['order'] is None and not work_segs:
            goal_seg = seg['text']
        else:
            work_segs.append(seg)
    # 剥离提醒/时间状语（"每天9点提醒"/"下周一前完成" 等不构成子任务），
    # 其语义分别保留在全局 reminders / due_date
    cleaned_segs = []
    for seg in work_segs:
        clean = _strip_reminder_clause(seg['text'])
        if _is_pure_deadline_clause(clean):
            clean = ''
        if clean.strip():
            cleaned_segs.append({'text': clean, 'order': seg['order']})
    work_segs = cleaned_segs
    if not work_segs:
        work_segs = [{'text': goal_seg or segments[0]['text'], 'order': 1}]

    title = goal_seg or _short_title(work_segs[0]['text'])
    priority = parse_priority(raw) or 'medium'
    due_date = find_deadline(raw)
    reminders = parse_reminders(raw)

    subtasks = []
    prev = None
    for seg in work_segs:
        clean = seg['text']
        sub = {
            'title': _strip_marker(clean),
            'description': clean,
            'priority': parse_priority(clean) or priority,
            'due_date': find_deadline(clean) or due_date,
            'reminder': (parse_reminders(seg['text']) or [None])[0],
            'depends_on': [],
        }
        # 顺序依赖：前一个有明确顺序的后一个依赖它
        if prev is not None and seg['order'] is not None and prev >= 1:
            sub['depends_on'].append(prev - 1)
        deps = _detect_dependency(clean)
        if deps:
            for i, other in enumerate(work_segs):
                if other is not seg and deps in other['text']:
                    sub['depends_on'].append(i)
        subtasks.append(sub)
        if seg['order'] is not None and seg['order'] != 999:
            prev = len(subtasks)

    order = _plan_order(subtasks, fallback=list(range(len(subtasks))))
    return {
        'title': title,
        'goal': raw,
        'priority': priority,
        'due_date': due_date,
        'reminders': reminders,
        'subtasks': subtasks,
        'execution_order': order,
    }


def _short_title(text) -> str:
    text = _strip_marker(text)
    return text[:14] + '…' if len(text) > 15 else text


def _strip_marker(text) -> str:
    text = _NUMBERED_ITEM_RE.sub('', text).strip()
    for w in _SEQUENCE_WORDS:
        if text.startswith(w):
            text = text[len(w):].lstrip('，, ')
            break
    return text.strip()


def _plan_order(subtasks, fallback=None) -> list:
    """按子任务依赖计算执行顺序（拓扑），出现环时回退到自然顺序。"""
    n = len(subtasks)
    if n == 0:
        return []
    edges = []
    for i, sub in enumerate(subtasks):
        for d in sub.get('depends_on', []) or []:
            if 0 <= d < n and d != i:
                edges.append((d, i))
    indegree = [0] * n
    adj = [[] for _ in range(n)]
    for before, after in edges:
        indegree[after] += 1
        adj[before].append(after)
    from collections import deque
    queue = deque(i for i in range(n) if indegree[i] == 0)
    order = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for nxt in adj[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(order) != n:
        return list(fallback or range(n))
    return order