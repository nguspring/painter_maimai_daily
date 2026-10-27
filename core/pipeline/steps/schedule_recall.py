"""Step: 调度自动撤回

- schedule_recall=False / send_image=False / cache_hit 时跳过
- basic.auto_recall_enabled + 模型 auto_recall_delay + 会话撤回开关三重校验
- 使用 SDK 发送回执中的平台消息 ID，按实际归属选择适配器公开 API
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import logging

from ...utils import schedule_auto_recall
from ..result import StepResult
from ..step import BaseStep

if TYPE_CHECKING:
    from ..request import GenerationRequest
    from ..step import PipelineContext

logger = logging.getLogger("plugin.painter_maimai_daily.step.recall")


class ScheduleRecall(BaseStep):
    async def run(self, req: "GenerationRequest", ctx: "PipelineContext") -> Optional[StepResult]:
        if not req.schedule_recall or not req.send_image or req.cache_hit:
            return None
        if not req.resolved_model_config or not req.stream_id or req.delivery_state != "sent":
            return None

        plugin = ctx.plugin
        global_enabled = plugin.config.basic.auto_recall_enabled
        if not global_enabled:
            return None

        delay_seconds = int(req.resolved_model_config.get("auto_recall_delay") or 0)
        if delay_seconds <= 0:
            return None

        if req.model_id and not plugin.runtime_state.is_recall_enabled(req.chat_id, req.model_id, global_enabled):
            logger.info(f"{req.log_prefix} 模型 {req.model_id} 撤回已在当前聊天流禁用")
            return None

        if not req.sent_message_id:
            logger.warning("%s SDK 发送回执未包含平台消息 ID，无法安排撤回", req.log_prefix)
            return None

        task = await schedule_auto_recall(
            plugin.ctx, req.chat_id, req.sent_message_id, delay_seconds, req.log_prefix,
        )
        if task is not None:
            plugin._recall_tasks.add(task)
            task.add_done_callback(plugin._recall_tasks.discard)
        return None
