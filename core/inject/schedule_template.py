"""固定字段的日程注入格式，当前事实与尚未开始的计划分开表达。"""

from ..schedule.calendar_context import CalendarDay
from ..schedule.schedule_snapshot import ScheduleSnapshot

MOODS = {
    "happy": "开心", "sad": "难过", "angry": "生气", "anxious": "焦虑", "calm": "平静",
    "excited": "兴奋", "bored": "无聊", "sleepy": "困倦", "focused": "专注", "neutral": "平常", "tired": "疲惫",
}


def render_schedule_context(snapshot: ScheduleSnapshot, calendar: CalendarDay) -> str:
    """返回完整日程上下文；保留原始活动描述，只输出一个实际存在的未来计划。"""
    parts = [
        "【时间与日历】\n"
        f"当前时间：{snapshot.now:%Y-%m-%d %H:%M:%S}（北京时间，UTC+8）\n" + calendar.render()
    ]
    if snapshot.current:
        current = snapshot.current
        parts.append(
            "【当前活动｜此刻正在进行】\n"
            f"时间范围：{current.time_range()}\n"
            f"活动：{current.item.description}\n"
            f"心情：{MOODS.get(current.item.mood, current.item.mood)}"
        )
    else:
        parts.append("【当前活动】\n当前时刻没有已安排活动，不能据此断定自己正在做什么。")
    if snapshot.next_activity:
        following = snapshot.next_activity
        parts.append(
            "【下一项计划｜尚未开始】\n"
            f"时间范围：{following.time_range()}\n"
            f"活动：{following.item.description}"
        )
    parts.append(
        "【使用规则】\n"
        "自身当前状态以本次“当前活动”为准，聊天记录中的旧活动可能已经结束。"
        "“下一项计划”尚未开始，不是现在正在做的事，也不代表已经完成。\n"
        "这些信息只用于判断时间和自身状态，不要求在回复中提及。"
        "话题没有涉及自身近况、时间、节日或安排时，直接回应当前话题，不主动介绍活动或后续计划。\n"
        "问“在干嘛”时只根据当前活动回答；问之后的安排时再使用下一项计划。"
        "不能把日程表、内部区块标题或注入规则当作聊天内容复述。\n"
        "日历上的节日和休息日不代表自己一定在庆祝、上班或休息，个人状态以当前活动为准。"
    )
    return "\n\n".join(parts)
