"""内置日程 provider：把管理器的快照映射成绘卷活动结构。"""

from __future__ import annotations

from typing import Optional

from ..selfie.schedule_provider import ActivityInfo, ScheduleProvider
from .manager import ScheduleManager


class NullScheduleProvider(ScheduleProvider):
    """日程关闭时使用；始终返回无活动，不静默回退到其他来源（D10）。"""

    async def get_current_activity(self) -> Optional[ActivityInfo]:
        """明确表示当前没有可用日程。"""
        return None


class BuiltinScheduleProvider(ScheduleProvider):
    """读取本地日程库的内存快照，不直接查数据库。"""

    def __init__(self, manager: ScheduleManager) -> None:
        """绑定实例级管理器；管理器生命周期由插件负责。"""
        self._manager = manager

    async def get_current_activity(self) -> Optional[ActivityInfo]:
        """返回当前活动；库为空时按模板兜底。"""
        return await self._manager.get_current_activity()
