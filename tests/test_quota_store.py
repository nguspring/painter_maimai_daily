"""使用真实 SQLite 验证额度占用、审计、周期隔离与异步生命周期。"""

from contextlib import asynccontextmanager, closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any, AsyncIterator, Iterator, List, Optional, Tuple

import asyncio
import pytest
import sqlite3

from painter_maimai_daily.core.quota import (
    QuotaExceeded,
    QuotaPeriod,
    QuotaPolicy,
    QuotaReservation,
    QuotaStateError,
    QuotaStore,
    QuotaSubject,
    QuotaSubjectMismatch,
    SettlementOutcome,
)
from painter_maimai_daily.core.quota import store as quota_module


GROUP = QuotaSubject("qq", "group", "10001")
POLICY = QuotaPolicy(3)


@dataclass
class Clock:
    now: datetime = datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@asynccontextmanager
async def opened(path: Path, clock: Clock) -> AsyncIterator[QuotaStore]:
    store = QuotaStore(clock=clock)
    await store.load(path)
    try:
        yield store
    finally:
        await store.close()


def audit_rows(path: Path) -> List[Tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT token,action,amount,actor,reason,previous_status,status,period_key FROM quota_audit ORDER BY id"
        ).fetchall()


async def wait_until_entered(event: Event) -> None:
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 5), timeout=6), "后台线程未进入受控操作"


def test_constructor_has_no_io_and_unloaded_operations_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_io(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("构造额度账本不应执行 I/O 或创建执行器")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", reject_io)
        patch.setattr(Path, "mkdir", reject_io)
        patch.setattr(quota_module, "ThreadPoolExecutor", reject_io)
        store = QuotaStore()
        assert not store.loaded

    async def exercise() -> None:
        for operation in (
            lambda: store.status(GROUP, POLICY),
            lambda: store.reserve(GROUP, POLICY),
            lambda: store.settle("missing", "released"),
            lambda: store.adjust(GROUP, POLICY, 1, actor="管理员", reason="补额"),
            lambda: store.list_pending(GROUP),
            lambda: store.resolve(GROUP, "missing", "released", actor="管理员", reason="核实"),
        ):
            with pytest.raises(QuotaStateError, match="尚未加载或已经关闭"):
                await operation()
        await store.close()

    asyncio.run(exercise())
    assert list(tmp_path.iterdir()) == []


def test_load_is_idempotent_and_closed_store_can_be_reloaded(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "state" / "quota.sqlite3"

    async def exercise() -> None:
        store = QuotaStore(clock=clock)
        await store.load(path)
        try:
            reservation = await store.reserve(GROUP, POLICY)
            await store.load(path)
            assert await store.list_pending(GROUP) == []
            with pytest.raises(QuotaStateError, match="切换数据库前必须关闭"):
                await store.load(tmp_path / "other.sqlite3")
            await store.close()
            assert not store.loaded
            with pytest.raises(QuotaStateError, match="已经关闭"):
                await store.status(GROUP, POLICY)
            await store.load(path)
            assert [item.token for item in await store.list_pending(GROUP)] == [reservation.token]
        finally:
            await store.close()
            await store.close()

    asyncio.run(exercise())


def test_many_concurrent_reservations_never_exceed_limit(tmp_path: Path, clock: Clock) -> None:
    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            policy = QuotaPolicy(7)
            results = await asyncio.gather(*(store.reserve(GROUP, policy) for _ in range(80)), return_exceptions=True)
            accepted = [result for result in results if isinstance(result, QuotaReservation)]
            rejected = [result for result in results if isinstance(result, QuotaExceeded)]
            assert len(accepted) == len({result.token for result in accepted}) == 7
            assert len(rejected) == 73
            assert all(result.status.remaining == 0 and result.quota_status is result.status for result in rejected)
            status = await store.status(GROUP, policy)
            assert (status.consumed, status.reserved, status.unknown, status.remaining, status.used) == (0, 7, 0, 0, 7)

    asyncio.run(exercise())


def test_two_connections_reserve_atomically(tmp_path: Path, clock: Clock) -> None:
    async def exercise() -> None:
        path = tmp_path / "quota.sqlite3"
        # 两个实例均先加载，随后共同争用 SQLite 写事务；加载恢复不参与本轮并发。
        async with opened(path, clock) as first, opened(path, clock) as second:
            policy = QuotaPolicy(9)
            stores = (first, second)
            results = await asyncio.gather(
                *(stores[index % 2].reserve(GROUP, policy) for index in range(70)), return_exceptions=True
            )
            assert sum(isinstance(result, QuotaReservation) for result in results) == 9
            assert sum(isinstance(result, QuotaExceeded) for result in results) == 61
            status = await first.status(GROUP, policy)
            assert status == await second.status(GROUP, policy)
            assert (status.reserved, status.remaining) == (9, 0)

    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["consumed", "released"])
def test_repeated_settlement_across_connections_is_idempotent(
    tmp_path: Path, clock: Clock, outcome: SettlementOutcome
) -> None:
    path = tmp_path / "quota.sqlite3"

    async def exercise() -> str:
        async with opened(path, clock) as first, opened(path, clock) as second:
            policy = QuotaPolicy(1)
            reservation = await first.reserve(GROUP, policy)
            stores = (first, second)
            results = await asyncio.gather(
                *(stores[index % 2].settle(reservation.token, outcome) for index in range(40))
            )
            assert all(result == results[0] for result in results)
            status = await first.status(GROUP, policy)
            assert status.reserved == 0
            assert status.consumed == (1 if outcome == "consumed" else 0)
            assert status.remaining == (0 if outcome == "consumed" else 1)
            opposite = "released" if outcome == "consumed" else "consumed"
            with pytest.raises(QuotaStateError, match="不能更改已确认结果"):
                await second.settle(reservation.token, opposite)
            if outcome == "released":
                await first.reserve(GROUP, policy)
                with pytest.raises(QuotaExceeded):
                    await second.reserve(GROUP, policy)
            return reservation.token

    token = asyncio.run(exercise())
    rows = [row for row in audit_rows(path) if row[0] == token]
    assert len(rows) == 2
    assert (rows[1][1], rows[1][2], rows[1][5], rows[1][6]) == (
        "settle",
        1 if outcome == "released" else 0,
        "reserved",
        outcome,
    )


def test_competing_terminal_results_cannot_overwrite_each_other(tmp_path: Path, clock: Clock) -> None:
    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            reservation = await store.reserve(GROUP, QuotaPolicy(1))
            results = await asyncio.gather(
                store.settle(reservation.token, "consumed"),
                store.settle(reservation.token, "released"),
                return_exceptions=True,
            )
            assert sum(isinstance(result, QuotaReservation) for result in results) == 1
            assert sum(isinstance(result, QuotaStateError) for result in results) == 1

    asyncio.run(exercise())


def test_unknown_remains_occupied_and_only_its_subject_can_refund(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "quota.sqlite3"

    async def exercise() -> str:
        async with opened(path, clock) as store:
            policy = QuotaPolicy(1)
            reservation = await store.reserve(GROUP, policy)
            unknown = await store.settle(reservation.token, "unknown", reason="回执超时")
            assert await store.settle(reservation.token, "unknown") == unknown
            status = await store.status(GROUP, policy)
            assert (status.reserved, status.unknown, status.remaining) == (1, 1, 0)
            assert await store.list_pending(GROUP) == [unknown]
            with pytest.raises(QuotaExceeded):
                await store.reserve(GROUP, policy)
            with pytest.raises(QuotaStateError, match="不能自动退款"):
                await store.settle(reservation.token, "released")
            for subject in (
                QuotaSubject("qq", "group", "10002"),
                QuotaSubject("qq", "user", GROUP.target_id),
                QuotaSubject("discord", "group", GROUP.target_id),
            ):
                assert await store.list_pending(subject) == []
                with pytest.raises(QuotaSubjectMismatch, match="不属于当前聊天"):
                    await store.resolve(subject, reservation.token, "released", actor="管理员", reason="核实")
            resolved = await store.resolve(
                GROUP, reservation.token, "released", actor=" 管理员 42 ", reason=" 已核实未投递 "
            )
            assert resolved.actor == "管理员 42"
            assert resolved.reason == "已核实未投递"
            assert (
                await store.resolve(GROUP, reservation.token, "released", actor="另一管理员", reason="重试") == resolved
            )
            # 已退款的编号也不能从另一个聊天幂等重放，主体核对必须先于状态判断。
            with pytest.raises(QuotaSubjectMismatch):
                await store.resolve(
                    QuotaSubject("qq", "group", "10002"), reservation.token, "released", actor="管理员", reason="重试"
                )
            assert await store.list_pending(GROUP) == []
            assert (await store.status(GROUP, policy)).remaining == 1
            return reservation.token

    token = asyncio.run(exercise())
    resolutions = [row for row in audit_rows(path) if row[0] == token and row[1] == "resolve"]
    assert len(resolutions) == 1
    assert resolutions[0][2:7] == (1, "管理员 42", "已核实未投递", "unknown", "released")


def test_late_success_can_consume_unknown_but_active_reservations_cannot_be_resolved(
    tmp_path: Path, clock: Clock
) -> None:
    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            reservation = await store.reserve(GROUP, POLICY)
            for outcome in ("consumed", "released"):
                with pytest.raises(QuotaStateError, match="仍在处理中"):
                    await store.resolve(GROUP, reservation.token, outcome, actor="管理员", reason="核实")
            await store.settle(reservation.token, "unknown")
            result = await store.settle(reservation.token, "consumed", reason="迟到的成功回执")
            assert result.status == result.state == "consumed"
            assert result.reservation_id == reservation.token
            assert result.policy == POLICY
            assert (await store.status(GROUP, POLICY)).consumed == 1
            assert await store.list_pending(GROUP) == []
            with pytest.raises(QuotaStateError, match="未找到"):
                await store.settle("missing", "consumed")

    asyncio.run(exercise())


def test_restart_preserves_usage_adjustments_and_recovers_only_active_reservations(
    tmp_path: Path, clock: Clock
) -> None:
    path = tmp_path / "quota.sqlite3"

    async def exercise() -> str:
        policy = QuotaPolicy(5)
        async with opened(path, clock) as first:
            await first.adjust(GROUP, policy, 3, actor="管理员", reason="本日补额")
            consumed = await first.reserve(GROUP, policy)
            released = await first.reserve(GROUP, policy)
            active = await first.reserve(GROUP, policy)
            unknown = await first.reserve(GROUP, policy)
            await first.settle(consumed.token, "consumed")
            await first.settle(released.token, "released")
            await first.settle(unknown.token, "unknown")
        clock.now += timedelta(hours=1)
        async with opened(path, clock) as second:
            status = await second.status(GROUP, policy)
            assert (status.adjustment, status.consumed, status.reserved, status.unknown, status.remaining) == (
                3,
                1,
                2,
                2,
                5,
            )
            pending = await second.list_pending(GROUP)
            assert {item.token for item in pending} == {active.token, unknown.token}
            recovered = next(item for item in pending if item.token == active.token)
            assert recovered.actor == "system"
            assert "重新加载" in recovered.reason
            assert recovered.created_at == active.created_at
            assert recovered.updated_at > active.updated_at
            live = await second.reserve(GROUP, policy)
            await second.load(path)
            assert live.token not in {item.token for item in await second.list_pending(GROUP)}
            return active.token

    active_token = asyncio.run(exercise())
    recoveries = [row for row in audit_rows(path) if row[1] == "recover"]
    assert len(recoveries) == 1
    assert recoveries[0][0] == active_token


@pytest.mark.parametrize(
    ("period", "before", "after", "key", "next_key", "reset"),
    [
        (
            "daily",
            "2026-09-11T15:59:59+00:00",
            "2026-09-11T16:00:00+00:00",
            "2026-09-11",
            "2026-09-12",
            "2026-09-12T00:00:00+08:00",
        ),
        (
            "weekly",
            "2026-09-13T15:59:59+00:00",
            "2026-09-13T16:00:00+00:00",
            "2026-09-07",
            "2026-09-14",
            "2026-09-14T00:00:00+08:00",
        ),
        (
            "weekly",
            "2026-01-04T15:59:59+00:00",
            "2026-01-04T16:00:00+00:00",
            "2025-12-29",
            "2026-01-05",
            "2026-01-05T00:00:00+08:00",
        ),
        (
            "monthly",
            "2026-12-31T15:59:59+00:00",
            "2026-12-31T16:00:00+00:00",
            "2026-12",
            "2027-01",
            "2027-01-01T00:00:00+08:00",
        ),
        (
            "monthly",
            "2028-02-29T15:59:59+00:00",
            "2028-02-29T16:00:00+00:00",
            "2028-02",
            "2028-03",
            "2028-03-01T00:00:00+08:00",
        ),
        ("once", "2026-12-31T15:59:59+00:00", "2030-01-01T16:00:00+00:00", "once", "once", None),
    ],
)
def test_calendar_periods_reset_on_their_local_boundaries(
    tmp_path: Path,
    clock: Clock,
    period: QuotaPeriod,
    before: str,
    after: str,
    key: str,
    next_key: str,
    reset: Optional[str],
) -> None:
    clock.now = datetime.fromisoformat(before)
    policy = QuotaPolicy(1, period)

    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            reservation = await store.reserve(GROUP, policy)
            expected_key = "once" if period == "once" else f"{period}:Asia/Shanghai:{key}"
            assert reservation.period_key == expected_key
            assert reservation.reset_at == (datetime.fromisoformat(reset) if reset else None)
            await store.settle(reservation.token, "consumed")
            clock.now = datetime.fromisoformat(after)
            status = await store.status(GROUP, policy)
            assert status.period_key == ("once" if period == "once" else f"{period}:Asia/Shanghai:{next_key}")
            assert status.remaining == (0 if period == "once" else 1)
            if period == "once":
                with pytest.raises(QuotaExceeded):
                    await store.reserve(GROUP, policy)
            else:
                await store.reserve(GROUP, policy)

    asyncio.run(exercise())


def test_daylight_saving_transition_uses_the_next_local_midnight(tmp_path: Path, clock: Clock) -> None:
    clock.now = datetime.fromisoformat("2026-03-08T06:59:59+00:00")
    policy = QuotaPolicy(1, timezone="America/New_York")

    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            reservation = await store.reserve(GROUP, policy)
            assert reservation.reset_at == datetime.fromisoformat("2026-03-09T00:00:00-04:00")
            clock.now = datetime.fromisoformat("2026-03-08T07:00:00+00:00")
            assert (await store.status(GROUP, policy)).remaining == 0
            clock.now = datetime.fromisoformat("2026-03-09T04:00:00+00:00")
            assert (await store.status(GROUP, policy)).remaining == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["consumed", "released"])
def test_cross_period_settlement_does_not_charge_or_refund_the_new_period(
    tmp_path: Path, clock: Clock, outcome: SettlementOutcome
) -> None:
    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            old = await store.reserve(GROUP, POLICY)
            uncertain = await store.reserve(GROUP, POLICY)
            await store.settle(uncertain.token, "unknown")
            clock.now += timedelta(days=1)
            current = await store.reserve(GROUP, POLICY)
            before = await store.status(GROUP, POLICY)
            settled = await store.settle(old.token, outcome)
            assert settled.period_key == old.period_key != current.period_key
            assert settled.reset_at == old.reset_at
            assert [item.token for item in await store.list_pending(GROUP)] == [uncertain.token]
            await store.resolve(GROUP, uncertain.token, outcome, actor="管理员", reason="核对前一日发送")
            assert await store.status(GROUP, POLICY) == before

    asyncio.run(exercise())


@pytest.mark.parametrize("period", ["daily", "weekly", "monthly", "once"])
def test_timezone_changes_isolate_calendar_periods_but_keep_once_usage(
    tmp_path: Path, clock: Clock, period: QuotaPeriod
) -> None:
    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            original = QuotaPolicy(2, period, "Asia/Shanghai")
            changed = QuotaPolicy(2, period, "UTC")
            old = await store.reserve(GROUP, original)
            new_status = await store.status(GROUP, changed)
            assert new_status.reserved == (1 if period == "once" else 0)
            current = await store.reserve(GROUP, changed)
            assert (current.period_key == old.period_key) == (period == "once")
            settled = await store.settle(old.token, "consumed")
            assert settled.policy == original
            assert settled.reset_at == old.reset_at
            status = await store.status(GROUP, changed)
            assert (status.consumed, status.reserved) == ((1, 1) if period == "once" else (0, 1))
            assert (await store.status(GROUP, original)).consumed == 1

    asyncio.run(exercise())


def test_limit_and_period_changes_do_not_rewrite_inflight_reservations(tmp_path: Path, clock: Clock) -> None:
    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            original = QuotaPolicy(2)
            old = await store.reserve(GROUP, original)
            await store.adjust(GROUP, original, 1, actor="管理员", reason="补充当前日额度")
            assert (await store.status(GROUP, QuotaPolicy(10))).remaining == 10
            assert (await store.status(GROUP, QuotaPolicy(0))).balance == 0
            changed = QuotaPolicy(1, "weekly")
            current = await store.reserve(GROUP, changed)
            settled = await store.settle(old.token, "consumed")
            assert settled.policy == original
            assert settled.period_key == old.period_key != current.period_key
            assert settled.reset_at == old.reset_at
            assert (await store.status(GROUP, changed)).adjustment == 0
            assert (await store.status(GROUP, changed)).remaining == 0
            assert (await store.status(GROUP, original)).consumed == 1

    asyncio.run(exercise())


def test_manual_adjustment_preserves_negative_balance_and_audit(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "quota.sqlite3"

    async def exercise() -> None:
        async with opened(path, clock) as store:
            policy = QuotaPolicy(2)
            reservation = await store.reserve(GROUP, policy)
            await store.settle(reservation.token, "consumed")
            added = await store.adjust(GROUP, policy, 2, actor=" 管理员 ", reason=" 临时加额 ")
            assert added.remaining == 3
            reduced = await store.adjust(GROUP, policy, -5, actor="管理员", reason="追回误加额度")
            assert (reduced.balance, reduced.remaining, reduced.adjustment) == (-2, 0, -3)
            with pytest.raises(QuotaExceeded):
                await store.reserve(GROUP, policy)
            restored = await store.adjust(GROUP, policy, 3, actor="管理员", reason="补齐额度")
            assert restored.balance == 1
            await store.reserve(GROUP, policy)
            assert (await store.status(GROUP, policy)).balance == 0
            clock.now += timedelta(days=1)
            status = await store.status(GROUP, policy)
            assert (status.adjustment, status.consumed, status.reserved, status.remaining) == (0, 0, 0, 2)

    asyncio.run(exercise())
    adjustments = [row for row in audit_rows(path) if row[1] == "adjust"]
    assert [row[2:5] for row in adjustments] == [
        (2, "管理员", "临时加额"),
        (-5, "管理员", "追回误加额度"),
        (3, "管理员", "补齐额度"),
    ]
    assert len({row[7] for row in adjustments}) == 1


def test_platform_group_and_private_subjects_are_independent(tmp_path: Path, clock: Clock) -> None:
    subjects = [
        QuotaSubject(platform, target_type, target_id)
        for platform in ("qq", "discord")
        for target_type in ("group", "user")
        for target_id in ("10001", "10002")
    ]

    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            policy = QuotaPolicy(1, "once")
            for subject in subjects:
                reservation = await store.reserve(subject, policy)
                await store.settle(reservation.token, "unknown")
            await store.adjust(subjects[0], policy, 1, actor="管理员", reason="本聊天补额")
            for index, subject in enumerate(subjects):
                status = await store.status(subject, policy)
                assert (status.reserved, status.unknown, status.remaining) == (1, 1, 1 if index == 0 else 0)
                pending = await store.list_pending(subject)
                assert len(pending) == 1 and pending[0].subject == subject

    asyncio.run(exercise())


@pytest.mark.parametrize("amount", [0, True, 1.5, "2"])
def test_adjustment_requires_nonzero_integer(tmp_path: Path, clock: Clock, amount: Any) -> None:
    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            with pytest.raises(ValueError, match="非零整数"):
                await store.adjust(GROUP, POLICY, amount, actor="管理员", reason="测试")
            assert (await store.status(GROUP, POLICY)).remaining == POLICY.limit

    asyncio.run(exercise())


@pytest.mark.parametrize(("actor", "reason"), [("", "理由"), ("管理员", "  ")])
def test_administration_requires_actor_and_reason(tmp_path: Path, clock: Clock, actor: str, reason: str) -> None:
    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            with pytest.raises(ValueError, match="不能为空"):
                await store.adjust(GROUP, POLICY, 1, actor=actor, reason=reason)
            with pytest.raises(ValueError, match="不能为空"):
                await store.resolve(GROUP, "missing", "released", actor=actor, reason=reason)

    asyncio.run(exercise())


@pytest.mark.parametrize("limit", [-1, 1000001, True, 1.5, "2"])
def test_quota_policy_rejects_invalid_limits(limit: Any) -> None:
    with pytest.raises(ValueError, match="额度上限"):
        QuotaPolicy(limit)


@pytest.mark.parametrize(
    ("platform", "target_type", "target_id"),
    [("", "group", "1"), ("qq ", "group", "1"), ("qq", "group", "1 2"), ("qq", "private", "1")],
)
def test_quota_subject_rejects_ambiguous_identity(platform: str, target_type: Any, target_id: str) -> None:
    with pytest.raises(ValueError):
        QuotaSubject(platform, target_type, target_id)


def test_invalid_timezone_and_naive_clock_raise_without_using_fallback(tmp_path: Path, clock: Clock) -> None:
    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            with pytest.raises(ValueError, match="无效的额度时区"):
                await store.reserve(GROUP, QuotaPolicy(1, timezone="invalid/invalid"))
            clock.now = datetime(2026, 9, 11)
            with pytest.raises(ValueError, match="带时区"):
                await store.reserve(GROUP, POLICY)
            clock.now = datetime(2026, 9, 11, tzinfo=timezone.utc)
            assert (await store.status(GROUP, POLICY)).used == 0

    asyncio.run(exercise())


def test_load_failure_cleans_up_and_allows_loading_another_database(tmp_path: Path, clock: Clock) -> None:
    unsupported = tmp_path / "unsupported.sqlite3"
    with closing(sqlite3.connect(unsupported)) as connection:
        connection.execute("PRAGMA user_version=99")

    async def exercise() -> None:
        store = QuotaStore(clock=clock)
        try:
            with pytest.raises(QuotaStateError, match="版本不兼容"):
                await store.load(unsupported)
            assert not store.loaded
            await store.load(tmp_path / "supported.sqlite3")
            assert (await store.status(GROUP, POLICY)).remaining == POLICY.limit
        finally:
            await store.close()

    asyncio.run(exercise())


def test_audit_failure_rolls_back_reserve_settle_and_adjust(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_audit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("审计写入失败")

    async def exercise() -> None:
        async with opened(tmp_path / "quota.sqlite3", clock) as store:
            with monkeypatch.context() as patch:
                patch.setattr(store, "_audit_sync", fail_audit)
                with pytest.raises(RuntimeError, match="审计写入失败"):
                    await store.reserve(GROUP, POLICY)
            assert (await store.status(GROUP, POLICY)).used == 0
            reservation = await store.reserve(GROUP, POLICY)
            with monkeypatch.context() as patch:
                patch.setattr(store, "_audit_sync", fail_audit)
                with pytest.raises(RuntimeError, match="审计写入失败"):
                    await store.settle(reservation.token, "released")
                with pytest.raises(RuntimeError, match="审计写入失败"):
                    await store.adjust(GROUP, POLICY, 2, actor="管理员", reason="补额")
            status = await store.status(GROUP, POLICY)
            assert (status.reserved, status.adjustment, status.remaining) == (1, 0, 2)
            await store.settle(reservation.token, "released")

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["reserve", "adjust"])
def test_waiting_for_another_writer_uses_period_after_the_write_lock_is_acquired(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    entered = Event()
    path = tmp_path / "quota.sqlite3"
    clock.now = datetime.fromisoformat("2026-09-11T15:59:59+00:00")

    async def exercise() -> None:
        async with opened(path, clock) as store:
            original = store._transaction

            @contextmanager
            def observed_transaction(*, write: bool = True) -> Iterator[sqlite3.Connection]:
                entered.set()
                with original(write=write) as connection:
                    yield connection

            monkeypatch.setattr(store, "_transaction", observed_transaction)
            # 外部连接先占住写锁，使本次业务操作排队跨越当地午夜。
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                task = asyncio.create_task(
                    store.reserve(GROUP, POLICY)
                    if operation == "reserve"
                    else store.adjust(GROUP, POLICY, 1, actor="管理员", reason="午夜补额")
                )
                try:
                    await wait_until_entered(entered)
                    clock.now += timedelta(seconds=1)
                finally:
                    writer.rollback()
                    result = await task
            assert result.period_key == "daily:Asia/Shanghai:2026-09-12"
            status = await store.status(GROUP, POLICY)
            assert status.reserved == (1 if operation == "reserve" else 0)
            assert status.adjustment == (1 if operation == "adjust" else 0)

    asyncio.run(exercise())


def test_cancellation_keeps_the_transaction_locked_and_close_waits_for_worker(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    path = tmp_path / "quota.sqlite3"

    async def exercise() -> None:
        store = QuotaStore(clock=clock)
        await store.load(path)
        original = store._audit_sync

        def pause_audit(*args: Any, **kwargs: Any) -> None:
            entered.set()
            assert release.wait(5), "测试未释放后台事务"
            original(*args, **kwargs)

        monkeypatch.setattr(store, "_audit_sync", pause_audit)
        reservation_task = asyncio.create_task(store.reserve(GROUP, POLICY))
        tasks = [reservation_task]
        try:
            await wait_until_entered(entered)
            reservation_task.cancel()
            await asyncio.sleep(0)
            reservation_task.cancel()
            await asyncio.sleep(0)
            assert not reservation_task.done()
            status_task = asyncio.create_task(store.status(GROUP, POLICY))
            close_task = asyncio.create_task(store.close())
            tasks.extend((status_task, close_task))
            # 事件循环仍可调度，读操作与关闭都必须等待预占事务结束。
            await asyncio.sleep(0)
            assert not status_task.done() and not close_task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await reservation_task
            assert (await status_task).reserved == 1
            await close_task
            assert not store.loaded
            await store.load(path)
            assert (await store.status(GROUP, POLICY)).unknown == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await store.close()

    asyncio.run(exercise())
    assert [row[1] for row in audit_rows(path)] == ["reserve", "recover"]


def test_cancelled_load_can_be_cancelled_again_during_cleanup_and_reused(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened_event, allow_open, closing_event, allow_close = Event(), Event(), Event(), Event()

    async def exercise() -> None:
        store = QuotaStore(clock=clock)
        original_open, original_close = store._open_sync, store._close_sync

        def delayed_open(path: Path) -> None:
            original_open(path)
            opened_event.set()
            assert allow_open.wait(5), "测试未释放数据库加载"

        def delayed_close() -> None:
            closing_event.set()
            assert allow_close.wait(5), "测试未释放数据库关闭"
            original_close()

        monkeypatch.setattr(store, "_open_sync", delayed_open)
        monkeypatch.setattr(store, "_close_sync", delayed_close)
        task = asyncio.create_task(store.load(tmp_path / "cancelled.sqlite3"))
        try:
            await wait_until_entered(opened_event)
            task.cancel()
            await asyncio.sleep(0)
            allow_open.set()
            await wait_until_entered(closing_event)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            allow_close.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not store.loaded
            await store.load(tmp_path / "reopened.sqlite3")
            assert (await store.status(GROUP, POLICY)).remaining == POLICY.limit
        finally:
            allow_open.set()
            allow_close.set()
            await asyncio.gather(task, return_exceptions=True)
            await store.close()

    asyncio.run(exercise())
