"""日程生成的人设上下文构建。

主程序配置经 SDK ``ctx.config.get`` 异步读取；插件配置由调用方传入，
最终拼成一段可直接放进提示词的资料文本。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maibot_sdk.context import PluginContext


async def get_bot_nickname(ctx: "PluginContext") -> str:
    """读取 ``bot.nickname``；空值使用中性占位，避免提示词出现空主语。"""
    value = await ctx.config.get("bot.nickname", "")
    text = str(value or "").strip()
    return text or "麦麦"


async def get_personality(ctx: "PluginContext") -> str:
    """读取主程序人格描述；空值使用最小可用描述。"""
    value = await ctx.config.get("personality.personality", "")
    text = str(value or "").strip()
    return text or "是一个普通人"


async def get_reply_style(ctx: "PluginContext") -> str:
    """读取表达风格；该字段可为空，不影响日程生成。"""
    value = await ctx.config.get("personality.reply_style", "")
    return str(value or "").strip()


async def build_persona_context(
    ctx: "PluginContext",
    *,
    schedule_identity: str = "",
    schedule_interest: str = "",
    schedule_lifestyle: str = "",
) -> str:
    """合并主程序人设与插件提供的身份/兴趣/生活习惯。"""
    nickname = await get_bot_nickname(ctx)
    personality = await get_personality(ctx)
    reply_style = await get_reply_style(ctx)

    parts = [f"你是{nickname}，{personality}"]
    if schedule_identity.strip():
        parts.append(f"\n【身份】\n{schedule_identity.strip()}")
    if schedule_interest.strip():
        parts.append(f"\n【兴趣】\n{schedule_interest.strip()}")
    if schedule_lifestyle.strip():
        parts.append(f"\n【生活规律】\n{schedule_lifestyle.strip()}")
    if reply_style:
        parts.append(f"\n【回复风格】\n{reply_style}")
    return "\n".join(parts)
