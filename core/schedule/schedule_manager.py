"""
日程管理器：对外提供 async API，内部通过 asyncio.to_thread 调用 ScheduleDB。
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

from .schedule_db import ScheduleDB
from .schedule_llm_generator import generate_schedule_via_llm
from .schedule_models import (
    ActivityInfo,
    ActivityType,
    ScheduleItem,
    from_db_row,
    is_minutes_in_range,
    schedule_item_to_activity_info,
    to_db_dict,
)
from .schedule_templates import get_template_schedule
from .calendar_context import BEIJING, CalendarDay, get_calendar_service, now_beijing
from .schedule_snapshot import ScheduleSnapshot, build_snapshot

logger = logging.getLogger(__name__)

_manager_instance: ScheduleManager | None = None


def get_schedule_manager() -> "ScheduleManager":
    """获取全局日程管理器。"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = ScheduleManager()
    return _manager_instance


class ScheduleManager:
    """进程级日程管理器。修复 F9：LLM 覆盖任务去重。"""

    def __init__(self):
        """初始化数据库。"""
        self._db: ScheduleDB = ScheduleDB()
        # 修复 F9：保存 LLM 覆盖任务句柄，按日期去重
        self._llm_override_tasks: dict[str, asyncio.Task] = {}

    async def ensure_db_initialized(self) -> None:
        """确保数据库 schema 已建立。"""
        await asyncio.to_thread(self._db.ensure_schema)

    async def stop_override_tasks(self) -> None:
        """取消并等待本管理器创建的 LLM 覆盖任务，卸载后不再写入日程。"""
        tasks = list(self._llm_override_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._llm_override_tasks.clear()

    async def ensure_today_schedule(self, plugin: Any | None = None) -> None:
        """确保今日有日程，优先模板，再异步尝试 LLM 覆盖（修复 F9：去重）。"""
        today_date = now_beijing().date()
        today = today_date.isoformat()
        items = await asyncio.to_thread(self._db.list_schedule_items, today)

        if not items:
            calendar = await get_calendar_service().get_day(today_date)
            template_items = get_template_schedule(today, is_off_day=calendar.is_off_day)
            await asyncio.to_thread(
                self._db.replace_schedule_items,
                today,
                [to_db_dict(item) for item in template_items],
            )
            await asyncio.to_thread(self._db.set_state, "schedule_last_generated_date", today)
            await asyncio.to_thread(self._db.set_state, "schedule_last_generated_source", "template")

        if plugin is not None:
            # 模板可继续被 LLM 覆盖，仅成功的 LLM 日程阻止重复生成。
            last_generated_date = await asyncio.to_thread(self._db.get_state, "schedule_last_generated_date")
            last_generated_source = await asyncio.to_thread(self._db.get_state, "schedule_last_generated_source")
            if last_generated_date == today and last_generated_source == "llm":
                logger.debug(f"[ScheduleManager] 今日日程已生成过（{last_generated_date}），跳过 LLM 覆盖")
                return

            # 修复 F9：检查是否已有该日期的 LLM 覆盖任务（进程内去重）
            if today in self._llm_override_tasks:
                existing_task = self._llm_override_tasks[today]
                if not existing_task.done():
                    logger.debug(f"[ScheduleManager] LLM 覆盖任务已存在且未完成: {today}")
                    return
                # 任务已完成，移除旧句柄
                del self._llm_override_tasks[today]

            # 创建新任务并保存句柄
            task = asyncio.create_task(self._try_llm_override(plugin, today))
            self._llm_override_tasks[today] = task

    async def _try_llm_override(self, plugin: Any, target_date: str) -> None:
        """尝试使用 LLM 覆盖今日日程。"""
        try:
            model_id = str(plugin.get_config("schedule.model_id", "planner"))
            # 使用增强版生成器，传入 schedule_manager 以支持历史记忆
            items = await generate_schedule_via_llm(
                plugin=plugin,
                target_date=target_date,
                model_id=model_id,
                schedule_manager=self,  # 传入 self 以支持历史记忆
            )
            if not items:
                return

            await asyncio.to_thread(
                self._db.replace_schedule_items,
                target_date,
                [to_db_dict(item) for item in items],
            )
            await asyncio.to_thread(self._db.set_state, "schedule_last_generated_date", target_date)
            await asyncio.to_thread(self._db.set_state, "schedule_last_generated_source", "llm")
            logger.info("[ScheduleManager] LLM 覆盖日程成功: %s", target_date)
        except Exception as exc:
            logger.error("[ScheduleManager] LLM 覆盖异常: %s", exc, exc_info=True)

    @staticmethod
    def _pick_activity_fallback_item(items: list[ScheduleItem], current_minutes: int) -> ScheduleItem | None:
        """未命中时间区间时，选择最贴近“当前时刻”的活动项。

        规则：
        1. 优先选择“已经开始”的最后一项（start_min <= current_minutes 的最大项）
        2. 若当前时间早于当天首项，则回退到第一项
        """
        if not items:
            return None

        started_items = [item for item in items if item.start_min <= current_minutes]
        if started_items:
            return started_items[-1]

        return items[0]

    async def get_current_activity(self):
        """获取当前活动，永不返回 None。"""
        now = now_beijing()
        today = now.date().isoformat()
        current_minutes = now.hour * 60 + now.minute
        now_str = now.strftime("%H:%M")

        rows = await asyncio.to_thread(self._db.list_schedule_items, today)

        if rows:
            items = [from_db_row(row) for row in rows]
            for item in items:
                if is_minutes_in_range(current_minutes, item.start_min, item.end_min):
                    return schedule_item_to_activity_info(item, now_str)

            fallback_item = self._pick_activity_fallback_item(items, current_minutes)
            if fallback_item is not None:
                return schedule_item_to_activity_info(fallback_item, now_str)

        fallback_items = get_template_schedule(today)
        for item in fallback_items:
            if is_minutes_in_range(current_minutes, item.start_min, item.end_min):
                return schedule_item_to_activity_info(item, now_str)

        fallback_item = self._pick_activity_fallback_item(fallback_items, current_minutes)
        if fallback_item is not None:
            return schedule_item_to_activity_info(fallback_item, now_str)

        return ActivityInfo(
            activity_type=ActivityType.OTHER,
            description="日常活动",
            mood="neutral",
            time_point=now_str,
        )

    async def get_future_activities(self, limit: int = 3) -> list[Any]:
        """获取后续活动列表。"""
        now = now_beijing()
        today = now.date().isoformat()
        current_minutes = now.hour * 60 + now.minute

        rows = await asyncio.to_thread(self._db.list_schedule_items, today)
        items = [from_db_row(row) for row in rows]

        future = [item for item in items if item.start_min > current_minutes]
        return [schedule_item_to_activity_info(item) for item in future[: max(0, limit)]]

    async def list_schedule_items(self, schedule_date: str) -> list[ScheduleItem]:
        """按日期获取日程项列表。"""
        rows = await asyncio.to_thread(self._db.list_schedule_items, schedule_date)
        return [from_db_row(row) for row in rows]

    async def get_injection_snapshot(self, now: datetime.datetime, calendar: CalendarDay) -> ScheduleSnapshot:
        """按同一北京时间读取精确注入快照；查询数据库但不修改既有日程。

        仅今日无数据时采用模板，前后两天只读取已存在的计划，避免编造明日安排。
        """
        if now.tzinfo is None:
            raise ValueError("日程快照时间必须包含时区")
        now = now.astimezone(BEIJING)
        today = now.date()
        if today != calendar.date:
            raise ValueError("日程快照与日历必须使用同一天")
        items = await self.list_schedule_items(today.isoformat())
        if not items:
            items = get_template_schedule(today.isoformat(), is_off_day=calendar.is_off_day)
        # 前一天用于恢复跨午夜活动，后一天用于展示实际已有的唯一下一项计划。
        for offset in (-1, 1):
            items.extend(await self.list_schedule_items((today + datetime.timedelta(days=offset)).isoformat()))
        return build_snapshot(now, items)

    async def regen_today_schedule_via_llm(self, plugin: Any) -> bool:
        """手动触发 LLM 重生成。"""
        today = now_beijing().date().isoformat()
        model_id = str(plugin.get_config("schedule.model_id", "planner"))
        items = await generate_schedule_via_llm(plugin, today, model_id=model_id)
        if not items:
            return False

        await asyncio.to_thread(
            self._db.replace_schedule_items,
            today,
            [to_db_dict(item) for item in items],
        )
        await asyncio.to_thread(self._db.set_state, "schedule_last_generated_date", today)
        await asyncio.to_thread(self._db.set_state, "schedule_last_generated_source", "llm")
        return True

    async def get_inject_override(self, stream_id: str) -> bool | None:
        """读取注入覆盖开关。"""
        value = await asyncio.to_thread(self._db.get_state, f"schedule_inject_enabled_override:{stream_id}")
        if value is None:
            return None
        return value.lower() == "true"

    async def set_inject_override(self, stream_id: str, enabled: bool) -> None:
        """写入注入覆盖开关。"""
        await asyncio.to_thread(
            self._db.set_state,
            f"schedule_inject_enabled_override:{stream_id}",
            "true" if enabled else "false",
        )

    async def get_state(self, key: str) -> str | None:
        """读取状态。"""
        return await asyncio.to_thread(self._db.get_state, key)

    async def set_state(self, key: str, value: str) -> None:
        """写入状态。"""
        await asyncio.to_thread(self._db.set_state, key, value)

    # ========== 历史记忆相关方法 ==========

    async def get_history_schedule_items(self, days: int = 1) -> dict[str, list[Any]]:
        """
        获取历史日程数据。

        Args:
            days: 历史天数（默认1=昨天）

        Returns:
            dict[str, list]: 日期到日程项列表的映射
            例如：{"2026-03-01": [ScheduleItem, ...], "2026-03-02": [...]}

        示例：
            # 获取昨天的日程
            history = await manager.get_history_schedule_items(days=1)
            # 获取最近3天的日程
            history = await manager.get_history_schedule_items(days=3)
        """
        if days <= 0:
            return {}

        today = now_beijing().date()
        result: dict[str, list[Any]] = {}

        for i in range(1, days + 1):
            date = today - datetime.timedelta(days=i)
            date_str = date.isoformat()
            rows = await asyncio.to_thread(self._db.list_schedule_items, date_str)
            if rows:
                items = [from_db_row(row) for row in rows]
                result[date_str] = items

        return result

    async def get_history_schedule_summary(self, days: int = 1, max_length: int = 500) -> str:
        """
        获取历史日程摘要（用于 LLM 上下文）。

        将历史日程压缩为简短的摘要字符串，用于日程生成的连续性参考。

        Args:
            days: 历史天数（默认1=昨天）
            max_length: 最大字符长度限制

        Returns:
            str: 历史日程摘要

        示例输出：
            "昨天(2026-03-01)的日程：
            08:00-09:00 起床洗漱
            09:00-12:00 写代码
            ...
            主要活动：工作(4h)、休息(2h)、学习(3h)"
        """
        history = await self.get_history_schedule_items(days)

        if not history:
            return ""

        parts: list[str] = []
        total_length = 0

        for date_str, items in sorted(history.items(), reverse=True):
            # 计算各类型活动的时长
            type_durations: dict[str, int] = {}
            day_parts: list[str] = []

            for item in items:
                duration = item.end_min - item.start_min
                activity_type = item.activity_type
                type_durations[activity_type] = type_durations.get(activity_type, 0) + duration

                # 格式化时间
                start_h, start_m = divmod(item.start_min, 60)
                end_h, end_m = divmod(item.end_min, 60)
                time_str = f"{start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d}"
                day_parts.append(f"{time_str} {item.description}")

            # 活动类型统计（转换为小时）
            type_summary = ", ".join(
                f"{t}({d // 60}h)" for t, d in sorted(type_durations.items(), key=lambda x: -x[1])[:3]
            )

            day_str = f"{date_str}的日程：{'; '.join(day_parts[:5])}... 主要活动：{type_summary}"

            if total_length + len(day_str) > max_length:
                break

            parts.append(day_str)
            total_length += len(day_str)

        if not parts:
            return ""

        return "\n".join(parts)

    async def cleanup_old_schedule_data(self, retention_days: int) -> int:
        """
        清理旧的日程数据。

        Args:
            retention_days: 保留天数（-1=永久保留）

        Returns:
            int: 删除的记录数
        """
        return await asyncio.to_thread(self._db.cleanup_old_schedule_items, retention_days)
