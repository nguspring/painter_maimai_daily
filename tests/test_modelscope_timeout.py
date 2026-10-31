"""魔搭异步生图的轮询超时契约：超时视为平台排队过久，直接失败，不再重复提交任务。"""

from typing import Any, Dict

import asyncio
import pytest

from painter_maimai_daily.core.api_clients import modelscope_client as ms
from painter_maimai_daily.core.api_clients.client_context import ClientContext


class _FakeClock:
    """替身时钟：让轮询循环在毫秒内跑完 120 秒预算。"""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Dict[str, Any]:
        return self._payload


class _FakeRequests:
    """记录提交次数：超时后若再次 POST 就说明又提交了一个重复任务。"""

    def __init__(self) -> None:
        self.posts = 0
        self.gets = 0

    def post(self, **kwargs: Any) -> _FakeResponse:
        self.posts += 1
        return _FakeResponse({"task_id": "task-1"})

    def get(self, **kwargs: Any) -> _FakeResponse:
        self.gets += 1
        return _FakeResponse({"task_status": "RUNNING"})


def test_poll_timeout_fails_without_resubmitting_a_duplicate_job(monkeypatch: Any) -> None:
    """单次轮询超时后不再重试：否则会在旧任务仍排队时又提交一个新任务。"""
    clock = _FakeClock()
    fake_requests = _FakeRequests()
    monkeypatch.setattr(ms, "time", clock)
    monkeypatch.setattr(ms, "requests", fake_requests)

    client = ms.ModelscopeClient(ClientContext(log_prefix="[魔搭离线]"))
    ok, error = asyncio.run(client.generate_image(
        prompt="测试提示词",
        model_config={
            "api_key": "test-key",
            "model": "krea/Krea-2-Turbo",
            "base_url": "https://example.invalid",
        },
        size="1024x1024",
        max_retries=2,
    ))

    assert ok is False
    assert "超时" in error
    assert fake_requests.posts == 1, "超时后不允许再提交新任务"


@pytest.mark.parametrize("budget,expected", [
    (170, 125.0),   # 默认预算下，单次等待要留出提示词和下载的余量
    (240, 180.0),   # 预算放大后，单次等待顶到 180 秒上限
    (600, 180.0),   # 再大也不超过上限
    (100, 55.0),
    (30, 45.0),     # 预算极小时保底，避免立刻超时
])
def test_poll_cap_follows_the_generation_budget(budget: float, expected: float) -> None:
    """单次轮询上限必须由总预算推导，避免外层时限比内层等待还短。"""
    client = ms.ModelscopeClient(
        ClientContext(config_getter=lambda _key, default=None: budget),
    )
    assert client._resolve_max_poll_seconds() == expected
