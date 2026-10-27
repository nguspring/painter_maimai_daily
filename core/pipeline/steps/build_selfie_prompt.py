"""Step: 构造自拍提示词

- 非自拍直接跳过
- selfie.enabled=False → fail
- 委托给 prompts.selfie_prompt_builder.build() 完成同一次场景中的服装、构图、动作与身份
- 把结果写回 req.description + req.selfie_negative_prompt
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import logging

from ...prompts.selfie_prompt_builder import supports_reference_strength
from ...selfie.schedule_provider import ActivityInfo, ActivityType, classify_activity
from ..result import StepResult
from ..step import BaseStep

if TYPE_CHECKING:
    from ..request import GenerationRequest
    from ..step import PipelineContext

logger = logging.getLogger("plugin.painter_maimai_daily.step.selfie_prompt")


class BuildSelfiePrompt(BaseStep):
    async def run(self, req: "GenerationRequest", ctx: "PipelineContext") -> Optional[StepResult]:
        if not req.is_selfie:
            return None

        plugin = ctx.plugin
        if not plugin.config.selfie.enabled:
            return StepResult.fail(error="自拍功能未启用", user_message="自拍功能暂未启用~")

        from ...prompts.selfie_prompt_builder import build as build_selfie_prompt

        activity = ActivityInfo(
            activity_type=ActivityType(req.activity_type) if req.activity_type else classify_activity(req.activity_description or req.description),
            description=req.activity_description or req.description,
            outfit=req.outfit,
            location=req.activity_location, time_window=req.activity_time_window,
            as_of=req.activity_as_of, timezone=req.activity_timezone,
            scene=req.activity_scene_description,
        )
        rules = [rule for rule in plugin.config.selfie.activity_rules if rule.activity_type == activity.activity_type]
        if len(rules) > 1:
            raise ValueError(f"活动规则重复: {activity.activity_type.value}")
        rule = {"selfie_style": rules[0].selfie_style, "outfit": rules[0].outfit} if rules else None
        if req.resolved_model_config is None:
            raise ValueError("自拍请求缺少已解析的模型配置")
        prompt_syntax = req.resolved_model_config.get("prompt_syntax", "auto")
        logger.info(f"{req.log_prefix} 启用自拍模式，风格: {req.selfie_style}")
        result = await build_selfie_prompt(
            ctx=plugin.ctx,
            selfie_style=req.selfie_style,
            description=req.description,
            free_hand_action=req.free_hand_action,
            activity_scene=req.activity_scene,
            bot_appearance=plugin.config.selfie.prompt_prefix,
            base_negative=plugin.config.selfie.negative_prompt,
            log_prefix=req.log_prefix,
            run_scene_optimizer=plugin.config.basic.prompt_optimizer_enabled,
            llm_task=plugin.config.basic.llm_task_name,
            activity_info=activity,
            activity_rule=rule,
            outfit=req.outfit,
            api_format=req.api_format,
            prompt_syntax=prompt_syntax,
            timeout_seconds=plugin.config.basic.llm_timeout_seconds,
            recent_styles=req.recent_selfie_styles,
        )

        req.description = result.prompt
        req.selfie_negative_prompt = result.negative_prompt
        req.selfie_style = result.selfie_style
        req.outfit = result.outfit
        # 内部主动任务可获取本次实际方案，配文不再只根据粗活动重新猜测。
        req.extras["photo_plan"] = result.photo_plan
        # 只有接受扩散降噪参数的后端才自动设置参考强度，保留调用方显式指定的值。
        if req.input_image_base64 and req.strength is None and supports_reference_strength(req.api_format, prompt_syntax):
            req.strength = float(req.resolved_model_config.get("selfie_reference_strength", 0.6))
        logger.info(f"{req.log_prefix} 自拍提示词构造完成: {req.description[:100]}...")
        return None
