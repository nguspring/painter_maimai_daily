"""将宿主的真实聊天流映射到额度主体，不创建或推算会话 ID。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .store import QuotaPolicy, QuotaSubject

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin
    from ..config.quota import QuotaSection


PERIOD_NAMES = {"daily": "每天", "weekly": "每周", "monthly": "每月", "once": "一次性"}


@dataclass(frozen=True)
class QuotaContext:
    subject: QuotaSubject
    policy: QuotaPolicy
    display_name: str


def select_policy(config: "QuotaSection", subject: QuotaSubject) -> QuotaPolicy:
    """显式规则优先；群额度由全群共享，用户规则只用于私聊。"""
    for rule in config.rules:
        if (rule.platform, rule.target_type, rule.target_id) == (
            subject.platform,
            subject.target_type,
            subject.target_id,
        ):
            return QuotaPolicy(rule.limit, rule.period or config.period, config.timezone)
    limit = config.group_limit if subject.target_type == "group" else config.private_limit
    return QuotaPolicy(limit, config.period, config.timezone)


async def resolve_quota_context(
    plugin: "PainterMaimaiPlugin",
    *,
    stream_id: str,
    platform: str,
    group_id: str,
    user_id: str,
) -> QuotaContext:
    """按宿主注入的 session_id 精确匹配，避免多账号环境按群号取错会话。"""
    if not stream_id or not platform or (not group_id and not user_id):
        raise ValueError("宿主未提供完整的聊天流、平台及用户/群信息，无法确认额度归属")
    streams = await plugin.ctx.call_capability("chat.get_all_streams", timeout_ms=10000, platform=platform)
    if not isinstance(streams, list):
        raise ValueError("SDK 聊天流查询返回了无效结果")
    matches = [item for item in streams if isinstance(item, dict) and item.get("session_id") == stream_id]
    if len(matches) != 1:
        raise ValueError("当前聊天流未在宿主中唯一登记，无法确认额度归属")
    stream = matches[0]
    if stream.get("platform") != platform:
        raise ValueError("宿主聊天流的平台与本次请求不一致")
    if group_id:
        if stream.get("chat_type") != "group" or str(stream.get("group_id", "")) != group_id:
            raise ValueError("宿主聊天流的群归属与本次请求不一致")
        subject = QuotaSubject(platform, "group", group_id)
        display_name = str(stream.get("group_name") or "当前群聊")
    else:
        if stream.get("chat_type") != "private" or str(stream.get("user_id", "")) != user_id:
            raise ValueError("宿主聊天流的私聊归属与本次请求不一致")
        subject = QuotaSubject(platform, "user", user_id)
        nickname = str(stream.get("user_nickname") or "")
        display_name = f"{nickname}的私聊" if nickname else "当前私聊"
    return QuotaContext(subject, select_policy(plugin.config.quota, subject), display_name)
