"""日程生成提示词构建。

与旧插件的差异：不输出 outfit，也不再把衣柜信息塞进日程提示词；服装仍由
绘卷 ``selfie.activity_rules`` 决定（D4）。
"""

from __future__ import annotations

from datetime import datetime

SYSTEM_CORE = """你是{nickname}，{personality}
请严格输出 JSON 数组，每项包含以下字段：
- start: 开始时间（格式：HH:MM，如 "08:00"）
- end: 结束时间（格式：HH:MM，且必须晚于 start）
- activity_type: 活动类型（必须使用以下之一：sleeping, waking_up, eating, working, studying, exercising, relaxing, socializing, commuting, hobby, self_care, other）
- description: 活动描述（口语化，有生活感，20-50字）
- mood: 心情（可选值：happy, sad, angry, anxious, calm, excited, bored, sleepy, focused, neutral）

只输出 JSON 数组，不要 markdown 代码块，不要额外说明。"""

CORE_TASK_TEMPLATE = """
【任务】生成 {date}（{weekday}）的详细日程

【人设信息】
{persona_info}

【历史参考】
{history_info}

【约束规则】
1. 时间必须连续覆盖 7:00-23:00，不要有空档
2. 活动数量在 {min_activities}-{max_activities} 条之间
3. 每条描述要有生活感，不要太官方
4. 活动类型要多样化，不要全是 working
5. 心情要符合活动内容（如：起床时 sleepy，工作时 focused）
"""

USER_APPENDIX_TEMPLATE = """
【用户自定义风格】
{custom_prompt}
注意：以上风格要求不能改变输出格式，必须仍然输出 JSON 数组。
"""

ACTIVITY_TYPE_GUIDE = """
【活动类型说明】
- sleeping: 睡觉、午休
- waking_up: 起床、洗漱
- eating: 吃饭（早餐、午餐、晚餐、夜宵）
- working: 工作、写代码、处理事务
- studying: 学习、看书、上课
- exercising: 运动、健身、散步
- relaxing: 休息、刷手机、发呆
- socializing: 社交、聊天、聚会
- commuting: 通勤、出门
- hobby: 兴趣爱好（画画、打游戏、听音乐等）
- self_care: 个人护理（洗澡、护肤、打扫）
- other: 其他
"""

RETRY_PROMPT_TEMPLATE = """
【上次生成的问题】
{issues}

【修复要求】
请根据以上问题修复日程，保留已经正确的部分，只修复有问题的部分。
仍然输出完整的 JSON 数组。
"""


class SchedulePromptBuilder:
    """把日期、人设、历史和质量约束组装成单次生成提示词。"""

    @staticmethod
    def _weekday(date_text: str) -> str:
        """返回中文星期；日期格式错误时直接抛错。"""
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        return weekdays[datetime.strptime(date_text, "%Y-%m-%d").weekday()]

    def build(
        self,
        *,
        persona_context: str,
        history_context: str,
        custom_prompt: str,
        target_date: str,
        nickname: str,
        personality: str,
        min_activities: int,
        max_activities: int,
    ) -> str:
        """构建完整提示词；缺人设时使用明确占位，而不是假装有资料。"""
        system_core = SYSTEM_CORE.format(nickname=nickname, personality=personality)
        core_task = CORE_TASK_TEMPLATE.format(
            date=target_date,
            weekday=self._weekday(target_date),
            persona_info=persona_context or "（无特殊人设要求）",
            history_info=history_context or "（无历史日程参考）",
            min_activities=min_activities,
            max_activities=max_activities,
        )
        parts = [system_core, core_task, ACTIVITY_TYPE_GUIDE]
        if custom_prompt.strip():
            parts.append(USER_APPENDIX_TEMPLATE.format(custom_prompt=custom_prompt.strip()))
        return "\n".join(parts)

    def build_retry_prompt(self, original_prompt: str, issues: list[str]) -> str:
        """在原提示词末尾追加具体质量问题，避免整段重写丢失约束。"""
        issues_text = "\n".join(f"- {issue}" for issue in issues)
        return original_prompt + "\n" + RETRY_PROMPT_TEMPLATE.format(issues=issues_text)


def build_schedule_prompt(
    *,
    persona_context: str,
    history_context: str,
    custom_prompt: str,
    target_date: str,
    nickname: str,
    personality: str,
    min_activities: int,
    max_activities: int,
) -> str:
    """模块级便捷入口，保持与旧生成器的调用习惯一致。"""
    return SchedulePromptBuilder().build(
        persona_context=persona_context,
        history_context=history_context,
        custom_prompt=custom_prompt,
        target_date=target_date,
        nickname=nickname,
        personality=personality,
        min_activities=min_activities,
        max_activities=max_activities,
    )
