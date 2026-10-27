"""自动自拍：一轮判断后把同一张照片按需发到空间、群和私聊。

每个目标独立冷却、独立预占、独立结算；单个渠道失败不会阻断其他渠道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import asyncio
import logging
import random
import time

from ..utils.time_utils import is_in_time_range
from .caption_generator import generate_caption
from .private_targets import resolve_private_context
from .proactive_context import (
    GroupContext,
    activity_arguments,
    activity_block_reason,
    group_allowed,
    proactive_model,
    read_group_context,
    recheck_activity,
)
from .publication_decision import decide_publication
from .schedule_provider import get_schedule_provider

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin
    from ..qzone.publisher import QzonePublisher

logger = logging.getLogger("plugin.painter_maimai_daily.auto_selfie")


@dataclass
class _Target:
    """一次自动自拍的发送目标；identifier 同时用于 LLM 候选 id。"""

    identifier: str
    channel: str
    name: str
    session_id: str = ""
    group_id: str = ""
    user_id: str = ""
    messages: List[Dict[str, str]] = field(default_factory=list)

    def decision_payload(self) -> Dict[str, Any]:
        """转换为决策提示词中的候选结构。"""
        payload: Dict[str, Any] = {
            "id": self.identifier,
            "channel": self.channel,
            "name": self.name,
        }
        if self.messages:
            payload["messages"] = self.messages
        return payload


class AutoSelfieTask:
    """带安静时段、活动限制、分渠道冷却和失败退避的自动自拍任务。"""

    def __init__(self, plugin: "PainterMaimaiPlugin") -> None:
        """保存插件引用和周期状态。"""
        self.plugin = plugin
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        self._last_attempt_at: Optional[float] = None
        self._consecutive_failures = 0

    async def start(self) -> None:
        """启动循环；关闭配文时必须已有非空固定文案。"""
        if self.is_running:
            return
        if not self.plugin.config.selfie.caption_enabled and not self.plugin.config.selfie.caption_text.strip():
            raise ValueError("关闭自动配文后必须配置非空的固定配文")
        self.is_running = True
        self.task = asyncio.create_task(self._selfie_loop(), name="painter-maimai:auto-selfie")
        logger.info("自动自拍任务已启动")

    async def stop(self) -> None:
        """取消并等待循环退出，避免卸载后继续生成。"""
        self.is_running = False
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        logger.info("自动自拍任务已停止")

    def _is_quiet_hours(self) -> bool:
        """安静时段支持跨午夜；起止相同表示不设静默。"""
        cfg = self.plugin.config.selfie
        return cfg.quiet_hours_start != cfg.quiet_hours_end and is_in_time_range(
            cfg.quiet_hours_start, cfg.quiet_hours_end,
        )

    def _allowed(self) -> bool:
        """检查插件、自拍、自动自拍和安静时段四个总开关。"""
        cfg = self.plugin.config.selfie
        return (
            self.plugin.config.plugin.enabled
            and cfg.enabled
            and cfg.auto_enabled
            and not self._is_quiet_hours()
        )

    def _has_enabled_channel(self) -> bool:
        """至少有一个渠道在配置层开启，避免空跑。"""
        cfg = self.plugin.config.selfie
        return (
            (cfg.qzone_enabled and self.plugin.qzone_publisher is not None)
            or cfg.group_enabled
            or (cfg.private_enabled and bool(cfg.private_user_ids))
        )

    async def _selfie_loop(self) -> None:
        """按判断间隔轮询；连续失败时指数退避，避免风控期间反复尝试。"""
        await asyncio.sleep(30)
        while self.is_running:
            try:
                cfg = self.plugin.config.selfie
                interval = cfg.check_interval_minutes * 60 * (2 ** min(self._consecutive_failures // 3, 4))
                elapsed = time.monotonic() - self._last_attempt_at if self._last_attempt_at is not None else interval
                if elapsed >= interval:
                    async with asyncio.timeout(cfg.cycle_timeout_seconds):
                        await self._execute_selfie()
                    self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._consecutive_failures += 1
                logger.exception("自动自拍周期失败，本轮不会重试发布")
            await asyncio.sleep(30)

    async def _collect_targets(self, activity: Any, model_id: str) -> List[_Target]:
        """按各渠道自己的开关、静默、冷却和会话状态收集候选。"""
        cfg = self.plugin.config.selfie
        store = self.plugin.proactive_state
        targets: List[_Target] = []

        qzone_allowed = bool(
            cfg.qzone_enabled
            and self.plugin.qzone_publisher is not None
            and self.plugin.qzone_publisher.available
            and store.available(
                "qzone", cfg.interval_minutes * 60,
                source="auto_selfie", source_cooldown=cfg.interval_minutes * 60,
            )
        )
        if qzone_allowed:
            targets.append(_Target("qzone", "qzone", "QQ空间"))

        if cfg.group_enabled:
            groups: List[GroupContext] = []
            for group_id in dict.fromkeys(self.plugin.config.group_image.group_ids):
                try:
                    context = await read_group_context(self.plugin, group_id)
                    if context is None or not context.messages:
                        continue
                    if activity.activity_type in self.plugin.config.group_image.disabled_activity_types:
                        continue
                    if not store.available(
                        f"group:{context.session_id}",
                        self.plugin.config.group_image.cooldown_minutes * 60,
                        source="auto_selfie", source_cooldown=cfg.interval_minutes * 60,
                    ):
                        continue
                    if not self.plugin.runtime_state.is_model_enabled(context.session_id, model_id):
                        continue
                    groups.append(context)
                except Exception:
                    logger.exception("读取自动自拍候选群 %s 失败，本轮排除该群", group_id)
            for index, context in enumerate(groups, start=1):
                targets.append(
                    _Target(
                        f"group_{index}", "group", context.name,
                        session_id=context.session_id, group_id=context.group_id,
                        messages=context.messages,
                    )
                )

        if cfg.private_enabled and cfg.private_user_ids:
            for user_id in dict.fromkeys(str(item).strip() for item in cfg.private_user_ids):
                if not user_id:
                    continue
                try:
                    context = await resolve_private_context(self.plugin, user_id)
                    if context is None:
                        continue
                    if not store.available(
                        f"private:{context.session_id}", cfg.interval_minutes * 60,
                        source="auto_selfie", source_cooldown=cfg.interval_minutes * 60,
                    ):
                        continue
                    if not self.plugin.runtime_state.is_plugin_enabled(
                        context.session_id, self.plugin.config.plugin.enabled,
                    ):
                        continue
                    if not self.plugin.runtime_state.is_model_enabled(context.session_id, model_id):
                        continue
                    targets.append(
                        _Target(
                            f"private_{len([t for t in targets if t.channel == 'private']) + 1}",
                            "private", context.name,
                            session_id=context.session_id, user_id=context.user_id,
                        )
                    )
                except Exception:
                    logger.exception("解析自动自拍私聊目标 %s 失败，本轮排除该目标", user_id)

        return targets

    async def _execute_selfie(self) -> None:
        """执行一轮多渠道判断；空决策、判重或概率未命中都直接结束。"""
        cfg = self.plugin.config.selfie
        if not self._allowed() or cfg.probability <= 0 or not self._has_enabled_channel():
            return
        self._last_attempt_at = time.monotonic()
        if not await self.plugin.proactive_state.claim_check("auto_selfie", cfg.check_interval_minutes * 60):
            return

        provider = get_schedule_provider(self.plugin, include_life=True)
        activity = await provider.get_current_activity()
        block = activity_block_reason(self.plugin, activity, automatic=True, before_generation=True)
        if block:
            logger.info("跳过自动自拍：%s", block)
            return
        assert activity is not None

        model_id = proactive_model(self.plugin, is_selfie=True, automatic=True)
        targets = await self._collect_targets(activity, model_id)
        if not targets:
            return

        decision = await decide_publication(
            self.plugin, activity, [target.decision_payload() for target in targets],
        )
        logger.info("自动自拍决定：%s，理由：%s", decision["destinations"], decision["reason"])
        if not decision["destinations"] or random.random() >= cfg.probability:
            return
        if not self._allowed() or activity_block_reason(
            self.plugin, activity, automatic=True, before_generation=True,
        ):
            return

        by_id = {target.identifier: target for target in targets}
        selected = [by_id[identifier] for identifier in decision["destinations"]]
        activity_key = activity.publication_key()
        if self.plugin.proactive_state.batch_duplicate(
            decision["prompt"], is_selfie=True, activity_key=activity_key,
        ):
            logger.info("自动自拍跳过：本轮描述或活动已在近期发布")
            return

        reserved: List[tuple[_Target, str]] = []
        for target in selected:
            token = await self.plugin.proactive_state.reserve(
                source="auto_selfie",
                target=self._store_key(target),
                cooldown_seconds=(
                    self.plugin.config.group_image.cooldown_minutes * 60
                    if target.channel == "group" else cfg.interval_minutes * 60
                ),
                # 候选收集阶段已按来源冷却筛选过；同一批预占不能再被第一条
                # 刚写入的 source 时间挡掉，否则永远只能发一个渠道。
                source_cooldown=0,
                activity_key=activity_key,
                activity=activity.description,
                location=activity.location,
                prompt=decision["prompt"],
                reason=decision["reason"],
                is_selfie=True,
                messages=target.messages or None,
                dedupe=False,
            )
            if token is not None:
                reserved.append((target, token))
        if not reserved:
            return

        finished: set[str] = set()

        async def finish(token: str, status: str, reason: str) -> None:
            """保证每个预占只结算一次。"""
            if token in finished:
                return
            finished.add(token)
            await self.plugin.proactive_state.finish(token, status, reason)

        async def finish_all(status: str, reason: str) -> None:
            """批量结算尚未处理的目标。"""
            for _target, token in reserved:
                await finish(token, status, reason)

        try:
            result = await self.plugin.api_generate_image(
                prompt=decision["prompt"],
                model_id=model_id,
                selfie_mode=True,
                selfie_style=cfg.default_style,
                use_cache=False,
                outfit=activity.outfit,
                recent_selfie_styles=self.plugin.proactive_state.recent_styles(),
                include_photo_plan=True,
                **activity_arguments(activity),
            )
            if not result.get("success") or not result.get("image_base64"):
                raise RuntimeError(f"自动自拍生图失败: {result.get('error', '没有图片')}")
            if not await self.plugin.proactive_state.remember_image(
                reserved[0][1], result["image_base64"],
                selfie_style=result.get("selfie_style", ""),
                outfit=result.get("outfit", ""),
            ):
                await finish_all("skipped", "生成结果与近期图片重复")
                return

            captions = await self._build_captions(reserved, activity, result)
            if not self._allowed():
                await finish_all("skipped", "发布前配置已关闭")
                return
            block = await recheck_activity(
                self.plugin, activity, provider, automatic=True,
                group=any(target.channel == "group" for target, _token in reserved),
            )
            if block:
                await finish_all("skipped", f"发布前跳过：{block}")
                return

            for target, token in reserved:
                caption = captions.get(target.identifier)
                if caption is None:
                    await finish(token, "skipped", "该渠道配文生成失败")
                    continue
                try:
                    status, reason = await self._publish_one(target, result["image_base64"], caption, model_id)
                except Exception as exc:
                    logger.exception("自动自拍发布到 %s 失败，不影响其他渠道", target.name)
                    status, reason = "failed", str(exc) or "发送异常"
                await finish(token, status, reason)
        except BaseException as exc:
            await finish_all("failed", str(exc) or "本轮被取消")
            raise

    def _store_key(self, target: _Target) -> str:
        """冷却记录使用渠道 + 真实会话，避免群与私聊 ID 冲突。"""
        if target.channel == "group":
            return f"group:{target.session_id}"
        if target.channel == "private":
            return f"private:{target.session_id}"
        return "qzone"

    async def _build_captions(
        self,
        reserved: List[tuple[_Target, str]],
        activity: Any,
        result: Dict[str, Any],
    ) -> Dict[str, Optional[str]]:
        """按配文粒度生成文本；失败的渠道返回 ``None`` 并跳过。"""
        cfg = self.plugin.config.selfie
        if not cfg.caption_enabled:
            text = cfg.caption_text.strip()
            if not text:
                raise ValueError("自动自拍固定配文不能为空")
            return {target.identifier: text for target, _token in reserved}

        if cfg.caption_mode == "shared":
            caption = await generate_caption(
                self.plugin.ctx,
                activity,
                llm_task=self.plugin.config.basic.llm_task_name,
                timeout_seconds=self.plugin.config.basic.llm_timeout_seconds,
                destination="multi",
                outfit=result.get("outfit", activity.outfit),
                photo_plan=result.get("photo_plan"),
            )
            if not caption:
                raise RuntimeError("自动自拍共用配文生成失败，本轮不发布")
            return {target.identifier: caption for target, _token in reserved}

        captions: Dict[str, Optional[str]] = {}
        for target, _token in reserved:
            caption = await generate_caption(
                self.plugin.ctx,
                activity,
                llm_task=self.plugin.config.basic.llm_task_name,
                timeout_seconds=self.plugin.config.basic.llm_timeout_seconds,
                destination=target.channel,
                group_context=target.messages or None,
                outfit=result.get("outfit", activity.outfit),
                photo_plan=result.get("photo_plan"),
            )
            captions[target.identifier] = caption or None
        return captions

    async def _publish_one(
        self, target: _Target, image_data: str, caption: str, model_id: str,
    ) -> tuple[str, str]:
        """按渠道发布；返回主动图片状态机允许的状态与原因。"""
        cfg = self.plugin.config.selfie
        if target.channel == "qzone":
            if not cfg.qzone_enabled or self.plugin.qzone_publisher is None:
                return "skipped", "发布前空间渠道已关闭"
            result = await self.plugin.qzone_publisher.publish(self.plugin, image_data, caption)
            if result.status == "sent":
                logger.info("自动自拍已发布到 QQ 空间")
                return "sent", "空间发布成功"
            if result.status == "uncertain":
                return "unknown", result.error or "空间发布结果待核对"
            return "failed", result.error or "空间发布被拒绝"

        if target.channel == "group":
            if (
                not cfg.group_enabled
                or not group_allowed(self.plugin, target.group_id, target.session_id)
                or not self.plugin.runtime_state.is_model_enabled(target.session_id, model_id)
            ):
                return "skipped", "发布前目标群或模型已禁用"
        else:
            private_ids = {str(item).strip() for item in self.plugin.config.selfie.private_user_ids}
            if (
                not cfg.private_enabled
                or target.user_id not in private_ids
                or not self.plugin.runtime_state.is_plugin_enabled(
                    target.session_id, self.plugin.config.plugin.enabled,
                )
                or not self.plugin.runtime_state.is_model_enabled(target.session_id, model_id)
            ):
                return "skipped", "发布前私聊目标或模型已禁用"

        sent = await self.plugin.ctx.send.hybrid(
            [{"type": "image", "content": image_data}, {"type": "text", "content": caption}],
            target.session_id,
            timeout_ms=30000,
        )
        if sent is not True:
            raise RuntimeError("自拍图文消息未确认发送成功，本轮不重发")
        logger.info("自动自拍已发送到 %s %s", target.channel, target.name)
        return "sent", "发送成功"
