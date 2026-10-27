"""Pipeline 主循环

按顺序执行 steps，按 StepResult 控制流程。
异常会被捕获并写入 req.error；失败时若有 user_message 会自动发送到 stream_id。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List

import asyncio
import logging

from ..api_clients.base_client import NonRetryableError
from ..config.model_registry import get_model_config, resolve_img2img_model, resolve_txt2img_model
from ..quota import QuotaExceeded
from ..quota.service import resolve_quota_context

if TYPE_CHECKING:
    from .request import GenerationRequest
    from .step import PipelineContext, PipelineStep

logger = logging.getLogger("plugin.painter_maimai_daily.pipeline")


def _generation_deadline_seconds(ctx: "PipelineContext", req: "GenerationRequest") -> float:
    """为 ComfyUI 工作流留出完整的轮询预算和结果下载时间。

    普通后端继续使用 basic.generation_timeout_seconds；ComfyUI 的工作流可能
    需要数分钟，不能在固定的 170 秒总时限内提前结束。外层 Tool RPC 仍由
    组件声明的 timeout_ms 负责兜底。
    """
    try:
        base_timeout = float(ctx.plugin.config.basic.generation_timeout_seconds)
    except (AttributeError, TypeError, ValueError):
        base_timeout = 170.0

    try:
        model_id = str(req.model_id or "").strip()
        if not model_id:
            model_id = (
                resolve_img2img_model(ctx.plugin)
                if req.input_image_base64 is not None
                else resolve_txt2img_model(ctx.plugin)
            )
        model_config = get_model_config(ctx.plugin, model_id)
        if str(model_config.get("format", "")).lower() != "comfyui":
            return base_timeout
        wait_seconds = float(ctx.plugin.config.comfyui.wait_seconds)
    except (AttributeError, TypeError, ValueError, KeyError):
        return base_timeout

    # 轮询结束后还需要下载输出图，并留出少量调度余量。配置模型已将
    # wait_seconds 限制在 30..3600，外层宿主超时由 Tool/API 元数据控制。
    return max(base_timeout, wait_seconds + 60.0)


class Pipeline:
    """串行执行一组 Step"""

    def __init__(self, steps: List["PipelineStep"], name: str = "pipeline"):
        self.steps = steps
        self.name = name

    async def run(
        self,
        req: "GenerationRequest",
        ctx: "PipelineContext",
    ) -> "GenerationRequest":
        """统一期限内预占额度；无论在哪一步结束，都按实际投递状态结算。"""
        try:
            async with asyncio.timeout(_generation_deadline_seconds(ctx, req)):
                # 独立生图 API 与后台图片沿用各自的调度限制，不能扣到某个聊天用户头上。
                if req.send_image and req.source in ("tool", "cmd_style", "cmd_natural") and ctx.plugin.config.quota.enabled:
                    await self._reserve_quota(req, ctx)
                return await self._run_steps(req, ctx)
        except TimeoutError:
            req.success = False
            if req.delivery_state == "unknown":
                req.error = "图片生成超过总时限，投递结果尚未确认；请勿重复提交"
            elif req.delivery_state == "sent":
                # 图片已投递，后续缓存/撤回调度超时不能退还额度，也不能诱导用户重试。
                req.success = True
                req.error = None
                req.user_message = "图片已发送，后续处理超过总时限"
            else:
                req.error = "图片生成超过总时限，本次结果不会投递"
            logger.warning("%s %s", req.log_prefix, req.error or req.user_message)
            return req
        except QuotaExceeded:
            req.success = False
            req.error = "本聊天的生图额度已用完"
            req.user_message = "本聊天的生图额度已用完，可用 quota 子命令查看，或请管理员调整额度。"
            await self._send_quota_error(req, ctx)
            return req
        except Exception as exc:
            # _run_steps 自己处理步骤异常；这里的失败来自额度归属/预占，必须阻止请求。
            logger.exception("%s 生图请求准备失败", req.log_prefix)
            req.success = False
            req.error = f"生图请求准备失败：{exc}"
            req.user_message = "未能确认生图额度，本次未开始生图。请管理员查看日志。"
            await self._send_quota_error(req, ctx)
            return req
        finally:
            if req.delivery_state == "sent" and not req.success:
                logger.warning("%s 图片已发送，后续步骤未完成：%s", req.log_prefix, req.error)
                req.success = True
                req.error = None
                req.user_message = "图片已发送，后续处理未完成，请管理员查看日志。"
            if req.quota_reservation_id:
                await self._settle_quota(req, ctx)

    @staticmethod
    async def _send_quota_error(req: "GenerationRequest", ctx: "PipelineContext") -> None:
        if req.stream_id:
            try:
                await ctx.plugin.ctx.send.text(req.user_message, req.stream_id)
            except Exception:
                logger.exception("%s 额度失败提示未能发送", req.log_prefix)

    @staticmethod
    async def _reserve_quota(req: "GenerationRequest", ctx: "PipelineContext") -> None:
        quota_ctx = await resolve_quota_context(
            ctx.plugin, stream_id=req.stream_id, platform=req.platform,
            group_id=req.group_id, user_id=req.user_id,
        )
        # 数据库提交不能在取消时遗失返回的 token；先等提交完成，再由 finally 退款。
        pending = asyncio.create_task(ctx.plugin.quota_store.reserve(quota_ctx.subject, quota_ctx.policy))
        cancelled = False
        while True:
            try:
                reservation = await asyncio.shield(pending)
                break
            except asyncio.CancelledError:
                cancelled = True
                if pending.cancelled():
                    raise
            except Exception as exc:
                if cancelled:
                    raise asyncio.CancelledError() from exc
                raise
        req.quota_reservation_id = reservation.token
        if cancelled:
            raise asyncio.CancelledError()

    @staticmethod
    async def _settle_quota(req: "GenerationRequest", ctx: "PipelineContext") -> None:
        outcome = {
            "sent": "consumed", "unknown": "unknown", "not_sent": "released",
        }[req.delivery_state]
        pending = asyncio.create_task(ctx.plugin.quota_store.settle(req.quota_reservation_id, outcome))
        cancelled = False
        while True:
            try:
                await asyncio.shield(pending)
                break
            except asyncio.CancelledError:
                cancelled = True
                if pending.cancelled():
                    raise
            except Exception:
                # 保留数据库中的占用记录，重启后进入待核对；不能以成功退款掩盖落盘错误。
                logger.exception("%s 额度记录 %s 结算失败，需要管理员核对", req.log_prefix, req.quota_reservation_id)
                req.user_message = "额度记录未能结算，请管理员检查；不要重复提交生图请求。"
                break
        if cancelled:
            raise asyncio.CancelledError()

    async def _run_steps(
        self, req: "GenerationRequest", ctx: "PipelineContext",
    ) -> "GenerationRequest":
        for step in self.steps:
            try:
                result = await step.run(req, ctx)
            except NonRetryableError as e:
                req.success = False
                req.error = str(e)
                logger.warning(f"{req.log_prefix} pipeline {self.name} 在 {step.name} 遇到不可重试错误: {e}")
                return req
            except Exception as e:
                req.success = False
                req.error = repr(e)
                logger.exception(f"{req.log_prefix} pipeline {self.name} 在 {step.name} 抛出异常")
                return req

            if result is None:
                continue

            if result.action == "continue":
                continue
            if result.action == "skip_remaining":
                break
            if result.action == "fail":
                if result.user_message and req.stream_id and req.delivery_state != "sent":
                    try:
                        await ctx.plugin.ctx.send.text(
                            result.user_message, req.stream_id,
                            sync_to_maisaka_history=True,
                            maisaka_source_kind="plugin_send_text",
                        )
                    except Exception as e:
                        logger.warning(f"{req.log_prefix} 发送失败信息失败: {e}")
                req.success = False
                req.error = result.error
                req.user_message = result.user_message
                return req
            if result.action == "done":
                req.success = True
                req.user_message = result.user_message
                return req

        req.success = True
        return req
