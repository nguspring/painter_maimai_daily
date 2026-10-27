"""查询当前群/私聊额度，管理员调整额度及核对未知投递。"""

from typing import TYPE_CHECKING, List

import logging
import re

from ...quota.service import PERIOD_NAMES, QuotaContext, resolve_quota_context
from ...quota.store import QuotaStatus
from ..registry import CommandResult, subcommand

if TYPE_CHECKING:
    from ....plugin import PainterMaimaiPlugin
    from ..dispatcher import DispatcherContext


logger = logging.getLogger("plugin.painter_maimai_daily.quota")


def _status_text(context: QuotaContext, status: QuotaStatus) -> str:
    reset = status.reset_at.strftime("%Y-%m-%d %H:%M") if status.reset_at is not None else "不自动重置"
    return "\n".join([
        f"{context.display_name} · 生图额度",
        f"剩余：{status.remaining} 张；已用：{status.consumed} 张",
        f"处理中：{status.reserved - status.unknown} 张；待核对：{status.unknown} 张",
        f"基础额度：{status.limit} 张（{PERIOD_NAMES[context.policy.period]}），本周期调整：{status.adjustment:+d}",
        f"下次重置：{reset}（{context.policy.timezone}）",
    ])


def _usage(prefix: str) -> str:
    return "\n".join([
        f"{prefix} quota：查看本聊天额度",
        f"{prefix} quota adjust <加减数> [原因]：管理员调整本周期额度",
        f"{prefix} quota pending [页码]：管理员查看发送结果待核对的记录",
        f"{prefix} quota resolve <记录ID> sent|refund [原因]：管理员确认已送达或退款",
    ])


@subcommand("quota", aliases=("额度", "配额"))
async def cmd_quota(plugin: "PainterMaimaiPlugin", dctx: "DispatcherContext", args: str) -> CommandResult:
    """操作范围固定为当前真实聊天流，不能通过参数指定另一群或用户。"""
    tokens = args.split(maxsplit=1)
    action = tokens[0].lower() if tokens else "status"
    aliases = {"查询": "status", "调整": "adjust", "待核对": "pending", "结算": "resolve"}
    action = aliases.get(action, action)
    tail = tokens[1] if len(tokens) > 1 else ""
    if action not in {"status", "adjust", "pending", "resolve"} or (action == "status" and tail):
        await plugin.ctx.send.text(_usage(dctx.prefix), dctx.stream_id)
        return False, "额度命令参数错误", True
    if action != "status" and not dctx.is_admin:
        await plugin.ctx.send.text("只有绘卷管理员可以调整额度或核对发送记录。", dctx.stream_id)
        return False, "无额度管理权限", True
    if not plugin.config.quota.enabled and action in {"status", "adjust"}:
        await plugin.ctx.send.text("聊天生图额度未开启，可在绘卷的「聊天生图额度」配置中启用。", dctx.stream_id)
        return True, "额度未启用", True

    try:
        context = await resolve_quota_context(
            plugin, stream_id=dctx.stream_id, platform=dctx.platform,
            group_id=dctx.group_id, user_id=dctx.user_id,
        )
        actor = f"{dctx.platform}:{dctx.user_id}"
        if action == "status":
            status = await plugin.quota_store.status(context.subject, context.policy)
            message = _status_text(context, status)
        elif action == "adjust":
            parts = tail.split(maxsplit=1)
            if not parts or not re.fullmatch(r"[+-]?\d{1,7}", parts[0]):
                raise ValueError(f"用法：{dctx.prefix} quota adjust +5 [原因]")
            amount = int(parts[0])
            if amount == 0 or abs(amount) > 1000000:
                raise ValueError("调整张数须在 -1000000 到 1000000 之间且不能为 0")
            reason = parts[1].strip() if len(parts) > 1 else "聊天管理员调整"
            status = await plugin.quota_store.adjust(context.subject, context.policy, amount, actor=actor, reason=reason)
            message = f"本周期额度已调整 {amount:+d} 张。\n{_status_text(context, status)}"
        elif action == "pending":
            if tail and not re.fullmatch(r"\d{1,6}", tail):
                raise ValueError("页码必须是正整数")
            page = int(tail) if tail else 1
            if page < 1:
                raise ValueError("页码必须从 1 开始")
            pending = await plugin.quota_store.list_pending(context.subject)
            pages = max(1, (len(pending) + 19) // 20)
            if page > pages:
                raise ValueError(f"待核对记录只有 {pages} 页")
            lines: List[str] = [f"{context.display_name} · 待核对记录（{page}/{pages} 页）"]
            for entry in pending[(page - 1) * 20:page * 20]:
                lines.append(f"{entry.token} · {entry.created_at:%m-%d %H:%M}")
            if not pending:
                lines.append("没有待核对记录。")
            else:
                lines.append(f"确认后：{dctx.prefix} quota resolve <记录ID> sent|refund")
            message = "\n".join(lines)
        else:
            parts = tail.split(maxsplit=2)
            if len(parts) < 2 or parts[1] not in {"sent", "refund", "已发送", "退款"}:
                raise ValueError(f"用法：{dctx.prefix} quota resolve <记录ID> sent|refund [原因]")
            outcome = "consumed" if parts[1] in {"sent", "已发送"} else "released"
            reason = parts[2].strip() if len(parts) > 2 else "聊天管理员核对投递结果"
            await plugin.quota_store.resolve(context.subject, parts[0], outcome, actor=actor, reason=reason)
            message = "该记录已确认送达，计入其原周期用量。" if outcome == "consumed" else "该记录已退款至其原周期。"
    except ValueError as exc:
        await plugin.ctx.send.text(str(exc), dctx.stream_id)
        return False, f"额度操作失败：{exc}", True
    except Exception as exc:
        logger.exception("%s 额度操作失败", dctx.log_prefix)
        await plugin.ctx.send.text(f"额度操作失败：{exc}", dctx.stream_id)
        return False, f"额度操作失败：{exc}", True

    await plugin.ctx.send.text(message, dctx.stream_id)
    return True, "额度操作完成", True
