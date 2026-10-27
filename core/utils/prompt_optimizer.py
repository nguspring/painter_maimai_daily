"""通过 SDK 辅助 LLM 整理专业提示词，按实际后端选择语法。

用户描述以独立数据消息传入；优化失败明确返回错误，不能伪装成成功。
旧调用参数与常量导出保留，普通图、图生图和旧场景入口共用多语言模板。
"""

from typing import TYPE_CHECKING, Dict, List, Tuple

import asyncio
import json
import logging
import re

from .prompt_policy import strip_prompt_weights, uses_weighted_tags

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext

logger = logging.getLogger("plugin.painter_maimai_daily.optimizer")

# 保留旧模块导出；完整编辑规则来自三语言模板，不再固定为 SD 权重。
OPTIMIZER_SYSTEM_PROMPT = "You are a professional visual prompt editor. Preserve the requested content, composition and medium, and use the specified backend syntax."
SELFIE_SCENE_SYSTEM_PROMPT = "Describe only the supplied setting, environment, weather, time, lighting and atmosphere. Do not add character appearance or clothing."


def _clean_response(response: str) -> str:
    """移除常见输出前缀、外层引号和多余空白，保留画面文字。"""
    result = response.strip()
    result = re.sub(r"^```[a-zA-Z_-]*\s*|\s*```$", "", result).strip()
    for prefix in ("Output:", "output:", "Prompt:", "prompt:"):
        if result.startswith(prefix):
            result = result[len(prefix):].strip()
            break
    if len(result) >= 2 and result[0] == result[-1] and result[0] in {"\"", "'"}:
        result = result[1:-1]
    return " ".join(result.split())


def _build_optimizer_messages(
    description: str, *, api_format: str, prompt_syntax: str,
    scene_only: bool, is_img2img: bool, output_size: str,
) -> List[Dict[str, str]]:
    # utils 和 prompts 仍保留旧 re-export，在调用时加载模板以避免循环导入。
    from ..prompts.prompt_templates import load_prompt_template

    return [
        {"role": "system", "content": load_prompt_template("prompt_optimizer")},
        {"role": "user", "content": json.dumps({
            "image_description": description, "api_format": api_format,
            "output_syntax": "tags" if uses_weighted_tags(api_format, prompt_syntax) else "natural",
            "scene_only": scene_only, "is_img2img": is_img2img, "output_size": output_size,
        }, ensure_ascii=False)},
    ]


async def optimize_prompt(
    ctx: "PluginContext",
    user_description: str,
    log_prefix: str = "[PromptOptimizer]",
    scene_only: bool = False,
    llm_task: str = "utils",
    timeout_seconds: float = 45,
    *,
    api_format: str = "openai",
    prompt_syntax: str = "auto",
    is_img2img: bool = False,
    output_size: str = "",
) -> Tuple[bool, str]:
    """返回 (成功, 提示词或错误)，新增后端/编辑参数不改变旧位置参数。"""
    if not user_description or not user_description.strip():
        return False, "描述不能为空"

    logger.info("%s 开始优化提示词，backend=%s，img2img=%s", log_prefix, api_format, is_img2img)
    try:
        messages = await asyncio.to_thread(
            _build_optimizer_messages,
            user_description.strip(), api_format=api_format, prompt_syntax=prompt_syntax,
            scene_only=scene_only, is_img2img=is_img2img, output_size=output_size,
        )
        result = await ctx.llm.generate(
            prompt=messages, model=llm_task, temperature=0.3, max_tokens=2048,
            timeout_ms=int(timeout_seconds * 1000),
        )
        if not isinstance(result, dict) or result.get("success") is False:
            raise RuntimeError("提示词优化 LLM 调用失败")
        response = result.get("response", result.get("content", ""))
        if not isinstance(response, str) or not response.strip():
            raise ValueError("提示词优化 LLM 返回空响应")
        optimized = _clean_response(response)
        if not optimized or optimized.startswith(("{", "[")) or len(optimized) > 6000:
            raise ValueError("提示词优化 LLM 返回无效提示词")
        if not uses_weighted_tags(api_format, prompt_syntax):
            optimized = strip_prompt_weights(optimized)
        if not optimized.strip():
            raise ValueError("提示词优化 LLM 返回无效提示词")
        logger.info("%s 提示词优化完成，语法=%s", log_prefix, "tags" if uses_weighted_tags(api_format, prompt_syntax) else "natural")
        return True, optimized
    except Exception as exc:
        logger.exception("%s 提示词优化失败", log_prefix)
        return False, f"提示词优化失败: {exc}"
