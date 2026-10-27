"""日程适配层：只通过 MaiTrace 的公开 SDK API 读取活动。

ActivityInfo 是绘卷内部的稳定结构；不读其他插件的数据库，不计算聊天流 ID。
外部生图调用应显式传入活动，只有主动任务和聊天自拍入口会查询此接口。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import hashlib
import logging
import math
import re
import time

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext

    from ...plugin import PainterMaimaiPlugin

logger = logging.getLogger("plugin.painter_maimai_daily.schedule")
_PLANNING_API_NAME = "Rabbit-Jia-Er.MaiTrace.get_current_activity"
_TIME_WINDOW_PATTERN = re.compile(r"(\d{1,2}):(\d{2})\s*[-~～至]\s*(\d{1,2}):(\d{2})")
_BREAK_PATTERN = re.compile(
    r"休息|休憩|课间|下班|饭后|餐后|跑完|做完|刚完成|完成后|结束后|"
    r"\b(?:on|taking|during)\s+(?:a\s+)?break\b|\b(?:finished|completed)\b|"
    r"\bafter\s+(?:work|class|studying|cooking|exercise|running|lunch|dinner)\b|休憩|終わった|終えた",
    re.IGNORECASE,
)
_DRIVING_PATTERN = re.compile(r"开车|驾驶|骑车|骑行|\b(?:driving|cycling|riding a bike)\b|運転|自転車", re.IGNORECASE)
_DRIVING_FINISHED_PATTERN = re.compile(
    r"(?:开车|驾驶|骑车|骑行)(?:结束后|后)(?:.*?)(?:休息|休憩)|结束驾驶|开完车|"
    r"\b(?:finished|after)\s+(?:driving|cycling)\b|運転を終えた|サイクリング後の休憩", re.IGNORECASE,
)
_FOCUS_PATTERN = re.compile(r"开会|会议|考试|专注|赶工|赶稿|写代码|\b(?:meeting|exam|focused work|coding)\b|会議|試験", re.IGNORECASE)


class ActivityType(str, Enum):
    """配置、场景规则和主动任务共用的活动类型。"""

    SLEEPING = "sleeping"
    WAKING_UP = "waking_up"
    EATING = "eating"
    WORKING = "working"
    STUDYING = "studying"
    EXERCISING = "exercising"
    RELAXING = "relaxing"
    SOCIALIZING = "socializing"
    COMMUTING = "commuting"
    HOBBY = "hobby"
    SELF_CARE = "self_care"
    COOKING = "cooking"
    SHOPPING = "shopping"
    GAMING = "gaming"
    BATHING = "bathing"
    OTHER = "other"


@dataclass
class ActivityInfo:
    """活动信息；额外服装和地点字段允许 MaiTrace 不提供。"""

    activity_type: ActivityType
    description: str
    mood: str = "neutral"
    time_point: str = ""
    outfit: str = ""
    location: str = ""
    time_window: str = ""
    as_of: str = ""
    timezone: str = ""
    next_activities: List[Dict[str, str]] = field(default_factory=list)
    scene: str = ""
    sleep_phase: str = ""
    energy: Optional[float] = None
    health_note: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "type": self.activity_type.value, "description": self.description,
            "outfit": self.outfit, "location": self.location, "time_window": self.time_window,
            "as_of": self.as_of, "timezone": self.timezone, "next_activities": self.next_activities,
            "scene": self.scene, "sleep_phase": self.sleep_phase, "energy": self.energy,
            "mood": self.mood, "health_note": self.health_note,
            "current_time": self._moment().isoformat(timespec="seconds"),
            "remaining_seconds": self.remaining_seconds(),
        }

    def _moment(self, now: Optional[datetime] = None) -> datetime:
        zone = ZoneInfo(self.timezone) if self.timezone else datetime.now().astimezone().tzinfo
        if now is None:
            return datetime.now(zone)
        return now.replace(tzinfo=zone) if now.tzinfo is None else now.astimezone(zone)

    def _time_bounds(self) -> Optional[Tuple[int, int]]:
        if not self.time_window:
            return None
        match = _TIME_WINDOW_PATTERN.fullmatch(self.time_window)
        if match is None:
            raise ValueError("日程时间窗格式无效")
        start = int(match[1]) * 60 + int(match[2])
        end = int(match[3]) * 60 + int(match[4])
        if not 0 <= start < 1440 or not 0 <= end <= 1440 or int(match[2]) >= 60 or int(match[4]) >= 60 or start == end:
            raise ValueError("日程时间窗范围无效")
        return start, end

    def remaining_seconds(self, *, now: Optional[datetime] = None) -> Optional[float]:
        """跨午夜时间窗按实际结束日计算；没有时间窗的旧快照返回 None。"""
        bounds = self._time_bounds()
        if bounds is None:
            return None
        moment = self._moment(now)
        start, end = bounds
        current = moment.hour * 60 + moment.minute
        active = start <= current < end if end > start else current >= start or current < end
        if not active:
            return 0.0
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        ends_at = midnight + timedelta(minutes=end)
        if end < start and current >= start:
            ends_at += timedelta(days=1)
        return (ends_at - moment).total_seconds()

    def publication_key(self) -> str:
        """同一活动区间只分享一次；跨午夜沿用活动开始日期。"""
        moment = self._moment()
        day = moment.date()
        match = _TIME_WINDOW_PATTERN.fullmatch(self.time_window)
        if match:
            start = int(match[1]) * 60 + int(match[2])
            end = int(match[3]) * 60 + int(match[4])
            if end <= start and moment.hour * 60 + moment.minute < end:
                day -= timedelta(days=1)
        text = f"{day}:{self.time_window or self.time_point}:{self.activity_type.value}:{self.description}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def publication_block_reason(
        self, *, min_energy: float = 30, min_remaining_seconds: float = 0,
        now: Optional[datetime] = None,
    ) -> str:
        """所有主动图片共用硬规则；通过后仍需 LLM 判断自然分享动机。"""
        if not self.description.strip():
            return "日程活动为空"
        if self.activity_type in {ActivityType.SLEEPING, ActivityType.BATHING}:
            return "正在睡眠或洗澡"
        if self.sleep_phase in {"falling_asleep", "light_sleep", "deep_sleep", "woken"}:
            return "处于睡眠阶段或刚醒未清醒"
        if self.energy is not None:
            if not math.isfinite(self.energy) or not 0 <= self.energy <= 100:
                return "生活精力值无效"
            if self.energy < min_energy:
                return "当前精力不足"
        text = f"{self.description} {self.scene}"
        if _DRIVING_PATTERN.search(text) and not _DRIVING_FINISHED_PATTERN.search(text):
            return "正在驾驶或骑行"
        taking_break = bool(_BREAK_PATTERN.search(text))
        if not taking_break and (self.activity_type in {
            ActivityType.WORKING, ActivityType.STUDYING, ActivityType.EXERCISING,
            ActivityType.COOKING, ActivityType.GAMING, ActivityType.SELF_CARE, ActivityType.EATING,
        } or _FOCUS_PATTERN.search(text)):
            return "当前活动需要专注，尚未进入休息时间"
        if self.activity_type == ActivityType.WAKING_UP and not re.search(r"已清醒|清醒后|fully awake|目が覚めて", text, re.IGNORECASE):
            return "刚起床，尚未确认清醒"
        if not self.as_of and not self.time_window:
            return "日程缺少快照时间和有效时间窗"
        try:
            moment = self._moment(now)
            if self.as_of:
                snapshot_time = datetime.fromisoformat(self.as_of)
                if snapshot_time.tzinfo is None:
                    snapshot_time = snapshot_time.replace(tzinfo=moment.tzinfo)
                if not -60 <= moment.timestamp() - snapshot_time.timestamp() <= 15 * 60:
                    return "日程快照已过期或来自未来"
            remaining = self.remaining_seconds(now=moment)
            if remaining is not None and remaining <= min_remaining_seconds:
                return "当前不在活动时间窗内或剩余时间不足"
        except (ValueError, ZoneInfoNotFoundError) as exc:
            return f"日程时间无效: {exc}"
        return ""

    def current_time(self) -> str:
        """供提示词使用的本地时间，避免场景生成猜测季节和时段。"""
        return self._moment().isoformat(timespec="seconds")

    def can_publish(self) -> bool:
        """兼容旧调用；主动任务可获取具体拒绝原因并传入统一精力阈值。"""
        return not self.publication_block_reason()


# MaiTrace 保留 goal_type 兼容字段；明确分类优先于描述中的关键词。
_TYPE_KEYWORDS: Dict[str, ActivityType] = {
    "waking": ActivityType.WAKING_UP, "起床": ActivityType.WAKING_UP,
    "睡醒": ActivityType.WAKING_UP, "醒来": ActivityType.WAKING_UP,
    "sleep": ActivityType.SLEEPING, "睡": ActivityType.SLEEPING,
    "bath": ActivityType.BATHING, "洗澡": ActivityType.BATHING,
    "cook": ActivityType.COOKING, "烹饪": ActivityType.COOKING, "做饭": ActivityType.COOKING,
    "下厨": ActivityType.COOKING, "做早餐": ActivityType.COOKING,
    "做午饭": ActivityType.COOKING, "做晚饭": ActivityType.COOKING,
    "work": ActivityType.WORKING, "工作": ActivityType.WORKING, "上班": ActivityType.WORKING,
    "study": ActivityType.STUDYING, "学习": ActivityType.STUDYING, "阅读": ActivityType.STUDYING,
    "learn_topic": ActivityType.STUDYING, "读书": ActivityType.STUDYING,
    "exercise": ActivityType.EXERCISING, "运动": ActivityType.EXERCISING,
    "跑步": ActivityType.EXERCISING, "健身": ActivityType.EXERCISING,
    "锻炼": ActivityType.EXERCISING, "瑜伽": ActivityType.EXERCISING,
    "eat": ActivityType.EATING, "meal": ActivityType.EATING, "吃": ActivityType.EATING, "餐": ActivityType.EATING,
    "shop": ActivityType.SHOPPING, "购物": ActivityType.SHOPPING,
    "game": ActivityType.GAMING, "gaming": ActivityType.GAMING, "游戏": ActivityType.GAMING,
    "rest": ActivityType.RELAXING, "relax": ActivityType.RELAXING, "休息": ActivityType.RELAXING,
    "看电影": ActivityType.RELAXING, "追剧": ActivityType.RELAXING, "听音乐": ActivityType.RELAXING,
    "social": ActivityType.SOCIALIZING, "社交": ActivityType.SOCIALIZING,
    "commut": ActivityType.COMMUTING, "通勤": ActivityType.COMMUTING,
    "self_care": ActivityType.SELF_CARE, "梳妆": ActivityType.SELF_CARE, "护肤": ActivityType.SELF_CARE,
    "洗脸": ActivityType.SELF_CARE, "刷牙": ActivityType.SELF_CARE,
    "hobby": ActivityType.HOBBY,
}


def classify_activity(*hints: str) -> ActivityType:
    """按结构化类型、名称、描述依次识别；未分类的活动明确归为 other。"""
    known_types = {item.value for item in ActivityType}
    # rest/meal 等粗分类不能掩盖睡觉、做饭等描述中的具体活动。
    coarse_hints = {"rest", "relax", "meal", "eat", "entertainment", "daily_routine", "custom"}
    ordered = [hint for hint in hints if hint.strip().lower() not in coarse_hints]
    ordered.extend(hint for hint in hints if hint.strip().lower() in coarse_hints)
    for hint in ordered:
        normalized = hint.strip().lower()
        if normalized in known_types:
            return ActivityType(normalized)
        for keyword, activity_type in _TYPE_KEYWORDS.items():
            if keyword in normalized:
                return activity_type
    return ActivityType.OTHER


class ScheduleProvider:
    async def get_current_activity(self) -> Optional[ActivityInfo]:
        raise NotImplementedError


class ApiScheduleProvider(ScheduleProvider):
    """通过 Host 转发读取 MaiTrace 日程，缺失或服务错误时记录原因并返回空活动。"""

    def __init__(self, ctx: "PluginContext", chat_id: str = "global", timeout_seconds: float = 20, *, include_life: bool = False):
        self._ctx = ctx
        self._chat_id = chat_id
        self._timeout_seconds = timeout_seconds
        self._include_life = include_life

    async def get_current_activity(self) -> Optional[ActivityInfo]:
        try:
            result = await self._ctx.call_capability(
                "api.call", timeout_ms=int(self._timeout_seconds * 1000),
                api_name=_PLANNING_API_NAME, version="1", args={"chat_id": self._chat_id},
            )
            if not isinstance(result, dict):
                raise ValueError("MaiTrace 日程 API 返回值必须为字典")
            if result.get("success") is False or result.get("error"):
                raise RuntimeError(str(result.get("error") or "MaiTrace 日程 API 调用失败"))
            activity = self._snapshot_to_activity(result)
            if activity is not None and self._include_life:
                await self._enrich_life(activity)
            return activity
        except Exception:
            logger.exception("读取 MaiTrace 当前日程失败，本次不使用日程")
            return None

    async def _enrich_life(self, activity: ActivityInfo) -> None:
        """当前 MaiTrace 公开生活接口可选；禁用生活模块时仍使用有效日程。"""
        try:
            state = await self._ctx.call_capability(
                "api.call", timeout_ms=10000,
                api_name="Rabbit-Jia-Er.MaiTrace.get_life_state", version="1", args={},
            )
            if not isinstance(state, dict):
                raise ValueError("MaiTrace 生活状态必须为字典")
            if state.get("success") is False or state.get("error"):
                raise RuntimeError(str(state.get("error") or "MaiTrace 生活 API 调用失败"))
            if not state.get("enabled"):
                return
            # 全局睡眠/精力也必须新鲜；旧生活快照不能覆盖当前日程。
            updated = state.get("last_updated_at", 0)
            if type(updated) not in (int, float) or not -60 <= time.time() - updated <= 15 * 60:
                return
            activity.sleep_phase = str(state.get("sleep_phase", ""))
            activity.health_note = str(state.get("health_note", "")).strip()
            energy = state.get("energy")
            if type(energy) in (int, float) and math.isfinite(energy):
                activity.energy = float(energy)
            mood = state.get("mood_valence")
            if type(mood) in (int, float) and math.isfinite(mood):
                activity.mood = f"valence={mood:g}"
            # 具体场景和地点只能来自同一活动，不能把之前的厨房/卧室带进本轮。
            if str(state.get("current_activity", "")).strip() != activity.description:
                return
            activity.location = activity.location or str(state.get("current_location", "")).strip()
            scene = str(state.get("scene", "")).strip()
            if scene:
                activity.scene = scene
        except Exception:
            logger.exception("读取 MaiTrace 生活状态失败，本轮仅使用日程")

    @staticmethod
    def _snapshot_to_activity(snapshot: Dict[str, Any]) -> Optional[ActivityInfo]:
        if not snapshot.get("has_activity"):
            return None
        activity = snapshot.get("activity")
        if not isinstance(activity, dict):
            raise ValueError("MaiTrace 日程标记 has_activity，但未提供 activity 字典")
        parameters = activity.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError("MaiTrace activity.parameters 必须为字典")
        name = str(activity.get("name", "")).strip()
        description = str(activity.get("description", "")).strip() or name
        if not description:
            raise ValueError("MaiTrace 当前活动缺少名称和描述")
        activity_type = classify_activity(
            str(activity.get("activity_type", "")), str(activity.get("goal_type", "")), name, description,
        )
        time_window = str(activity.get("time_window", ""))
        upcoming = snapshot.get("next_activities", [])
        if not isinstance(upcoming, list):
            raise ValueError("MaiTrace next_activities 必须为列表")
        return ActivityInfo(
            activity_type=activity_type, description=description,
            time_point=time_window.split("-", 1)[0].strip(),
            # MaiTrace 6.x 将地点、服装和场景放在 parameters；保留旧顶层字段的优先级。
            outfit=str(activity.get("outfit", "")).strip() or str(parameters.get("outfit", "")).strip(),
            location=str(activity.get("location", "")).strip() or str(parameters.get("location", "")).strip(),
            scene=str(activity.get("scene", "")).strip() or str(parameters.get("scene_hint", "")).strip(),
            time_window=time_window.strip(), as_of=str(snapshot.get("as_of", "")).strip(),
            timezone=str(snapshot.get("timezone", "")).strip(),
            next_activities=[{"time": str(item.get("time", "")), "name": str(item.get("name", ""))} for item in upcoming[:3] if isinstance(item, dict)],
        )


def get_schedule_provider(
    plugin: "PainterMaimaiPlugin", chat_id: str = "global", timeout_seconds: float = 20, *, include_life: bool = False,
) -> ScheduleProvider:
    """按配置选择日程来源；关闭时返回空实现，不回退到 MaiTrace（D10）。"""
    schedule = plugin.config.schedule
    if not schedule.enabled:
        from ..schedule.provider import NullScheduleProvider
        return NullScheduleProvider()
    if schedule.provider == "builtin":
        manager = plugin.schedule_manager
        if manager is None:
            raise RuntimeError("内置日程已启用，但日程管理器尚未初始化")
        from ..schedule.provider import BuiltinScheduleProvider
        return BuiltinScheduleProvider(manager)
    return ApiScheduleProvider(plugin.ctx, chat_id, timeout_seconds, include_life=include_life)
