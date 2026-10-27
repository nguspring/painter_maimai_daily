"""QQ 空间发布器：懒加载 Cookie，鉴权失败自动刷新一次。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import base64
import logging

from .client import PublishResult, QzoneAuthError, QzoneClient
from .cookies import fetch_cookies, resolve_bot_uin

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin

logger = logging.getLogger("plugin.painter_maimai_daily.qzone.publisher")


class QzonePublisher:
    """实例级发布器；只缓存 Cookie，不持有长连接。"""

    def __init__(self, plugin: "PainterMaimaiPlugin") -> None:
        """绑定插件；真正的 Cookie 在首次发布时才获取。"""
        self.plugin = plugin
        self._uin = ""
        self._cookies: Optional[dict[str, str]] = None

    @property
    def available(self) -> bool:
        """配置开启且至少配置了一种取 Cookie 方式时才参与渠道选择。"""
        cfg = self.plugin.config.qzone
        return bool(cfg.enabled and cfg.cookie_methods)

    async def _ensure_cookies(self, *, force_refresh: bool = False) -> dict[str, str]:
        """解析 QQ 号并按需刷新 Cookie；内存命中时直接复用。"""
        if self._uin and self._cookies is not None and not force_refresh:
            return self._cookies
        uin = self._uin or await resolve_bot_uin(self.plugin)
        if not uin:
            raise RuntimeError("无法确定机器人 QQ 号，空间渠道跳过")
        cookies = await fetch_cookies(self.plugin, uin, force_refresh=force_refresh)
        self._uin = uin
        self._cookies = cookies
        return cookies

    async def publish(
        self, plugin: "PainterMaimaiPlugin", image_base64: str, text: str,
    ) -> PublishResult:
        """发布一张图文说说；任何异常都转成明确结果，不向调用方抛出不确定状态。"""
        cfg = plugin.config.qzone
        if not cfg.enabled:
            return PublishResult("rejected", error="空间渠道未启用")
        if not str(text or "").strip():
            return PublishResult("rejected", error="空间说说正文不能为空")
        try:
            image_bytes = base64.b64decode(image_base64, validate=True)
        except (ValueError, TypeError) as exc:
            return PublishResult("rejected", error=f"空间图片 base64 无效: {exc}")
        if not image_bytes:
            return PublishResult("rejected", error="空间图片为空")

        for attempt in range(2):
            try:
                cookies = await self._ensure_cookies(force_refresh=attempt > 0)
            except Exception as exc:
                logger.warning("获取空间 Cookie 失败: %s", exc)
                return PublishResult("rejected", error=str(exc))

            client = QzoneClient(cookies, self._uin, cfg.publish_timeout_seconds)
            try:
                result = await client.publish(text, [image_bytes])
            except QzoneAuthError as exc:
                if attempt == 0:
                    logger.warning("空间鉴权失败，刷新 Cookie 后重试一次: %s", exc)
                    self._cookies = None
                    continue
                return PublishResult("rejected", error=str(exc))
            except Exception as exc:
                logger.exception("空间发布异常")
                return PublishResult("uncertain", error=f"空间发布异常: {exc}")
            return result

        return PublishResult("rejected", error="空间鉴权失败，刷新 Cookie 后仍未恢复")
