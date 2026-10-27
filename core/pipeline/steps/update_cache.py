"""Step: 写入缓存

- update_cache=False / cache_hit 时跳过
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ...utils.cache_manager import request_cache_identity
from ..result import StepResult
from ..step import BaseStep

if TYPE_CHECKING:
    from ..request import GenerationRequest
    from ..step import PipelineContext

logger = logging.getLogger("plugin.painter_maimai_daily.step.update_cache")


class UpdateCache(BaseStep):
    async def run(self, req: "GenerationRequest", ctx: "PipelineContext") -> Optional[StepResult]:
        if not req.update_cache or req.cache_hit:
            return None
        if not req.resolved_image_data or not req.resolved_model_config:
            return None

        model_name = request_cache_identity(req.resolved_model_config, req.description, req.input_image_base64)
        try:
            ctx.cache.cache_result(
                req.description, model_name, req.final_size,
                req.strength, req.is_img2img, req.resolved_image_data,
            )
        except Exception as e:
            logger.warning(f"{req.log_prefix} 写入缓存失败: {e}")
        return None
