"""QQ 空间 Cookie 解析：适配器优先，其次 NapCat HTTP，最后本地缓存。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

import json
import logging
import os
import tempfile
import time
from urllib.parse import unquote

import httpx

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin

logger = logging.getLogger("plugin.painter_maimai_daily.qzone.cookies")

_ADAPTER_PREFIXES = ("adapter.snowluma.", "adapter.napcat.")
_REQUIRED_KEYS = ("p_skey", "skey")


def normalize_uin(value: Any) -> str:
    """只接受纯数字 QQ 号；官方机器人 OpenID 不能作为空间 uin。"""
    text = str(value or "").strip().lstrip("oO")
    return text if text.isascii() and text.isdecimal() else ""


def parse_cookie_string(text: str) -> Dict[str, str]:
    """把 ``k=v; k2=v2`` 解析成字典；重复键以后出现者为准。"""
    cookies: Dict[str, str] = {}
    for segment in str(text or "").split(";"):
        if "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        key = key.strip()
        if key:
            cookies[key] = unquote(value.strip().strip('"'))
    return cookies


def _lookup(cookies: Dict[str, str], name: str) -> str:
    """Cookie 名大小写不固定，按小写比较取值。"""
    for key, value in cookies.items():
        if key.lower() == name.lower():
            return str(value)
    return ""


def validate_cookies(cookies: Dict[str, str], expected_uin: str = "") -> Dict[str, str]:
    """校验必需字段与账号归属；不匹配时直接抛错，不把别人的 Cookie 发出去。"""
    if not isinstance(cookies, dict):
        raise ValueError("Cookie 必须为字典")
    normalized = {str(key): str(value) for key, value in cookies.items()}
    p_skey = _lookup(normalized, "p_skey")
    skey = _lookup(normalized, "skey")
    if not p_skey or not skey:
        raise ValueError("Cookie 缺少 p_skey 或 skey")
    owners = {
        normalize_uin(_lookup(normalized, key).lstrip("oO"))
        for key in ("uin", "p_uin")
        if _lookup(normalized, key)
    }
    owners.discard("")
    if expected_uin:
        if owners and owners != {expected_uin}:
            raise ValueError("Cookie 账号与目标 QQ 不一致")
        normalized["uin"] = expected_uin
    else:
        if not owners:
            raise ValueError("Cookie 缺少 uin/p_uin")
        normalized["uin"] = next(iter(owners))
    normalized["p_skey"] = p_skey
    normalized["skey"] = skey
    return normalized


def _onebot_data(result: Any) -> Dict[str, Any]:
    """解包适配器响应；SDK 已去掉 Host 外层，这里只处理 OneBot 内层。"""
    if not isinstance(result, dict):
        raise RuntimeError("适配器未返回响应对象")
    if result.get("status") == "ok" or "retcode" in result:
        retcode = int(result.get("retcode", 0) or 0)
        if result.get("status") != "ok" or retcode != 0:
            raise RuntimeError(str(result.get("wording") or result.get("message") or "适配器动作失败"))
        data = result.get("data")
        if isinstance(data, dict):
            return data
    data = result.get("data")
    return data if isinstance(data, dict) else result


def _extract_cookie_string(result: Any) -> str:
    """从适配器或 HTTP 返回值里提取 Cookie 字符串。"""
    if isinstance(result, str) and result.strip():
        return result.strip()
    data = _onebot_data(result)
    for key in ("cookies", "cookie"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise RuntimeError("适配器未返回 Cookie 字符串")


async def resolve_bot_uin(plugin: "PainterMaimaiPlugin") -> str:
    """遍历适配器登录信息，兜底读取 ``bot.qq_account``。"""
    try:
        entries = await plugin.ctx.api.list()
    except Exception:
        logger.exception("查询适配器 API 列表失败，尝试使用 bot.qq_account")
        entries = []
    if not isinstance(entries, list):
        raise RuntimeError("api.list 返回值必须为列表")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("enabled") is False or entry.get("public") is False:
            continue
        if not str(entry.get("name") or "").endswith("system.get_login_info"):
            continue
        full_name = str(entry.get("full_name") or "")
        if not full_name:
            continue
        try:
            result = await plugin.ctx.api.call(full_name, version=str(entry.get("version") or "1"))
            uin = normalize_uin(_onebot_data(result).get("user_id"))
            if uin:
                return uin
        except Exception:
            logger.exception("读取适配器登录账号失败: %s", full_name)
    fallback = normalize_uin(await plugin.ctx.config.get("bot.qq_account", ""))
    return fallback


async def _adapter_candidates(plugin: "PainterMaimaiPlugin") -> list[Dict[str, str]]:
    """筛选实际注册的 adapter get_cookies API，并按用户偏好排序。"""
    entries = await plugin.ctx.api.list()
    if not isinstance(entries, list):
        raise RuntimeError("api.list 返回值必须为列表")
    candidates: list[Dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("enabled") is False or entry.get("public") is False:
            continue
        name = str(entry.get("name") or "")
        if not name.startswith(_ADAPTER_PREFIXES) or not name.endswith("account.get_cookies"):
            continue
        candidates.append({
            "plugin_id": str(entry.get("plugin_id") or ""),
            "full_name": str(entry.get("full_name") or ""),
            "version": str(entry.get("version") or "1"),
        })

    priority = [str(item).strip() for item in plugin.config.qzone.adapter_priority if str(item).strip()]

    def rank(candidate: Dict[str, str]) -> int:
        plugin_id = candidate["plugin_id"]
        for index, preferred in enumerate(priority):
            if plugin_id == preferred or plugin_id.endswith(f".{preferred}"):
                return index
        return len(priority)

    return sorted(candidates, key=rank)


async def _fetch_by_adapter(plugin: "PainterMaimaiPlugin", uin: str) -> Dict[str, str]:
    """按候选顺序调用适配器；全部失败时抛最后一个错误。"""
    errors: list[str] = []
    for candidate in await _adapter_candidates(plugin):
        try:
            result = await plugin.ctx.api.call(
                candidate["full_name"],
                version=candidate["version"],
                domain="qzone.qq.com",
            )
            cookies = validate_cookies(parse_cookie_string(_extract_cookie_string(result)), uin)
            logger.info("已通过适配器获取空间 Cookie: %s", candidate["plugin_id"])
            return cookies
        except Exception as exc:
            errors.append(f"{candidate['plugin_id']}: {exc}")
            logger.warning("适配器 %s 获取空间 Cookie 失败: %s", candidate["plugin_id"], exc)
    raise RuntimeError("；".join(errors) or "没有可用的 QQ 适配器")


async def _fetch_by_napcat(plugin: "PainterMaimaiPlugin", uin: str) -> Dict[str, str]:
    """直连 NapCat / SnowLuma HTTP 服务；Token 为空时不发 Authorization。"""
    cfg = plugin.config.qzone
    url = f"http://{cfg.http_host}:{cfg.http_port}/get_cookies"
    headers = {"Content-Type": "application/json"}
    if cfg.http_token:
        headers["Authorization"] = f"Bearer {cfg.http_token}"
    async with httpx.AsyncClient(timeout=cfg.publish_timeout_seconds) as client:
        response = await client.post(url, json={}, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"NapCat 取 Cookie 返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("NapCat 取 Cookie 响应不是 JSON") from exc
    return validate_cookies(parse_cookie_string(_extract_cookie_string(payload)), uin)


def cookie_file(plugin: "PainterMaimaiPlugin", uin: str) -> Path:
    """本地缓存路径固定为 ``data_dir/qzone_cookies-<uin>.json``。"""
    return Path(plugin.ctx.paths.data_dir) / f"qzone_cookies-{uin}.json"


def read_local_cookies(plugin: "PainterMaimaiPlugin", uin: str) -> Optional[tuple[Dict[str, str], float]]:
    """读取本地缓存；兼容直接存字符串和存字典两种格式。"""
    path = cookie_file(plugin, uin)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        logger.warning("读取空间 Cookie 缓存失败 %s: %s", path, exc)
        return None
    saved_at = float(payload.get("saved_at", 0) or 0)
    raw = payload.get("cookies", payload.get("cookie_string", payload))
    if isinstance(raw, dict):
        cookies = {str(key): str(value) for key, value in raw.items()}
    elif isinstance(raw, str):
        cookies = parse_cookie_string(raw)
    else:
        return None
    try:
        return validate_cookies(cookies, uin), saved_at
    except ValueError as exc:
        logger.warning("空间 Cookie 缓存无效 %s: %s", path, exc)
        return None


def save_cookies(plugin: "PainterMaimaiPlugin", uin: str, cookies: Dict[str, str]) -> None:
    """原子写入缓存；写盘失败只记日志，不阻断发布。"""
    path = cookie_file(plugin, uin)
    payload = {
        "saved_at": time.time(),
        "uin": uin,
        "cookies": "; ".join(f"{key}={value}" for key, value in cookies.items()),
    }
    temporary: Optional[Path] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError:
        logger.exception("保存空间 Cookie 缓存失败: %s", path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def is_fresh(saved_at: float, ttl_hours: int) -> bool:
    """缓存是否仍在配置的有效期内。"""
    return saved_at > 0 and time.time() - saved_at < ttl_hours * 3600


async def fetch_cookies(
    plugin: "PainterMaimaiPlugin", uin: str, *, force_refresh: bool = False,
) -> Dict[str, str]:
    """按配置顺序获取 Cookie；刷新失败时允许退回未过期前的本地缓存。"""
    if not uin:
        raise RuntimeError("无法确定机器人 QQ 号")
    cached = read_local_cookies(plugin, uin)
    if cached is not None and not force_refresh and is_fresh(cached[1], plugin.config.qzone.cookie_ttl_hours):
        return cached[0]

    errors: list[str] = []
    for method in plugin.config.qzone.cookie_methods:
        try:
            if method == "adapter":
                cookies = await _fetch_by_adapter(plugin, uin)
            elif method == "napcat":
                cookies = await _fetch_by_napcat(plugin, uin)
            elif method == "local":
                if cached is None:
                    raise RuntimeError("本地 Cookie 文件不存在")
                cookies = cached[0]
            else:
                raise ValueError(f"未知 Cookie 获取方式: {method}")
            save_cookies(plugin, uin, cookies)
            return cookies
        except Exception as exc:
            errors.append(f"{method}: {exc}")
            logger.warning("获取空间 Cookie 失败（%s）: %s", method, exc)

    if cached is not None:
        logger.warning("Cookie 刷新全部失败，继续使用旧缓存")
        return cached[0]
    raise RuntimeError("无法获取 QQ 空间 Cookie: " + "；".join(errors))
