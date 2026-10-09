"""
日程生成 Prompt 构建器

用于构建日程生成所需的完整 Prompt，采用模板式设计：
- SYSTEM_CORE：核心指令，不可修改（保证 JSON 格式）
- CORE_TASK：任务说明，包含人设、历史、约束
- USER_APPENDIX：用户自定义风格要求

功能：
1. 合并人设上下文
2. 合并历史日程摘要
3. 添加用户自定义风格
4. 构建完整 Prompt
5. 构建重试 Prompt（用于多轮生成）
"""

from typing import Optional
from datetime import datetime


# ========== Prompt 模板常量 ==========

# 系统核心指令（不可修改）
SYSTEM_CORE = """你是{nickname}，{personality}
请严格输出 JSON 数组，每项包含以下字段：
- start: 开始时间（格式：HH:MM，如 "08:00"）
- end: 结束时间（格式：HH:MM，如 "09:00"）
- activity_type: 活动类型（必须使用以下之一：sleeping, waking_up, eating, working, studying, exercising, relaxing, socializing, commuting, hobby, self_care, other）
- description: 活动描述（口语化，有生活感，20-50字）
- mood: 心情（可选值：happy, sad, angry, anxious, calm, excited, bored, sleepy, focused, neutral）
- outfit: 穿搭（当前活动适合穿的衣服，简短描述，如："宽松休闲装"、"可爱睡衣"、"运动服"）

只输出 JSON 数组，不要 markdown 代码块，不要额外说明。"""

# 核心任务说明模板
CORE_TASK_TEMPLATE = """
【任务】生成 {date}（{weekday}）的详细日程

【人设信息】
{persona_info}

【历史参考】
{history_info}

【衣柜信息】
{wardrobe_info}

【约束规则】
1. 时间必须连续覆盖 7:00-23:00，不要有空档
2. 活动数量在 8-15 条之间
3. 每条描述要有生活感，不要太官方
4. 活动类型要多样化，不要全是 working
5. 心情要符合活动内容（如：起床时 sleepy，工作时 focused）
6. 穿搭要符合活动场景：
   - 睡觉/休息 → 睡衣
   - 运动/健身 → 运动服
   - 其他活动 → 从日常穿搭中选择
   - 考虑季节：冬天穿厚衣服，夏天穿薄衣服
"""

# 用户追加模板
USER_APPENDIX_TEMPLATE = """
【用户自定义风格】
{custom_prompt}
注意：以上风格要求不能改变输出格式，必须仍然输出 JSON 数组。
"""

# 活动类型说明
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

# 重试 Prompt 模板
RETRY_PROMPT_TEMPLATE = """
【上次生成的问题】
{issues}

【修复要求】
请根据以上问题修复日程，保留已经正确的部分，只修复有问题的部分。
仍然输出完整的 JSON 数组。
"""


class SchedulePromptBuilder:
    """
    日程生成 Prompt 构建器

    负责构建日程生成所需的完整 Prompt。

    使用示例：
        builder = SchedulePromptBuilder()
        prompt = builder.build(
            persona_context="你是麦麦，是一个女大学生...",
            history_context="昨天在学Python...",
            custom_prompt="日程要宽松一些",
            target_date="2026-03-02"
        )
    """

    def __init__(self):
        """初始化 Prompt 构建器"""
        pass

    def _get_weekday(self, date_str: str) -> str:
        """
        获取日期对应的星期几

        Args:
            date_str: 日期字符串（格式：YYYY-MM-DD）

        Returns:
            str: 星期几（如：星期一）
        """
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        return weekdays[date_obj.weekday()]

    def build(
        self,
        persona_context: str,
        history_context: str,
        custom_prompt: str,
        target_date: str,
        wardrobe_info: str = "",
        nickname: str = "麦麦",
        personality: str = "是一个女大学生",
        calendar_context: str = "",
    ) -> str:
        """
        构建完整的日程生成 Prompt

        Args:
            persona_context: 人设上下文（由 PersonaContextBuilder 生成）
            history_context: 历史日程摘要（由 ScheduleManager.get_history_schedule_summary 生成）
            custom_prompt: 用户自定义风格要求
            target_date: 目标日期（格式：YYYY-MM-DD）
            nickname: Bot 名称
            personality: 人设描述
            calendar_context: 目标日期的日历事实；仅供生成安排参考，不改变 JSON 输出格式

        Returns:
            str: 完整的 Prompt

        示例：
            prompt = builder.build(
                persona_context="你是麦麦...",
                history_context="昨天在学Python...",
                custom_prompt="日程要宽松",
                target_date="2026-03-02"
            )
        """
        # 构建系统核心指令
        system_core = SYSTEM_CORE.format(nickname=nickname, personality=personality)

        # 获取星期几
        weekday = self._get_weekday(target_date)

        # 处理人设信息
        persona_info = persona_context if persona_context else "（无特殊人设要求）"

        # 处理历史信息
        history_info = history_context if history_context else "（无历史日程参考）"

        # 处理衣柜信息
        wardrobe_info_text = wardrobe_info if wardrobe_info else "（无衣柜配置，请根据活动场景自行选择合适的穿搭）"

        # 构建核心任务
        core_task = CORE_TASK_TEMPLATE.format(
            date=target_date, weekday=weekday, persona_info=persona_info, history_info=history_info, wardrobe_info=wardrobe_info_text
        )

        # 构建用户追加部分
        user_appendix = ""
        if custom_prompt:
            user_appendix = USER_APPENDIX_TEMPLATE.format(custom_prompt=custom_prompt)

        # 合并所有部分
        parts = [system_core, core_task, ACTIVITY_TYPE_GUIDE]
        if calendar_context:
            parts.append(
                "【目标日期的日历事实｜北京时间】\n" + calendar_context +
                "\n按已确认的休息日或调休工作日安排作息，不能仅凭星期判断是否放假。"
                "\n节日、纪念日和节气用于参考；是否参加相关活动取决于人设和兴趣，"
                "不必为每个节日安排庆祝，也不要把普通纪念日当成放假。"
                "\n未确认的数据不得自行编造，保留普通生活安排。"
            )

        if user_appendix:
            parts.append(user_appendix)

        return "\n".join(parts)

    def build_retry_prompt(self, original_prompt: str, issues: list[str]) -> str:
        """
        构建重试 Prompt

        当生成的日程质量不达标时，使用此方法构建重试 Prompt，
        让 LLM 只修复问题部分，而不是完全重新生成。

        Args:
            original_prompt: 原始 Prompt
            issues: 问题列表（由质量评估器生成）

        Returns:
            str: 重试 Prompt

        示例：
            retry_prompt = builder.build_retry_prompt(
                original_prompt=prompt,
                issues=["活动数量不足", "时间有空档"]
            )
        """
        issues_text = "\n".join(f"- {issue}" for issue in issues)

        retry_section = RETRY_PROMPT_TEMPLATE.format(issues=issues_text)

        return original_prompt + "\n" + retry_section


# 模块级单例实例
_builder_instance: Optional[SchedulePromptBuilder] = None


def get_prompt_builder() -> SchedulePromptBuilder:
    """
    获取 Prompt 构建器单例实例

    Returns:
        SchedulePromptBuilder: 构建器实例
    """
    global _builder_instance
    if _builder_instance is None:
        _builder_instance = SchedulePromptBuilder()
    return _builder_instance


def build_schedule_prompt(
    persona_context: str,
    history_context: str,
    custom_prompt: str,
    target_date: str,
    nickname: str = "麦麦",
    personality: str = "是一个女大学生",
    calendar_context: str = "",
) -> str:
    """
    快捷函数：构建日程生成 Prompt

    Args:
        persona_context: 人设上下文
        history_context: 历史日程摘要
        custom_prompt: 用户自定义风格
        target_date: 目标日期
        nickname: Bot 名称
        personality: 人设描述
        calendar_context: 目标日期的已确认日历事实及获取状态

    Returns:
        str: 完整的 Prompt
    """
    return get_prompt_builder().build(
        persona_context=persona_context,
        history_context=history_context,
        custom_prompt=custom_prompt,
        target_date=target_date,
        nickname=nickname,
        personality=personality,
        calendar_context=calendar_context,
    )
