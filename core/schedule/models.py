"""内置日程的数据模型与时间工具。

本模块不定义 ActivityInfo：绘卷全链路只使用 ``core.selfie.schedule_provider``
里的活动结构，避免出现两套同名类型互相污染。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import re


@dataclass
class ScheduleItem:
    """单条日程记录；数据库没有 outfit 列，因此模型也不保留该字段。"""

    schedule_date: str
    start_min: int
    end_min: int
    activity_type: str
    description: str
    mood: str = "neutral"
    source: str = "template"


def parse_hhmm(value: str) -> int:
    """把 ``HH:MM`` 解析为当天分钟数；非法输入直接抛错，不做静默纠正。"""
    matched = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if matched is None:
        raise ValueError(f"无效时间格式: {value!r}")
    hour, minute = int(matched.group(1)), int(matched.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间范围越界: {value!r}")
    return hour * 60 + minute


def is_minutes_in_range(current: int, start: int, end: int) -> bool:
    """判断当前分钟是否落在 ``[start, end)``；``end < start`` 表示跨午夜。"""
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def format_time_range(start_min: int, end_min: int) -> str:
    """格式化为 ``HH:MM-HH:MM``；跨午夜时结束时间按次日 00:00 显示。"""
    start_hour, start_minute = divmod(start_min, 60)
    end_hour, end_minute = divmod(end_min % 1440, 60)
    return f"{start_hour:02d}:{start_minute:02d}-{end_hour:02d}:{end_minute:02d}"


def to_db_dict(item: ScheduleItem) -> dict[str, Any]:
    """转换成 ``ScheduleDB.replace_schedule_items`` 需要的字典。"""
    return {
        "start_min": int(item.start_min),
        "end_min": int(item.end_min),
        "activity_type": item.activity_type,
        "description": item.description,
        "mood": item.mood,
        "source": item.source,
    }


def from_db_row(row: dict[str, Any]) -> ScheduleItem:
    """把 SQLite 行还原为类型安全的日程项。"""
    return ScheduleItem(
        schedule_date=str(row.get("schedule_date", "")),
        start_min=int(row.get("start_min", 0)),
        end_min=int(row.get("end_min", 0)),
        activity_type=str(row.get("activity_type", "other")),
        description=str(row.get("description", "")),
        mood=str(row.get("mood", "neutral")),
        source=str(row.get("source", "template")),
    )
