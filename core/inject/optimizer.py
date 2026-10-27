"""注入时机优化：按意图、活动、冷却与消息数决定是否值得再次注入。"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import random
import time

from .intent_classifier import UserIntent


class InjectOptimizer:
    """会话级注入历史；只驻留内存，重启后自然清空。"""

    def __init__(
        self,
        *,
        cache_ttl: int = 600,
        casual_inject_probability: float = 0.5,
        min_seconds: int = 300,
        min_messages: int = 5,
    ) -> None:
        """保存冷却参数；调用方负责把配置值传进来。"""
        self.cache_ttl = cache_ttl
        self.casual_inject_probability = casual_inject_probability
        self.min_seconds = min_seconds
        self.min_messages = min_messages
        self.inject_history: Dict[str, Dict] = {}
        self._message_seq: Dict[str, int] = {}
        self._message_time: Dict[str, float] = {}

    def note_message(self, session_id: str) -> int:
        """记录一次规划请求并返回递增序号，用于“最少新消息”判断。"""
        sequence = self._message_seq.get(session_id, 0) + 1
        self._message_seq[session_id] = sequence
        self._message_time[session_id] = time.time()
        return sequence

    def sequence_of(self, session_id: str) -> int:
        """返回当前消息序号，供回复阶段沿用 planner 的节流状态。"""
        return self._message_seq.get(session_id, 0)

    def should_inject(
        self,
        session_id: str,
        intent: UserIntent,
        current_activity: Optional[str],
        confidence: float,
        sequence: int,
    ) -> Tuple[bool, Optional[str]]:
        """按硬规则判断是否注入；返回原因便于日志定位。"""
        if intent in (UserIntent.TECH_QUESTION, UserIntent.COMMAND_EXECUTION):
            return False, f"{intent.value} 场景，跳过注入"
        if not current_activity and intent not in (UserIntent.QUERY_CURRENT, UserIntent.QUERY_FUTURE):
            return False, "无当前活动且非询问意图"

        now = time.time()
        history = self.inject_history.get(session_id)
        if history is not None and intent != UserIntent.QUERY_FUTURE:
            elapsed = now - float(history.get("last_time", 0.0))
            same_activity = history.get("last_activity") == current_activity
            same_intent = history.get("last_intent") == intent.value
            if elapsed < self.cache_ttl and same_activity and same_intent:
                return False, f"短时间内重复注入（{int(elapsed)} 秒前已注入相同活动和意图）"
            new_messages = sequence - int(history.get("last_seq", 0))
            if elapsed < self.min_seconds and new_messages < self.min_messages:
                return False, f"冷却中：{int(elapsed)} 秒内仅新增 {new_messages} 条消息"

        if intent == UserIntent.CASUAL_CHAT and random.random() >= self.casual_inject_probability:
            return False, "闲聊随机决定不注入"
        if confidence < 0.4:
            return False, f"意图置信度过低（{confidence:.2f}）"
        return True, None

    def should_inject_replyer(
        self, session_id: str, current_activity: str, sequence: int,
    ) -> Tuple[bool, Optional[str]]:
        """回复器没有新的用户消息；沿用 planner 冷却但忽略“最少新消息”。"""
        history = self.inject_history.get(session_id)
        if history is not None and history.get("last_activity") == current_activity:
            elapsed = time.time() - float(history.get("last_time", 0.0))
            if elapsed < self.cache_ttl:
                return False, "planner / replyer 共享冷却中"
        return self.should_inject(
            session_id, UserIntent.CASUAL_CHAT, current_activity, 0.5, sequence,
        )

    def record_injection(
        self, session_id: str, activity: str, content: str, intent: UserIntent, sequence: int,
    ) -> None:
        """记录本次注入，后续节流与去重都以此为准。"""
        self.inject_history[session_id] = {
            "last_time": time.time(),
            "last_activity": activity,
            "last_content": content,
            "last_intent": intent.value,
            "last_seq": sequence,
            "count": int(self.inject_history.get(session_id, {}).get("count", 0)) + 1,
        }

    def cleanup_expired_cache(self) -> None:
        """删除超过两倍 TTL 的会话记录，避免内存无界增长。"""
        now = time.time()
        for session_id, history in list(self.inject_history.items()):
            if now - float(history.get("last_time", 0.0)) > self.cache_ttl * 2:
                del self.inject_history[session_id]
        for session_id, touched_at in list(self._message_time.items()):
            if now - touched_at > self.cache_ttl * 2:
                del self._message_time[session_id]
                self._message_seq.pop(session_id, None)
