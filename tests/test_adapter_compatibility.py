"""只使用现有适配器和 SDK 公开接口的离线兼容回归。"""

from contextlib import contextmanager
from importlib.machinery import ModuleSpec
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import logging
import sys

from maibot_sdk import PluginContext
import pytest

from painter_maimai_daily.core.config.models import PainterMaimaiConfig
from painter_maimai_daily.core.pipeline.request import GenerationRequest
from painter_maimai_daily.core.pipeline.steps.schedule_recall import ScheduleRecall
from painter_maimai_daily.core.pipeline.steps.send_image import SendImage
from painter_maimai_daily.core.utils.image_utils import ImageProcessor
from painter_maimai_daily.core.utils.recall_utils import schedule_auto_recall
from painter_maimai_daily.core.utils.request_context import RequestContext
from painter_maimai_daily.plugin import PainterMaimaiPlugin


SESSION = "registered-qq-session"
SNOWLUMA_PLUGIN = "maibot-team.snowluma-adapter"


def api_entry(name: str, plugin_id: str = SNOWLUMA_PLUGIN) -> dict:
    return {"name": name, "full_name": f"{plugin_id}.{name}", "plugin_id": plugin_id, "version": "1", "enabled": True}


@contextmanager
def adapter_module(directory: str, name: str):
    adapter_dir = Path(__file__).resolve().parents[2] / directory
    if not (adapter_dir / "plugin.py").is_file():
        pytest.skip(f"未安装适配器 {directory}")
    package = ModuleType(name)
    package.__path__ = [str(adapter_dir)]
    package.__spec__ = ModuleSpec(name, loader=None, is_package=True)
    sys.modules[name] = package
    spec = spec_from_file_location(f"{name}.plugin", adapter_dir / "plugin.py")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting
        for module_name in list(sys.modules):
            if module_name == name or module_name.startswith(name + "."):
                del sys.modules[module_name]


@pytest.mark.parametrize("prefix,message_id", [("napcat", "12345"), ("snowluma", "-12345")])
def test_onebot_recall_uses_registered_provider(prefix: str, message_id: str) -> None:
    async def scenario() -> None:
        calls = []
        entry = api_entry(f"adapter.{prefix}.message.delete_msg")

        async def rpc(method: str, plugin_id: str, payload: dict, **kwargs: object) -> dict:
            calls.append(payload)
            if payload["capability"] == "api.list":
                return {"success": True, "apis": [entry]}
            return {"success": True, "result": {"status": "ok", "retcode": 0}}

        task = await schedule_auto_recall(PluginContext("art", rpc_call=rpc), SESSION, message_id, 0.001, "[test]")
        assert task is not None
        await task
        assert calls[-1]["args"] == {"api_name": entry["full_name"], "version": "1", "args": {"message_id": message_id}}
    asyncio.run(scenario())


@pytest.mark.parametrize("message_id", ["official-message-id", "0", "abc123"])
def test_unsupported_message_never_reaches_onebot_delete(message_id: str, caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        api = SimpleNamespace(list=AsyncMock(), call=AsyncMock())
        assert await schedule_auto_recall(SimpleNamespace(api=api), SESSION, message_id, 1, "[test]") is None
        api.list.assert_not_awaited()
        api.call.assert_not_awaited()
    asyncio.run(scenario())
    assert "本次跳过" in caplog.text


def test_ambiguous_provider_does_not_guess_account() -> None:
    async def scenario() -> None:
        api = SimpleNamespace(list=AsyncMock(return_value=[
            api_entry("adapter.snowluma.message.delete_msg", "first"),
            api_entry("adapter.snowluma.message.delete_msg", "second"),
        ]), call=AsyncMock())
        assert await schedule_auto_recall(SimpleNamespace(api=api), SESSION, "12345", 1, "[test]") is None
        api.call.assert_not_awaited()
    asyncio.run(scenario())


def test_missing_recall_api_skips_without_calling_tools_or_http() -> None:
    async def scenario() -> None:
        api = SimpleNamespace(list=AsyncMock(return_value=[api_entry("adapter.avatar.get", "Mai-with-u.qq-adapter")]), call=AsyncMock())
        assert await schedule_auto_recall(SimpleNamespace(api=api), SESSION, "12345", 1, "[test]") is None
        api.call.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("scene,mapped_ids", [(scene, mapped) for scene in ("group", "private") for mapped in (True, False)])
def test_existing_official_gateway_sends_sdk_image_and_skips_auto_recall(scene: str, mapped_ids: bool) -> None:
    with adapter_module("Mai-with-u_qq-adapter", "_painter_maimai_test_qq_official") as module:
        async def scenario() -> None:
            adapter = module.QQOfficialAdapterPlugin()
            adapter.set_plugin_config(adapter.get_default_config())
            adapter._set_context(SimpleNamespace(logger=logging.getLogger("test.qq_official")))
            adapter._ready, adapter._account_id = True, "original-bot-openid"
            adapter._id_map = {"group": {"group-openid": "1001"}, "user": {"user-openid": "2002"}} if mapped_ids else {"group": {}, "user": {}}
            target_id = ("1001" if scene == "group" else "2002") if mapped_ids else ("group-openid" if scene == "group" else "user-openid")
            image = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9WQAAAAASUVORK5CYII="
            adapter._upload_image = AsyncMock(return_value="uploaded-file-info")
            adapter._api_post = AsyncMock(return_value={"id": "official-image-id"})
            calls = []

            async def rpc(method: str, plugin_id: str, payload: dict, **kwargs: object) -> dict:
                calls.append(payload)
                assert payload["capability"] == "send.image"
                args = payload["args"]
                info = {"additional_config": {}}
                if scene == "group":
                    info["group_info"] = {"group_id": target_id}
                else:
                    info["additional_config"]["platform_io_target_user_id"] = target_id
                result = await adapter.handle_qq_official_gateway({
                    "session_id": args["stream_id"], "message_info": info,
                    "raw_message": [{"type": "image", "binary_data_base64": args["image_base64"]}],
                })
                return {"success": result["success"], "sent": result["success"], "message_id": result.get("external_message_id")}

            plugin = PainterMaimaiPlugin()
            config = PainterMaimaiConfig()
            config.basic.auto_recall_enabled = True
            plugin.set_plugin_config(config.model_dump())
            plugin._set_context(PluginContext("art", rpc_call=rpc))
            req = GenerationRequest(
                stream_id=SESSION, chat_id=SESSION, resolved_image_data=image, debug_info=False,
                resolved_model_config={"auto_recall_delay": 10},
            )
            assert await SendImage().run(req, SimpleNamespace(plugin=plugin)) is None
            assert req.delivery_state == "sent" and req.sent_message_id == "official-image-id"
            assert await ScheduleRecall().run(req, SimpleNamespace(plugin=plugin)) is None
            assert not plugin._recall_tasks and len(calls) == 1
            path = "/v2/groups/group-openid/messages" if scene == "group" else "/v2/users/user-openid/messages"
            assert adapter._api_post.await_args.args[0] == path
        asyncio.run(scenario())


@pytest.mark.parametrize("client_type,scene", [(client, scene) for client in ("napcat", "snowluma") for scene in ("group", "private")])
def test_real_onebot_gateway_accepts_sdk_image_segments(client_type: str, scene: str) -> None:
    with adapter_module("maibot-team_snowluma-adapter", "_painter_maimai_test_onebot") as module:
        adapter = module.SnowLumaAdapterPlugin()
        adapter.set_plugin_config(adapter.get_default_config())
        adapter.config.client.client_type = client_type
        adapter._set_context(SimpleNamespace(logger=logging.getLogger("test.onebot"), gateway=SimpleNamespace()))
        runtime = adapter._require_runtime_bundle()
        response = {"status": "ok", "retcode": 0, "data": {"message_id": "-987"}}
        runtime.action_service.call_action = AsyncMock(return_value=response)

        async def scenario() -> None:
            message_info = {"additional_config": {}}
            if scene == "group":
                message_info["group_info"] = {"group_id": "1001"}
            else:
                message_info["additional_config"]["platform_io_target_user_id"] = "2002"
            result = await adapter.handle_snowluma_gateway({
                "session_id": SESSION, "message_info": message_info,
                "raw_message": [{"type": "image", "binary_data_base64": "offline-image"}],
            })
            assert result["success"] is True and result["external_message_id"] == "-987"
            action, params = runtime.action_service.call_action.await_args.args
            expected_action = "send_msg" if client_type == "snowluma" else f"send_{scene}_msg"
            assert action == expected_action
            assert params["message"][0]["type"] == "image"
            assert params["message"][0]["data"]["file"] == "base64://offline-image"
        asyncio.run(scenario())


@pytest.mark.parametrize("user_id,group_id", [("2002", "1001"), ("user-openid", "group-openid")])
def test_inbound_images_and_quotes_keep_openids(user_id: str, group_id: str) -> None:
    async def scenario() -> None:
        message = {"message_id": "trigger-id", "reply_to": "quoted-id", "raw_message": [], "message_info": {
            "user_info": {"user_id": user_id}, "group_info": {"group_id": group_id},
        }}
        plugin = PainterMaimaiPlugin()
        plugin._set_context(SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(return_value={
            "raw_message": [{"type": "image", "binary_data_base64": "offline-image"}],
        }))))
        processor = ImageProcessor(RequestContext(plugin, command_message=message, chat_id=SESSION))
        assert await processor.get_recent_image() == "offline-image"
        plugin.ctx.message.get_by_id.assert_awaited_once_with(message_id="quoted-id", chat_id=SESSION, include_binary_data=True)
    asyncio.run(scenario())
