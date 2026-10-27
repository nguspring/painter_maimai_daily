"""按会话保存最近几轮对话，用于话题延续与回复阶段复用注入内容。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

import time


@dataclass
class ConversationTurn:
    """一轮对话的注入状态，不保存完整聊天记录以外的隐私。"""

    user_message: str
    timestamp: float
    intent: Optional[str]
    injected: bool
    activity: Optional[str]


class ConversationContextCache:
    """固定窗口 + TTL 的会话上下文；只做内存操作。"""

    def __init__(self, max_turns: int = 3, ttl_seconds: int = 600) -> None:
        """保存窗口大小与过期时间。"""
        if max_turns <= 0 or ttl_seconds <= 0:
            raise ValueError("上下文窗口和 TTL 必须为正数")
        self.max_turns = max_turns
        self.ttl = ttl_seconds
        self.user_contexts: Dict[str, Deque[ConversationTurn]] = {}
        self._last_cleanup = time.time()

    def add_turn(
        self,
        session_id: str,
        user_message: str,
        *,
        intent: Optional[str] = None,
        injected: bool = False,
        activity: Optional[str] = None,
    ) -> None:
        """追加一轮；超过窗口自动淘汰最旧记录。"""
        queue = self.user_contexts.setdefault(session_id, deque(maxlen=self.max_turns))
        queue.append(
            ConversationTurn(
                user_message=user_message[:500],
                timestamp=time.time(),
                intent=intent,
                injected=injected,
                activity=activity,
            )
        )
        if time.time() - self._last_cleanup > 300:
            self.cleanup_expired()

    def get_recent_turns(self, session_id: str, count: Optional[int] = None) -> list[ConversationTurn]:
        """返回未过期的最近轮次，从旧到新。"""
        turns = list(self.user_contexts.get(session_id, ()))
        now = time.time()
        turns = [turn for turn in turns if now - turn.timestamp < self.ttl]
        return turns[-count:] if count else turns

    def is_schedule_topic_ongoing(self, session_id: str) -> bool:
        """最近两轮内有注入且最后一轮在 60 秒内，视为话题仍在延续。"""
        recent = self.get_recent_turns(session_id, 2)
        if not recent or time.time() - recent[-1].timestamp > 60:
            return False
        return any(turn.injected for turn in recent)

    def get_last_activity(self, session_id: str) -> Optional[str]:
        """返回上一轮注入时的活动描述，用于检测活动切换。"""
        recent = self.get_recent_turns(session_id, 1)
        return recent[0].activity if recent and recent[0].activity else None

    def should_continue_inject(self, session_id: str, current_activity: Optional[str]) -> tuple[bool, Optional[str]]:
        """连续追问或活动切换时继续注入，其余情况交给常规节流。"""
        if self.is_schedule_topic_ongoing(session_id):
            return True, "连续对话：用户在追问日程相关内容"
        last_activity = self.get_last_activity(session_id)
        if last_activity and current_activity and last_activity != current_activity:
            return True, f"活动变化：{last_activity} → {current_activity}"
        return False, None

    def cleanup_expired(self) -> None:
        """移除全部过期的会话与轮次，防止长期运行内存增长。"""
        now = time.time()
        for session_id, turns in list(self.user_contexts.items()):
            valid = deque(
                (turn for turn in turns if now - turn.timestamp < self.ttl),
                maxlen=self.max_turns,
            )
            if valid:
                self.user_contexts[session_id] = valid
            else:
                del self.user_contexts[session_id]
        self._last_cleanup = now
