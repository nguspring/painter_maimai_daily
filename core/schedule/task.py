"""日程后台任务：模板兜底、每日 LLM 生成、快照刷新与旧数据清理。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import asyncio
import logging
import time

from .models import parse_hhmm

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin

logger = logging.getLogger("plugin.painter_maimai_daily.schedule.task")


class ScheduleTask:
    """单实例后台循环；配置热更新时整体重启。"""

    def __init__(self, plugin: "PainterMaimaiPlugin") -> None:
        """绑定插件；管理器由插件在启动前创建。"""
        self.plugin = plugin
        self.task: Optional[asyncio.Task] = None
        self.is_running = False
        self._first_iteration = True
        self._last_llm_attempt = 0.0
        self._cleanup_date = ""

    async def start(self) -> None:
        """启动循环；重复调用不会创建第二个任务。"""
        if self.is_running:
            return
        if self.plugin.schedule_manager is None:
            raise RuntimeError("日程管理器尚未初始化")
        self.is_running = True
        self._first_iteration = True
        self.task = asyncio.create_task(self._loop(), name="painter-maimai:schedule")
        logger.info("日程任务已启动")

    async def stop(self) -> None:
        """取消并等待循环结束，避免关闭数据库时仍有读写。"""
        self.is_running = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        logger.info("日程任务已停止")

    async def _loop(self) -> None:
        """按快照间隔持续维护当天日程。"""
        while self.is_running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("日程任务本轮失败，保留现有数据")
            delay = self.plugin.config.schedule.snapshot_refresh_seconds
            await asyncio.sleep(delay)

    async def _tick(self) -> None:
        """单轮：补模板、必要时生成、刷新快照、清理过期数据。"""
        manager = self.plugin.schedule_manager
        if manager is None:
            return
        await manager.ensure_today_schedule()
        now = manager.now()
        today = now.date().isoformat()

        if await manager.needs_llm_generation(today):
            auto_minutes = parse_hhmm(self.plugin.config.schedule.auto_generate_time)
            current_minutes = now.hour * 60 + now.minute
            elapsed = time.monotonic() - self._last_llm_attempt
            if (self._first_iteration or current_minutes >= auto_minutes) and elapsed >= 1800:
                self._last_llm_attempt = time.monotonic()
                logger.info("开始生成 %s 的 LLM 日程", today)
                generated = await manager.generate_for_date(self.plugin, today)
                if not generated:
                    logger.warning("LLM 日程生成失败，继续使用模板数据")

        await manager.refresh_snapshot()
        if self._cleanup_date != today:
            await manager.cleanup_old_data(self.plugin.config.schedule.retention_days)
            self._cleanup_date = today
        self._first_iteration = False
