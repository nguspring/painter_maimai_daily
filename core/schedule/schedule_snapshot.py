"""用于聊天注入的精确日程快照；空档不使用最近或未来活动冒充当前活动。"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt

from .calendar_context import BEIJING
from .schedule_models import ScheduleItem


@dataclass(frozen=True)
class TimedActivity:
    """带完整日期边界的日程项，区分午夜结束和跨到次日结束。"""

    item: ScheduleItem
    start: dt.datetime
    end: dt.datetime

    def time_range(self) -> str:
        """返回含日期的起止时间，防止次日计划被当成今天的活动。"""
        return f"{self.start:%Y-%m-%d %H:%M}—{self.end:%Y-%m-%d %H:%M}"


@dataclass(frozen=True)
class ScheduleSnapshot:
    """同一时间点的当前活动及唯一下一项计划，缺失时保持 None。"""

    now: dt.datetime
    current: TimedActivity | None
    next_activity: TimedActivity | None


def build_snapshot(now: dt.datetime, items: list[ScheduleItem]) -> ScheduleSnapshot:
    """用已取得的日程构建快照；按真实日期处理跨午夜，活动区间为左闭右开。"""
    if now.tzinfo is None:
        raise ValueError("日程快照时间必须包含时区")
    now = now.astimezone(BEIJING)
    timed = []
    for item in items:
        if not 0 <= item.start_min < 1440 or not 0 <= item.end_min <= 1440 or item.start_min == item.end_min:
            raise ValueError("日程起止分钟无效或区间为空")
        midnight = dt.datetime.combine(dt.date.fromisoformat(item.schedule_date), dt.time(), BEIJING)
        start = midnight + dt.timedelta(minutes=item.start_min)
        end = midnight + dt.timedelta(minutes=item.end_min)
        # 旧模板使用结束分钟 0 表示次日午夜；数据库中的真正跨日活动同样延长到次日。
        if end <= start:
            end += dt.timedelta(days=1)
        timed.append(TimedActivity(item, start, end))
    timed.sort(key=_start_time)
    active = [activity for activity in timed if activity.start <= now < activity.end]
    future = [activity for activity in timed if activity.start > now]
    return ScheduleSnapshot(now, active[-1] if active else None, future[0] if future else None)


def _start_time(activity: TimedActivity) -> dt.datetime:
    """提供按实际开始时间排序的键，使跨日活动与当天活动使用同一时间轴。"""
    return activity.start
