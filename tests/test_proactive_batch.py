"""多目标同批判重与 reserve(dedupe=False)。"""

from pathlib import Path

import asyncio

from painter_maimai_daily.core.state.proactive_state import ProactiveStateStore


def test_batch_duplicate_is_read_only_and_dedupe_can_be_disabled(tmp_path: Path) -> None:
    async def run() -> None:
        store = ProactiveStateStore()
        await store.load(tmp_path / "proactive_images.json")
        first = await store.reserve(
            source="auto_selfie", target="qzone", cooldown_seconds=0,
            prompt="同一张照片", is_selfie=True, activity_key="activity-1",
        )
        assert first is not None
        assert store.batch_duplicate("同一张照片", is_selfie=True, activity_key="activity-1")
        assert not store.batch_duplicate("另一张照片", is_selfie=True, activity_key="activity-2")

        second = await store.reserve(
            source="auto_selfie", target="group:g1", cooldown_seconds=0,
            prompt="同一张照片", is_selfie=True, activity_key="activity-1", dedupe=False,
        )
        assert second is not None

        blocked = await store.reserve(
            source="auto_selfie", target="private:u1", cooldown_seconds=0,
            prompt="同一张照片", is_selfie=True, activity_key="activity-1", dedupe=True,
        )
        assert blocked is None
        assert len(store.recent()) == 2
        await store.close()

    asyncio.run(run())
