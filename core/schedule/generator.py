"""日程 LLM 生成器：人设 + 历史 + 质量评估 + 多轮重试。

生成的每一项都会先经过严格解析，再写入数据库；解析失败或质量过低时
保留已有模板日程，不覆盖用户可见数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import asyncio
import json
import logging
import re

from .models import ScheduleItem, parse_hhmm
from .persona_builder import build_persona_context, get_bot_nickname, get_personality
from .prompt_builder import SchedulePromptBuilder
from .quality import EvalResult, ScheduleQualityEvaluator

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin
    from .manager import ScheduleManager

logger = logging.getLogger("plugin.painter_maimai_daily.schedule.generator")

_VALID_ACTIVITY_TYPES = {
    "sleeping", "waking_up", "eating", "working", "studying", "exercising",
    "relaxing", "socializing", "commuting", "hobby", "self_care", "other",
}
_VALID_MOODS = {
    "happy", "neutral", "calm", "sleepy", "focused", "tired",
    "anxious", "excited", "bored", "sad", "angry",
}


@dataclass
class GenerateResult:
    """一次生成的质量与轮次摘要，供日志和调试使用。"""

    items: list[ScheduleItem]
    score: float
    issues: list[str]
    rounds: int
    success: bool


def _strip_fence(text: str) -> str:
    """剥离模型偶尔附带的 markdown 代码块围栏。"""
    matched = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", text.strip(), re.DOTALL)
    return matched.group(1).strip() if matched else text.strip()


def parse_schedule_items(raw: str, target_date: str) -> tuple[list[ScheduleItem], list[str]]:
    """解析模型 JSON；非法条目记录警告并跳过，不猜测缺失字段。"""
    warnings: list[str] = []
    try:
        payload = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        return [], [f"JSON解析失败: {exc}"]
    if not isinstance(payload, list):
        return [], ["响应不是数组格式"]

    items: list[ScheduleItem] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            warnings.append(f"第{index + 1}项不是对象")
            continue
        try:
            start = parse_hhmm(str(entry.get("start", "")))
            end = parse_hhmm(str(entry.get("end", "")))
        except ValueError as exc:
            warnings.append(f"第{index + 1}项时间格式错误: {exc}")
            continue
        if start == end:
            warnings.append(f"第{index + 1}项开始时间等于结束时间")
            continue
        if start > end:
            warnings.append(f"第{index + 1}项开始时间晚于结束时间")
            continue

        activity_type = str(entry.get("activity_type", "other")).strip().lower()
        if activity_type not in _VALID_ACTIVITY_TYPES:
            warnings.append(f"第{index + 1}项活动类型无效: {activity_type}")
            activity_type = "other"
        description = str(entry.get("description", "")).strip()[:60] or "日常活动"
        mood = str(entry.get("mood", "neutral")).strip().lower()
        if mood not in _VALID_MOODS:
            mood = "neutral"

        items.append(
            ScheduleItem(
                schedule_date=target_date,
                start_min=start,
                end_min=end,
                activity_type=activity_type,
                description=description,
                mood=mood,
                source="llm",
            )
        )

    items.sort(key=lambda item: item.start_min)
    deduped: list[ScheduleItem] = []
    last_end: Optional[int] = None
    for item in items:
        if last_end is not None and item.start_min < last_end:
            warnings.append(f"检测到时间重叠，已跳过: {item.description}")
            continue
        deduped.append(item)
        last_end = 1440 if item.end_min <= item.start_min else item.end_min
    return deduped, warnings


async def _generate_once(plugin: "PainterMaimaiPlugin", prompt: str, target_date: str) -> tuple[list[ScheduleItem], list[str]]:
    """调用一次宿主 LLM；空响应或失败返回空列表并记录原因。"""
    cfg = plugin.config.schedule
    try:
        result = await plugin.ctx.llm.generate(
            prompt=prompt,
            model=cfg.llm_task_name,
            temperature=0.7,
            max_tokens=cfg.max_tokens,
            timeout_ms=cfg.generation_timeout_seconds * 1000,
        )
    except Exception as exc:
        logger.error("日程 LLM 调用失败: %s", exc, exc_info=True)
        return [], [f"生成异常: {exc}"]

    if not isinstance(result, dict) or result.get("success") is False:
        return [], [f"LLM 生成失败: {result.get('error', '') if isinstance(result, dict) else '返回值不是字典'}"]

    content = result.get("response", result.get("content", ""))
    if not isinstance(content, str) or not content.strip():
        return [], ["LLM 返回空响应"]
    return parse_schedule_items(content, target_date)


async def _build_prompt(
    plugin: "PainterMaimaiPlugin",
    target_date: str,
    manager: Optional["ScheduleManager"],
) -> str:
    """汇总人设、历史与配置约束，生成首轮提示词。"""
    cfg = plugin.config.schedule
    persona = await build_persona_context(
        plugin.ctx,
        schedule_identity=cfg.schedule_identity,
        schedule_interest=cfg.schedule_interest,
        schedule_lifestyle=cfg.schedule_lifestyle,
    )
    history = ""
    if manager is not None and cfg.history_days > 0:
        try:
            history = await manager.get_history_schedule_summary(days=cfg.history_days)
        except Exception:
            logger.exception("读取历史日程失败，本次生成不参考历史")
    nickname = await get_bot_nickname(plugin.ctx)
    personality = await get_personality(plugin.ctx)
    return SchedulePromptBuilder().build(
        persona_context=persona,
        history_context=history,
        custom_prompt=cfg.custom_prompt,
        target_date=target_date,
        nickname=nickname,
        personality=personality,
        min_activities=cfg.min_activities,
        max_activities=cfg.max_activities,
    )


async def generate_schedule_via_llm(
    plugin: "PainterMaimaiPlugin",
    target_date: str,
    manager: Optional["ScheduleManager"] = None,
) -> Optional[list[ScheduleItem]]:
    """生成指定日期的日程；失败或超时返回 ``None``，绝不写半成品。"""
    cfg = plugin.config.schedule
    prompt = await _build_prompt(plugin, target_date, manager)
    evaluator = ScheduleQualityEvaluator(cfg.min_activities, cfg.max_activities)
    max_rounds = cfg.max_rounds if cfg.multi_round else 1
    best: Optional[tuple[list[ScheduleItem], EvalResult]] = None
    current_prompt = prompt

    try:
        async with asyncio.timeout(cfg.generation_timeout_seconds):
            for round_index in range(1, max_rounds + 1):
                items, warnings = await _generate_once(plugin, current_prompt, target_date)
                if not items:
                    logger.warning("日程第 %d 轮没有可用条目: %s", round_index, warnings[:3])
                    continue
                evaluation = evaluator.evaluate(items, warnings)
                logger.info(
                    "日程第 %d 轮完成：分数 %.2f，条目 %d，问题 %d",
                    round_index, evaluation.score, len(items), len(evaluation.issues),
                )
                if best is None or evaluation.score > best[1].score:
                    best = (items, evaluation)
                if evaluation.score >= cfg.quality_threshold:
                    return items
                if round_index < max_rounds and evaluation.issues:
                    current_prompt = SchedulePromptBuilder().build_retry_prompt(prompt, evaluation.issues)
    except TimeoutError:
        logger.error("日程生成超过 %d 秒，使用本轮最佳结果或模板", cfg.generation_timeout_seconds)

    if best is not None:
        logger.info("日程生成结束：最佳分数 %.2f（阈值 %.2f）", best[1].score, cfg.quality_threshold)
        return best[0]
    logger.warning("日程生成失败，保留已有或模板数据")
    return None
