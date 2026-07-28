"""A5 single-flight — 같은 사용자의 동시 authoritative 검증을 1회로 합친다.

계약 근거: `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A5.

핵심 실측(이 슬라이스의 존재 이유):

    await task          + waiter 취소 → 공유 task가 **CANCELLED**
    await shield(task)  + waiter 취소 → 공유 task가 completed

즉 shield가 없으면 **한 WebSocket이 끊길 때 그 uid의 공유 검증이 죽고**, 그 결과를 기다리던
다른 연결들이 전부 실패한다. 계획이 "detached task + shield 계열"이라고 못 박은 이유다.
"""
import asyncio
import unittest

from app.strict_authz import InactiveReason
from app.strict_cache import StrictObservationCache
from app.strict_single_flight import StrictSingleFlight
from app.strict_verifier import (
    IdentityFound,
    VerifiedActive,
    VerifiedInactive,
    verify_strict,
)
from app.subscription import Determined

UID = "uid-1"
IAT = 1_700_000_000
WATERMARK_MS = (IAT - 100) * 1000


class _Clock:
    def mono(self):
        return 1000.0


class TestCollapsing(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_same_key_runs_the_factory_once(self):
        flight = StrictSingleFlight()
        runs = []

        async def factory():
            runs.append(1)
            await asyncio.sleep(0.01)

        await asyncio.gather(*(flight.run(("u", 1, "premium"), factory) for _ in range(5)))
        self.assertEqual(len(runs), 1)

    async def test_different_keys_do_not_collapse(self):
        flight = StrictSingleFlight()
        runs = []

        async def factory():
            runs.append(1)
            await asyncio.sleep(0.01)

        await asyncio.gather(
            flight.run(("u", 1, "premium"), factory),
            flight.run(("u", 1, "identity"), factory),
            flight.run(("u", 2, "premium"), factory),
            flight.run(("other", 1, "premium"), factory),
        )
        self.assertEqual(len(runs), 4)

    async def test_run_carries_no_value(self):
        """§A5 계약 — 공유 flight는 관측을 실어 나르지 않는다. waiter는 저장소에서 다시 읽는다."""
        flight = StrictSingleFlight()

        async def factory():
            return "값을 돌려줘도"

        self.assertIsNone(await flight.run(("u", 1, "premium"), factory))


class TestCancellationIsolation(unittest.IsolatedAsyncioTestCase):
    """⛔ 이 슬라이스의 핵심 — 한 연결의 취소가 공유 검증을 죽이면 안 된다."""

    async def _flight_with_blocker(self):
        flight = StrictSingleFlight()
        started, release = asyncio.Event(), asyncio.Event()
        outcome = []

        async def factory():
            started.set()
            try:
                await release.wait()
                outcome.append("completed")
            except asyncio.CancelledError:
                outcome.append("CANCELLED")
                raise

        return flight, started, release, outcome, factory

    async def test_waiter_cancellation_does_not_cancel_the_shared_flight(self):
        flight, started, release, outcome, factory = await self._flight_with_blocker()
        key = ("u", 1, "premium")

        owner = asyncio.create_task(flight.run(key, factory))
        await started.wait()
        waiter = asyncio.create_task(flight.run(key, factory))
        await asyncio.sleep(0)

        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        release.set()
        await owner
        self.assertEqual(outcome, ["completed"], "waiter 취소가 공유 검증을 죽였다")

    async def test_owner_cancellation_does_not_cancel_the_shared_flight(self):
        """owner의 소켓이 먼저 끊겨도 **다른 연결이 그 결과를 기다린다**(§A5)."""
        flight, started, release, outcome, factory = await self._flight_with_blocker()
        key = ("u", 1, "premium")

        owner = asyncio.create_task(flight.run(key, factory))
        await started.wait()
        waiter = asyncio.create_task(flight.run(key, factory))
        await asyncio.sleep(0)

        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner

        release.set()
        await waiter
        self.assertEqual(outcome, ["completed"], "owner 취소가 공유 검증을 죽였다")


class TestSlotCleanup(unittest.IsolatedAsyncioTestCase):
    """§A5 — 실패·취소 후 슬롯이 정리돼야 다음 요청이 다시 검증할 수 있다."""

    async def test_slot_is_released_after_success(self):
        flight = StrictSingleFlight()
        runs = []

        async def factory():
            runs.append(1)

        key = ("u", 1, "premium")
        await flight.run(key, factory)
        await flight.run(key, factory)
        self.assertEqual(len(runs), 2, "성공 후 슬롯이 안 비면 낡은 결과가 영구 재사용된다")

    async def test_slot_is_released_after_failure(self):
        flight = StrictSingleFlight()
        runs = []

        async def factory():
            runs.append(1)
            raise RuntimeError("boom")

        key = ("u", 1, "premium")
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await flight.run(key, factory)
        self.assertEqual(len(runs), 2, "실패 후 슬롯이 안 비면 그 키가 영구히 막힌다")

    async def test_failure_reaches_every_awaiter(self):
        flight = StrictSingleFlight()
        started = asyncio.Event()

        async def factory():
            started.set()
            await asyncio.sleep(0.01)
            raise RuntimeError("boom")

        key = ("u", 1, "premium")
        results = await asyncio.gather(
            *(flight.run(key, factory) for _ in range(3)), return_exceptions=True
        )
        self.assertTrue(all(isinstance(r, RuntimeError) for r in results))

    async def test_no_flights_leak_after_completion(self):
        flight = StrictSingleFlight()

        async def factory():
            await asyncio.sleep(0)

        await asyncio.gather(*(flight.run(("u", i, "premium"), factory) for i in range(5)))
        self.assertEqual(flight.in_flight_count(), 0, "슬롯 누수 = 메모리 누수")


    async def test_release_only_removes_its_own_flight(self):
        """⛔ 완료된 task의 콜백이 **다음 flight의 슬롯**을 지우면 안 된다.

        `add_done_callback`은 완료 직후가 아니라 다음 loop 반복에 돌기 때문에, 그 사이 같은 키로
        새 flight가 자리를 차지할 수 있다. 소유권 확인이 없으면 그 새 flight가 미아가 되고
        합치기가 조용히 무력화된다.
        """
        flight = StrictSingleFlight()
        key = ("u", 1, "premium")

        async def factory():
            await asyncio.sleep(0.05)

        current = asyncio.ensure_future(factory())
        flight._flights[key] = current
        stale = asyncio.ensure_future(factory())      # 이미 끝난 **다른** flight를 흉내
        stale.cancel()
        try:
            await stale
        except asyncio.CancelledError:
            pass

        flight._release(key, stale)
        self.assertIs(flight._flights.get(key), current, "남의 flight를 지웠다")
        current.cancel()
        try:
            await current
        except asyncio.CancelledError:
            pass


class TestKeyIncludesEpoch(unittest.IsolatedAsyncioTestCase):
    """실측 — epoch가 키에 없으면 새 요청이 **이미 무효화된 flight**에 붙어 한 RTT를 버린다.

    ⚠️ 정확성은 저장소의 CAS가 보장한다(어느 쪽이든 최종 결과는 정상). 이건 **지연** 속성이다.
    """

    async def test_new_request_does_not_join_an_invalidated_flight(self):
        cache = StrictObservationCache()
        flight = StrictSingleFlight()
        gate = asyncio.Event()
        identity_calls = []

        async def identity(uid):
            identity_calls.append(uid)
            await gate.wait()
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        async def premium(uid):
            return Determined(is_premium=True)

        def _verify():
            return verify_strict(UID, token_iat_seconds=IAT, clock=_Clock(), cache=cache,
                                 premium_provider=premium, identity_provider=identity,
                                 single_flight=flight)

        first = asyncio.create_task(_verify())
        while not identity_calls:
            await asyncio.sleep(0)
        cache.bump(UID)                       # 진행 중 flight를 무효화
        before = len(identity_calls)
        second = asyncio.create_task(_verify())
        await asyncio.sleep(0.02)

        self.assertGreater(
            len(identity_calls), before,
            "새 요청이 무효화된 flight에 붙어 기다렸다 — 한 RTT 낭비",
        )
        gate.set()
        await asyncio.gather(first, second, return_exceptions=True)


class TestVerifierIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_verifications_call_each_authority_once(self):
        cache = StrictObservationCache()
        flight = StrictSingleFlight()
        prem, iden = [], []

        async def premium(uid):
            prem.append(uid)
            await asyncio.sleep(0.01)
            return Determined(is_premium=True)

        async def identity(uid):
            iden.append(uid)
            await asyncio.sleep(0.01)
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        results = await asyncio.gather(*(
            verify_strict(UID, token_iat_seconds=IAT, clock=_Clock(), cache=cache,
                          premium_provider=premium, identity_provider=identity,
                          single_flight=flight)
            for _ in range(4)
        ))
        self.assertTrue(all(isinstance(r, VerifiedActive) for r in results))
        self.assertEqual(len(iden), 1, "identity 조회가 합쳐지지 않았다")
        self.assertEqual(len(prem), 1, "premium 조회가 합쳐지지 않았다")

    async def test_different_users_are_not_collapsed(self):
        cache = StrictObservationCache()
        flight = StrictSingleFlight()
        seen = []

        async def identity(uid):
            seen.append(uid)
            await asyncio.sleep(0.01)
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        async def premium(uid):
            return Determined(is_premium=True)

        await asyncio.gather(*(
            verify_strict(u, token_iat_seconds=IAT, clock=_Clock(), cache=cache,
                          premium_provider=premium, identity_provider=identity,
                          single_flight=flight)
            for u in ("a", "b")
        ))
        self.assertEqual(sorted(seen), ["a", "b"])

    async def test_shared_observation_still_yields_per_token_verdicts(self):
        """⛔ 합쳐도 **판정은 토큰마다 다르다** — A6-1의 저장/파생 분리가 여기서 값을 한다.

        같은 uid의 두 연결이 identity 조회를 공유하지만, watermark보다 오래된 토큰은 revoked이고
        새 토큰은 통과해야 한다. verdict를 저장했다면 한쪽이 다른 쪽 판정을 물려받았을 것이다.
        """
        cache = StrictObservationCache()
        flight = StrictSingleFlight()
        watermark_ms = IAT * 1000  # IAT 이전 토큰은 revoked
        calls = []

        async def identity(uid):
            calls.append(uid)
            await asyncio.sleep(0.01)
            return IdentityFound(disabled=False, tokens_valid_after_ms=watermark_ms)

        async def premium(uid):
            return Determined(is_premium=True)

        old_token, new_token = IAT - 10, IAT + 10
        results = await asyncio.gather(*(
            verify_strict(UID, token_iat_seconds=iat, clock=_Clock(), cache=cache,
                          premium_provider=premium, identity_provider=identity,
                          single_flight=flight)
            for iat in (old_token, new_token)
        ))
        self.assertEqual(len(calls), 1, "조회는 한 번이어야 한다")
        self.assertEqual(results[0], VerifiedInactive(reason=InactiveReason.TOKEN_REVOKED))
        self.assertIsInstance(results[1], VerifiedActive)


if __name__ == "__main__":
    unittest.main()
