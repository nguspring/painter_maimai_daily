"""根据真实群聊天流的最近消息主动发图。

群内容仅作为不可信语境交给 LLM；目标、频率和是否允许发送均由本地配置决定。
判断先消费语境，决定生成时才占用共享发布冷却；不重复发布投递状态未知的图片。
"""

from typing import TYPE_CHECKING, Any, Dict, List, Optional

import asyncio
import json
import logging
import random
import re

from ..prompts.prompt_templates import load_prompt_template
from .caption_generator import _get_bot_profile, generate_caption
from .proactive_context import activity_arguments, activity_block_reason, group_allowed, proactive_model, read_group_context, recheck_activity
from .schedule_provider import ActivityInfo, get_schedule_provider

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin

logger = logging.getLogger("plugin.painter_maimai_daily.group_image")

def parse_decision(response: str) -> Dict[str, Any]:
    """严格验证模型决定；字符串 false 不能被 bool() 误判为真。"""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip())
    result = json.loads(cleaned)
    if not isinstance(result, dict) or set(result) - {"should_draw", "prompt", "is_selfie", "style", "outfit", "reason"} or type(result.get("should_draw")) is not bool:
        raise ValueError("群图判断必须包含布尔 should_draw")
    reason = result.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 200 or ("reason" in result and not reason.strip()):
        raise ValueError("群图判断理由无效")
    if not result["should_draw"]:
        return {"should_draw": False, **({"reason": reason.strip()} if reason else {})}
    if type(result.get("is_selfie")) is not bool:
        raise ValueError("群图判断必须包含布尔 is_selfie")
    prompt = result.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1500:
        raise ValueError("群图描述为空或超过 1500 字")
    style = result.get("style")
    if style not in {"auto", "standard", "mirror", "photo"}:
        raise ValueError("群图判断返回未知构图")
    outfit = result.get("outfit", "")
    if not isinstance(outfit, str) or len(outfit) > 300:
        raise ValueError("群图服装描述无效")
    return {
        "should_draw": True, "prompt": prompt.strip(), "is_selfie": result["is_selfie"],
        "style": style, "outfit": outfit.strip(), "reason": reason.strip(),
    }


class GroupImageTask:
    """可取消的轮询；判断游标独立，实际生图与日程自拍共享群冷却。"""

    def __init__(self, plugin: "PainterMaimaiPlugin") -> None:
        self.plugin = plugin
        self.task: Optional[asyncio.Task] = None
        self.is_running = False

    async def start(self) -> None:
        if self.is_running:
            return
        cfg = self.plugin.config.group_image
        if cfg.min_messages > cfg.context_messages:
            raise ValueError("群聊主动图片的 min_messages 不能大于 context_messages")
        if any(not group_id or group_id != group_id.strip() for group_id in cfg.group_ids):
            raise ValueError("主动图片名单不能包含空群号或首尾空白")
        self.is_running = True
        self.task = asyncio.create_task(self._loop(), name="painter-maimai:group-image")
        logger.info("群聊主动图片任务已启动")

    async def stop(self) -> None:
        self.is_running = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        logger.info("群聊主动图片任务已停止")

    def _allowed(self, group_id: str, session_id: str = "") -> bool:
        return self.plugin.config.group_image.enabled and group_allowed(self.plugin, group_id, session_id)

    async def _loop(self) -> None:
        await asyncio.sleep(30)
        while self.is_running:
            for group_id in dict.fromkeys(self.plugin.config.group_image.group_ids):
                try:
                    async with asyncio.timeout(self.plugin.config.group_image.cycle_timeout_seconds):
                        await self._process_group(group_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("主动群图周期失败，已消费的上下文不会重复发送")
            await asyncio.sleep(self.plugin.config.group_image.interval_minutes * 60)

    async def _process_group(self, group_id: str) -> None:
        if not self._allowed(group_id):
            return
        cfg = self.plugin.config.group_image
        if cfg.probability <= 0:
            return
        context = await read_group_context(self.plugin, group_id)
        if context is None:
            return
        store = self.plugin.proactive_state
        target = f"group:{context.session_id}"
        if len(store.unconsumed(target, context.messages)) < cfg.min_messages:
            return
        provider = get_schedule_provider(self.plugin, include_life=True)
        activity = await provider.get_current_activity()
        block = activity_block_reason(self.plugin, activity, group=True, before_generation=True)
        if block:
            logger.info("跳过 %s 主动图片：%s", context.name, block)
            return
        assert activity is not None
        # 判断失败或跳过不占用共享冷却，但不再判断已消费的同一批消息。
        if not await store.claim_group_check(target, context.messages, cfg.min_messages, cfg.interval_minutes * 60):
            return
        decision = await self._decide(context.messages, activity)
        logger.info("群 %s 发图决定：%s，理由：%s", context.name, decision["should_draw"], decision.get("reason", ""))
        if not decision["should_draw"] or random.random() >= cfg.probability:
            return
        is_selfie = decision["is_selfie"]
        if is_selfie and not self.plugin.config.selfie.enabled:
            return
        if not self._allowed(group_id, context.session_id) or activity_block_reason(
            self.plugin, activity, automatic=is_selfie, group=True, before_generation=True,
        ):
            return
        model_id = proactive_model(self.plugin, is_selfie=is_selfie)
        if not self.plugin.runtime_state.is_model_enabled(context.session_id, model_id):
            return
        activity_key = activity.publication_key() if is_selfie else ""
        token = await store.reserve(
            source="group_image", target=target, cooldown_seconds=cfg.cooldown_minutes * 60,
            activity=activity.description, location=activity.location,
            activity_key=activity_key, prompt=decision["prompt"], reason=decision.get("reason", ""),
            is_selfie=is_selfie,
        )
        if token is None:
            return
        outcome, reason = "failed", "群图生成或发布失败"
        try:
            result = await self.plugin.api_generate_image(
                prompt=decision["prompt"], model_id=model_id,
                selfie_mode=is_selfie, selfie_style=decision["style"] if is_selfie else "standard",
                outfit=(activity.outfit or decision["outfit"]) if is_selfie else "", use_cache=False,
                recent_selfie_styles=store.recent_styles() if is_selfie else None,
                include_photo_plan=is_selfie,
                **activity_arguments(activity),
            )
            if not result.get("success") or not result.get("image_base64"):
                raise RuntimeError(f"主动群图生图失败: {result.get('error', '没有图片')}")
            if not await store.remember_image(token, result["image_base64"], selfie_style=result.get("selfie_style", ""), outfit=result.get("outfit", "")):
                outcome, reason = "skipped", "生成结果与近期图片重复"
                return
            caption = ""
            if is_selfie:
                if self.plugin.config.selfie.caption_enabled:
                    caption = await generate_caption(
                        self.plugin.ctx, activity, llm_task=self.plugin.config.basic.llm_task_name,
                        timeout_seconds=self.plugin.config.basic.llm_timeout_seconds,
                        destination="group", group_context=context.messages,
                        outfit=result.get("outfit", activity.outfit), photo_plan=result.get("photo_plan"),
                    )
                else:
                    caption = self.plugin.config.selfie.caption_text.strip()
                if not caption:
                    raise RuntimeError("群聊自拍配文为空，本轮不发送")
            if not self._allowed(group_id, context.session_id):
                outcome, reason = "skipped", "发送前群配置已关闭"
                return
            block = await recheck_activity(self.plugin, activity, provider, automatic=is_selfie, group=True)
            if block:
                outcome, reason = "skipped", f"发送前跳过：{block}"
                return
            if not self._allowed(group_id, context.session_id) or context.platform != self.plugin.config.group_image.platform or not self.plugin.runtime_state.is_model_enabled(context.session_id, model_id) or (is_selfie and not self.plugin.config.selfie.enabled):
                outcome, reason = "skipped", "发送前群、模型或自拍配置已禁用"
                return
            # 不重试发送：RPC 超时意味着投递状态未知，重发会产生重复消息。
            outcome = "unknown"
            if is_selfie:
                sent = await self.plugin.ctx.send.hybrid(
                    [{"type": "image", "content": result["image_base64"]}, {"type": "text", "content": caption}],
                    context.session_id, timeout_ms=30000,
                )
            else:
                sent = await self.plugin.ctx.send.image(result["image_base64"], context.session_id, timeout_ms=30000)
            if sent is not True:
                raise RuntimeError("主动群图未确认发送成功，本轮不重发")
            outcome, reason = "sent", decision.get("reason", "")
            logger.info("已向 %s 发送主动图片", context.name)
        except BaseException as exc:
            reason = str(exc) or "本轮被取消"
            raise
        finally:
            await store.finish(token, outcome, reason)

    async def _decide(self, context: List[Dict[str, str]], activity: ActivityInfo) -> Dict[str, Any]:
        payload = {
            "messages": context,
            "persona": await _get_bot_profile(self.plugin.ctx),
            "activity": activity.to_payload(),
            "recent_publications": self.plugin.proactive_state.recent(),
        }
        system_prompt = await asyncio.to_thread(load_prompt_template, "group_image_decision")
        result = await self.plugin.ctx.llm.generate(
            prompt=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            model=self.plugin.config.basic.llm_task_name, temperature=0.2, max_tokens=2048,
            timeout_ms=self.plugin.config.basic.llm_timeout_seconds * 1000,
        )
        if not isinstance(result, dict) or result.get("success") is False:
            raise RuntimeError("群图判断 LLM 调用失败")
        response = result.get("response", result.get("content", ""))
        if not isinstance(response, str):
            raise ValueError("群图判断 LLM 返回了非文本响应")
        return parse_decision(response)
