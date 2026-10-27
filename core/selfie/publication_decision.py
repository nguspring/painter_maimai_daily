"""自动自拍多渠道决策：模型只能从本轮候选中多选，或返回空列表跳过。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

import asyncio
import json
import re

from ..prompts.prompt_templates import load_prompt_template
from .caption_generator import _get_bot_profile
from .schedule_provider import ActivityInfo

if TYPE_CHECKING:
    from ...plugin import PainterMaimaiPlugin


def parse_publication_decision(response: str, candidate_ids: List[str]) -> Dict[str, Any]:
    """严格校验多选 JSON；重复、未知目标或超长文本直接拒绝。"""
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip())
    decision = json.loads(clean)
    if not isinstance(decision, dict) or set(decision) != {"destinations", "reason", "prompt"}:
        raise ValueError("自拍发布判断必须且只能包含 destinations/reason/prompt")
    destinations = decision.get("destinations")
    if not isinstance(destinations, list) or any(not isinstance(item, str) for item in destinations):
        raise ValueError("destinations 必须为字符串数组")
    if len(destinations) != len(set(destinations)):
        raise ValueError("destinations 不能重复")
    if any(item not in candidate_ids for item in destinations):
        raise ValueError("自拍发布渠道不在本轮候选中")

    reason = decision.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
        raise ValueError("自拍发布判断缺少有效理由")
    prompt = decision.get("prompt")
    if not isinstance(prompt, str) or len(prompt) > 1500:
        raise ValueError("自拍发布判断缺少有效图片描述")
    if destinations and not prompt.strip():
        raise ValueError("需要发布时必须提供图片描述")
    return {
        "destinations": destinations,
        "reason": reason.strip(),
        "prompt": prompt.strip(),
    }


async def decide_publication(
    plugin: "PainterMaimaiPlugin",
    activity: ActivityInfo,
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """把候选交给 LLM 多选；候选为空时直接返回空决策，不浪费调用。"""
    if not candidates:
        return {"destinations": [], "reason": "本轮没有可用渠道", "prompt": ""}
    payload = {
        "persona": await _get_bot_profile(plugin.ctx),
        "activity": activity.to_payload(),
        "candidates": candidates,
        "recent_publications": plugin.proactive_state.recent(),
    }
    system_prompt = await asyncio.to_thread(load_prompt_template, "selfie_publication")
    result = await plugin.ctx.llm.generate(
        prompt=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model=plugin.config.basic.llm_task_name,
        temperature=0.2,
        max_tokens=2048,
        timeout_ms=plugin.config.basic.llm_timeout_seconds * 1000,
    )
    if not isinstance(result, dict) or result.get("success") is False:
        raise RuntimeError("自拍发布判断 LLM 调用失败")
    response = result.get("response", result.get("content", ""))
    if not isinstance(response, str):
        raise ValueError("自拍发布判断 LLM 返回非文本响应")
    return parse_publication_decision(response, [item["id"] for item in candidates])
