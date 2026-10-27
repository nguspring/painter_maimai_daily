"""配文生成器

为自拍图片生成配文：
- 基于当前活动/日程 + MaiBot 完整人设（昵称 / 人设描述 / 兴趣 / 表达风格）自然生成
- 通过 ctx.llm.generate 调用主 LLM
- 通过 ctx.config.get 读取主程序 bot.nickname / personality.* 字段
- 生成失败返回空字符串，由调用方决定是否发布

字段对齐说明：
    使用 MaiBot 的人设配置字段
    （personality / reply_style / interest / bot_name），并保留本插件特色的
    ``multiple_reply_style`` + ``multiple_probability`` 随机替换。
"""

from typing import TYPE_CHECKING, Dict, List, Optional

import asyncio
import json
import logging
import random

from .schedule_provider import ActivityInfo
from ..prompts.prompt_templates import load_prompt_template

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext

logger = logging.getLogger("plugin.painter_maimai_daily.caption")


async def _get_reply_style(ctx: "PluginContext") -> str:
    """获取表达风格，按概率走 multiple_reply_style 随机替换"""
    reply_style = await ctx.config.get("personality.reply_style", "")

    multi_styles = await ctx.config.get("personality.multiple_reply_style", [])
    probability_raw = await ctx.config.get("personality.multiple_probability", 0.0)
    try:
        probability = float(probability_raw or 0.0)
    except (TypeError, ValueError):
        probability = 0.0

    if multi_styles and probability > 0 and random.random() < probability:
        try:
            reply_style = random.choice(list(multi_styles))
        except Exception:
            pass

    return str(reply_style or "")


async def _get_bot_profile(ctx: "PluginContext") -> Dict[str, str]:
    """统一拉取 bot 完整人设字段

    使用 Host 配置能力读取：
        ``bot.nickname`` / ``personality.personality`` / ``personality.interest`` /
        ``personality.reply_style``（含 multiple_reply_style 随机）。

    全部字段缺失时不抛异常，由调用方决定是否生成配文。
    """
    try:
        bot_name = await ctx.config.get("bot.nickname", "")
        personality = await ctx.config.get("personality.personality", "")
        interest = await ctx.config.get("personality.interest", "")
        reply_style = await _get_reply_style(ctx)
    except Exception as exc:
        logger.warning(f"拉取 bot_profile 失败，使用空人设: {exc}")
        bot_name = personality = interest = reply_style = ""

    return {
        "bot_name": str(bot_name or ""),
        "personality": str(personality or ""),
        "interest": str(interest or ""),
        "reply_style": str(reply_style or ""),
    }


def _build_caption_prompt(
    activity_info: ActivityInfo,
    bot_profile: Dict[str, str],
    *, destination: str = "qzone", group_context: Optional[List[Dict[str, str]]] = None,
    outfit: str = "", photo_plan: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    """角色资料和群语境放入独立数据消息，避免它们改写配文规则。"""
    return [
        {"role": "system", "content": load_prompt_template("selfie_caption")},
        {"role": "user", "content": json.dumps({
            "persona": bot_profile, "activity": activity_info.to_payload(), "destination": destination,
            "group_messages": group_context or [], "photo_outfit": outfit or activity_info.outfit,
            "photo_plan": photo_plan or {},
        }, ensure_ascii=False)},
    ]


async def generate_caption(
    ctx: "PluginContext",
    activity_info: ActivityInfo,
    llm_task: str = "utils",
    timeout_seconds: float = 45,
    *, destination: str = "qzone", group_context: Optional[List[Dict[str, str]]] = None,
    outfit: str = "", photo_plan: Optional[Dict[str, str]] = None,
) -> str:
    """为自拍生成配文

    基于当前活动 + bot 完整人设（昵称/人设/兴趣/表达风格）由 LLM 自然生成。
    生成失败返回空字符串。

    Args:
        ctx: PluginContext
        activity_info: 当前活动信息
        llm_task: 调用 ctx.llm.generate 时使用的任务名

    Returns:
        配文文本，失败时返回空字符串
    """
    bot_profile = await _get_bot_profile(ctx)

    try:
        prompt = await asyncio.to_thread(
            _build_caption_prompt,
            activity_info, bot_profile, destination=destination, group_context=group_context,
            outfit=outfit, photo_plan=photo_plan,
        )

        result = await ctx.llm.generate(
            prompt=prompt,
            model=llm_task,
            temperature=0.85,
            max_tokens=512,
            timeout_ms=int(timeout_seconds * 1000),
        )

        success = False
        caption_raw = ""
        if isinstance(result, dict):
            success = bool(result.get("success", True))
            caption_raw = str(result.get("response") or result.get("content") or "")

        if not (success and caption_raw):
            logger.warning("LLM 返回空响应，配文生成失败")
            return ""

        # 清理输出
        caption = caption_raw.strip().strip('"').strip("'").strip("「").strip("」")
        # 限制长度
        if len(caption) > 80:
            caption = caption[:77] + "..."
        if len(caption) < 2:
            logger.warning("LLM 返回配文过短，视为失败")
            return ""

        # 完整性检查：配文应以标点或表情结尾，否则可能被截断
        valid_endings = ("。", "！", "？", "~", "～", "…", ")", "）",
                         "」", "'", '"', "♪", "☆", "♡",
                         "呢", "哦", "啊", "呀", "吧", "了", "嘛", "哈", "噢", "耶")
        if len(caption) >= 8 and not caption.endswith(valid_endings):
            # 尝试截断到最后一个完整句子
            for punct in ("。", "！", "？", "~", "～", "…"):
                last_pos = caption.rfind(punct)
                if last_pos > 0:
                    caption = caption[:last_pos + 1]
                    break

        bot_name_display = bot_profile.get("bot_name") or "<未配置 bot.nickname>"
        logger.info(f"LLM 生成配文（bot={bot_name_display}）: {caption}")
        return caption

    except Exception as e:
        logger.error(f"LLM 配文生成失败: {e}")
        return ""
