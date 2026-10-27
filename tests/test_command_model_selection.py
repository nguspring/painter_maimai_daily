"""指定模型命令从实际分发入口选模型，不被聊天流默认值覆盖。"""

from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock

import asyncio
import pytest

from painter_maimai_daily.core.commands import generate
from painter_maimai_daily.core.commands.dispatcher import CommandDispatcher
from painter_maimai_daily.core.config.models import ModelConfig
from painter_maimai_daily.core.pipeline.steps.resolve_model import ResolveModel
from painter_maimai_daily.plugin import PainterMaimaiPlugin


@pytest.mark.parametrize("description,model_id,prompt", [
    ("用模型四画一张美少女", "model4", "一张美少女"),
    ("用模型4画一张美少女", "model4", "一张美少女"),
    ("使用 模型 四 生成一张美少女", "model4", "一张美少女"),
    ("用model4画一张美少女", "model4", "一张美少女"),
    ("使用 MODEL4 生成一张美少女", "model4", "一张美少女"),
    ("model4 画一张美少女", "model4", "一张美少女"),
    ("模型4画一张美少女", "model4", "一张美少女"),
    ("模型四画一张美少女", "model4", "一张美少女"),
    ("型号四创作一张美少女", "model4", "一张美少女"),
    ("帮我用模型四画一张美少女", "model4", "帮我一张美少女"),
    ("用模型两画一只猫", "model2", "一只猫"),
    ("用模型十画一只猫", "model10", "一只猫"),
    ("用模型十二画一只猫", "model12", "一只猫"),
    ("用模型二十画一只猫", "model20", "一只猫"),
    ("用模型二十三画一只猫", "model23", "一只猫"),
    ("用模型一百零四画一只猫", "model104", "一只猫"),
    ("画一只猫", None, "画一只猫"),
    ("画一个飞机模型四周的风景", None, "画一个飞机模型四周的风景"),
    ("用model4abc画一只猫", None, "用model4abc画一只猫"),
])
def test_model_selector_keeps_prompt_and_model_consistent(
    description: str, model_id: Optional[str], prompt: str,
) -> None:
    assert generate._extract_model_id(description) == model_id
    assert generate._remove_model_pattern(description) == prompt


@pytest.mark.parametrize("command,enabled", [
    ("用模型四画一张美少女", True), ("用模型4画一张美少女", True),
    ("用model4画一张美少女", True), ("模型4画一张美少女", True),
    ("用模型四画一张美少女", False),
])
def test_dispatch_selects_model4_over_doubao_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, enabled: bool,
) -> None:
    async def scenario() -> None:
        plugin = PainterMaimaiPlugin()
        config = plugin.get_default_config()
        config["plugin"]["enabled"] = True
        config["basic"].update(command_prefix="/dv", pic_command_model="model3")
        config["models"]["items"] = [
            ModelConfig(id="model3", format="doubao").model_dump(),
            ModelConfig(id="model4", format="novelai", novelai_api_mode="json", model="nai-diffusion-5-full").model_dump(),
        ]
        plugin.set_plugin_config(config)
        plugin._set_context(SimpleNamespace(send=SimpleNamespace(text=AsyncMock())))
        await plugin.runtime_state.load(tmp_path / "session_settings.json")
        await plugin.runtime_state.set_command_default_model("actual-session", "model3")
        if not enabled:
            await plugin.runtime_state.set_model_enabled("actual-session", "model4", False)
        pipeline = SimpleNamespace(run=AsyncMock(side_effect=lambda req, ctx: req))

        async def run(req, ctx):
            assert await ResolveModel().run(req, ctx) is None
            req.success = True
            return req

        pipeline.run.side_effect = run
        monkeypatch.setattr(generate, "_get_natural_pipeline", lambda: pipeline)
        monkeypatch.setattr(generate.ImageProcessor, "get_recent_image", AsyncMock(return_value=None))
        monkeypatch.setattr(generate, "make_pipeline_context", lambda plugin, req: SimpleNamespace(plugin=plugin))
        result = await CommandDispatcher(plugin).dispatch("actual-session", {}, command)
        if enabled:
            assert result[0]
            req = pipeline.run.await_args.args[0]
            assert req.model_id == "model4" and req.description == "一张美少女"
            assert req.source == "cmd_natural"
        else:
            assert not result[0] and "已禁用" in result[1]
            pipeline.run.assert_not_awaited()
        assert plugin.runtime_state.get_command_default_model("actual-session", "model3") == "model3"
        await plugin.runtime_state.close()

    asyncio.run(scenario())
