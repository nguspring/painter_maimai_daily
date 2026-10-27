from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

import base64
import logging
import traceback
import urllib.request

if TYPE_CHECKING:
    from .request_context import RequestContext

logger = logging.getLogger("plugin.painter_maimai_daily.image")


class ImageProcessor:
    """图片处理工具类

    构造时接收当前请求的 RequestContext，使用：
    - log_prefix
    - 可选：message（Command 路径触发消息 dict；Tool 路径为 None）
    - ctx（PluginContext，用于 get_by_id 回拉 reply 目标消息）
    - chat_id（用于 get_by_id 时定位会话）
    """

    def __init__(self, request_ctx: "RequestContext") -> None:
        self.request_ctx = request_ctx
        self.log_prefix = request_ctx.log_prefix
        self._ctx = request_ctx.ctx
        self._chat_id = request_ctx.chat_id or request_ctx.stream_id

    # ==================== 当前消息内提取 ====================

    @staticmethod
    def _reply_target(message: Dict[str, Any]) -> str:
        """读取 SDK 的 reply_to 或 reply 段中的被引用消息 ID。"""
        target = str(message.get("reply_to") or "").strip()
        if target:
            return target
        for segment in message.get("raw_message") or []:
            if segment["type"] == "reply":
                return str(segment["data"].get("target_message_id") or "").strip()
        return ""

    # ==================== ctx capability 路径 ====================

    async def _fetch_image_by_message_id(self, message_id: str) -> Optional[str]:
        """通过 ctx.message.get_by_id(include_binary_data=True) 拉回完整消息并抓图"""
        if not message_id:
            return None
        full = await self._ctx.message.get_by_id(
            message_id=message_id, chat_id=self._chat_id, include_binary_data=True,
        )
        if not isinstance(full, dict):
            return None
        return self._scan_raw_for_binary(full.get("raw_message"))

    async def fetch_image_by_triggering_id(
        self,
        triggering_message_id: Optional[str],
    ) -> Optional[str]:
        """Tool 路径专用：依据 LLM 透传的 msg_id 100% 锁定触发消息抓图。

        查找顺序：
        1. 触发消息自身含 image/emoji 段 → 直接返回
        2. 触发消息含 reply（看 reply_to 快捷字段，缺则扫 raw_message 的 reply 段）
           → 拉被引用消息再抓图
        """
        if not triggering_message_id:
            return None

        message = await self._ctx.message.get_by_id(
            message_id=triggering_message_id, chat_id=self._chat_id, include_binary_data=True,
        )
        if not isinstance(message, dict):
            return None

        b64 = self._scan_raw_for_binary(message.get("raw_message"))
        if b64:
            logger.info(f"{self.log_prefix} 触发消息含图，按 msg_id 直接命中")
            return b64

        target_id = self._reply_target(message)

        if target_id:
            logger.info(f"{self.log_prefix} 触发消息引用了 {target_id}，拉被引用消息取图")
            return await self._fetch_image_by_message_id(target_id)

        return None

    @staticmethod
    def _scan_raw_for_binary(raw: Any) -> Optional[str]:
        """从 raw_message（list[dict]）中找带 binary_data_base64 的 image/emoji 段"""
        if not isinstance(raw, list):
            return None
        for seg in raw:
            if not isinstance(seg, dict):
                continue
            stype = str(seg.get("type") or "").lower()
            if stype not in ("image", "emoji"):
                continue
            b64 = seg.get("binary_data_base64")
            if isinstance(b64, str) and b64:
                return b64
        return None

    # ==================== 主入口 ====================

    async def get_recent_image(self) -> Optional[str]:
        """严格模式：仅从触发消息本身解析图片（base64），找不到返回 None。

        查找顺序：
        1. 当前消息含 image/emoji 段（直接 base64 或 hash → get_by_id 回填）
        2. 当前消息含 reply 段 → 拉被引用消息取图

        命令路径直接读取触发消息；Tool 路径通过 fetch_image_by_triggering_id 定位。
        """
        message = self.request_ctx.message
        if message is None:
            return None
        raw = message.get("raw_message") or []
        binary = self._scan_raw_for_binary(raw)
        if binary:
            return binary
        # 当前附图优先于引用图。command 消息不带二进制时，仅回拉本条消息补齐。
        if any(segment["type"] in ("image", "emoji") for segment in raw):
            message_id = message.get("message_id")
            if message_id:
                return await self._fetch_image_by_message_id(message_id)
            return None
        target_id = self._reply_target(message)
        if target_id:
            return await self._fetch_image_by_message_id(target_id)
        return None

    # ==================== 通用工具 ====================

    def download_and_encode_base64(self, image_url: str, proxy_url: str = None) -> Tuple[bool, str]:
        """下载图片或处理 Base64 数据 URL

        Args:
            image_url: 图片 URL 或 data:image/ 数据 URL
            proxy_url: 代理地址（如 http://127.0.0.1:7890），为空则直连
        """
        logger.info(f"{self.log_prefix} (B64) 处理图片: {image_url[:50]}...")

        try:
            if image_url.startswith('data:image/'):
                logger.info(f"{self.log_prefix} (B64) 检测到 Base64 数据 URL")
                if ';base64,' in image_url:
                    base64_data = image_url.split(';base64,', 1)[1]
                    logger.info(f"{self.log_prefix} (B64) 提取完成, 长度: {len(base64_data)}")
                    return True, base64_data
                error_msg = "Base64 数据 URL 格式不正确"
                logger.error(f"{self.log_prefix} (B64) {error_msg}")
                return False, error_msg

            if proxy_url:
                import requests
                logger.info(f"{self.log_prefix} (B64) 下载 HTTP 图片 (proxy: {proxy_url})")
                resp = requests.get(image_url, timeout=180, proxies={"http": proxy_url, "https": proxy_url})
                if resp.status_code == 200:
                    base64_encoded_image = base64.b64encode(resp.content).decode("utf-8")
                    logger.info(f"{self.log_prefix} (B64) 下载编码完成, 长度: {len(base64_encoded_image)}")
                    return True, base64_encoded_image
                error_msg = f"下载图片失败 (状态: {resp.status_code})"
                logger.error(f"{self.log_prefix} (B64) {error_msg} URL: {image_url[:30]}...")
                return False, error_msg

            logger.info(f"{self.log_prefix} (B64) 下载 HTTP 图片")
            with urllib.request.urlopen(image_url, timeout=180) as response:
                if response.status == 200:
                    image_bytes = response.read()
                    base64_encoded_image = base64.b64encode(image_bytes).decode("utf-8")
                    logger.info(f"{self.log_prefix} (B64) 下载编码完成, 长度: {len(base64_encoded_image)}")
                    return True, base64_encoded_image
                error_msg = f"下载图片失败 (状态: {response.status})"
                logger.error(f"{self.log_prefix} (B64) {error_msg} URL: {image_url[:30]}...")
                return False, error_msg

        except Exception as e:
            logger.error(f"{self.log_prefix} (B64) 处理图片时错误: {e!r}", exc_info=True)
            traceback.print_exc()
            return False, f"处理图片时发生错误: {str(e)[:50]}"

    def process_api_response(self, result) -> Optional[str]:
        """统一处理 API 响应，提取图片数据"""
        try:
            if isinstance(result, str):
                return result

            if isinstance(result, dict):
                for key in ['url', 'image', 'b64_json', 'data']:
                    if key in result and result[key]:
                        return result[key]

                if 'output' in result and isinstance(result['output'], dict):
                    output = result['output']
                    for key in ['image_url', 'images']:
                        if key in output:
                            data = output[key]
                            return data[0] if isinstance(data, list) and data else data

            return None
        except Exception as e:
            logger.error(f"{self.log_prefix} 处理 API 响应失败: {str(e)[:50]}")
            return None
