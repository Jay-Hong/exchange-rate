"""topic lease registry — §8.1 B4 / C1 / C3.

운영 dispatcher를 건드리지 않는 **별도 모듈**로 먼저 만든다. 상태기계와 B4 경쟁을 여기서
test-first로 잠근 뒤에 최소 배선해야 원인 분리가 된다.

## 잠그는 계약 6개

1. `(ws, topic, uid, lease_id)` identity + inactive/active 상태
2. 연결별 `asyncio.Lock`
3. `등록(비활성) → is_current(snapshot) → 활성화`
4. **같은 lock 아래** UID binding · lease CAS · ack 자료 확정
5. B4 인터리빙 harness
6. 요청당 **단일 `now_mono`** + 3-way lease 계산
"""
import asyncio
import unittest

from app.strict_cache import StrictObservationCache
from app.topic_lease import LEASE_MAX_SECONDS
from app.topic_lease_registry import (
    Discarded,
    Issued,
    Rejected,
    TopicLeaseRegistry,
)

UID = "uid-1"
TOPIC = "krx:usd-krw-futures"


class _WS:
    """WebSocket 자리표시자 — registry는 신원만 쓰고 I/O를 하지 않는다."""

    def __init__(self, name="ws"):
        self.name = name

    def __repr__(self):
        return f"<WS {self.name}>"


def _fresh(cache=None, uid=UID):
    cache = cache or StrictObservationCache()
    return cache, cache.snapshot(uid)


class TestIdentityAndStates(unittest.IsolatedAsyncioTestCase):
    """계약 1 — `(ws, topic, uid, lease_id)` identity와 inactive/active 상태."""

    async def test_issued_lease_carries_the_full_identity(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        _, snapshot = _fresh(cache)
        result = await registry.issue(
            ws=ws, topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache,
            now_mono=1000.0, premium_verified_at_mono=1000.0, identity_verified_at_mono=1000.0,
        )
        self.assertIsInstance(result, Issued)
        lease = result.lease
        self.assertIs(result.ws, ws)
        self.assertEqual((lease.topic, lease.uid, lease.epoch), (TOPIC, UID, snapshot.epoch))
        self.assertTrue(lease.lease_id)

    async def test_lease_ids_are_unique_per_issue(self):
        """⛔ 같은 id를 재사용하면 C3의 `(ws, topic, lease_id)` CAS가 무의미해진다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        ids = set()
        for _ in range(3):
            _, snapshot = _fresh(cache)
            result = await registry.issue(
                ws=ws, topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache, now_mono=1000.0,
                premium_verified_at_mono=1000.0, identity_verified_at_mono=1000.0,
            )
            ids.add(result.lease.lease_id)
        self.assertEqual(len(ids), 3)

    async def test_only_active_leases_are_visible(self):
        """inactive는 **존재하되 보이지 않는다** — 활성화 전에는 전송 대상이 아니다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        self.assertIsNone(registry.active_lease(ws, TOPIC))
        _, snapshot = _fresh(cache)
        await registry.issue(ws=ws, topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache,
                             now_mono=1000.0, premium_verified_at_mono=1000.0,
                             identity_verified_at_mono=1000.0)
        self.assertIsNotNone(registry.active_lease(ws, TOPIC))


class TestFenceOrdering(unittest.IsolatedAsyncioTestCase):
    """계약 3 — `등록(비활성) → is_current(snapshot) → 활성화`."""

    async def test_invalidation_before_activation_discards_the_lease(self):
        """⛔ webhook이 검증 후·활성화 전에 들어오면 lease를 **발급하지 않는다**."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        _, snapshot = _fresh(cache)
        cache.bump(UID)                      # 재확인 직전에 무효화

        result = await registry.issue(
            ws=ws, topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache, now_mono=1000.0,
            premium_verified_at_mono=1000.0, identity_verified_at_mono=1000.0,
        )
        self.assertIsInstance(result, Discarded)
        self.assertIsNone(registry.active_lease(ws, TOPIC), "폐기했는데 흔적이 남았다")

    async def test_registration_happens_before_the_recheck(self):
        """⛔ 순서가 뒤집히면(재확인 → 등록) 그 사이 무효화를 못 본다.

        `is_current`가 불릴 때 **이미 등록되어 있어야** 한다. 등록 여부를 재확인 시점에 관찰한다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        _, snapshot = _fresh(cache)
        observed = {}

        real_is_current = cache.is_current

        def spy(snap):
            observed["registered_at_recheck"] = registry.has_pending(ws, TOPIC)
            # ⛔ 그 순간 **활성으로 보이면 안 된다** — 미활성은 존재하되 전송 대상이 아니다.
            observed["visible_at_recheck"] = registry.active_lease(ws, TOPIC)
            return real_is_current(snap)

        cache.is_current = spy
        await registry.issue(ws=ws, topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache,
                             now_mono=1000.0, premium_verified_at_mono=1000.0,
                             identity_verified_at_mono=1000.0)
        self.assertTrue(observed.get("registered_at_recheck"), "재확인 시점에 등록돼 있지 않았다")
        self.assertIsNone(observed.get("visible_at_recheck"),
                          "활성화 전인데 전송 대상으로 보였다")


class TestUidBinding(unittest.IsolatedAsyncioTestCase):
    """계약 4 — UID binding이 **같은 lock 아래** 처리된다(§C1)."""

    async def test_first_subscribe_binds_the_connection(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        _, snapshot = _fresh(cache)
        await registry.issue(ws=ws, topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache,
                             now_mono=1000.0, premium_verified_at_mono=1000.0,
                             identity_verified_at_mono=1000.0)
        self.assertEqual(registry.bound_uid(ws), UID)

    async def test_cross_uid_subscribe_on_a_live_socket_is_rejected(self):
        """⛔ 한 소켓이 두 사용자 권한을 섞으면 안 된다.

        §C1은 두 대안(클라 소유 identity generation / live 소켓 cross-UID 거부) 중 **택일**로
        열어 두었다. 여기서는 **거부**(fail-closed)를 택했다 — 다른 대안을 나중에 고르는 것을
        막지 않으면서, 지금 섞이는 것만은 확실히 막는다.
        """
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        _, snapshot = _fresh(cache)
        await registry.issue(ws=ws, topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache,
                             now_mono=1000.0, premium_verified_at_mono=1000.0,
                             identity_verified_at_mono=1000.0)

        other_cache = StrictObservationCache()
        other_snapshot = other_cache.snapshot("uid-2")
        result = await registry.issue(
            ws=ws, topic="fx:usd-krw", uid="uid-2", snapshot=other_snapshot,
            cache=other_cache, now_mono=1000.0, premium_verified_at_mono=1000.0,
            identity_verified_at_mono=1000.0,
        )
        self.assertIsInstance(result, Rejected)
        self.assertIsNone(registry.active_lease(ws, "fx:usd-krw"))
        self.assertEqual(registry.bound_uid(ws), UID, "구 UID subscribe가 바인딩을 되돌렸다")

    async def test_different_connections_may_hold_different_uids(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        # ⚠️ 연결 참조를 **붙들어야** 한다 — registry는 약한 참조라 놓으면 즉시 사라진다
        # (실제 handler는 소켓을 붙들고 있으므로 이게 정상 조건이다).
        sockets = [_WS("a"), _WS("b")]
        for ws, uid in zip(sockets, ("uid-1", "uid-2")):
            c = StrictObservationCache()
            snap = c.snapshot(uid)
            await registry.issue(ws=ws, topic=TOPIC, uid=uid, snapshot=snap, cache=c,
                                 now_mono=1000.0, premium_verified_at_mono=1000.0,
                                 identity_verified_at_mono=1000.0)
        self.assertEqual(registry.connection_count(), 2)
        self.assertEqual({registry.bound_uid(ws) for ws in sockets}, {"uid-1", "uid-2"})


class TestLeaseComputation(unittest.IsolatedAsyncioTestCase):
    """계약 6 — 요청당 **단일 `now_mono`** + 3-way lease 계산."""

    async def test_expiry_is_the_three_way_minimum(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        _, snapshot = _fresh(cache)
        result = await registry.issue(
            ws=_WS(), topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache,
            now_mono=1000.0,
            premium_verified_at_mono=940.0,       # 가장 오래된 축
            identity_verified_at_mono=980.0,
        )
        self.assertAlmostEqual(result.lease.expires_at_mono, 940.0 + LEASE_MAX_SECONDS)

    async def test_registry_does_not_read_a_clock_itself(self):
        """⛔ registry가 스스로 시계를 읽으면 요청당 단일 `now_mono` 계약이 깨진다.

        같은 요청 안에서 축이 어긋나면 lease 길이가 호출 순서에 따라 흔들린다.
        """
        import inspect

        import app.topic_lease_registry as module

        source = inspect.getsource(module)
        for forbidden in ("time.monotonic", "time.time", "clock.mono"):
            self.assertNotIn(forbidden, source, f"{forbidden} 를 직접 읽고 있다")


class TestConnectionLock(unittest.IsolatedAsyncioTestCase):
    """계약 2·5 — 연결별 `asyncio.Lock`과 인터리빙 harness."""

    async def test_same_connection_is_serialised(self):
        """⛔ 같은 연결의 두 subscribe가 겹치면 UID 바인딩·CAS가 찢어진다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        order = []
        gate = asyncio.Event()
        _, snapshot = _fresh(cache)
        real_is_current = cache.is_current

        async def slow_issue(topic):
            order.append(f"enter:{topic}")
            result = await registry.issue(
                ws=ws, topic=topic, uid=UID, snapshot=snapshot, cache=cache, now_mono=1000.0,
                premium_verified_at_mono=1000.0, identity_verified_at_mono=1000.0,
            )
            order.append(f"exit:{topic}")
            return result

        def blocking_recheck(snap):
            if not gate.is_set():
                gate.set()
            return real_is_current(snap)

        cache.is_current = blocking_recheck
        await asyncio.gather(slow_issue("a"), slow_issue("b"))
        # 두 번째가 첫 번째의 exit **뒤에** 들어와야 한다
        self.assertEqual(order, ["enter:a", "exit:a", "enter:b", "exit:b"])

    async def test_different_connections_are_not_serialised(self):
        """연결별 lock이어야 한다 — 전역 lock이면 한 느린 연결이 전체를 막는다."""
        registry = TopicLeaseRegistry()
        lock_a = registry.connection_lock(_WS("a"))
        lock_b = registry.connection_lock(_WS("b"))
        self.assertIsNot(lock_a, lock_b)

    def test_critical_section_has_no_await_other_than_the_lock(self):
        """⛔ 이 불변식이 깨지는 순간 lock이 **load-bearing이 된다**.

        오늘 `issue`의 임계구역에는 lock 획득 말고 `await`가 없다 — 그래서 event loop가 자연히
        직렬화하고, lock을 지워도 테스트가 통과한다(mutation 생존으로 확인). 즉 지금 lock은
        **구조적 요구(§B4)이자 중복**이다.

        누군가 그 안에 `await`(예: ack 전송, 비동기 저장소)를 넣으면 그때부터 lock 없이는
        UID 바인딩·CAS가 찢어진다. 그 변경을 여기서 red로 드러낸다 — lock을 지우는 회귀는
        못 잡아도, **중복을 안전하게 만드는 전제**가 사라지는 것은 잡는다.
        """
        import ast
        import inspect

        import app.topic_lease_registry as module

        tree = ast.parse(inspect.getsource(module))
        issue = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "issue"
        )
        awaits = [n for n in ast.walk(issue) if isinstance(n, ast.Await)]
        self.assertEqual(
            awaits, [],
            "임계구역에 await가 생겼다 — 이제 연결별 lock이 실제로 필요하다",
        )

    async def test_lock_is_stable_per_connection(self):
        registry = TopicLeaseRegistry()
        ws = _WS()
        self.assertIs(registry.connection_lock(ws), registry.connection_lock(ws))


class TestWeakCleanupActuallyWorks(unittest.IsolatedAsyncioTestCase):
    """⛔ `WeakKeyDictionary`는 **키만** 약하다.

    값(`Lease`)이 `ws`를 강하게 잡으면 `dict → Lease → ws` 경로가 키를 살려 둔다 —
    약한 참조로 바꾼 의미가 통째로 사라진다.
    """

    async def test_connection_is_collected_when_the_handler_drops_it(self):
        import gc

        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        _, snapshot = _fresh(cache)
        await registry.issue(ws=ws, topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache,
                             now_mono=1000.0, premium_verified_at_mono=1000.0,
                             identity_verified_at_mono=1000.0)
        self.assertEqual(registry.connection_count(), 1)

        del ws                                  # handler가 비정상 종료해 정리를 못 부른 상황
        gc.collect()
        self.assertEqual(registry.connection_count(), 0, "값이 키를 살려 두고 있다")


class TestExpiredHorizonIsNotIssued(unittest.IsolatedAsyncioTestCase):
    """⛔ 이미 만료된 horizon으로 lease를 발급하면 **태어날 때부터 죽은** 구독이 된다.

    검증기의 freshness 게이트가 보통 막아 주지만, 이 모듈은 **독립 모듈**이라 그 불변식에
    기대면 안 된다 — 죽은 lease를 조용히 발급하는 쪽이 바로 여기이기 때문이다.
    """

    async def test_expired_expiry_is_rejected(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        _, snapshot = _fresh(cache)
        result = await registry.issue(
            ws=ws, topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache,
            now_mono=2000.0,
            premium_verified_at_mono=1000.0,     # 1000 + 900 = 1900 < now
            identity_verified_at_mono=1000.0,
        )
        self.assertIsInstance(result, Rejected)
        self.assertIsNone(registry.active_lease(ws, TOPIC), "만료된 lease가 활성화됐다")

    async def test_boundary_is_inclusive(self):
        """§A2 — `now >= expires_at`이 만료다(fail-closed)."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        _, snapshot = _fresh(cache)
        result = await registry.issue(
            ws=_WS(), topic=TOPIC, uid=UID, snapshot=snapshot, cache=cache,
            now_mono=1000.0 + LEASE_MAX_SECONDS,   # 정확히 경계
            premium_verified_at_mono=1000.0, identity_verified_at_mono=1000.0,
        )
        self.assertIsInstance(result, Rejected)


class TestRemoveIsNotAB4Bypass(unittest.IsolatedAsyncioTestCase):
    """⛔ `remove`가 연결 lock을 안 잡으면 **B4 우회로**가 된다.

    오늘은 임계구역에 `await`가 없어 우연히 직렬이라 동작으로는 구분되지 않는다(mutation 생존).
    그래서 **구조**를 잠근다 — 다음 조각인 sweep의 claim/CAS가 들어오는 순간 이게 load-bearing이
    되는데, 그때 red로 드러나는 것보다 지금 못 박는 편이 낫다.
    """

    def test_remove_acquires_the_connection_lock(self):
        import ast
        import inspect

        import app.topic_lease_registry as module

        tree = ast.parse(inspect.getsource(module))
        remove = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "remove"
        )
        locks = [
            n for n in ast.walk(remove)
            if isinstance(n, ast.AsyncWith)
            and any("connection_lock" in ast.dump(item.context_expr) for item in n.items)
        ]
        self.assertEqual(len(locks), 1, "remove가 연결 lock을 잡지 않는다")

    def test_public_mutators_are_async_so_the_lock_is_expressible(self):
        """동기 public 변경 API가 생기면 lock을 잡을 자리가 없다."""
        import inspect

        from app.topic_lease_registry import TopicLeaseRegistry as R

        for name in ("issue", "remove"):
            self.assertTrue(inspect.iscoroutinefunction(getattr(R, name)), f"{name}이 동기다")


class TestRemovalCas(unittest.IsolatedAsyncioTestCase):
    """§C3 — 제거·갱신은 `(ws, topic, lease_id)` CAS다."""

    async def _issue(self, registry, cache, ws, topic=TOPIC):
        _, snapshot = _fresh(cache)
        return await registry.issue(
            ws=ws, topic=topic, uid=UID, snapshot=snapshot, cache=cache, now_mono=1000.0,
            premium_verified_at_mono=1000.0, identity_verified_at_mono=1000.0,
        )

    async def test_removal_requires_the_matching_lease_id(self):
        """⛔ 구 sweep이 **갱신된** lease를 지우면 살아 있는 구독이 사라진다."""
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws = _WS()
        first = await self._issue(registry, cache, ws)
        second = await self._issue(registry, cache, ws)      # 갱신
        self.assertFalse(await registry.remove(ws, TOPIC, first.lease.lease_id), "구 id로 지워졌다")
        self.assertIsNotNone(registry.active_lease(ws, TOPIC))
        self.assertTrue(await registry.remove(ws, TOPIC, second.lease.lease_id))
        self.assertIsNone(registry.active_lease(ws, TOPIC))

    async def test_remove_websocket_clears_everything_for_that_connection(self):
        registry, cache = TopicLeaseRegistry(), StrictObservationCache()
        ws, other = _WS("a"), _WS("b")
        await self._issue(registry, cache, ws)
        await self._issue(registry, cache, other)
        registry.remove_websocket(ws)
        self.assertIsNone(registry.active_lease(ws, TOPIC))
        self.assertIsNone(registry.bound_uid(ws))
        self.assertIsNotNone(registry.active_lease(other, TOPIC), "남의 연결까지 지웠다")


if __name__ == "__main__":
    unittest.main()
