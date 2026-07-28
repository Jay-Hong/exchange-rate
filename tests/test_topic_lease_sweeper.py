"""만료 lease sweep — §C3 claim-then-notify.

registry와 **별도 모듈**로 먼저 만든다. registry를 그렇게 만들어 원인 분리가 됐던 것과 같은
이유다 — 문제가 났을 때 sweep인지 registry인지 갈려야 한다.

## 잠그는 계약

1. `claim(lock 안) → 통지 → 제거` 순서. 통지가 제거보다 먼저여야 클라가 모르는 사이
   구독이 사라지지 않는다(§C3).
2. **claim된 항목은 제거 전이라도 인가에서 즉시 제외**된다 — 아니면 통지하는 동안 live
   publish가 다시 통과한다(§C3).
3. 제거는 `(ws, topic, lease_id)` **CAS** — 갱신된 lease는 보존된다.
4. **sweeper의 lock 획득은 blocking이 아니다** — 경합하면 그 연결만 건너뛰고 다음 주기로
   미룬다. 무한 대기하면 역압 연결 K개에 대해 한 사이클이 5s×K 늘어나, **무관한 다른
   연결**의 통지가 D-const의 "만료→통지 10초" 관측 계약을 넘긴다(§C3).
5. 통지 실패·timeout → **그 소켓 전체 정리**(부분 상태 잔존 금지, §C3).
"""
import asyncio
import unittest

from app.strict_cache import StrictObservationCache
from app.topic_lease import LEASE_MAX_SECONDS
from app.topic_lease_registry import LockNotHeld, TopicLeaseRegistry
from app.topic_lease_sweeper import sweep_once

UID = "uid-1"
TOPIC = "krx:usd-krw-futures"
OTHER = "fx:usd-krw"
ISSUED_AT = 1000.0
EXPIRED_AT = ISSUED_AT + LEASE_MAX_SECONDS + 1


class _WS:
    def __init__(self, name="ws"):
        self.name = name

    def __repr__(self):
        return f"<WS {self.name}>"


async def _ok_ack(ack):
    return True


async def _ok_reauth(ws, claimed):
    """정상 sender — **`True` 반환이 계약이다**(§B4의 `send_ack`와 같은 이유)."""
    return True


async def _subscribe(registry, ws, topics, *, uid=UID, cache=None, now_mono=ISSUED_AT):
    cache = cache or StrictObservationCache()
    await registry.apply_subscribe(
        ws=ws, uid=uid, topics=topics, rejected_topics=(), snapshot=cache.snapshot(uid),
        cache=cache, now_mono=now_mono, premium_verified_at_mono=now_mono,
        identity_verified_at_mono=now_mono, send_ack=_ok_ack,
    )
    return cache


class TestClaimThenNotify(unittest.IsolatedAsyncioTestCase):
    """계약 1·3 — `claim → 통지 → 제거`, 제거는 CAS."""

    async def test_expired_lease_is_notified_then_removed(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        sent = []

        async def capture(target, claimed):
            sent.append((target, claimed))
            return True

        outcome = await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=capture)
        self.assertEqual(len(sent), 1)
        target, claimed = sent[0]
        self.assertIs(target, ws)
        self.assertEqual([c.topic for c in claimed], [TOPIC])
        self.assertTrue(claimed[0].lease_id)
        self.assertEqual(outcome.notified, 1)
        self.assertEqual(outcome.removed, 1)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=ISSUED_AT))

    async def test_unexpired_lease_is_untouched(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        sent = []

        async def capture(target, claimed):
            sent.append(claimed)
            return True

        outcome = await sweep_once(registry, now_mono=ISSUED_AT, send_reauth=capture)
        self.assertEqual(sent, [], "미만료 lease를 통지했다")
        self.assertEqual(outcome.notified, 0)
        self.assertIsNotNone(registry.authorized_lease(ws, TOPIC, now_mono=ISSUED_AT))

    async def test_only_expired_topics_of_a_mixed_connection_are_claimed(self):
        """한 연결에 만료·미만료가 섞이면 만료분만 간다 — 미만료를 지우면 살아 있는 구독이 사라진다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        cache = await _subscribe(registry, ws, [TOPIC], now_mono=ISSUED_AT)
        # OTHER는 훨씬 나중에 발급 → 같은 now에서 아직 살아 있다
        await _subscribe(registry, ws, [OTHER], cache=cache, now_mono=EXPIRED_AT)
        sent = []

        async def capture(target, claimed):
            sent.append([c.topic for c in claimed])
            return True

        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=capture)
        self.assertEqual(sent, [[TOPIC]])
        self.assertIsNotNone(registry.authorized_lease(ws, OTHER, now_mono=EXPIRED_AT))

    async def test_a_second_sweep_does_not_notify_the_same_lease_again(self):
        """성공한 주기는 lease를 제거하므로 다음 주기가 같은 것을 다시 통지하지 않는다.

        ⚠️ 이 성질을 지는 것은 **제거**이지 claim 표식이 아니다 — claim에 "이미 claim된 것은
        건너뛴다"를 넣어 이 성질을 만들려 하면, 취소 경로에서 좀비가 생긴다
        (`TestCancelledSweepSelfHeals` 참조).
        """
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        seen = []

        async def capture(target, claimed):
            seen.extend(c.lease_id for c in claimed)
            return True

        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=capture)
        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=capture)
        self.assertEqual(len(seen), 1, "같은 lease를 두 번 통지했다")

    async def test_nothing_expired_means_no_sender_call(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        calls = []

        async def counting(target, claimed):
            calls.append(target)
            return True

        await sweep_once(registry, now_mono=ISSUED_AT, send_reauth=counting)
        self.assertEqual(calls, [], "보낼 것이 없는데 sender를 불렀다")


class TestClaimedIsExcludedBeforeRemoval(unittest.IsolatedAsyncioTestCase):
    """계약 2 — ⛔ **claim된 항목은 제거 전이라도 인가에서 즉시 제외**된다(§C3).

    아니면 통지를 보내는 동안 live publish가 그 lease로 다시 통과한다. 통지와 제거 사이는
    `send_json` 하나만큼 벌어져 있고, 그 사이 tick이 최소 한 번은 지나간다.
    """

    async def test_claimed_lease_does_not_authorize_during_the_notification(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        lease = registry.authorized_lease(ws, TOPIC, now_mono=ISSUED_AT)
        observed = {}

        async def probing(target, claimed):
            # 통지 **도중** — 아직 제거 전이다.
            observed["lookup"] = registry.authorized_lease(ws, TOPIC, now_mono=ISSUED_AT)
            observed["send"] = registry.authorizes_send(
                ws, TOPIC, uid=UID, lease_id=lease.lease_id, now_mono=ISSUED_AT
            )
            return True

        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=probing)
        self.assertIsNone(observed["lookup"], "claim된 lease가 조회에 남아 있다")
        self.assertFalse(observed["send"], "claim된 lease가 전송을 인가한다")

    async def test_exclusion_uses_a_time_independent_axis(self):
        """⛔ 만료 시각으로만 거르면 부족하다 — 제외는 **claim 사실**이 근거여야 한다.

        `authorized_lease`는 호출자가 넘긴 `now`로 판정하므로, 통지 중 tick이 (조회 시점이
        만료 전인) 과거 시각을 쓰면 그대로 통과한다. claim 표식이 그 축을 닫는다.
        """
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        observed = {}

        async def probing(target, claimed):
            observed["past"] = registry.authorized_lease(ws, TOPIC, now_mono=ISSUED_AT)
            return True

        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=probing)
        self.assertIsNone(observed["past"])


class TestCancelledSweepSelfHeals(unittest.IsolatedAsyncioTestCase):
    """⛔ claim 후 통지 **도중 취소**되면 그 lease는 claim된 채 남는다.

    그때 "이미 claim됐으니 건너뛴다"는 가드가 있으면 통지도 제거도 영영 일어나지 않는다
    (실측: 인가에서만 빠진 좀비 — fail-closed지만 클라는 재인증 신호를 못 받아 자기 D6
    타이머까지 방치된다). 다음 주기가 **다시 통지**해야 한다.

    ⚠️ 중복 통지 우려는 이 설계에서 성립하지 않는다: 통지는 lock 안이고 실패·hang은 소켓
    정리로 끝나므로 claim이 남는 경로는 취소뿐이다. §D3상 클라는 자기가 들고 있는 `lease_id`와
    일치하는 통지를 그대로 적용하면 된다.
    """

    async def _cancel_mid_notification(self, registry, ws):
        entered = asyncio.Event()

        async def stalling(target, claimed):
            entered.set()
            await asyncio.sleep(60)
            return True

        task = asyncio.create_task(
            sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=stalling)
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_next_cycle_retries_the_notification(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        await self._cancel_mid_notification(registry, ws)

        sent = []

        async def capture(target, claimed):
            sent.extend(c.topic for c in claimed)
            return True

        outcome = await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=capture)
        self.assertEqual(sent, [TOPIC], "취소된 통지가 다시 시도되지 않았다(좀비)")
        self.assertEqual(outcome.removed, 1)

    async def test_the_lease_stays_excluded_while_it_is_unresolved(self):
        """복구 전까지는 fail-closed여야 한다 — 취소됐다고 인가가 되살아나면 안 된다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        await self._cancel_mid_notification(registry, ws)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=ISSUED_AT))

    async def test_the_connection_lock_is_released_by_the_cancellation(self):
        """⛔ 취소가 lock을 남기면 그 연결의 모든 전이가 막힌다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        await self._cancel_mid_notification(registry, ws)
        self.assertFalse(registry.connection_lock(ws).locked())


class TestLockAcquisitionIsBounded(unittest.IsolatedAsyncioTestCase):
    """계약 4 — ⛔ sweeper가 연결 lock을 **무한 대기하면 안 된다**.

    ack이 최대 `ACK_TIMEOUT_SECONDS` 동안 그 lock을 쥐므로, 순회하며 무한 대기하면 역압
    연결 K개에 대해 한 사이클이 5s×K 늘어난다 → **무관한 다른 연결**의 만료 통지가
    "만료→통지 10초" 관측 계약을 넘긴다.
    """

    async def test_busy_connection_is_skipped_and_others_still_swept(self):
        registry = TopicLeaseRegistry()
        busy, free = _WS("busy"), _WS("free")
        await _subscribe(registry, busy, [TOPIC])
        await _subscribe(registry, free, [TOPIC], uid="uid-2")
        released = asyncio.Event()
        swept = []

        async def holder():
            async with registry.connection_lock(busy):
                await released.wait()

        held = asyncio.create_task(holder())
        while not registry.connection_lock(busy).locked():
            await asyncio.sleep(0)

        async def capture(target, claimed):
            swept.append(target)
            return True

        try:
            outcome = await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=capture,
                                       lock_timeout=0.02)
            self.assertEqual(swept, [free], "경합 연결에서 head-of-line 지연이 생겼다")
            self.assertEqual(outcome.skipped_busy, 1)
        finally:
            released.set()
            await asyncio.gather(held, return_exceptions=True)

    async def test_a_timed_out_acquire_does_not_leak_the_lock(self):
        """⛔ 실패한 획득이 lock을 쥔 채로 남으면 그 연결이 영구 정지한다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        released = asyncio.Event()

        async def holder():
            async with registry.connection_lock(ws):
                await released.wait()

        held = asyncio.create_task(holder())
        while not registry.connection_lock(ws).locked():
            await asyncio.sleep(0)
        try:
            await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=_ok_reauth,
                             lock_timeout=0.02)
        finally:
            released.set()
            await asyncio.gather(held, return_exceptions=True)
        self.assertFalse(registry.connection_lock(ws).locked(), "실패한 획득이 lock을 남겼다")

    async def test_skipped_connection_is_swept_on_the_next_cycle(self):
        """지연 상한이 sweep 주기 **1회**로 유지된다 — 건너뛴 것은 다음 주기가 처리한다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        released = asyncio.Event()
        sent = []

        async def holder():
            async with registry.connection_lock(ws):
                await released.wait()

        held = asyncio.create_task(holder())
        while not registry.connection_lock(ws).locked():
            await asyncio.sleep(0)

        async def capture(target, claimed):
            sent.append(target)
            return True

        try:
            await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=capture, lock_timeout=0.02)
            self.assertEqual(sent, [])
        finally:
            released.set()
            await asyncio.gather(held, return_exceptions=True)
        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=capture, lock_timeout=0.02)
        self.assertEqual(sent, [ws])


class TestNotificationFailureCleansTheSocket(unittest.IsolatedAsyncioTestCase):
    """계약 5 — 통지 실패·timeout → **그 소켓 전체 정리**(부분 상태 잔존 금지, §C3)."""

    async def test_send_failure_terminates_that_connection_only(self):
        registry = TopicLeaseRegistry()
        doomed, healthy = _WS("doomed"), _WS("healthy")
        await _subscribe(registry, doomed, [TOPIC, OTHER])
        await _subscribe(registry, healthy, [TOPIC], uid="uid-2")

        async def failing(target, claimed):
            if target is doomed:
                raise ConnectionResetError("socket gone")
            return True

        outcome = await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=failing)
        self.assertEqual(outcome.terminated, 1)
        self.assertIsNone(registry.bound_uid(doomed), "부분 상태가 남았다")
        self.assertIsNone(registry.authorized_lease(doomed, OTHER, now_mono=ISSUED_AT))
        self.assertIsNone(registry.authorized_lease(healthy, TOPIC, now_mono=ISSUED_AT),
                          "정상 연결은 sweep이 처리했어야 한다")
        self.assertIsNotNone(registry.bound_uid(healthy), "남의 연결까지 정리했다")

    async def test_notification_timeout_terminates_the_connection(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])

        async def stalling(target, claimed):
            await asyncio.sleep(60)
            return True

        outcome = await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=stalling,
                                   notify_timeout=0.02)
        self.assertEqual(outcome.terminated, 1)
        self.assertIsNone(registry.bound_uid(ws))

    async def test_sender_that_does_not_confirm_is_a_failure(self):
        """§B4의 `send_ack`와 같은 계약 — 제어 흐름은 배달의 증거가 아니다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])

        async def silent(target, claimed):
            return None

        outcome = await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=silent)
        self.assertEqual(outcome.terminated, 1)


class TestClaimRequiresTheLock(unittest.IsolatedAsyncioTestCase):
    """⛔ claim은 상태 변경이다 — lock 없이 부르면 §B4 우회로가 된다."""

    async def test_claim_without_the_lock_raises(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        with self.assertRaises(LockNotHeld):
            registry.claim_expired_locked(ws, now_mono=EXPIRED_AT)

    async def test_removal_without_the_lock_raises(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        lease = registry.authorized_lease(ws, TOPIC, now_mono=ISSUED_AT)
        with self.assertRaises(LockNotHeld):
            registry.remove_locked(ws, TOPIC, lease.lease_id)


if __name__ == "__main__":
    unittest.main()
