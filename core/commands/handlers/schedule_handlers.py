"""/dr schedule 子命令：查看当天日程，管理员可强制重新生成。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import logging

from ...schedule.models import format_time_range
from ..registry import CommandResult, subcommand

if TYPE_CHECKING:
    from ....plugin import PainterMaimaiPlugin
    from ..dispatcher import DispatcherContext

logger = logging.getLogger("plugin.painter_maimai_daily.cmd.schedule")


@subcommand("schedule", admin=False)
async def cmd_schedule(plugin: "PainterMaimaiPlugin", dctx: "DispatcherContext", args: str) -> CommandResult:
    """``/dr schedule`` 查看日程；``/dr schedule regen`` 仅管理员可用。"""
    action = args.strip().lower()
    if action == "regen":
        if not dctx.is_admin:
            await plugin.ctx.send.text("你无权使用此命令", dctx.stream_id)
            return False, "无权限", True
        return await _regen(plugin, dctx)
    if action:
        await plugin.ctx.send.text(f"格式：{dctx.prefix} schedule 或 {dctx.prefix} schedule regen", dctx.stream_id)
        return False, "参数无效", True
    return await _show(plugin, dctx)


async def _show(plugin: "PainterMaimaiPlugin", dctx: "DispatcherContext") -> CommandResult:
    """输出当前活动与当天全部日程，保持紧凑不插空行。"""
    manager = plugin.schedule_manager
    if not plugin.config.schedule.enabled or manager is None:
        await plugin.ctx.send.text("内置日程未启用，当前没有可查看的日程", dctx.stream_id)
        return False, "日程未启用", True

    snapshot = manager.snapshot
    if snapshot is None or snapshot.schedule_date != manager.now().date().isoformat():
        snapshot = await manager.refresh_snapshot()
    items = list(snapshot.items)
    if not items:
        await plugin.ctx.send.text("今天还没有日程数据", dctx.stream_id)
        return True, "日程为空", True

    lines = [f"📅 今日日程 · {snapshot.schedule_date}"]
    if snapshot.current is not None:
        window = f"（{snapshot.current.time_window}）" if snapshot.current.time_window else ""
        lines.append(f"现在：{snapshot.current.description}{window}")
    else:
        lines.append("现在：暂无活动")
    for item in items:
        lines.append(f"{format_time_range(item.start_min, item.end_min)} {item.description}")
    sources = {item.source for item in items}
    lines.append(f"来源：{'/'.join(sorted(sources))}（共 {len(items)} 条）")

    await plugin.ctx.send.text("\n".join(lines), dctx.stream_id)
    return True, "日程查询成功", True


async def _regen(plugin: "PainterMaimaiPlugin", dctx: "DispatcherContext") -> CommandResult:
    """调用 LLM 覆盖当天日程；失败保留原数据。"""
    manager = plugin.schedule_manager
    if not plugin.config.schedule.enabled or manager is None:
        await plugin.ctx.send.text("内置日程未启用，无法重新生成", dctx.stream_id)
        return False, "日程未启用", True

    await plugin.ctx.send.text("正在用 LLM 重新生成今日日程...", dctx.stream_id)
    try:
        success = await manager.regen_today_schedule_via_llm(plugin)
    except Exception as exc:
        logger.exception("重新生成日程失败")
        await plugin.ctx.send.text(f"重新生成失败：{exc}", dctx.stream_id)
        return False, "重新生成失败", True

    if success:
        await plugin.ctx.send.text(f"日程已重新生成，使用 {dctx.prefix} schedule 查看", dctx.stream_id)
        return True, "日程重新生成成功", True
    await plugin.ctx.send.text("LLM 生成失败，已保留原日程", dctx.stream_id)
    return False, "日程生成失败", True
