"""按 SDK 发送回执中的平台消息 ID 调度撤回。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

import asyncio
import logging
import re

if TYPE_CHECKING:
    from maibot_sdk import PluginContext


logger = logging.getLogger("plugin.painter_maimai_daily.recall")


@dataclass(frozen=True)
class RecallTarget:
    api_name: str
    args: Dict[str, Any]


async def _resolve_recall_target(
    ctx: "PluginContext", message_id: str, log_prefix: str,
) -> Optional[RecallTarget]:
    """仅使用现有适配器公开 API；官方消息不交给 OneBot 撤回接口。"""
    # QQ 官方回执使用不透明字符串；现有官方适配器只开放头像 API，未开放撤回 API。
    # OneBot 的消息 ID 是非零整数（兼容负数），不得把官方消息 ID 交给它。
    if re.fullmatch(r"-?[0-9]+", message_id) is None or int(message_id) == 0:
        logger.warning("%s 当前适配器未提供可用的自动撤回接口，本次跳过，message_id=%s；QQ 官方消息仍可正常发送", log_prefix, message_id)
        return None

    apis = await ctx.api.list()
    if not isinstance(apis, list):
        raise RuntimeError("宿主未返回可用的适配器 API 列表")

    def find_api(name: str) -> str:
        matches = [
            entry for entry in apis
            if isinstance(entry, dict) and entry.get("name") == name
            and entry.get("version") == "1" and entry.get("enabled") is True
        ]
        if len(matches) > 1:
            raise RuntimeError(f"撤回 API {name} 有多个提供方，无法确认发送账号")
        if not matches:
            return ""
        entry = matches[0]
        return str(entry.get("full_name") or f"{entry['plugin_id']}.{name}")

    # 两个前缀在当前统一 QQ 适配器中是同一处理器的别名，不切换客户端类型。
    for name in ("adapter.snowluma.message.delete_msg", "adapter.napcat.message.delete_msg"):
        api_name = find_api(name)
        if api_name:
            return RecallTarget(api_name, {"message_id": message_id})
    logger.warning("%s 未找到本次发送消息可用的撤回接口，本次跳过，message_id=%s", log_prefix, message_id)
    return None


async def schedule_auto_recall(
    ctx: "PluginContext", chat_id: str, message_id: str,
    delay_seconds: float, log_prefix: str,
) -> Optional[asyncio.Task]:
    """只撤回本次发送返回的真实消息 ID；返回任务，由插件负责卸载时取消。"""
    if delay_seconds <= 0 or not chat_id or not message_id:
        return None
    try:
        target = await _resolve_recall_target(ctx, message_id, log_prefix)
    except Exception:
        logger.exception("%s 确认撤回接口失败，message_id=%s", log_prefix, message_id)
        return None
    if target is None:
        return None

    async def recall() -> None:
        await asyncio.sleep(delay_seconds)
        try:
            result = await ctx.api.call(
                target.api_name, version="1", **target.args,
            )
            if (
                isinstance(result, dict) and result.get("status") == "ok"
                and result.get("retcode", 0) == 0 and result.get("success") is not False
            ):
                logger.info("%s 图片已撤回，message_id=%s", log_prefix, message_id)
            else:
                logger.warning("%s 撤回未成功，message_id=%s，回执=%r", log_prefix, message_id, result)
        except Exception:
            logger.exception("%s 撤回 API 调用异常，message_id=%s", log_prefix, message_id)

    return asyncio.create_task(recall(), name=f"painter-maimai:recall:{message_id}")
