"""读取当前强类型 models.items 配置，旧结构由升级入口迁移。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin


def list_models(plugin: "PainterMaimaiPlugin") -> Dict[str, Dict[str, Any]]:
    """返回 {id: config_dict} 形式的所有模型。"""
    return {
        item.id: item.model_dump(mode="python")
        for item in plugin.config.models.items
    }


def resolve_txt2img_model(plugin: "PainterMaimaiPlugin") -> str:
    """解析默认文生图模型 ID，留空时使用 model1。"""
    return plugin.config.basic.default_txt2img_model.strip() or "model1"


def resolve_img2img_model(plugin: "PainterMaimaiPlugin") -> str:
    """解析默认图生图模型 ID，留空时使用文生图模型。"""
    return (
        plugin.config.basic.default_img2img_model.strip()
        or resolve_txt2img_model(plugin)
    )


def get_model_config(plugin: "PainterMaimaiPlugin", model_id: Optional[str] = None) -> Dict[str, Any]:
    """按 ID 精确查找模型；未指定时使用默认模型，不存在时返回空字典。"""
    resolved_id = str(model_id or resolve_txt2img_model(plugin)).strip()
    return list_models(plugin).get(resolved_id, {})


def model_exists(plugin: "PainterMaimaiPlugin", model_id: str) -> bool:
    return bool(model_id) and model_id in list_models(plugin)
