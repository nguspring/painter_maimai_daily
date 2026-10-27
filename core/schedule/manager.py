"""日程管理器：数据库读写、时区计算、内存快照与作息摘要。

快照只由后台任务刷新；注入 Hook 只读取最近一次快照，不查库、不调 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

import asyncio
import logging

from ..selfie.schedule_provider import ActivityInfo, ActivityType, classify_activity
from .db import ScheduleDB
from .generator import generate_schedule_via_llm
from .models import ScheduleItem, format_time_range, from_db_row, is_minutes_in_range, to_db_dict
from .templates import get_template_schedule

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin

logger = logging.getLogger("plugin.painter_maimai_daily.schedule.manager")


@dataclass(frozen=True)
class ScheduleSnapshot:
    """某一天的只读快照；注入和 provider 共用同一份数据。"""

    schedule_date: str
    items: tuple[ScheduleItem, ...]
    current: Optional[ActivityInfo]
    next_activities: tuple[dict[str, str], ...] = field(default_factory=tuple)
    as_of: str = ""
    timezone: str = ""


class ScheduleManager:
    """实例级日程管理器；所有数据库方法都在线程池中执行。"""

    def __init__(self, db_path: Path | str, timezone: str, template_mode: str = "auto") -> None:
        """创建数据库封装并保存时区/模板模式；schema 由 ``initialize`` 建立。"""
        self._db = ScheduleDB(db_path)
        self._snapshot: Optional[ScheduleSnapshot] = None
        self._timezone_name = timezone
        self._template_mode = template_mode
        self._timezone = ZoneInfo(timezone)

    @property
    def snapshot(self) -> Optional[ScheduleSnapshot]:
        """返回最近一次快照；未初始化时为 ``None``。"""
        return self._snapshot

    @property
    def timezone(self) -> ZoneInfo:
        """日程计算统一使用的时区。"""
        return self._timezone

    def configure(self, *, timezone: str, template_mode: str) -> None:
        """配置热更新时切换时区/模板；快照会被清空并在下一轮重建。"""
        self._timezone_name = timezone
        self._timezone = ZoneInfo(timezone)
        self._template_mode = template_mode
        self._snapshot = None

    def now(self) -> datetime:
        """返回日程时区下的当前时间。"""
        return datetime.now(self._timezone)

    def _now(self) -> datetime:
        """模块内部沿用短名，避免每处都写 ``now()``。"""
        return self.now()

    async def initialize(self) -> None:
        """建立数据库 schema。"""
        await asyncio.to_thread(self._db.ensure_schema)

    async def close(self) -> None:
        """卸载时关闭所有线程连接。"""
        self._snapshot = None
        await asyncio.to_thread(self._db.close_all)

    async def ensure_today_schedule(self) -> bool:
        """确保当天有模板兜底；返回是否刚刚写入模板。"""
        today = self._now().date().isoformat()
        rows = await asyncio.to_thread(self._db.list_schedule_items, today)
        if rows:
            return False
        items = get_template_schedule(today, self._template_mode)
        await asyncio.to_thread(
            self._db.replace_schedule_items, today, [to_db_dict(item) for item in items]
        )
        await asyncio.to_thread(self._db.set_state, "schedule_last_generated_date", today)
        await asyncio.to_thread(self._db.set_state, "schedule_last_generated_source", "template")
        await self.refresh_snapshot()
        logger.info("已写入 %s 的模板日程，共 %d 条", today, len(items))
        return True

    async def needs_llm_generation(self, target_date: Optional[str] = None) -> bool:
        """当天尚未由 LLM 成功生成过时才允许自动调用模型，避免重启烧 token。"""
        target = target_date or self._now().date().isoformat()
        generated_date = await asyncio.to_thread(self._db.get_state, "schedule_last_generated_date")
        generated_source = await asyncio.to_thread(self._db.get_state, "schedule_last_generated_source")
        return generated_date != target or generated_source != "llm"

    async def generate_for_date(self, plugin: "PainterMaimaiPlugin", target_date: str) -> bool:
        """生成并写入指定日期日程；失败不碰数据库。"""
        items = await generate_schedule_via_llm(plugin, target_date, self)
        if not items:
            return False
        await asyncio.to_thread(
            self._db.replace_schedule_items, target_date, [to_db_dict(item) for item in items]
        )
        await asyncio.to_thread(self._db.set_state, "schedule_last_generated_date", target_date)
        await asyncio.to_thread(self._db.set_state, "schedule_last_generated_source", "llm")
        if target_date == self._now().date().isoformat():
            await self.refresh_snapshot()
        logger.info("已写入 %s 的 LLM 日程，共 %d 条", target_date, len(items))
        return True

    async def regen_today_schedule_via_llm(self, plugin: "PainterMaimaiPlugin") -> bool:
        """命令入口：强制重新生成今天的日程。"""
        return await self.generate_for_date(plugin, self._now().date().isoformat())

    async def refresh_snapshot(self) -> ScheduleSnapshot:
        """读取当天数据并重算当前活动与后续三条。"""
        now = self._now()
        today = now.date().isoformat()
        rows = await asyncio.to_thread(self._db.list_schedule_items, today)
        items = [from_db_row(row) for row in rows]
        if not items:
            items = get_template_schedule(today, self._template_mode)
        current_item = self._pick_current_item(items, now.hour * 60 + now.minute)
        activity = None
        if current_item is not None:
            activity = self._to_activity(
                current_item,
                now,
                [item for item in items if item.start_min > now.hour * 60 + now.minute][:3],
            )
        snapshot = ScheduleSnapshot(
            schedule_date=today,
            items=tuple(items),
            current=activity,
            next_activities=tuple(activity.next_activities if activity is not None else []),
            as_of=now.isoformat(),
            timezone=self._timezone_name,
        )
        self._snapshot = snapshot
        return snapshot

    @staticmethod
    def _pick_current_item(items: list[ScheduleItem], current_minutes: int) -> Optional[ScheduleItem]:
        """命中时间窗优先；否则取已开始的最后一项，再退到当天首项。"""
        if not items:
            return None
        for item in items:
            if is_minutes_in_range(current_minutes, item.start_min, item.end_min):
                return item
        started = [item for item in items if item.start_min <= current_minutes]
        return started[-1] if started else items[0]

    def _to_activity(
        self, item: ScheduleItem, now: datetime, future_items: list[ScheduleItem],
    ) -> ActivityInfo:
        """把数据库日程映射成绘卷 ActivityInfo；outfit 必须留空（D4）。"""
        return ActivityInfo(
            activity_type=classify_activity(item.activity_type, item.description),
            description=item.description,
            mood=item.mood or "neutral",
            time_point=now.strftime("%H:%M"),
            outfit="",
            location="",
            time_window=format_time_range(item.start_min, item.end_min),
            as_of=now.isoformat(),
            timezone=self._timezone_name,
            next_activities=[
                {"time": f"{future.start_min // 60:02d}:{future.start_min % 60:02d}", "name": future.description}
                for future in future_items
            ],
            scene="",
            sleep_phase="",
            energy=None,
            health_note="",
        )

    async def get_current_activity(self) -> ActivityInfo | None:
        """返回当前活动；没有快照时先刷新，仍无数据则按模板兜底。"""
        snapshot = self._snapshot
        if snapshot is None or snapshot.schedule_date != self._now().date().isoformat():
            snapshot = await self.refresh_snapshot()
        if snapshot.current is not None:
            return snapshot.current
        return ActivityInfo(
            activity_type=ActivityType.OTHER,
            description="日常活动",
            mood="neutral",
            time_point=self._now().strftime("%H:%M"),
            as_of=self._now().isoformat(),
            timezone=self._timezone_name,
        )

    async def list_schedule_items(self, schedule_date: Optional[str] = None) -> list[ScheduleItem]:
        """按日期读取日程；默认今天。"""
        target = schedule_date or self._now().date().isoformat()
        rows = await asyncio.to_thread(self._db.list_schedule_items, target)
        return [from_db_row(row) for row in rows]

    async def get_history_schedule_items(self, days: int) -> dict[str, list[ScheduleItem]]:
        """读取最近 N 天历史；当天不算历史。"""
        today = self._now().date()
        result: dict[str, list[ScheduleItem]] = {}
        for offset in range(1, max(0, days) + 1):
            target = (today - timedelta(days=offset)).isoformat()
            rows = await asyncio.to_thread(self._db.list_schedule_items, target)
            if rows:
                result[target] = [from_db_row(row) for row in rows]
        return result

    async def get_history_schedule_summary(self, days: int = 1, max_length: int = 500) -> str:
        """压缩历史日程为模型可读摘要，避免把整库塞进提示词。"""
        history = await self.get_history_schedule_items(days)
        if not history:
            return ""
        parts: list[str] = []
        total = 0
        for date_text, items in sorted(history.items(), reverse=True):
            durations: dict[str, int] = {}
            segments: list[str] = []
            for item in items:
                duration = item.end_min - item.start_min
                if duration <= 0:
                    duration += 1440
                durations[item.activity_type] = durations.get(item.activity_type, 0) + duration
                segments.append(f"{format_time_range(item.start_min, item.end_min)} {item.description}")
            top_types = ", ".join(
                f"{name}({minutes // 60}h)" for name, minutes in sorted(durations.items(), key=lambda pair: -pair[1])[:3]
            )
            day_text = f"{date_text}：{'; '.join(segments[:5])}... 主要活动：{top_types}"
            if total + len(day_text) > max_length:
                break
            parts.append(day_text)
            total += len(day_text)
        return "\n".join(parts)

    async def cleanup_old_data(self, retention_days: int) -> int:
        """删除超过保留期的日程；截止日期按日程时区计算。"""
        cutoff = (self._now().date() - timedelta(days=retention_days)).isoformat()
        return await asyncio.to_thread(self._db.cleanup_old_schedule_items, cutoff)

    async def get_state(self, key: str) -> str | None:
        """透传状态读取，供任务判断每日生成进度。"""
        return await asyncio.to_thread(self._db.get_state, key)

    async def set_state(self, key: str, value: str) -> None:
        """透传状态写入。"""
        await asyncio.to_thread(self._db.set_state, key, value)
