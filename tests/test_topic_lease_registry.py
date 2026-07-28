"""topic lease registry — §8.1 B4 / C1 / C3.

운영 dispatcher를 건드리지 않는 **별도 모듈**로 먼저 만든다. 상태기계와 B4 경쟁을 여기서
test-first로 잠근 뒤에 최소 배선해야 원인 분리가 된다.

## 잠그는 계약

1. `(ws, topic, uid, lease_id)` identity + inactive/active 상태
2. 연결별 `asyncio.Lock`
3. `등록(비활성) → is_current(snapshot) → ack → 활성화`
4. **요청 단위 트랜잭션** — 한 요청 = lock 1회 = ack 1건 = 전부 아니면 전무
5. ack이 **권위 있는 상태 전체**를 싣는다(D2: 기존 topic 포함, lock 아래 단일 snapshot)
6. §C1 cross-UID = **purge 후 재바인딩**(거부가 아니다)
7. ack 실패 = **연결 사망**(tombstone) — 취소해도 이미 버퍼에 들어간 ack은 배달될 수 있다
8. `send_ack` 재진입은 **hang이 아니라 raise**
9. 요청당 단일 `now_mono` + 3-way lease 계산
"""
import asyncio
import unittest
from unittest.mock import patch

from app.strict_cache import StrictObservationCache
from app.topic_lease import LEASE_MAX_SECONDS
from app.topic_lease_registry import (
    ACK_TIMEOUT_SECONDS,
    AckState,
    Applied,
    ConnectionTerminated,
    Discarded,
    Rejected,
    ReentrantRegistryCall,
    TopicLeaseRegistry,
)

UID = "uid-1"
TOPIC = "krx:usd-krw-futures"
OTHER = "fx:usd-krw"


class _WS:
    """WebSocket 자리표시자 — registry는 신원만 쓰고 I/O를 하지 않는다."""

    def __init__(self, name="ws"):
        self.name = name

    def __repr__(self):
        return f"<WS {self.name}>"


async def _ok_ack(ack):
    """정상 sender — **`True`를 돌려주는 것이 계약이다**(§B4)."""
    return True


def _fresh(cache=None, uid=UID):
    cache = cache or StrictObservationCache()
    return cache, cache.snapshot(uid)


async def _apply(registry, cache, ws, topics, *, uid=UID, snapshot=None, rejected=(),
                 now_mono=1000.0, premium=1000.0, identity=1000.0, send_ack=_ok_ack):
    if snapshot is None:
        snapshot = cache.snapshot(uid)
    return await registry.apply_subscribe(
        ws=ws, uid=uid, topics=topics, rejected_topics=rejected, snapshot=snapshot, cache=cache,
        now_mono=now_mono, premium_verified_at_mono=premium,
        identity_verified_at_mono=identity, send_ack=send_ack,
    )


class TestIdentityAndStates(unittest.IsolatedAsyncioTestCase):
    """계약 1 — `(ws, topic, uid, lease_id)` identity와 inactive/active 상태."""

    async def test_applied_leases_carry_the_full_identity(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        result = await _apply(registry, cache, ws, [TOPIC], snapshot=snapshot)
        self.assertIsInstance(result, Applied)
        self.assertIs(result.ws, ws)
        lease = registry.active_lease(ws, TOPIC)
        self.assertEqual((lease.topic, lease.uid, lease.epoch), (TOPIC, UID, snapshot.epoch))
        self.assertTrue(lease.lease_id)

    async def test_lease_ids_are_unique_per_request(self):
        """⛔ 같은 id를 재사용하면 C3의 `(ws, topic, lease_id)` CAS가 무의미해진다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        ids = set()
        for _ in range(3):
            await _apply(registry, cache, ws, [TOPIC])
            ids.add(registry.active_lease(ws, TOPIC).lease_id)
        self.assertEqual(len(ids), 3)

    async def test_lease_ids_are_unique_across_topics_in_one_request(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER])
        self.assertNotEqual(
            registry.active_lease(ws, TOPIC).lease_id,
            registry.active_lease(ws, OTHER).lease_id,
        )

    async def test_only_active_leases_are_visible(self):
        """inactive는 **존재하되 보이지 않는다** — 활성화 전에는 전송 대상이 아니다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        self.assertIsNone(registry.active_lease(ws, TOPIC))
        await _apply(registry, cache, ws, [TOPIC])
        self.assertIsNotNone(registry.active_lease(ws, TOPIC))


class TestRequestIsOneTransaction(unittest.IsolatedAsyncioTestCase):
    """계약 4 — 한 요청 = **lock 1회 · ack 1건 · 전부 아니면 전무**.

    ⛔ topic 단위 API로는 이게 불가능했다. 3 topic 요청에서 가능한 배선은 둘뿐이고 둘 다
    깨진다: (i) ack 3건 = §8-B의 "요청당 1건" 위반, (ii) 마지막에만 ack = 앞의 두 topic이
    **ack보다 먼저 활성화**(§B4 순서 고정의 pre-ack live 수신 부활).
    """

    async def test_one_request_sends_exactly_one_ack(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        calls = []

        async def counting(ack):
            calls.append(ack)
            return True

        await _apply(registry, cache, ws, [TOPIC, OTHER, "usdt:krw"], send_ack=counting)
        self.assertEqual(len(calls), 1, "topic마다 ack이 나갔다")
        self.assertEqual({s.topic for s in calls[0].accepted}, {TOPIC, OTHER, "usdt:krw"})

    async def test_all_topics_activate_together_after_the_single_ack(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        seen = {}

        async def probing(ack):
            # ⛔ ack 시점에 **하나도** 활성이면 안 된다 — 그 topic은 ack 전에 live였다는 뜻.
            seen["active_at_ack"] = [t for t in (TOPIC, OTHER)
                                     if registry.active_lease(ws, t) is not None]
            seen["pending_at_ack"] = [t for t in (TOPIC, OTHER) if registry.has_pending(ws, t)]
            return True

        await _apply(registry, cache, ws, [TOPIC, OTHER], send_ack=probing)
        self.assertEqual(seen["active_at_ack"], [], "ack 시점에 이미 live 대상이었다")
        self.assertEqual(seen["pending_at_ack"], [TOPIC, OTHER])
        self.assertIsNotNone(registry.active_lease(ws, TOPIC))
        self.assertIsNotNone(registry.active_lease(ws, OTHER))

    async def test_ack_failure_activates_none_of_them(self):
        """⛔ 부분 적용은 "ack이 광고한 상태"를 무통지로 거짓으로 만든다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()

        async def failing(ack):
            raise ConnectionResetError("socket gone")

        result = await _apply(registry, cache, ws, [TOPIC, OTHER], send_ack=failing)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.active_lease(ws, TOPIC))
        self.assertIsNone(registry.active_lease(ws, OTHER))
        self.assertFalse(registry.has_pending(ws, TOPIC))
        self.assertFalse(registry.has_pending(ws, OTHER))

    async def test_fence_failure_activates_none_of_them(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        cache.bump(UID)
        result = await _apply(registry, cache, ws, [TOPIC, OTHER], snapshot=snapshot)
        self.assertIsInstance(result, Discarded)
        self.assertIsNone(registry.active_lease(ws, TOPIC))
        self.assertIsNone(registry.active_lease(ws, OTHER))

    async def test_duplicate_topics_in_one_request_collapse(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        calls = []

        async def counting(ack):
            calls.append(ack)
            return True

        await _apply(registry, cache, ws, [TOPIC, TOPIC], send_ack=counting)
        self.assertEqual(len(calls[0].accepted), 1, "같은 topic이 두 번 실렸다")


class TestAckCarriesAuthoritativeState(unittest.IsolatedAsyncioTestCase):
    """계약 5 — ack은 **그 연결의 최종 상태 전체**를 싣는다(§D2).

    ⛔ 이번 요청 topic만 실으면, 클라는 언급되지 않은 기존 topic을 여전히 accepted로
    오인한다(§C1 — ack 권위). 그래서 registry가 lock 아래에서 만들어 sender에게 **넘긴다** —
    sender가 조회로 만들면 그 시점 새 lease는 아직 `_pending`이라 보이지도 않는다.
    """

    async def test_ack_includes_pre_existing_topics(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])

        captured = {}

        async def capture(ack):
            captured["ack"] = ack
            return True

        await _apply(registry, cache, ws, [OTHER], send_ack=capture)
        ack = captured["ack"]
        self.assertEqual({s.topic for s in ack.accepted}, {OTHER}, "accepted는 이번 요청 것만")
        self.assertEqual({s.topic for s in ack.active}, {TOPIC, OTHER},
                         "기존 topic이 빠졌다 — 클라가 구독을 잃었다고 오인한다")

    async def test_ack_carries_lease_id_and_remaining_duration_per_topic(self):
        """⛔ 문자열 목록이면 이번 요청에 없던 기존 topic의 lease 상태를 복구할 수 없다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        captured = {}

        async def capture(ack):
            captured["ack"] = ack
            return True

        await _apply(registry, cache, ws, [TOPIC], send_ack=capture,
                     now_mono=1000.0, premium=1000.0, identity=1000.0)
        entry = captured["ack"].active[0]
        self.assertEqual(entry.topic, TOPIC)
        self.assertEqual(entry.lease_id, registry.active_lease(ws, TOPIC).lease_id)
        self.assertEqual(entry.lease_duration_seconds, int(LEASE_MAX_SECONDS))

    async def test_remaining_duration_is_floored_not_rounded(self):
        """§D2 — 내림(floor)이다. 올림하면 만료 뒤를 살아 있다고 광고한다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC], now_mono=1000.0, premium=999.4, identity=1000.0)

        captured = {}

        async def capture(ack):
            captured["ack"] = ack
            return True

        # 두 번째 요청의 now는 0.5초 뒤 — 기존 lease 잔여는 899.5 - 0.5 = 899.1 → 899
        await _apply(registry, cache, ws, [OTHER], send_ack=capture, now_mono=1000.5)
        remaining = {s.topic: s.lease_duration_seconds for s in captured["ack"].active}
        self.assertEqual(remaining[TOPIC], 898)

    async def test_expired_existing_lease_is_not_advertised_as_active(self):
        """§D2 — `≤ 0`이면 넣지 않는다. **만료를 "활성"으로 광고 금지.**

        ⚠️ 그렇다고 registry에서 지우지는 않는다 — 실제 제거·`reauth_required` 통지는
        C3 sweep의 소유다. 여기서 조용히 지우면 그 통지가 영영 나가지 않는다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC], now_mono=1000.0)

        captured = {}

        async def capture(ack):
            captured["ack"] = ack
            return True

        later = 1000.0 + LEASE_MAX_SECONDS + 1
        await _apply(registry, cache, ws, [OTHER], send_ack=capture,
                     now_mono=later, premium=later, identity=later)
        self.assertEqual({s.topic for s in captured["ack"].active}, {OTHER})
        self.assertIsNotNone(registry.active_lease(ws, TOPIC),
                             "C3이 통지할 대상을 registry가 조용히 지웠다")

    async def test_ack_names_the_bound_uid(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        captured = {}

        async def capture(ack):
            captured["ack"] = ack
            return True

        await _apply(registry, cache, ws, [TOPIC], send_ack=capture)
        self.assertEqual(captured["ack"].uid, UID)

    async def test_applied_returns_the_same_ack_that_was_sent(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        captured = {}

        async def capture(ack):
            captured["ack"] = ack
            return True

        result = await _apply(registry, cache, ws, [TOPIC], send_ack=capture)
        self.assertIsInstance(result.ack, AckState)
        self.assertIs(result.ack, captured["ack"])


class TestCrossUidPurge(unittest.IsolatedAsyncioTestCase):
    """계약 6 — §C1은 **purge 후 재바인딩**이다(§C1), 거부가 아니다.

    ⛔ 한때 이 모듈은 cross-UID를 `Rejected`로 막았다. 그건 §C1의 전이 규칙·처리 순서 및 §D2의 producer 목록과 정면으로
    충돌한다 — §D2는 `removed_topics`의 producer로 "C1의 UID purge"를 **명시**한다.
    """

    async def test_first_subscribe_binds_the_connection(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        self.assertEqual(registry.bound_uid(ws), UID)

    async def test_cross_uid_purges_the_previous_subscriptions_and_rebinds(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])

        other = StrictObservationCache()
        captured = {}

        async def capture(ack):
            captured["ack"] = ack
            return True

        result = await _apply(registry, other, ws, [OTHER], uid="uid-2", send_ack=capture)
        self.assertIsInstance(result, Applied)
        self.assertEqual(registry.bound_uid(ws), "uid-2")
        self.assertIsNone(registry.active_lease(ws, TOPIC), "구 UID 구독이 살아남았다")
        self.assertIsNotNone(registry.active_lease(ws, OTHER))
        self.assertEqual(captured["ack"].removed, (TOPIC,))
        self.assertEqual({s.topic for s in captured["ack"].active}, {OTHER})

    async def test_purge_happens_even_when_no_topic_is_accepted(self):
        """§C1 처리 순서 — B의 premium 확인이 실패해도 A 구독을 **복원하지 않는다**.

        premium이 per-topic으로 전부 reject되면 accepted가 빈 요청이 registry에 도달한다.
        그때도 purge는 커밋돼야 한다 — 아니면 B의 소켓이 A의 데이터를 계속 받는다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])

        other = StrictObservationCache()
        result = await _apply(registry, other, ws, [], uid="uid-2")
        self.assertIsInstance(result, Applied)
        self.assertEqual(registry.bound_uid(ws), "uid-2")
        self.assertIsNone(registry.active_lease(ws, TOPIC))
        self.assertEqual(result.ack.removed, (TOPIC,))
        self.assertEqual(result.ack.active, ())

    async def test_purge_is_not_committed_when_the_ack_fails(self):
        """전부 아니면 전무 — ack이 못 나갔으면 클라는 새 상태를 모른다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])

        async def failing(ack):
            raise ConnectionResetError()

        other = StrictObservationCache()
        result = await _apply(registry, other, ws, [OTHER], uid="uid-2", send_ack=failing)
        self.assertIsInstance(result, ConnectionTerminated)
        # ⚠️ `bound_uid`는 **기록**이라 tombstone과 무관하게 보인다 — 여기서 반쯤 넘어간 UID를 잡는다.
        #    인가 응답인 `active_lease`는 tombstone에서 fail-closed라 아래처럼 전부 None이다.
        self.assertEqual(registry.bound_uid(ws), UID, "UID가 반쯤 넘어갔다")
        self.assertIsNone(registry.active_lease(ws, OTHER), "새 lease가 활성화됐다")

    async def test_retaken_topic_is_not_reported_as_removed(self):
        """같은 topic을 새 UID가 다시 잡으면 **교체**다 — removed와 accepted에 동시에 실으면 모순이다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER])

        other = StrictObservationCache()
        result = await _apply(registry, other, ws, [TOPIC], uid="uid-2")
        self.assertEqual(result.ack.removed, (OTHER,))
        self.assertEqual({s.topic for s in result.ack.accepted}, {TOPIC})
        self.assertEqual({s.topic for s in result.ack.active}, {TOPIC})

    async def test_different_connections_may_hold_different_uids(self):
        registry = TopicLeaseRegistry()
        # ⚠️ 연결 참조를 **붙들어야** 한다 — registry는 약한 참조라 놓으면 즉시 사라진다.
        sockets = [_WS("a"), _WS("b")]
        for ws, uid in zip(sockets, ("uid-1", "uid-2")):
            c = StrictObservationCache()
            await _apply(registry, c, ws, [TOPIC], uid=uid)
        self.assertEqual(registry.connection_count(), 2)
        self.assertEqual({registry.bound_uid(ws) for ws in sockets}, {"uid-1", "uid-2"})


class TestFenceOrdering(unittest.IsolatedAsyncioTestCase):
    """계약 3 — `등록(비활성) → is_current(snapshot) → 활성화`."""

    async def test_invalidation_before_activation_discards_everything(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        cache.bump(UID)
        result = await _apply(registry, cache, ws, [TOPIC], snapshot=snapshot)
        self.assertIsInstance(result, Discarded)
        self.assertIsNone(registry.active_lease(ws, TOPIC), "폐기했는데 흔적이 남았다")

    async def test_registration_happens_before_the_recheck(self):
        """⛔ 순서가 뒤집히면(재확인 → 등록) 그 사이 무효화를 못 본다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        observed = {}
        real_is_current = cache.is_current

        def spy(snap):
            observed["registered"] = registry.has_pending(ws, TOPIC)
            observed["visible"] = registry.active_lease(ws, TOPIC)
            return real_is_current(snap)

        cache.is_current = spy
        await _apply(registry, cache, ws, [TOPIC], snapshot=snapshot)
        self.assertTrue(observed.get("registered"), "재확인 시점에 등록돼 있지 않았다")
        self.assertIsNone(observed.get("visible"), "활성화 전인데 전송 대상으로 보였다")

    async def test_fence_is_checked_once_per_request_not_per_topic(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        calls = []
        real_is_current = cache.is_current
        cache.is_current = lambda snap: (calls.append(snap), real_is_current(snap))[1]
        await _apply(registry, cache, ws, [TOPIC, OTHER, "usdt:krw"])
        self.assertEqual(len(calls), 1)


class TestLeaseComputation(unittest.IsolatedAsyncioTestCase):
    """계약 9 — 요청당 **단일 `now_mono`** + 3-way lease 계산."""

    async def test_expiry_is_the_three_way_minimum(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC], now_mono=1000.0,
                     premium=940.0, identity=980.0)
        self.assertAlmostEqual(
            registry.active_lease(ws, TOPIC).expires_at_mono, 940.0 + LEASE_MAX_SECONDS
        )

    async def test_every_topic_in_a_request_shares_one_expiry(self):
        """D1 — 한 ack 안의 accepted들은 **같은 순간** 갱신된다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER])
        self.assertEqual(
            registry.active_lease(ws, TOPIC).expires_at_mono,
            registry.active_lease(ws, OTHER).expires_at_mono,
        )

    async def test_registry_does_not_read_a_clock_itself(self):
        import inspect

        import app.topic_lease_registry as module

        source = inspect.getsource(module)
        for forbidden in ("time.monotonic", "time.time", "clock.mono"):
            self.assertNotIn(forbidden, source, f"{forbidden} 를 직접 읽고 있다")


class TestConnectionLock(unittest.IsolatedAsyncioTestCase):
    """계약 2 — 연결별 `asyncio.Lock`과 인터리빙 harness."""

    async def test_different_connections_are_not_serialised(self):
        registry = TopicLeaseRegistry()
        self.assertIsNot(registry.connection_lock(_WS("a")), registry.connection_lock(_WS("b")))

    async def test_lock_is_stable_per_connection(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        self.assertIs(registry.connection_lock(ws), registry.connection_lock(ws))

    async def test_ack_under_lock_actually_excludes_a_concurrent_request(self):
        """⛔ lock이 **실제로** load-bearing이다 — ack이 임계구역 안에 있으므로."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        release = asyncio.Event()
        order = []

        async def slow_ack(ack):
            order.append("ack:start")
            await release.wait()
            order.append("ack:end")
            return True

        first = asyncio.create_task(_apply(registry, cache, ws, [TOPIC], send_ack=slow_ack))
        while "ack:start" not in order:
            await asyncio.sleep(0)

        async def quick_ack(ack):
            order.append("second:ack")
            return True

        second = asyncio.create_task(_apply(registry, cache, ws, [OTHER], send_ack=quick_ack))
        try:
            await asyncio.sleep(0.03)
            self.assertNotIn("second:ack", order, "ack 구간에 다른 요청이 끼어들었다")
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(order, ["ack:start", "ack:end", "second:ack"])

    async def test_ack_runs_under_the_connection_lock(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        observed = {}

        async def probe(ack):
            observed["locked"] = registry.connection_lock(ws).locked()
            return True

        await _apply(registry, cache, ws, [TOPIC], send_ack=probe)
        self.assertTrue(observed.get("locked"), "ack이 lock 밖에서 나갔다")


class TestAckFailureKillsTheConnection(unittest.IsolatedAsyncioTestCase):
    """계약 7 — ack 실패는 "발급 안 함"이 아니라 **연결 사망**이다(§B2a).

    ⛔ 취소는 이미 transport 버퍼에 들어간 프레임을 **되돌리지 못한다**(websockets legacy는
    `write_frame_sync` 후 `drain()`을 await한다). 역압이 풀리면 그 ack이 나중에 배달돼,
    클라는 lease를 가졌다고 믿는데 서버엔 없는 상태가 된다 — 무데이터·무오류로 D6 재인증
    타이머까지 방치된다. 그래서 registry가 **직접 tombstone을 찍어** 같은 소켓의 재발급을
    fail-closed로 막는다(호출자의 close 의무는 그 위에 얹힌다).
    """

    async def test_ack_exception_marks_the_connection_dead(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()

        async def failing(ack):
            raise ConnectionResetError("socket gone")

        result = await _apply(registry, cache, ws, [TOPIC], send_ack=failing)
        self.assertIsInstance(result, ConnectionTerminated)
        again = await _apply(registry, cache, ws, [TOPIC])
        self.assertIsInstance(again, ConnectionTerminated)
        self.assertEqual(again.reason, "connection_closed")

    async def test_ack_timeout_releases_the_lock_and_marks_dead(self):
        """⛔ 멈춘 클라가 **연결 lock을 무한 점유**하면 그 연결의 모든 전이가 막힌다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()

        async def stalling(ack):
            await asyncio.sleep(60)

        with patch("app.topic_lease_registry.ACK_TIMEOUT_SECONDS", 0.02):
            result = await _apply(registry, cache, ws, [TOPIC], send_ack=stalling)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertEqual(result.reason, "ack_timeout")
        self.assertFalse(registry.connection_lock(ws).locked(), "lock이 남았다")
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC]), ConnectionTerminated)

    async def test_sender_that_does_not_confirm_is_a_failure(self):
        """계약 — `send_ack`는 `True`를 돌려줘야 한다.

        ⛔ `wait_for`의 제어 흐름은 배달의 증거가 아니다: sender가 `CancelledError`를
        삼키고 정상 반환하면 `wait_for`는 **정상 반환**한다(`asyncio.Timeout.__aexit__`는
        예외가 실제로 전파될 때만 `TimeoutError`로 바꾼다). 그러면 취소된 ack을 성공으로
        읽고 활성화한다. 반환값 계약이 그 shape을 잡는다.
        ⚠️ 한계는 정직하게 적는다 — 삼킨 뒤 `True`를 돌려주면 여전히 속는다. 그건
        docstring의 금지 규칙이 지고, 여기서는 **사고로 인한** 무확인만 막는다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()

        async def silent(ack):
            return None       # `await ws.send_json(...)`만 하고 끝나는 가장 흔한 형태

        result = await _apply(registry, cache, ws, [TOPIC], send_ack=silent)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertEqual(result.reason, "ack_not_confirmed")
        self.assertIsNone(registry.active_lease(ws, TOPIC))

    async def test_sender_that_swallows_cancellation_is_not_read_as_success(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()

        async def swallowing(ack):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return "sent, honest"      # 삼키고 정상 반환 → wait_for는 정상 반환한다
            return True

        with patch("app.topic_lease_registry.ACK_TIMEOUT_SECONDS", 0.02):
            result = await _apply(registry, cache, ws, [TOPIC], send_ack=swallowing)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.active_lease(ws, TOPIC))

    async def test_pre_existing_leases_stop_authorizing_after_an_ack_failure(self):
        """⛔ tombstone이 **신규 전이만** 막으면 부족하다.

        close가 지연되거나 실패하면, 죽었다고 판정한 소켓으로 **기존 UID의 데이터가 계속**
        나간다. cross-UID 상황에서는 그게 곧 entitlement 우회다: A의 KRX lease가 살아 있는
        채로 클라는 (늦게 도착한 ack을 보고) B 세션이라고 믿는다.
        → 인가 응답인 `active_lease`가 tombstone에서 **즉시 fail-closed**여야 한다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        self.assertIsNotNone(registry.active_lease(ws, TOPIC))

        async def failing(ack):
            raise ConnectionResetError()

        self.assertIsInstance(await _apply(registry, cache, ws, [OTHER], send_ack=failing),
                              ConnectionTerminated)
        self.assertIsNone(registry.active_lease(ws, TOPIC),
                          "죽었다고 판정한 소켓의 기존 lease가 여전히 인가된다")

    async def test_swallowed_external_cancellation_does_not_commit(self):
        """⛔ sender가 **외부** 취소를 삼키고 `True`를 돌려주면 커밋돼 버렸다(실측 `Applied`).

        모듈 문서는 "삼키면 무조건 속는다"고 적었는데 **실제보다 넓은 주장**이었다:
        바깥 프레임의 `cancelling()`이 예산 만료에서는 0→0(`wait_for`가 `uncancel()`),
        외부 취소에서는 0→1로 갈린다(실측, py3.13). 예산 만료는 여전히 구별 불가지만
        **teardown의 취소를 삼키는 경우는 공짜로 fail-close**할 수 있다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        entered = asyncio.Event()

        async def swallowing(ack):
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return True                      # 삼키고 성공 반환
            return True

        task = asyncio.create_task(_apply(registry, cache, ws, [TOPIC], send_ack=swallowing))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(registry.active_lease(ws, TOPIC), "취소를 삼켰는데 활성화됐다")

    async def test_ack_timeout_is_below_the_client_ack_deadline(self):
        """§D6 클라 ack timeout이 10초다 — 그보다 길면 클라가 먼저 포기한다."""
        self.assertLess(ACK_TIMEOUT_SECONDS, 10)

    async def test_invalidation_does_not_kill_the_connection(self):
        """무효화는 **재시도**가 맞다 — ack 실패(연결 종료)와 대응이 다르다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        cache.bump(UID)
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC], snapshot=snapshot),
                              Discarded)
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC]), Applied)

    async def test_external_cancellation_is_not_converted_to_a_result(self):
        """⛔ teardown의 취소를 반환값으로 바꾸면 구조적 종료가 조용히 삼켜진다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        entered = asyncio.Event()

        async def stalling(ack):
            entered.set()
            await asyncio.sleep(60)
            return True

        task = asyncio.create_task(_apply(registry, cache, ws, [TOPIC], send_ack=stalling))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(registry.active_lease(ws, TOPIC))
        self.assertFalse(registry.has_pending(ws, TOPIC), "pending이 남았다")
        self.assertFalse(registry.connection_lock(ws).locked(), "lock이 남았다")
        # 취소도 **배달 여부 불명**이라 timeout과 같은 fail-closed다. teardown이 취소한
        # 경우엔 어차피 `remove_websocket`이 tombstone을 찍지만, 그렇지 않은 취소
        # (per-message task를 감독자가 접는 경우)에는 여기가 유일한 방어다.
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC]), ConnectionTerminated)


class TestSenderReentrancyRaisesInsteadOfHanging(unittest.IsolatedAsyncioTestCase):
    """계약 8 — `asyncio.Lock`은 재진입 불가라 sender가 registry를 다시 부르면 **영구 정지**다.

    ⛔ 가장 자연스러운 §B2a sender가 `finally: await registry.remove_websocket(ws)`이다.
    ack 예산이 끝나 취소가 그 `finally`로 풀려도 `lock.acquire()`를 기다리며, 그 lock은
    **우리 자신의 바깥 프레임**이 쥐고 있다 — 아무도 깨우지 못한다. 그 연결의 teardown ·
    sweep · unsubscribe가 통째로 죽는다. hang 대신 **시끄럽게** 실패시킨다.
    """

    async def _run_bounded(self, coro):
        """⛔ hang이면 테스트가 매달리는 대신 실패해야 한다."""
        return await asyncio.wait_for(coro, timeout=2.0)

    async def test_sender_calling_remove_websocket_raises(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        seen = {}

        async def reentrant(ack):
            try:
                await registry.remove_websocket(ws)
            except ReentrantRegistryCall as exc:
                seen["raised"] = exc
                raise
            return True

        with patch("app.topic_lease_registry.ACK_TIMEOUT_SECONDS", 30.0):
            result = await self._run_bounded(
                _apply(registry, cache, ws, [TOPIC], send_ack=reentrant)
            )
        self.assertIn("raised", seen, "재진입이 hang했다(또는 조용히 통과했다)")
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertEqual(result.reason, "ReentrantRegistryCall")

    async def test_sender_calling_remove_raises(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        seen = {}

        async def reentrant(ack):
            try:
                await registry.remove(ws, TOPIC, "whatever")
            except ReentrantRegistryCall:
                seen["raised"] = True
            return True

        with patch("app.topic_lease_registry.ACK_TIMEOUT_SECONDS", 30.0):
            await self._run_bounded(_apply(registry, cache, ws, [TOPIC], send_ack=reentrant))
        self.assertTrue(seen.get("raised"))

    async def test_sender_calling_apply_subscribe_raises(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        seen = {}

        async def reentrant(ack):
            try:
                await _apply(registry, cache, ws, [OTHER])
            except ReentrantRegistryCall:
                seen["raised"] = True
            return True

        with patch("app.topic_lease_registry.ACK_TIMEOUT_SECONDS", 30.0):
            await self._run_bounded(_apply(registry, cache, ws, [TOPIC], send_ack=reentrant))
        self.assertTrue(seen.get("raised"))

    async def test_another_task_still_waits_normally(self):
        """⛔ 오탐 금지 — 무관한 task의 정당한 대기까지 막으면 sweep/teardown이 깨진다.

        그래서 판정 기준은 ws가 아니라 **task 동일성**이다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        release = asyncio.Event()
        entered = asyncio.Event()

        async def slow_ack(ack):
            entered.set()
            await release.wait()
            return True

        first = asyncio.create_task(_apply(registry, cache, ws, [TOPIC], send_ack=slow_ack))
        await entered.wait()
        other = asyncio.create_task(registry.remove_websocket(ws))
        try:
            await asyncio.sleep(0.03)
            self.assertFalse(other.done(), "다른 task가 대기 대신 예외를 받았다")
        finally:
            release.set()
            await asyncio.gather(first, other, return_exceptions=True)
        self.assertIsNone(other.exception(), "무관한 task가 재진입으로 오탐됐다")


class TestWeakCleanupActuallyWorks(unittest.IsolatedAsyncioTestCase):
    """⛔ `WeakKeyDictionary`는 **키만** 약하다 — 값이 키를 잡으면 통째로 무력화된다."""

    async def test_connection_is_collected_when_the_handler_drops_it(self):
        import gc

        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        self.assertEqual(registry.connection_count(), 1)

        del ws
        gc.collect()
        self.assertEqual(registry.connection_count(), 0, "값이 키를 살려 두고 있다")


class TestExpiredHorizonIsNotIssued(unittest.IsolatedAsyncioTestCase):
    """⛔ 이미 만료된 horizon으로 발급하면 **태어날 때부터 죽은** 구독이 된다."""

    async def test_expired_expiry_is_rejected(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        result = await _apply(registry, cache, ws, [TOPIC],
                              now_mono=2000.0, premium=1000.0, identity=1000.0)
        self.assertIsInstance(result, Rejected)
        self.assertIsNone(registry.active_lease(ws, TOPIC), "만료된 lease가 활성화됐다")

    async def test_boundary_is_inclusive(self):
        """§A2 — `now >= expires_at`이 만료다(fail-closed)."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        result = await _apply(registry, cache, _WS(), [TOPIC],
                              now_mono=1000.0 + LEASE_MAX_SECONDS, premium=1000.0, identity=1000.0)
        self.assertIsInstance(result, Rejected)

    async def test_a_pure_purge_is_not_blocked_by_an_expired_horizon(self):
        """⛔ 발급할 lease가 없으면 horizon은 무관하다.

        여기서 거부하면 **접근을 없애기만 하는 요청**이 막힌다 — B의 소켓이 A의 데이터를
        계속 받는 fail-open이다. 만료 판정은 "발급"에만 건다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC], now_mono=1000.0)

        other = StrictObservationCache()
        result = await _apply(registry, other, ws, [], uid="uid-2",
                              now_mono=5000.0, premium=1000.0, identity=1000.0)
        self.assertIsInstance(result, Applied)
        self.assertIsNone(registry.active_lease(ws, TOPIC))


class TestMutatorsAreNotB4Bypasses(unittest.IsolatedAsyncioTestCase):
    """⛔ 연결 lock을 안 잡는 public 변경 API가 있으면 그게 **B4 우회로**다."""

    def test_remove_acquires_the_connection_lock(self):
        import ast
        import inspect

        import app.topic_lease_registry as module

        tree = ast.parse(inspect.getsource(module))
        remove = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.AsyncFunctionDef) and n.name == "remove")
        locks = [n for n in ast.walk(remove)
                 if isinstance(n, ast.AsyncWith)
                 and any("connection_lock" in ast.dump(i.context_expr) for i in n.items)]
        self.assertEqual(len(locks), 1, "remove가 연결 lock을 잡지 않는다")

    def test_public_mutators_are_async_so_the_lock_is_expressible(self):
        import inspect

        from app.topic_lease_registry import TopicLeaseRegistry as R

        for name in ("apply_subscribe", "remove", "remove_websocket"):
            self.assertTrue(inspect.iscoroutinefunction(getattr(R, name)), f"{name}이 동기다")


class TestTeardownDoesNotBreakLockIdentity(unittest.IsolatedAsyncioTestCase):
    """⛔ `remove_websocket`이 lock 항목을 지우면 **상호배제가 깨진다**."""

    async def test_lock_identity_survives_teardown(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        lock = registry.connection_lock(ws)
        await registry.remove_websocket(ws)
        self.assertIs(registry.connection_lock(ws), lock, "teardown이 lock을 바꿔치기했다")

    async def test_teardown_waits_for_the_connection_lock(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        done = asyncio.Event()
        released = asyncio.Event()

        async def holder():
            async with registry.connection_lock(ws):
                await released.wait()

        held = asyncio.create_task(holder())
        while not registry.connection_lock(ws).locked():
            await asyncio.sleep(0)

        teardown = asyncio.create_task(registry.remove_websocket(ws))
        teardown.add_done_callback(lambda _: done.set())
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(done.is_set(), "lock을 쥔 구간에 teardown이 끼어들었다")
        finally:
            released.set()
            await asyncio.gather(held, teardown, return_exceptions=True)

    async def test_reissue_after_teardown_is_terminal(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        await registry.remove_websocket(ws)
        result = await _apply(registry, cache, ws, [TOPIC])
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.active_lease(ws, TOPIC), "teardown 뒤 구독이 되살아났다")


class TestRemovalCas(unittest.IsolatedAsyncioTestCase):
    """§C3 — 제거·갱신은 `(ws, topic, lease_id)` CAS다."""

    async def test_removal_requires_the_matching_lease_id(self):
        """⛔ 구 sweep이 **갱신된** lease를 지우면 살아 있는 구독이 사라진다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        first_id = registry.active_lease(ws, TOPIC).lease_id
        await _apply(registry, cache, ws, [TOPIC])          # 갱신
        second_id = registry.active_lease(ws, TOPIC).lease_id

        self.assertFalse(await registry.remove(ws, TOPIC, first_id), "구 id로 지워졌다")
        self.assertIsNotNone(registry.active_lease(ws, TOPIC))
        self.assertTrue(await registry.remove(ws, TOPIC, second_id))
        self.assertIsNone(registry.active_lease(ws, TOPIC))

    async def test_remove_websocket_clears_everything_for_that_connection(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws, other = _WS("a"), _WS("b")
        await _apply(registry, cache, ws, [TOPIC])
        await _apply(registry, cache, other, [TOPIC])
        await registry.remove_websocket(ws)
        self.assertIsNone(registry.active_lease(ws, TOPIC))
        self.assertIsNone(registry.bound_uid(ws))
        self.assertIsNotNone(registry.active_lease(other, TOPIC), "남의 연결까지 지웠다")


class TestC2RejectEviction(unittest.IsolatedAsyncioTestCase):
    """§C2 — 인증 성공한 subscribe에서 **reject된 topic은 registry에서 제거**한다(§C2).

    ⛔ accepted만 받으면 권한을 잃은 기존 등록이 **lease 만료까지 잔존**한다(710~711행).
    같은 UID 재인증에서 KRX entitlement를 잃어도 최대 15분 더 KRX 데이터가 나간다는 뜻이다.
    §D2는 `removed_topics`의 producer로 "C2의 reject eviction"을 명시한다.
    """

    async def test_rejected_topic_that_was_active_is_evicted(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [OTHER, TOPIC])

        result = await _apply(registry, cache, ws, [OTHER], rejected=[TOPIC])
        self.assertIsInstance(result, Applied)
        self.assertIsNone(registry.active_lease(ws, TOPIC), "권한 잃은 구독이 만료까지 살아남았다")
        self.assertEqual(result.ack.removed, (TOPIC,))
        self.assertEqual({s.topic for s in result.ack.active}, {OTHER})

    async def test_unmentioned_topics_are_untouched(self):
        """§C2 — 이번 요청에 **언급되지 않은** topic은 불변(증분 subscribe 보존)."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER, "usdt:krw"])

        result = await _apply(registry, cache, ws, [], rejected=[TOPIC])
        self.assertIsNone(registry.active_lease(ws, TOPIC))
        self.assertIsNotNone(registry.active_lease(ws, OTHER), "언급 없던 topic이 지워졌다")
        self.assertIsNotNone(registry.active_lease(ws, "usdt:krw"))
        self.assertEqual({s.topic for s in result.ack.active}, {OTHER, "usdt:krw"})

    async def test_rejected_topic_that_was_not_active_is_not_reported_removed(self):
        """거부됐지만 원래 없던 topic은 **제거된 것이 아니다** — removed는 상태 델타다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        result = await _apply(registry, cache, ws, [OTHER], rejected=[TOPIC])
        self.assertEqual(result.ack.removed, ())

    async def test_eviction_is_not_committed_when_the_ack_fails(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])

        async def failing(ack):
            raise ConnectionResetError()

        lease_id = registry.active_lease(ws, TOPIC).lease_id
        result = await _apply(registry, cache, ws, [], rejected=[TOPIC], send_ack=failing)
        self.assertIsInstance(result, ConnectionTerminated)
        # ⚠️ `active_lease`는 tombstone에서 fail-closed라 여기선 관측 도구가 못 된다.
        #    `remove()`의 CAS는 tombstone을 보지 않으므로 "그 lease가 아직 존재하는가"를 답한다.
        #    ⛔ 구 버전은 `identity_generation == 1`을 단언했는데, 같은 UID 재인증에서는
        #    커밋 여부와 무관하게 항상 참이라 **공허**했다 — 제거를 ack 전에 커밋하는 변이가
        #    전체 스위트를 통과했다(실측).
        self.assertTrue(await registry.remove(ws, TOPIC, lease_id),
                        "ack이 실패했는데 제거가 커밋됐다(all-or-nothing 위반)")

    async def test_eviction_and_purge_can_happen_in_one_request(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER])

        other = StrictObservationCache()
        result = await _apply(registry, other, ws, [], uid="uid-2", rejected=[TOPIC])
        # purge가 둘 다 걷어 간다 — eviction 집합이 purge 집합의 부분집합이라도 중복 보고는 없다.
        self.assertEqual(result.ack.removed, tuple(sorted((TOPIC, OTHER))))
        self.assertEqual(result.ack.active, ())

    async def test_topic_both_accepted_and_rejected_is_a_caller_bug(self):
        """⛔ D5 단계상 한 topic이 둘 다일 수 없다. 조용히 한쪽을 고르면 나중에 물린다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        with self.assertRaises(ValueError):
            await _apply(registry, cache, _WS(), [TOPIC], rejected=[TOPIC])


class TestConnectionIdentityGeneration(unittest.IsolatedAsyncioTestCase):
    """§D2 — `identity_generation`은 **연결별**이고 **같은 소켓의 UID 재바인딩만** 표현한다.

    ⛔ strict cache의 `snapshot.epoch`을 실으면 안 된다. 그건 **UID별**이고 최초 관측 순서로
    할당되므로, 먼저 관측된 UID B의 epoch가 나중에 관측된 A보다 **작다**. A→B 재바인딩 ack의
    generation이 감소하고, G 매트릭스가 요구하는 "구 `identity_generation` ack 무시"를 지키는
    클라는 **유효한 새 ack을 버린다**. (실측: A=2 → B=1)
    """

    async def test_starts_at_one_on_first_bind(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        result = await _apply(registry, cache, ws, [TOPIC])
        self.assertEqual(result.ack.identity_generation, 1)
        self.assertEqual(registry.identity_generation(ws), 1)

    async def test_unchanged_for_same_uid_reauth(self):
        """재인증은 재바인딩이 아니다 — 올리면 클라가 자기 상태를 불필요하게 버린다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        result = await _apply(registry, cache, ws, [OTHER])
        self.assertEqual(result.ack.identity_generation, 1)

    async def test_increases_on_rebinding_even_when_the_uid_epoch_decreases(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        cache = StrictObservationCache()
        snap_b_early = cache.snapshot("uid-B")      # 먼저 할당 → 낮은 epoch
        snap_a = cache.snapshot("uid-A")            # 나중 할당 → 높은 epoch
        self.assertLess(snap_b_early.epoch, snap_a.epoch, "전제가 성립하지 않는다")

        first = await _apply(registry, cache, ws, [TOPIC], uid="uid-A", snapshot=snap_a)
        second = await _apply(registry, cache, ws, [TOPIC], uid="uid-B",
                              snapshot=cache.snapshot("uid-B"))
        self.assertEqual(first.ack.identity_generation, 1)
        self.assertEqual(second.ack.identity_generation, 2, "재바인딩에서 generation이 감소했다")

    async def test_is_not_advanced_when_the_ack_fails(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])

        async def failing(ack):
            raise ConnectionResetError()

        other = StrictObservationCache()
        await _apply(registry, other, ws, [OTHER], uid="uid-2", send_ack=failing)
        self.assertEqual(registry.identity_generation(ws), 1, "실패한 재바인딩이 세대를 올렸다")

    async def test_unbound_connection_has_generation_zero(self):
        self.assertEqual(TopicLeaseRegistry().identity_generation(_WS()), 0)


class TestAbortedAccessReductionIsFailClosed(unittest.IsolatedAsyncioTestCase):
    """⛔ **접근을 줄이는 전이는 중단돼도 되돌아가지 않는다.**

    되돌아가려면 그만큼의 접근이 계속 살아 있어야 하는데, 그게 정확히 C1/C2가 막으려는 상태다.
    구 구현은 purge·eviction을 **발급 성공에 종속**시켜, 세 중단 경로(만료 horizon / fence
    실패 / 축 위반)에서 전부 fail-open이었다 — 실측: B의 소켓에서 A의 lease가 계속 인가됨.

    ⚠️ "제거만 커밋하고 ack을 보낸다"는 대안도 있으나 중단 경로마다 부분 커밋 규칙이 생겨
    표면이 넓어진다. tombstone은 **한 메커니즘**으로 모든 중단 경로에서 접근을 0으로 만든다
    (의도한 축소보다 크거나 같으므로 항상 안전한 쪽이다).
    """

    async def _armed(self, uid=UID):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER], uid=uid)
        return registry, cache, ws

    async def test_expired_horizon_with_pending_eviction_closes_the_connection(self):
        registry, cache, ws = await self._armed()
        result = await _apply(registry, cache, ws, [OTHER], rejected=[TOPIC],
                              now_mono=2000.0, premium=1000.0, identity=1000.0)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.active_lease(ws, TOPIC), "거부된 topic이 계속 인가된다")

    async def test_expired_horizon_with_pending_purge_closes_the_connection(self):
        registry, cache, ws = await self._armed()
        other = StrictObservationCache()
        result = await _apply(registry, other, ws, [OTHER], uid="uid-2",
                              now_mono=2000.0, premium=1000.0, identity=1000.0)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.active_lease(ws, TOPIC), "B 소켓에서 A의 lease가 계속 인가된다")
        self.assertIsNone(registry.active_lease(ws, OTHER))

    async def test_fence_failure_with_pending_eviction_closes_the_connection(self):
        registry, cache, ws = await self._armed()
        snapshot = cache.snapshot(UID)
        cache.bump(UID)
        result = await _apply(registry, cache, ws, [OTHER], rejected=[TOPIC], snapshot=snapshot)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.active_lease(ws, TOPIC))

    async def test_fence_failure_with_pending_purge_closes_the_connection(self):
        registry, cache, ws = await self._armed()
        other = StrictObservationCache()
        snapshot = other.snapshot("uid-2")
        other.bump("uid-2")
        result = await _apply(registry, other, ws, [OTHER], uid="uid-2", snapshot=snapshot)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.active_lease(ws, TOPIC))

    async def test_axis_violation_with_pending_purge_closes_the_connection(self):
        """축 위반(wall epoch 혼입 등)은 예외로 크게 터지지만, 그 전에 접근은 닫혀야 한다."""
        registry, cache, ws = await self._armed()
        other = StrictObservationCache()
        with self.assertRaises(ValueError):
            await _apply(registry, other, ws, [OTHER], uid="uid-2", premium=float("nan"))
        self.assertIsNone(registry.active_lease(ws, TOPIC), "예외 경로가 fail-open이었다")

    async def test_abort_without_any_reduction_leaves_the_connection_usable(self):
        """⛔ 오탐 금지 — 줄일 게 없던 중단까지 연결을 죽이면 정상 webhook 경쟁이 연결을 끊는다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        cache.bump(UID)
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC], snapshot=snapshot),
                              Discarded)
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC]), Applied)

    async def test_pure_purge_still_validates_the_time_axis(self):
        """⛔ 만료 **판정**만 `topics`에 걸어야 한다 — 입력 검증까지 건너뛰면 축 혼입이 조용해진다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        other = StrictObservationCache()
        with self.assertRaises(ValueError):
            await _apply(registry, other, ws, [], uid="uid-2", premium=float("nan"))


class TestResultTypeMatchesConnectionLiveness(unittest.IsolatedAsyncioTestCase):
    """⛔ **결과 타입 == 연결 생존 여부.** 둘이 어긋나면 호출자는 알 방법이 없다.

    실측된 결함: 축소가 예정된 중단은 tombstone을 찍으면서도 `Discarded`를 그대로
    돌려줬다 — 그 타입의 계약은 "연결은 멀쩡하니 재시도"인데, 재시도는 영원히
    `connection_closed`를 받는다. 클라는 데이터도 오류도 없는 상태에 갇힌다.

    그래서 종단 결과 타입을 **하나로** 둔다. 둘 이상이면 호출자가 전부 알아야 하고,
    하나라도 놓치면 같은 함정이 재생된다.
    """

    async def _armed(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER])
        return registry, cache, ws

    async def _is_dead(self, registry, cache, ws):
        """후속 요청이 종단으로 막히는가 = tombstone이 찍혔는가."""
        result = await _apply(registry, cache, ws, [TOPIC])
        return isinstance(result, ConnectionTerminated) and result.reason == "connection_closed"

    async def test_every_abort_path_agrees_with_its_result_type(self):
        async def fence_with_reduction():
            registry, cache, ws = await self._armed()
            snapshot = cache.snapshot(UID)
            cache.bump(UID)
            r = await _apply(registry, cache, ws, [], rejected=[TOPIC], snapshot=snapshot)
            return registry, cache, ws, r

        async def fence_without_reduction():
            registry, cache = TopicLeaseRegistry(), StrictObservationCache()
            ws = _WS()
            snapshot = cache.snapshot(UID)
            cache.bump(UID)
            r = await _apply(registry, cache, ws, [TOPIC], snapshot=snapshot)
            return registry, cache, ws, r

        async def expired_with_reduction():
            registry, cache, ws = await self._armed()
            r = await _apply(registry, cache, ws, [OTHER], rejected=[TOPIC],
                             now_mono=2000.0, premium=1000.0, identity=1000.0)
            return registry, cache, ws, r

        async def expired_without_reduction():
            registry, cache = TopicLeaseRegistry(), StrictObservationCache()
            ws = _WS()
            r = await _apply(registry, cache, ws, [TOPIC],
                             now_mono=2000.0, premium=1000.0, identity=1000.0)
            return registry, cache, ws, r

        async def ack_failure():
            registry, cache, ws = await self._armed()

            async def failing(ack):
                raise ConnectionResetError()

            r = await _apply(registry, cache, ws, ["usdt:krw"], send_ack=failing)
            return registry, cache, ws, r

        for name, scenario in (
            ("fence+축소", fence_with_reduction),
            ("fence-축소없음", fence_without_reduction),
            ("만료+축소", expired_with_reduction),
            ("만료-축소없음", expired_without_reduction),
            ("ack 실패", ack_failure),
        ):
            with self.subTest(scenario=name):
                registry, cache, ws, result = await scenario()
                dead = await self._is_dead(registry, cache, ws)
                self.assertEqual(
                    isinstance(result, ConnectionTerminated), dead,
                    f"{name}: 결과 타입({type(result).__name__})과 연결 생존(dead={dead})이 어긋난다",
                )

    async def test_only_one_terminal_type_exists(self):
        """⛔ 종단 타입이 둘이면 호출자가 둘 다 알아야 한다 — 하나만 알면 그게 곧 결함이다."""
        import app.topic_lease_registry as module

        terminal_names = {
            name for name in dir(module)
            if name.endswith("Failed") or name.endswith("Terminated")
        }
        self.assertEqual(terminal_names, {"ConnectionTerminated"})

    async def test_retryable_discard_really_is_retryable(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        cache.bump(UID)
        result = await _apply(registry, cache, ws, [TOPIC], snapshot=snapshot)
        self.assertIsInstance(result, Discarded)
        self.assertNotIsInstance(result, ConnectionTerminated)
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC]), Applied)

    async def test_plain_rejection_keeps_the_connection_usable(self):
        """축소가 없던 만료 거부는 연결을 죽이지 않는다 — 재인증 후 그대로 쓸 수 있다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        result = await _apply(registry, cache, ws, [TOPIC],
                              now_mono=2000.0, premium=1000.0, identity=1000.0)
        self.assertIsInstance(result, Rejected)
        self.assertNotIsInstance(result, ConnectionTerminated)
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC]), Applied)


class TestIssuanceBoundaryMatchesTheAdvertisedInteger(unittest.IsolatedAsyncioTestCase):
    """⛔ ack이 자기모순이면 안 된다 — `accepted`에 있는데 `active`에 없는 topic.

    발급 판정은 **raw float**(`now >= expires_at`)인데 광고는 **내림 정수**라, 잔여가
    0<x<1인 lease는 발급되어 `accepted`에 실리고 `active`에서는 `>0` 필터에 걸려 빠진다.
    실측: `accepted=[(krx, 0)]` / `active=[]`. 클라는 구독이 드롭됐다고 결론짓는데 서버는
    곧바로 발행을 시작하고, D6 공식상 duration 0은 **즉시 재구독**이라 루프가 된다.
    → 발급 경계를 광고하는 정수에 맞춘다.
    """

    async def test_sub_second_lease_is_not_issued(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        result = await _apply(registry, cache, ws, [TOPIC],
                              now_mono=1000.0, premium=100.4, identity=1000.0)
        self.assertIsInstance(result, Rejected)
        self.assertIsNone(registry.active_lease(ws, TOPIC))

    async def test_accepted_is_always_a_subset_of_active(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        captured = {}

        async def capture(ack):
            captured["ack"] = ack
            return True

        await _apply(registry, cache, ws, [TOPIC, OTHER], send_ack=capture,
                     now_mono=1000.0, premium=101.0, identity=1000.0)
        ack = captured["ack"]
        self.assertTrue({s.topic for s in ack.accepted} <= {s.topic for s in ack.active},
                        "ack이 수락했다고 한 topic이 최종 상태에 없다")


class TestMandatoryInputs(unittest.IsolatedAsyncioTestCase):
    """⛔ `send_ack`와 같은 논리 — 선택 인자로 두면 배선이 한 번 빠뜨렸을 때 §C2가 조용히 꺼진다."""

    async def test_rejected_topics_is_mandatory(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        with self.assertRaises(TypeError):
            await registry.apply_subscribe(
                ws=_WS(), uid=UID, topics=[TOPIC], snapshot=cache.snapshot(UID), cache=cache,
                now_mono=1000.0, premium_verified_at_mono=1000.0,
                identity_verified_at_mono=1000.0, send_ack=_ok_ack,
            )


if __name__ == "__main__":
    unittest.main()
