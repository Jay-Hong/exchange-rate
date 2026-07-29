"""topic lease registry — §8.1 B4 / C1 / C3.

운영 dispatcher를 건드리지 않는 **별도 모듈**로 먼저 만든다. 상태기계와 B4 경쟁을 여기서
test-first로 잠근 뒤에 최소 배선해야 원인 분리가 된다.

## 잠그는 계약

1. `(ws, topic, uid, lease_id)` identity + inactive/active 상태
2. 연결별 `asyncio.Lock`
3. `등록(비활성) → is_current(snapshot) → ack → 활성화`
4. **요청 단위 트랜잭션** — 한 요청 = lock 1회 = ack 1건 = 전부 아니면 전무
5. ack이 **권위 있는 상태 전체**를 싣는다(D2: 기존 topic 포함, lock 아래 단일 snapshot)
6. §C1 cross-UID = **tombstone + 종료**(B5(d) — purge 후 재바인딩은 폐기)
7. ack 실패 = **연결 사망**(tombstone) — 취소해도 이미 버퍼에 들어간 ack은 배달될 수 있다
8. `send_ack` 재진입은 **hang이 아니라 raise**
9. 요청당 단일 `now_mono` + 3-way lease 계산
"""
import asyncio
import typing
import pathlib
import unittest
from unittest.mock import patch

from app.strict_cache import StrictObservationCache
from app.topic_initial_snapshot import unleased_registration_topics
from app.topic_lease import LEASE_MAX_SECONDS
from app.topic_lease_registry import (
    ACK_TIMEOUT_SECONDS,
    CLIENT_ACK_TIMEOUT_SECONDS,
    AckState,
    SubscriberGrant,
    UnleasedApplied,
    UnleasedRegistrationDisabled,
    UnleasedRoutingError,
    Applied,
    ConnectionTerminated,
    ConnectionTerminatedError,
    Discarded,
    TransitionResult,
    Rejected,
    ReentrantRegistryCall,
    TopicLeaseRegistry,
)

_REGISTRY_SRC = pathlib.Path(__file__).resolve().parent.parent / "app" / "topic_lease_registry.py"

UID = "uid-1"
TOPIC_FX = "fx:usd-krw"
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
    result = await registry.apply_subscribe(
        ws=ws, uid=uid, topics=topics, rejected_topics=rejected, snapshot=snapshot, cache=cache,
        now_mono=now_mono, premium_verified_at_mono=premium,
        identity_verified_at_mono=identity, send_ack=send_ack,
    )
    # ⛔ **거의 모든 테스트가 지나가는 지점**이라, 여기서 보면 "반환값은 공개 union 안"이
    #    스위트 전체에 강제된다. 그전에는 이 성질이 **우연히** 지켜졌다 — 행동 테스트들이
    #    정확한 타입을 단언한 덕이었고, 부수효과만 보는 테스트는 union 밖 타입도 통과시켰다.
    #    AST trip-wire는 `return X(...)` 형태만 보므로 `r = X(...); return r`이나 별칭 생성을
    #    놓친다(실측). 이 단언이 그 구멍을 메운다.
    assert type(result) in typing.get_args(TransitionResult), (
        f"union 밖 결과 타입이 반환됐다: {type(result).__name__}"
    )
    return result


class TestIdentityAndStates(unittest.IsolatedAsyncioTestCase):
    """계약 1 — `(ws, topic, uid, lease_id)` identity와 inactive/active 상태."""

    async def test_applied_leases_carry_the_full_identity(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        result = await _apply(registry, cache, ws, [TOPIC], snapshot=snapshot)
        self.assertIsInstance(result, Applied)
        self.assertIs(result.ws, ws)
        lease = registry.authorized_lease(ws, TOPIC, now_mono=1000.0)
        self.assertEqual((lease.topic, lease.uid, lease.epoch), (TOPIC, UID, snapshot.epoch))
        self.assertTrue(lease.lease_id)

    async def test_lease_ids_are_unique_per_request(self):
        """⛔ 같은 id를 재사용하면 C3의 `(ws, topic, lease_id)` CAS가 무의미해진다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        ids = set()
        for _ in range(3):
            await _apply(registry, cache, ws, [TOPIC])
            ids.add(registry.authorized_lease(ws, TOPIC, now_mono=1000.0).lease_id)
        self.assertEqual(len(ids), 3)

    async def test_lease_ids_are_unique_across_topics_in_one_request(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER])
        self.assertNotEqual(
            registry.authorized_lease(ws, TOPIC, now_mono=1000.0).lease_id,
            registry.authorized_lease(ws, OTHER, now_mono=1000.0).lease_id,
        )

    async def test_only_active_leases_are_visible(self):
        """inactive는 **존재하되 보이지 않는다** — 활성화 전에는 전송 대상이 아니다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))
        await _apply(registry, cache, ws, [TOPIC])
        self.assertIsNotNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))


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
                                     if registry.authorized_lease(ws, t, now_mono=1000.0) is not None]
            seen["pending_at_ack"] = [t for t in (TOPIC, OTHER) if registry.has_pending(ws, t)]
            return True

        await _apply(registry, cache, ws, [TOPIC, OTHER], send_ack=probing)
        self.assertEqual(seen["active_at_ack"], [], "ack 시점에 이미 live 대상이었다")
        self.assertEqual(seen["pending_at_ack"], [TOPIC, OTHER])
        self.assertIsNotNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))
        self.assertIsNotNone(registry.authorized_lease(ws, OTHER, now_mono=1000.0))

    async def test_ack_failure_activates_none_of_them(self):
        """⛔ 부분 적용은 "ack이 광고한 상태"를 무통지로 거짓으로 만든다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()

        async def failing(ack):
            raise ConnectionResetError("socket gone")

        result = await _apply(registry, cache, ws, [TOPIC, OTHER], send_ack=failing)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))
        self.assertIsNone(registry.authorized_lease(ws, OTHER, now_mono=1000.0))
        self.assertFalse(registry.has_pending(ws, TOPIC))
        self.assertFalse(registry.has_pending(ws, OTHER))

    async def test_fence_failure_activates_none_of_them(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        cache.bump(UID)
        result = await _apply(registry, cache, ws, [TOPIC, OTHER], snapshot=snapshot)
        self.assertIsInstance(result, Discarded)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))
        self.assertIsNone(registry.authorized_lease(ws, OTHER, now_mono=1000.0))

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
        self.assertEqual(entry.lease_id, registry.authorized_lease(ws, TOPIC, now_mono=1000.0).lease_id)
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

        lease_id = registry.authorized_lease(ws, TOPIC, now_mono=1000.0).lease_id
        later = 1000.0 + LEASE_MAX_SECONDS + 1
        await _apply(registry, cache, ws, [OTHER], send_ack=capture,
                     now_mono=later, premium=later, identity=later)
        self.assertEqual({s.topic for s in captured["ack"].active}, {OTHER})
        # ⚠️ 인가 뷰(`authorized_lease`)로는 이걸 물을 수 없다 — 만료된 lease는 정의상
        #    걸러진다. "row가 아직 있는가"는 `remove()`의 CAS가 정확히 답한다.
        self.assertTrue(await registry.remove(ws, TOPIC, lease_id),
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


class TestCrossUidRequiresReconnect(unittest.IsolatedAsyncioTestCase):
    """§C1 — 한 소켓은 **평생 한 UID**다. cross-UID는 **tombstone + 종료**다(B5(d) 결정).

    ⛔ 구 규칙(purge + rebind)은 폐기됐다. purge는 *적용하기로 한* 전이의 의미만 정하고
    *적용할지*는 정하지 않아, 지연 도착한 구 UID subscribe(C4 retry, 구 토큰은 아직 유효)가
    새 UID의 구독을 purge하고 되돌려 놓을 수 있었다 — 거부 정책이면 무해했을 요청이 purge에서는
    **파괴적**이었다.
    ⛔ 단순 거부로 **연결을 살려 두는 것도** 안 된다: 구 UID 데이터가 계속 전송될 수 있다.
    """

    async def test_first_subscribe_binds_the_connection(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        self.assertEqual(registry.bound_uid(ws), UID)

    async def test_cross_uid_terminates_the_connection(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        result = await _apply(registry, StrictObservationCache(), ws, [OTHER], uid="uid-2")
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertEqual(result.reason, "reconnect_required")

    async def test_the_new_uid_is_not_bound(self):
        """⛔ 새 UID는 **새 연결에서만** 바인딩된다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        await _apply(registry, StrictObservationCache(), ws, [OTHER], uid="uid-2")
        self.assertEqual(registry.bound_uid(ws), UID, "새 UID가 살아 있는 소켓에 바인딩됐다")
        self.assertIsNone(registry.authorized_lease(ws, OTHER, now_mono=1000.0))

    async def test_old_uid_data_stops_flowing_immediately(self):
        """⛔ 거부만 하고 연결을 살려 두면 구 UID 데이터가 계속 나간다 — tombstone이 그걸 막는다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        self.assertIsNotNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))
        await _apply(registry, StrictObservationCache(), ws, [OTHER], uid="uid-2")
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0),
                          "종료 판정 뒤에도 구 UID의 lease가 인가된다")

    async def test_a_stale_old_uid_request_cannot_revert_state(self):
        """B5(d)의 원래 시나리오 — 지연 도착한 구 UID 요청이 새 연결에 와도 되돌리지 못한다."""
        registry = TopicLeaseRegistry()
        fresh = _WS("fresh")
        await _apply(registry, StrictObservationCache(), fresh, [OTHER], uid="uid-2")
        stale = await _apply(registry, StrictObservationCache(), fresh, [TOPIC], uid=UID)
        self.assertIsInstance(stale, ConnectionTerminated)
        self.assertEqual(registry.bound_uid(fresh), "uid-2", "stale 요청이 바인딩을 되돌렸다")

    async def test_different_connections_may_hold_different_uids(self):
        registry = TopicLeaseRegistry()
        # ⚠️ 연결 참조를 **붙들어야** 한다 — registry는 약한 참조라 놓으면 즉시 사라진다.
        sockets = [_WS("a"), _WS("b")]
        for ws, uid in zip(sockets, ("uid-1", "uid-2")):
            await _apply(registry, StrictObservationCache(), ws, [TOPIC], uid=uid)
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
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0), "폐기했는데 흔적이 남았다")

    async def test_registration_happens_before_the_recheck(self):
        """⛔ 순서가 뒤집히면(재확인 → 등록) 그 사이 무효화를 못 본다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        observed = {}
        real_is_current = cache.is_current

        def spy(snap):
            observed["registered"] = registry.has_pending(ws, TOPIC)
            observed["visible"] = registry.authorized_lease(ws, TOPIC, now_mono=1000.0)
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
            registry.authorized_lease(ws, TOPIC, now_mono=1000.0).expires_at_mono, 940.0 + LEASE_MAX_SECONDS
        )

    async def test_every_topic_in_a_request_shares_one_expiry(self):
        """D1 — 한 ack 안의 accepted들은 **같은 순간** 갱신된다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER])
        self.assertEqual(
            registry.authorized_lease(ws, TOPIC, now_mono=1000.0).expires_at_mono,
            registry.authorized_lease(ws, OTHER, now_mono=1000.0).expires_at_mono,
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
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))

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
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))

    async def test_pre_existing_leases_stop_authorizing_after_an_ack_failure(self):
        """⛔ tombstone이 **신규 전이만** 막으면 부족하다.

        close가 지연되거나 실패하면, 죽었다고 판정한 소켓으로 **기존 UID의 데이터가 계속**
        나간다. cross-UID 상황에서는 그게 곧 entitlement 우회다: A의 KRX lease가 살아 있는
        채로 클라는 (늦게 도착한 ack을 보고) B 세션이라고 믿는다.
        → 인가 응답인 `authorized_lease`가 tombstone에서 **즉시 fail-closed**여야 한다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        self.assertIsNotNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))

        async def failing(ack):
            raise ConnectionResetError()

        self.assertIsInstance(await _apply(registry, cache, ws, [OTHER], send_ack=failing),
                              ConnectionTerminated)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0),
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
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0), "취소를 삼켰는데 활성화됐다")

    async def test_late_ack_after_the_budget_is_not_success(self):
        """⛔ 예산이 지난 뒤 도착한 `True`를 성공으로 인정하면 안 된다.

        ⚠️ 이 모듈 문서는 한때 "예산 만료를 삼킨 경우만 여전히 구별 불가"라고 적었는데
        **틀렸다** — `asyncio.timeout(...).expired()`가 시계를 읽지 않고 구별한다(실측).
        인정하면 ack이 실제로 못 나갔는데 lease가 활성화되고, 클라는 자기가 구독한 줄 모른다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        release = asyncio.Event()

        async def swallowing(ack):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await release.wait()
                return True
            return True

        with patch("app.topic_lease_registry.ACK_TIMEOUT_SECONDS", 0.01):
            task = asyncio.create_task(_apply(registry, cache, ws, [TOPIC], send_ack=swallowing))
            await asyncio.sleep(0.03)
            release.set()
            result = await asyncio.wait_for(task, timeout=5)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))

    async def test_ack_send_budget_alone_cannot_exhaust_the_client_deadline(self):
        """§D6 클라 ack timeout(10s)을 **ack 송신 한 단계만으로** 태워선 안 된다.

        ⛔ **필요조건이지 충분조건이 아니다.** `< 10`이라고 클라가 먼저 포기하지 않는다는
        뜻이 **아니다** — 클라의 10초는 요청→ack **전체**를 재는데 그 앞에 상한 없는 lock
        대기가 있고, 알려진 몫(검증 8s + 송신 5s)만 더해도 이미 13s > 10s다(§D-const).
        이 단언이 겨냥하는 좁은 실패: 예산이 10s 이상이면 **예산 안에서 성공한** ack이
        클라가 이미 포기한 뒤일 수 있고, 그러면 서버는 클라가 버린 lease를 활성화한다.
        구 이름·docstring은 이 필요조건을 충분조건처럼 적었다.

        ⛔ 다만 **이 단언 단독으로는 그 축을 못 막는다** — `ACK`만 키우는 변이는 잡지만
        `ACK`와 `CLIENT_ACK`가 **같이** 움직이면 green이다(실측: `ACK 5.0→1.0` +
        `CLIENT_ACK 10.0→3600.0` → 이 단언 green, red는 리터럴 pin 2건뿐).
        그 축을 실제로 잡는 것은 `TestRegistryConstantsArePinned`의 절대값 pin이다.
        """
        self.assertLess(ACK_TIMEOUT_SECONDS, CLIENT_ACK_TIMEOUT_SECONDS)

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
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))
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
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0), "만료된 lease가 활성화됐다")

    async def test_boundary_is_inclusive(self):
        """§A2 — `now >= expires_at`이 만료다(fail-closed)."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        result = await _apply(registry, cache, _WS(), [TOPIC],
                              now_mono=1000.0 + LEASE_MAX_SECONDS, premium=1000.0, identity=1000.0)
        self.assertIsInstance(result, Rejected)

    async def test_a_removal_only_request_is_not_blocked_by_an_expired_horizon(self):
        """⛔ 발급할 lease가 없으면 horizon은 무관하다.

        여기서 거부하면 **접근을 없애기만 하는 요청**이 막힌다 — B의 소켓이 A의 데이터를
        계속 받는 fail-open이다. 만료 판정은 "발급"에만 건다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER], now_mono=1000.0)

        result = await _apply(registry, cache, ws, [], rejected=[TOPIC],
                              now_mono=5000.0, premium=1000.0, identity=1000.0)
        self.assertIsInstance(result, Applied)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))


class TestMutatorsAreNotB4Bypasses(unittest.IsolatedAsyncioTestCase):
    """⛔ 연결 lock을 안 잡는 public 변경 API가 있으면 그게 **B4 우회로**다."""

    def test_public_mutators_acquire_the_owning_lock(self):
        """⛔ **소유를 기록하는** 획득 경로여야 한다.

        구 버전은 `"connection_lock" in ast.dump(...)`로 판정했는데, 그 문자열은
        `hold_connection_lock`의 **부분 문자열**이라 소유 기록 없는 구 형태로 되돌려도
        그대로 통과했다(실측 BLIND). 게다가 `remove` 하나만 봤다 — 리포 최대 전이인
        `apply_subscribe`의 소유 계약은 어떤 테스트도 관측하지 않았다.
        """
        import ast
        import inspect

        import app.topic_lease_registry as module

        tree = ast.parse(inspect.getsource(module))
        registry = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.ClassDef) and n.name == "TopicLeaseRegistry")
        for name in ("apply_subscribe", "remove", "remove_websocket"):
            method = next(n for n in registry.body
                          if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
            owning = [
                item for node in ast.walk(method) if isinstance(node, ast.AsyncWith)
                for item in node.items
                if isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr == "hold_connection_lock"
            ]
            self.assertEqual(len(owning), 1, f"{name}이 소유 기록 획득 경로를 쓰지 않는다")

    def test_every_injected_sender_is_wrapped_in_the_cancellation_fence(self):
        """⛔ 검사를 손으로 복제하면 빠뜨린다 — 실제로 두 곳(unsubscribe·sweep)을 빠뜨렸다.

        주입된 sender를 `await`하는 지점은 전부 `cancellation_fence()` 안이어야 한다.
        """
        import ast
        import inspect

        import app.topic_lease_registry as registry_module
        import app.topic_lease_sweeper as sweeper_module

        senders = {"send_ack", "send_reauth"}
        for module in (registry_module, sweeper_module):
            tree = ast.parse(inspect.getsource(module))
            fenced = set()
            for node in ast.walk(tree):
                if not (isinstance(node, ast.With) and any(
                    isinstance(i.context_expr, ast.Call)
                    and getattr(i.context_expr.func, "id", None) == "cancellation_fence"
                    for i in node.items
                )):
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                        fenced.add(inner.func.id)
            called = {
                node.func.id for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in senders
            }
            self.assertTrue(called or module is registry_module,
                            f"{module.__name__}: sender 호출을 못 찾았다 — 탐지기 고장")
            self.assertLessEqual(
                called, fenced,
                f"{module.__name__}: fence 밖에서 sender를 부른다: {sorted(called - fenced)}",
            )

    def test_claim_marks_are_read_only_through_the_lease_aware_helper(self):
        """⛔ `_claimed`를 topic 문자열로 읽는 지점이 **하나라도 새로 생기면** 영구 차단이 부활한다.

        §C-API 2(`get_subscribers` topic 인덱스)가 열려 있어 그 슬라이스가 가장 자연스러운
        형태(`if topic in self._claimed...`)로 제외를 재구현할 여지가 크다. 읽기 지점을
        `_is_claimed`(Lease를 받는다) 하나로 묶어 잘못된 질문 자체를 표현 불가능하게 한다.
        """
        import ast
        import inspect

        import app.topic_lease_registry as module

        tree = ast.parse(inspect.getsource(module))
        registry = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.ClassDef) and n.name == "TopicLeaseRegistry")
        # ⚠️ 표식 **변경**은 `_drop_claim_locked` 하나로 좁혔고, **조회**는 `_is_claimed` 하나다.
        #    `apply_unsubscribe`가 직접 만지려다 이 trip-wire에 걸려 그렇게 정리됐다.
        allowed = {"_is_claimed", "claimed_topics", "claim_expired_locked",
                   "_drop_claim_locked", "teardown_locked", "__init__"}
        touching = set()
        for method in registry.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(method):
                if isinstance(node, ast.Attribute) and node.attr == "_claimed":
                    touching.add(method.name)
        self.assertTrue(touching, "탐지기가 `_claimed` 접근을 하나도 못 찾았다 — 고장")
        self.assertLessEqual(
            touching, allowed,
            f"`_claimed`를 새 지점에서 읽는다: {sorted(touching - allowed)} — `_is_claimed`를 쓸 것",
        )

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
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0), "teardown 뒤 구독이 되살아났다")


class TestRemovalCas(unittest.IsolatedAsyncioTestCase):
    """§C3 — 제거·갱신은 `(ws, topic, lease_id)` CAS다."""

    async def test_removal_requires_the_matching_lease_id(self):
        """⛔ 구 sweep이 **갱신된** lease를 지우면 살아 있는 구독이 사라진다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        first_id = registry.authorized_lease(ws, TOPIC, now_mono=1000.0).lease_id
        await _apply(registry, cache, ws, [TOPIC])          # 갱신
        second_id = registry.authorized_lease(ws, TOPIC, now_mono=1000.0).lease_id

        self.assertFalse(await registry.remove(ws, TOPIC, first_id), "구 id로 지워졌다")
        self.assertIsNotNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))
        self.assertTrue(await registry.remove(ws, TOPIC, second_id))
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))

    async def test_remove_websocket_clears_everything_for_that_connection(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws, other = _WS("a"), _WS("b")
        await _apply(registry, cache, ws, [TOPIC])
        await _apply(registry, cache, other, [TOPIC])
        await registry.remove_websocket(ws)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))
        self.assertIsNone(registry.bound_uid(ws))
        self.assertIsNotNone(registry.authorized_lease(other, TOPIC, now_mono=1000.0), "남의 연결까지 지웠다")


class TestC2RejectEviction(unittest.IsolatedAsyncioTestCase):
    """§C2 — 인증 성공한 subscribe에서 **reject된 topic은 registry에서 제거**한다(§C2).

    ⛔ accepted만 받으면 권한을 잃은 기존 등록이 **lease 만료까지 잔존**한다(§C2).
    같은 UID 재인증에서 KRX entitlement를 잃어도 최대 15분 더 KRX 데이터가 나간다는 뜻이다.
    §D2는 `removed_topics`의 producer로 "C2의 reject eviction"을 명시한다.
    """

    async def test_rejected_topic_that_was_active_is_evicted(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [OTHER, TOPIC])

        result = await _apply(registry, cache, ws, [OTHER], rejected=[TOPIC])
        self.assertIsInstance(result, Applied)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0), "권한 잃은 구독이 만료까지 살아남았다")
        self.assertEqual(result.ack.removed, (TOPIC,))
        self.assertEqual({s.topic for s in result.ack.active}, {OTHER})

    async def test_unmentioned_topics_are_untouched(self):
        """§C2 — 이번 요청에 **언급되지 않은** topic은 불변(증분 subscribe 보존)."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC, OTHER, "usdt:krw"])

        result = await _apply(registry, cache, ws, [], rejected=[TOPIC])
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))
        self.assertIsNotNone(registry.authorized_lease(ws, OTHER, now_mono=1000.0), "언급 없던 topic이 지워졌다")
        self.assertIsNotNone(registry.authorized_lease(ws, "usdt:krw", now_mono=1000.0))
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

        lease_id = registry.authorized_lease(ws, TOPIC, now_mono=1000.0).lease_id
        result = await _apply(registry, cache, ws, [], rejected=[TOPIC], send_ack=failing)
        self.assertIsInstance(result, ConnectionTerminated)
        # ⚠️ `authorized_lease`는 tombstone에서 fail-closed라 여기선 관측 도구가 못 된다.
        #    `remove()`의 CAS는 tombstone을 보지 않으므로 "그 lease가 아직 존재하는가"를 답한다.
        #    ⛔ 구 버전은 `identity_generation == 1`을 단언했는데(그 필드는 이후 제거됐다), 같은 UID 재인증에서는
        #    커밋 여부와 무관하게 항상 참이라 **공허**했다 — 제거를 ack 전에 커밋하는 변이가
        #    전체 스위트를 통과했다(실측).
        self.assertTrue(await registry.remove(ws, TOPIC, lease_id),
                        "ack이 실패했는데 제거가 커밋됐다(all-or-nothing 위반)")

    async def test_topic_both_accepted_and_rejected_is_a_caller_bug(self):
        """⛔ D5 단계상 한 topic이 둘 다일 수 없다. 조용히 한쪽을 고르면 나중에 물린다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        with self.assertRaises(ValueError):
            await _apply(registry, cache, _WS(), [TOPIC], rejected=[TOPIC])


class TestFirstBindIsAtomic(unittest.IsolatedAsyncioTestCase):
    """바인딩은 ack이 확인된 뒤에만 커밋된다 — 실패한 첫 바인딩은 UID를 남기지 않는다.

    ⚠️ 이 클래스는 구 `TestConnectionIdentityGeneration`의 잔존물이다. `identity_generation`은
    B5(d)로 의미가 소진돼(재바인딩 불가 → 바인딩 후 상수) wire·내부 machinery 전부 제거했다.
    그러나 그 클래스가 **generation을 통해 우연히 잠그고 있던** 성질 하나는 제거 대상이 아니라
    여기로 옮겼다 — 실패한 첫 바인딩이 상태를 남기지 않는다는 것. 전수 확인 결과 이 성질을
    잠그는 다른 테스트가 없었다(`bound_uid`를 보는 곳은 `remove_websocket` 테스트뿐이었다).

    ⛔ 구 클래스가 잠그던 나머지는 여기 없다. 근거: cross-UID → `ConnectionTerminated`는
    `test_a_cross_uid_attempt_stops_the_captured_decision`이 이미 잠근다(중복이었다).
    "값이 상수"·"미바인딩은 0"은 필드와 함께 사라진 계약이라 잠글 대상이 없다.

    ⛔ 역사적 주의(서버 측 generation을 되살린다면 유효): strict cache의 `snapshot.epoch`을
    쓰면 안 된다 — **UID별**이고 최초 관측 순서로 할당돼 순서 판별이 뒤집힌다(실측 A=2→B=1).
    """

    async def test_a_failed_first_bind_leaves_no_uid(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()

        async def failing(ack):
            raise ConnectionResetError()

        await _apply(registry, cache, ws, [TOPIC], send_ack=failing)
        self.assertIsNone(registry.bound_uid(ws), "실패한 바인딩이 UID를 남겼다")


class TestAbortedAccessReductionIsFailClosed(unittest.IsolatedAsyncioTestCase):
    """⛔ **접근을 줄이는 전이는 중단돼도 되돌아가지 않는다.**

    되돌아가려면 그만큼의 접근이 계속 살아 있어야 하는데, 그게 정확히 C1/C2가 막으려는 상태다.
    구 구현은 축소(당시 purge·eviction)를 **발급 성공에 종속**시켜, 세 중단 경로(만료 horizon / fence
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
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0), "거부된 topic이 계속 인가된다")

    async def test_expired_horizon_with_multiple_evictions_closes_the_connection(self):
        registry, cache, ws = await self._armed()
        # ⚠️ 만료 게이트는 **발급**에만 걸린다 — topics가 비면 애초에 통과한다(의도).
        #    그래서 발급 대상을 하나 넣어 게이트를 태우고, 동시에 축소를 예정해 둔다.
        result = await _apply(registry, cache, ws, ["usdt:krw"], rejected=[TOPIC, OTHER],
                              now_mono=2000.0, premium=1000.0, identity=1000.0)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0), "B 소켓에서 A의 lease가 계속 인가된다")
        self.assertIsNone(registry.authorized_lease(ws, OTHER, now_mono=1000.0))

    async def test_fence_failure_with_pending_eviction_closes_the_connection(self):
        registry, cache, ws = await self._armed()
        snapshot = cache.snapshot(UID)
        cache.bump(UID)
        result = await _apply(registry, cache, ws, [OTHER], rejected=[TOPIC], snapshot=snapshot)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))

    async def test_fence_failure_with_multiple_evictions_closes_the_connection(self):
        registry, cache, ws = await self._armed()
        snapshot = cache.snapshot(UID)
        cache.bump(UID)
        result = await _apply(registry, cache, ws, [], rejected=[TOPIC, OTHER],
                              snapshot=snapshot)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))

    async def test_axis_violation_with_pending_eviction_closes_the_connection(self):
        """축 위반(wall epoch 혼입 등)은 예외로 크게 터지지만, 그 전에 접근은 닫혀야 한다.

        ⛔ 그리고 **예외 채널에도 종단 신호가 있어야** 한다 — tombstone만 찍고 원래 예외를
        그대로 던지면, `ConnectionTerminated`만 분기하는 호출자는 close 필요성을 알 수 없다.
        지금은 직렬 endpoint라 예외가 상위 루프를 빠져나가 teardown으로 이어지지만,
        §B5(b) task-spawn 배선에서는 task 예외가 소켓 종료로 연결되지 않으면 다시
        무데이터·무오류 상태가 된다.
        """
        registry, cache, ws = await self._armed()
        with self.assertRaises(ConnectionTerminatedError) as caught:
            await _apply(registry, cache, ws, [], rejected=[TOPIC], premium=float("nan"))
        self.assertIsInstance(caught.exception.__cause__, ValueError,
                              "원인이 끊겨 진단이 사라졌다")
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0), "예외 경로가 fail-open이었다")

    async def test_axis_violation_without_reduction_propagates_raw(self):
        """⛔ 줄일 것이 없으면 연결은 멀쩡하다 — 감싸면 배선이 멀쩡한 연결을 닫는다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        with self.assertRaises(ValueError):
            await _apply(registry, cache, ws, [TOPIC], premium=float("nan"))
        self.assertNotIsInstance(await _apply(registry, cache, ws, [TOPIC]),
                                 ConnectionTerminated)

    async def test_cancellation_is_never_wrapped(self):
        """⛔ `CancelledError`를 감싸면 asyncio의 **구조적 취소가 깨진다**.

        `wait_for`·`TaskGroup`이 그 예외로 동작하므로, 감싸는 순간 취소가 전파되지 않는다.
        (`CancelledError`는 `Exception`이 아니라 `BaseException`이라 감싸기 대상에서 자연히
        빠진다 — 실측으로 확인한 전제다.)
        """
        registry, cache, ws = await self._armed()
        entered = asyncio.Event()

        async def stalling(ack):
            entered.set()
            await asyncio.sleep(30)
            return True

        task = asyncio.create_task(
            _apply(registry, cache, ws, ["usdt:krw"], rejected=[TOPIC], send_ack=stalling)
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_abort_without_any_reduction_leaves_the_connection_usable(self):
        """⛔ 오탐 금지 — 줄일 게 없던 중단까지 연결을 죽이면 정상 webhook 경쟁이 연결을 끊는다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        snapshot = cache.snapshot(UID)
        cache.bump(UID)
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC], snapshot=snapshot),
                              Discarded)
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC]), Applied)

    async def test_topicless_request_still_validates_the_time_axis(self):
        """⛔ 만료 **판정**만 `topics`에 걸어야 한다 — 입력 검증까지 건너뛰면 축 혼입이 조용해진다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        with self.assertRaises(ConnectionTerminatedError) as caught:
            await _apply(registry, cache, ws, [], rejected=[TOPIC], premium=float("nan"))
        self.assertIsInstance(caught.exception.__cause__, ValueError,
                              "축 검증이 돌지 않았다")


class TestSendAuthorizationChecksIdentity(unittest.IsolatedAsyncioTestCase):
    """§B1 — **전송 직전** 재검증은 캡처한 `(uid, lease_id)`와 정확히 일치해야 한다(§B1 검사 항목).

    ⛔ `authorized_lease`는 §B2(조회 경계 필터)의 답이지 §B1의 답이 아니다. 실측:

      같은 UID 재인증  : L1 캡처 → L2로 교체 → 재확인이 L2를 돌려줘 **L1 기준 결정이 통과**
      cross-UID 재바인딩: 캡처 uid=A → 현재 uid=B → A로 내린 결정이 B의 소켓에 적용
                        (⚠️ 이 변종은 B5(d)로 **경로가 사라졌다** — `uid` 축은 이제 호출부 오류 방어다)

    `is not None`만 보는 것이 가장 자연스러운 사용법이라, identity를 **필수 입력**으로 받아
    registry가 직접 대조해야 그 오용이 불가능해진다.
    """

    async def _issue(self, registry, cache, ws, uid=UID, topic=TOPIC):
        await _apply(registry, cache, ws, [topic], uid=uid)
        return registry.authorized_lease(ws, topic, now_mono=1000.0)

    async def test_renewed_lease_does_not_authorize_the_captured_one(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        captured = await self._issue(registry, cache, ws)
        renewed = await self._issue(registry, cache, ws)
        self.assertNotEqual(captured.lease_id, renewed.lease_id, "전제가 성립하지 않는다")

        self.assertFalse(
            registry.authorizes_send(ws, TOPIC, uid=UID, lease_id=captured.lease_id,
                                     now_mono=1000.0),
            "교체된 lease가 구 캡처 기준 전송을 인가한다",
        )
        self.assertTrue(
            registry.authorizes_send(ws, TOPIC, uid=UID, lease_id=renewed.lease_id,
                                     now_mono=1000.0)
        )

    async def test_a_cross_uid_attempt_stops_the_captured_decision(self):
        """⚠️ 이름 정정: 이 테스트가 잠그는 것은 **uid 축 대조**가 아니라 **tombstone**이다.

        B5(d) 이후 cross-UID는 연결 종료라 `authorizes_send`가 False인 이유는 uid 불일치가 아니라
        tombstone이다 — 구 이름(`..._rebound_uid_...`)은 존재하지 않는 계약을 광고했다.
        uid 축 자체는 `test_uid_mismatch_alone_blocks_the_send`가 잠근다(호출부 오류 방어).
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        captured = await self._issue(registry, cache, ws, uid="uid-A")
        terminated = await _apply(registry, StrictObservationCache(), ws, [TOPIC], uid="uid-B")
        self.assertIsInstance(terminated, ConnectionTerminated)
        self.assertFalse(
            registry.authorizes_send(ws, TOPIC, uid="uid-A", lease_id=captured.lease_id,
                                     now_mono=1000.0),
            "종료 판정 뒤에도 캡처한 결정이 통과한다",
        )

    async def test_uid_mismatch_alone_blocks_the_send(self):
        """⛔ `lease_id`만 보면 uid 축이 열린다 — 둘 다 대조해야 §B1의 identity가 성립한다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        lease = await self._issue(registry, cache, ws)
        self.assertFalse(
            registry.authorizes_send(ws, TOPIC, uid="someone-else", lease_id=lease.lease_id,
                                     now_mono=1000.0)
        )

    async def test_expired_lease_does_not_authorize_a_send(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        lease = await self._issue(registry, cache, ws)
        self.assertFalse(
            registry.authorizes_send(ws, TOPIC, uid=UID, lease_id=lease.lease_id,
                                     now_mono=1000.0 + LEASE_MAX_SECONDS)
        )

    async def test_tombstone_blocks_the_send(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        lease = await self._issue(registry, cache, ws)

        async def failing(ack):
            raise ConnectionResetError()

        await _apply(registry, cache, ws, [OTHER], send_ack=failing)
        self.assertFalse(
            registry.authorizes_send(ws, TOPIC, uid=UID, lease_id=lease.lease_id,
                                     now_mono=1000.0)
        )

    async def test_unknown_topic_is_not_authorized(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        lease = await self._issue(registry, cache, ws)
        self.assertFalse(
            registry.authorizes_send(ws, "never:subscribed", uid=UID,
                                     lease_id=lease.lease_id, now_mono=1000.0)
        )

    def test_send_authorization_delegates_the_liveness_rules(self):
        """⛔ tombstone·만료 규칙을 여기서 다시 쓰면 §B2 필터와 두 곳이 어긋난다."""
        import ast
        import inspect

        import app.topic_lease_registry as module

        tree = ast.parse(inspect.getsource(module))
        registry = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.ClassDef) and n.name == "TopicLeaseRegistry")
        method = next(n for n in registry.body
                      if isinstance(n, ast.FunctionDef) and n.name == "authorizes_send")
        called = {n.func.attr for n in ast.walk(method)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertIn("authorized_lease", called, "생존 규칙을 직접 구현했다")


class TestLookupBoundaryFilter(unittest.IsolatedAsyncioTestCase):
    """§B2 — 조회 경계 필터. **캡처된 identity가 아직 없는** 단계의 1차 방어다.

    ⛔ 구 `active_lease`는 docstring으로 "인가 응답"을 자처하면서 시각을 받지 않았다 —
    실측: 만료 10000초 뒤에도 lease를 반환했다. sweep이 늦거나 아직 미배선이면 그 답을 믿는
    전송 경로가 **영원히** 통과한다. 시각을 필수 인자로 만들어 만료 미확인 답 자체를
    얻을 수 없게 했다.
    """

    async def _issued(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC], now_mono=1000.0,
                     premium=1000.0, identity=1000.0)
        return registry, ws                       # expiry = 1000 + LEASE_MAX

    async def test_expired_lease_does_not_authorize(self):
        registry, ws = await self._issued()
        self.assertIsNotNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))
        self.assertIsNone(
            registry.authorized_lease(ws, TOPIC, now_mono=1000.0 + LEASE_MAX_SECONDS + 1),
            "만료된 lease가 전송을 인가한다 — sweep이 늦으면 영원히 나간다",
        )

    async def test_authorization_boundary_is_inclusive(self):
        """§A2 — `now >= expires_at`이 만료다(fail-closed). 발급 경계와 같은 규약이어야 한다."""
        registry, ws = await self._issued()
        self.assertIsNone(
            registry.authorized_lease(ws, TOPIC, now_mono=1000.0 + LEASE_MAX_SECONDS)
        )

    async def test_non_finite_now_is_fail_closed(self):
        """비유한 시각은 "판정 불가"다 — `is_expired`에 위임했으므로 규약이 자동으로 따라온다."""
        registry, ws = await self._issued()
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=float("nan")))

    async def test_expiry_check_is_delegated_not_reimplemented(self):
        """⛔ 경계·비유한 규약을 여기서 다시 쓰면 §A2와 두 곳이 어긋난다."""
        import ast
        import inspect

        import app.topic_lease_registry as module

        tree = ast.parse(inspect.getsource(module))
        registry = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.ClassDef) and n.name == "TopicLeaseRegistry")
        method = next(n for n in registry.body
                      if isinstance(n, ast.FunctionDef) and n.name == "authorized_lease")
        called = {n.func.id for n in ast.walk(method)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn("is_expired", called, "만료 판정을 직접 구현했다")

    async def test_tombstone_still_fails_closed(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])

        async def failing(ack):
            raise ConnectionResetError()

        await _apply(registry, cache, ws, [OTHER], send_ack=failing)
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))


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

    async def test_tombstone_always_produces_a_terminal_signal_on_either_channel(self):
        """⛔ tombstone을 찍는 **모든** 이탈은 종단 신호를 낸다 — 결과면 `ConnectionTerminated`,
        예외면 `ConnectionTerminatedError`. 한쪽만 지키면 다른 쪽으로 close 의무가 샌다.
        """
        async def returns_terminal():
            registry, cache, ws = await self._armed()
            snapshot = cache.snapshot(UID)
            cache.bump(UID)
            try:
                r = await _apply(registry, cache, ws, [], rejected=[TOPIC], snapshot=snapshot)
                return registry, cache, ws, r, None
            except BaseException as exc:                     # pragma: no cover - 방어
                return registry, cache, ws, None, exc

        async def raises_terminal():
            registry, cache, ws = await self._armed()
            other = StrictObservationCache()
            try:
                r = await _apply(registry, other, ws, [OTHER], uid="uid-2", premium=float("nan"))
                return registry, cache, ws, r, None
            except BaseException as exc:
                return registry, cache, ws, None, exc

        for name, scenario in (("fence+축소(결과)", returns_terminal),
                               ("축위반+축소(예외)", raises_terminal)):
            with self.subTest(scenario=name):
                registry, cache, ws, result, exc = await scenario()
                signalled = isinstance(result, ConnectionTerminated) or isinstance(
                    exc, ConnectionTerminatedError
                )
                dead = await self._is_dead(registry, cache, ws)
                self.assertEqual(signalled, dead,
                                 f"{name}: tombstone={dead}인데 종단 신호={signalled}")

    def test_result_union_is_exactly_the_documented_four(self):
        """⛔ 호출자가 분기해야 하는 **결과 타입 집합**을 공개 union으로 못 박는다.

        구 버전은 이름이 `Failed`/`Terminated`로 끝나는 클래스만 셌다. 그러면
        `ConnectionClosed` 같은 이름으로 종단 결과가 하나 더 들어와도 통과하고(실측 SURVIVED),
        union에서 하나가 조용히 **빠져도** 통과한다(실측 SURVIVED). 이름 규칙이 아니라
        **계약 표면 자체**를 본다.

        새 멤버가 생기면 이 테스트가 먼저 깨진다 — 그때 "이것이 종단인가"를 판단하고 호출자의
        close 분기를 함께 고치라는 뜻이다.
        ⚠️ "그중 종단은 정확히 하나"라는 **의미**는 이 테스트가 지지 않는다(타입 이름으로는 알
        수 없다). 그건 행동 테스트가 진다 — `test_every_abort_path_agrees_with_its_result_type`,
        `test_retryable_discard_really_is_retryable`, `test_plain_rejection_keeps_the_connection_usable`.
        """
        import typing

        import app.topic_lease_registry as module

        self.assertEqual(
            set(typing.get_args(module.TransitionResult)),
            {module.Applied, module.Discarded, module.Rejected, module.ConnectionTerminated},
        )

    def test_directly_constructed_return_values_are_in_the_union(self):
        """소스 수준 trip-wire — `return X(...)`로 **직접 생성**되는 타입만 본다.

        ⚠️ **이름이 약속하는 범위를 정확히 적는다.** 이 테스트는 "모든 반환 경로"를 보장하지
        않는다. 실측으로 확인한 사각지대:
          - `result = ConnectionClosed(...)` 다음 `return result` (Return 안에 Call이 없다)
          - 별칭·attribute 경유 생성(`return _make(...)`) — func가 모듈 타입 이름이 아니다
          - `result_makers`에 없는 새 helper를 통한 반환
        AST에 dataflow를 붙여 이걸 메우는 대신, **실제 반환값**을 보는 `_apply`의 런타임 단언이
        본 그물이다(탐지기를 키우면 탐지기 자체가 버그 원천이 된다 — 이 세션에서 두 번 겪었다).
        이 테스트는 테스트가 **실행하지 않는** 반환 경로에 대한 보조 방어로 남긴다.
        """
        import ast
        import inspect
        import typing

        import app.topic_lease_registry as module

        tree = ast.parse(inspect.getsource(module))
        module_types = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        registry = next(n for n in tree.body
                        if isinstance(n, ast.ClassDef) and n.name == "TopicLeaseRegistry")
        result_makers = {"apply_subscribe", "_abort_locked", "_terminate_locked"}
        constructed = set()
        for method in registry.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if method.name not in result_makers:
                continue
            for returned in ast.walk(method):
                if not isinstance(returned, ast.Return):
                    continue
                for node in ast.walk(returned):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id in module_types):
                        constructed.add(node.func.id)

        # ⚠️ 탐지기 자기 검사 — 아무것도 못 찾으면 아래 부분집합 단언이 **공허하게** 통과한다.
        self.assertTrue(constructed, "반환 경로에서 결과 타입을 하나도 못 찾았다 — 탐지기 고장")
        allowed = {t.__name__ for t in typing.get_args(module.TransitionResult)}
        self.assertLessEqual(
            constructed, allowed, f"union 밖 타입을 반환한다: {sorted(constructed - allowed)}"
        )

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
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))

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


class TestDocumentationAnchors(unittest.IsolatedAsyncioTestCase):
    """⛔ 계획을 **행번호로 인용하지 않는다** — 계획을 한 줄만 고쳐도 인용이 전부 어긋난다.

    이 규칙을 세운 커밋의 **바로 다음 커밋에서 내가 다시 어겼다**. 사람이 지키는 규칙으로는
    부족하다는 증거라 기계로 잠갔는데, **그 trip-wire도 처음엔 틀렸다**: 정규식이 "계획"
    접두사를 요구해서 접두사 없는 인용을 전부 놓쳤다. 그래서 "위반 1건뿐"·"전량 제거"라고
    보고했지만 실제로는 6건이 남아 있었다(그중 2건은 섹션 앵커와 행번호를 **함께** 쓴 형태).

    ⛔ **교훈: 검사는 내가 기억하는 형태가 아니라 내가 선언한 규칙의 모양이어야 한다.**
    검증에 쓴 grep도 같은 접두사를 박아 둬서 같은 눈먼 지점을 공유했다 — 규칙과 검사가
    같은 오해를 공유하면 검사는 통과 도장을 찍어 줄 뿐이다.

    범위는 규칙이 선언된 두 파일이다 — 리포 전체 강제는 이 슬라이스의 범위 밖이다
    (실측: 지금 이 패턴을 쓰는 파일은 이 두 개뿐이었다).
    """

    def test_no_plan_line_number_citations(self):
        import pathlib
        import re

        # 접두사를 요구하지 않는다. 범위 인용(`N~M행`)도 잡는다.
        # ⚠️ `발행`·`진행`·`행동`은 앞에 숫자가 없어 매치되지 않는다(실측 확인).
        cited = re.compile(r"\d+\s*(?:~\s*\d+\s*)?행")
        root = pathlib.Path(__file__).resolve().parents[1]
        for name in ("app/topic_lease_registry.py", "tests/test_topic_lease_registry.py"):
            hits = [
                f"{name}:{number}"
                for number, line in enumerate(
                    (root / name).read_text(encoding="utf-8").splitlines(), 1
                )
                if cited.search(line)
            ]
            self.assertEqual(hits, [], "§ 섹션 앵커를 쓸 것 — 행번호는 계획 편집마다 어긋난다")


class TestCancellationFence(unittest.IsolatedAsyncioTestCase):
    """⛔ 외부 취소는 **다른 예외보다 우선**해야 한다.

    구 구현은 `yield` 뒤에 검사를 뒀다 — 블록이 예외를 던지면 검사에 **도달하지 않는다**.
    나는 그걸 "의도"라고 적으면서 "그 예외가 이미 실패를 말한다"를 근거로 들었는데 **틀렸다**:
    예외는 "이 작업이 실패했다"를 말하고 취소는 "이 task를 접어라"를 말한다. 호출자가 예외를
    결과값으로 바꾸는 순간(ConnectionTerminated) 후자만 사라진다 — 실측 확인.
    """

    async def test_cancellation_wins_over_an_exception_raised_after_swallowing(self):
        from app.topic_lease_registry import cancellation_fence

        async def body():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise ConnectionResetError("소켓도 죽었다")

        async def run():
            with cancellation_fence():
                await body()

        task = asyncio.create_task(run())
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertIsInstance(caught.exception.__context__, ConnectionResetError,
                              "원래 예외가 진단에서 사라졌다")

    async def test_an_ordinary_exception_is_untouched_without_cancellation(self):
        """⛔ 오탐 금지 — 취소가 없었으면 원래 예외가 그대로 나가야 한다."""
        from app.topic_lease_registry import cancellation_fence

        async def run():
            with cancellation_fence():
                raise ValueError("평범한 실패")

        with self.assertRaises(ValueError):
            await run()


class TestUnsubscribeIsFailOpen(unittest.IsolatedAsyncioTestCase):
    """§D8 — `unsubscribe`는 **접근을 줄이는** 작업이라 인증 성공에 종속되면 안 된다.

    계획: "`unsubscribe`를 인증 성공에 종속시키면 권한 축소가 실패한다. 토큰 만료·revocation·
    Firebase 장애 시 제거가 막혀 **사용자가 끄라고 한 데이터가 lease 만료까지 계속 흐른다**."
    → `id_token` 불요, premium/entitlement 재검증 없이 **idempotent하게 제거한 뒤 ack**.

    ⚠️ 그래서 subscribe와 **순서가 반대**다. subscribe는 `ack → 활성화`(활성화가 ack을 앞서면
    클라가 모르는 데이터가 먼저 온다). unsubscribe는 `제거 → ack`(ack을 기다리는 동안 끄라고 한
    데이터가 계속 흐르면 안 된다). 두 경우 모두 **위험한 쪽이 나중**이다.
    """

    async def _subscribed(self, topics=(TOPIC, OTHER)):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, list(topics))
        return registry, ws

    async def test_active_topic_is_removed_and_reported(self):
        registry, ws = await self._subscribed()
        result = await registry.apply_unsubscribe(
            ws=ws, topics=[TOPIC], now_mono=1000.0, send_ack=_ok_ack
        )
        self.assertIsInstance(result, Applied)
        self.assertEqual(result.ack.removed, (TOPIC,))
        self.assertEqual({s.topic for s in result.ack.active}, {OTHER})
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=1000.0))

    async def test_unknown_topic_is_idempotent_and_not_reported(self):
        """제거는 **상태 델타**다 — 원래 없던 topic은 "제거됨"이 아니다."""
        registry, ws = await self._subscribed()
        result = await registry.apply_unsubscribe(
            ws=ws, topics=["never:subscribed"], now_mono=1000.0, send_ack=_ok_ack
        )
        self.assertEqual(result.ack.removed, ())
        self.assertEqual({s.topic for s in result.ack.active}, {TOPIC, OTHER})

    async def test_unmentioned_topics_are_untouched(self):
        registry, ws = await self._subscribed()
        await registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0,
                                         send_ack=_ok_ack)
        self.assertIsNotNone(registry.authorized_lease(ws, OTHER, now_mono=1000.0))

    async def test_removal_is_committed_before_the_ack(self):
        """⛔ ack을 기다리는 동안 사용자가 끄라고 한 데이터가 계속 흐르면 안 된다."""
        registry, ws = await self._subscribed()
        observed = {}

        async def probing(ack):
            observed["still_authorized"] = registry.authorized_lease(ws, TOPIC, now_mono=1000.0)
            return True

        await registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0,
                                         send_ack=probing)
        self.assertIsNone(observed["still_authorized"], "ack 시점에 아직 인가되고 있었다")

    async def test_removal_survives_an_ack_failure(self):
        """⛔ fail-open — ack이 실패해도 축소는 유지된다(되돌리면 끈 데이터가 다시 흐른다)."""
        registry, ws = await self._subscribed()

        async def failing(ack):
            raise ConnectionResetError()

        result = await registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0,
                                                  send_ack=failing)
        self.assertIsInstance(result, ConnectionTerminated)
        # ⚠️ `authorized_lease`는 tombstone에서 fail-closed라 되돌렸든 아니든 None이다 —
        #    관측 도구가 못 된다(이 세션에서 같은 가림에 두 번 당했다). 상태를 직접 본다.
        self.assertNotIn(TOPIC, registry.topics_for_test(ws), "ack 실패에 제거를 되돌렸다")
        self.assertIn(OTHER, registry.topics_for_test(ws), "무관한 topic까지 지웠다")

    async def test_removal_works_on_a_terminated_connection(self):
        """⛔ tombstone에서 제거를 거부하면, D8이 제거를 보장해야 하는 바로 그 실패 경로에서 막힌다."""
        registry, ws = await self._subscribed()

        async def failing(ack):
            raise ConnectionResetError()

        await _apply(registry, StrictObservationCache(), ws, ["usdt:krw"], send_ack=failing)
        self.assertIsInstance(
            await registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0,
                                             send_ack=_ok_ack),
            ConnectionTerminated,
        )
        self.assertEqual(registry.claimed_topics(ws), ())
        self.assertNotIn(TOPIC, registry.topics_for_test(ws))

    async def test_unconfirmed_ack_terminates_but_keeps_the_removal(self):
        """§B4와 같은 확인 계약 + §D8의 fail-open이 **동시에** 성립해야 한다."""
        registry, ws = await self._subscribed()

        async def silent(ack):
            return None

        result = await registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0,
                                                  send_ack=silent)
        self.assertIsInstance(result, ConnectionTerminated)
        self.assertEqual(result.reason, "ack_not_confirmed")
        self.assertNotIn(TOPIC, registry.topics_for_test(ws))

    async def test_swallowed_cancellation_wins_over_a_later_exception(self):
        """⛔ 취소를 삼킨 뒤 **다른 예외**를 던져도 취소가 우선해야 한다.

        실측(구 구현): 결과=ConnectionTerminated / `cancelled()`=False / `cancelling()`=1 —
        예외가 결과값으로 변환되면서 teardown 신호가 사라졌다.
        """
        registry, ws = await self._subscribed()

        async def swallow_then_raise(ack):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise ConnectionResetError("소켓도 죽었다")

        task = asyncio.create_task(
            registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0,
                                       send_ack=swallow_then_raise)
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(TOPIC, registry.topics_for_test(ws), "fail-open 제거를 되돌렸다")

    async def test_swallowed_external_cancellation_is_not_success(self):
        """⛔ sender가 teardown 취소를 삼키고 `True`를 돌려주면 `Applied`로 정상 종료됐다.

        실측: 결과=Applied, `cancelled()`=False, `cancelling()`=1 — 즉 dispatcher의 teardown
        취소가 무력화돼 연결 정리가 누락될 수 있다. subscribe 경로에는 이 검사가 있었는데
        unsubscribe에는 없었다(같은 검사를 두 곳에 손으로 복제한 결과).
        ⚠️ 제거는 유지된다 — fail-open이라 되돌리지 않는다.
        """
        registry, ws = await self._subscribed()

        async def swallowing(ack):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return True
            return True

        task = asyncio.create_task(
            registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0,
                                       send_ack=swallowing)
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(TOPIC, registry.topics_for_test(ws), "fail-open 제거를 되돌렸다")

    async def test_ack_names_the_operation(self):
        """§8-B — subscribe/unsubscribe가 **같은 schema**를 쓰므로 구분자가 필요하다."""
        registry, ws = await self._subscribed()
        result = await registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0,
                                                  send_ack=_ok_ack)
        self.assertEqual(result.ack.operation, "unsubscribe")

    async def test_subscribe_ack_names_its_own_operation(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        result = await _apply(registry, cache, _WS(), [TOPIC])
        self.assertEqual(result.ack.operation, "subscribe")

    async def test_one_request_sends_exactly_one_ack(self):
        registry, ws = await self._subscribed()
        calls = []

        async def counting(ack):
            calls.append(ack)
            return True

        await registry.apply_unsubscribe(ws=ws, topics=[TOPIC, OTHER], now_mono=1000.0,
                                         send_ack=counting)
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(calls[0].removed), {TOPIC, OTHER})

    async def test_no_authentication_arguments_are_required(self):
        """⛔ 인증 인자를 받으면 배선이 그것을 검증에 쓰고 싶어진다 — D8은 그걸 금지한다."""
        import inspect

        from app.topic_lease_registry import TopicLeaseRegistry as R

        params = set(inspect.signature(R.apply_unsubscribe).parameters)
        self.assertEqual(params & {"uid", "snapshot", "cache", "premium_verified_at_mono",
                                   "identity_verified_at_mono"}, set())
        for name in ("ws", "topics", "now_mono", "send_ack"):
            self.assertIs(inspect.signature(R.apply_unsubscribe).parameters[name].default,
                          inspect.Parameter.empty, f"{name}에 기본값이 생겼다")


class TestMandatoryInputs(unittest.IsolatedAsyncioTestCase):
    """⛔ `send_ack`와 같은 논리 — 선택 인자로 두면 배선이 한 번 빠뜨렸을 때 안전장치가 꺼진다."""

    def test_safety_critical_arguments_have_no_defaults(self):
        """⛔ 기본값이 하나 생기면 그 안전장치가 **조용히** 꺼진다.

        실측: `authorized_lease`의 `now_mono`에 `= 0.0` 기본값을 넣어도 전 테스트가 통과했다 —
        모든 호출부가 이미 명시적으로 넘기고 있어서 행동으로는 드러나지 않는다. 그런데 기본값
        `0.0`은 양수 expiry에 대해 **항상 미만료**라 배선이 인자를 빠뜨리는 순간 fail-open이다.
        `send_ack`(ack 없이 활성화) · `rejected_topics`(§C2 무력화)도 같은 부류라 함께 잠근다.
        """
        import inspect

        from app.topic_lease_registry import TopicLeaseRegistry as R

        required = {
            R.apply_subscribe: (
                "ws", "uid", "topics", "rejected_topics", "snapshot", "cache",
                "now_mono", "premium_verified_at_mono", "identity_verified_at_mono", "send_ack",
            ),
            R.authorized_lease: ("ws", "topic", "now_mono"),
            R.authorizes_send: ("ws", "topic", "uid", "lease_id", "now_mono"),
        }
        for func, names in required.items():
            signature = inspect.signature(func)
            for name in names:
                self.assertIs(
                    signature.parameters[name].default, inspect.Parameter.empty,
                    f"{func.__name__}({name}=)에 기본값이 생겼다 — 배선이 빠뜨리면 조용히 통과한다",
                )

    async def test_rejected_topics_is_mandatory(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        with self.assertRaises(TypeError):
            await registry.apply_subscribe(
                ws=_WS(), uid=UID, topics=[TOPIC], snapshot=cache.snapshot(UID), cache=cache,
                now_mono=1000.0, premium_verified_at_mono=1000.0,
                identity_verified_at_mono=1000.0, send_ack=_ok_ack,
            )


class TestRegistryConstantsArePinned(unittest.TestCase):
    """`assertLess(ACK, CLIENT_ACK)` 관계 단언만으로는 **둘이 같이 움직이면** 통과한다.

    실측(2026-07-29): `ACK 5.0→1.0`, `CLIENT_ACK 10.0→3600.0` 둘 다 전체 스위트 생존.
    """

    def test_ack_timeout_is_pinned(self):
        """D-const 표 `ACK_TIMEOUT_SECONDS` 행 · 근거 칸 `8s + 5s = 13s` 산술의 5가 이 값이다.

        ⛔ 잠그는 것은 **코드 기본값뿐**이다 — 표 쪽을 고쳐도 red는 나지 않는다(doc→code 비구속).
        """
        self.assertEqual(ACK_TIMEOUT_SECONDS, 5.0)

    def test_client_ack_timeout_is_pinned(self):
        """⚠️ **이 값의 소유자는 iOS repo다.**

        red의 뜻은 **서버 코드의 기록값이 10.0에서 벗어났다**는 것뿐이다. 클라가 어긋났다는 뜻이
        **아니고**, 반대로 §D6 쪽이 바뀌면 red는 **안 난다**(doc→code 비구속, 실측 확인).
        서버는 이 값을 강제하지 않는다 — `assertLess(ACK, CLIENT_ACK)`의 입력일 뿐이다.

        ⛔ 클라 값과 묶는 통합 테스트는 **지금 지을 수 없다**(실측 2026-07-29): iOS
        `WebSocketService`의 subscribe는 fire-and-forget이고 수신 switch에 `ack` case가 없어
        ack이 와도 폐기된다. Android는 topic subscribe 송신 경로 자체가 없다. 서버
        `topic_dispatcher`도 ack을 보내지 않는다(이 모듈은 아직 미배선 harness). 지금 통합
        테스트를 짜면 **양쪽 다 아무것도 안 해서 통과**하는 vacuous green이다.

        묶기 위한 선행조건: ① dispatcher ack 배선 → ② 클라 request_id + 대기 테이블 + 타이머 +
        1회 재시도(§D4) → ③ 클라 connectionGeneration(구 소켓 ack 폐기, §B5(e)). ③은 테스트
        편의가 아니라 정확성 전제다 — 없으면 재연결 시 구 ack이 신 구독을 오염시킨다.
        """
        self.assertEqual(CLIENT_ACK_TIMEOUT_SECONDS, 10.0)

class TestTombstoneIsReadThroughOneHelper(unittest.TestCase):
    """`_closed` **읽기**는 `_tombstoned` 하나를 거친다.

    ⚠️ 쓰기(`_closed.add`)는 대상이 아니다 — 종단 판정은 여러 경로가 내리는 게 맞다.
    읽기가 흩어지면 무토큰 세계 분기가 4번째 인라인 판정을 만들고, 그때부터 두 곳이 갈릴 수 있다
    (`_claimed`/`_is_claimed`가 같은 이유로 이미 좁혀져 있고 trip-wire까지 있다).
    """

    def test_closed_membership_is_read_only_through_the_helper(self):
        import ast
        src = pathlib.Path(_REGISTRY_SRC).read_text(encoding="utf-8")
        tree = ast.parse(src)
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "TopicLeaseRegistry")
        offenders = []
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name == "_tombstoned":
                continue
            for node in ast.walk(fn):
                # `x in self._closed` / `x not in self._closed`
                if isinstance(node, ast.Compare) and any(
                    isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
                ):
                    for cmp_ in node.comparators:
                        if (isinstance(cmp_, ast.Attribute) and cmp_.attr == "_closed"
                                and isinstance(cmp_.value, ast.Name) and cmp_.value.id == "self"):
                            offenders.append(fn.name)
        self.assertEqual(offenders, [], f"`_closed` 인라인 읽기가 남았다: {offenders}")

    def test_the_helper_exists_and_is_the_detector_target(self):
        """⛔ 탐지기 자기검사 — `_tombstoned`가 사라지면 위 테스트가 **공허하게** 통과한다."""
        self.assertTrue(hasattr(TopicLeaseRegistry, "_tombstoned"))
        import ast
        src = pathlib.Path(_REGISTRY_SRC).read_text(encoding="utf-8")
        cls = next(n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.ClassDef) and n.name == "TopicLeaseRegistry")
        helper = next(f for f in cls.body
                      if isinstance(f, ast.FunctionDef) and f.name == "_tombstoned")
        reads = [n for n in ast.walk(helper)
                 if isinstance(n, ast.Attribute) and n.attr == "_closed"]
        self.assertTrue(reads, "helper가 정작 `_closed`를 읽지 않는다 — 탐지기가 엉뚱한 것을 잠그고 있다")


class TestObservationCounterIsAsymmetric(unittest.IsolatedAsyncioTestCase):
    """§B3 — 세는 축 둘이 **다른 질문**에 답한다. 시그니처가 그 비대칭을 진술한다.

    legacy `subscribed_connection_count`의 오용은 "틀린 값"이 아니라 **"틀린 질문"**이었다
    (단말이 `fx:usd-krw`만 구독해도 1이라 `usdt:krw` publisher가 오판). 그래서 새 API는
    **topic도 시각도 받지 않는다** — per-topic guard로 쓰는 것 자체가 표현 불가능하다.
    """

    async def test_connection_with_no_remaining_topics_is_not_counted(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])

        async def ack(_):
            return True

        await registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0, send_ack=ack)
        self.assertEqual(registry.connections_with_subscriptions(), 0)
        # ⚠️ 같은 상태에서 `connection_count`는 1이다 — UID 바인딩은 연결 수명 동안 유지된다.
        self.assertEqual(registry.connection_count(), 1, "UID 바인딩이 사라졌다")

    async def test_expired_only_connection_is_still_counted(self):
        """⛔ 관찰 축은 만료를 반영하지 **않는다**. 인가 축과 한 테스트에 두어 비대칭을 잠근다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        far = 1000.0 + LEASE_MAX_SECONDS + 1
        self.assertEqual(registry.connections_with_subscriptions(), 1, "관찰 축이 만료를 반영했다")
        self.assertIsNone(registry.authorized_lease(ws, TOPIC, now_mono=far), "인가 축이 만료를 놓쳤다")

    def test_observation_counter_takes_no_topic_and_no_clock(self):
        """시그니처가 오용을 막는다 — 인자가 늘면 red."""
        import inspect
        params = list(inspect.signature(TopicLeaseRegistry.connections_with_subscriptions).parameters)
        self.assertEqual(params, ["self"])

    async def test_unsubscribe_on_unknown_connection_creates_no_entry(self):
        """⛔ 구독한 적 없는 연결에 빈 항목을 심으면 그 자체가 상태 누수다.

        실측(수정 전): `apply_unsubscribe` → `Applied` 반환 + `_active[ws] = {}` 생성 →
        `connections_snapshot()`에 등장 → **sweep이 매 주기 방문**한다.
        """
        registry = TopicLeaseRegistry()
        ws = _WS()

        async def ack(_):
            return True

        result = await registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0,
                                                  send_ack=ack)
        self.assertIsInstance(result, Applied, "§D8 idempotency — 없던 구독 해제는 조용히 성공한다")
        self.assertNotIn(ws, registry.connections_snapshot(), "빈 항목이 생겼다")
        self.assertEqual(registry.connections_with_subscriptions(), 0)


class TestSubscriberQueryBoundary(unittest.IsolatedAsyncioTestCase):
    """§B2 조회 경계 + §C-API 2 — topic으로 묻고, 생존 규칙은 `authorized_lease`가 **정의**한다.

    ⛔ 필터를 여기서 **재구현하지 않는다**(상속이 아니라 동일 함수). 재구현하면 두 답이 갈리고,
    이 파일의 claim trip-wire가 이미 그 형태를 경고하고 있다.
    """

    async def _one(self, registry, cache, ws, topics=(TOPIC,)):
        return await _apply(registry, cache, ws, list(topics))

    async def test_subscribers_excludes_expired_without_sweep(self):
        """sweep이 안 돌아도 만료는 빠진다 — 인가는 sweep의 부지런함에 의존하지 않는다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await self._one(registry, cache, ws)
        far = 1000.0 + LEASE_MAX_SECONDS + 1
        self.assertEqual(registry.subscribers(TOPIC, now_mono=1000.0)[0].ws, ws)
        self.assertEqual(registry.subscribers(TOPIC, now_mono=far), ())

    async def test_subscribers_excludes_claimed_before_removal(self):
        """§C3 — claim된 lease는 **제거 전이라도** 빠진다(통지 중 live publish 통과 금지)."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await self._one(registry, cache, ws)
        far = 1000.0 + LEASE_MAX_SECONDS + 1
        async with registry.hold_connection_lock(ws):
            registry.claim_expired_locked(ws, now_mono=far)
        self.assertEqual(registry.subscribers(TOPIC, now_mono=1000.0), (),
                         "claim된 lease가 조회를 통과했다")

    async def test_subscribers_excludes_tombstoned_connection(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await self._one(registry, cache, ws)
        await registry.remove_websocket(ws)
        self.assertEqual(registry.subscribers(TOPIC, now_mono=1000.0), ())

    async def test_subscribers_excludes_pending_leases(self):
        """§B4 순서의 **관측 가능성** — ack 대기 중 lease는 fanout 대상이 아니다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        seen = []

        async def ack(_):
            seen.append(registry.subscribers(TOPIC, now_mono=1000.0))
            return True

        await _apply(registry, cache, ws, [TOPIC], send_ack=ack)
        self.assertEqual(seen, [()], "ack 이전에 이미 live 대상이었다")
        self.assertEqual(len(registry.subscribers(TOPIC, now_mono=1000.0)), 1)

    async def test_subscribers_returns_a_materialized_snapshot(self):
        """⛔ generator면 소비 중 다른 연결의 커밋이 순회를 깬다 — tuple이 계약이다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        first = _WS("a")
        await self._one(registry, cache, first)
        result = registry.subscribers(TOPIC, now_mono=1000.0)
        self.assertIsInstance(result, tuple)
        await _apply(registry, StrictObservationCache(), _WS("b"), [TOPIC], uid="uid-2")
        self.assertEqual([g.ws for g in result], [first], "스냅샷이 나중 커밋에 오염됐다")

    async def test_subscriber_count_equals_len_of_subscribers(self):
        """§B3 **상속하는 쪽** — 상속을 약속이 아니라 호출 관계로 만든다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        for i, name in enumerate("abc"):
            await _apply(registry, StrictObservationCache(), _WS(name), [TOPIC], uid=f"uid-{i}")
        for now in (1000.0, 1000.0 + LEASE_MAX_SECONDS + 1):
            self.assertEqual(registry.subscriber_count(TOPIC, now_mono=now),
                             len(registry.subscribers(TOPIC, now_mono=now)))

    def test_query_apis_require_an_explicit_clock(self):
        """시각을 기본값 있는 인자로 만들면 '만료 미확인 답'을 얻을 수 있게 된다."""
        import inspect
        for name in ("subscribers", "subscriber_count", "grant_for"):
            sig = inspect.signature(getattr(TopicLeaseRegistry, name))
            param = sig.parameters["now_mono"]
            self.assertIs(param.default, inspect.Parameter.empty, f"{name}의 now_mono에 기본값이 있다")
            self.assertIs(param.kind, inspect.Parameter.KEYWORD_ONLY, f"{name}의 now_mono가 위치 인자다")


class TestGrantIsTheSendTimeIdentity(unittest.IsolatedAsyncioTestCase):
    """§B1 — grant가 `(ws, topic, uid, lease_id)`를 **조회와 같은 스냅샷에서** 나른다.

    재조회로 identity를 다시 캡처하면 교체된 lease를 잡아 재검증이 tautology가 된다(실측 결함).
    """

    async def test_grant_pairs_uid_and_lease_id(self):
        with self.assertRaises(ValueError):
            SubscriberGrant(ws=_WS(), topic=TOPIC, uid=UID, lease_id=None)
        with self.assertRaises(ValueError):
            SubscriberGrant(ws=_WS(), topic=TOPIC, uid=None, lease_id="x")

    async def test_grant_for_returns_none_when_not_authorized(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        self.assertIsNone(registry.grant_for(ws, TOPIC, now_mono=1000.0))
        await _apply(registry, cache, ws, [TOPIC])
        self.assertIsNotNone(registry.grant_for(ws, TOPIC, now_mono=1000.0))
        far = 1000.0 + LEASE_MAX_SECONDS + 1
        self.assertIsNone(registry.grant_for(ws, TOPIC, now_mono=far), "만료를 놓쳤다")

    async def test_authorizes_grant_false_after_lease_replacement(self):
        """같은 UID 재인증으로 L1→L2 교체 시 구 grant는 죽는다(`lease_id` 축이 load-bearing)."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        old = registry.grant_for(ws, TOPIC, now_mono=1000.0)
        await _apply(registry, cache, ws, [TOPIC])
        self.assertFalse(registry.authorizes_grant(old, now_mono=1000.0))
        fresh = registry.grant_for(ws, TOPIC, now_mono=1000.0)
        self.assertTrue(registry.authorizes_grant(fresh, now_mono=1000.0))

    async def test_authorizes_grant_false_after_tombstone(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        grant = registry.grant_for(ws, TOPIC, now_mono=1000.0)
        await registry.remove_websocket(ws)
        self.assertFalse(registry.authorizes_grant(grant, now_mono=1000.0))

    def test_authorizes_grant_delegates_instead_of_reimplementing(self):
        """AST — 생존 규칙을 다시 쓰면 §B2 필터와 두 곳이 갈린다."""
        import ast
        src = pathlib.Path(_REGISTRY_SRC).read_text(encoding="utf-8")
        cls = next(n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.ClassDef) and n.name == "TopicLeaseRegistry")
        fn = next(f for f in cls.body
                  if isinstance(f, ast.FunctionDef) and f.name == "authorizes_grant")
        names = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        self.assertNotIn("_active", names)
        self.assertNotIn("_closed", names)
        self.assertIn("authorizes_send", names, "위임하지 않는다 — 규칙을 재구현했다")


PERMISSIVE = frozenset({TOPIC_FX})


def _permissive():
    return TopicLeaseRegistry(unleased_registration=PERMISSIVE)


class TestUnleasedModeIsProcessFixed(unittest.TestCase):
    """§C-API 9 / §E1 — 모드는 생성자에서 1회 확정되고 setter가 없다.

    flip = 새 프로세스(config는 import-time getenv 1회 + env 변경에 `--force-recreate`)이므로
    무토큰 등록은 flip 시점에 **전부 소멸**한다 → 우회로가 flip을 건너 살아남을 수 없다.
    """

    def test_strict_is_the_default(self):
        self.assertFalse(TopicLeaseRegistry().unleased_registration_enabled())
        self.assertTrue(_permissive().unleased_registration_enabled())

    def test_empty_allowlist_is_rejected_at_construction(self):
        """켰는데 아무것도 등록 못 하는 상태는 strict와 구분되지 않아 배선 오류를 숨긴다."""
        with self.assertRaises(ValueError):
            TopicLeaseRegistry(unleased_registration=frozenset())

    def test_per_user_gated_topic_cannot_enter_the_allowlist(self):
        """⛔ 이게 없으면 생성자 호출자의 선의가 유일한 방어다 = entitlement 우회."""
        from app.topic_initial_snapshot import per_user_gated_snapshot_topics

        gated = next(iter(per_user_gated_snapshot_topics()))
        with self.assertRaises(ValueError):
            TopicLeaseRegistry(unleased_registration=frozenset({gated}))

    def test_allowlist_excludes_every_per_user_filtered_topic(self):
        """**trip-wire** — 실제로 per-user 필터에 걸리는 topic은 무토큰 allowlist에 없어야 한다.

        ⛔ 구 버전은 `unleased_registration_topics() & per_user_gated_snapshot_topics() == ∅`를
        단언했는데 **공허했다**: allowlist가 `supported − per_user_gated`이므로 `(A−B)&B = ∅`은
        집합 항등식이라 `per_user_gated`의 내용과 **무관하게** 항상 통과한다.

        그래서 걸러지는 집합을 **behavior**(`visible_snapshot_topics_sync`)에서 유도한다 —
        allowlist와 **다른 출처**라 실제로 물 수 있다. 게이팅 topic이 `per_user_gated`에 등록되지
        않으면 allowlist에 남고, 그러면 여기서 red가 된다.

        ⚠️ 이 검사도 게이팅이 `visible_snapshot_topics_sync` 안에서 일어날 때만 본다 — 다른 축에
        게이트를 만들면 놓친다(helper docstring의 같은 단서 참조).
        """
        from unittest.mock import MagicMock

        from app import config
        from app.topic_initial_snapshot import (
            supported_snapshot_topics,
            unleased_registration_topics,
            visible_snapshot_topics_sync,
        )

        with patch.object(config, "KRX_CLIENT_DISTRIBUTION_EFFECTIVE", True), \
             patch("app.database.SessionLocal", return_value=MagicMock()), \
             patch("app.entitlements.compute_krx_visible", return_value=False):
            supported = set(supported_snapshot_topics())
            visible = set(visible_snapshot_topics_sync("u1", premium_active=True))
            allowlist = unleased_registration_topics()
        filtered = supported - visible
        self.assertTrue(filtered, "필터가 아무것도 안 걸러내면 이 검사가 vacuous해진다")
        self.assertEqual(allowlist & filtered, frozenset(),
                         f"per-user 필터 대상이 무토큰 allowlist에 있다: {sorted(allowlist & filtered)}")

    def test_mode_is_assigned_only_in_init(self):
        """AST — setter가 생기면 red(살아 있는 registry의 모드 전환은 표현 불가능해야 한다)."""
        import ast
        src = pathlib.Path(_REGISTRY_SRC).read_text(encoding="utf-8")
        cls = next(n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.ClassDef) and n.name == "TopicLeaseRegistry")
        writers = set()
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Attribute) and tgt.attr == "_unleased_allowlist":
                            writers.add(fn.name)
        self.assertEqual(writers, {"__init__"}, f"모드 대입 지점: {sorted(writers)}")


class TestUnleasedWorldIsExclusive(unittest.IsolatedAsyncioTestCase):
    """한 소켓은 leased **또는** unauthenticated 하나다. 섞이면 시끄럽게 실패한다."""

    async def test_strict_registry_raises_loudly_on_unleased_registration(self):
        with self.assertRaises(UnleasedRegistrationDisabled):
            await TopicLeaseRegistry().apply_unleased_subscribe(ws=_WS(), topics=[TOPIC_FX])

    async def test_leased_connection_cannot_register_unleased(self):
        registry, cache = _permissive(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])
        with self.assertRaises(UnleasedRoutingError):
            await registry.apply_unleased_subscribe(ws=ws, topics=[TOPIC_FX])

    async def test_leased_predicate_survives_full_unsubscribe(self):
        """⛔ `_active`의 비어 있음만 보면 틀린다 — UID 바인딩은 연결 수명 동안 남는다."""
        registry, cache = _permissive(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])

        async def ack(_):
            return True

        await registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0, send_ack=ack)
        self.assertEqual(registry.topics_for_test(ws), ())
        self.assertEqual(registry.registration_kind(ws), "leased", "바인딩된 연결이 none이 됐다")
        with self.assertRaises(UnleasedRoutingError):
            await registry.apply_unleased_subscribe(ws=ws, topics=[TOPIC_FX])

    async def test_unleased_connection_cannot_use_leased_unsubscribe(self):
        """현행 결함 재현 방지: 조용한 no-op은 **거짓 ack + 데이터 계속**이다."""
        registry = _permissive()
        ws = _WS()
        await registry.apply_unleased_subscribe(ws=ws, topics=[TOPIC_FX])

        async def ack(_):
            return True

        with self.assertRaises(UnleasedRoutingError):
            await registry.apply_unsubscribe(ws=ws, topics=[TOPIC_FX], now_mono=1000.0,
                                             send_ack=ack)

    async def test_registration_kind_is_three_state(self):
        registry = _permissive()
        self.assertEqual(registry.registration_kind(_WS()), "none")
        unleased = _WS("u")
        await registry.apply_unleased_subscribe(ws=unleased, topics=[TOPIC_FX])
        self.assertEqual(registry.registration_kind(unleased), "unauthenticated")
        leased = _WS("l")
        await _apply(registry, StrictObservationCache(), leased, [TOPIC])
        self.assertEqual(registry.registration_kind(leased), "leased")


class TestUnleasedRegistrationContract(unittest.IsolatedAsyncioTestCase):
    """등록 자체의 계약 — allowlist 원자 거부 · D7 상한 · 무기한 · tombstone."""

    async def test_topic_outside_allowlist_registers_nothing(self):
        """부분 커밋 금지 — 이 세계엔 무엇이 등록됐는지 알려줄 ack이 없다."""
        registry = _permissive()
        ws = _WS()
        result = await registry.apply_unleased_subscribe(
            ws=ws, topics=[TOPIC_FX, "usdt:krw"])
        self.assertIsInstance(result, Rejected)
        self.assertEqual(result.reason, "authentication_required")
        self.assertEqual(registry.unleased_topics_for_test(ws), (), "부분 커밋됐다")

    async def test_d7_caps_are_enforced_cumulatively(self):
        registry = TopicLeaseRegistry(
            unleased_registration=unleased_registration_topics())
        ws = _WS()
        result = await registry.apply_unleased_subscribe(ws=ws, topics=["x" * 65])
        self.assertIsInstance(result, Rejected)
        self.assertEqual(result.reason, "request_too_large")
        with patch("app.topic_lease_registry.MAX_UNLEASED_TOPICS", 2):
            await registry.apply_unleased_subscribe(ws=ws, topics=["fx:usd-krw", "fx:jpy-krw"])
            over = await registry.apply_unleased_subscribe(ws=ws, topics=["fx:eur-krw"])
        self.assertIsInstance(over, Rejected, "누적 상한이 강제되지 않았다")
        self.assertEqual(registry.unleased_topics_for_test(ws), ("fx:jpy-krw", "fx:usd-krw"))

    async def test_unleased_entry_never_expires(self):
        """§E1 '전원 무기한' — 만료 개념 자체가 없다."""
        registry = _permissive()
        ws = _WS()
        await registry.apply_unleased_subscribe(ws=ws, topics=[TOPIC_FX])
        for now in (1000.0, 1000.0 + LEASE_MAX_SECONDS * 100):
            self.assertEqual(len(registry.subscribers(TOPIC_FX, now_mono=now)), 1)

    async def test_tombstoned_socket_is_excluded_everywhere(self):
        """죽었다고 판정한 소켓으로 데이터가 계속 나가면 그게 곧 우회다."""
        registry = _permissive()
        ws = _WS()
        await registry.apply_unleased_subscribe(ws=ws, topics=[TOPIC_FX])
        grant = registry.grant_for(ws, TOPIC_FX, now_mono=1000.0)
        await registry.remove_websocket(ws)
        self.assertEqual(registry.subscribers(TOPIC_FX, now_mono=1000.0), ())
        self.assertFalse(registry.authorizes_grant(grant, now_mono=1000.0))
        self.assertEqual(registry.unleased_topics_for_test(ws), (), "teardown이 정리하지 않았다")

    async def test_grant_dies_after_unleased_unsubscribe(self):
        registry = _permissive()
        ws = _WS()
        await registry.apply_unleased_subscribe(ws=ws, topics=[TOPIC_FX])
        grant = registry.grant_for(ws, TOPIC_FX, now_mono=1000.0)
        self.assertTrue(registry.authorizes_grant(grant, now_mono=1000.0))
        await registry.apply_unleased_unsubscribe(ws=ws, topics=[TOPIC_FX])
        self.assertFalse(registry.authorizes_grant(grant, now_mono=1000.0),
                         "끈 데이터가 계속 흐른다")

    async def test_unleased_unsubscribe_takes_no_sender(self):
        """이 세계엔 ack 프로토콜이 없다 — 시그니처가 그것을 진술한다."""
        import inspect
        params = set(inspect.signature(
            TopicLeaseRegistry.apply_unleased_unsubscribe).parameters)
        self.assertNotIn("send_ack", params)
        self.assertNotIn("now_mono", params)

    async def test_strict_registry_rejects_a_foreign_unleased_grant(self):
        """무토큰 세계를 모르는 registry가 그런 grant를 받으면 fail-closed다."""
        foreign = SubscriberGrant(ws=_WS(), topic=TOPIC_FX, uid=None, lease_id=None)
        self.assertFalse(TopicLeaseRegistry().authorizes_grant(foreign, now_mono=1000.0))

    async def test_observation_counter_sums_both_worlds(self):
        registry = _permissive()
        # ⚠️ ws를 **변수에 담아 살려 둔다** — `_active`/`_unleased`가 둘 다 weak key라
        #    인자로만 넘기면 await 뒤 GC가 회수해 0이 나온다(실측: 이 테스트가 그렇게 처음 red).
        unleased, leased = _WS("u"), _WS("l")
        await registry.apply_unleased_subscribe(ws=unleased, topics=[TOPIC_FX])
        await _apply(registry, StrictObservationCache(), leased, [TOPIC])
        self.assertEqual(registry.connections_with_subscriptions(), 2)

    async def test_weak_keys_release_both_worlds(self):
        """위 테스트가 처음 red였던 이유를 **계약으로** 잠근다 — 연결이 사라지면 자동 회수된다."""
        import gc

        registry = _permissive()
        ws = _WS("gone")
        await registry.apply_unleased_subscribe(ws=ws, topics=[TOPIC_FX])
        self.assertEqual(registry.connections_with_subscriptions(), 1)
        del ws
        gc.collect()
        self.assertEqual(registry.connections_with_subscriptions(), 0,
                         "무토큰 저장소가 강한 참조를 쥐고 있다")


class TestWorldTransitionIsOneWay(unittest.IsolatedAsyncioTestCase):
    """§E1 sticky 모드는 **양방향 대칭이 아니다** — 문서가 한때 절대문으로 적었던 것을 잠근다.

    ⚠️ 이 클래스가 존재하는 이유: "전환하려면 재연결한다"고 확정문으로 적었는데 실측에서
    `unauthenticated → leased`가 재연결 없이 성립했다. 방향이 무인증→검증완료라 권한 상승은
    아니지만, 배선이 "세계는 연결 수명 동안 고정"을 전제로 캐시하면 그 전제가 깨진다.
    """

    async def test_leased_to_unauthenticated_is_permanently_blocked(self):
        """UID 바인딩이 연결 수명 동안 남아 **영구 차단**된다(전 topic을 해제해도)."""
        registry, cache = _permissive(), StrictObservationCache()
        ws = _WS()
        await _apply(registry, cache, ws, [TOPIC])

        async def ack(_):
            return True

        await registry.apply_unsubscribe(ws=ws, topics=[TOPIC], now_mono=1000.0, send_ack=ack)
        with self.assertRaises(UnleasedRoutingError):
            await registry.apply_unleased_subscribe(ws=ws, topics=[TOPIC_FX])

    async def test_unauthenticated_to_leased_is_possible_once_empty(self):
        """⛔ 등록이 0이 되면 `none`이 되고 그때는 leased 진입이 **가능하다**(재연결 불요)."""
        registry, cache = _permissive(), StrictObservationCache()
        ws = _WS()
        await registry.apply_unleased_subscribe(ws=ws, topics=[TOPIC_FX])
        self.assertEqual(registry.registration_kind(ws), "unauthenticated")
        await registry.apply_unleased_unsubscribe(ws=ws, topics=[TOPIC_FX])
        self.assertEqual(registry.registration_kind(ws), "none")
        self.assertIsInstance(await _apply(registry, cache, ws, [TOPIC]), Applied)
        self.assertEqual(registry.registration_kind(ws), "leased")
        self.assertEqual(registry.topics_for_test(ws), (TOPIC,),
                         "무토큰 멤버십이 leased 세계로 승계됐다")

    async def test_empty_unleased_request_creates_no_entry(self):
        """U1과 대칭 — 빈 요청이 빈 항목을 심으면 매 fanout 순회에 낀다."""
        registry = _permissive()
        ws = _WS()
        result = await registry.apply_unleased_subscribe(ws=ws, topics=[])
        self.assertIsInstance(result, UnleasedApplied)
        self.assertEqual(registry.registration_kind(ws), "none")
        self.assertEqual(registry.unleased_topics_for_test(ws), ())


class TestUnleasedAuthorizationIsNotAlwaysFalse(unittest.IsolatedAsyncioTestCase):
    """§B1 진입점의 무토큰 분기가 **두 갈래**임을 잠근다.

    ⚠️ docstring이 한때 "무토큰 grant는 여기서 False다"라고 적어 두고 같은 커밋에서
    permissive 분기를 갖고 있었다. 배선이 그 문장을 읽고 자기 쪽 판정을 하나 더 만들면
    이 함수가 위임으로 없애려던 이중 판정이 부활한다.
    """

    async def test_permissive_authorizes_a_live_unleased_grant(self):
        registry = _permissive()
        ws = _WS()
        await registry.apply_unleased_subscribe(ws=ws, topics=[TOPIC_FX])
        grant = registry.grant_for(ws, TOPIC_FX, now_mono=1000.0)
        self.assertTrue(registry.authorizes_grant(grant, now_mono=1000.0),
                        "permissive에서 무토큰 grant가 인가되지 않았다 — §E1 중간 상태가 성립하지 않는다")

    async def test_strict_refuses_the_same_shape(self):
        same = SubscriberGrant(ws=_WS(), topic=TOPIC_FX, uid=None, lease_id=None)
        self.assertFalse(TopicLeaseRegistry().authorizes_grant(same, now_mono=1000.0))


if __name__ == "__main__":
    unittest.main()
