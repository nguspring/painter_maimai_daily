"""插件核心模块（Pipeline 架构）"""

from .commands import CommandDispatcher
from .config import PainterMaimaiConfig
from .pipeline import GenerationRequest, Pipeline, build_action_pipeline
from .state import RuntimeStateManager
from .utils import CacheManager, ImageProcessor, RequestContext

__all__ = [
    "PainterMaimaiConfig",
    "Pipeline",
    "GenerationRequest",
    "build_action_pipeline",
    "CommandDispatcher",
    "RuntimeStateManager",
    "ImageProcessor",
    "CacheManager",
    "RequestContext",
]
