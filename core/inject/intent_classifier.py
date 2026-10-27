"""基于规则的轻量意图分类，用于决定本轮是否值得注入日程。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import re


@dataclass(frozen=True)
class TimeRange:
    """用户提到的时段，用于筛选未来日程。"""

    name: str
    start_hour: int
    end_hour: int


class UserIntent(Enum):
    """注入决策关心的用户意图。"""

    QUERY_CURRENT = "query_current"
    QUERY_FUTURE = "query_future"
    CASUAL_CHAT = "casual_chat"
    TECH_QUESTION = "tech_question"
    COMMAND_EXECUTION = "command"
    UNKNOWN = "unknown"


class IntentClassifier:
    """关键词加权分类；单次调用只做字符串匹配，满足 Hook 的零 I/O 要求。"""

    def __init__(self) -> None:
        """预编译命令正则，并准备关键词与时段表。"""
        self.current_keywords = {
            "现在", "当前", "正在", "在做", "在干", "在忙", "这会儿", "此刻", "目前", "眼下",
            "做什么", "干什么", "忙什么", "在吗", "有空吗", "忙吗", "空闲吗",
            "刚", "刚才", "刚刚", "在", "在哪", "去哪",
        }
        self.activity_verbs = {
            "吃", "睡", "玩", "聊", "看", "学", "写", "做", "休息", "工作", "运动",
            "看剧", "追剧", "聊天", "打游戏", "学习", "写代码",
        }
        self.future_keywords = {
            "接下来", "等下", "稍后", "之后", "待会", "一会儿", "明天", "今晚", "晚上", "下午",
            "打算", "计划", "安排", "准备", "要做", "会做", "打算做", "然后", "后面", "接着",
        }
        self.tech_keywords = {
            "怎么", "如何", "为什么", "什么是", "配置", "安装", "设置", "调试",
            "错误", "报错", "异常", "bug", "代码", "程序", "脚本", "函数",
            "数据库", "服务器", "API", "接口", "版本", "更新", "升级", "兼容",
        }
        self.casual_keywords = {
            "你好", "hi", "hello", "嗨", "早", "晚安", "再见", "拜拜",
            "哈哈", "呵呵", "嘿嘿", "好的", "ok", "嗯", "嗯嗯", "谢谢", "多谢", "感谢",
        }
        self.casual_keywords = {word.lower() for word in self.casual_keywords}
        self.command_regex = re.compile(
            r"^(?:/\w|sudo\s+|git\s+|npm\s+|python\s+|cd\s+|ls\s+)", re.IGNORECASE
        )
        self.time_ranges = {
            "凌晨": TimeRange("凌晨", 0, 6),
            "早上": TimeRange("早上", 6, 9),
            "上午": TimeRange("上午", 9, 12),
            "中午": TimeRange("中午", 11, 14),
            "下午": TimeRange("下午", 14, 18),
            "傍晚": TimeRange("傍晚", 17, 19),
            "晚上": TimeRange("晚上", 18, 23),
            "深夜": TimeRange("深夜", 22, 24),
        }

    def classify(self, message: str) -> tuple[UserIntent, float]:
        """按命令、技术、当前、未来、闲聊的优先级返回意图和置信度。"""
        text = (message or "").strip().lower()
        if not text:
            return UserIntent.UNKNOWN, 0.0
        if self.command_regex.match(text):
            return UserIntent.COMMAND_EXECUTION, 1.0

        tech_score = self._score(text, self.tech_keywords)
        if tech_score > 0.5:
            return UserIntent.TECH_QUESTION, tech_score

        current_score = self._score(text, self.current_keywords)
        if any(word in text for word in ("正在", "在做", "在干", "现在", "当前")):
            current_score = min(1.0, current_score * 1.5)
        if ("不是" in text and any(mark in text for mark in ("吗", "?", "？"))
                and any(verb in text for verb in self.activity_verbs)):
            current_score = max(current_score, 0.85)
        if any(verb in text for verb in self.activity_verbs) and any(
            word in text for word in ("刚", "刚才", "刚刚", "在")
        ):
            current_score = max(current_score, 0.80)
        if current_score > 0.4:
            return UserIntent.QUERY_CURRENT, current_score

        future_score = self._score(text, self.future_keywords)
        if any(word in text for word in ("接下来", "等下", "计划", "安排", "打算")):
            future_score = min(1.0, future_score * 1.5)
        if future_score > 0.4:
            return UserIntent.QUERY_FUTURE, future_score

        casual_score = self._score(text, self.casual_keywords)
        if casual_score > 0.3:
            return UserIntent.CASUAL_CHAT, casual_score
        if len(text) < 10 and any(mark in text for mark in ("?", "？")):
            return UserIntent.QUERY_CURRENT, 0.6
        return UserIntent.CASUAL_CHAT, 0.40

    @staticmethod
    def _score(message: str, keywords: set[str]) -> float:
        """按命中数量与关键词长度加权，避免单个短词就拿到高置信度。"""
        matched = [keyword for keyword in keywords if keyword in message]
        if not matched:
            return 0.0
        base = min(1.0, len(matched) / 3.0)
        bonus = min(0.5, sum(len(keyword) / 5.0 for keyword in matched) / 5.0)
        return min(1.0, base + bonus)

    def extract_time_range(self, message: str) -> Optional[TimeRange]:
        """识别“下午/晚上”等时段；未提到则返回 ``None``。"""
        text = (message or "").lower()
        for keyword, time_range in self.time_ranges.items():
            if keyword in text:
                return time_range
        return None
