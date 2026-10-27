"""私聊目标解析：只使用宿主已存在的真实私聊流，不自行拼 session_id。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin


@dataclass(frozen=True)
class PrivateContext:
    """一个可发送的私聊目标。"""

    session_id: str
    user_id: str
    platform: str
    name: str


async def resolve_private_context(plugin: "PainterMaimaiPlugin", user_id: str) -> Optional[PrivateContext]:
    """按配置平台查询私聊流；无流或字段不符时返回 ``None``/抛错。"""
    platform = plugin.config.group_image.platform or "qq"
    stream = await plugin.ctx.chat.get_stream_by_user_id(user_id, platform=platform)
    if stream is None:
        return None
    if not isinstance(stream, dict):
        raise ValueError("私聊流查询返回值必须为字典")
    session_id = str(stream.get("session_id") or "")
    if not session_id:
        raise ValueError("私聊流缺少 session_id")
    if str(stream.get("user_id") or "") != user_id:
        raise ValueError("私聊流 user_id 与配置名单不一致")
    if stream.get("chat_type") != "private":
        raise ValueError("私聊流 chat_type 不是 private")
    name = str(
        stream.get("user_nickname") or stream.get("user_cardname") or user_id
    ).strip()
    return PrivateContext(session_id=session_id, user_id=user_id, platform=platform, name=name)
