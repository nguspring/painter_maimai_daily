"""QQ 空间图文说说协议：上传图片、计算 g_tk 与发布结果分级。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

import base64
import json
import re

import httpx

UPLOAD_IMAGE_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
PUBLISH_URL = (
    "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
)

_AUTH_CODES = {-3000, -10000, -4001, -4002}
_AUTH_HINTS = ("登录", "login", "expired", "重新登录")


@dataclass(frozen=True)
class PublishResult:
    """一次空间发布的结果；``uncertain`` 表示可能已发出，绝不能自动重发。"""

    status: Literal["sent", "rejected", "uncertain"]
    tid: str = ""
    error: str = ""


class QzoneAuthError(RuntimeError):
    """Cookie 失效或鉴权被拒；由 publisher 刷新一次后重试。"""


def parse_upload_result(payload: dict) -> tuple[str, str]:
    """从上传响应解析 ``(pic_bo, richval)``；字段缺失即视为协议失败。"""
    ret = payload.get("ret")
    if ret not in (0, "0", None):
        raise RuntimeError(f"上传空间图片失败: {payload}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("url"), str):
        raise RuntimeError("上传空间图片响应缺少 url")
    url = data["url"]
    if "&bo=" not in url:
        raise RuntimeError("上传空间图片响应缺少 bo 参数")
    pic_bo = url.split("&bo=", 1)[1].split("&", 1)[0]
    for key in ("albumid", "lloc", "type", "height", "width"):
        if key not in data:
            raise RuntimeError(f"上传空间图片响应缺少 {key}")
    # 与 QQ 空间网页端保持一致：第 2/3 段都填 lloc，且只保留一组横纵尺寸。
    richval = ",{},{},{},{},{},{},,{},{}".format(
        data["albumid"], data["lloc"], data["lloc"], data["type"],
        data["height"], data["width"], data["height"], data["width"],
    )
    return pic_bo, richval


class QzoneClient:
    """无状态客户端；每次发布使用独立 httpx 连接。"""

    def __init__(self, cookies: dict[str, str], uin: str, timeout_seconds: float = 30) -> None:
        """保存已校验的 Cookie 与目标 QQ 号。"""
        self.cookies = cookies
        self.uin = uin
        self.timeout = timeout_seconds
        self.gtk = self._gtk(cookies["p_skey"])

    @staticmethod
    def _gtk(p_skey: str) -> str:
        """QQ 空间约定的 djb2 变体哈希。"""
        value = 5381
        for char in p_skey:
            value += (value << 5) + ord(char)
        return str(value & 2147483647)

    def _headers(self) -> dict[str, str]:
        """补齐 Cookie/Referer/Origin；缺少 Cookie 时 QQ 会直接判定未登录。"""
        return {
            "Cookie": "; ".join(f"{key}={value}" for key, value in self.cookies.items()),
            "Referer": f"https://user.qzone.qq.com/{self.uin}",
            "Origin": "https://user.qzone.qq.com",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        }

    @staticmethod
    def _extract_json(text: str) -> dict:
        """从纯 JSON、夹带 HTML 的 JSON 或回调包装中提取对象。"""
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return payload
        except ValueError:
            pass
        matched = re.search(r"\{.*\}", text, re.DOTALL)
        if matched is not None:
            try:
                payload = json.loads(matched.group(0))
                if isinstance(payload, dict):
                    return payload
            except ValueError:
                pass
        raise ValueError("响应不是可解析的 JSON 对象")

    async def upload_image(self, image_bytes: bytes) -> tuple[str, str]:
        """上传单张图片，返回 ``(pic_bo, richval)``；失败直接抛错。"""
        if not image_bytes:
            raise ValueError("空间图片不能为空")
        form = {
            "filename": "filename",
            "zzpanelkey": "",
            "uploadtype": "1",
            "albumtype": "7",
            "exttype": "0",
            "skey": self.cookies.get("skey", ""),
            "zzpaneluin": self.uin,
            "p_uin": self.uin,
            "uin": self.uin,
            "p_skey": self.cookies.get("p_skey", ""),
            "output_type": "json",
            "qzonetoken": "",
            "refer": "shuoshuo",
            "charset": "utf-8",
            "output_charset": "utf-8",
            "upload_hd": "1",
            "hd_width": "2048",
            "hd_height": "10000",
            "hd_quality": "96",
            "backUrls": (
                "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
            ),
            "url": f"{UPLOAD_IMAGE_URL}?g_tk={self.gtk}",
            "base64": "1",
            "picfile": base64.b64encode(image_bytes).decode("ascii"),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(UPLOAD_IMAGE_URL, data=form, headers=self._headers())
        except httpx.RequestError as exc:
            raise RuntimeError(f"上传空间图片网络异常: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(f"上传空间图片返回 HTTP {response.status_code}")
        return parse_upload_result(self._extract_json(response.text))

    async     def publish(self, content: str, images: Sequence[bytes] = ()) -> PublishResult:
        """上传全部图片后发布说说，并按 code/tid 分级结果。"""
        if not content.strip():
            return PublishResult("rejected", error="说说正文不能为空")

        post_data = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": content,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": self.uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
        }

        if images:
            richvals: list[str] = []
            try:
                for image in images:
                    _, richval = await self.upload_image(image)
                    richvals.append(richval)
            except Exception as exc:
                # 上传阶段失败说明说说请求尚未发出，可以安全判定为拒绝。
                return PublishResult("rejected", error=f"图片上传失败: {exc}")
            post_data.update({
                "richtype": "1",
                "richval": "\t".join(richvals),
            })

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    PUBLISH_URL,
                    params={"g_tk": self.gtk, "uin": self.uin},
                    data=post_data,
                    headers=self._headers(),
                )
        except httpx.RequestError as exc:
            return PublishResult("uncertain", error=f"发布请求未确认完成: {exc}")
        if response.status_code != 200:
            return PublishResult("uncertain", error=f"空间发布返回 HTTP {response.status_code}")
        try:
            payload = self._extract_json(response.text)
        except ValueError as exc:
            return PublishResult("uncertain", error=f"空间发布响应无法解析: {exc}")
        if "code" not in payload:
            return PublishResult("uncertain", error="空间发布响应缺少 code")

        try:
            code = int(payload.get("code"))
        except (TypeError, ValueError):
            return PublishResult("uncertain", error="空间发布响应 code 无效")
        if code != 0:
            message = str(payload.get("message") or payload.get("msg") or code)
            lowered = message.lower()
            if code in _AUTH_CODES or any(hint in lowered or hint in message for hint in _AUTH_HINTS):
                raise QzoneAuthError(f"空间鉴权失败: {message}")
            return PublishResult("rejected", error=f"空间发布被拒绝: {message}")

        tid = str(payload.get("tid") or "").strip()
        if not tid:
            return PublishResult("uncertain", error="空间返回成功但缺少动态 ID")
        return PublishResult("sent", tid=tid)
