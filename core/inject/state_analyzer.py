"""活动状态分析：把时间窗换算成“刚开始 / 进行中 / 快结束”。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import math
import re

from ..selfie.schedule_provider import ActivityInfo

_TIME_WINDOW = re.compile(r"(\d{1,2}):(\d{2})\s*[-~～至]\s*(\d{1,2}):(\d{2})")


@dataclass(frozen=True)
class ActivityState:
    """注入文本需要的三个时间事实。"""

    phase: str
    elapsed_minutes: int
    remaining_minutes: Optional[int]


def analyze_activity_state(activity: ActivityInfo, now: Optional[datetime] = None) -> Optional[ActivityState]:
    """解析活动时间窗；没有合法时间窗时返回 ``None``，不编造进度。"""
    match = _TIME_WINDOW.fullmatch(activity.time_window.strip()) if activity.time_window else None
    if match is None:
        return None
    start_minutes = int(match.group(1)) * 60 + int(match.group(2))
    end_minutes = int(match.group(3)) * 60 + int(match.group(4))
    if start_minutes == end_minutes:
        return None

    moment = activity._moment(now)  # noqa: SLF001 - 与 ActivityInfo 共用同一时区换算
    current_minutes = moment.hour * 60 + moment.minute
    if end_minutes > start_minutes:
        if not start_minutes <= current_minutes < end_minutes:
            return None
        elapsed = current_minutes - start_minutes
        total = end_minutes - start_minutes
    else:
        if current_minutes < end_minutes:
            current_minutes += 1440
        elif current_minutes < start_minutes:
            return None
        elapsed = current_minutes - start_minutes
        total = 1440 - start_minutes + end_minutes

    progress = elapsed / total if total else 0.0
    phase = "刚开始" if progress < 0.1 else "快结束" if progress > 0.8 else "正在进行"
    remaining_seconds = activity.remaining_seconds(now=moment) or 0.0
    remaining = math.ceil(remaining_seconds / 60)
    return ActivityState(phase, elapsed, remaining if remaining > 0 else None)
