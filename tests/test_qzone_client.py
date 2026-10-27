"""QQ 空间客户端：g_tk、richval 与发布状态分级。"""

import asyncio
import json
import pytest
import httpx

from painter_maimai_daily.core.qzone import client as client_module
from painter_maimai_daily.core.qzone.client import (
    PublishResult,
    QzoneAuthError,
    QzoneClient,
    parse_upload_result,
)


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, response) -> None:
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, *args, **kwargs):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def patch_http(monkeypatch: pytest.MonkeyPatch, response) -> None:
    """让 AsyncClient 返回固定响应或抛出固定异常。"""
    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda **kwargs: _FakeClient(response))


def make_client() -> QzoneClient:
    return QzoneClient({"p_skey": "abc", "skey": "sk", "uin": "123"}, "123", 5)


def test_gtk_matches_qq_space_algorithm() -> None:
    assert QzoneClient._gtk("abc") == "193485963"


def test_upload_result_parses_pic_bo_and_richval() -> None:
    payload = {
        "ret": 0,
        "data": {
            "url": "https://example.test/image?x=1&bo=PICBO",
            "albumid": "album", "lloc": "lloc", "sloc": "sloc", "type": "jpg",
            "height": 100, "width": 200,
        },
    }
    pic_bo, richval = parse_upload_result(payload)
    assert pic_bo == "PICBO"
    assert richval == ",album,lloc,lloc,jpg,100,200,,100,200"


@pytest.mark.parametrize(("response", "expected"), [
    (_FakeResponse(200, json.dumps({"code": 0, "tid": "tid-1"})), "sent"),
    (_FakeResponse(200, json.dumps({"code": -100, "message": "参数错误"})), "rejected"),
    (_FakeResponse(200, json.dumps({"code": 0})), "uncertain"),
    (_FakeResponse(200, "not-json"), "uncertain"),
    (_FakeResponse(502, "gateway error"), "uncertain"),
    (httpx.ConnectError("boom"), "uncertain"),
])
def test_publish_result_grading(monkeypatch: pytest.MonkeyPatch, response, expected: str) -> None:
    patch_http(monkeypatch, response)
    result = asyncio.run(make_client().publish("正文"))
    assert isinstance(result, PublishResult)
    assert result.status == expected


def test_auth_error_raises_for_publisher_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_http(monkeypatch, _FakeResponse(200, json.dumps({"code": -3000, "message": "请先登录"})))
    with pytest.raises(QzoneAuthError):
        asyncio.run(make_client().publish("正文"))


def test_headers_carry_cookie_for_qzone_auth() -> None:
    headers = make_client()._headers()
    assert headers["Cookie"] == "p_skey=abc; skey=sk; uin=123"


def test_upload_failure_is_rejected_before_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_http(monkeypatch, httpx.ConnectError("upload down"))
    result = asyncio.run(make_client().publish("正文", [b"image"]))
    assert result.status == "rejected"
    assert "上传" in result.error
