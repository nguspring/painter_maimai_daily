"""主动图片的共享冷却、消息游标与发布记录，写入 SDK 数据目录。

两个后台任务在同一把锁中预占目标；提交失败时不得生成或发送。
每次写入在线程中原子替换，取消也等待提交完成，重启不重放已消费的语境。
"""

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
import time
import uuid

from .runtime_state import StatePersistenceError


T = TypeVar("T")
_HISTORY_LIMIT = 80
_RECENT_SECONDS = 24 * 3600
_OUTCOMES = {"reserved", "skipped", "failed", "unknown", "sent"}
_RECORD_FIELDS = {
    "token", "source", "target", "created_at", "activity_key", "activity", "location",
    "topic", "prompt_key", "image_hash", "is_selfie", "selfie_style", "outfit", "reason", "status",
}


def _empty_snapshot() -> Dict[str, Any]:
    return {"version": 1, "checks": {}, "sources": {}, "targets": {}, "history": []}


def _fingerprint(text: str) -> str:
    normalized = re.sub(r"[\W_]+", "", text.casefold())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ProactiveStateStore:
    """实例级原子预占；群目标使用宿主解析的 session_id。"""

    def __init__(self) -> None:
        self._data = _empty_snapshot()
        self._storage_path: Optional[Path] = None
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._storage_path is not None

    def _require_loaded(self) -> Path:
        if self._storage_path is None:
            raise StatePersistenceError("主动图片记录尚未加载或插件已经卸载")
        return self._storage_path

    async def load(self, path: Path) -> None:
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("主动图片记录必须使用 SDK 数据目录的绝对路径")
        async with self._lock:
            if self._storage_path is not None:
                if path != self._storage_path:
                    raise StatePersistenceError("主动图片记录已加载，不能更换文件")
                return
            self._data = await asyncio.to_thread(self._read, path)
            self._storage_path = path

    async def close(self) -> None:
        async with self._lock:
            self._storage_path = None
            self._data = _empty_snapshot()

    @staticmethod
    def _read(path: Path) -> Dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _empty_snapshot()
        except (OSError, UnicodeError, ValueError) as exc:
            raise StatePersistenceError(f"读取主动图片记录失败：{path}：{exc}") from exc
        try:
            if not isinstance(data, dict) or set(data) != set(_empty_snapshot()) or type(data["version"]) is not int or data["version"] != 1:
                raise ValueError("主动图片记录格式或版本不受支持")
            for key in ("checks", "sources", "targets"):
                if not isinstance(data[key], dict):
                    raise ValueError(f"{key} 必须是对象")
                for identifier, value in data[key].items():
                    if not identifier or identifier.strip() != identifier:
                        raise ValueError("记录标识不能为空或包含首尾空白")
                    timestamp = value["last_attempt"] if key == "targets" else value
                    if type(timestamp) not in (int, float) or not 0 <= timestamp < float("inf"):
                        raise ValueError("冷却时间无效")
                    if key == "targets":
                        if set(value) != {"last_attempt", "consumed"} or not isinstance(value["consumed"], list):
                            raise ValueError("消息游标格式无效")
                        if any(not isinstance(item, str) or not item for item in value["consumed"]):
                            raise ValueError("已消费消息必须具有稳定 ID")
            if not isinstance(data["history"], list) or len(data["history"]) > _HISTORY_LIMIT:
                raise ValueError("发布历史格式无效")
            for record in data["history"]:
                if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
                    raise ValueError("发布记录字段不完整")
                if type(record["is_selfie"]) is not bool or type(record["created_at"]) not in (int, float) or not 0 <= record["created_at"] < float("inf"):
                    raise ValueError("发布记录类型无效")
                if any(not isinstance(record[key], str) for key in _RECORD_FIELDS - {"is_selfie", "created_at"}):
                    raise ValueError("发布记录文本字段无效")
                if record["status"] not in _OUTCOMES:
                    raise ValueError("未知发布状态")
                # 上次进程未完成的预占可能已经投递；保留去重记录，不恢复发送。
                if record["status"] == "reserved":
                    record["status"] = "unknown"
            return data
        except (KeyError, TypeError, ValueError) as exc:
            raise StatePersistenceError(f"主动图片记录损坏：{path}：{exc}") from exc

    @staticmethod
    def _write(path: Path, data: Dict[str, Any]) -> None:
        temporary_path: Optional[Path] = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
                temporary_path = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError as exc:
            raise StatePersistenceError(f"保存主动图片记录失败：{path}：{exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def _change(self, mutate: Callable[[Dict[str, Any]], T]) -> T:
        async with self._lock:
            path = self._require_loaded()
            updated = deepcopy(self._data)
            result = mutate(updated)
            if updated != self._data:
                write = asyncio.create_task(asyncio.to_thread(self._write, path, updated))
                cancelled = False
                while not write.done():
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        cancelled = True
                write.result()
                self._data = updated
                if cancelled:
                    raise asyncio.CancelledError
            return result

    async def claim_check(self, source: str, interval_seconds: float, *, now: Optional[float] = None) -> bool:
        moment = time.time() if now is None else now

        def change(data: Dict[str, Any]) -> bool:
            if source in data["checks"] and moment - data["checks"][source] < interval_seconds:
                return False
            data["checks"][source] = moment
            return True

        return await self._change(change)

    async def claim_group_check(
        self, target: str, messages: List[Dict[str, str]], min_messages: int,
        interval_seconds: float,
    ) -> bool:
        """仅预占判断并消费语境，不更新随机图与自拍共享的发布冷却。

        沿用 version=1 的 checks/targets 字段，已有记录无需迁移。
        模型判断失败或不发图，也不会反复判断同一批群消息。
        """
        def change(data: Dict[str, Any]) -> bool:
            now = time.time()
            source = f"group_image:{target}"
            if source in data["checks"] and now - data["checks"][source] < interval_seconds:
                return False
            previous = data["targets"].get(target, {"last_attempt": 0, "consumed": []})
            fresh = [item["message_id"] for item in messages if item["message_id"] not in previous["consumed"]]
            if len(fresh) < min_messages:
                return False
            data["checks"][source] = now
            data["targets"][target] = {
                "last_attempt": previous["last_attempt"],
                "consumed": list(dict.fromkeys(previous["consumed"] + fresh))[-400:],
            }
            return True

        return await self._change(change)

    def available(self, target: str, cooldown_seconds: float, *, source: str = "", source_cooldown: float = 0, now: Optional[float] = None) -> bool:
        self._require_loaded()
        moment = time.time() if now is None else now
        previous = self._data["targets"].get(target)
        return (
            not any(record["target"] == target and record["status"] == "reserved" for record in self._data["history"])
            and (previous is None or previous["last_attempt"] == 0 or moment - previous["last_attempt"] >= cooldown_seconds)
            and (not source or source not in self._data["sources"] or moment - self._data["sources"][source] >= source_cooldown)
        )

    def unconsumed(self, target: str, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        self._require_loaded()
        consumed = set(self._data["targets"].get(target, {}).get("consumed", []))
        return [item for item in messages if item["message_id"] not in consumed]

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        self._require_loaded()
        return deepcopy([record for record in self._data["history"] if record["created_at"] >= time.time() - _RECENT_SECONDS][-limit:])

    def recent_styles(self) -> List[str]:
        return [record["selfie_style"] for record in self.recent() if record["selfie_style"] and record["status"] in {"sent", "unknown"}][-6:]

    @staticmethod
    def _duplicate(data: Dict[str, Any], token: str, prompt: str, is_selfie: bool, activity_key: str) -> bool:
        return any(
            record["token"] != token and record["created_at"] >= time.time() - _RECENT_SECONDS
            and record["status"] in {"reserved", "sent", "unknown"}
            and ((prompt and record["prompt_key"] == _fingerprint(prompt)) or (is_selfie and activity_key and record["is_selfie"] and record["activity_key"] == activity_key))
            for record in data["history"]
        )

    def batch_duplicate(self, prompt: str = "", *, is_selfie: bool = False, activity_key: str = "") -> bool:
        """只读判重：多目标同批发布前检查一次，避免第一条预占后误伤后续目标。"""
        self._require_loaded()
        return self._duplicate(self._data, "", prompt, is_selfie, activity_key)

    async def reserve(self, *, source: str, target: str, cooldown_seconds: float, activity_key: str = "", activity: str = "", location: str = "", prompt: str = "", reason: str = "", is_selfie: bool = False, messages: Optional[List[Dict[str, str]]] = None, min_messages: int = 0, source_cooldown: float = 0, dedupe: bool = True) -> Optional[str]:
        """预占单个目标；同一批的后续目标可传 ``dedupe=False``（D13）。"""
        token = uuid.uuid4().hex

        def change(data: Dict[str, Any]) -> Optional[str]:
            if not self.available(target, cooldown_seconds, source=source, source_cooldown=source_cooldown):
                return None
            consumed = data["targets"].get(target, {}).get("consumed", [])
            fresh = [item["message_id"] for item in (messages or []) if item["message_id"] not in consumed]
            if len(fresh) < min_messages:
                return None
            if dedupe and self._duplicate(data, token, prompt, is_selfie, activity_key):
                return None
            now = time.time()
            data["targets"][target] = {"last_attempt": now, "consumed": list(dict.fromkeys(consumed + fresh))[-400:]}
            data["sources"][source] = now
            data["history"].append({
                "token": token, "source": source, "target": target, "created_at": now,
                "activity_key": activity_key, "activity": activity[:500], "location": location[:200],
                "topic": prompt[:500], "prompt_key": _fingerprint(prompt) if prompt else "", "image_hash": "",
                "is_selfie": is_selfie, "selfie_style": "", "outfit": "", "reason": reason[:200], "status": "reserved",
            })
            active = [record for record in data["history"] if record["status"] == "reserved"]
            completed = [record for record in data["history"] if record["status"] != "reserved"]
            data["history"] = sorted(completed[-max(0, _HISTORY_LIMIT - len(active)):] + active, key=lambda record: record["created_at"])
            return token

        try:
            return await self._change(change)
        except asyncio.CancelledError:
            if any(record["token"] == token for record in self._data["history"]):
                await self.finish(token, "failed", "预占期间取消，本轮未发送")
            raise

    async def set_plan(self, token: str, prompt: str, *, is_selfie: bool, activity_key: str) -> bool:
        def change(data: Dict[str, Any]) -> bool:
            if self._duplicate(data, token, prompt, is_selfie, activity_key):
                return False
            record = next(record for record in data["history"] if record["token"] == token)
            record.update(topic=prompt[:500], prompt_key=_fingerprint(prompt), is_selfie=is_selfie, activity_key=activity_key)
            return True

        return await self._change(change)

    async def remember_image(self, token: str, image_data: str, *, selfie_style: str = "", outfit: str = "") -> bool:
        def digest() -> str:
            raw = base64.b64decode(image_data, validate=True)
            if not raw:
                raise ValueError("主动图片不能为空")
            return hashlib.sha256(raw).hexdigest()

        image_hash = await asyncio.to_thread(digest)

        def change(data: Dict[str, Any]) -> bool:
            if any(record["token"] != token and record["image_hash"] == image_hash and record["status"] in {"reserved", "unknown", "sent"} and record["created_at"] >= time.time() - _RECENT_SECONDS for record in data["history"]):
                return False
            record = next(record for record in data["history"] if record["token"] == token)
            record.update(image_hash=image_hash, selfie_style=selfie_style, outfit=outfit[:300])
            return True

        return await self._change(change)

    async def finish(self, token: str, status: str, reason: str = "", *, release_cooldown: bool = False) -> None:
        """结算一次预占。

        ``release_cooldown`` 只用于"本轮真的失败、值得尽快再试"的场景：预占时
        已经写入的渠道冷却会被清零，下一轮检查（受 check_interval 和失败退避
        控制）就能重试，而不是被 interval_minutes 的整段冷却压住。
        结算状态不是 failed 时该参数不生效，避免把已发出的记录重新放行。
        """
        if status not in _OUTCOMES - {"reserved"}:
            raise ValueError("主动图片结算状态无效")

        def change(data: Dict[str, Any]) -> None:
            record = next(record for record in data["history"] if record["token"] == token)
            record["status"] = status
            if reason:
                record["reason"] = reason[:200]
            if not release_cooldown or status != "failed":
                return
            # 只回滚属于本次预占的时间戳：并发或后续轮次可能已经写入更新的冷却。
            target_state = data["targets"].get(record["target"])
            if target_state is not None and target_state["last_attempt"] == record["created_at"]:
                target_state["last_attempt"] = 0.0
            if record["source"] and data["sources"].get(record["source"]) == record["created_at"]:
                data["sources"][record["source"]] = 0.0
        await self._change(change)
