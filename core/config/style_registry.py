"""读取当前强类型 styles.items 配置，旧结构由升级入口迁移。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin


def list_styles(plugin: "PainterMaimaiPlugin") -> List[Dict[str, Any]]:
    """返回所有风格预设的独立字典副本。"""
    return [item.model_dump(mode="python") for item in plugin.config.styles.items]


def resolve_style_alias(plugin: "PainterMaimaiPlugin", input_name: str) -> Optional[str]:
    """根据英文风格名或配置的中文别名查找风格。"""
    needle = str(input_name or "").strip()
    if not needle:
        return None
    for style in list_styles(plugin):
        name = str(style.get("name") or "")
        if name == needle:
            return name
        aliases = style.get("aliases") or ""
        if isinstance(aliases, str) and needle in {item.strip() for item in aliases.split(",") if item.strip()}:
            return name
    return None


def get_style_prompt(plugin: "PainterMaimaiPlugin", style_name: str) -> Optional[str]:
    """按名称读取风格提示词，不存在或为空时返回 None。"""
    for style in list_styles(plugin):
        if style.get("name") != style_name:
            continue
        prompt = style.get("prompt")
        return prompt.strip() if isinstance(prompt, str) and prompt.strip() else None
    return None


def get_style_aliases(plugin: "PainterMaimaiPlugin", style_name: str) -> List[str]:
    """返回指定风格的中文别名列表。"""
    for style in list_styles(plugin):
        if style.get("name") != style_name:
            continue
        aliases = style.get("aliases") or ""
        if isinstance(aliases, str):
            return [item.strip() for item in aliases.split(",") if item.strip()]
    return []
