"""主动任务共用的群名单、真实聊天流与近期文字语境。"""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

import time

from ..config import resolve_img2img_model, resolve_txt2img_model
from ..utils.time_utils import is_in_time_range
from .schedule_provider import ActivityInfo, ScheduleProvider

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin


@dataclass
class GroupContext:
    session_id: str
    group_id: str
    platform: str
    name: str
    messages: List[Dict[str, str]]


def recent_context(messages: Any, *, session_id: str, earliest: float) -> List[Dict[str, str]]:
    """只收集当前真实聊天流内、时限内且有稳定消息 ID 的文字消息。"""
    if not isinstance(messages, list):
        raise ValueError("message.get_recent 返回值必须为消息列表")
    collected: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for item in messages:
        if not isinstance(item, dict):
            continue
        if str(item.get("session_id", item.get("chat_id", ""))) != session_id:
            continue
        raw_id = item.get("message_id")
        if not isinstance(raw_id, str):
            continue
        message_id = raw_id.strip()
        raw_timestamp = item.get("timestamp", item.get("time"))
        try:
            timestamp = float(raw_timestamp)
        except (TypeError, ValueError):
            if not isinstance(raw_timestamp, str):
                continue
            try:
                timestamp = datetime.fromisoformat(raw_timestamp).timestamp()
            except ValueError:
                continue
        text = item.get("processed_plain_text", item.get("content", ""))
        if not message_id or message_id in seen or not isfinite(timestamp) or timestamp < earliest:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        seen.add(message_id)
        collected.append({"message_id": message_id, "text": text.strip()[:500]})
    return collected


def group_allowed(plugin: "PainterMaimaiPlugin", group_id: str, session_id: str = "") -> bool:
    cfg = plugin.config.group_image
    quiet = cfg.quiet_hours_start != cfg.quiet_hours_end and is_in_time_range(cfg.quiet_hours_start, cfg.quiet_hours_end)
    return (
        plugin.config.plugin.enabled and group_id in cfg.group_ids and not quiet
        and (not session_id or plugin.runtime_state.is_plugin_enabled(session_id, plugin.config.plugin.enabled))
    )


async def read_group_context(plugin: "PainterMaimaiPlugin", group_id: str) -> Optional[GroupContext]:
    if not group_allowed(plugin, group_id):
        return None
    cfg = plugin.config.group_image
    # SDK 查找已存在的真实群流；无流时不造 ID，也不自行创建会话。
    stream = await plugin.ctx.chat.get_stream_by_group_id(group_id, platform=cfg.platform)
    if stream is None:
        return None
    if not isinstance(stream, dict) or not stream.get("session_id"):
        raise ValueError("群聊天流查询没有返回真实 session_id")
    if str(stream.get("group_id", "")) != group_id or stream.get("platform") != cfg.platform:
        raise ValueError("群聊天流平台或目标与允许名单不一致")
    session_id = str(stream["session_id"])
    if not group_allowed(plugin, group_id, session_id):
        return None
    target = f"group:{session_id}"
    if not plugin.proactive_state.available(target, cfg.cooldown_minutes * 60):
        return None
    messages = await plugin.ctx.call_capability(
        "message.get_recent", timeout_ms=20000, chat_id=session_id,
        limit=cfg.context_messages, hours=cfg.context_minutes / 60, filter_mai=True,
    )
    return GroupContext(
        session_id, group_id, cfg.platform, str(stream.get("group_name") or group_id),
        recent_context(messages, session_id=session_id, earliest=time.time() - cfg.context_minutes * 60),
    )


def proactive_model(plugin: "PainterMaimaiPlugin", *, is_selfie: bool, automatic: bool = False) -> str:
    """使用与生图 API 相同的回退顺序，在生成前检查群内模型开关。"""
    configured = plugin.config.selfie.selfie_model if automatic else plugin.config.group_image.model_id
    if configured.strip():
        return configured.strip()
    if is_selfie and plugin.config.selfie.llm_selfie_model.strip():
        return plugin.config.selfie.llm_selfie_model.strip()
    if is_selfie and plugin.config.selfie.reference_image_path.strip():
        return resolve_img2img_model(plugin)
    return resolve_txt2img_model(plugin)


def activity_arguments(activity: Optional[ActivityInfo]) -> Dict[str, str]:
    if activity is None:
        return {"activity_type": "", "activity_description": ""}
    return {
        "activity_type": activity.activity_type.value, "activity_description": activity.description,
        "activity_location": activity.location, "activity_time_window": activity.time_window,
        "activity_as_of": activity.as_of, "activity_timezone": activity.timezone, "activity_scene": activity.scene,
    }


def activity_block_reason(
    plugin: "PainterMaimaiPlugin", activity: Optional[ActivityInfo], *, automatic: bool = False,
    group: bool = False, before_generation: bool = False,
) -> str:
    """所有主动入口使用同一日程/生活门槛，渠道禁用配置可叠加。"""
    if activity is None:
        return "当前没有有效日程"
    if automatic and activity.activity_type in plugin.config.selfie.disabled_activity_types:
        return "当前活动禁止主动自拍"
    if group and activity.activity_type in plugin.config.group_image.disabled_activity_types:
        return "当前活动禁止主动群图"
    return activity.publication_block_reason(
        min_energy=plugin.config.basic.proactive_min_energy,
        min_remaining_seconds=plugin.config.basic.generation_timeout_seconds if before_generation else 0,
    )


async def recheck_activity(
    plugin: "PainterMaimaiPlugin", original: ActivityInfo, provider: ScheduleProvider,
    *, automatic: bool = False, group: bool = False,
) -> str:
    """临发送重新读公开日程，生成期间活动/地点/服装变化时丢弃本轮结果。"""
    current = await provider.get_current_activity()
    reason = activity_block_reason(plugin, current, automatic=automatic, group=group)
    if reason:
        return reason
    assert current is not None
    if current.publication_key() != original.publication_key():
        return "生成期间日程活动已经切换"
    if current.location != original.location or current.outfit != original.outfit:
        return "生成期间活动地点或服装已经变化"
    if current.scene != original.scene:
        return "生成期间生活场景已经变化"
    return ""
