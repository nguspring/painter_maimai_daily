"""按真实聊天流保存会话设置，插件实例之间不共享运行状态。

只持久化插件开关、模型与撤回开关、默认模型和自拍配置的会话覆盖项。
没有覆盖项的聊天流直接读取当前全局配置，不创建状态，也不需要过期清理。
"""

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Optional, Set

import asyncio
import json
import logging
import os
import tempfile


logger = logging.getLogger("plugin.painter_maimai_daily.state")

_SCHEMA_VERSION = 1
_VALID_SELFIE_STYLES = frozenset({"auto", "standard", "mirror", "photo"})
_STATE_FIELDS = frozenset({
    "plugin_enabled", "disabled_models", "recall_disabled_models",
    "action_default_model", "command_default_model", "selfie_schedule_enabled", "selfie_style",
})


class StatePersistenceError(RuntimeError):
    """会话设置未就绪、文件损坏或读写失败，调用方不得宣称设置成功。"""


@dataclass(frozen=True)
class ChatStreamState:
    """单个真实 session_id 的配置覆盖；None 表示跟随当前全局配置。"""

    plugin_enabled: Optional[bool] = None
    disabled_models: FrozenSet[str] = field(default_factory=frozenset)
    recall_disabled_models: FrozenSet[str] = field(default_factory=frozenset)
    action_default_model: Optional[str] = None
    command_default_model: Optional[str] = None
    selfie_schedule_enabled: Optional[bool] = None
    selfie_style: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """返回可序列化副本，调用方不能借此修改管理器内部状态。"""
        return {
            "plugin_enabled": self.plugin_enabled,
            "disabled_models": sorted(self.disabled_models),
            "recall_disabled_models": sorted(self.recall_disabled_models),
            "action_default_model": self.action_default_model,
            "command_default_model": self.command_default_model,
            "selfie_schedule_enabled": self.selfie_schedule_enabled,
            "selfie_style": self.selfie_style,
        }


def _validate_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field_name} 必须是非空且不含首尾空白的字符串")


def _decode_state(data: Any) -> ChatStreamState:
    """严格校验落盘数据；类型错误和未知字段不能被默默当成全局默认值。"""
    if not isinstance(data, dict) or set(data) != _STATE_FIELDS:
        raise ValueError("会话覆盖项字段不完整或包含未知字段")
    for name in ("plugin_enabled", "selfie_schedule_enabled"):
        if data[name] is not None and not isinstance(data[name], bool):
            raise ValueError(f"{name} 必须是布尔值或 null")
    for name in ("action_default_model", "command_default_model", "selfie_style"):
        if data[name] is not None:
            _validate_identifier(data[name], name)
    if data["selfie_style"] is not None and data["selfie_style"] not in _VALID_SELFIE_STYLES:
        raise ValueError(f"未知自拍风格：{data['selfie_style']}")
    for name in ("disabled_models", "recall_disabled_models"):
        if not isinstance(data[name], list):
            raise ValueError(f"{name} 必须是模型 ID 列表")
        for model_id in data[name]:
            _validate_identifier(model_id, name)
        if len(set(data[name])) != len(data[name]):
            raise ValueError(f"{name} 中存在重复模型 ID")
    return ChatStreamState(
        plugin_enabled=data["plugin_enabled"],
        disabled_models=frozenset(data["disabled_models"]),
        recall_disabled_models=frozenset(data["recall_disabled_models"]),
        action_default_model=data["action_default_model"],
        command_default_model=data["command_default_model"],
        selfie_schedule_enabled=data["selfie_schedule_enabled"],
        selfie_style=data["selfie_style"],
    )


class RuntimeStateManager:
    """一个插件实例拥有一个管理器，异步落盘完成后才发布新的内存快照。

    调用 load() 前不能读写。热更新沿用当前实例，全局配置始终由读取方传入；
    所有会话 ID 必须来自宿主已有聊天流，不在这里计算、补造或转换 ID。
    """

    def __init__(self) -> None:
        self._states: Dict[str, ChatStreamState] = {}
        self._storage_path: Optional[Path] = None
        self._write_lock = asyncio.Lock()

    async def load(self, storage_path: Path) -> None:
        """在插件入口和后台任务启动前加载；首次运行允许文件不存在。"""
        path = Path(storage_path)
        if not path.is_absolute():
            raise ValueError("会话设置文件必须使用插件数据目录的绝对路径")
        async with self._write_lock:
            if self._storage_path is not None:
                if path != self._storage_path:
                    raise StatePersistenceError("会话设置已经加载，不能切换到其他文件")
                return
            states = await asyncio.to_thread(self._read_snapshot, path)
            self._states = states
            self._storage_path = path

    async def close(self) -> None:
        """等待正在提交的写入结束；每次修改都已落盘，无需退出时补写。"""
        async with self._write_lock:
            self._storage_path = None
            self._states = {}

    @staticmethod
    def _read_snapshot(path: Path) -> Dict[str, ChatStreamState]:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as exc:
            raise StatePersistenceError(f"读取会话设置失败：{path}：{exc}") from exc
        try:
            document = json.loads(raw)
            if not isinstance(document, dict) or set(document) != {"version", "sessions"}:
                raise ValueError("文件必须包含 version 和 sessions 字段")
            if type(document["version"]) is not int or document["version"] != _SCHEMA_VERSION:
                raise ValueError(f"不支持的会话设置版本：{document['version']}")
            if not isinstance(document["sessions"], dict):
                raise ValueError("sessions 必须是按真实 session_id 索引的对象")
            states: Dict[str, ChatStreamState] = {}
            for session_id, data in document["sessions"].items():
                _validate_identifier(session_id, "session_id")
                state = _decode_state(data)
                if RuntimeStateManager._has_custom_settings(state):
                    states[session_id] = state
            return states
        except (TypeError, ValueError) as exc:
            raise StatePersistenceError(f"会话设置文件损坏或格式不受支持：{path}：{exc}") from exc

    @staticmethod
    def _write_snapshot(path: Path, states: Dict[str, ChatStreamState]) -> None:
        """同目录临时文件完整写入并 fsync 后原子替换，失败不截断原文件。"""
        temporary_path: Optional[Path] = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(
                    {"version": _SCHEMA_VERSION, "sessions": {
                        session_id: state.to_dict() for session_id, state in states.items()
                    }},
                    temporary, ensure_ascii=False, indent=2, sort_keys=True,
                )
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError as exc:
            raise StatePersistenceError(f"保存会话设置失败：{path}：{exc}") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    logger.exception("清理未提交的会话设置临时文件失败：%s", temporary_path)

    def _require_loaded(self) -> Path:
        if self._storage_path is None:
            raise StatePersistenceError("会话设置尚未加载或插件已经卸载")
        return self._storage_path

    def _get_state(self, chat_id: str) -> ChatStreamState:
        self._require_loaded()
        _validate_identifier(chat_id, "真实 session_id")
        return self._states.get(chat_id, ChatStreamState())

    @staticmethod
    def _has_custom_settings(state: ChatStreamState) -> bool:
        return bool(
            state.plugin_enabled is not None
            or state.disabled_models
            or state.recall_disabled_models
            or state.action_default_model is not None
            or state.command_default_model is not None
            or state.selfie_schedule_enabled is not None
            or state.selfie_style is not None
        )

    async def _change_state(
        self, chat_id: str, change: Callable[[ChatStreamState], ChatStreamState],
    ) -> None:
        async with self._write_lock:
            path = self._require_loaded()
            current = self._get_state(chat_id)
            updated = change(current)
            if updated == current:
                return
            states = self._states.copy()
            if self._has_custom_settings(updated):
                states[chat_id] = updated
            else:
                states.pop(chat_id, None)

            # to_thread 无法停止已经开始的磁盘写入。取消请求时仍须等提交完成，
            # 再更新内存并传播取消，避免下一次写入覆盖已落盘但内存未同步的设置。
            write_task = asyncio.create_task(asyncio.to_thread(self._write_snapshot, path, states))
            cancelled = False
            while not write_task.done():
                try:
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    cancelled = True
            write_task.result()
            self._states = states
            if cancelled:
                raise asyncio.CancelledError

    # ==================== 插件开关 ====================

    def is_plugin_enabled(self, chat_id: str, global_enabled: bool) -> bool:
        state = self._get_state(chat_id)
        return global_enabled if state.plugin_enabled is None else state.plugin_enabled

    async def set_plugin_enabled(self, chat_id: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("插件开关必须是布尔值")
        await self._change_state(chat_id, lambda state: replace(state, plugin_enabled=enabled))
        logger.info("聊天流 %s 插件状态已保存为：%s", chat_id, enabled)

    async def reset_plugin_enabled(self, chat_id: str) -> None:
        await self._change_state(chat_id, lambda state: replace(state, plugin_enabled=None))
        logger.info("聊天流 %s 插件状态已重置为全局配置", chat_id)

    # ==================== 模型开关 ====================

    def is_model_enabled(self, chat_id: str, model_id: str) -> bool:
        return model_id not in self._get_state(chat_id).disabled_models

    async def set_model_enabled(self, chat_id: str, model_id: str, enabled: bool) -> None:
        _validate_identifier(model_id, "模型 ID")
        if not isinstance(enabled, bool):
            raise ValueError("模型开关必须是布尔值")
        await self._change_state(chat_id, lambda state: replace(
            state, disabled_models=(
                state.disabled_models - {model_id} if enabled else state.disabled_models | {model_id}
            ),
        ))
        logger.info("聊天流 %s 模型 %s 开关已保存为：%s", chat_id, model_id, enabled)

    def get_disabled_models(self, chat_id: str) -> Set[str]:
        return set(self._get_state(chat_id).disabled_models)

    # ==================== 撤回开关 ====================

    def is_recall_enabled(self, chat_id: str, model_id: str, global_enabled: bool) -> bool:
        state = self._get_state(chat_id)
        return global_enabled and model_id not in state.recall_disabled_models

    async def set_recall_enabled(self, chat_id: str, model_id: str, enabled: bool) -> None:
        _validate_identifier(model_id, "模型 ID")
        if not isinstance(enabled, bool):
            raise ValueError("撤回开关必须是布尔值")
        await self._change_state(chat_id, lambda state: replace(
            state, recall_disabled_models=(
                state.recall_disabled_models - {model_id} if enabled else state.recall_disabled_models | {model_id}
            ),
        ))
        logger.info("聊天流 %s 模型 %s 撤回开关已保存为：%s", chat_id, model_id, enabled)

    def get_recall_disabled_models(self, chat_id: str) -> Set[str]:
        return set(self._get_state(chat_id).recall_disabled_models)

    # ==================== 默认模型 ====================

    def get_action_default_model(self, chat_id: str, global_default: str) -> str:
        state = self._get_state(chat_id)
        return global_default if state.action_default_model is None else state.action_default_model

    async def set_action_default_model(self, chat_id: str, model_id: str) -> None:
        _validate_identifier(model_id, "模型 ID")
        await self._change_state(chat_id, lambda state: replace(state, action_default_model=model_id))
        logger.info("聊天流 %s 智能生图默认模型已保存为：%s", chat_id, model_id)

    async def reset_action_default_model(self, chat_id: str) -> None:
        await self._change_state(chat_id, lambda state: replace(state, action_default_model=None))
        logger.info("聊天流 %s 智能生图默认模型已重置为全局配置", chat_id)

    def get_command_default_model(self, chat_id: str, global_default: str) -> str:
        state = self._get_state(chat_id)
        return global_default if state.command_default_model is None else state.command_default_model

    async def set_command_default_model(self, chat_id: str, model_id: str) -> None:
        _validate_identifier(model_id, "模型 ID")
        await self._change_state(chat_id, lambda state: replace(state, command_default_model=model_id))
        logger.info("聊天流 %s 命令默认模型已保存为：%s", chat_id, model_id)

    async def reset_command_default_model(self, chat_id: str) -> None:
        await self._change_state(chat_id, lambda state: replace(state, command_default_model=None))
        logger.info("聊天流 %s 命令默认模型已重置为全局配置", chat_id)

    # ==================== 自拍日程开关 ====================

    def is_selfie_schedule_enabled(self, chat_id: str, global_enabled: bool) -> bool:
        state = self._get_state(chat_id)
        return global_enabled if state.selfie_schedule_enabled is None else state.selfie_schedule_enabled

    async def set_selfie_schedule_enabled(self, chat_id: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("自拍日程开关必须是布尔值")
        await self._change_state(chat_id, lambda state: replace(state, selfie_schedule_enabled=enabled))
        logger.info("聊天流 %s 自拍日程增强已保存为：%s", chat_id, enabled)

    async def reset_selfie_schedule_enabled(self, chat_id: str) -> None:
        await self._change_state(chat_id, lambda state: replace(state, selfie_schedule_enabled=None))
        logger.info("聊天流 %s 自拍日程增强已重置为全局配置", chat_id)

    # ==================== 自拍风格 ====================

    def get_selfie_style(self, chat_id: str, global_default: Optional[str] = None) -> Optional[str]:
        state = self._get_state(chat_id)
        return global_default if state.selfie_style is None else state.selfie_style

    async def set_selfie_style(self, chat_id: str, style: str) -> None:
        if style not in _VALID_SELFIE_STYLES:
            raise ValueError(f"未知自拍风格：{style}")
        await self._change_state(chat_id, lambda state: replace(state, selfie_style=style))
        logger.info("聊天流 %s 自拍风格已保存为：%s", chat_id, style)

    async def reset_selfie_style(self, chat_id: str) -> None:
        await self._change_state(chat_id, lambda state: replace(state, selfie_style=None))
        logger.info("聊天流 %s 自拍风格已重置为全局配置", chat_id)

    # ==================== 状态重置 ====================

    async def reset_chat_state(self, chat_id: str) -> None:
        await self._change_state(chat_id, lambda state: ChatStreamState())
        logger.info("聊天流 %s 的会话覆盖项已全部清除", chat_id)

    def get_chat_state_summary(self, chat_id: str) -> Dict[str, Any]:
        return self._get_state(chat_id).to_dict()
