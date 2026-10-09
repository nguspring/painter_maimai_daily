"""selfie_painter_v2 插件的静态元数据。"""

from __future__ import annotations

from typing import List

from src.plugin_system.base.component_types import PythonDependency

PLUGIN_NAME = "selfie_painter_v2"
PLUGIN_VERSION = "3.6.14"
PLUGIN_AUTHOR = "Ptrel，Rabbit，saberlights Kiuon，nguspring"
ENABLE_PLUGIN = True
DEPENDENCIES: List[str] = []
PYTHON_DEPENDENCIES: List[PythonDependency] = [
    PythonDependency(
        package_name="requests",
        optional=True,
        description="用于部分图片后端（如魔搭、Gemini、砂糖云）的 HTTP 请求",
    ),
    PythonDependency(
        package_name="httpx",
        optional=True,
        description="用于自动自拍发布时拉取网络图片",
    ),
    PythonDependency(
        package_name="volcengine-python-sdk",
        install_name="volcengine-python-sdk[ark]",
        optional=True,
        description="用于豆包（Ark）模型接入",
    ),
    PythonDependency(
        package_name="beautifulsoup4",
        install_name="beautifulsoup4",
        optional=True,
        description="用于角色参考图功能的 Bing 图片搜索解析",
    ),
]
CONFIG_FILE_NAME = "config.toml"

__all__ = [
    "CONFIG_FILE_NAME",
    "DEPENDENCIES",
    "ENABLE_PLUGIN",
    "PLUGIN_AUTHOR",
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
    "PYTHON_DEPENDENCIES",
]
