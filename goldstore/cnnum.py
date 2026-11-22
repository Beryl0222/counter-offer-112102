"""中文数字与时刻短语解析（覆盖活动话术与客服来信中出现的形式）。"""
from __future__ import annotations

import re

CN_DIGIT = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def chinese_number(text: str) -> int:
    """解析「四十」「两百」「四千」这类中文数字。"""
    total = 0
    num = 0
    for ch in text:
        if ch in CN_DIGIT:
            num = CN_DIGIT[ch]
        elif ch in CN_UNIT:
            total += (num or 1) * CN_UNIT[ch]
            num = 0
        else:
            raise ValueError(f"无法解析的中文数字: {text!r}")
    return total + num


_TIME_RE = re.compile(
    r"(中午|上午|下午|晚上)?([一二两三四五六七八九十]{1,3})点(半|([一二两三四五六七八九十]{1,3})分)?"
)


def parse_time_phrase(text: str) -> str | None:
    """从一句话里找出「X点 / X点半 / 中午X点」时刻，返回 HH:MM 或 None。"""
    m = _TIME_RE.search(text)
    if not m:
        return None
    period, hour_cn, tail, minute_cn = m.group(1), m.group(2), m.group(3), m.group(4)
    hour = chinese_number(hour_cn)
    if tail == "半":
        minute = 30
    elif minute_cn:
        minute = chinese_number(minute_cn)
    else:
        minute = 0
    if period in ("中午", "下午", "晚上") and hour < 12:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def parse_duration_minutes(text: str) -> int | None:
    """「一刻钟」→ 15，「两刻钟」→ 30。"""
    m = re.search(r"([一二两三四五六七八九十]{1,3})?刻钟", text)
    if not m:
        return None
    return chinese_number(m.group(1)) * 15 if m.group(1) else 15


def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
