"""验证当前 SDK 接口与旧插件升级时的配置、会话和 SQLite 数据保留。"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import json
import sqlite3
import tomllib

from maibot_sdk import PluginContext, PluginPaths
from maibot_sdk.config import rebuild_plugin_config_data
import pytest
import tomlkit

from painter_maimai_daily.core.config import migration as config_migration
from painter_maimai_daily.core.config.migration import normalize_config_for_upgrade, upgrade_config_file
from painter_maimai_daily.core.config.models import PainterMaimaiConfig
from painter_maimai_daily.core.pipeline.request import GenerationRequest
from painter_maimai_daily.core.pipeline.steps.schedule_recall import ScheduleRecall
from painter_maimai_daily.core.pipeline.steps.send_image import SendImage
from painter_maimai_daily.core.quota import QuotaPolicy, QuotaStore, QuotaSubject
from painter_maimai_daily.core.state import RuntimeStateManager
from painter_maimai_daily.core.state import migration as data_migration
from painter_maimai_daily.core.state.migration import migrate_plugin_data
from painter_maimai_daily.core.utils.image_utils import ImageProcessor
from painter_maimai_daily.core.utils.request_context import RequestContext
from painter_maimai_daily.plugin import PainterMaimaiPlugin, _extract_display_names, _looks_like_quoted_command, create_plugin


CURRENT_VERSION = PainterMaimaiConfig().plugin.config_version
SESSION = "host-registered-session"
RECALL_API = {
    "name": "adapter.napcat.message.delete_msg",
    "full_name": "maibot-team.snowluma-adapter.adapter.napcat.message.delete_msg",
    "plugin_id": "maibot-team.snowluma-adapter", "version": "1", "enabled": True,
}


def test_split_sections_preserve_settings_models_and_style_aliases() -> None:
    old = {
        "plugin": {"config_version": "3.4.0", "enabled": True},
        "generation": {"default_model": "custom"},
        "components": {
            "pic_command_model": "custom", "enable_pic_command": False,
            "admin_users": ["admin"], "max_retries": 4,
        },
        "cache": {"enabled": False, "max_size": 25},
        "auto_recall": {"enabled": True},
        "prompt_optimizer": {"enabled": False, "llm_task_name": "replyer"},
        "selfie": {"reference_image_path": "images/identity.png", "prompt_prefix": "silver hair"},
        "auto_selfie": {
            "enabled": True, "interval_minutes": 180, "selfie_style": "mirror",
            "selfie_model": "custom", "caption_enabled": False,
        },
        "models": {"custom": {
            "name": "自定义", "format": "gemini", "model": "custom-image-model",
            "api_key": "offline-test-key", "guidance_scale": -1, "support_img2img": False,
        }},
        "styles": {"custom-style": "my original prompt"},
        "style_aliases": {"custom-style": "自定义,原风格"},
    }
    original = deepcopy(old)
    normalized, changed = normalize_config_for_upgrade(old)
    assert changed and old == original
    config = PainterMaimaiConfig.model_validate(normalized)
    assert config.plugin.config_version == CURRENT_VERSION and config.plugin.enabled
    assert config.basic.default_txt2img_model == "custom"
    assert config.basic.pic_command_model == "custom" and not config.basic.enable_pic_command
    assert config.basic.admin_users == ["admin"] and config.basic.max_retries == 4
    assert not config.basic.cache_enabled and config.basic.cache_max_size == 25
    assert config.basic.auto_recall_enabled and not config.basic.prompt_optimizer_enabled
    assert config.basic.llm_task_name == "replyer"
    assert config.selfie.reference_image_path == "images/identity.png"
    assert config.selfie.prompt_prefix == "silver hair"
    assert config.selfie.auto_enabled and config.selfie.interval_minutes == 180
    assert config.selfie.default_style == "mirror" and config.selfie.selfie_model == "custom"
    assert not config.selfie.caption_enabled
    model, = config.models.items
    assert (model.id, model.api_key, model.format, model.guidance_scale) == ("custom", "offline-test-key", "gemini", -1)
    assert not model.support_img2img
    style, = config.styles.items
    assert (style.name, style.prompt, style.aliases) == ("custom-style", "my original prompt", "自定义,原风格")
    assert set(normalized) == set(PainterMaimaiConfig.model_fields)
    assert "default_model" not in normalized["basic"]


@pytest.mark.parametrize("version,models,styles", [
    ("4.0.0", {"custom": {"api_key": "key"}}, {"special": "prompt"}),
    ("3.1.0", [{"id": "custom", "api_key": "key"}], [{"name": "special", "prompt": "prompt"}]),
    ("3.2.1", {"items": [{"id": "custom", "api_key": "key"}]}, {"items": [{"name": "special", "prompt": "prompt"}]}),
])
def test_each_released_collection_shape_upgrades_without_defaults_replacing_it(
    version: str, models: object, styles: object,
) -> None:
    plugin = PainterMaimaiPlugin()
    plugin.set_plugin_config({
        "plugin": {"config_version": version}, "basic": {"default_model": "custom"},
        "models": models, "styles": styles, "style_aliases": {"special": "特殊"},
    })
    assert [item.id for item in plugin.config.models.items] == ["custom"]
    assert plugin.config.models.items[0].api_key == "key"
    assert [item.name for item in plugin.config.styles.items] == ["special"]
    assert plugin.config.styles.items[0].aliases == "特殊"
    assert plugin.config.basic.default_txt2img_model == "custom"
    assert "default_model" not in PainterMaimaiConfig.model_fields["basic"].annotation.model_fields
    assert normalize_config_for_upgrade(plugin.get_plugin_config_data())[1] is False


def test_new_fields_take_precedence_and_intentionally_empty_lists_stay_empty() -> None:
    normalized, _ = normalize_config_for_upgrade({
        "plugin": {"config_version": "3.2.1"},
        "generation": {"default_model": "old"}, "cache": {"enabled": True},
        "basic": {"default_txt2img_model": "new", "cache_enabled": False, "admin_users": []},
        "auto_selfie": {"enabled": True, "selfie_style": "mirror"},
        "selfie": {"auto_enabled": False, "default_style": "photo"},
        "models": {"items": []}, "styles": [],
    })
    assert normalized["basic"]["default_txt2img_model"] == "new"
    assert normalized["basic"]["cache_enabled"] is False
    assert normalized["selfie"]["auto_enabled"] is False
    assert normalized["selfie"]["default_style"] == "photo"
    assert normalized["models"]["items"] == normalized["styles"]["items"] == []


def test_same_version_legacy_default_is_migrated_before_sdk_rebuild() -> None:
    data = PainterMaimaiConfig().model_dump(mode="python")
    data["basic"]["default_model"] = "already-customized"
    normalized, changed = normalize_config_for_upgrade(data)
    assert changed and normalized["basic"]["default_txt2img_model"] == "already-customized"
    assert "default_model" not in normalized["basic"]


def test_factory_upgrades_before_host_can_discard_old_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = (
        '# original comment\n[plugin]\nconfig_version = "3.4.0"\nenabled = true\n'
        '[generation]\ndefault_model = "special"\n'
        '[models.special]\napi_key = "offline-secret"\n'
        '[styles]\nmine = "user prompt"\n[style_aliases]\nmine = "我的风格"\n'
    ).encode("utf-8")
    path = tmp_path / "config.toml"
    path.write_bytes(original)
    monkeypatch.setattr(PainterMaimaiPlugin, "plugin_dir", property(lambda self: str(tmp_path)))
    plugin = create_plugin()
    # 对应当前宿主的实际顺序：先重建，再调用插件 normalize/set_plugin_config。
    disk = tomllib.loads(path.read_text(encoding="utf-8"))
    prepared = rebuild_plugin_config_data(plugin.get_default_config(), disk)
    plugin.set_plugin_config(prepared)
    assert plugin.config.basic.default_txt2img_model == "special"
    assert plugin.config.models.items[0].api_key == "offline-secret"
    assert plugin.config.styles.items[0].aliases == "我的风格"
    backup, = (tmp_path / "old").glob("*.toml")
    assert backup.read_bytes() == original
    assert "# original comment" in path.read_text(encoding="utf-8")
    migrated = path.read_bytes()
    assert upgrade_config_file(path) is False and path.read_bytes() == migrated
    assert len(list((tmp_path / "old").glob("*.toml"))) == 1


@pytest.mark.parametrize("content", [
    '',
    '# empty config\n',
    '[basic]\ndefault_model="custom"\n',
    '[plugin]\nconfig_version="3.2.1"\n[models.custom]\nguidance_scale=-9\n',
    '[plugin]\nconfig_version="99.0.0"\n',
    '[plugin]\nconfig_version=""\n',
    '[plugin]\nconfig_version="3.2.1"\n[models]\nitems="invalid"\n',
    'not valid toml',
])
def test_invalid_or_newer_config_is_not_replaced(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.toml"
    original = content.encode()
    path.write_bytes(original)
    with pytest.raises((ValueError, tomlkit.exceptions.ParseError)):
        upgrade_config_file(path)
    assert path.read_bytes() == original and not (tmp_path / "old").exists()


@pytest.mark.parametrize("failure", ["backup", "replace"])
def test_upgrade_write_failure_keeps_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    path = tmp_path / "config.toml"
    original = b'[plugin]\nconfig_version="3.2.1"\n[basic]\ndefault_model="custom"\n'
    path.write_bytes(original)

    def reject(*args: object, **kwargs: object) -> None:
        raise OSError("offline disk failure")

    if failure == "backup":
        monkeypatch.setattr(config_migration, "backup_config_if_version_changed", reject)
    else:
        monkeypatch.setattr(config_migration.os, "replace", reject)
    with pytest.raises(OSError, match="offline disk failure"):
        upgrade_config_file(path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_data_migration_keeps_session_overrides_and_quota_usage(tmp_path: Path) -> None:
    source, destination = tmp_path / "plugin-data", tmp_path / "sdk-data"
    subject, policy = QuotaSubject("qq", "group", "group"), QuotaPolicy(10)

    async def scenario() -> None:
        state = RuntimeStateManager()
        await state.load(source / "session_settings.json")
        await state.set_selfie_style(SESSION, "mirror")
        await state.set_action_default_model(SESSION, "custom")
        await state.close()
        quota = QuotaStore()
        await quota.load(source / "quota.sqlite3")
        reservation = await quota.reserve(subject, policy)
        await quota.settle(reservation.token, "consumed")
        await quota.adjust(subject, policy, 2, actor="admin", reason="original adjustment")
        before = await quota.status(subject, policy)
        await quota.close()
        original_settings = (source / "session_settings.json").read_bytes()

        await asyncio.to_thread(migrate_plugin_data, source, destination)
        assert (source / "session_settings.json").read_bytes() == original_settings
        assert (source / "quota.sqlite3").exists()
        restored = RuntimeStateManager()
        await restored.load(destination / "session_settings.json")
        assert restored.get_selfie_style(SESSION) == "mirror"
        assert restored.get_action_default_model(SESSION, "model1") == "custom"
        await restored.set_selfie_style(SESSION, "photo")
        await restored.close()
        copied = QuotaStore()
        await copied.load(destination / "quota.sqlite3")
        after = await copied.status(subject, policy)
        assert (after.remaining, after.consumed, after.adjustment) == (before.remaining, before.consumed, before.adjustment)
        await copied.close()
        migrate_plugin_data(source, destination)
        assert json.loads((destination / "session_settings.json").read_text())["sessions"][SESSION]["selfie_style"] == "photo"

    asyncio.run(scenario())


def test_sqlite_migration_includes_committed_wal_rows(tmp_path: Path) -> None:
    source, destination = tmp_path / "legacy", tmp_path / "current"
    source.mkdir()
    path = source / "quota.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE original_data(value TEXT)")
        connection.execute("INSERT INTO original_data VALUES ('committed in WAL')")
        connection.commit()
        assert path.with_name("quota.sqlite3-wal").stat().st_size > 0
        migrate_plugin_data(source, destination)
        with sqlite3.connect(destination / path.name) as copied:
            assert copied.execute("SELECT value FROM original_data").fetchone()[0] == "committed in WAL"
    finally:
        connection.close()


def test_existing_target_wins_and_success_marker_prevents_stale_reimport(tmp_path: Path) -> None:
    source, destination = tmp_path / "legacy", tmp_path / "current"
    source.mkdir()
    destination.mkdir()
    (source / "session_settings.json").write_bytes(b"old settings")
    target = destination / "session_settings.json"
    target.write_bytes(b"current settings")
    migrate_plugin_data(source, destination)
    assert target.read_bytes() == b"current settings"
    target.unlink()
    migrate_plugin_data(source, destination)
    assert not target.exists()


def test_data_copy_failure_leaves_no_target_and_can_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, destination = tmp_path / "legacy", tmp_path / "current"
    source.mkdir()
    (source / "session_settings.json").write_bytes(b"original settings")
    real_link = data_migration.os.link
    with monkeypatch.context() as patch:
        patch.setattr(data_migration.os, "link", lambda *args: (_ for _ in ()).throw(OSError("publish failed")))
        with pytest.raises(OSError, match="publish failed"):
            migrate_plugin_data(source, destination)
    assert not (destination / "session_settings.json").exists()
    assert not list(destination.glob(".*"))
    assert data_migration.os.link is real_link
    migrate_plugin_data(source, destination)
    assert (destination / "session_settings.json").read_bytes() == b"original settings"


def test_marker_failure_preserves_migrated_data_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, destination = tmp_path / "legacy", tmp_path / "current"
    source.mkdir()
    (source / "session_settings.json").write_bytes(b"original settings")
    real_link = data_migration.os.link

    def reject_marker(source_path: Path, target_path: Path) -> None:
        if target_path.name == data_migration._MIGRATION_MARKER:
            raise OSError("marker publish failed")
        real_link(source_path, target_path)

    with monkeypatch.context() as patch:
        patch.setattr(data_migration.os, "link", reject_marker)
        with pytest.raises(OSError, match="marker publish failed"):
            migrate_plugin_data(source, destination)
    target = destination / "session_settings.json"
    assert target.read_bytes() == b"original settings" and not list(destination.glob(".*"))
    target.write_bytes(b"new settings")
    migrate_plugin_data(source, destination)
    assert target.read_bytes() == b"new settings"
    assert (destination / data_migration._MIGRATION_MARKER).read_bytes() == b"1\n"


def test_concurrent_marker_publication_preserves_completed_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, destination = tmp_path / "legacy", tmp_path / "current"
    source.mkdir()
    (source / "session_settings.json").write_bytes(b"original settings")
    real_link = data_migration.os.link

    def another_migration_wins(source_path: Path, target_path: Path) -> None:
        real_link(source_path, target_path)
        if target_path.name == data_migration._MIGRATION_MARKER:
            raise FileExistsError("another migration completed")

    monkeypatch.setattr(data_migration.os, "link", another_migration_wins)
    migrate_plugin_data(source, destination)
    assert (destination / "session_settings.json").read_bytes() == b"original settings"
    assert (destination / data_migration._MIGRATION_MARKER).read_bytes() == b"1\n"
    assert not list(destination.glob(".*.tmp"))


def test_current_sdk_image_and_quote_handling_uses_rpc_dicts() -> None:
    message = {
        "message_id": "trigger", "reply_to": "referenced",
        "raw_message": [{"type": "reply", "data": {"target_message_id": "referenced"}}, {"type": "text", "data": " /dr 改背景"}],
        "message_info": {"user_info": {"user_nickname": "昵称"}, "group_info": {"group_name": "群名"}},
    }
    assert _extract_display_names(message) == ("昵称", "群名")
    assert not _looks_like_quoted_command(message, "/dr")
    message["raw_message"][1]["data"] = "看看"
    assert _looks_like_quoted_command(message, "/dr")

    async def scenario() -> None:
        plugin = PainterMaimaiPlugin()
        plugin._set_context(SimpleNamespace(message=SimpleNamespace(get_by_id=AsyncMock(return_value={
            "raw_message": [{"type": "image", "binary_data_base64": "referenced-image"}],
        }))))
        processor = ImageProcessor(RequestContext(plugin, command_message=message, chat_id=SESSION))
        assert await processor.get_recent_image() == "referenced-image"
        plugin.ctx.message.get_by_id.assert_awaited_once_with(
            message_id="referenced", chat_id=SESSION, include_binary_data=True,
        )
        plugin.ctx.message.get_by_id.reset_mock()
        message["raw_message"].append({"type": "image", "hash": "direct-hash"})
        assert await processor.get_recent_image() == "referenced-image"
        plugin.ctx.message.get_by_id.assert_awaited_once_with(
            message_id="trigger", chat_id=SESSION, include_binary_data=True,
        )
    asyncio.run(scenario())


def test_send_receipt_id_is_used_for_recall_and_unload_cancels_pending_tasks(tmp_path: Path) -> None:
    async def scenario() -> None:
        calls = []

        async def rpc(method: str, plugin_id: str, payload: dict, **kwargs: object) -> dict:
            calls.append(payload)
            if payload["capability"] == "api.list":
                return {"success": True, "apis": [RECALL_API]}
            return {"success": True, "sent": True, "message_id": "-12345"}

        plugin = PainterMaimaiPlugin()
        plugin._set_context(PluginContext(
            "nguspring.painter_maimai_daily", rpc_call=rpc,
            paths=PluginPaths(data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime"),
        ))
        config = PainterMaimaiConfig()
        config.basic.auto_recall_enabled = True
        plugin.set_plugin_config(config.model_dump())
        await plugin.on_load()
        req = GenerationRequest(
            stream_id=SESSION, chat_id=SESSION, resolved_image_data="image", model_id="model1",
            resolved_model_config={"auto_recall_delay": 1}, debug_info=False,
        )
        assert await SendImage().run(req, SimpleNamespace(plugin=plugin)) is None
        assert req.sent_message_id == "-12345" and req.delivery_state == "sent"
        assert calls[-1]["args"]["return_details"] is True
        await ScheduleRecall().run(req, SimpleNamespace(plugin=plugin))
        pending, = plugin._recall_tasks
        await plugin.on_unload()
        assert pending.cancelled() and not plugin._recall_tasks
        assert [call["capability"] for call in calls] == ["send.image", "api.list"]

    asyncio.run(scenario())


@pytest.mark.parametrize("result,expected_log", [
    ({"status": "ok", "retcode": 0}, "图片已撤回"),
    ({"status": "ok", "retcode": 100}, "撤回未成功"),
    ({"status": "failed", "retcode": 0}, "撤回未成功"),
])
def test_recall_uses_current_adapter_api_for_the_received_id(
    result: dict, expected_log: str, caplog: pytest.LogCaptureFixture,
) -> None:
    from painter_maimai_daily.core.utils.recall_utils import schedule_auto_recall

    async def scenario() -> None:
        calls = []

        async def rpc(method: str, plugin_id: str, payload: dict, **kwargs: object) -> dict:
            calls.append(payload)
            if payload["capability"] == "api.list":
                return {"success": True, "apis": [RECALL_API]}
            return {"success": True, "result": result}

        ctx = PluginContext("nguspring.painter_maimai_daily", rpc_call=rpc)
        task = await schedule_auto_recall(ctx, SESSION, "12345", 0.001, "[test]")
        assert task is not None
        await task
        call = calls[-1]
        assert call["capability"] == "api.call"
        assert call["args"] == {
            "api_name": RECALL_API["full_name"], "version": "1",
            "args": {"message_id": "12345"},
        }
        assert await schedule_auto_recall(ctx, SESSION, "", 1, "[test]") is None

    with caplog.at_level("INFO", logger="plugin.painter_maimai_daily.recall"):
        asyncio.run(scenario())
    assert expected_log in caplog.text


def test_recall_api_failure_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    from painter_maimai_daily.core.utils.recall_utils import schedule_auto_recall

    async def scenario() -> None:
        async def rpc(method: str, plugin_id: str, payload: dict, **kwargs: object) -> dict:
            if payload["capability"] == "api.list":
                return {"success": True, "apis": [RECALL_API]}
            raise RuntimeError("对端动作返回失败")

        ctx = PluginContext("nguspring.painter_maimai_daily", rpc_call=rpc)
        task = await schedule_auto_recall(ctx, SESSION, "12345", 0.001, "[test]")
        assert task is not None
        await task

    asyncio.run(scenario())
    assert "撤回 API 调用异常" in caplog.text and "对端动作返回失败" in caplog.text
