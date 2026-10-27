"""日程质量评估：数量、描述长度、时间覆盖与解析警告惩罚。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ScheduleItem


@dataclass
class QualityIssue:
    """单条结构化质量问题，供重试提示词或日志使用。"""

    severity: str
    category: str
    message: str
    suggestion: str


@dataclass
class EvalResult:
    """评估结果；``issues`` 直接拼进重试提示词。"""

    score: float
    issues: list[str]
    details: dict = field(default_factory=dict)
    quality_issues: list[QualityIssue] = field(default_factory=list)


class ScheduleQualityEvaluator:
    """按旧版评分维度评估，但数量阈值来自配置。"""

    TARGET_DESC_LENGTH = 35
    MIN_DESC_LENGTH = 20
    COVERAGE_START = 7 * 60
    COVERAGE_END = 23 * 60
    COVERAGE_TOTAL = COVERAGE_END - COVERAGE_START

    def __init__(self, min_activities: int = 8, max_activities: int = 15) -> None:
        """保存本次生成使用的数量范围，避免默认值和配置冲突。"""
        if min_activities <= 0 or max_activities < min_activities:
            raise ValueError("日程数量范围无效")
        self.min_activities = min_activities
        self.max_activities = max_activities

    def evaluate(self, items: list["ScheduleItem"], warnings: list[str] | None = None) -> EvalResult:
        """计算 0-1 分数；空列表直接判 0 分。"""
        warnings = warnings or []
        if not items:
            return EvalResult(
                score=0.0,
                issues=["没有生成任何日程项"],
                details={"error": "empty_schedule"},
                quality_issues=[QualityIssue("error", "count", "没有生成任何日程项", "请生成完整日程")],
            )

        score = 0.5
        details: dict = {"base_score": 0.5}
        issues: list[str] = []
        quality_issues: list[QualityIssue] = []

        count_score, count_issues, count_quality = self._evaluate_count(items)
        score += count_score
        issues.extend(count_issues)
        quality_issues.extend(count_quality)
        details.update(count_score=count_score, activity_count=len(items))

        length_score, length_issues, length_quality = self._evaluate_description_length(items)
        score += length_score
        issues.extend(length_issues)
        quality_issues.extend(length_quality)
        details.update(
            length_score=length_score,
            avg_desc_length=sum(len(item.description) for item in items) / len(items),
        )

        coverage_score, coverage_issues, coverage_quality = self._evaluate_time_coverage(items)
        score += coverage_score
        issues.extend(coverage_issues)
        quality_issues.extend(coverage_quality)
        details.update(coverage_score=coverage_score, time_coverage=coverage_score / 0.15)

        warning_penalty = min(len(warnings) * 0.05, 0.3)
        score -= warning_penalty
        details.update(warning_penalty=warning_penalty, warning_count=len(warnings))
        if warnings:
            issues.append(f"解析警告：{', '.join(warnings)}")
            quality_issues.extend(
                QualityIssue("warning", "format", f"解析警告：{warning}", "检查输出格式是否符合要求")
                for warning in warnings
            )

        return EvalResult(
            score=max(0.0, min(1.0, score)),
            issues=issues,
            details=details,
            quality_issues=quality_issues,
        )

    def _evaluate_count(self, items: list["ScheduleItem"]):
        """数量在配置区间内满分，接近减半，过少不得分。"""
        count = len(items)
        if self.min_activities <= count <= self.max_activities:
            return 0.2, [], []
        message = f"活动数量不符合要求（{count}个，建议{self.min_activities}-{self.max_activities}个）"
        severity = "warning" if count >= max(1, self.min_activities - 2) else "error"
        return (
            0.1 if severity == "warning" else 0.0,
            [message],
            [QualityIssue(severity, "count", message, "调整活动数量到配置范围内")],
        )

    def _evaluate_description_length(self, items: list["ScheduleItem"]):
        """平均 35 字满分，20-34 字一半分，低于 20 字不得分。"""
        average = sum(len(item.description) for item in items) / len(items)
        if average >= self.TARGET_DESC_LENGTH:
            return 0.15, [], []
        message = f"描述平均长度偏短（{average:.1f}字，建议{self.TARGET_DESC_LENGTH}字以上）"
        severity = "warning" if average >= self.MIN_DESC_LENGTH else "error"
        return (
            0.075 if severity == "warning" else 0.0,
            [message],
            [QualityIssue(severity, "length", message, "扩充活动描述，让描述更有生活感")],
        )

    def _evaluate_time_coverage(self, items: list["ScheduleItem"]):
        """按 7:00-23:00 的分钟覆盖率给分，跨午夜项按当天剩余时间计算。"""
        covered = [False] * self.COVERAGE_TOTAL
        for item in items:
            start = max(item.start_min, self.COVERAGE_START)
            end = item.end_min
            if end <= item.start_min:
                end = 1440
            end = min(end, self.COVERAGE_END)
            if start < end:
                offset = start - self.COVERAGE_START
                for index in range(offset, offset + (end - start)):
                    if 0 <= index < self.COVERAGE_TOTAL:
                        covered[index] = True

        ratio = sum(covered) / self.COVERAGE_TOTAL
        score = ratio * 0.15
        if ratio >= 0.8:
            return score, [], []
        gaps = self._find_gaps(items)
        gap_text = ", ".join(f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}" for start, end in gaps[:3])
        message = f"时间覆盖不完整（覆盖率{ratio * 100:.1f}%），空档：{gap_text or '未知'}"
        severity = "warning" if ratio >= 0.5 else "error"
        return score, [message], [QualityIssue(severity, "coverage", message, "在空档时间段添加活动，确保时间连续")]

    def _find_gaps(self, items: list["ScheduleItem"]) -> list[tuple[int, int]]:
        """找出 7:00-23:00 内超过 0 分钟的空档。"""
        sorted_items = sorted(items, key=lambda item: item.start_min)
        gaps: list[tuple[int, int]] = []
        current = self.COVERAGE_START
        for item in sorted_items:
            if item.start_min > current:
                gap_end = min(item.start_min, self.COVERAGE_END)
                if current < gap_end:
                    gaps.append((max(current, self.COVERAGE_START), gap_end))
            end = 1440 if item.end_min <= item.start_min else item.end_min
            current = max(current, min(end, self.COVERAGE_END))
        if current < self.COVERAGE_END:
            gaps.append((current, self.COVERAGE_END))
        return gaps


def evaluate_schedule_quality(
    items: list["ScheduleItem"], warnings: list[str] | None = None,
    *, min_activities: int = 8, max_activities: int = 15,
) -> EvalResult:
    """模块级便捷入口。"""
    return ScheduleQualityEvaluator(min_activities, max_activities).evaluate(items, warnings)
