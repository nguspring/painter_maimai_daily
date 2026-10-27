"""内置日程：映射、跨午夜、时区与 provider 切换。"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import asyncio
from maibot_sdk import PluginPaths

from painter_maimai_daily.core.commands.handlers.schedule_handlers import cmd_schedule
from painter_maimai_daily.core.schedule.manager import ScheduleManager
from painter_maimai_daily.core.schedule.models import format_time_range, is_minutes_in_range
from painter_maimai_daily.core.schedule.provider import BuiltinScheduleProvider, NullScheduleProvider
from painter_maimai_daily.core.schedule.templates import get_template_schedule
from painter_maimai_daily.core.selfie.schedule_provider import (
    ActivityType,
    ApiScheduleProvider,
    get_schedule_provider,
)
from painter_maimai_daily.plugin import PainterMaimaiPlugin

TZ = ZoneInfo("Asia/Shanghai")


def make_plugin(tmp_path: Path) -> PainterMaimaiPlugin:
    """构造只注入配置与最小上下文的插件，不启动真实后台任务。"""
    plugin = PainterMaimaiPlugin()
    plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "4.0.0"}})
    plugin._set_context(SimpleNamespace(
        send=SimpleNamespace(text=AsyncMock(return_value=True)),
        paths=PluginPaths(data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime"),
    ))
    return plugin


def build_manager(tmp_path: Path, when: datetime) -> ScheduleManager:
    """创建使用固定时钟的管理器，避免测试依赖真实当前时间。"""
    manager = ScheduleManager(tmp_path / "schedule.db", "Asia/Shanghai", "auto")
    manager.now = lambda: when  # type: ignore[method-assign]
    return manager


def test_template_modes_keep_old_weekday_and_weekend_behavior() -> None:
    weekday = get_template_schedule("2026-10-27", "auto")
    weekend = get_template_schedule("2026-10-31", "auto")
    rest_day = get_template_schedule("2026-10-27", "rest_day")
    assert weekday[0].start_min == 7 * 60
    assert weekend[0].start_min == 9 * 60
    assert rest_day[0].start_min == 9 * 60
    assert weekday[-1].end_min == 0 and weekday[-1].start_min == 22 * 60 + 30


def test_cross_midnight_range_and_format() -> None:
    assert is_minutes_in_range(23 * 60, 22 * 60 + 30, 0)
    assert not is_minutes_in_range(30, 22 * 60 + 30, 0)
    assert is_minutes_in_range(30, 22 * 60 + 30, 60)
    assert not is_minutes_in_range(12 * 60, 22 * 60 + 30, 0)
    assert format_time_range(22 * 60 + 30, 0) == "22:30-00:00"


def test_manager_maps_items_to_activity_info_without_outfit(tmp_path: Path) -> None:
    when = datetime(2026, 10, 27, 10, 30, tzinfo=TZ)
    manager = build_manager(tmp_path, when)

    async def run() -> None:
        await manager.initialize()
        await asyncio.to_thread(manager._db.replace_schedule_items, when.date().isoformat(), [
            {"start_min": 9 * 60, "end_min": 11 * 60, "activity_type": "working",
             "description": "写代码", "mood": "focused", "source": "llm"},
            {"start_min": 11 * 60, "end_min": 12 * 60, "activity_type": "eating",
             "description": "午饭", "mood": "neutral", "source": "llm"},
        ])
        snapshot = await manager.refresh_snapshot()
        assert snapshot.schedule_date == "2026-10-27"
        assert snapshot.current is not None
        assert snapshot.current.activity_type is ActivityType.WORKING
        assert snapshot.current.time_window == "09:00-11:00"
        assert snapshot.current.timezone == "Asia/Shanghai"
        assert snapshot.current.outfit == ""
        assert snapshot.current.next_activities == [{"time": "11:00", "name": "午饭"}]
        await manager.close()

    asyncio.run(run())


def test_cross_midnight_activity_has_remaining_time(tmp_path: Path) -> None:
    when = datetime(2026, 10, 27, 23, 30, tzinfo=TZ)
    manager = build_manager(tmp_path, when)

    async def run() -> None:
        await manager.initialize()
        await asyncio.to_thread(manager._db.replace_schedule_items, when.date().isoformat(), [
            {"start_min": 22 * 60 + 30, "end_min": 0, "activity_type": "sleeping",
             "description": "准备睡觉", "mood": "calm", "source": "template"},
        ])
        snapshot = await manager.refresh_snapshot()
        assert snapshot.current is not None
        assert snapshot.current.time_window == "22:30-00:00"
        assert snapshot.current.activity_type is ActivityType.SLEEPING
        assert 1700 < snapshot.current.remaining_seconds(now=when) < 1900
        await manager.close()

    asyncio.run(run())


def test_provider_switch_respects_enabled_flag(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    manager = build_manager(tmp_path, datetime.now(TZ))
    plugin.schedule_manager = manager  # type: ignore[assignment]
    assert isinstance(get_schedule_provider(plugin), BuiltinScheduleProvider)

    plugin.config.schedule.provider = "maitrace"
    assert isinstance(get_schedule_provider(plugin), ApiScheduleProvider)

    plugin.config.schedule.enabled = False
    assert isinstance(get_schedule_provider(plugin), NullScheduleProvider)


def test_schedule_command_prints_current_and_all_items(tmp_path: Path) -> None:
    when = datetime(2026, 10, 27, 10, 30, tzinfo=TZ)
    manager = build_manager(tmp_path, when)
    plugin = make_plugin(tmp_path)
    plugin.schedule_manager = manager  # type: ignore[assignment]
    plugin.ctx.send.text = AsyncMock(return_value=True)  # type: ignore[attr-defined]
    dctx = SimpleNamespace(prefix="/dr", stream_id="s1", is_admin=False, chat_id="s1")

    async def run() -> None:
        await manager.initialize()
        await asyncio.to_thread(manager._db.replace_schedule_items, when.date().isoformat(), [
            {"start_min": 9 * 60, "end_min": 11 * 60, "activity_type": "working",
             "description": "写代码", "mood": "focused", "source": "llm"},
        ])
        success, _reason, intercepted = await cmd_schedule(plugin, dctx, "")
        assert success and intercepted
        text = plugin.ctx.send.text.await_args.args[0]
        assert "现在：写代码" in text and "09:00-11:00 写代码" in text
        await manager.close()

    asyncio.run(run())
