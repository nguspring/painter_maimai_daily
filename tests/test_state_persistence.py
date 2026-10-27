"""会话覆盖项持久化：重启、隔离、原子提交、取消与命令反馈。"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock

import asyncio
import json
import pytest
import threading

from painter_maimai_daily.core.commands.dispatcher import CommandDispatcher, DispatcherContext
from painter_maimai_daily.core.commands.help_renderer import render_current_config
from painter_maimai_daily.core.pipeline.factories import build_request_from_tool_kwargs
from painter_maimai_daily.core.pipeline.request import GenerationRequest
from painter_maimai_daily.core.pipeline.steps.resolve_model import ResolveModel
from painter_maimai_daily.core.state import RuntimeStateManager, StatePersistenceError
from painter_maimai_daily.core.state import runtime_state as state_module
from painter_maimai_daily.plugin import PainterMaimaiPlugin
from maibot_sdk import PluginPaths


SESSION_ID = "existing-group-session"
OTHER_SESSION_ID = "existing-private-session"


def make_plugin(tmp_path: Path) -> PainterMaimaiPlugin:
    """只注入内存配置和消息替身，持久化文件全部落在测试临时目录。"""
    plugin = PainterMaimaiPlugin()
    plugin.set_plugin_config({
        "plugin": {"enabled": True, "config_version": PainterMaimaiPlugin.config_model().plugin.config_version},
        "basic": {
            "admin_users": ["admin"], "default_txt2img_model": "model1", "pic_command_model": "model1",
            "prompt_optimizer_enabled": False, "auto_recall_enabled": False,
        },
        "selfie": {"auto_enabled": False, "schedule_enabled": False},
        "group_image": {"enabled": False},
        "models": {"items": [
            {"id": "model1", "name": "默认测试模型"},
            {"id": "model2", "name": "会话测试模型"},
        ]},
    })
    plugin._plugin_dir = str(tmp_path)
    plugin._set_context(SimpleNamespace(
        paths=PluginPaths(data_dir=tmp_path / "sdk-data", runtime_dir=tmp_path / "sdk-runtime"),
        send=SimpleNamespace(
            text=AsyncMock(return_value=True),
            image=AsyncMock(return_value={"sent": True, "message_id": "generated-message"}),
        ),
    ))
    return plugin


def test_all_overrides_survive_restart_and_stay_in_their_session(tmp_path: Path) -> None:
    path = tmp_path / "data" / "session_settings.json"

    async def scenario() -> None:
        state = RuntimeStateManager()
        await state.load(path)
        await state.set_plugin_enabled(SESSION_ID, False)
        await state.set_model_enabled(SESSION_ID, "model2", False)
        await state.set_recall_enabled(SESSION_ID, "model3", False)
        await state.set_action_default_model(SESSION_ID, "model4")
        await state.set_command_default_model(SESSION_ID, "model5")
        await state.set_selfie_schedule_enabled(SESSION_ID, True)
        await state.set_selfie_style(SESSION_ID, "mirror")
        before = state.get_chat_state_summary(SESSION_ID)
        await state.close()

        restarted = RuntimeStateManager()
        await restarted.load(path)
        assert restarted.get_chat_state_summary(SESSION_ID) == before
        assert restarted.is_plugin_enabled(SESSION_ID, True) is False
        assert restarted.is_model_enabled(SESSION_ID, "model2") is False
        assert restarted.is_recall_enabled(SESSION_ID, "model3", True) is False
        assert restarted.get_action_default_model(SESSION_ID, "global") == "model4"
        assert restarted.get_command_default_model(SESSION_ID, "global") == "model5"
        assert restarted.is_selfie_schedule_enabled(SESSION_ID, False) is True
        assert restarted.get_selfie_style(SESSION_ID, "auto") == "mirror"
        assert restarted.is_plugin_enabled(OTHER_SESSION_ID, True) is True
        assert restarted.get_command_default_model(OTHER_SESSION_ID, "global") == "global"

        # 读取结果是副本，展示层修改集合或列表不能更改已保存的设置。
        restarted.get_disabled_models(SESSION_ID).clear()
        restarted.get_chat_state_summary(SESSION_ID)["disabled_models"].clear()
        assert restarted.is_model_enabled(SESSION_ID, "model2") is False
        await restarted.close()

    asyncio.run(scenario())


def test_reading_defaults_does_not_create_records_and_tracks_global_changes(tmp_path: Path) -> None:
    path = tmp_path / "data" / "session_settings.json"

    async def scenario() -> None:
        state = RuntimeStateManager()
        await state.load(path)
        assert state.get_action_default_model(SESSION_ID, "old-global") == "old-global"
        assert state.get_action_default_model(SESSION_ID, "new-global") == "new-global"
        assert state.is_plugin_enabled(SESSION_ID, False) is False
        assert state.is_plugin_enabled(SESSION_ID, True) is True
        assert state.get_selfie_style(SESSION_ID, "auto") == "auto"
        assert state.get_selfie_style(SESSION_ID, "photo") == "photo"
        assert not path.exists()

        await state.set_selfie_style(OTHER_SESSION_ID, "mirror")
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert set(stored["sessions"]) == {OTHER_SESSION_ID}

    asyncio.run(scenario())


def test_instances_are_independent_and_repeated_load_preserves_overrides(tmp_path: Path) -> None:
    async def scenario() -> None:
        first, second = RuntimeStateManager(), RuntimeStateManager()
        first_path = tmp_path / "first" / "session_settings.json"
        second_path = tmp_path / "second" / "session_settings.json"
        await first.load(first_path)
        await second.load(second_path)
        await first.set_command_default_model(SESSION_ID, "first-model")
        await second.set_command_default_model(SESSION_ID, "second-model")
        await first.load(first_path)
        assert first.get_command_default_model(SESSION_ID, "global") == "first-model"
        assert second.get_command_default_model(SESSION_ID, "global") == "second-model"
        with pytest.raises(StatePersistenceError, match="不能切换"):
            await first.load(second_path)

    asyncio.run(scenario())


def test_reset_removes_saved_overrides_and_follows_new_global_values(tmp_path: Path) -> None:
    path = tmp_path / "session_settings.json"

    async def scenario() -> None:
        state = RuntimeStateManager()
        await state.load(path)
        await state.set_plugin_enabled(SESSION_ID, False)
        await state.set_command_default_model(SESSION_ID, "custom-model")
        await state.set_model_enabled(SESSION_ID, "model2", False)
        await state.set_recall_enabled(SESSION_ID, "model2", False)
        await state.set_selfie_schedule_enabled(SESSION_ID, True)
        await state.set_selfie_style(SESSION_ID, "mirror")
        await state.set_selfie_style(OTHER_SESSION_ID, "photo")
        await state.reset_chat_state(SESSION_ID)
        assert SESSION_ID not in json.loads(path.read_text(encoding="utf-8"))["sessions"]
        await state.close()

        restarted = RuntimeStateManager()
        await restarted.load(path)
        assert restarted.is_plugin_enabled(SESSION_ID, True) is True
        assert restarted.get_command_default_model(SESSION_ID, "new-global") == "new-global"
        assert restarted.is_model_enabled(SESSION_ID, "model2") is True
        assert restarted.is_recall_enabled(SESSION_ID, "model2", False) is False
        assert restarted.is_selfie_schedule_enabled(SESSION_ID, False) is False
        assert restarted.get_selfie_style(SESSION_ID, "auto") == "auto"
        assert restarted.get_selfie_style(OTHER_SESSION_ID, "auto") == "photo"

    asyncio.run(scenario())


@pytest.mark.parametrize("contents", [
    "{",
    "{}",
    '{"version": 2, "sessions": {}}',
    '{"version": true, "sessions": {}}',
    '{"version": 1, "sessions": []}',
    '{"version": 1, "sessions": {"": {}}}',
    '{"version": 1, "sessions": {"existing-session": {"plugin_enabled": "false"}}}',
])
def test_bad_store_is_reported_and_never_replaced_with_empty_state(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "session_settings.json"
    path.write_text(contents, encoding="utf-8")

    async def scenario() -> None:
        state = RuntimeStateManager()
        with pytest.raises(StatePersistenceError, match="损坏或格式不受支持"):
            await state.load(path)
        with pytest.raises(StatePersistenceError, match="尚未加载"):
            await state.set_plugin_enabled(SESSION_ID, False)
        assert path.read_text(encoding="utf-8") == contents

    asyncio.run(scenario())


def test_loading_and_saving_require_explicit_lifecycle_and_real_session_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        state = RuntimeStateManager()
        with pytest.raises(StatePersistenceError, match="尚未加载"):
            state.is_plugin_enabled(SESSION_ID, True)
        with pytest.raises(StatePersistenceError, match="尚未加载"):
            await state.set_plugin_enabled(SESSION_ID, False)
        with pytest.raises(ValueError, match="绝对路径"):
            await state.load(Path("session_settings.json"))

        path = tmp_path / "session_settings.json"
        await state.load(path)
        for invalid in ("", " ", " existing-session"):
            with pytest.raises(ValueError, match="真实 session_id"):
                await state.set_plugin_enabled(invalid, False)
        assert not path.exists()
        await state.close()
        with pytest.raises(StatePersistenceError, match="已经卸载"):
            state.get_command_default_model(SESSION_ID, "global")

    asyncio.run(scenario())


def test_failed_atomic_replace_preserves_memory_file_and_removes_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "session_settings.json"

    async def scenario() -> None:
        state = RuntimeStateManager()
        await state.load(path)
        await state.set_selfie_style(SESSION_ID, "mirror")
        original_bytes = path.read_bytes()

        def reject_replace(source: Any, destination: Any) -> None:
            raise PermissionError("模拟目标文件不可写")

        monkeypatch.setattr(state_module.os, "replace", reject_replace)
        with pytest.raises(StatePersistenceError, match="目标文件不可写"):
            await state.set_selfie_style(SESSION_ID, "photo")
        assert state.get_selfie_style(SESSION_ID) == "mirror"
        assert path.read_bytes() == original_bytes
        assert list(tmp_path.glob("*.tmp")) == []

        restarted = RuntimeStateManager()
        await restarted.load(path)
        assert restarted.get_selfie_style(SESSION_ID) == "mirror"

    asyncio.run(scenario())


def test_concurrent_updates_keep_all_fields_and_sessions(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "session_settings.json"
        state = RuntimeStateManager()
        await state.load(path)
        await asyncio.gather(
            state.set_action_default_model(SESSION_ID, "model2"),
            state.set_command_default_model(SESSION_ID, "model3"),
            state.set_selfie_style(SESSION_ID, "photo"),
            state.set_model_enabled(SESSION_ID, "model4", False),
            state.set_selfie_style(OTHER_SESSION_ID, "mirror"),
        )
        restarted = RuntimeStateManager()
        await restarted.load(path)
        assert restarted.get_action_default_model(SESSION_ID, "global") == "model2"
        assert restarted.get_command_default_model(SESSION_ID, "global") == "model3"
        assert restarted.get_selfie_style(SESSION_ID) == "photo"
        assert restarted.is_model_enabled(SESSION_ID, "model4") is False
        assert restarted.get_selfie_style(OTHER_SESSION_ID) == "mirror"

    asyncio.run(scenario())


def test_cancelled_write_finishes_off_loop_before_next_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "session_settings.json"
        state = RuntimeStateManager()
        await state.load(path)
        started, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        original_write = state._write_snapshot

        def paused_write(target: Path, states: Dict[str, Any]) -> None:
            assert threading.get_ident() != loop_thread
            started.set()
            assert release.wait(5), "测试未及时释放模拟写入"
            original_write(target, states)

        monkeypatch.setattr(state, "_write_snapshot", paused_write)
        task = asyncio.create_task(state.set_selfie_style(SESSION_ID, "mirror"))
        assert await asyncio.to_thread(started.wait, 5)
        assert state.get_selfie_style(SESSION_ID, "auto") == "auto"
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state.get_selfie_style(SESSION_ID) == "mirror"

        await state.set_command_default_model(SESSION_ID, "model2")
        restarted = RuntimeStateManager()
        await restarted.load(path)
        assert restarted.get_selfie_style(SESSION_ID) == "mirror"
        assert restarted.get_command_default_model(SESSION_ID, "global") == "model2"

    asyncio.run(scenario())


def test_command_waits_for_commit_before_sending_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        plugin = make_plugin(tmp_path)
        await plugin.runtime_state.load(tmp_path / "session_settings.json")
        started, release = threading.Event(), threading.Event()
        original_write = plugin.runtime_state._write_snapshot

        def paused_write(path: Path, states: Dict[str, Any]) -> None:
            started.set()
            assert release.wait(5), "测试未及时释放模拟写入"
            original_write(path, states)

        monkeypatch.setattr(plugin.runtime_state, "_write_snapshot", paused_write)
        task = asyncio.create_task(CommandDispatcher(plugin).dispatch(
            SESSION_ID, {}, "off", user_id="admin",
        ))
        assert await asyncio.to_thread(started.wait, 5)
        plugin.ctx.send.text.assert_not_awaited()
        release.set()
        assert (await task)[0] is True
        assert plugin.runtime_state.is_plugin_enabled(SESSION_ID, True) is False
        plugin.ctx.send.text.assert_awaited_once_with("已禁用", SESSION_ID)

    asyncio.run(scenario())


def test_failed_command_reports_failure_and_disabled_session_can_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        plugin = make_plugin(tmp_path)
        path = tmp_path / "session_settings.json"
        await plugin.runtime_state.load(path)
        dispatcher = CommandDispatcher(plugin)
        original_replace = state_module.os.replace

        def reject_replace(source: Any, destination: Any) -> None:
            raise PermissionError("模拟磁盘写入失败")

        monkeypatch.setattr(state_module.os, "replace", reject_replace)
        result = await dispatcher.dispatch(SESSION_ID, {}, "off", user_id="admin")
        assert result == (False, "会话设置保存失败", True)
        assert plugin.runtime_state.is_plugin_enabled(SESSION_ID, True) is True
        assert "保存失败" in plugin.ctx.send.text.await_args.args[0]
        assert "已禁用" not in plugin.ctx.send.text.await_args.args[0]

        monkeypatch.setattr(state_module.os, "replace", original_replace)
        assert (await dispatcher.dispatch(SESSION_ID, {}, "off", user_id="admin"))[0] is True
        assert (await dispatcher.dispatch(SESSION_ID, {}, "reset", user_id="ordinary"))[0] is False
        assert plugin.runtime_state.is_plugin_enabled(SESSION_ID, True) is False
        assert (await dispatcher.dispatch(SESSION_ID, {}, "reset", user_id="admin"))[0] is True
        assert plugin.runtime_state.is_plugin_enabled(SESSION_ID, True) is True
        assert SESSION_ID not in json.loads(path.read_text(encoding="utf-8"))["sessions"]

    asyncio.run(scenario())


def test_tool_and_model_resolution_use_instance_overrides_and_host_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin = make_plugin(tmp_path)
        await plugin.runtime_state.load(tmp_path / "session_settings.json")
        await plugin.runtime_state.set_action_default_model(SESSION_ID, "model2")
        await plugin.runtime_state.set_selfie_style(SESSION_ID, "photo")
        request = await build_request_from_tool_kwargs(plugin, {
            "stream_id": SESSION_ID, "chat_id": "1001", "user_id": "2001",
            "group_id": "1001", "platform": "qq", "description": "画一只小狐狸",
        })
        assert request is not None
        assert request.chat_id == SESSION_ID
        assert (request.user_id, request.group_id, request.platform) == ("2001", "1001", "qq")
        assert request.selfie_style == "photo"
        assert await ResolveModel().run(request, SimpleNamespace(plugin=plugin)) is None
        assert request.model_id == "model2"

        external = GenerationRequest(source="standalone", chat_id="")
        assert await ResolveModel().run(external, SimpleNamespace(plugin=plugin)) is None
        assert external.model_id == "model1"
        with pytest.raises(ValueError, match="真实 session_id"):
            await build_request_from_tool_kwargs(plugin, {"chat_id": "1001", "description": "画一只小狐狸"})

    asyncio.run(scenario())


def test_settings_commands_are_restored_by_the_plugin_lifecycle(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin = make_plugin(tmp_path)
        await plugin.on_load()
        dispatcher = CommandDispatcher(plugin)
        for command in (
            "default model2", "set model2", "model off model1", "recall off model2",
            "selfie on", "selfie mirror",
        ):
            result = await dispatcher.dispatch(SESSION_ID, {}, command, user_id="admin", platform="qq")
            assert result[0] is True, (command, result)
        await plugin.on_unload()

        restarted = make_plugin(tmp_path)
        await restarted.on_load()
        state = restarted.runtime_state
        assert state.get_action_default_model(SESSION_ID, "model1") == "model2"
        assert state.get_command_default_model(SESSION_ID, "model1") == "model2"
        assert state.is_model_enabled(SESSION_ID, "model1") is False
        assert state.is_recall_enabled(SESSION_ID, "model2", True) is False
        assert state.is_selfie_schedule_enabled(SESSION_ID, False) is True
        assert state.get_selfie_style(SESSION_ID, "auto") == "mirror"
        assert state.get_action_default_model(OTHER_SESSION_ID, "model1") == "model1"
        await restarted.on_unload()

    asyncio.run(scenario())


def test_config_view_uses_chat_name_and_hot_reload_keeps_overrides(tmp_path: Path) -> None:
    async def scenario() -> None:
        plugin = make_plugin(tmp_path)
        await plugin.on_load()
        await plugin.runtime_state.set_selfie_style(SESSION_ID, "mirror")
        original_state = plugin.runtime_state
        data = plugin.get_plugin_config_data()
        data["basic"]["default_txt2img_model"] = "model2"
        plugin.set_plugin_config(data)
        await plugin.on_config_update("self", data, "test")
        assert plugin.runtime_state is original_state
        assert plugin.runtime_state.get_selfie_style(SESSION_ID, "photo") == "mirror"
        assert plugin.runtime_state.get_action_default_model(SESSION_ID, "model2") == "model2"

        dctx = DispatcherContext(
            plugin=plugin, stream_id=SESSION_ID, chat_id=SESSION_ID, message={},
            is_admin=True, log_prefix="[测试群]", group_name="测试群",
        )
        assert (await render_current_config(dctx))[0] is True
        message = plugin.ctx.send.text.await_args.args[0]
        assert "测试群配置" in message
        assert SESSION_ID[:8] not in message
        assert "mirror" in message
        await plugin.on_unload()

    asyncio.run(scenario())
