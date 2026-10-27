"""模板兜底日程：LLM 不可用时仍能保证“活人感”。

模板保持旧插件行为：工作日和周末两套；``rest_day`` 只作为可选手动模式，
不参与自动判断（D14）。
"""

from __future__ import annotations

from datetime import date as Date

from .models import ScheduleItem

TemplateRow = tuple[int, int, int, int, str, str, str]

_WEEKDAY_TEMPLATE: list[TemplateRow] = [
    (7, 0, 7, 30, "relaxing", "赖床五分钟，手机刷个没完", "sleepy"),
    (7, 30, 8, 30, "self_care", "洗漱换衣服，顺便发呆", "neutral"),
    (8, 30, 9, 0, "eating", "随便塞了点早饭，边吃边看手机", "neutral"),
    (9, 0, 12, 0, "working", "坐在电脑前假装很忙实则摸鱼", "focused"),
    (12, 0, 13, 0, "eating", "午饭时间，今天不知道吃什么好", "neutral"),
    (13, 0, 14, 0, "relaxing", "饭后犯困，眯了一会儿", "sleepy"),
    (14, 0, 18, 0, "working", "下午继续对着屏幕发呆、写代码", "focused"),
    (18, 0, 19, 0, "eating", "晚饭，终于能离开椅子了", "happy"),
    (19, 0, 21, 0, "hobby", "刷剧刷短视频，彻底放松", "happy"),
    (21, 0, 22, 30, "relaxing", "随意逛逛、玩手机、发呆", "neutral"),
    (22, 30, 0, 0, "sleeping", "洗完澡躺下，刷着手机慢慢睡着", "calm"),
]

_WEEKEND_TEMPLATE: list[TemplateRow] = [
    (9, 0, 9, 30, "sleeping", "睡到自然醒，赖床再刷会儿手机", "sleepy"),
    (9, 30, 10, 30, "self_care", "慢慢洗漱，不用急", "calm"),
    (10, 30, 12, 0, "hobby", "追了几集番，或者玩会儿游戏", "happy"),
    (12, 0, 13, 0, "eating", "中午随便点个外卖", "neutral"),
    (13, 0, 15, 0, "relaxing", "午休或者继续刷剧", "sleepy"),
    (15, 0, 17, 0, "socializing", "跟朋友聊天或出门溜达", "happy"),
    (17, 0, 18, 30, "hobby", "逛逛网上的帖子，偷看各种有意思的东西", "happy"),
    (18, 30, 19, 30, "eating", "晚饭，可能自己做或者点外卖", "neutral"),
    (19, 30, 22, 0, "relaxing", "窝在沙发刷手机，偶尔发呆", "calm"),
    (22, 0, 0, 0, "sleeping", "困了，洗澡睡觉", "calm"),
]


def _build_items(template: list[TemplateRow], schedule_date: str) -> list[ScheduleItem]:
    """把紧凑的模板元组展开成日程项；结束 0 点保留为跨午夜写法。"""
    items: list[ScheduleItem] = []
    for start_h, start_m, end_h, end_m, activity_type, description, mood in template:
        items.append(
            ScheduleItem(
                schedule_date=schedule_date,
                start_min=start_h * 60 + start_m,
                end_min=end_h * 60 + end_m,
                activity_type=activity_type,
                description=description,
                mood=mood,
                source="template",
            )
        )
    return items


def get_template_schedule(schedule_date: str, template_mode: str = "auto") -> list[ScheduleItem]:
    """按模式返回模板；``rest_day`` 固定使用周末模板。"""
    target = Date.fromisoformat(schedule_date)
    if template_mode == "rest_day" or target.weekday() >= 5:
        return _build_items(_WEEKEND_TEMPLATE, schedule_date)
    return _build_items(_WEEKDAY_TEMPLATE, schedule_date)
