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
   미룬다. 무한 대기하면 역압 연결 K개에 대해 한 사이클이 5s×K 늘어나, D-const 관측 지연
   식의 `(lock + notify + close)` 항이 **그 연결 자신의 몫**이 아니라 K배가 된다 —
   **무관한 다른 연결**의 통지 지연이 남의 역압에 좌우된다(§C3).
   ⛔ D-const에 "만료→통지 10초" 같은 **상한은 없다**(유한 상한이 아니다). 여기서 지키는
   것은 상한이 아니라 **연결 간 격리**다.
5. 통지 실패·timeout → **그 소켓 전체 정리**(부분 상태 잔존 금지, §C3).
"""
import asyncio
import unittest

from app.strict_cache import StrictObservationCache
from app.strict_wire import Accepted
from app.topic_lease import LEASE_MAX_SECONDS
from app.topic_lease_registry import (
    ConnectionLockBusy,
    LockNotHeld,
    ReentrantRegistryCall,
    TopicLeaseRegistry,
)
from app.topic_lease_sweeper import (
    CLOSE_TIMEOUT_SECONDS,
    LOCK_ACQUIRE_TIMEOUT_SECONDS,
    NOTIFY_TIMEOUT_SECONDS,
    sweep_once as _sweep_once,
)


def _auth(cache, uid, at_mono):
    """§B4 검증 묶음 — **실제** `strict_wire.Accepted`(테스트 double 아님)."""
    return Accepted(
        snapshot=cache.snapshot(uid), premium_verified_at_mono=at_mono,
        identity_verified_at_mono=at_mono, uid=uid,
    )

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


async def _noop_close(ws):
    """소켓 close는 배선의 I/O다 — 테스트는 호출 사실만 본다."""


async def sweep_once(registry, **kwargs):
    """`close_connection`을 기본 제공하는 테스트 wrapper (필수 인자 자체는 별도 테스트가 잠근다)."""
    kwargs.setdefault("close_connection", _noop_close)
    return await _sweep_once(registry, **kwargs)


async def _subscribe(registry, ws, topics, *, uid=UID, cache=None, now_mono=ISSUED_AT):
    cache = cache or StrictObservationCache()
    await registry.apply_subscribe(
        ws=ws, topics=topics, rejected_topics=(), cache=cache, now_mono=now_mono,
        authorization=_auth(cache, uid, now_mono), send_ack=_ok_ack,
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
    연결 K개에 대해 한 사이클이 5s×K 늘어난다 → **무관한 다른 연결**의 만료 통지 지연이
    남의 역압에 비례한다. ⛔ 넘는 대상이 되는 "10초 상한"은 D-const에 **없다** —
    깨지는 것은 상한이 아니라 **연결 간 격리**다.
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
        """건너뛴 연결은 **다음 주기가 다시 시도한다**.

        ⚠️ 이 테스트가 잠그는 것은 **재시도**이지 지연 **상한**이 아니다 — 여기서는 보유자가
        놓아 주지만, 실제로는 lock 획득에 공정성·aging이 없어 점유가 이어지면 **연속 skip 횟수에
        상한이 없다**. 상한이 필요하면 escalation(n회 연속 시 blocking 획득 또는 강제 teardown)을
        계약으로 세워야 한다.
        """
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


class TestStaleClaimNeverBlocksANewLease(unittest.IsolatedAsyncioTestCase):
    """⛔ claim은 **topic이 아니라 lease**에 걸린다.

    취소로 L1 claim이 남은 뒤 재인증이 L2를 발급하면, topic 존재만 보는 제외는 **유효한 새
    lease를 영구 차단**한다(실측: L2 인가 None, 다음 sweep은 미만료 L2를 claim하지 않아
    notified=0·removed=0으로 복구도 없다). 직전 좀비보다 나쁘다 — 그쪽은 만료된 lease였고
    이쪽은 방금 정당하게 발급된 lease다.
    """

    async def _stale_claim(self, registry, ws):
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

    async def test_reissued_lease_authorizes_despite_a_stale_claim(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        cache = await _subscribe(registry, ws, [TOPIC])
        await self._stale_claim(registry, ws)
        await _subscribe(registry, ws, [TOPIC], cache=cache, now_mono=EXPIRED_AT)
        self.assertIsNotNone(
            registry.authorized_lease(ws, TOPIC, now_mono=EXPIRED_AT),
            "재발급된 lease가 구 claim에 막혔다",
        )

    async def test_reissued_unexpired_lease_is_not_notified(self):
        """⚠️ 구 이름은 `test_reissue_clears_the_stale_claim`이었는데 **코드가 하지 않는 계약을
        이름으로 광고**했다(청소 로직은 잉여로 제거했고, 이 단언은 청소 유무와 무관하다 —
        양방향 무감지 실측). 실제로 잠그는 것으로 개명한다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        cache = await _subscribe(registry, ws, [TOPIC])
        await self._stale_claim(registry, ws)
        await _subscribe(registry, ws, [TOPIC], cache=cache, now_mono=EXPIRED_AT)

        sent = []

        async def capture(target, claimed):
            sent.extend(c.topic for c in claimed)
            return True

        # 새 lease는 아직 미만료 → 통지 대상이 아니어야 한다
        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=capture)
        self.assertEqual(sent, [])
        self.assertIsNotNone(registry.authorized_lease(ws, TOPIC, now_mono=EXPIRED_AT))

    async def test_evicted_topic_does_not_leave_a_blocking_claim(self):
        """§C2 eviction으로 사라진 topic의 claim도 나중 재구독을 막으면 안 된다.

        ⚠️ 구 버전은 §C1 cross-UID purge로 이 상황을 만들었는데, B5(d) 결정으로 그 경로가
        연결 종료가 됐다(purge 폐기). `apply_subscribe`에서는 eviction이 유일한 축소 producer다
        (`apply_unsubscribe`도 `removed`를 채우지만 그건 별 진입점이다).
        """
        registry = TopicLeaseRegistry()
        ws = _WS()
        cache = await _subscribe(registry, ws, [TOPIC])
        await self._stale_claim(registry, ws)
        # 권한 상실로 evict → 나중에 다시 구독
        await registry.apply_subscribe(
            ws=ws, topics=[], rejected_topics=[TOPIC], cache=cache, now_mono=EXPIRED_AT,
            authorization=_auth(cache, UID, EXPIRED_AT), send_ack=_ok_ack,
        )
        await _subscribe(registry, ws, [TOPIC], cache=cache, now_mono=EXPIRED_AT)
        self.assertIsNotNone(registry.authorized_lease(ws, TOPIC, now_mono=EXPIRED_AT))


class TestLockOwnershipIsChecked(unittest.IsolatedAsyncioTestCase):
    """⛔ `asyncio.Lock.locked()`는 **누가** 쥐었는지 모른다 — 다른 task가 쥐어도 True다.

    실측: task A가 lock을 쥔 동안 task B의 `claim_expired_locked()`가 성공했다. 즉 lock 보유
    검사가 장식이었고 public primitive가 그대로 §B4 우회로였다. 소유 task를 추적해야 한다.
    """

    async def test_another_tasks_lock_does_not_satisfy_the_check(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        released = asyncio.Event()
        result = {}

        async def holder():
            async with registry.hold_connection_lock(ws):
                await released.wait()

        held = asyncio.create_task(holder())
        while not registry.connection_lock(ws).locked():
            await asyncio.sleep(0)

        async def intruder():
            try:
                registry.claim_expired_locked(ws, now_mono=EXPIRED_AT)
                result["bypassed"] = True
            except LockNotHeld:
                result["blocked"] = True

        await asyncio.create_task(intruder())
        released.set()
        await held
        self.assertEqual(result, {"blocked": True}, "남의 lock으로 §B4를 우회했다")

    async def test_the_owning_task_passes(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        async with registry.hold_connection_lock(ws):
            self.assertEqual(len(registry.claim_expired_locked(ws, now_mono=EXPIRED_AT)), 1)

    async def test_ownership_does_not_outlive_the_block(self):
        """⛔ 소유 기록이 남으면 lock을 놓은 뒤에도 `*_locked`가 통과한다 — 검사가 무의미해진다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        async with registry.hold_connection_lock(ws):
            pass
        with self.assertRaises(LockNotHeld):
            registry.claim_expired_locked(ws, now_mono=EXPIRED_AT)

    async def test_busy_lock_raises_a_dedicated_error(self):
        """⛔ `asyncio.TimeoutError`로 알리면 본문이 던진 timeout과 구별되지 않는다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        released = asyncio.Event()

        async def holder():
            async with registry.hold_connection_lock(ws):
                await released.wait()

        held = asyncio.create_task(holder())
        while not registry.connection_lock(ws).locked():
            await asyncio.sleep(0)
        try:
            with self.assertRaises(ConnectionLockBusy):
                async with registry.hold_connection_lock(ws, timeout=0.02):
                    pass
        finally:
            released.set()
            await held

    async def test_ownership_and_lock_are_released_on_cancellation(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        entered = asyncio.Event()

        async def holder():
            async with registry.hold_connection_lock(ws):
                entered.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(holder())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(registry.connection_lock(ws).locked(), "lock이 남았다")
        async with registry.hold_connection_lock(ws):
            pass                                   # 소유 기록도 정리됐어야 재획득이 정상 동작


class TestTerminationClosesTheSocket(unittest.IsolatedAsyncioTestCase):
    """⛔ registry 정리만으로는 §C3의 "소켓 전체 정리"가 아니다 — 실제 close 경로가 필요하다.

    §B2a도 "전송 실패는 연결이 죽은 것으로 간주하고 **소켓을 닫는다**"이다. registry는 I/O를
    하지 않으므로 close는 주입받아야 하고, `send_ack`와 같은 이유로 **필수 인자**다.
    """

    async def test_close_is_called_for_the_failed_connection(self):
        registry = TopicLeaseRegistry()
        doomed, healthy = _WS("doomed"), _WS("healthy")
        await _subscribe(registry, doomed, [TOPIC])
        await _subscribe(registry, healthy, [TOPIC], uid="uid-2")
        closed = []

        async def failing(target, claimed):
            if target is doomed:
                raise ConnectionResetError()
            return True

        async def closer(target):
            closed.append(target)

        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=failing,
                         close_connection=closer)
        self.assertEqual(closed, [doomed], "실패한 연결만 닫아야 한다")

    async def test_close_runs_after_the_lock_is_released(self):
        """⛔ lock을 쥔 채 close하면 stalled transport에서 그 연결의 모든 전이가 막힌다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        observed = {}

        async def failing(target, claimed):
            raise ConnectionResetError()

        async def closer(target):
            observed["locked"] = registry.connection_lock(target).locked()

        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=failing,
                         close_connection=closer)
        self.assertFalse(observed.get("locked", True), "close가 lock 안에서 실행됐다")

    async def test_a_failing_close_does_not_abort_the_cycle(self):
        registry = TopicLeaseRegistry()
        first, second = _WS("a"), _WS("b")
        await _subscribe(registry, first, [TOPIC])
        await _subscribe(registry, second, [TOPIC], uid="uid-2")

        async def failing(target, claimed):
            raise ConnectionResetError()

        async def bad_close(target):
            raise OSError("already gone")

        outcome = await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=failing,
                                   close_connection=bad_close)
        self.assertEqual(outcome.terminated, 2, "close 실패가 사이클을 끊었다")
        self.assertEqual(outcome.close_failed, 2)

    async def test_close_connection_is_mandatory(self):
        """§B4의 `send_ack`와 같은 논리 — 선택으로 두면 배선이 한 번 빠뜨렸을 때 조용히 안 닫힌다."""
        registry = TopicLeaseRegistry()
        with self.assertRaises(TypeError):
            await _sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=_ok_reauth)


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

    async def test_late_confirmation_after_the_budget_is_not_success(self):
        """⛔ 예산이 지난 뒤 도착한 `True`를 성공으로 인정하면 안 된다.

        `wait_for`는 callback이 취소를 삼키면 **예산 시점에 반환하지 않고**, 늦게 온 `True`를
        정상 반환값으로 준다(실측: 예산 0.01s인데 0.05s 뒤 완료, 반환 True). 그러면 통지가
        실제로는 나가지 못했는데 lease가 제거된다 — 클라는 재인증 신호를 못 받는다.
        `asyncio.timeout(...).expired()`가 **시계를 읽지 않고** 그 경우를 구별한다.
        """
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        release = asyncio.Event()

        async def swallowing(target, claimed):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await release.wait()          # 취소를 삼키고 계속 산다
                return True                   # 예산이 지난 뒤의 성공 보고
            return True

        async def noop_close(target):
            pass

        sweep = asyncio.create_task(
            sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=swallowing,
                       close_connection=noop_close, notify_timeout=0.01, close_timeout=0.05)
        )
        await asyncio.sleep(0.03)
        release.set()
        outcome = await asyncio.wait_for(sweep, timeout=5)
        self.assertEqual(outcome.notified, 0, "예산 뒤 도착한 True를 성공으로 인정했다")
        self.assertEqual(outcome.removed, 0, "통지도 못 했는데 lease를 제거했다")
        self.assertEqual(outcome.terminated, 1)

    async def test_late_close_after_the_budget_is_counted_as_failure(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        release = asyncio.Event()

        async def failing(target, claimed):
            raise ConnectionResetError()

        async def swallowing_close(target):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await release.wait()

        sweep = asyncio.create_task(
            sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=failing,
                       close_connection=swallowing_close, close_timeout=0.01)
        )
        await asyncio.sleep(0.03)
        release.set()
        outcome = await asyncio.wait_for(sweep, timeout=5)
        self.assertEqual(outcome.close_failed, 1, "예산 뒤 끝난 close를 성공으로 봤다")

    async def test_swallowed_external_cancellation_does_not_remove_the_lease(self):
        """⛔ 취소된 통지를 성공으로 보고 구독을 지우면 클라는 재인증 신호를 못 받는다.

        실측(구 구현): gather는 `CancelledError`를 올렸지만 **자식이 그 전에 lease를 제거**했다.
        바깥 계약만 보면 정상이라 눈에 띄지 않는다.
        """
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        entered = asyncio.Event()

        async def swallowing(target, claimed):
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return True
            return True

        async def noop_close(target):
            pass

        task = asyncio.create_task(
            sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=swallowing,
                       close_connection=noop_close)
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn(TOPIC, registry.topics_for_test(ws),
                      "취소된 통지를 성공으로 보고 구독을 지웠다")

    async def test_swallowed_cancellation_wins_over_a_later_exception(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        entered = asyncio.Event()

        async def swallow_then_raise(target, claimed):
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise ConnectionResetError("소켓도 죽었다")

        async def noop_close(target):
            pass

        task = asyncio.create_task(
            sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=swallow_then_raise,
                       close_connection=noop_close)
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn(TOPIC, registry.topics_for_test(ws), "취소 중에 구독을 지웠다")

    async def test_sender_that_does_not_confirm_is_a_failure(self):
        """§B4의 `send_ack`와 같은 계약 — 제어 흐름은 배달의 증거가 아니다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])

        async def silent(target, claimed):
            return None

        outcome = await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=silent)
        self.assertEqual(outcome.terminated, 1)


class TestTerminationIsBounded(unittest.IsolatedAsyncioTestCase):
    """⛔ terminate 경로가 모듈 자신의 교리를 깨면 안 된다 — lock 0.05s · notify 5.0s인데
    close만 무제한이면 D-const 관측 지연 식의 `close` 항이 **무한**이 되어, 그 연결의 통지
    지연뿐 아니라(§C3) 식 자체가 의미를 잃는다. (D-const에 "10초 상한"은 없다.)

    실측: 응답 없는 피어 하나로 사이클이 25초 안에 끝나지 않았고 `close_failed`도 0이라
    telemetry에 보이지도 않았다. 운영 상한 근거: uvicorn은 websockets `close_timeout`
    기본 10s를 쓰고 `close()`는 최악 4×close_timeout이 걸릴 수 있다.
    """

    async def test_a_stalled_close_does_not_hang_the_cycle(self):
        registry = TopicLeaseRegistry()
        doomed, victim = _WS("doomed"), _WS("victim")
        await _subscribe(registry, doomed, [TOPIC])
        await _subscribe(registry, victim, [TOPIC], uid="uid-2")
        notified = []

        async def reauth(target, claimed):
            if target is doomed:
                raise ConnectionResetError()
            notified.append(target)
            return True

        async def stalling_close(target):
            await asyncio.sleep(60)

        outcome = await asyncio.wait_for(
            sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=reauth,
                       close_connection=stalling_close, close_timeout=0.05),
            timeout=5,
        )
        self.assertEqual(outcome.close_failed, 1, "멈춘 close가 계수되지 않았다")
        self.assertEqual(notified, [victim], "멈춘 close가 무관한 연결의 통지를 막았다")

    async def test_notifications_do_not_accumulate_across_connections(self):
        """⛔ **통지**가 직렬이면 사이클이 연결 수에 비례한다 — 격리가 깨지는 축이 이것이다.

        실측(구 구현): 4연결의 통지 시작이 0.000 / 0.051 / 0.102 / 0.154초로 누적됐다.
        기본값(notify 5s)이면 정지 연결 3개만으로 마지막 연결의 통지가 15초 뒤로 밀린다 —
        자기 잘못이 아닌데도. (넘는 대상이 되는 "10초 계약"은 D-const에 **없다**.)
        lock 획득 상한도 같이 누적된다.

        ⚠️ 연결별 작업은 **서로 다른 lock**을 잡으므로 병렬화해도 직렬화 계약을 깨지 않는다.
        """
        registry = TopicLeaseRegistry()
        sockets = [_WS(f"s{i}") for i in range(4)]
        for index, ws in enumerate(sockets):
            await _subscribe(registry, ws, [TOPIC], uid=f"uid-{index}")

        loop = asyncio.get_running_loop()
        started_at = []

        async def stalling(target, claimed):
            started_at.append(loop.time())
            await asyncio.sleep(60)
            return True

        async def noop_close(target):
            pass

        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=stalling,
                         close_connection=noop_close, notify_timeout=0.05, close_timeout=0.05)
        self.assertEqual(len(started_at), 4)
        spread = max(started_at) - min(started_at)
        self.assertLess(spread, 0.05,
                        f"통지가 직렬로 누적됐다 (시작 시각 편차 {spread:.3f}s)")

    async def test_stalled_closes_do_not_accumulate_across_connections(self):
        """⛔ 직렬이면 K개가 합산돼 사이클이 주기를 넘긴다 — close는 연결 간 순서가 없다."""
        registry = TopicLeaseRegistry()
        sockets = [_WS(f"s{i}") for i in range(4)]
        for index, ws in enumerate(sockets):
            await _subscribe(registry, ws, [TOPIC], uid=f"uid-{index}")

        async def failing(target, claimed):
            raise ConnectionResetError()

        async def stalling_close(target):
            await asyncio.sleep(60)

        loop = asyncio.get_running_loop()
        started = loop.time()
        outcome = await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=failing,
                                   close_connection=stalling_close, close_timeout=0.1)
        elapsed = loop.time() - started
        self.assertEqual(outcome.terminated, 4)
        self.assertLess(elapsed, 0.35, f"close가 직렬로 합산됐다 ({elapsed:.2f}s)")


class TestNoVisitOutlivesTheCycle(unittest.IsolatedAsyncioTestCase):
    """⛔ `sweep_once`가 끝났는데 살아 있는 방문 task가 있으면 다음 주기와 **겹친다**.

    기본 `asyncio.gather`는 첫 예외를 즉시 전파하고 나머지를 **취소하지도 대기하지도 않는다**.
    실측: 한 연결의 programming error로 `sweep_once`가 예외 종료한 시점에 sibling은 여전히
    lock을 쥐고 있었고, 그 뒤에 lease 제거까지 수행했다 — 중복 통지·claim 경쟁의 씨앗이다.

    ⚠️ sibling **취소**가 아니라 **완료 대기**를 택했다: 취소는 성공 직전의 통지까지 죽이고,
    모든 `_visit`이 상한(lock·notify·close)을 갖는다는 것은 **조건부**다 — 주입된 callback이
    취소에 협조할 때만 성립한다(비협조 callback은 순수 asyncio로 강제 종료할 수 없다).
    """

    async def _two_connections(self):
        registry = TopicLeaseRegistry()
        boom, slow = _WS("boom"), _WS("slow")
        await _subscribe(registry, boom, [TOPIC], uid="uid-boom")
        await _subscribe(registry, slow, [TOPIC], uid="uid-slow")
        return registry, boom, slow

    @staticmethod
    def _explode_on(registry, targets):
        original = registry.claim_expired_locked

        def exploding(ws, *, now_mono):
            if ws in targets:
                raise RuntimeError(f"programming error: {ws!r}")
            return original(ws, now_mono=now_mono)

        registry.claim_expired_locked = exploding

    async def test_siblings_finish_before_the_failure_surfaces(self):
        registry, boom, slow = await self._two_connections()
        self._explode_on(registry, {boom})
        finished = []

        async def slow_reauth(target, claimed):
            await asyncio.sleep(0.05)
            finished.append(target)
            return True

        async def noop_close(target):
            pass

        with self.assertRaises(RuntimeError):
            await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=slow_reauth,
                             close_connection=noop_close)
        self.assertEqual(finished, [slow], "sibling이 아직 진행 중인데 사이클이 끝났다")
        self.assertFalse(registry.connection_lock(slow).locked(),
                         "sibling이 lock을 쥔 채 사이클 밖으로 살아남았다")

    async def test_structural_exceptions_are_not_downgraded_into_a_group(self):
        """⛔ `CancelledError`를 group으로 묶으면 asyncio의 취소 전파가 깨진다.

        `SystemExit`·`KeyboardInterrupt`도 같다 — 묶으면 프로세스 종료가 막힌다.
        registry의 `ConnectionTerminatedError` 감싸기에서 `Exception`만 감싼 것과 같은 규칙이다.
        """
        registry, boom, slow = await self._two_connections()
        original = registry.claim_expired_locked

        def exploding(ws, *, now_mono):
            if ws is boom:
                raise asyncio.CancelledError()
            if ws is slow:
                raise RuntimeError("평범한 실패")
            return original(ws, now_mono=now_mono)

        registry.claim_expired_locked = exploding

        async def noop_close(target):
            pass

        with self.assertRaises(asyncio.CancelledError):
            await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=_ok_reauth,
                             close_connection=noop_close)

    async def test_every_failure_is_surfaced_not_just_the_first(self):
        """⛔ 여럿이 실패했는데 하나만 던지면 나머지 진단이 조용히 사라진다."""
        registry, boom, slow = await self._two_connections()
        self._explode_on(registry, {boom, slow})

        async def noop_close(target):
            pass

        with self.assertRaises(BaseExceptionGroup) as caught:
            await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=_ok_reauth,
                             close_connection=noop_close)
        self.assertEqual(len(caught.exception.exceptions), 2)


class TestTeardownPrecedesClose(unittest.IsolatedAsyncioTestCase):
    """⛔ 순서(정리 → close)는 주석이 아니라 **계약**이어야 한다.

    실측: 순서를 뒤집은 mutant가 30 테스트를 전부 통과했다. 그런데 차이는 관측 가능하다 —
    close가 진행되는 동안(최악 수십 초) close-먼저 변형은 살아 있는 다른 topic의 전송을
    **계속 인가**한다. 그게 `authorized_lease`가 tombstone을 즉시 fail-closed로 만든 이유다.
    """

    async def test_state_is_already_fail_closed_when_close_runs(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        cache = await _subscribe(registry, ws, [TOPIC], now_mono=ISSUED_AT)
        await _subscribe(registry, ws, [OTHER], cache=cache, now_mono=EXPIRED_AT)
        live = registry.authorized_lease(ws, OTHER, now_mono=EXPIRED_AT)
        observed = {}

        async def failing(target, claimed):
            raise ConnectionResetError()

        async def closer(target):
            observed["uid"] = registry.bound_uid(target)
            observed["send"] = registry.authorizes_send(
                target, OTHER, uid=UID, lease_id=live.lease_id, now_mono=EXPIRED_AT
            )

        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=failing,
                         close_connection=closer)
        self.assertIsNone(observed["uid"], "close 시점에 아직 정리되지 않았다")
        self.assertFalse(observed["send"], "죽었다고 판정한 소켓이 여전히 인가된다")

    async def test_no_lease_can_be_issued_in_the_termination_window(self):
        """⛔ 정리를 위해 lock을 **다시 잡으면** 그 사이 경쟁 subscribe가 900초 lease를 받는다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        observed = {}

        async def failing(target, claimed):
            raise ConnectionResetError()

        async def closer(target):
            observed["result"] = await _subscribe_result(registry, target)

        await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=failing,
                         close_connection=closer)
        self.assertEqual(type(observed["result"]).__name__, "ConnectionTerminated")


class TestReentrancyIsLoudEverywhere(unittest.IsolatedAsyncioTestCase):
    """⛔ 재진입 감지가 ack 창에서만 동작하면 sweep·unsubscribe 경로는 **조용히 hang**한다.

    실측: 다음 슬라이스(§C-API 5 `apply_unsubscribe`)의 가장 자연스러운 배선
    `async with hold_connection_lock(ws): await registry.remove(...)`가 영구 정지했다.
    lock 소유 기록이 이미 있는데 재진입 가드가 그걸 안 봤다.
    """

    async def test_registry_call_inside_a_held_lock_raises(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        lease = registry.authorized_lease(ws, TOPIC, now_mono=ISSUED_AT)
        async with registry.hold_connection_lock(ws):
            with self.assertRaises(ReentrantRegistryCall):
                await asyncio.wait_for(registry.remove(ws, TOPIC, lease.lease_id), timeout=2)

    async def test_nested_hold_by_the_same_task_raises(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        async with registry.hold_connection_lock(ws):
            with self.assertRaises(ReentrantRegistryCall):
                await asyncio.wait_for(
                    _enter_nested(registry, ws), timeout=2
                )

    async def test_non_positive_timeout_is_rejected(self):
        """⛔ `timeout=0`은 "즉시 한 번 시도"가 아니라 **항상 busy**다(wait_for 특수 경로).

        그러면 전 연결이 skip되어 통지가 영영 안 나가는데 로그에는 "경합 중"으로만 보인다.
        """
        registry = TopicLeaseRegistry()
        with self.assertRaises(ValueError):
            async with registry.hold_connection_lock(_WS(), timeout=0):
                pass

    async def test_body_raised_busy_is_not_counted_as_contention(self):
        """⛔ 획득 실패와 본문 예외를 같은 handler가 잡으면 거짓 skip이 된다."""
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])

        async def raises_busy(target, claimed):
            raise ConnectionLockBusy("본문에서 발생")

        outcome = await sweep_once(registry, now_mono=EXPIRED_AT, send_reauth=raises_busy)
        self.assertEqual(outcome.skipped_busy, 0, "본문 예외가 경합으로 집계됐다")
        self.assertEqual(outcome.terminated, 1)


async def _enter_nested(registry, ws):
    async with registry.hold_connection_lock(ws):
        return None


async def _subscribe_result(registry, ws, *, uid="uid-9", now_mono=EXPIRED_AT):
    cache = StrictObservationCache()
    return await registry.apply_subscribe(
        ws=ws, topics=[OTHER], rejected_topics=(), cache=cache, now_mono=now_mono,
        authorization=_auth(cache, uid, now_mono), send_ack=_ok_ack,
    )


class TestClaimMarkHousekeeping(unittest.IsolatedAsyncioTestCase):
    """⛔ 표식 청소는 "(b) 청소 로직을 뺀" 결정의 **근거로 인용된 줄**이다 — 잠겨 있어야 한다."""

    async def test_removal_clears_the_claim_mark(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        async with registry.hold_connection_lock(ws):
            claimed = registry.claim_expired_locked(ws, now_mono=EXPIRED_AT)
            self.assertTrue(registry.claimed_topics(ws))
            self.assertTrue(registry.remove_locked(ws, claimed[0].topic, claimed[0].lease_id))
            self.assertEqual(registry.claimed_topics(ws), (),
                             "제거했는데 claim 표식이 남았다")


class TestClaimRequiresTheLock(unittest.IsolatedAsyncioTestCase):
    """⛔ claim은 상태 변경이다 — lock 없이 부르면 §B4 우회로가 된다."""

    async def test_claim_without_the_lock_raises(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        with self.assertRaises(LockNotHeld):
            registry.claim_expired_locked(ws, now_mono=EXPIRED_AT)

    async def test_teardown_without_the_lock_raises(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        with self.assertRaises(LockNotHeld):
            registry.teardown_locked(ws)

    def test_busy_handler_does_not_wrap_the_body(self):
        """⛔ 획득 실패 포착이 본문까지 감싸면 본문의 `ConnectionLockBusy`가 **거짓 skip**이 된다.

        ⚠️ 오늘은 본문에 lock 획득이 없어 **관측 가능한 차이가 없다** — 행동 테스트로는 잡히지
        않는다(실측: 구 형태로 되돌려도 전부 green). 그래서 잠그는 대상은 동작이 아니라 **형태**다:
        `except ConnectionLockBusy`가 붙은 try에는 획득만 들어간다. 다음 슬라이스가 본문에
        획득을 하나 추가하는 순간(예: cross-connection 정리) 이 구조가 유일한 방어다.
        """
        import ast
        import inspect

        import app.topic_lease_sweeper as module

        tree = ast.parse(inspect.getsource(module))
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "sweep_once")
        guarded = [
            node for node in ast.walk(func) if isinstance(node, ast.Try)
            and any(h.type is not None and getattr(h.type, "id", None) == "ConnectionLockBusy"
                    for h in node.handlers)
        ]
        self.assertEqual(len(guarded), 1, "busy handler를 못 찾았다 — 탐지기 고장")
        # ⚠️ try **본문 전체**를 훑는다. 한때 `body[0]`(첫 문장)만 봐서, 두 번째 문장으로
        #    추가된 호출을 놓쳤다(실측 SURVIVED) — 탐지기가 자기 범위를 좁게 잡은 사례다.
        guarded_calls = {
            node.func.attr
            for statement in guarded[0].body
            for node in ast.walk(statement)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        # 자기 검사 — 획득 호출조차 못 찾으면 아래 단언이 **공허하게** 통과한다.
        self.assertIn("enter_async_context", guarded_calls, "탐지기가 획득 호출을 못 찾았다")
        for forbidden in ("claim_expired_locked", "remove_locked", "teardown_locked"):
            self.assertNotIn(forbidden, guarded_calls,
                             f"busy 포착이 본문({forbidden})을 감쌌다")

    async def test_removal_without_the_lock_raises(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        await _subscribe(registry, ws, [TOPIC])
        lease = registry.authorized_lease(ws, TOPIC, now_mono=ISSUED_AT)
        with self.assertRaises(LockNotHeld):
            registry.remove_locked(ws, TOPIC, lease.lease_id)


class TestSweeperConstantsArePinned(unittest.TestCase):
    """실측(2026-07-29): pin 없이 `NOTIFY 5.0→60.0` / `LOCK_ACQUIRE 0.05→5.0` /
    `CLOSE 5.0→60.0` 셋 다 전체 스위트 4192 passed로 **생존**했다. 여기가 그 red다.

    ⚠️ 이유는 "행동 테스트가 timeout을 주입해서"가 **아니다**(구 docstring이 그렇게 적었고
    틀렸다). 실측: 이 파일의 `sweep_once(` 호출 36건 중 **26건은 timeout kwarg를 하나도 주지
    않아 기본값으로 돈다**. 기본값을 안 보는 게 아니라, 행동 테스트가 **값에 둔감**한 것이다 —
    양수이기만 하면 통과한다. 실측(이 파일, 전부 pin 1건만 red / 51 passed):
    `LOCK_ACQUIRE 0.05 → 0.0005 · 5.0 · 60.0 · 600.0`.

    이유: **경합을 만드는 테스트는 기본값을 쓰지 않는다** — `lock_timeout=0.02`를 명시
    주입한다(4곳). 기본값으로 도는 26건은 경합이 없어 값이 얼마든 즉시 획득한다.

    ⛔ 단 `0.0` 이하는 예외다 — `hold_connection_lock`이 ValueError로 거부해 스위트가
    **hang**한다(실측: 100초 내 미종료). red가 아니라 hang이라 원인 파악이 더 어렵다.
    즉 "양수인 한 이 pin이 유일한 red"이지, 무조건 유일한 방어는 아니다.

    ⛔ 생존은 **런타임 결함이 아니었다** — "기본값 변경을 탐지하지 못했다"는 뜻일 뿐이다.
    게다가 `sweep_once`는 아직 **프로덕션 호출자가 0**이라(배선 전) 이 기본값들로 실제로 도는
    코드는 없다. pin은 배선 시점에 쓰일 값을 잠글 뿐, "운영 파라미터가 검증됐다"는 뜻이 아니다.

    ⛔ 그리고 **코드 방향만** 잠근다. D-const 표의 값을 고쳐도 red는 나지 않는다(doc→code 비구속).
    일치를 강제할 뿐 **정확성**을 강제하지도 않는다 — red가 나면 "어느 쪽이 맞나"를 판단해라.
    코드에 맞춰 이 리터럴을 고치는 습관이 들면 이 테스트는 드리프트 은폐 도구가 된다.
    """

    def test_lock_acquire_timeout_is_pinned(self):
        self.assertEqual(LOCK_ACQUIRE_TIMEOUT_SECONDS, 0.05)

    def test_notify_timeout_is_pinned(self):
        self.assertEqual(NOTIFY_TIMEOUT_SECONDS, 5.0)

    def test_close_timeout_is_pinned(self):
        self.assertEqual(CLOSE_TIMEOUT_SECONDS, 5.0)

if __name__ == "__main__":
    unittest.main()
