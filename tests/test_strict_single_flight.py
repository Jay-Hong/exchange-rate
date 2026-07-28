"""A5 single-flight — 같은 사용자의 동시 authoritative 검증을 1회로 합친다.

계약 근거: `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A5.

핵심 실측(이 슬라이스의 존재 이유):

    await task          + waiter 취소 → 공유 task가 **CANCELLED**
    await shield(task)  + waiter 취소 → 공유 task가 completed

즉 shield가 없으면 **한 WebSocket이 끊길 때 그 uid의 공유 검증이 죽고**, 그 결과를 기다리던
다른 연결들이 전부 실패한다. 계획이 "detached task + shield 계열"이라고 못 박은 이유다.
"""
import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from app.strict_authz import InactiveReason
from app.strict_cache import StrictObservationCache
from app.strict_single_flight import SharedFlightCancelled, StrictSingleFlight
from app.strict_verifier import (
    IdentityFound,
    IdentityUnavailable,
    ConfigFaultKind,
    StrictVerifierConfigError,
    TemporarilyUnavailable,
    VerifiedActive,
    VerifiedInactive,
    verify_strict,
)
from app.subscription import Determined

UID = "uid-1"
IAT = 1_700_000_000
WATERMARK_MS = (IAT - 100) * 1000


class _Clock:
    """고정 시각 — freshness 계산이 테스트마다 흔들리지 않게."""

    def mono(self):
        return 1000.0


class _RealClock:
    """실제 monotonic.

    ⚠️ 전체 deadline은 **주입된 시계**로 잰다. 고정 시계에서는 `remaining`이 매 반복 그대로라
    전체 상한이 호출별 상한으로 **퇴화**한다(실측). 운영에서는 `time.monotonic`이 흐르므로
    진짜 요청 단위 상한이 되고, deadline을 검증하려면 테스트도 흐르는 시계를 써야 한다.
    """

    def mono(self):
        return time.monotonic()


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

    async def test_run_carries_the_terminal_outcome(self):
        """§A5 계약 — 나르는 것은 **종결 방식**이지 관측이 아니다.

        관측을 나르면 대기자가 남의 토큰 기준 판정을 물려받는다(§A6-1). 반대로 종결 방식마저
        안 나르면 대기자가 전부 각자 owner가 되어 **장애 때 합치기가 사라진다**(실측).
        """
        flight = StrictSingleFlight()

        async def factory():
            return "unavailable"

        self.assertEqual(await flight.run(("u", 1, "premium"), factory), "unavailable")


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


class TestOutageStillCollapses(unittest.IsolatedAsyncioTestCase):
    """⛔ 회귀 잠금 — 합치기의 이득이 **정확히 필요한 순간**에 사라지던 결함.

    초안은 flight가 종결 방식을 안 날라서, 대기자들이 전부 "저장소에 아무것도 없네"를 보고
    각자 다시 owner가 됐다. 실측:

        정상 N=20 → provider 1회
        장애 N=14 → provider **14회** (= single-flight 없는 것과 동일)
        장애 N=20 → 그 재-루프가 반복 예산을 태워 `StrictVerifierConfigError` **6건**
                    (= "죽은 API key" 경보가 provider 장애에 오발화)
    """

    async def _run_outage(self, n):
        cache, flight = StrictObservationCache(), StrictSingleFlight()
        calls = []

        async def identity(uid):
            calls.append(uid)
            await asyncio.sleep(0.005)
            return IdentityUnavailable(code="UNAVAILABLE")

        async def premium(uid):
            return Determined(is_premium=True)

        results = await asyncio.gather(*(
            verify_strict(UID, token_iat_seconds=IAT, clock=_Clock(), cache=cache,
                          premium_provider=premium, identity_provider=identity,
                          single_flight=flight)
            for _ in range(n)
        ), return_exceptions=True)
        return calls, results

    async def test_outage_collapses_to_one_provider_call(self):
        calls, results = await self._run_outage(14)
        self.assertEqual(len(calls), 1, "장애 때 합치기가 사라졌다")
        self.assertTrue(all(isinstance(r, TemporarilyUnavailable) for r in results))

    async def test_outage_does_not_fire_the_config_error_alarm(self):
        """⛔ 그 경보는 '우리 설정 결함' 전용이다 — provider 장애에 울리면 운영자를 오도한다."""
        calls, results = await self._run_outage(20)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [r for r in results if isinstance(r, StrictVerifierConfigError)], [],
            "provider 장애가 죽은-API-key 경보를 울렸다",
        )


class TestHungProviderKeepsCallersResponsive(unittest.IsolatedAsyncioTestCase):
    """멈춘 provider에서 **보장되는 것과 안 되는 것**을 정확히 나눈다.

    보장: (a) 호출자는 매번 답을 받는다(매달리지 않는다), (b) 작업이 곱해지지 않는다.
    ⛔ **보장 안 되는 것: 복구.** flight는 실제 작업이 끝날 때까지 슬롯을 쥐므로, provider가
    영원히 안 끝나면 그 키는 영구히 열등 상태다(매 요청 `temporarily_unavailable`).
    복구는 **오직 provider가 스스로 끝날 때만** 일어난다 — 그래서 provider의 자체 timeout이
    이 설계의 나머지 절반이고, Firebase adapter 슬라이스의 필수 과제다.

    ⚠️ 구 이름은 "키가 brick되지 않는다"였는데 **과대주장**이었다. 위 실측대로 soft brick은 남는다.
    """

    async def test_later_requests_get_an_answer_instead_of_hanging(self):
        cache, flight = StrictObservationCache(), StrictSingleFlight()
        calls = []

        async def identity(uid):
            calls.append(uid)
            await asyncio.Event().wait()   # 영원히

        async def premium(uid):
            return Determined(is_premium=True)

        def _verify():
            return verify_strict(UID, token_iat_seconds=IAT, clock=_Clock(), cache=cache,
                                 premium_provider=premium, identity_provider=identity,
                                 single_flight=flight)

        with patch("app.strict_verifier.VERIFY_DEADLINE_SECONDS", 0.02):
            first = await _verify()
            later = await asyncio.gather(*(_verify() for _ in range(3)))

        self.assertIsInstance(first, TemporarilyUnavailable)
        self.assertTrue(all(isinstance(r, TemporarilyUnavailable) for r in later))
        self.assertEqual(len(calls), 1, "뒤이은 요청이 같은 flight에 안 붙고 새 작업을 시작했다")
        self.assertEqual(flight.in_flight_count(), 1, "복구되지 않는다는 사실 자체를 기록해 둔다")

    async def test_recovery_happens_only_when_the_provider_finishes(self):
        """⛔ 복구 조건을 명시적으로 잠근다 — provider가 끝나야 비로소 다음 요청이 성공한다."""
        cache, flight = StrictObservationCache(), StrictSingleFlight()
        release = asyncio.Event()
        calls = []

        async def identity(uid):
            calls.append(uid)
            await release.wait()
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        async def premium(uid):
            return Determined(is_premium=True)

        def _verify(deadline):
            return patch("app.strict_verifier.VERIFY_DEADLINE_SECONDS", deadline)

        with _verify(0.02):
            self.assertIsInstance(
                await verify_strict(UID, token_iat_seconds=IAT, clock=_Clock(), cache=cache,
                                    premium_provider=premium, identity_provider=identity,
                                    single_flight=flight),
                TemporarilyUnavailable,
            )
        release.set()                       # provider가 드디어 끝난다
        await asyncio.sleep(0.01)
        self.assertEqual(flight.in_flight_count(), 0, "끝난 flight가 슬롯을 안 놨다")

        result = await verify_strict(UID, token_iat_seconds=IAT, clock=_Clock(), cache=cache,
                                     premium_provider=premium, identity_provider=identity,
                                     single_flight=flight)
        self.assertIsInstance(result, VerifiedActive)
        self.assertEqual(len(calls), 1, "복구에 새 authority 호출이 필요했다면 합치기가 샌 것이다")


class TestUncancellableWorkDoesNotMultiply(unittest.IsolatedAsyncioTestCase):
    """⛔ 회귀 잠금 — `wait_for`는 `asyncio.to_thread`의 **실제 작업을 멈추지 못한다**.

    Python은 스레드를 취소할 수 없다. factory **안**에 상한을 걸면 슬롯만 비고 스레드는 계속
    돌아, 다음 요청이 같은 작업을 또 시작한다. 실측(수정 전): 실행 중 스레드 1 → 2.
    t3.small의 기본 executor는 6 스레드라 몇 번만 반복돼도 리포 전역의 `to_thread`
    86곳이 함께 막힌다.

    ⚠️ 앞의 테스트는 취소 **가능한** `asyncio.Event().wait()`만 써서 이 경로를 놓쳤다.
    """

    async def test_repeated_timeouts_do_not_spawn_more_threads(self):
        cache, flight = StrictObservationCache(), StrictSingleFlight()
        lock = threading.Lock()
        state = {"running": 0, "peak": 0}

        def blocking():
            with lock:
                state["running"] += 1
                state["peak"] = max(state["peak"], state["running"])
            time.sleep(0.3)                # 취소 불가능한 실제 작업
            with lock:
                state["running"] -= 1

        async def identity(uid):
            await asyncio.to_thread(blocking)
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        async def premium(uid):
            return Determined(is_premium=True)

        def _verify():
            return verify_strict(UID, token_iat_seconds=IAT, clock=_Clock(), cache=cache,
                                 premium_provider=premium, identity_provider=identity,
                                 single_flight=flight)

        with patch("app.strict_verifier.VERIFY_DEADLINE_SECONDS", 0.02):
            for _ in range(3):
                self.assertIsInstance(await _verify(), TemporarilyUnavailable)

        self.assertEqual(state["peak"], 1, "타임아웃마다 취소 불가능한 작업이 곱해졌다")
        await asyncio.sleep(0.35)          # 스레드 정리 대기


class TestWholeRequestDeadline(unittest.IsolatedAsyncioTestCase):
    """⛔ 회귀 잠금 — 상한은 **호출별이 아니라 요청 전체**여야 한다.

    ## deadline의 의미 (실제 시간으로 재측정해 **정정**)

    **호출자는 전체 deadline에 반환한다.** shield된 공유 flight만 계속 돌아, 뒤이은 요청이 그
    결과를 재사용한다.

    ⚠️ 구 버전은 주입 시계만 전진시키는 fake clock을 썼다. 그러면 event loop 시간이 안 흘러
    `wait_for`가 **발화하지 않고**, 루프 상단의 명시 검사만 남는다. 그 인공 세계에서 나온 표로
    "이미 시작한 호출은 끝까지 기다려 답을 낸다"고 적었는데 **거짓**이었다. 실제 시간 재측정:

        비용/상한      구 fake-clock 표          실제 시간
        10ms/100ms    VerifiedActive  iip      VerifiedActive          iip   34ms
        40ms/100ms    VerifiedActive  iip   →  **TemporarilyUnavailable iip  101ms**
        60ms/100ms    TemporarilyUnav ii       TemporarilyUnavailable  ii   101ms
        60ms/ 50ms    TemporarilyUnav i        TemporarilyUnavailable  i     51ms

    그래서 이 테스트는 provider가 **실제로** 시간을 쓰고 주입 시계도 `time.monotonic`이다 —
    production과 두 시계가 일치하는 조건에서만 계약이 검증된다.
    """

    async def _run(self, cost_per_call, deadline):
        cache, flight = StrictObservationCache(), StrictSingleFlight()
        calls = []

        async def identity(uid):
            calls.append("i")
            if len(calls) == 1:
                cache.bump(uid)          # 첫 조회 중 무효화 → 재조회 유발
            await asyncio.sleep(cost_per_call)
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        async def premium(uid):
            calls.append("p")
            await asyncio.sleep(cost_per_call)
            return Determined(is_premium=True)

        started = time.monotonic()
        with patch("app.strict_verifier.VERIFY_DEADLINE_SECONDS", deadline):
            result = await verify_strict(UID, token_iat_seconds=IAT, clock=_RealClock(), cache=cache,
                                         premium_provider=premium, identity_provider=identity,
                                         single_flight=flight)
        elapsed = time.monotonic() - started
        await asyncio.sleep(cost_per_call * 1.5)   # 배경 flight 정리
        return result, "".join(calls), elapsed, flight

    async def test_caller_returns_at_the_deadline(self):
        for cost, deadline, expected_type, expected_calls in (
            (0.01, 0.10, VerifiedActive, "iip"),          # 여유 → 완주
            (0.04, 0.10, TemporarilyUnavailable, "iip"),  # ⛔ 구 표가 Active라고 했던 행
            (0.06, 0.10, TemporarilyUnavailable, "ii"),
            (0.06, 0.05, TemporarilyUnavailable, "i"),
        ):
            with self.subTest(cost=cost, deadline=deadline):
                result, calls, elapsed, flight = await self._run(cost, deadline)
                self.assertIsInstance(result, expected_type)
                self.assertEqual(calls, expected_calls)
                self.assertLess(elapsed, deadline * 2, f"deadline을 크게 넘겼다: {elapsed:.3f}s")
                if expected_type is TemporarilyUnavailable:
                    # 일찍 포기한 게 아니라 **deadline까지 기다렸다가** 반환했음을 잠근다.
                    self.assertGreaterEqual(elapsed, deadline * 0.8)
                self.assertEqual(flight.in_flight_count(), 0, "배경 flight가 슬롯을 안 놨다")


class TestFlightCancellationIsFoldedIntoThreeState(unittest.IsolatedAsyncioTestCase):
    """⛔ C4 종결 지점 — 도메인 오류로 **바꾸기만** 해서는 안 닫힌다.

    `SharedFlightCancelled`는 `RuntimeError`라서 그대로 올려보내면 `main.py:998`의
    `except Exception`에 걸려 `finally`가 그 연결의 registry 구독을 지운다. 검증기가 **3-state로
    접어야** 비로소 "registry 불변"이 성립한다.
    """

    async def test_verify_strict_returns_temporarily_unavailable(self):
        cache, flight = StrictObservationCache(), StrictSingleFlight()
        started = asyncio.Event()

        async def identity(uid):
            started.set()
            await asyncio.sleep(10)

        async def premium(uid):
            return Determined(is_premium=True)

        task = asyncio.create_task(
            verify_strict(UID, token_iat_seconds=IAT, clock=_Clock(), cache=cache,
                          premium_provider=premium, identity_provider=identity,
                          single_flight=flight)
        )
        await started.wait()
        await asyncio.sleep(0)
        next(iter(flight._flights.values())).cancel()   # flight 자체를 취소

        result = await task
        self.assertIsInstance(result, TemporarilyUnavailable)


class TestConfigErrorInstancesAreNotShared(unittest.IsolatedAsyncioTestCase):
    """⛔ 예외 **인스턴스**를 공유하면 배선이 `__context__`를 얹어 한 연결의 로그에 다른 연결의
    상태가 찍힌다. 메시지만 넘기고 각자 자기 인스턴스로 raise한다."""

    async def _gather_config_errors(self, provider_result, n=6):
        cache, flight = StrictObservationCache(), StrictSingleFlight()

        async def identity(uid):
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        async def premium(uid):
            await asyncio.sleep(0.005)
            return provider_result

        return await asyncio.gather(*(
            verify_strict(UID, token_iat_seconds=IAT, clock=_Clock(), cache=cache,
                          premium_provider=premium, identity_provider=identity,
                          single_flight=flight)
            for _ in range(n)
        ), return_exceptions=True)

    async def test_each_caller_gets_its_own_exception_object(self):
        from app.subscription import ProviderMisconfigured

        results = await self._gather_config_errors(ProviderMisconfigured(status=401), n=3)
        self.assertTrue(all(isinstance(r, StrictVerifierConfigError) for r in results))
        self.assertEqual(len({id(r) for r in results}), 3, "예외 인스턴스가 공유됐다")

    async def test_waiters_get_the_same_message_not_a_content_free_stub(self):
        """⛔ 회귀 잠금 — fault를 **값으로** 안 나르면 대기자는 owner의 closure를 못 본다.

        실측(수정 전): 6건 중 **5건**이 provider payload도 HTTP status도 없는 stub이었다.
        계획상 운영자 신호는 ERROR 로그가 내는데 83%가 비면 계약이 깨진 것이다.
        """
        from app.subscription import ProviderMisconfigured

        results = await self._gather_config_errors(ProviderMisconfigured(status=401))
        self.assertEqual(len({str(r) for r in results}), 1, "대기자가 stub 메시지를 받았다")
        self.assertIn("401", str(results[0]), "provider payload가 사라졌다")

    async def test_kind_is_bounded_and_discriminates_causes(self):
        """⛔ 메시지는 `repr(result)`를 담아 cardinality가 열려 있다 — **카운터는 `kind`로** 센다.

        죽은 API key(즉시 호출)와 스키마 drift(계약 재검토)는 대응 주체가 다르다.
        """
        from app.subscription import BadRequest, ProtocolViolation, ProviderMisconfigured

        cases = {
            ConfigFaultKind.PREMIUM_MISCONFIGURED: ProviderMisconfigured(status=401),
            ConfigFaultKind.PREMIUM_BAD_REQUEST: BadRequest(),
            ConfigFaultKind.PREMIUM_PROTOCOL_VIOLATION: ProtocolViolation(detail="shape"),
        }
        for expected_kind, provider_result in cases.items():
            with self.subTest(kind=expected_kind.value):
                results = await self._gather_config_errors(provider_result, n=3)
                self.assertEqual({r.kind for r in results}, {expected_kind})


class TestSingleFlightInjectionIsFailClosed(unittest.TestCase):
    """⛔ `single_flight`에 기본값을 두면 배선에서 한 번 빠뜨렸을 때 **조용히 비활성**된다.

    그러면 호출자 쪽 `wait_for`가 `to_thread` 코루틴만 취소하고 실제 스레드는 남아, 다음 요청이
    새 작업을 시작한다 — 앞서 닫은 "작업 곱하기"가 그대로 재발한다. 소비자가 0인 지금 필수 인자로
    두는 게 안전하다.

    ⚠️ 모든 테스트가 인자를 명시로 넘기므로, 이 성질은 **시그니처를 직접 봐야** 잠긴다
    (기본값을 되살리는 mutation이 그 전까지 생존했다).
    """

    def test_single_flight_has_no_default(self):
        import inspect

        param = inspect.signature(verify_strict).parameters["single_flight"]
        self.assertIs(param.default, inspect.Parameter.empty, "기본값이 생기면 조용히 비활성된다")


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
