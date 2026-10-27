"""验证绘卷与 MaiTrace 的接口边界、场景换装和主动投递生命周期。"""

from maibot_sdk import PluginPaths
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import asyncio
import base64
import json
import pytest
import re
import time

from painter_maimai_daily.core.pipeline import Pipeline
from painter_maimai_daily.core.pipeline.request import GenerationRequest
from painter_maimai_daily.core.pipeline.steps.build_selfie_prompt import BuildSelfiePrompt
from painter_maimai_daily.core.prompts import selfie_prompt_builder
from painter_maimai_daily.core.selfie import auto_selfie_task, group_image_task, proactive_context
from painter_maimai_daily.core.selfie.activity_rules import ACTIVITY_OUTFITS, choose_auto_style
from painter_maimai_daily.core.selfie.auto_selfie_task import AutoSelfieTask
from painter_maimai_daily.core.selfie.group_image_task import GroupImageTask, parse_decision
from painter_maimai_daily.core.selfie.proactive_context import recent_context
from painter_maimai_daily.core.selfie.reference_image import load_reference_image
from painter_maimai_daily.core.selfie.schedule_provider import ActivityInfo, ActivityType, ApiScheduleProvider, classify_activity
from painter_maimai_daily.core.qzone.client import PublishResult
from painter_maimai_daily.core.state.proactive_state import ProactiveStateStore
from painter_maimai_daily.plugin import PainterMaimaiPlugin


IMAGE = base64.b64encode(b"offline-image-result").decode("ascii")
SESSION_ID = "existing-group-session"
GROUP_ID = "1001"
API_FIELDS = {"success", "image_base64", "model_id", "size", "is_img2img", "error", "selfie_style", "outfit"}


def make_plugin(tmp_path: Path, **overrides: Any) -> PainterMaimaiPlugin:
    """从内存注入 SDK 配置与能力替身，不读取任何实际账号或密钥。"""
    data = {
        "plugin": {"enabled": True, "config_version": "3.3.0"},
        "basic": {"prompt_optimizer_enabled": False},
        "selfie": {
            "auto_enabled": True, "caption_enabled": False, "caption_text": "记录一下此刻。",
            "quiet_hours_start": "00:00", "quiet_hours_end": "00:00", "group_enabled": False,
        },
        "group_image": {
            "enabled": True, "group_ids": [GROUP_ID], "min_messages": 2,
            "probability": 1.0, "quiet_hours_start": "00:00", "quiet_hours_end": "00:00",
        },
    }
    for section, fields in overrides.items():
        data[section].update(fields)
    plugin = PainterMaimaiPlugin()
    plugin.set_plugin_config(data)
    plugin._plugin_dir = str(tmp_path)
    plugin._set_context(SimpleNamespace(
        call_capability=AsyncMock(return_value={"result": True}),
        llm=SimpleNamespace(generate=AsyncMock(return_value={"success": True, "response": json.dumps({
            "destinations": ["qzone"], "reason": "休息时记录今天", "prompt": "在当前日程地点休息时的照片",
        })})),
        config=SimpleNamespace(get=AsyncMock(side_effect=lambda key, default="": default)),
        paths=PluginPaths(data_dir=tmp_path / "sdk-data", runtime_dir=tmp_path / "sdk-runtime"),
        chat=SimpleNamespace(
            get_stream_by_group_id=AsyncMock(return_value={
                "session_id": SESSION_ID, "platform": "qq", "group_id": GROUP_ID, "group_name": "测试群",
            }),
            get_stream_by_user_id=AsyncMock(return_value={
                "session_id": "private-session", "platform": "qq", "user_id": "u1",
                "user_nickname": "小明", "chat_type": "private",
            }),
        ),
        send=SimpleNamespace(image=AsyncMock(return_value=True), text=AsyncMock(return_value=True), hybrid=AsyncMock(return_value=True)),
    ))
    plugin.qzone_publisher = SimpleNamespace(
        available=True,
        publish=AsyncMock(return_value=PublishResult("sent", tid="test-tid")),
    )
    async def load_states() -> None:
        await plugin.runtime_state.load(plugin.ctx.paths.data_dir / "session_settings.json")
        await plugin.proactive_state.load(plugin.ctx.paths.data_dir / "proactive_images.json")

    asyncio.run(load_states())
    return plugin


def mock_schedule(monkeypatch: pytest.MonkeyPatch, module: Any, activity: Optional[ActivityInfo]) -> AsyncMock:
    lookup = AsyncMock(return_value=activity)
    monkeypatch.setattr(module, "get_schedule_provider", lambda ctx, **kwargs: SimpleNamespace(get_current_activity=lookup))
    return lookup


def scene_response(style: str = "photo") -> Dict[str, Any]:
    return {"success": True, "response": json.dumps({
        "action": "typing on a laptop", "environment": "office desk", "expression": "focused smile",
        "lighting": "soft daylight", "outfit": "neat work clothes", "selfie_style": style,
        "character_identity": "silver hair, blue eyes",
    })}


def messages() -> List[Dict[str, Any]]:
    return [
        {"message_id": f"message-{index}", "session_id": SESSION_ID,
         "timestamp": str(time.time()), "processed_plain_text": text}
        for index, text in enumerate(["刚做好的晚饭很好吃", "想看看这样的家常菜"])
    ]


def prepare_group(monkeypatch: pytest.MonkeyPatch, plugin: PainterMaimaiPlugin) -> GroupImageTask:
    mock_schedule(monkeypatch, group_image_task, resting_activity())
    plugin.ctx.call_capability.return_value = messages()
    monkeypatch.setattr(plugin, "api_generate_image", AsyncMock(return_value={"success": True, "image_base64": IMAGE}))
    plugin.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({
        "should_draw": True, "prompt": "一桌家常菜", "is_selfie": False, "style": "auto", "outfit": "围裙",
    })}
    return GroupImageTask(plugin)


def resting_activity(description: str = "饭后休息", **fields: Any) -> ActivityInfo:
    return ActivityInfo(ActivityType.RELAXING, description, as_of=datetime.now(timezone.utc).isoformat(), **fields)


def test_schedule_uses_public_maitrace_api_and_legacy_goal_type() -> None:
    ctx = SimpleNamespace(call_capability=AsyncMock(return_value={
        "has_activity": True,
        "activity": {"name": "工作", "description": "整理文档", "goal_type": "working", "time_window": "09:00-11:00"},
    }))
    activity = asyncio.run(ApiScheduleProvider(ctx).get_current_activity())
    assert activity == ActivityInfo(ActivityType.WORKING, "整理文档", time_point="09:00", time_window="09:00-11:00")
    ctx.call_capability.assert_awaited_once_with(
        "api.call", timeout_ms=20000, api_name="Rabbit-Jia-Er.MaiTrace.get_current_activity",
        version="1", args={"chat_id": "global"},
    )


def test_unavailable_schedule_returns_no_activity_and_cancel_propagates() -> None:
    ctx = SimpleNamespace(call_capability=AsyncMock(side_effect=TimeoutError("离线模拟日程超时")))
    assert asyncio.run(ApiScheduleProvider(ctx).get_current_activity()) is None
    ctx.call_capability.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ApiScheduleProvider(ctx).get_current_activity())


def test_maitrace_nested_schedule_parameters_reach_the_activity_payload() -> None:
    snapshot = {
        "has_activity": True, "as_of": "2026-10-23T14:05:00+08:00", "timezone": "Asia/Shanghai", "error": None,
        "activity": {
            "name": "午后休息", "description": "在公园读书休息", "goal_type": "rest",
            "time_window": "14:00-15:00", "goal_id": "afternoon-break",
            "parameters": {"outfit": "浅色毛衣", "location": "公园", "scene_hint": "坐在树荫下的长椅上"},
            "start_at": "2026-10-23T14:00:00+08:00", "end_at": "2026-10-23T15:00:00+08:00",
        },
        "next_activities": [{"time": "15:00", "name": "下午茶"}],
    }
    ctx = SimpleNamespace(call_capability=AsyncMock(return_value=snapshot))
    activity = asyncio.run(ApiScheduleProvider(ctx).get_current_activity())
    assert activity is not None
    assert (activity.outfit, activity.location, activity.scene) == ("浅色毛衣", "公园", "坐在树荫下的长椅上")
    assert (activity.time_window, activity.as_of, activity.timezone) == (
        "14:00-15:00", snapshot["as_of"], "Asia/Shanghai",
    )
    payload = activity.to_payload()
    assert payload["outfit"] == "浅色毛衣" and payload["location"] == "公园"
    assert payload["scene"] == "坐在树荫下的长椅上" and payload["next_activities"] == snapshot["next_activities"]
    ctx.call_capability.assert_awaited_once_with(
        "api.call", timeout_ms=20000, api_name="Rabbit-Jia-Er.MaiTrace.get_current_activity",
        version="1", args={"chat_id": "global"},
    )


@pytest.mark.parametrize("legacy_fields,expected", [
    ({"outfit": "旧服装", "location": "旧地点", "scene": "旧场景"}, ("旧服装", "旧地点", "旧场景")),
    ({"outfit": "  ", "location": "", "scene": ""}, ("日程服装", "日程地点", "日程场景")),
])
def test_legacy_activity_fields_take_priority_over_nested_defaults(
    legacy_fields: Dict[str, str], expected: Tuple[str, str, str],
) -> None:
    activity = ApiScheduleProvider._snapshot_to_activity({
        "has_activity": True,
        "activity": {
            "description": "休息", **legacy_fields,
            "parameters": {"outfit": "日程服装", "location": "日程地点", "scene_hint": "日程场景"},
        },
    })
    assert activity is not None
    assert (activity.outfit, activity.location, activity.scene) == expected


@pytest.mark.parametrize("parameters", [None, [], "公园"])
def test_invalid_maitrace_activity_parameters_are_reported(parameters: Any, caplog: pytest.LogCaptureFixture) -> None:
    ctx = SimpleNamespace(call_capability=AsyncMock(return_value={
        "has_activity": True, "activity": {"description": "休息", "parameters": parameters},
    }))
    assert asyncio.run(ApiScheduleProvider(ctx).get_current_activity()) is None
    assert "activity.parameters 必须为字典" in caplog.text


@pytest.mark.parametrize("goal_type,description,expected", [
    ("meal", "吃午饭", ActivityType.EATING),
    ("study", "阅读技术书", ActivityType.STUDYING),
    ("exercise", "慢跑", ActivityType.EXERCISING),
    ("learn_topic", "学习 Python", ActivityType.STUDYING),
    ("daily_routine", "刚刚睡醒", ActivityType.WAKING_UP),
    ("custom", "去公园跑步", ActivityType.EXERCISING),
    ("entertainment", "正在打游戏", ActivityType.GAMING),
])
def test_legacy_schedule_types_and_specific_chinese_activities(
    goal_type: str, description: str, expected: ActivityType,
) -> None:
    assert classify_activity(goal_type, description) == expected


def test_empty_configured_reference_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "reference.png").write_bytes(b"")
    with pytest.raises(ValueError, match="空文件"):
        load_reference_image(str(tmp_path), "reference.png")


def test_one_scene_llm_preserves_identity_and_replaces_reference_outfit(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    plugin.ctx.llm.generate.return_value = scene_response()
    result = asyncio.run(selfie_prompt_builder.build(
        ctx=plugin.ctx, selfie_style="auto", description="在办公室工作",
        activity_info=ActivityInfo(ActivityType.WORKING, "在办公室工作"),
        bot_appearance="silver hair, blue eyes, school uniform", api_format="gemini",
    ))
    plugin.ctx.llm.generate.assert_awaited_once()
    request = plugin.ctx.llm.generate.await_args.kwargs
    assert request["timeout_ms"] == 45000
    assert request["prompt"][0]["role"] == "system"
    payload = json.loads(request["prompt"][1]["content"])
    assert payload["activity_style"] == "photo"
    assert result.selfie_style == "photo"
    assert result.outfit == "neat work clothes"
    assert "silver hair" in result.prompt and "blue eyes" in result.prompt
    assert "school uniform" not in result.prompt
    assert re.search(r":[0-9]+(?:\.[0-9]+)?\)", result.prompt + result.negative_prompt) is None


@pytest.mark.parametrize("activity_type", [
    ActivityType.WORKING, ActivityType.STUDYING, ActivityType.EXERCISING,
    ActivityType.COOKING, ActivityType.GAMING, ActivityType.SHOPPING, ActivityType.SELF_CARE,
])
def test_optimizer_disabled_uses_the_same_activity_rules(activity_type: ActivityType, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    result = asyncio.run(selfie_prompt_builder.build(
        ctx=plugin.ctx, selfie_style="auto", description="当前活动", run_scene_optimizer=False,
        activity_info=ActivityInfo(activity_type, "当前活动"),
        bot_appearance="silver hair, blue eyes, school uniform", api_format="comfyui",
    ))
    assert result.selfie_style == choose_auto_style(ActivityInfo(activity_type, "当前活动"))
    assert result.outfit == ACTIVITY_OUTFITS[activity_type]
    assert "school uniform" not in result.prompt
    assert "solo character" in result.prompt
    assert re.search(r":1\.[3-9]\)", result.prompt) is None
    plugin.ctx.llm.generate.assert_not_awaited()


@pytest.mark.parametrize("response", [
    {"success": False, "response": ""},
    {"success": True, "response": "not-json"},
    scene_response("standard"),
])
def test_scene_failure_stops_pipeline_before_image_generation(response: Dict[str, Any], tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path, basic={"prompt_optimizer_enabled": True})
    plugin.ctx.llm.generate.return_value = response
    request = GenerationRequest(
        description="在办公室工作", is_selfie=True, selfie_style="auto", activity_type="working",
        resolved_model_config={}, api_format="openai", send_image=False,
    )
    generate_step = SimpleNamespace(name="generate", run=AsyncMock())
    result = asyncio.run(Pipeline([BuildSelfiePrompt(), generate_step]).run(request, SimpleNamespace(plugin=plugin)))
    assert not result.success
    assert "场景生成失败" in result.error
    generate_step.run.assert_not_awaited()


@pytest.mark.parametrize("api_format,syntax,explicit,expected", [
    ("comfyui", "auto", None, 0.6),
    ("comfyui", "natural", None, 0.6),
    ("openai", "auto", None, None),
    ("openai", "tags", None, 0.6),
    ("gemini", "auto", None, None),
    ("modelscope", "auto", None, None),
    ("comfyui", "auto", 0.85, 0.85),
])
def test_reference_strength_matches_actual_client_capability(
    api_format: str, syntax: str, explicit: Optional[float], expected: Optional[float],
    tmp_path: Path,
) -> None:
    plugin = make_plugin(tmp_path)
    request = GenerationRequest(
        description="工作照", is_selfie=True, selfie_style="auto", activity_type="working",
        input_image_base64=IMAGE, strength=explicit, api_format=api_format,
        resolved_model_config={"prompt_syntax": syntax, "selfie_reference_strength": 0.6},
    )
    asyncio.run(BuildSelfiePrompt().run(request, SimpleNamespace(plugin=plugin)))
    assert request.strength == expected


def test_generate_api_passes_explicit_activity_without_callback_or_send(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    captured: Dict[str, GenerationRequest] = {}

    async def run_pipeline(request: GenerationRequest, context: Any) -> GenerationRequest:
        captured["request"] = request
        request.success = True
        request.resolved_image_data = IMAGE
        request.selfie_style = "photo"
        return request

    plugin._action_pipeline = SimpleNamespace(run=AsyncMock(side_effect=run_pipeline))
    result = asyncio.run(plugin.api_generate_image(
        prompt="做饭时的照片", selfie_mode=True, selfie_style="auto",
        activity_type="cooking", activity_description="准备午饭", outfit="围裙", strength=0.8,
        model_id="model2", input_image_base64=IMAGE, use_cache=False,
        activity_location="厨房", activity_time_window="11:00-12:00",
        activity_as_of="2026-10-23T11:05:00+08:00", activity_timezone="Asia/Shanghai",
        activity_scene="在料理台旁准备食材",
    ))
    request = captured["request"]
    assert request.activity_type == "cooking" and request.activity_description == "准备午饭"
    assert request.outfit == "围裙" and request.strength == 0.8
    assert request.model_id == "model2" and request.input_image_base64 == IMAGE and not request.update_cache
    assert (request.activity_location, request.activity_time_window, request.activity_as_of, request.activity_timezone) == (
        "厨房", "11:00-12:00", "2026-10-23T11:05:00+08:00", "Asia/Shanghai",
    )
    assert request.activity_scene_description == "在料理台旁准备食材"
    assert request.source == "standalone" and request.stream_id == ""
    assert not request.send_image and not request.schedule_recall
    assert set(result) == API_FIELDS and result["selfie_style"] == "photo"
    plugin.ctx.call_capability.assert_not_awaited()
    plugin.ctx.send.image.assert_not_awaited()


@pytest.mark.parametrize("arguments", [
    {"prompt": ""}, {"prompt": "图片", "strength": 4},
    {"prompt": "图片", "selfie_style": "unknown"}, {"prompt": "图片", "activity_type": "unknown"},
])
def test_invalid_api_arguments_keep_the_public_response_shape(arguments: Dict[str, Any], tmp_path: Path) -> None:
    result = asyncio.run(make_plugin(tmp_path).api_generate_image(**arguments))
    assert set(result) == API_FIELDS
    assert result["success"] is False and result["error"]


def test_api_metadata_has_generation_budget(tmp_path: Path) -> None:
    component = next(item for item in make_plugin(tmp_path).get_components() if item["name"] == "generate_image")
    assert component["metadata"]["timeout_ms"] == 660000


def test_auto_selfie_requires_activity_and_skips_disabled_activity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    monkeypatch.setattr(plugin, "api_generate_image", AsyncMock())
    task = AutoSelfieTask(plugin)
    lookup = mock_schedule(monkeypatch, auto_selfie_task, None)
    asyncio.run(task._execute_selfie())
    lookup.return_value = ActivityInfo(ActivityType.SLEEPING, "睡觉")
    asyncio.run(task._execute_selfie())
    plugin.api_generate_image.assert_not_awaited()
    plugin.ctx.call_capability.assert_not_awaited()


def test_auto_selfie_uses_fixed_caption_and_publishes_existing_image_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    mock_schedule(monkeypatch, auto_selfie_task, resting_activity())
    monkeypatch.setattr(plugin, "api_generate_image", AsyncMock(return_value={"success": True, "image_base64": IMAGE}))
    asyncio.run(AutoSelfieTask(plugin)._execute_selfie())
    plugin.api_generate_image.assert_awaited_once()
    plugin.ctx.llm.generate.assert_awaited_once()
    plugin.qzone_publisher.publish.assert_awaited_once_with(plugin, IMAGE, "记录一下此刻。")
    assert plugin.proactive_state.recent()[-1]["status"] == "sent"


def test_auto_selfie_publishes_one_image_to_all_selected_channels(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    plugin = make_plugin(tmp_path, selfie={
        "group_enabled": True, "qzone_enabled": True,
        "private_enabled": True, "private_user_ids": ["u1"],
    })
    plugin.ctx.call_capability.return_value = messages()
    plugin.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({
        "destinations": ["qzone", "group_1", "private_1"],
        "reason": "吃完饭想记录一下", "prompt": "饭后在客厅休息的照片",
    })}
    mock_schedule(monkeypatch, auto_selfie_task, resting_activity())
    monkeypatch.setattr(plugin, "api_generate_image", AsyncMock(
        return_value={"success": True, "image_base64": IMAGE},
    ))

    asyncio.run(AutoSelfieTask(plugin)._execute_selfie())

    plugin.api_generate_image.assert_awaited_once()
    plugin.qzone_publisher.publish.assert_awaited_once_with(plugin, IMAGE, "记录一下此刻。")
    assert plugin.ctx.send.hybrid.await_count == 2
    sent_sessions = {call.args[1] for call in plugin.ctx.send.hybrid.await_args_list}
    assert sent_sessions == {SESSION_ID, "private-session"}
    assert [record["status"] for record in plugin.proactive_state.recent()] == ["sent", "sent", "sent"]


def test_auto_uncertain_space_result_is_recorded_without_retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    mock_schedule(monkeypatch, auto_selfie_task, resting_activity())
    monkeypatch.setattr(plugin, "api_generate_image", AsyncMock(return_value={"success": True, "image_base64": IMAGE}))
    plugin.qzone_publisher.publish.return_value = PublishResult("uncertain", error="发布结果待核对")
    task = AutoSelfieTask(plugin)
    asyncio.run(task._execute_selfie())
    assert task._last_attempt_at is not None
    plugin.qzone_publisher.publish.assert_awaited_once()
    assert plugin.proactive_state.recent()[-1]["status"] == "unknown"


def test_auto_rejected_space_result_is_recorded_as_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    mock_schedule(monkeypatch, auto_selfie_task, resting_activity())
    monkeypatch.setattr(plugin, "api_generate_image", AsyncMock(return_value={"success": True, "image_base64": IMAGE}))
    plugin.qzone_publisher.publish.return_value = PublishResult("rejected", error="空间发布被拒绝")
    asyncio.run(AutoSelfieTask(plugin)._execute_selfie())
    assert plugin.proactive_state.recent()[-1]["status"] == "failed"
    plugin.qzone_publisher.publish.assert_awaited_once()


def test_auto_cancel_during_generation_never_publishes_late_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    mock_schedule(monkeypatch, auto_selfie_task, resting_activity())

    async def scenario():
        started = asyncio.Event()
        released = asyncio.Event()

        async def generate(**kwargs: Any) -> Dict[str, Any]:
            started.set()
            await released.wait()
            return {"success": True, "image_base64": IMAGE}

        monkeypatch.setattr(plugin, "api_generate_image", AsyncMock(side_effect=generate))
        task = AutoSelfieTask(plugin)
        task.is_running = True
        task.task = asyncio.create_task(task._execute_selfie())
        await started.wait()
        await task.stop()
        released.set()
        await asyncio.sleep(0)
        plugin.ctx.call_capability.assert_not_awaited()

    asyncio.run(scenario())


def test_group_picture_uses_real_session_and_ordinary_image_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    task = prepare_group(monkeypatch, plugin)
    asyncio.run(task._process_group(GROUP_ID))
    call = plugin.ctx.call_capability.await_args
    assert call.args == ("message.get_recent",)
    assert call.kwargs["chat_id"] == SESSION_ID
    assert call.kwargs["filter_mai"] is True and call.kwargs["hours"] == 0.5
    generation = plugin.api_generate_image.await_args.kwargs
    assert generation["selfie_mode"] is False and generation["outfit"] == ""
    assert generation["activity_type"] == "relaxing"
    plugin.ctx.send.image.assert_awaited_once_with(IMAGE, SESSION_ID, timeout_ms=30000)
    decision_prompt = plugin.ctx.llm.generate.await_args.kwargs["prompt"]
    assert decision_prompt[0]["role"] == "system"
    assert decision_prompt[1]["role"] == "user"


@pytest.mark.parametrize("reason", ["not_allowed", "quiet", "probability", "too_few", "sleeping", "missing_schedule", "missing_stream"])
def test_group_gates_prevent_generation_and_send(monkeypatch: pytest.MonkeyPatch, reason: str, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    task = prepare_group(monkeypatch, plugin)
    if reason == "not_allowed":
        plugin.config.group_image.group_ids = []
    elif reason == "quiet":
        plugin.config.group_image.quiet_hours_end = "23:59"
        monkeypatch.setattr(proactive_context, "is_in_time_range", lambda start, end: True)
    elif reason == "probability":
        plugin.config.group_image.probability = 0
    elif reason == "too_few":
        plugin.ctx.call_capability.return_value = messages()[:1]
    elif reason == "sleeping":
        mock_schedule(monkeypatch, group_image_task, ActivityInfo(ActivityType.SLEEPING, "睡觉"))
    elif reason == "missing_schedule":
        mock_schedule(monkeypatch, group_image_task, None)
    elif reason == "missing_stream":
        plugin.ctx.chat.get_stream_by_group_id.return_value = None
    asyncio.run(task._process_group(GROUP_ID))
    plugin.api_generate_image.assert_not_awaited()
    plugin.ctx.send.image.assert_not_awaited()


def test_consumed_context_is_not_sent_again_after_cooldown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    task = prepare_group(monkeypatch, plugin)
    fixed_time = time.time()
    asyncio.run(task._process_group(GROUP_ID))
    asyncio.run(task._process_group(GROUP_ID))
    monkeypatch.setattr(proactive_context.time, "time", lambda: fixed_time + plugin.config.group_image.cooldown_minutes * 60 + 1)
    asyncio.run(task._process_group(GROUP_ID))
    plugin.api_generate_image.assert_awaited_once()
    plugin.ctx.send.image.assert_awaited_once()


@pytest.mark.parametrize("response", [
    '{"should_draw":"false"}',
    '{"should_draw":true,"is_selfie":"false","prompt":"food","style":"photo"}',
    '{"should_draw":true,"is_selfie":false,"prompt":"","style":"photo"}',
    '{"should_draw":true,"is_selfie":false,"prompt":"food","style":"other"}',
])
def test_group_decision_rejects_malformed_schema(response: str) -> None:
    with pytest.raises(ValueError):
        parse_decision(response)


def test_recent_context_discards_wrong_sessions_old_messages_and_duplicates() -> None:
    good = messages()[0]
    items = [
        good, good.copy(), {**good, "session_id": "another-session"},
        {**good, "message_id": "old", "timestamp": 1},
        {**good, "message_id": "invalid", "timestamp": "nan"},
        {**good, "message_id": None}, {**good, "session_id": None},
    ]
    assert recent_context(items, session_id=SESSION_ID, earliest=time.time() - 60) == [
        {"message_id": good["message_id"], "text": good["processed_plain_text"]},
    ]


def test_group_rechecks_configuration_before_sending(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    task = prepare_group(monkeypatch, plugin)

    async def disable_then_return(**kwargs: Any) -> Dict[str, Any]:
        plugin.config.group_image.enabled = False
        return {"success": True, "image_base64": IMAGE}

    plugin.api_generate_image.side_effect = disable_then_return
    asyncio.run(task._process_group(GROUP_ID))
    plugin.ctx.send.image.assert_not_awaited()


def test_unknown_group_send_result_is_not_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    task = prepare_group(monkeypatch, plugin)
    plugin.ctx.send.image.side_effect = TimeoutError("投递状态未知")
    with pytest.raises(TimeoutError):
        asyncio.run(task._process_group(GROUP_ID))
    asyncio.run(task._process_group(GROUP_ID))
    plugin.api_generate_image.assert_awaited_once()
    plugin.ctx.send.image.assert_awaited_once()


def test_pipeline_deadline_stops_following_send_step(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)

    async def scenario():
        started = asyncio.Event()

        async def blocked_generation(request: GenerationRequest, context: Any) -> None:
            started.set()
            await asyncio.Event().wait()

        # 只缩短测试替身的期限，避免等待配置允许的最小 10 秒。
        fake_plugin = SimpleNamespace(config=SimpleNamespace(
            basic=SimpleNamespace(generation_timeout_seconds=0.01),
            quota=SimpleNamespace(enabled=False),
        ))
        generation = SimpleNamespace(name="generation", run=AsyncMock(side_effect=blocked_generation))
        send = SimpleNamespace(name="send", run=plugin.ctx.send.image)
        result = await Pipeline([generation, send]).run(GenerationRequest(), SimpleNamespace(plugin=fake_plugin))
        assert started.is_set() and result.success is False
        assert "超过总时限" in result.error
        send.run.assert_not_awaited()

    asyncio.run(scenario())


def test_config_reload_cancels_old_tasks_and_preserves_cooldown(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)

    async def scenario():
        auto = AutoSelfieTask(plugin)
        group = GroupImageTask(plugin)
        plugin._auto_selfie_task = auto
        plugin._group_image_task = group
        auto._last_attempt_at = 123.0
        token = await plugin.proactive_state.reserve(
            source="group_image", target=f"group:{SESSION_ID}", cooldown_seconds=60,
            messages=[{"message_id": "message-already-used", "text": "旧语境"}], min_messages=1,
        )
        await plugin.proactive_state.finish(token, "skipped")
        history = plugin.proactive_state.recent()
        await auto.start()
        await group.start()
        old_auto, old_group = auto.task, group.task
        try:
            await plugin.on_config_update("self", {}, "3.3.0")
            assert old_auto.cancelled() and old_group.cancelled()
            assert plugin._auto_selfie_task is auto and plugin._group_image_task is group
            assert auto.task is not old_auto and group.task is not old_group
            assert auto._last_attempt_at == 123.0
            assert plugin.proactive_state.recent() == history
            assert not plugin.proactive_state.unconsumed(f"group:{SESSION_ID}", [{"message_id": "message-already-used"}])
        finally:
            await plugin.on_unload()

    asyncio.run(scenario())


def test_failed_group_start_cleans_up_the_already_started_auto_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = make_plugin(tmp_path)

    async def fail_start(task: GroupImageTask) -> None:
        raise RuntimeError("模拟群任务启动失败")

    monkeypatch.setattr(GroupImageTask, "start", fail_start)

    async def scenario():
        plugin._plugin_dir = str(tmp_path)
        plugin.ctx.paths = PluginPaths(data_dir=tmp_path / "sdk-data", runtime_dir=tmp_path / "sdk-runtime")
        with pytest.raises(RuntimeError, match="群任务启动失败"):
            await plugin.on_load()
        assert plugin._auto_selfie_task is not None
        assert plugin._auto_selfie_task.task is None
        assert plugin._auto_selfie_task.is_running is False
        assert plugin._group_image_task.task is None

    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["selfie", "group"])
@pytest.mark.parametrize("condition", ["missing", "no_time", "stale", "busy", "low_energy", "sleep_phase"])
def test_all_proactive_images_require_an_eligible_schedule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, condition: str,
) -> None:
    plugin = make_plugin(tmp_path)
    activity = resting_activity()
    if condition == "missing":
        activity = None
    elif condition == "no_time":
        activity.as_of = ""
    elif condition == "stale":
        activity.as_of = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    elif condition == "busy":
        activity.activity_type = ActivityType.WORKING
        activity.description = "专注整理技术文档"
    elif condition == "low_energy":
        activity.energy = 20
    elif condition == "sleep_phase":
        activity.sleep_phase = "deep_sleep"
    if source == "group":
        task = prepare_group(monkeypatch, plugin)
        mock_schedule(monkeypatch, group_image_task, activity)
        asyncio.run(task._process_group(GROUP_ID))
    else:
        mock_schedule(monkeypatch, auto_selfie_task, activity)
        monkeypatch.setattr(plugin, "api_generate_image", AsyncMock())
        asyncio.run(AutoSelfieTask(plugin)._execute_selfie())
    plugin.ctx.llm.generate.assert_not_awaited()
    plugin.api_generate_image.assert_not_awaited()
    plugin.ctx.send.image.assert_not_awaited()
    plugin.ctx.send.hybrid.assert_not_awaited()
    assert plugin.proactive_state.recent() == []
    assert plugin.proactive_state.available(f"group:{SESSION_ID}", 3600)


@pytest.mark.parametrize("source", ["selfie", "group"])
@pytest.mark.parametrize("change", ["missing", "busy", "activity", "location", "outfit", "scene"])
def test_proactive_results_are_discarded_when_schedule_changes_during_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str, change: str,
) -> None:
    plugin = make_plugin(tmp_path)
    original = resting_activity(location="客厅", outfit="休闲服")
    current = resting_activity(location="客厅", outfit="休闲服")
    if change == "missing":
        current = None
    elif change == "busy":
        current.activity_type = ActivityType.COOKING
        current.description = "正在做饭"
    elif change == "activity":
        current.description = "公园散步"
    elif change == "location":
        current.location = "公园"
    elif change == "outfit":
        current.outfit = "运动服"
    elif change == "scene":
        current.scene = "整理刚吃完饭的桌子"
    if source == "group":
        task = prepare_group(monkeypatch, plugin)
        lookup = mock_schedule(monkeypatch, group_image_task, original)
        lookup.side_effect = [original, current]
        asyncio.run(task._process_group(GROUP_ID))
    else:
        monkeypatch.setattr(plugin, "api_generate_image", AsyncMock(return_value={"success": True, "image_base64": IMAGE}))
        lookup = mock_schedule(monkeypatch, auto_selfie_task, original)
        lookup.side_effect = [original, current]
        asyncio.run(AutoSelfieTask(plugin)._execute_selfie())
        plugin.ctx.call_capability.assert_not_awaited()
    assert lookup.await_count == 2
    plugin.api_generate_image.assert_awaited_once()
    plugin.ctx.send.image.assert_not_awaited()
    plugin.ctx.send.hybrid.assert_not_awaited()
    assert plugin.proactive_state.recent()[-1]["status"] == "skipped"


def test_group_skip_consumes_context_without_blocking_an_appropriate_selfie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    plugin = make_plugin(tmp_path, selfie={"group_enabled": True, "qzone_enabled": False})
    task = prepare_group(monkeypatch, plugin)
    activity = resting_activity()
    mock_schedule(monkeypatch, group_image_task, activity)
    mock_schedule(monkeypatch, auto_selfie_task, activity)
    plugin.ctx.llm.generate.return_value = {"success": True, "response": '{"should_draw":false,"reason":"话题不需要插图"}'}

    async def scenario() -> None:
        await task._process_group(GROUP_ID)
        await task._process_group(GROUP_ID)
        assert plugin.ctx.llm.generate.await_count == 1
        assert not plugin.proactive_state.unconsumed(f"group:{SESSION_ID}", [{"message_id": "message-0"}])
        assert plugin.proactive_state.available(f"group:{SESSION_ID}", 3600)
        assert plugin.proactive_state.recent() == []
        plugin.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({
            "destinations": ["group_1"], "reason": "群里问起饭后近况", "prompt": "在当前地点饭后休息的照片",
        })}
        await AutoSelfieTask(plugin)._execute_selfie()

    asyncio.run(scenario())
    plugin.api_generate_image.assert_awaited_once()
    plugin.ctx.send.hybrid.assert_awaited_once()
    assert plugin.proactive_state.recent()[-1]["status"] == "sent"


def test_failed_group_decision_preserves_consumption_across_restart_without_publish_cooldown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    plugin = make_plugin(tmp_path)
    task = prepare_group(monkeypatch, plugin)
    plugin.ctx.llm.generate.side_effect = TimeoutError("判断模型超时")

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await task._process_group(GROUP_ID)
        restarted = ProactiveStateStore()
        await restarted.load(plugin.ctx.paths.data_dir / "proactive_images.json")
        assert restarted.available(f"group:{SESSION_ID}", 3600)
        assert restarted.unconsumed(f"group:{SESSION_ID}", [{"message_id": "message-0"}]) == []
        assert restarted.recent() == []
        assert not await restarted.claim_group_check(f"group:{SESSION_ID}", [{"message_id": "message-0"}], 1, 0)

    asyncio.run(scenario())
    plugin.api_generate_image.assert_not_awaited()


def test_inflight_group_decision_does_not_reserve_target_and_loses_to_a_published_selfie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    plugin = make_plugin(tmp_path, selfie={"group_enabled": True, "qzone_enabled": False})
    group = prepare_group(monkeypatch, plugin)
    mock_schedule(monkeypatch, auto_selfie_task, resting_activity())
    plugin.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({
        "destinations": ["group_1"], "reason": "回应近况问题", "prompt": "饭后休息的照片",
    })}

    async def scenario() -> None:
        deciding, released = asyncio.Event(), asyncio.Event()

        async def decide(*args: Any) -> Dict[str, Any]:
            deciding.set()
            await released.wait()
            return {"should_draw": True, "prompt": "家常菜", "is_selfie": False, "style": "auto", "outfit": ""}

        monkeypatch.setattr(group, "_decide", AsyncMock(side_effect=decide))
        pending = asyncio.create_task(group._process_group(GROUP_ID))
        await deciding.wait()
        await AutoSelfieTask(plugin)._execute_selfie()
        released.set()
        await pending

    asyncio.run(scenario())
    plugin.api_generate_image.assert_awaited_once()
    plugin.ctx.send.hybrid.assert_awaited_once()
    plugin.ctx.send.image.assert_not_awaited()


def test_group_selfie_caption_receives_actual_scene_plan_and_is_sent_with_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    plugin = make_plugin(tmp_path, selfie={"caption_enabled": True})
    task = prepare_group(monkeypatch, plugin)
    plan = {
        "identity": "silver hair", "environment": "living room", "action": "resting with a cup of tea",
        "outfit": "casual clothes", "expression": "relaxed smile", "lighting": "soft indoor lighting",
        "style": "standard", "framing": "upper-body portrait",
    }
    plugin.api_generate_image.return_value = {"success": True, "image_base64": IMAGE, "photo_plan": plan, "outfit": "casual clothes"}
    plugin.ctx.llm.generate.side_effect = [
        {"success": True, "response": json.dumps({
            "should_draw": True, "reason": "群里正在问饭后近况", "prompt": "饭后在客厅喝茶休息",
            "is_selfie": True, "style": "auto", "outfit": "casual clothes",
        })},
        {"success": True, "response": "吃完饭喝杯茶，歇一会儿～"},
    ]
    asyncio.run(task._process_group(GROUP_ID))
    payload = json.loads(plugin.ctx.llm.generate.await_args_list[-1].kwargs["prompt"][1]["content"])
    assert payload["photo_plan"] == plan and payload["destination"] == "group"
    assert payload["group_messages"]
    assert plugin.api_generate_image.await_args.kwargs["include_photo_plan"] is True
    plugin.ctx.send.hybrid.assert_awaited_once_with(
        [{"type": "image", "content": IMAGE}, {"type": "text", "content": "吃完饭喝杯茶，歇一会儿～"}],
        SESSION_ID, timeout_ms=30000,
    )
    plugin.ctx.send.image.assert_not_awaited()


def test_opt_in_photo_plan_preserves_default_public_api_contract(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    plan = {"environment": "park", "action": "reading a book"}

    async def generate(request: GenerationRequest, context: Any) -> GenerationRequest:
        request.success = True
        request.resolved_image_data = IMAGE
        request.extras["photo_plan"] = plan
        return request

    plugin._action_pipeline = SimpleNamespace(run=AsyncMock(side_effect=generate))

    async def scenario() -> None:
        normal = await plugin.api_generate_image(prompt="树下读书", selfie_mode=True)
        with_plan = await plugin.api_generate_image(prompt="树下读书", selfie_mode=True, include_photo_plan=True)
        assert set(normal) == API_FIELDS
        assert set(with_plan) == API_FIELDS | {"photo_plan"}
        assert with_plan["photo_plan"] == plan

    asyncio.run(scenario())
    plugin.ctx.call_capability.assert_not_awaited()
    plugin.ctx.send.image.assert_not_awaited()


@pytest.mark.parametrize("time_window,as_of,expected", [
    ("23:00-01:00", "2026-10-23T00:30:00+08:00", ""),
    ("23:00-01:00", "2026-10-23T00:10:00+08:00", "过期"),
    ("23:00-01:00", "2026-10-23T00:32:00+08:00", "未来"),
    ("02:00-03:00", "2026-10-23T00:30:00+08:00", "时间窗"),
    ("24:01-25:00", "2026-10-23T00:30:00+08:00", "时间无效"),
    ("00:00-00:00", "2026-10-23T00:30:00+08:00", "时间无效"),
    ("", "", "缺少"),
])
def test_schedule_timing_rejects_stale_invalid_and_out_of_window_snapshots(
    time_window: str, as_of: str, expected: str,
) -> None:
    activity = ActivityInfo(ActivityType.RELAXING, "休息", time_window=time_window, as_of=as_of, timezone="Asia/Shanghai")
    now = datetime.fromisoformat("2026-10-23T00:30:00+08:00")
    reason = activity.publication_block_reason(now=now)
    assert (reason == "") if not expected else expected in reason
    if not expected:
        assert activity.remaining_seconds(now=now) == 1800


def test_fresh_life_energy_blocks_even_if_scene_belongs_to_a_previous_activity() -> None:
    snapshot = {
        "has_activity": True, "as_of": datetime.now(timezone.utc).isoformat(),
        "activity": {"activity_type": "relaxing", "description": "公园休息"},
    }
    life = {"enabled": True, "last_updated_at": time.time(), "energy": 10, "current_activity": "之前在厨房", "scene": "kitchen"}
    ctx = SimpleNamespace(call_capability=AsyncMock(side_effect=[snapshot, life]))
    activity = asyncio.run(ApiScheduleProvider(ctx, include_life=True).get_current_activity())
    assert activity.energy == 10 and activity.scene == ""
    assert not activity.can_publish()


def test_stale_life_state_does_not_overwrite_a_valid_current_schedule() -> None:
    snapshot = {
        "has_activity": True, "as_of": datetime.now(timezone.utc).isoformat(),
        "activity": {"activity_type": "relaxing", "description": "公园休息"},
    }
    life = {"enabled": True, "last_updated_at": time.time() - 3600, "energy": 0, "sleep_phase": "deep_sleep"}
    ctx = SimpleNamespace(call_capability=AsyncMock(side_effect=[snapshot, life]))
    activity = asyncio.run(ApiScheduleProvider(ctx, include_life=True).get_current_activity())
    assert activity.energy is None and activity.sleep_phase == "" and activity.can_publish()


@pytest.mark.parametrize("life_description,life_scene,expected_scene", [
    ("公园休息", "", "树荫下的长椅"),
    ("公园休息", "  ", "树荫下的长椅"),
    ("公园休息", "坐在长椅上喝茶", "坐在长椅上喝茶"),
    ("之前在厨房", "在厨房做饭", "树荫下的长椅"),
])
def test_life_scene_only_enriches_the_same_activity_with_nonempty_details(
    life_description: str, life_scene: str, expected_scene: str,
) -> None:
    snapshot = {
        "has_activity": True,
        "activity": {
            "description": "公园休息",
            "parameters": {"location": "公园", "outfit": "休闲服", "scene_hint": "树荫下的长椅"},
        },
    }
    life = {
        "enabled": True, "last_updated_at": time.time(), "energy": 70, "sleep_phase": "awake",
        "current_activity": life_description, "current_location": "生活状态地点", "scene": life_scene,
    }
    ctx = SimpleNamespace(call_capability=AsyncMock(side_effect=[snapshot, life]))
    activity = asyncio.run(ApiScheduleProvider(ctx, include_life=True).get_current_activity())
    assert activity is not None
    assert activity.scene == expected_scene and activity.location == "公园" and activity.outfit == "休闲服"
    assert activity.energy == 70 and activity.sleep_phase == "awake"
    assert ctx.call_capability.await_count == 2
    assert ctx.call_capability.await_args.kwargs == {
        "timeout_ms": 10000, "api_name": "Rabbit-Jia-Er.MaiTrace.get_life_state", "version": "1", "args": {},
    }


@pytest.mark.parametrize("life_error", [
    {"success": False, "error": "API_NOT_FOUND"},
    {"error": "service_unavailable"},
])
def test_life_api_failure_is_logged_and_preserves_valid_schedule(
    life_error: Dict[str, Any], caplog: pytest.LogCaptureFixture,
) -> None:
    snapshot = {
        "has_activity": True,
        "activity": {"description": "公园休息", "parameters": {"scene_hint": "树荫下的长椅"}},
    }
    ctx = SimpleNamespace(call_capability=AsyncMock(side_effect=[snapshot, life_error]))
    activity = asyncio.run(ApiScheduleProvider(ctx, include_life=True).get_current_activity())
    assert activity is not None and activity.scene == "树荫下的长椅" and activity.energy is None
    assert "本轮仅使用日程" in caplog.text and life_error["error"] in caplog.text


def test_life_api_cancellation_propagates() -> None:
    ctx = SimpleNamespace(call_capability=AsyncMock(side_effect=[
        {"has_activity": True, "activity": {"description": "公园休息"}}, asyncio.CancelledError(),
    ]))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ApiScheduleProvider(ctx, include_life=True).get_current_activity())


def test_specific_description_is_not_hidden_by_a_legacy_coarse_activity_type() -> None:
    assert classify_activity("rest", "睡觉") == ActivityType.SLEEPING
    assert classify_activity("rest", "洗澡") == ActivityType.BATHING
    assert classify_activity("meal", "正在做饭") == ActivityType.COOKING
