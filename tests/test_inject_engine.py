"""注入引擎：规划器追加、标记去重、意图跳过与回复器复用。"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from zoneinfo import ZoneInfo

from painter_maimai_daily.core.inject.content_template import INJECT_MARKER
from painter_maimai_daily.core.inject.engine import InjectionEngine
from painter_maimai_daily.core.schedule.manager import ScheduleSnapshot
from painter_maimai_daily.core.schedule.models import ScheduleItem
from painter_maimai_daily.core.selfie.schedule_provider import ActivityInfo, ActivityType
from painter_maimai_daily.plugin import PainterMaimaiPlugin

TZ = ZoneInfo("Asia/Shanghai")


def make_plugin() -> tuple[PainterMaimaiPlugin, datetime]:
    """构造带固定日程快照的插件。"""
    when = datetime.now(TZ)
    plugin = PainterMaimaiPlugin()
    plugin.set_plugin_config({"plugin": {"enabled": True, "config_version": "4.0.0"}})
    activity = ActivityInfo(
        ActivityType.RELAXING,
        "正在窗边喝咖啡",
        time_window="00:00-23:59",
        as_of=when.isoformat(),
        timezone="Asia/Shanghai",
        next_activities=[{"time": "23:00", "name": "睡前洗漱"}],
    )
    item = ScheduleItem(when.date().isoformat(), 0, 1439, "relaxing", "正在窗边喝咖啡", "calm", "llm")
    snapshot = ScheduleSnapshot(
        when.date().isoformat(), (item,), activity,
        ({"time": "23:00", "name": "睡前洗漱"},), when.isoformat(), "Asia/Shanghai",
    )
    plugin.schedule_manager = SimpleNamespace(snapshot=snapshot, now=lambda: when)  # type: ignore[assignment]
    plugin.injection_engine = InjectionEngine(plugin)
    return plugin, when


def planner_kwargs(text: str, *, session_id: str = "s1") -> dict:
    """构造最小规划器 Hook 载荷。"""
    return {
        "session_id": session_id,
        "item_schema_version": 1,
        "items": [{"item_type": "UserMessageItem", "parts": [{"type": "text", "text": text}]}],
    }


def test_planner_appends_one_marked_context_item() -> None:
    plugin, _when = make_plugin()
    result = plugin.injection_engine.planner(planner_kwargs("你现在在干嘛？"))
    items = result["modified_kwargs"]["items"]
    assert len(items) == 2
    content = items[-1]["parts"][0]["text"]
    assert content.startswith(INJECT_MARKER)
    assert "正在窗边喝咖啡" in content and "睡前洗漱" in content
    assert items[-1]["meta"]["item_id"]


def test_planner_does_not_append_twice_to_same_request() -> None:
    plugin, _when = make_plugin()
    first = plugin.injection_engine.planner(planner_kwargs("你现在在干嘛？"))
    second = plugin.injection_engine.planner(first["modified_kwargs"])
    assert second == {"action": "continue"}


def test_planner_skips_technical_question() -> None:
    plugin, _when = make_plugin()
    assert plugin.injection_engine.planner(planner_kwargs("Python 怎么安装，报错了")) == {"action": "continue"}


def test_planner_skips_when_schedule_disabled() -> None:
    plugin, _when = make_plugin()
    plugin.config.schedule.enabled = False
    assert plugin.injection_engine.planner(planner_kwargs("你现在在干嘛？")) == {"action": "continue"}


def test_planner_respects_allowed_streams() -> None:
    plugin, _when = make_plugin()
    plugin.config.inject.allowed_streams = ["other"]
    plugin.injection_engine = InjectionEngine(plugin)
    assert plugin.injection_engine.planner(planner_kwargs("你现在在干嘛？")) == {"action": "continue"}


def test_planner_rejects_unknown_item_schema() -> None:
    plugin, _when = make_plugin()
    payload = planner_kwargs("你现在在干嘛？")
    payload["item_schema_version"] = 2
    with pytest.raises(ValueError, match="schema"):
        plugin.injection_engine.planner(payload)


def test_replyer_appends_and_reuses_content_on_retry() -> None:
    plugin, _when = make_plugin()
    plugin.injection_engine.planner(planner_kwargs("你现在在干嘛？"))
    first = plugin.injection_engine.replyer({"session_id": "s1", "attempt": 1, "extra_prompt": "原有要求"})
    prompt = first["modified_kwargs"]["extra_prompt"]
    assert prompt.startswith("原有要求") and INJECT_MARKER in prompt
    retry = plugin.injection_engine.replyer({"session_id": "s1", "attempt": 2, "extra_prompt": "原有要求"})
    assert retry["modified_kwargs"]["extra_prompt"] == prompt


def test_replyer_rejects_invalid_payload() -> None:
    plugin, _when = make_plugin()
    with pytest.raises(ValueError, match="attempt"):
        plugin.injection_engine.replyer({"session_id": "s1", "attempt": 0, "extra_prompt": ""})
    with pytest.raises(ValueError, match="extra_prompt"):
        plugin.injection_engine.replyer({"session_id": "s1", "attempt": 1, "extra_prompt": None})
