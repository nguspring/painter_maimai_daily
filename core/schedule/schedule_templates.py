"""
兜底模板日程。保证任何时候都有活人感的活动可用。
"""

from __future__ import annotations

import datetime

from .schedule_models import ScheduleItem

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


def _build_items(template: list[TemplateRow], date: str) -> list[ScheduleItem]:
    """将模板元组列表构造成 ScheduleItem 列表。"""
    items: list[ScheduleItem] = []
    for row in template:
        sh, sm, eh, em, activity_type, description, mood = row
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        items.append(
            ScheduleItem(
                schedule_date=date,
                start_min=start_min,
                end_min=end_min,
                activity_type=activity_type,
                description=description,
                mood=mood,
                source="template",
            )
        )
    return items


def get_template_schedule(date: str, is_off_day: bool | None = None) -> list[ScheduleItem]:
    """返回对应日期的模板；已确认休假状态优先，未知时保留原来的星期规则。"""
    target_date = datetime.date.fromisoformat(date)
    off_day = target_date.weekday() >= 5 if is_off_day is None else is_off_day
    if not off_day:
        return _build_items(_WEEKDAY_TEMPLATE, date)
    return _build_items(_WEEKEND_TEMPLATE, date)
