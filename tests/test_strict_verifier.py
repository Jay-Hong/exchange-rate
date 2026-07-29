"""A6 strict verifier — WS 인가용 3-state 검증기.

계약 근거: `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A6 / A6-1 / A6-2 / C4.

    active(verified_at_monotonic) | inactive(...) | temporarily_unavailable(retry_after_seconds)

`bool`로는 "권한 없음"과 "확인 불가"를 구분할 수 없어 C4(registry 불변 + lease 미연장)가
성립하지 않는다. 그래서 3-state다.

test-first로 작성됐다. provider는 **주입**한다 — 이 모듈이 `firebase_admin`을 최상위에서
import하면 단위 테스트가 import chain으로 깨진다(리포 관용구, `app/entitlements.py:3`).
"""
import asyncio
import unittest
from unittest.mock import patch

from app.strict_authz import Concern, InactiveReason
from app.strict_cache import StrictObservationCache
from app.strict_single_flight import StrictSingleFlight
from app.subscription import (
    BadRequest,
    Determined,
    ProtocolViolation,
    ProviderMisconfigured,
    ProviderUnavailable,
)
from app.strict_cache import PutAccepted, PutRejected, RejectReason
from app.strict_verifier import (
    VERIFY_DEADLINE_SECONDS,
    IdentityFound,
    IdentityMisconfigured,
    IdentityNotFound,
    IdentityUnavailable,
    StrictVerifierConfigError,
    TemporarilyUnavailable,
    VerifiedActive,
    VerifiedInactive,
    verify_strict,
)

UID = "uid-1"
IAT = 1_700_000_000
WATERMARK_MS = (IAT - 100) * 1000   # iat 이후 revoke 없음


class _Clock:
    """wall과 mono를 독립적으로 제어한다."""

    def __init__(self, mono=1000.0):
        self._mono = mono

    def mono(self):
        return self._mono

    def advance(self, seconds):
        self._mono += seconds


class _Provider:
    """호출을 기록하는 주입용 provider."""

    def __init__(self, *results):
        self._results = list(results)
        self.calls = []

    async def __call__(self, uid):
        self.calls.append(uid)
        result = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


def _run(**overrides):
    kwargs = dict(
        uid=UID,
        token_iat_seconds=IAT,
        clock=_Clock(),
        cache=StrictObservationCache(),
        premium_provider=_Provider(Determined(is_premium=True)),
        identity_provider=_Provider(IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)),
        single_flight=StrictSingleFlight(),
    )
    kwargs.update(overrides)
    return asyncio.run(verify_strict(**kwargs)), kwargs


class TestHappyPath(unittest.TestCase):
    def test_cold_cache_verifies_both_concerns_and_returns_active(self):
        result, kw = _run()
        self.assertIsInstance(result, VerifiedActive)
        self.assertEqual(kw["identity_provider"].calls, [UID])
        self.assertEqual(kw["premium_provider"].calls, [UID])

    def test_active_carries_both_verification_timestamps(self):
        clock = _Clock(mono=500.0)
        result, _ = _run(clock=clock)
        self.assertEqual(result.premium_verified_at_mono, 500.0)
        self.assertEqual(result.identity_verified_at_mono, 500.0)

    def test_identity_is_verified_before_premium(self):
        """§D5 — 인증(identity)이 인가(premium)보다 먼저다.

        순서가 뒤집히면 삭제된 계정에 대해 RevenueCat을 먼저 호출하게 된다.
        """
        order = []
        cache = StrictObservationCache()

        async def identity(uid):
            order.append("identity")
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        async def premium(uid):
            order.append("premium")
            return Determined(is_premium=True)

        _run(cache=cache, identity_provider=identity, premium_provider=premium)
        self.assertEqual(order, ["identity", "premium"])

    def test_warm_cache_does_no_io(self):
        """관측이 신선하면 authority를 다시 부르지 않는다 — 이게 저장소의 존재 이유다."""
        cache = StrictObservationCache()
        _run(cache=cache)
        prem, iden = _Provider(Determined(is_premium=True)), _Provider(
            IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)
        )
        result, _ = _run(cache=cache, premium_provider=prem, identity_provider=iden)
        self.assertIsInstance(result, VerifiedActive)
        self.assertEqual(prem.calls, [])
        self.assertEqual(iden.calls, [])

    def test_stale_observation_triggers_reverification(self):
        cache = StrictObservationCache()
        clock = _Clock()
        _run(cache=cache, clock=clock)
        clock.advance(10_000)  # 모든 horizon 초과
        prem = _Provider(Determined(is_premium=True))
        result, _ = _run(cache=cache, clock=clock, premium_provider=prem)
        self.assertIsInstance(result, VerifiedActive)
        self.assertEqual(prem.calls, [UID])


class TestInactiveOutcomes(unittest.TestCase):
    def test_disabled_account(self):
        result, _ = _run(
            identity_provider=_Provider(IdentityFound(disabled=True, tokens_valid_after_ms=WATERMARK_MS))
        )
        self.assertEqual(result, VerifiedInactive(reason=InactiveReason.ACCOUNT_DISABLED))

    def test_deleted_account(self):
        result, _ = _run(identity_provider=_Provider(IdentityNotFound()))
        self.assertEqual(result, VerifiedInactive(reason=InactiveReason.ACCOUNT_DELETED))

    def test_revoked_token(self):
        """watermark(ms) > iat(s) — 축을 섞으면 모든 토큰이 revoked로 오판된다."""
        result, _ = _run(
            identity_provider=_Provider(
                IdentityFound(disabled=False, tokens_valid_after_ms=(IAT + 500) * 1000)
            )
        )
        self.assertEqual(result, VerifiedInactive(reason=InactiveReason.TOKEN_REVOKED))

    def test_no_subscription(self):
        result, _ = _run(premium_provider=_Provider(Determined(is_premium=False)))
        self.assertEqual(result, VerifiedInactive(reason=InactiveReason.PREMIUM_INACTIVE))

    def test_disabled_beats_premium_check(self):
        """계정이 비활성이면 RevenueCat을 부를 이유가 없다."""
        prem = _Provider(Determined(is_premium=True))
        _run(
            premium_provider=prem,
            identity_provider=_Provider(IdentityFound(disabled=True, tokens_valid_after_ms=WATERMARK_MS)),
        )
        self.assertEqual(prem.calls, [])


class TestTemporarilyUnavailable(unittest.TestCase):
    """§C4 — 판정 **불가**는 거부와 다르다. registry 불변 + lease 미연장."""

    def test_premium_provider_unavailable(self):
        result, _ = _run(premium_provider=_Provider(ProviderUnavailable(status=503)))
        self.assertIsInstance(result, TemporarilyUnavailable)
        self.assertEqual(result.concern, Concern.PREMIUM)
        self.assertGreater(result.retry_after_seconds, 0)

    def test_identity_provider_unavailable(self):
        result, _ = _run(identity_provider=_Provider(IdentityUnavailable(code="UNAVAILABLE")))
        self.assertIsInstance(result, TemporarilyUnavailable)
        self.assertEqual(result.concern, Concern.IDENTITY)

    def test_unavailable_does_not_cache_anything(self):
        """⛔ 판정 불가를 저장하면 그 창 동안 재시도가 무의미해진다."""
        cache = StrictObservationCache()
        _run(cache=cache, premium_provider=_Provider(ProviderUnavailable(status=503)))
        snap = cache.snapshot(UID)
        self.assertIsNone(snap.premium)

    def test_identity_unavailable_does_not_call_premium(self):
        prem = _Provider(Determined(is_premium=True))
        _run(premium_provider=prem, identity_provider=_Provider(IdentityUnavailable()))
        self.assertEqual(prem.calls, [])


class TestConfigAndProgrammingErrorsPropagate(unittest.TestCase):
    """§A6-1 — programming·config 오류는 **verdict가 아니다**.

    이걸 `temporarily_unavailable`(retryable)로 접으면 재시도 storm이 된다. 계획이 명시적으로
    "그 변환이 없음을 mutation 테스트로 잠근다"고 요구한다.
    """

    def test_permanent_provider_faults_raise_the_exact_wiring_type(self):
        """⛔ **정확한 타입**을 단언한다 — 배선 계약이 "타입으로 구분"이기 때문이다.

        `assertRaises(Exception)`로 두면 `RuntimeError`나 `ValueError`로 바뀌어도 green이라,
        정작 지켜야 할 성질(배선이 타입으로 잡아 전용 카운터를 올린다)이 잠기지 않는다.
        """
        cases = {
            "premium misconfigured": dict(premium_provider=_Provider(ProviderMisconfigured(status=401))),
            "premium bad request": dict(premium_provider=_Provider(BadRequest())),
            "premium protocol violation": dict(
                premium_provider=_Provider(ProtocolViolation(detail="entitlements not a dict"))
            ),
            "identity misconfigured": dict(
                identity_provider=_Provider(IdentityMisconfigured(code="PERMISSION_DENIED"))
            ),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(StrictVerifierConfigError):
                    _run(**kwargs)

    def test_axis_violation_is_not_a_config_error(self):
        """축 위반은 **우리 코드 버그**라 설정 오류와 구분돼야 한다 — 대응 주체가 다르다."""
        with self.assertRaises(ValueError) as ctx:
            _run(
                identity_provider=_Provider(
                    IdentityFound(disabled=False, tokens_valid_after_ms=IAT)  # ms 자리에 초를 넣음
                )
            )
        self.assertNotIsInstance(ctx.exception, StrictVerifierConfigError)

    def test_arbitrary_provider_exception_is_not_a_config_error(self):
        """adapter가 던진 임의 예외를 설정 오류로 오분류하면 카운터가 오염된다."""
        with self.assertRaises(RuntimeError) as ctx:
            _run(premium_provider=_Provider(RuntimeError("boom")))
        self.assertNotIsInstance(ctx.exception, StrictVerifierConfigError)


class TestFenceHandoff(unittest.TestCase):
    """§A4 소비 fence는 **판정을 만든 그 snapshot**을 넘겨야 성립한다."""

    def test_active_carries_the_deriving_snapshot(self):
        cache = StrictObservationCache()
        result, _ = _run(cache=cache)
        self.assertIsNotNone(result.snapshot)
        self.assertTrue(cache.is_current(result.snapshot))

    def test_invalidation_after_verification_is_detectable_by_the_caller(self):
        """⛔ 이게 fence의 전부다 — 호출자가 snapshot을 **다시 뜨면** 이 검사는 통과해 버린다.

        `cache.snapshot()`은 없는 uid에 generation을 할당하는 **쓰기**라, 다시 뜬 snapshot은
        언제나 '현행'이다. 그래서 검증기가 자기 snapshot을 돌려줘야 한다.
        """
        cache = StrictObservationCache()
        result, _ = _run(cache=cache)
        cache.bump(UID)                       # 검증 직후 webhook
        self.assertFalse(cache.is_current(result.snapshot), "lease를 발급하면 안 되는 상태")
        self.assertTrue(cache.is_current(cache.snapshot(UID)), "다시 뜬 snapshot은 항상 현행이다")


class _CountingCache:
    """`snapshot()` 호출을 세는 proxy — 반환된 snapshot이 **파생에 쓴 그것**인지 보기 위해."""

    def __init__(self, inner):
        self._inner = inner
        self.snapshot_calls = 0

    def snapshot(self, uid):
        self.snapshot_calls += 1
        return self._inner.snapshot(uid)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _AlwaysRejectingCache:
    """epoch는 그대로인데 쓰기만 거부하는 저장소 — 경쟁 예산 회계를 **격리**해서 본다.

    실제로 bump가 나면 '쓰기 거부'와 '다음 반복의 epoch 변화'가 **함께** 일어나 두 경로가
    서로를 가려준다. 그래서 한쪽만 지워도 테스트가 통과했다(mutation 생존).
    """

    def __init__(self, inner):
        self._inner = inner

    def put_premium(self, observation, *, expected_epoch, key=None):
        return PutRejected(reason=RejectReason.REGRESSION, expected_epoch=expected_epoch,
                           current_epoch=expected_epoch)

    put_identity = put_premium

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestBudgetAccounting(unittest.TestCase):
    def test_write_rejection_alone_consumes_the_contention_budget(self):
        """⛔ epoch가 안 바뀌어도 쓰기 거부만으로 유한하게 포기해야 한다."""
        cache = _AlwaysRejectingCache(StrictObservationCache())
        iden = _Provider(IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS))
        result, _ = _run(cache=cache, identity_provider=iden)
        self.assertIsInstance(result, TemporarilyUnavailable)
        self.assertLessEqual(len(iden.calls), 5, "예산이 안 깎이면 호출이 폭증한다")

    def test_returned_snapshot_is_the_deriving_one_not_a_fresh_read(self):
        """⛔ 반환 직전에 새로 뜨면 fence가 자기 자신을 통과시킨다(`snapshot()`은 쓰기다)."""
        cache = _CountingCache(StrictObservationCache())
        result, _ = _run(cache=cache)
        # cold path = identity 조회 / premium 조회 / Active 파생 = snapshot 3회.
        # 반환 직전에 한 번 더 뜨면 4회가 된다.
        self.assertEqual(cache.snapshot_calls, 3)
        self.assertIsInstance(result, VerifiedActive)

    def test_giving_up_reports_the_concern_that_actually_churned(self):
        """Firebase 쪽에서 churn했는데 RevenueCat 장애로 보고하면 운영자가 엉뚱한 곳을 본다."""
        cache = StrictObservationCache()
        prem = _Provider(Determined(is_premium=True))

        async def identity(uid):
            cache.bump(uid)
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        result, _ = _run(cache=cache, identity_provider=identity, premium_provider=prem)
        self.assertIsInstance(result, TemporarilyUnavailable)
        self.assertEqual(result.concern, Concern.IDENTITY)
        self.assertEqual(prem.calls, [])


class _BumpAfterAcceptCache:
    """쓰기는 **성공**시키고 그 직후 무효화한다 — `[쓰기 수락, 다음 snapshot]` 창의 재현.

    이 창의 bump는 쓰기 거부를 만들지 않으므로, 거부만 세는 예산은 여기서 **전혀 깎이지 않는다**.
    실제 bump 시나리오에서는 거부와 epoch 변화가 함께 일어나 서로를 가려주기 때문에, 이 구멍은
    이렇게 격리해야만 드러난다(mutation 생존으로 발견).
    """

    def __init__(self, inner):
        self._inner = inner

    def _accept_then_bump(self, put_result, uid):
        if isinstance(put_result, PutAccepted):
            self._inner.bump(uid)
        return put_result

    def put_premium(self, observation, *, expected_epoch, key=None):
        return self._accept_then_bump(
            self._inner.put_premium(observation, expected_epoch=expected_epoch, key=key),
            observation.uid,
        )

    def put_identity(self, observation, *, expected_epoch, key=None):
        return self._accept_then_bump(
            self._inner.put_identity(observation, expected_epoch=expected_epoch, key=key),
            observation.uid,
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestPostAcceptInvalidationIsBounded(unittest.TestCase):
    def test_bump_after_an_accepted_write_still_consumes_the_budget(self):
        """⛔ 이 창의 bump를 안 세면 루프가 예산 없이 돌아 provider 호출이 폭증한다."""
        cache = _BumpAfterAcceptCache(StrictObservationCache())
        iden = _Provider(IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS))
        result, _ = _run(cache=cache, identity_provider=iden)
        self.assertIsInstance(result, TemporarilyUnavailable)
        self.assertLessEqual(len(iden.calls), 5, "예산이 안 깎이면 호출이 폭증한다")


class TestRetryAfterContract(unittest.TestCase):
    """§D-const — 정수 초, 1~30. 클라는 Int로 디코드한다."""

    def test_out_of_range_is_rejected(self):
        for bad in (0, 31, -1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    TemporarilyUnavailable(concern=Concern.PREMIUM, retry_after_seconds=bad)

    def test_non_integer_is_rejected(self):
        for bad in (5.0, True, "5"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    TemporarilyUnavailable(concern=Concern.PREMIUM, retry_after_seconds=bad)

    def test_default_is_in_range_and_integral(self):
        value = TemporarilyUnavailable(concern=Concern.PREMIUM).retry_after_seconds
        self.assertIsInstance(value, int)
        self.assertTrue(1 <= value <= 30)


class TestSelfInflictedLoopsRaise(unittest.TestCase):
    """§A6-1 — 우리 설정·논리 오류를 retryable로 접으면 재시도 storm이 된다."""

    def test_zero_freshness_horizon_raises_instead_of_looping(self):
        """⛔ 실측 — horizon을 0으로 두자 **요청 1건당 RevenueCat 6회** 후 retryable 반환이었다.

        계획이 이 상수를 낮추는 튜닝을 명시적으로 예고하므로(§A6-2) 도달 가능한 경로다.
        """
        calls = []

        async def premium(uid):
            calls.append(uid)
            return Determined(is_premium=False)

        with patch("app.strict_authz.PREMIUM_INACTIVE_RECHECK_SECONDS", 0.0):
            with self.assertRaises(Exception) as ctx:
                _run(premium_provider=premium)
        self.assertNotIsInstance(ctx.exception, AssertionError)
        self.assertLessEqual(len(calls), 2, "raise 전에 provider를 반복 호출하면 안 된다")


class TestEpochFencing(unittest.TestCase):
    def test_observation_is_written_under_the_captured_epoch(self):
        cache = StrictObservationCache()
        _run(cache=cache)
        snap = cache.snapshot(UID)
        self.assertEqual(snap.premium.epoch, snap.epoch)
        self.assertEqual(snap.identity.epoch, snap.epoch)

    def test_invalidation_during_verification_is_retried_and_converges(self):
        """webhook이 검증 도중 들어오면 그 쓰기는 거부되고, 재시도가 새 epoch로 수렴한다."""
        cache = StrictObservationCache()
        bumped = []

        async def identity(uid):
            if not bumped:
                bumped.append(True)
                cache.bump(uid)      # 이 검증이 캡처한 epoch를 무효화
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        result, _ = _run(cache=cache, identity_provider=identity)
        self.assertIsInstance(result, VerifiedActive)
        self.assertIsNotNone(cache.snapshot(UID).identity)

    def test_relentless_invalidation_gives_up_with_temporarily_unavailable(self):
        """⛔ 무한 루프 금지 — 경쟁이 끝나지 않으면 유한하게 포기한다."""
        cache = StrictObservationCache()

        async def identity(uid):
            cache.bump(uid)          # 매번 무효화 → 매번 쓰기 거부
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        result, _ = _run(cache=cache, identity_provider=identity)
        self.assertIsInstance(result, TemporarilyUnavailable)

    def test_giving_up_is_bounded_not_unbounded_io(self):
        """포기 전 authority 호출 횟수에 상한이 있어야 한다 — 아니면 장애 시 storm이 된다."""
        cache = StrictObservationCache()
        calls = []

        async def identity(uid):
            calls.append(uid)
            cache.bump(uid)
            return IdentityFound(disabled=False, tokens_valid_after_ms=WATERMARK_MS)

        _run(cache=cache, identity_provider=identity)
        self.assertLessEqual(len(calls), 6, "재시도가 사실상 무한이면 장애 시 storm이 된다")
        self.assertGreater(len(calls), 1, "한 번도 재시도하지 않으면 정상 경쟁에서 실패한다")


class TestVerifierDeadlineIsPinned(unittest.TestCase):
    """**코드 기본값만 잠근다.** 계획 문서 쪽은 지키지 않는다.

    실측(2026-07-29): pin 없이 `8.0→80.0`이 전체 스위트 생존했다. 이 상수의 행동은
    `tests/test_strict_single_flight.py`가 `patch("app.strict_verifier.VERIFY_DEADLINE_SECONDS", …)`로
    **주입해서** 검증하므로, 기본값 자체는 어떤 테스트도 읽지 않았다.

    ⛔ 이 값은 D-const `ACK_TIMEOUT_SECONDS` 행 **근거 칸 산문**에 `8s + 5s = 13s > 10s`로도
    인용돼 있지만, **이 pin은 그 산문을 지키지 않는다** — 실측: 문서의 `8s`를 `6s`로 바꿔도
    전체 스위트 green이다. 계획 문서는 doc→code 방향이 비구속이라고 D-const가 스스로 밝힌다.
    코드를 바꾸면 red가 나서 산문을 함께 고칠 기회가 생길 뿐, 산문 단독 변경은 무증상이다.

    ⛔ 생존은 **런타임 결함이 아니다** — "기본값 변경을 탐지하지 못했다"는 뜻일 뿐이다.

    ⚠️ 코드 방향에서도 이 pin이 **유일한 red는 아니다**. 실측 경계(2026-07-29, 전체 스위트):

        deadline <= 5.0        → 2 red (pin + TestTransportTimeout)   [4.0 · 5.0 확인]
        5.0 < deadline != 8.0  → 1 red (pin 단독)                     [5.5 · 6.0 · 20.0 확인]
        deadline == 8.0        → green

    `tests/test_firebase_identity.py::TestTransportTimeout::test_timeout_is_below_the_verifier_deadline`이
    `assertLess(FIREBASE_HTTP_TIMEOUT_SECONDS, VERIFY_DEADLINE_SECONDS)`(=5 < deadline)이라
    **5.0 자신도 red**다 — 엄격 부등호이므로 경계는 "5.0 아래"가 아니라 "5.0 이하"다.

    ⛔ 따라서 이 pin이 단독으로 잡는 범위는 **"상향"이 아니라 5.0 초과인 모든 비-8.0 값**이다.
    5.0~8.0 사이의 **하향** 변경(6.0 등)도 pin만 잡는다.
    """

    def test_verify_deadline_is_pinned(self):
        self.assertEqual(VERIFY_DEADLINE_SECONDS, 8.0)

if __name__ == "__main__":
    unittest.main()
