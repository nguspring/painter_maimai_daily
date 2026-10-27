"""Step: 解析模型 ID

- 缺失 model_id 时按 source + 模式（自拍 / 图生图 / 文生图）回退解析
- runtime_state 按聊天流覆盖
- 模型禁用检查 → fail

路由优先级（LLM 未显式指定 model_id 时）：
  1. 自动自拍 (source=auto_selfie)       → selfie.selfie_model（留空→下一行）
  2. 普通自拍 (is_selfie) 且配了自拍模型  → selfie.llm_selfie_model（留空→下一行）
  3. 有输入图 (img2img) 且配了图生图模型  → basic.default_img2img_model（留空→下一行）
  4. 兜底                                 → basic.default_txt2img_model

各层留空即向下一层回退。自拍参考图（selfie.reference_image_path）可选，
有参考图时使用第 3 层的图生图模型，没有参考图时使用第 4 层的文生图模型。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ...config import resolve_img2img_model, resolve_txt2img_model
from ..result import StepResult
from ..step import BaseStep

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin
    from ..request import GenerationRequest
    from ..step import PipelineContext

logger = logging.getLogger("plugin.painter_maimai_daily.step.resolve_model")


def _resolve_default_model(plugin: "PainterMaimaiPlugin", req: "GenerationRequest") -> str:
    """按「自拍 → 图生图 → 文生图」回退链解析默认模型 ID。"""
    selfie = plugin.config.selfie

    if req.is_selfie:
        model_id = (selfie.llm_selfie_model or "").strip()
        if model_id:
            return model_id

    if req.input_image_base64 is not None:
        return resolve_img2img_model(plugin)

    return resolve_txt2img_model(plugin)


class ResolveModel(BaseStep):
    async def run(self, req: "GenerationRequest", ctx: "PipelineContext") -> Optional[StepResult]:
        if not req.model_id:
            plugin = ctx.plugin
            if req.source in ("cmd_style", "cmd_natural"):
                global_default = plugin.config.basic.pic_command_model
                req.model_id = ctx.plugin.runtime_state.get_command_default_model(req.chat_id, global_default)
            elif req.source == "auto_selfie":
                # 自动自拍沿用专用模型；留空时按是否有参考图走通用回退链。
                auto_model = (plugin.config.selfie.selfie_model or "").strip()
                req.model_id = auto_model or _resolve_default_model(plugin, req)
            elif req.source == "standalone":
                req.model_id = _resolve_default_model(plugin, req)
            else:  # action（LLM 自然语言触发，含普通自拍）
                runtime_override = plugin.runtime_state.get_action_default_model(req.chat_id, "")
                req.model_id = runtime_override or _resolve_default_model(plugin, req)

        if req.source != "standalone" and not ctx.plugin.runtime_state.is_model_enabled(req.chat_id, req.model_id):
            return StepResult.fail(
                error=f"模型 {req.model_id} 已禁用",
                user_message=f"模型 {req.model_id} 当前不可用",
            )

        return None
