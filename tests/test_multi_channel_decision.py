"""自动自拍多渠道决策的解析与非法目标拒绝。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import json
import pytest

from painter_maimai_daily.core.selfie.publication_decision import decide_publication, parse_publication_decision

CANDIDATES = ["qzone", "group_1", "private_1"]


def test_multi_destinations_are_accepted() -> None:
    decision = parse_publication_decision(json.dumps({
        "destinations": ["qzone", "group_1"],
        "reason": "空间记录日常，群里也合适",
        "prompt": "饭后坐在客厅喝茶",
    }), CANDIDATES)
    assert decision["destinations"] == ["qzone", "group_1"]


def test_empty_destinations_means_skip() -> None:
    decision = parse_publication_decision(json.dumps({
        "destinations": [], "reason": "现在不适合分享", "prompt": "",
    }), CANDIDATES)
    assert decision["destinations"] == []


@pytest.mark.parametrize("payload", [
    {"destinations": ["unknown"], "reason": "x", "prompt": "p"},
    {"destinations": ["qzone", "qzone"], "reason": "x", "prompt": "p"},
    {"destinations": ["qzone"], "reason": "x", "prompt": ""},
    {"destinations": ["qzone"], "reason": "x", "prompt": "p", "extra": 1},
    {"destinations": "qzone", "reason": "x", "prompt": "p"},
    {"destinations": [], "reason": "", "prompt": ""},
])
def test_illegal_decision_is_rejected(payload: dict) -> None:
    with pytest.raises(ValueError):
        parse_publication_decision(json.dumps(payload), CANDIDATES)


def test_no_candidates_returns_empty_without_calling_llm() -> None:
    plugin = SimpleNamespace(
        ctx=SimpleNamespace(llm=SimpleNamespace(generate=AsyncMock())),
        config=SimpleNamespace(basic=SimpleNamespace(llm_task_name="utils", llm_timeout_seconds=45)),
        proactive_state=SimpleNamespace(recent=lambda: []),
    )
    activity = SimpleNamespace(to_payload=lambda: {})
    result = asyncio.run(decide_publication(plugin, activity, []))
    assert result["destinations"] == []
    plugin.ctx.llm.generate.assert_not_awaited()
