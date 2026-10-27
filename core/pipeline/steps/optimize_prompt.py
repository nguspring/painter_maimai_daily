"""Step: 通用提示词优化

- 自拍模式跳过（由 BuildSelfiePrompt 在同一次场景生成中完成优化）
- 优化器关闭时跳过
- 后端、图生图编辑要求与既定画幅一起传入；失败时终止生成
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import logging

from ...utils import optimize_prompt
from ..result import StepResult
from ..step import BaseStep

if TYPE_CHECKING:
    from ..request import GenerationRequest
    from ..step import PipelineContext

logger = logging.getLogger("plugin.painter_maimai_daily.step.optimize")


class OptimizePrompt(BaseStep):
    async def run(self, req: "GenerationRequest", ctx: "PipelineContext") -> Optional[StepResult]:
        if req.is_selfie:
            # 自拍模式由 BuildSelfiePrompt 一次生成场景、服装和构图。
            return None

        if not ctx.plugin.config.basic.prompt_optimizer_enabled:
            return None

        if not req.description.strip():
            return None

        if req.resolved_model_config is None:
            raise ValueError("提示词优化请求缺少已解析的模型配置")

        logger.info(f"{req.log_prefix} 开始优化提示词: {req.description[:50]}...")
        ok, optimized = await optimize_prompt(
            ctx.plugin.ctx,
            req.description,
            log_prefix=req.log_prefix,
            scene_only=False,
            llm_task=ctx.plugin.config.basic.llm_task_name,
            timeout_seconds=ctx.plugin.config.basic.llm_timeout_seconds,
            api_format=req.api_format,
            prompt_syntax=req.resolved_model_config.get("prompt_syntax", "auto"),
            is_img2img=bool(req.input_image_base64) and req.resolved_model_config.get("support_img2img", True),
            output_size=req.final_size,
        )
        if ok and optimized:
            logger.info(f"{req.log_prefix} 提示词优化完成: {optimized[:80]}...")
            req.description = optimized
        else:
            return StepResult.fail(error=optimized or "提示词优化失败", user_message="提示词优化失败，本次生图已取消。")
        return None
