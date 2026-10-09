"""插件后台任务生命周期逻辑。"""

from __future__ import annotations

import asyncio
from typing import Any

from src.common.logger import get_logger

logger = get_logger("selfie_painter_v2")


class PluginRuntimeMixin:
    """提供自动自拍与日程后台任务的生命周期管理。"""

    _auto_selfie_task: Any | None = None
    _auto_selfie_pending: bool = False
    _auto_selfie_startup_task: asyncio.Task[Any] | None = None  # 延迟启动任务句柄
    _schedule_gen_task: asyncio.Task[Any] | None = None
    _schedule_pending: bool = False
    _schedule_startup_task: asyncio.Task[Any] | None = None  # 延迟启动任务句柄

    def _initialize_runtime_state(self) -> None:
        """初始化后台任务运行时状态。"""
        self._auto_selfie_task = None
        self._auto_selfie_pending = False
        self._auto_selfie_startup_task = None
        self._schedule_gen_task = None
        self._schedule_pending = False
        self._schedule_startup_task = None
        self._runtime_bootstrapped = False

    @staticmethod
    def _schedule_background_task(coro_func) -> asyncio.Task[Any] | None:
        """在事件循环已运行时创建后台任务并返回句柄，否则返回 None。

        Returns:
            创建的 Task 对象，如果事件循环未就绪则返回 None
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        return loop.create_task(coro_func())

    def _bootstrap_runtime_tasks(self) -> None:
        """按配置启动后台任务，保存延迟任务句柄以便后续取消。

        由进程启动事件调用，不在构造函数中调用。
        """
        # 避免重复启动事件再次创建相同循环。
        if self._runtime_bootstrapped:
            self.try_start_auto_selfie()
            self.try_start_schedule_gen()
            return
        self._runtime_bootstrapped = True
        if self.get_config("auto_selfie.enabled", False):
            from .core.selfie import AutoSelfieTask

            self._auto_selfie_task = AutoSelfieTask(self)
            startup_task = self._schedule_background_task(self._start_auto_selfie_after_delay)
            if startup_task:
                self._auto_selfie_startup_task = startup_task
            else:
                self._auto_selfie_pending = True
                logger.info("事件循环未就绪，自动自拍任务将在首次执行时懒启动")

        startup_task = self._schedule_background_task(self._start_schedule_gen_after_delay)
        if startup_task:
            self._schedule_startup_task = startup_task
        else:
            self._schedule_pending = True

    async def _start_auto_selfie_after_delay(self):
        """延迟启动自动自拍任务。"""
        await asyncio.sleep(15)
        if self._auto_selfie_task:
            try:
                await self._auto_selfie_task.start()
                self._auto_selfie_pending = False
            except Exception as exc:
                logger.error("自动自拍任务启动失败: %s", exc, exc_info=True)
                # 启动失败时保持 pending 标记，允许后续重试
                self._auto_selfie_pending = True
                raise

    def try_start_auto_selfie(self):
        """尝试懒启动自动自拍任务（供组件首次执行时调用）。"""
        if not self._auto_selfie_pending or not self._auto_selfie_task:
            return
        startup_task = self._schedule_background_task(self._start_auto_selfie_after_delay)
        if startup_task:
            self._auto_selfie_startup_task = startup_task
            self._auto_selfie_pending = False
        else:
            logger.debug("自动自拍懒启动失败，等待下次重试: 事件循环未就绪")

    def try_start_schedule_gen(self):
        """尝试懒启动日程后台任务。"""
        if not self._schedule_pending:
            return
        startup_task = self._schedule_background_task(self._start_schedule_gen_after_delay)
        if startup_task:
            self._schedule_startup_task = startup_task
            self._schedule_pending = False
        else:
            logger.debug("日程任务懒启动失败，等待下次重试: 事件循环未就绪")

    async def _start_schedule_gen_after_delay(self) -> None:
        """延迟15秒后初始化日程管理器并确保今日有日程。"""
        await asyncio.sleep(15)
        try:
            from .core.schedule import get_schedule_manager

            mgr = get_schedule_manager()
            await mgr.ensure_db_initialized()
            await mgr.ensure_today_schedule(plugin=self)
            self._schedule_pending = False
            if self.get_config("schedule.auto_generate_enabled", True):
                self._schedule_gen_task = asyncio.create_task(self._schedule_gen_loop())
        except Exception as exc:
            logger.error("[SelfiePainter] 日程初始化失败: %s", exc, exc_info=True)

    async def _schedule_gen_loop(self) -> None:
        """每日在指定时间重新生成日程。"""
        import datetime
        import random
        from .core.schedule.calendar_context import now_beijing

        while True:
            try:
                gen_time_str = self.get_config("schedule.auto_generate_time", "06:30")
                now = now_beijing()
                hour, minute = map(int, gen_time_str.split(":"))
                target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target <= now:
                    target += datetime.timedelta(days=1)
                jitter = random.randint(0, 60)
                wait_seconds = (target - now).total_seconds() + jitter
                await asyncio.sleep(wait_seconds)

                from .core.schedule import get_schedule_manager

                mgr = get_schedule_manager()
                await mgr.ensure_today_schedule(plugin=self)
                logger.info("[SelfiePainter] 每日日程生成完成")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[SelfiePainter] 日程生成循环异常: %s", exc, exc_info=True)
                await asyncio.sleep(300)

    async def on_plugin_unload(self) -> None:
        """插件卸载时停止后台任务。

        由进程停止事件调用，可重复执行；当前宿主热卸载不会调用此方法。
        """
        await self._stop_auto_selfie_task()
        await self._stop_schedule_gen_task()
        from .core.schedule.schedule_manager import get_schedule_manager

        await get_schedule_manager().stop_override_tasks()
        self._runtime_bootstrapped = False

    async def _stop_auto_selfie_task(self) -> None:
        """停止自动自拍后台任务，包括延迟启动任务和运行中的任务。"""
        # 先取消延迟启动任务
        if self._auto_selfie_startup_task and not self._auto_selfie_startup_task.done():
            self._auto_selfie_startup_task.cancel()
            try:
                await self._auto_selfie_startup_task
            except asyncio.CancelledError:
                pass
            self._auto_selfie_startup_task = None
            # 重置 pending 标记，避免重启后懒启动逻辑跳过
            self._auto_selfie_pending = False

        # 再停止运行中的自拍任务
        if not self._auto_selfie_task:
            self._auto_selfie_pending = False
            return

        try:
            await self._auto_selfie_task.stop()
        except Exception as exc:
            logger.warning("停止自动自拍任务失败: %s", exc, exc_info=True)
        finally:
            self._auto_selfie_task = None
            self._auto_selfie_pending = False

    async def _stop_schedule_gen_task(self) -> None:
        """停止日程后台任务，包括延迟启动任务和运行中的任务。"""
        # 先取消延迟启动任务
        if self._schedule_startup_task and not self._schedule_startup_task.done():
            self._schedule_startup_task.cancel()
            try:
                await self._schedule_startup_task
            except asyncio.CancelledError:
                pass
            self._schedule_startup_task = None
            # 重置 pending 标记，避免重启后懒启动逻辑跳过
            self._schedule_pending = False

        # 再取消日程生成循环任务
        if self._schedule_gen_task and not self._schedule_gen_task.done():
            self._schedule_gen_task.cancel()
            try:
                await self._schedule_gen_task
            except asyncio.CancelledError:
                pass
        self._schedule_gen_task = None
        self._schedule_pending = False


__all__ = ["PluginRuntimeMixin"]
