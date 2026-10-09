"""隔离宿主外部依赖，执行生产方法的质量回归测试；不访问网络及运行数据库。"""

import ast
import asyncio
import datetime
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]


def load_class(relative_path, class_name, methods, namespace):
    """从源码提取指定类的方法并编译，使用传入依赖执行原始方法正文。"""
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    cls.bases = []
    cls.decorator_list = []
    cls.body = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in methods]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), relative_path, "exec"), namespace)
    return namespace[class_name]


class QualityTests(unittest.IsolatedAsyncioTestCase):
    """验证返回值契约、权限、日程来源以及完整提示词缓存隔离。"""

    async def test_style_tuple_and_actual_model(self):
        """真实风格方法必须解包配置，并使用回退后的模型进行权限检查。"""
        state = SimpleNamespace(get_command_default_model=Mock(return_value="missing"), is_model_enabled=Mock(return_value=True))
        allowed = Mock(return_value=True)
        cls = load_class("core/pic_command.py", "PicGenerationCommand", {"_execute_style_mode"}, {"runtime_state": state, "is_chat_allowed_for_model": allowed})
        obj = cls()
        obj._get_chat_id = Mock(return_value="qq:1:group")
        obj.get_config = Mock(return_value=False)
        obj._get_model_config = Mock(return_value=("model1", {"support_img2img": False}))
        obj.image_processor = SimpleNamespace(get_recent_image=AsyncMock(return_value="image"))
        obj.send_text = AsyncMock()
        result = await obj._execute_style_mode("style", "style", "prompt")
        self.assertFalse(result[0])
        self.assertIn("不支持图生图", result[1])
        state.is_model_enabled.assert_called_once_with("qq:1:group", "model1")
        allowed.assert_called_once_with(obj.get_config, "qq:1:group", "model1")

    async def test_natural_mode_rejects_disabled_fallback(self):
        """不存在的模型回退后，禁用模型不能进入图片获取或生成流程。"""
        state = SimpleNamespace(is_model_enabled=Mock(return_value=False))
        cls = load_class("core/pic_command.py", "PicGenerationCommand", {"_execute_natural_mode"}, {"runtime_state": state, "logger": logging.getLogger(__name__)})
        obj = cls()
        obj._get_chat_id = Mock(return_value="qq:1:group")
        obj._extract_model_id = Mock(return_value="missing")
        obj._remove_model_pattern = Mock(return_value="cat")
        obj._get_model_config = Mock(return_value=("model1", {"base_url": "test"}))
        obj.send_text = AsyncMock()
        obj.log_prefix = "test"
        result = await obj._execute_natural_mode("missing cat")
        self.assertFalse(result[0])
        state.is_model_enabled.assert_called_once_with("qq:1:group", "model1")

    async def test_template_allows_llm_and_llm_is_deduplicated(self):
        """空日程先保存模板仍会启动一次 LLM，成功来源记录会阻止再次生成。"""
        calendar_service = SimpleNamespace(get_day=AsyncMock(return_value=SimpleNamespace(is_off_day=False)))
        cls = load_class("core/schedule/schedule_manager.py", "ScheduleManager", {"ensure_today_schedule", "stop_override_tasks"}, {"asyncio": asyncio, "datetime": datetime, "now_beijing": datetime.datetime.now, "get_calendar_service": Mock(return_value=calendar_service), "logger": logging.getLogger(__name__), "get_template_schedule": Mock(return_value=[]), "to_db_dict": dict})
        obj = cls()
        values = {}
        obj._db = SimpleNamespace(list_schedule_items=Mock(return_value=[]), replace_schedule_items=Mock(), set_state=values.__setitem__, get_state=values.get)
        obj._llm_override_tasks = {}
        obj._try_llm_override = AsyncMock()
        await obj.ensure_today_schedule(plugin=object())
        await asyncio.gather(*obj._llm_override_tasks.values())
        obj._try_llm_override.assert_awaited_once()
        obj._db.list_schedule_items.return_value = [{}]
        values["schedule_last_generated_source"] = "llm"
        await obj.ensure_today_schedule(plugin=object())
        obj._try_llm_override.assert_awaited_once()
        await obj.stop_override_tasks()
        self.assertEqual(obj._llm_override_tasks, {})

    def test_cache_full_prompt_and_reference(self):
        """长提示词仅尾部变化、参考图变化或会话变化均不得命中原键。"""
        cls = load_class("core/utils/cache_manager.py", "CacheManager", {"_get_cache_key", "_get_img2img_cache_key"}, {"hashlib": hashlib})
        prefix = "a" * 150
        self.assertNotEqual(cls._get_cache_key("s", prefix + "cat", "m", "size"), cls._get_cache_key("s", prefix + "dog", "m", "size"))
        key = cls._get_img2img_cache_key
        self.assertNotEqual(key("s", prefix + "cat", "m", "size", input_image_data=b"a"), key("s", prefix + "dog", "m", "size", input_image_data=b"a"))
        self.assertNotEqual(key("s", prefix, "m", "size", input_image_data=b"a"), key("s", prefix, "m", "size", input_image_data=b"b"))
        self.assertNotEqual(cls._get_cache_key("s", prefix, "m", "size"), cls._get_cache_key("other", prefix, "m", "size"))

    def test_runtime_key_matches_command(self):
        """Action 状态键与命令规范化标识一致，同时保留发送所需宿主流 ID。"""
        cls = load_class("core/pic_action.py", "SelfiePainterAction", {"_get_runtime_state_id"}, {"extract_context_id_from_chat_stream": Mock(return_value="qq:1:group")})
        obj = cls()
        obj.chat_stream = object()
        obj.chat_id = "host-hash"
        self.assertEqual(obj._get_runtime_state_id(), "qq:1:group")
        self.assertEqual(obj.chat_id, "host-hash")

    def test_versions(self):
        """发布元数据必须与 Python 常量一致。"""
        manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
        tree = ast.parse((ROOT / "plugin_meta.py").read_text(encoding="utf-8"))
        version = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLUGIN_VERSION" for t in node.targets))
        self.assertEqual(manifest["version"], version)

    async def test_runtime_bootstrap_is_idempotent(self):
        """进程启动事件重复触发时只保留一个延迟任务，停止后不残留。"""
        cls = load_class("plugin_runtime.py", "PluginRuntimeMixin", {"_initialize_runtime_state", "_bootstrap_runtime_tasks", "_schedule_background_task", "try_start_auto_selfie", "try_start_schedule_gen", "_stop_schedule_gen_task"}, {"asyncio": asyncio, "logger": logging.getLogger(__name__)})
        obj = cls()
        obj._initialize_runtime_state()
        obj.get_config = Mock(return_value=False)

        async def pending():
            """模拟尚未完成的日程启动，供取消行为验证。"""
            await asyncio.Event().wait()

        obj._start_schedule_gen_after_delay = pending
        obj._bootstrap_runtime_tasks()
        task = obj._schedule_startup_task
        obj._bootstrap_runtime_tasks()
        self.assertIs(obj._schedule_startup_task, task)
        await obj._stop_schedule_gen_task()
        self.assertTrue(task.cancelled())

    async def test_context_is_written_and_isolated(self):
        """执行消息记录处理器后，仅相同会话能读到日程上下文。"""
        import collections
        import dataclasses
        import time

        namespace = {"time": time, "deque": collections.deque, "dataclass": dataclasses.dataclass}
        exec(compile((ROOT / "core/inject/context_cache.py").read_text(encoding="utf-8"), "context_cache.py", "exec"), namespace)
        cls = load_class("core/schedule_inject_handler.py", "ScheduleContextHandler", {"execute"}, {"get_context_cache": namespace["get_context_cache"]})
        obj = cls()
        obj.get_config = Mock(side_effect=lambda key, default: default)
        await obj.execute(SimpleNamespace(stream_id="one", plain_text="今天安排是什么"))
        self.assertTrue(namespace["get_context_cache"]("one").is_discussing_schedule())
        self.assertFalse(namespace["get_context_cache"]("two").is_discussing_schedule())

    async def test_false_image_send_is_not_success(self):
        """执行原始聊天发布代码，发送接口返回 False 时必须抛出全部发送失败。"""
        tree = ast.parse((ROOT / "core/selfie/auto_selfie_task.py").read_text(encoding="utf-8"))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute_selfie")
        start = next(i for i, node in enumerate(method.body) if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "send_to_chat")
        method.body = ast.parse("any_send_success = False\nqzone_failed = False\nchat_failed = False").body + method.body[start:]
        method.args = ast.arguments(posonlyargs=[], args=[ast.arg(arg="self")], kwonlyargs=[], kw_defaults=[], defaults=[])
        sender = SimpleNamespace(image_to_stream=AsyncMock(return_value=False), text_to_stream=AsyncMock(return_value=True))
        chat = SimpleNamespace(get_stream_by_group_id=Mock(return_value=SimpleNamespace(stream_id="hash")))
        modules = {
            "src.chat.message_receive.chat_stream": SimpleNamespace(get_chat_manager=Mock(return_value=SimpleNamespace())),
            "src.plugin_system": SimpleNamespace(chat_api=chat),
            "src.plugin_system.apis": SimpleNamespace(send_api=sender),
        }
        namespace = dict(send_to_chat=True, target_groups=["1"], target_users=[], image_data="aW1hZ2U=", caption_enabled=False, caption="", any_send_success=False, qzone_failed=False, chat_failed=False, actual_model_id="model1", selfie_model="missing", import_module=modules.__getitem__, logger=logging.getLogger(__name__), build_target_context_id=Mock(return_value="qq:1:group"), is_chat_allowed_for_model=Mock(return_value=True))
        import base64
        namespace["base64"] = base64
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), "auto_selfie_task.py", "exec"), namespace)
        obj = SimpleNamespace(get_config=Mock(), _resolve_image_to_bytes=AsyncMock(return_value=b"image"))
        with self.assertRaisesRegex(RuntimeError, "所有发送渠道均失败"):
            await namespace["_execute_selfie"](obj)
        sender.image_to_stream.assert_awaited_once()
        namespace["is_chat_allowed_for_model"].assert_called_once_with(obj.get_config, "qq:1:group", "model1")


if __name__ == "__main__":
    unittest.main()
