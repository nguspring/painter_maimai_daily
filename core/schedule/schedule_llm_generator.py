"""
日程 LLM 生成器（增强版）

支持：
1. 人设驱动：从主程序和插件配置读取人设信息
2. 历史记忆：参考历史日程，保持连续性
3. 自定义 Prompt：用户可追加风格要求
4. 多轮生成：质量评分 + 智能重试

使用示例：
    items = await generate_schedule_via_llm(
        plugin=plugin,
        target_date="2026-03-02",
        model_id="planner",
        schedule_manager=manager
    )
"""

from __future__ import annotations

import importlib
import datetime
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from .schedule_models import ScheduleItem, parse_hhmm
from .persona_builder import build_persona_context
from .prompt_builder import build_schedule_prompt, get_prompt_builder
from .quality_evaluator import evaluate_schedule_quality, EvalResult
from .calendar_context import get_calendar_service

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ========== 常量定义 ==========

_VALID_ACTIVITY_TYPES = {
    "sleeping",
    "waking_up",
    "eating",
    "working",
    "studying",
    "exercising",
    "relaxing",
    "socializing",
    "commuting",
    "hobby",
    "self_care",
    "other",
}

_VALID_MOODS = {"happy", "neutral", "calm", "sleepy", "focused", "tired", "anxious", "excited", "bored", "sad"}

_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ========== 数据类 ==========


@dataclass
class GenerateResult:
    """生成结果"""

    items: list[ScheduleItem]
    score: float
    issues: list[str]
    rounds: int
    success: bool


# ========== 解析工具函数 ==========


def _strip_fence(text: str) -> str:
    """剥离 markdown 代码块。"""
    raw = text.strip()
    match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", raw, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw


def _parse_items(raw: str, date: str) -> tuple[list[ScheduleItem], list[str]]:
    """
    解析 LLM JSON 输出。

    Returns:
        tuple: (日程项列表, 警告列表)
    """
    warnings: list[str] = []
    cleaned = _strip_fence(raw)

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.warning("[ScheduleLLM] JSON 解析失败: %s", exc)
        warnings.append(f"JSON解析失败: {exc}")
        return [], warnings

    if not isinstance(payload, list):
        logger.warning("[ScheduleLLM] 响应不是数组")
        warnings.append("响应不是数组格式")
        return [], warnings

    items: list[ScheduleItem] = []
    for i, entry in enumerate(payload):
        if not isinstance(entry, dict):
            warnings.append(f"第{i + 1}项不是对象")
            continue

        try:
            start = parse_hhmm(str(entry.get("start", "")))
            end = parse_hhmm(str(entry.get("end", "")))
        except ValueError as e:
            warnings.append(f"第{i + 1}项时间格式错误: {e}")
            continue

        if start == end:
            warnings.append(f"第{i + 1}项开始时间等于结束时间")
            continue

        if start > end:
            warnings.append(f"第{i + 1}项开始时间晚于结束时间")
            continue

        activity_type = str(entry.get("activity_type", "other")).strip()
        if activity_type not in _VALID_ACTIVITY_TYPES:
            warnings.append(f"第{i + 1}项活动类型无效: {activity_type}")
            activity_type = "other"

        description = str(entry.get("description", "")).strip()[:60] or "日常活动"

        mood = str(entry.get("mood", "neutral")).strip().lower()
        if mood not in _VALID_MOODS:
            mood = "neutral"

        outfit = str(entry.get("outfit", "")).strip()[:30]  # 穿搭字段

        items.append(
            ScheduleItem(
                schedule_date=date,
                start_min=start,
                end_min=end,
                activity_type=activity_type,
                description=description,
                mood=mood,
                outfit=outfit,
                source="llm",
            )
        )

    # 按开始时间排序
    items.sort(key=lambda item: item.start_min)

    # 去重（移除时间重叠的项）
    deduped: list[ScheduleItem] = []
    last_end: int | None = None
    for item in items:
        if last_end is not None and item.start_min < last_end:
            warnings.append(f"检测到时间重叠，已跳过: {item.description}")
            continue
        deduped.append(item)
        last_end = item.end_min if item.end_min > item.start_min else 1440

    return deduped, warnings


# ========== 生成函数 ==========


async def _generate_once(
    prompt: str,
    target_date: str,
    model_id: str,
) -> tuple[list[ScheduleItem], list[str]]:
    """
    执行一次生成。

    Returns:
        tuple: (日程项列表, 警告列表)
    """
    try:
        llm_api = importlib.import_module("src.plugin_system.apis.llm_api")
    except Exception:
        logger.warning("[ScheduleLLM] 无法导入 llm_api")
        return [], ["无法导入 llm_api"]

    try:
        models = llm_api.get_available_models()
        model_config = models.get(model_id) or models.get("replyer")
        if model_config is None:
            logger.warning("[ScheduleLLM] 未找到可用 LLM 模型: %s", model_id)
            return [], [f"未找到可用模型: {model_id}"]

        success, content, _, _ = await llm_api.generate_with_model(
            prompt=prompt,
            model_config=model_config,
            request_type="plugin.selfie_schedule_gen",
            temperature=0.7,
            max_tokens=8192,
        )

        if not success or not content:
            return [], ["LLM 生成失败"]

        items, warnings = _parse_items(content, target_date)
        return items, warnings

    except Exception as exc:
        logger.error("[ScheduleLLM] 日程生成失败: %s", exc, exc_info=True)
        return [], [f"生成异常: {exc}"]


async def generate_schedule_via_llm(
    plugin,
    target_date: str,
    model_id: str = "planner",
    schedule_manager: Any = None,
) -> list[ScheduleItem] | None:
    """
    调用 MaiBot LLM 生成日程（增强版）。

    支持：
    - 人设驱动
    - 历史记忆
    - 自定义 Prompt
    - 多轮生成优化

    Args:
        plugin: 插件实例
        target_date: 目标日期（格式：YYYY-MM-DD）
        model_id: LLM 模型 ID
        schedule_manager: 日程管理器实例

    Returns:
        list[ScheduleItem] | None: 日程项列表，失败返回 None
    """
    # ========== 读取配置 ==========

    # 人设补充配置
    schedule_identity = plugin.get_config("schedule.schedule_identity", "")
    schedule_interest = plugin.get_config("schedule.schedule_interest", "")
    schedule_lifestyle = plugin.get_config("schedule.schedule_lifestyle", "")

    # 历史记忆配置
    history_days = plugin.get_config("schedule.schedule_history_days", 1)

    # 自定义 Prompt
    custom_prompt = plugin.get_config("schedule.schedule_custom_prompt", "")

    # 多轮生成配置
    multi_round_enabled = plugin.get_config("schedule.schedule_multi_round", True)
    max_rounds = plugin.get_config("schedule.schedule_max_rounds", 2)
    quality_threshold = plugin.get_config("schedule.schedule_quality_threshold", 0.8)

    # ========== 构建人设上下文 ==========

    persona_context = build_persona_context(
        schedule_identity=schedule_identity, schedule_interest=schedule_interest, schedule_lifestyle=schedule_lifestyle
    )

    # ========== 构建历史上下文 ==========

    history_context = ""
    if schedule_manager and history_days > 0:
        try:
            history_context = await schedule_manager.get_history_schedule_summary(days=history_days)
        except Exception as e:
            logger.warning("[ScheduleLLM] 获取历史日程失败: %s", e)

    # ========== 构建完整 Prompt ==========

    # 从人设上下文中提取昵称和人设描述
    nickname = "麦麦"
    personality = "是一个女大学生"

    try:
        from src.plugin_system.apis import config_api

        nickname = config_api.get_global_config("bot.nickname", "麦麦")
        personality = config_api.get_global_config("personality.personality", "是一个女大学生")
    except Exception:
        pass

    calendar = await get_calendar_service().get_day(datetime.date.fromisoformat(target_date))
    prompt = build_schedule_prompt(
        persona_context=persona_context,
        history_context=history_context,
        custom_prompt=custom_prompt,
        target_date=target_date,
        nickname=nickname,
        personality=personality,
        calendar_context=calendar.render(),
    )

    # ========== 多轮生成 ==========

    best_result: Optional[tuple[list[ScheduleItem], EvalResult]] = None
    current_prompt = prompt

    for round_num in range(1, max_rounds + 1):
        logger.info("[ScheduleLLM] 开始第 %d 轮生成", round_num)

        # 生成日程
        items, warnings = await _generate_once(
            prompt=current_prompt,
            target_date=target_date,
            model_id=model_id,
        )

        if not items:
            logger.warning("[ScheduleLLM] 第 %d 轮生成失败: 无日程项", round_num)
            continue

        # 评估质量
        eval_result = evaluate_schedule_quality(items, warnings)

        logger.info(
            "[ScheduleLLM] 第 %d 轮完成: 分数=%.2f, 问题数=%d", round_num, eval_result.score, len(eval_result.issues)
        )

        # 记录最佳结果
        if best_result is None or eval_result.score > best_result[1].score:
            best_result = (items, eval_result)

        # 检查是否达标
        if eval_result.score >= quality_threshold:
            logger.info("[ScheduleLLM] 质量达标，提前结束")
            return items

        # 如果不达标且还有轮次，构建重试 Prompt
        if round_num < max_rounds and multi_round_enabled and eval_result.issues:
            prompt_builder = get_prompt_builder()
            current_prompt = prompt_builder.build_retry_prompt(original_prompt=prompt, issues=eval_result.issues)
            logger.info("[ScheduleLLM] 准备重试，修复问题: %s", eval_result.issues[:3])

    # ========== 返回结果 ==========

    if best_result:
        items, eval_result = best_result
        logger.info("[ScheduleLLM] 生成完成: 最终分数=%.2f (阈值=%.2f)", eval_result.score, quality_threshold)
        return items

    logger.warning("[ScheduleLLM] 所有轮次均失败")
    return None


async def generate_schedule_with_result(
    plugin,
    target_date: str,
    model_id: str = "planner",
    schedule_manager: Any = None,
) -> GenerateResult:
    """
    生成日程并返回详细结果。

    与 generate_schedule_via_llm 类似，但返回更多调试信息。

    Args:
        plugin: 插件实例
        target_date: 目标日期
        model_id: LLM 模型 ID
        schedule_manager: 日程管理器实例

    Returns:
        GenerateResult: 包含日程项、分数、问题、轮数等信息
    """
    # 读取配置
    _multi_round_enabled = plugin.get_config("schedule.schedule_multi_round", True)  # noqa: F841
    max_rounds = plugin.get_config("schedule.schedule_max_rounds", 2)
    quality_threshold = plugin.get_config("schedule.schedule_quality_threshold", 0.8)

    # 生成日程
    items = await generate_schedule_via_llm(
        plugin=plugin,
        target_date=target_date,
        model_id=model_id,
        schedule_manager=schedule_manager,
    )

    if items is None:
        return GenerateResult(items=[], score=0.0, issues=["生成失败"], rounds=max_rounds, success=False)

    # 评估最终结果
    eval_result = evaluate_schedule_quality(items, [])

    return GenerateResult(
        items=items,
        score=eval_result.score,
        issues=eval_result.issues,
        rounds=max_rounds,
        success=eval_result.score >= quality_threshold,
    )
