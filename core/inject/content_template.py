"""渲染注入文本：当前活动、剩余时间、未来安排和对话引导。"""

from __future__ import annotations

from typing import Optional

from ..selfie.schedule_provider import ActivityInfo
from ..utils.untrusted import wrap_untrusted
from .intent_classifier import UserIntent
from .state_analyzer import ActivityState

INJECT_MARKER = "【绘卷当前日程】"


def render_content(
    activity: ActivityInfo,
    *,
    max_future: int,
    intent: UserIntent = UserIntent.CASUAL_CHAT,
    state: Optional[ActivityState] = None,
    replyer: bool = False,
) -> str:
    """把活动事实包装成带标记的背景块；无有效描述时返回空串。"""
    if not activity.description.strip():
        return ""

    lines = [f"当前活动：{activity.description}"]
    if activity.time_window:
        lines.append(f"时段：{activity.time_window}")
    if state is not None:
        if state.remaining_minutes is not None:
            lines.append(f"剩余：约 {state.remaining_minutes} 分钟")
        lines.append(f"状态参考：{state.phase}，已经进行了约 {state.elapsed_minutes} 分钟")
    for entry in activity.next_activities[: max(0, max_future)]:
        lines.append(f"接下来 {entry.get('time', '')}：{entry.get('name', '')}")

    if replyer:
        guidance = "根据当前活动自然调整语气，忙时简短、放松时可多聊；只在相关时带过，不逐项播报日程。"
    elif intent == UserIntent.QUERY_CURRENT:
        guidance = "用户正在询问当前状态，请按真实活动自然回答，不逐项播报日程表。"
    elif intent == UserIntent.QUERY_FUTURE:
        guidance = "用户正在询问后续安排，请自然介绍所问时段的活动。"
    else:
        guidance = "背景仅供参考；不相关时完全忽略，不要刻意提及日程，也不要逐项播报。"

    return INJECT_MARKER + "\n" + guidance + wrap_untrusted("\n".join(lines), label="当前日程", max_chars=1500)
