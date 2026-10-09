"""
POST_LLM 日程注入处理器（增强版）

在 LLM 生成回复之前，将麦麦当前的日程信息注入到 prompt 中，
让 LLM 知道麦麦"现在在做什么"，从而生成更有代入感的回复。

always 模式每次提供北京时间、日历事实、精确当前活动与唯一下一项计划。
smart 模式保留原有关键词过滤和按会话限频，文本格式与 always 一致。

注入时机：POST_LLM（消息经过前处理后、LLM 调用前）
注入方式：在 llm_prompt 前面追加一段日程上下文
"""

import time
import logging
from typing import Dict, Optional, Tuple

from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.component_types import (
    EventType,
    CustomEventHandlerResult,
)

from .inject.context_cache import get_context_cache
from .inject.intent_classifier import classify_intent, IntentType
from .inject.schedule_template import render_schedule_context
from .schedule.calendar_context import get_calendar_service, now_beijing

logger = logging.getLogger(__name__)

# 内存级 per-stream 节流记录：{stream_id: last_inject_ts}
_stream_throttle: Dict[str, float] = {}
# 内存级 per-stream 消息计数：{stream_id: count_since_last_inject}
_stream_msg_count: Dict[str, int] = {}


class ScheduleContextHandler(BaseEventHandler):
    """在接收消息时记录用户上下文，供后续日程注入按会话查询。"""

    event_type = EventType.ON_MESSAGE
    handler_name = "selfie_schedule_context_handler"
    handler_description = "记录日程对话上下文"
    intercept_message = True

    async def execute(self, message=None):
        """按消息的流 ID 保存非命令文本，返回宿主约定的五元组。"""
        if message is not None and message.stream_id and message.plain_text.strip():
            # 命令不属于自然对话，避免配置操作污染日程话题识别。
            if not message.plain_text.strip().startswith("/"):
                cache = get_context_cache(
                    stream_id=message.stream_id,
                    max_turns=self.get_config("schedule_inject.schedule_context_cache_max_turns", 10),
                    ttl_minutes=self.get_config("schedule_inject.schedule_context_cache_ttl_minutes", 30),
                )
                cache.add_turn(message.plain_text, "")
        return True, True, None, None, message


class ScheduleInjectHandler(BaseEventHandler):
    """
    日程注入 EventHandler（增强版）

    在 POST_LLM 阶段将麦麦当前活动信息注入到 LLM prompt 中，
    让回复更贴合角色当前的"生活状态"。

    默认每次更新状态；仅 smart 模式使用意图识别与限频。
    """

    event_type = EventType.POST_LLM
    handler_name: str = "selfie_schedule_inject_handler"
    handler_description: str = "在 LLM 调用前注入时间、节假日、当前活动及下一项计划"
    weight: int = 10
    intercept_message: bool = True

    async def execute(
        self,
        message=None,
    ) -> Tuple[bool, bool, Optional[str], Optional[CustomEventHandlerResult], Optional[object]]:
        """
        执行日程注入

        Returns:
            (success, should_continue, optional_msg, custom_result, modified_message)
        """
        if message is None:
            return (True, True, None, None, None)

        # 检查全局开关
        enabled = self.get_config("schedule_inject.enabled", True)
        if not enabled:
            return (True, True, None, None, message)

        # 获取用户消息
        plain_text = getattr(message, "plain_text", "") or ""

        # 跳过命令消息（以 / 开头）
        if plain_text.strip().startswith("/"):
            return (True, True, None, None, message)

        mode = self.get_config("schedule_inject.mode", "always")
        if mode not in {"smart", "always"}:
            raise ValueError(f"日程注入模式无效：{mode!r}，仅支持 smart 或 always")

        # 获取 stream_id
        stream_id = getattr(message, "stream_id", None) or ""

        # ========== 智能注入增强 ==========

        # 1. 获取对话上下文缓存
        context_cache_ttl = self.get_config("schedule_inject.schedule_context_cache_ttl_minutes", 30)
        context_cache_max_turns = self.get_config("schedule_inject.schedule_context_cache_max_turns", 10)
        context_cache = get_context_cache(
            stream_id=stream_id,
            max_turns=context_cache_max_turns,
            ttl_minutes=context_cache_ttl,
        )
        # 宿主仅提供提示词时，从同一会话的接收消息记录恢复用户文本。
        recent_messages = context_cache.get_recent_messages(1)
        if not plain_text and recent_messages:
            plain_text = recent_messages[-1]

        # 2. 意图识别
        intent_enabled = self.get_config("schedule_inject.schedule_intent_enable", True)
        should_inject = True
        inject_reason = ""

        # always 必须每次提供状态，不让“怎么还没睡”等生活问句被技术关键词误过滤。
        if mode == "smart" and intent_enabled:
            intent_result = classify_intent(plain_text)
            should_inject, inject_reason = self._should_inject_by_intent(intent_result)

            logger.debug(
                f"[ScheduleInject] 意图识别: {intent_result.intent.value}, "
                f"置信度: {intent_result.confidence:.2f}, "
                f"是否注入: {should_inject}, 原因: {inject_reason}"
            )

            if not should_inject:
                return (True, True, None, None, message)

        # 3. 检查是否正在讨论日程话题
        is_discussing_schedule = context_cache.is_discussing_schedule()
        if is_discussing_schedule:
            logger.debug("[ScheduleInject] 检测到正在讨论日程话题，提高注入优先级")

        # 4. 检查 per-stream DB override
        try:
            override = await self._get_inject_override(stream_id)
            if override is not None and not override:
                return (True, True, None, None, message)
        except Exception as e:
            logger.debug(f"检查注入 override 失败: {e}")

        # 5. 节流检查（smart 模式）
        if mode == "smart" and not is_discussing_schedule and not self._should_inject_throttle(stream_id):
            # 增加消息计数
            _stream_msg_count[stream_id] = _stream_msg_count.get(stream_id, 0) + 1
            return (True, True, None, None, message)

        # ========== 获取日程信息 ==========

        try:
            from .schedule.schedule_manager import get_schedule_manager

            manager = get_schedule_manager()
            now = now_beijing()
            calendar = await get_calendar_service().get_day(now.date())
            # 首次下载可能跨越活动或日期边界，查询活动前重新取时，避免注入下载前的旧状态。
            now = now_beijing()
            if now.date() != calendar.date:
                calendar = await get_calendar_service().get_day(now.date())
            snapshot = await manager.get_injection_snapshot(now, calendar)
        except Exception as e:
            logger.warning(f"获取日程信息失败: {e}")
            return (True, True, None, None, message)

        # ========== 构建注入文本 ==========

        inject_text = render_schedule_context(snapshot, calendar)

        if not inject_text:
            return (True, True, None, None, message)

        # ========== 注入到 llm_prompt ==========（修复 F3：调用 modify_llm_prompt）

        current_prompt = getattr(message, "llm_prompt", "") or ""
        message.llm_prompt = inject_text + "\n\n" + current_prompt

        # 必须调用 modify_llm_prompt() 以设置宿主修改标记
        message.modify_llm_prompt(message.llm_prompt)

        # 更新节流状态
        _stream_throttle[stream_id] = time.time()
        _stream_msg_count[stream_id] = 0

        logger.debug(f"已注入日程信息到 stream {stream_id}")
        return (True, True, None, None, message)

    def _should_inject_by_intent(self, intent_result) -> Tuple[bool, str]:
        """
        根据意图判断是否应该注入

        Args:
            intent_result: 意图识别结果

        Returns:
            tuple: (是否注入, 原因)
        """
        intent = intent_result.intent

        # 询问日程 → 注入
        if intent == IntentType.SCHEDULE_QUERY:
            return True, "用户询问日程"

        # 修改日程 → 注入
        if intent == IntentType.SCHEDULE_MODIFY:
            return True, "用户想修改日程"

        # 技术问答 → 不注入
        if intent == IntentType.TECH_QUESTION:
            return False, "技术问答，不需要日程信息"

        # 命令 → 不注入
        if intent == IntentType.COMMAND:
            return False, "命令消息，不需要日程信息"

        # 闲聊 → 注入
        if intent == IntentType.CASUAL_CHAT:
            return True, "闲聊，可以注入日程增加自然感"

        # 其他 → 默认注入
        return True, "默认注入"

    def _should_inject_throttle(self, stream_id: str) -> bool:
        """
        smart 模式下的节流判断

        满足以下任一条件才注入：
        1. 该 stream 从未注入过
        2. 距离上次注入已超过 min_seconds
        3. 距离上次注入已有 min_messages 次回复调用被限频跳过
        """
        min_seconds = self.get_config("schedule_inject.min_seconds", 300)
        min_messages = self.get_config("schedule_inject.min_messages", 5)

        last_ts = _stream_throttle.get(stream_id)
        if last_ts is None:
            return True

        elapsed = time.time() - last_ts
        if elapsed >= min_seconds:
            return True

        msg_count = _stream_msg_count.get(stream_id, 0)
        if msg_count >= min_messages:
            return True

        return False

    async def _get_inject_override(self, stream_id: str) -> Optional[bool]:
        """
        从 DB 读取 per-stream 的注入开关 override

        Returns:
            True = 强制开启, False = 强制关闭, None = 使用全局配置
        """
        if not stream_id:
            return None

        try:
            from .schedule.schedule_manager import get_schedule_manager

            manager = get_schedule_manager()
            key = f"schedule_inject_enabled_override:{stream_id}"
            value = await manager.get_state(key)
            if value is None:
                return None
            return value.lower() in ("true", "1", "yes", "on")
        except Exception:
            return None
