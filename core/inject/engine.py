"""Maisaka 规划器 / 回复器 Hook 的注入引擎。

Hook 内只使用内存快照和内存缓存：不查库、不调 LLM、不发网络请求。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set
from uuid import uuid4

import logging
import re
import time

from .content_template import INJECT_MARKER, render_content
from .context_cache import ConversationContextCache
from .intent_classifier import IntentClassifier, UserIntent
from .optimizer import InjectOptimizer
from .state_analyzer import analyze_activity_state
from ..selfie.schedule_provider import ActivityInfo

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin

logger = logging.getLogger("plugin.painter_maimai_daily.inject")

_MESSAGE_PREFIX = re.compile(r"^\s*<message\b[^>]*>\s*", re.IGNORECASE)
_METADATA_PREFIXES = (
    "当前时间：", "当前时间:", "【人物画像-内部参考】", "<system-reminder>", INJECT_MARKER,
)


def latest_user_text(items: Sequence[Dict[str, Any]]) -> str:
    """取最后一条真实用户文本，跳过主程序插入的元信息与纯图片消息。"""
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = _MESSAGE_PREFIX.sub("", str(part.get("text", "")), count=1).strip()
            if text and not text.startswith(_METADATA_PREFIXES):
                return text
    return ""


def has_inject_marker(items: Sequence[Dict[str, Any]]) -> bool:
    """同一请求被重复分发时，只要已有标记就不再追加第二条。"""
    for item in items:
        if not isinstance(item, dict):
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text" and str(
                part.get("text", "")
            ).startswith(INJECT_MARKER):
                return True
    return False


def append_context_item(items: List[Dict[str, Any]], content: str) -> List[Dict[str, Any]]:
    """在历史末尾追加 UserMessageItem；保留主程序原有全部字段。"""
    existing_ids = {
        str((item.get("meta") if isinstance(item.get("meta"), dict) else {}).get("item_id", ""))
        for item in items if isinstance(item, dict)
    }
    item_id = uuid4().hex
    while item_id in existing_ids:
        item_id = uuid4().hex
    return [
        *items,
        {
            "item_type": "UserMessageItem",
            "meta": {
                "item_id": item_id,
                "logical_turn_id": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "parts": [{"type": "text", "text": content}],
        },
    ]


class InjectionEngine:
    """实例级注入状态；配置热更新后由插件重新创建。"""

    def __init__(self, plugin: "PainterMaimaiPlugin") -> None:
        """按当前配置初始化分类器、缓存与节流器。"""
        self.plugin = plugin
        config = plugin.config.inject
        self.classifier = IntentClassifier()
        self.context = ConversationContextCache(config.context_max_turns, config.context_ttl_seconds)
        self.optimizer = InjectOptimizer(
            cache_ttl=config.context_ttl_seconds,
            casual_inject_probability=config.casual_chat_inject_probability,
            min_seconds=config.min_seconds,
            min_messages=config.min_messages,
        )
        # attempt > 1 时主程序会重新传入原始 extra_prompt，必须复用同一段背景。
        self._replyer_contexts: Dict[str, tuple[float, str, str]] = {}
        self._last_cleanup = time.monotonic()
        self.allowed_streams: Optional[Set[str]] = (
            set(config.allowed_streams) if config.allowed_streams else None
        )

    def _enabled_for(self, session_id: str) -> bool:
        """检查总开关、日程来源与会话白名单。"""
        config = self.plugin.config
        return (
            config.inject.enabled
            and config.schedule.enabled
            and self.plugin.schedule_manager is not None
            and (self.allowed_streams is None or session_id in self.allowed_streams)
        )

    def _cleanup(self) -> None:
        """周期性清理纯内存缓存，不访问数据库或 SDK。"""
        now = time.monotonic()
        if now - self._last_cleanup < 300:
            return
        self.optimizer.cleanup_expired_cache()
        self.context.cleanup_expired()
        self._replyer_contexts = {
            key: value for key, value in self._replyer_contexts.items()
            if now - value[0] <= self.context.ttl
        }
        self._last_cleanup = now

    def _current_activity(self) -> Optional[ActivityInfo]:
        """读取管理器最近一次快照；世代不匹配（跨日）时不注入。"""
        manager = self.plugin.schedule_manager
        if manager is None:
            return None
        snapshot = manager.snapshot
        if snapshot is None or snapshot.current is None:
            return None
        if snapshot.schedule_date != manager.now().date().isoformat():
            return None
        return snapshot.current

    def planner(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """处理 ``maisaka.planner.before_request``，返回整份 kwargs。"""
        session_id = str(kwargs.get("session_id", ""))
        config = self.plugin.config.inject
        if not self._enabled_for(session_id) or not config.inject_into_planner:
            return {"action": "continue"}
        if kwargs.get("item_schema_version") != 1:
            raise ValueError("绘卷注入仅支持 Context Item schema 1")
        items = kwargs.get("items")
        if not isinstance(items, list):
            raise ValueError("Planner Hook 缺少 items 列表")

        self._cleanup()
        if has_inject_marker(items):
            return {"action": "continue"}

        user_text = latest_user_text(items)
        sequence = self.optimizer.note_message(session_id)
        intent, confidence = (
            self.classifier.classify(user_text)
            if config.enable_intent_classification
            else (UserIntent.CASUAL_CHAT, 0.5)
        )
        activity = self._current_activity()
        activity_name = activity.description if activity is not None else ""
        continuing, continuation_reason = self.context.should_continue_inject(session_id, activity_name)
        should_inject = activity is not None
        if intent in (UserIntent.TECH_QUESTION, UserIntent.COMMAND_EXECUTION):
            should_inject = False
            reason = f"{intent.value} 场景，跳过注入"
        elif should_inject and config.enable_inject_optimization and not continuing:
            should_inject, reason = self.optimizer.should_inject(
                session_id, intent, activity_name, confidence, sequence,
            )
        else:
            reason = None

        content = ""
        if should_inject and activity is not None:
            state = analyze_activity_state(activity) if config.enable_state_analysis else None
            content = render_content(
                activity,
                max_future=config.max_future_activities,
                intent=intent,
                state=state,
            )
        self.context.add_turn(
            session_id, user_text, intent=intent.value,
            injected=bool(content), activity=activity_name or None,
        )
        if not content:
            logger.debug("日程不注入 session=%s reason=%s", session_id, reason or "无有效活动")
            return {"action": "continue"}
        if config.enable_inject_optimization:
            self.optimizer.record_injection(session_id, activity_name, content, intent, sequence)
            if continuation_reason:
                logger.debug("日程续注原因: %s", continuation_reason)

        updated = dict(kwargs)
        updated["items"] = append_context_item(items, content)
        logger.info("日程已注入规划器 session=%s intent=%s activity=%s", session_id, intent.value, activity_name)
        return {"action": "continue", "modified_kwargs": updated}

    def replyer(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """处理 ``maisaka.replyer.before_request``，只追加 extra_prompt。"""
        session_id = str(kwargs.get("session_id", ""))
        config = self.plugin.config.inject
        if not self._enabled_for(session_id) or not config.inject_into_replyer:
            return {"action": "continue"}

        attempt = kwargs.get("attempt", 1)
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("Replyer attempt 必须为正整数")
        extra_prompt = kwargs.get("extra_prompt", "")
        if not isinstance(extra_prompt, str):
            raise ValueError("Replyer extra_prompt 必须为字符串")
        if INJECT_MARKER in extra_prompt:
            return {"action": "continue"}

        self._cleanup()
        activity = self._current_activity()
        if activity is None:
            return {"action": "continue"}
        activity_key = activity.publication_key()

        if attempt > 1:
            cached = self._replyer_contexts.get(session_id)
            if cached is None or time.monotonic() - cached[0] > self.context.ttl or cached[1] != activity_key:
                return {"action": "continue"}
            updated = dict(kwargs)
            updated["extra_prompt"] = "\n".join(part for part in (extra_prompt, cached[2]) if part)
            return {"action": "continue", "modified_kwargs": updated}

        self._replyer_contexts.pop(session_id, None)
        turn = self.context.get_recent_turns(session_id, 1)
        latest_turn = turn[-1] if turn else None
        if latest_turn is not None and latest_turn.intent in (
            UserIntent.TECH_QUESTION.value, UserIntent.COMMAND_EXECUTION.value,
        ):
            return {"action": "continue"}

        planner_injected = bool(
            latest_turn is not None and latest_turn.injected and latest_turn.activity == activity.description
        )
        if not planner_injected and config.enable_inject_optimization:
            should_inject, _reason = self.optimizer.should_inject_replyer(
                session_id, activity.description, self.optimizer.sequence_of(session_id),
            )
            if not should_inject:
                return {"action": "continue"}

        state = analyze_activity_state(activity) if config.enable_state_analysis else None
        content = render_content(
            activity,
            max_future=min(1, config.max_future_activities),
            state=state,
            replyer=True,
        )
        if not content:
            return {"action": "continue"}
        if config.enable_inject_optimization and not planner_injected:
            self.optimizer.record_injection(
                session_id, activity.description, content,
                UserIntent.CASUAL_CHAT, self.optimizer.sequence_of(session_id),
            )
        self._replyer_contexts[session_id] = (time.monotonic(), activity_key, content)

        updated = dict(kwargs)
        updated["extra_prompt"] = "\n".join(part for part in (extra_prompt, content) if part)
        logger.info("日程已注入回复器 session=%s activity=%s", session_id, activity.description)
        return {"action": "continue", "modified_kwargs": updated}
