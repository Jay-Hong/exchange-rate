"""`app/topic_lease.py` — A1 lease horizon 산술 + A2 만료 판정 (순수 primitive).

ADR-039 §8.1 A1/A2. **계산기만** 다룬다 — registry·sweeper 배선은 범위 밖이다.

⛔ 한때 여기 "strict cache / A6 3-state verifier / A5 single-flight" 를 현재 계획처럼 적었는데
그 설계는 **ADR-040 리셋에서 폐기**됐다.

⚠️ **이 파일이 닫지 않는 §8.1 G 행** (귀속을 흐리지 않기 위해 먼저 적는다):

- `[server] wall clock 역행에도 strict horizon 불변` — harness (1)이 못 박은 대로
  `verified_at_monotonic`의 **저장·재사용 경로**가 있어야 닫힌다. 여기서 증명되는 것은
  계산기가 wall을 **입력으로 갖지 않는다**는 구조적 사실뿐이다.
- `[server] stale fallback으로 연장 안 됨` — `verify_premium_status`가 최대 1시간 stale
  캐시로 ACTIVE를 돌려주고(`app/subscription.py`의 `CACHE_STALE_TTL`) 반환 타입에
  fresh/stale 구분이 없다. 그 구분은 A6 3-state verifier가 만든다.
- `[server] single-flight` / `[server] identity horizon이 토큰 단위` /
  `[server] horizon 계산(캐시 4분 → lease ~11분)`의 **저장 절반**.
  ⚠️ **모두 후속은 아니다** — identity horizon 의 관측·저장·재사용은 `app/topic_dispatcher.py`
  에 land 했고 e2e 가 잠근다. 남은 것은 **관측 캐시와 single-flight** 이고, 그건 실제 병목을
  측정하기 전에는 넣지 않는다(ADR-040).

⚠️ `CACHE_TTL < LEASE_MAX_SECONDS`가 **"최소 lease 10분"을 뜻하지 않는다**: 그 부등식은
3-way min의 *premium(RevenueCat) 항*에만 걸리는 상한이다. 10분 하한은
`firebase_identity_verified_at ≈ now`라는 E2 전제가 함께 있을 때만 성립하며, 그 전제가
깨진 경우(stale identity)는 아래 `TestStaleIdentity`가 **10분 미만·이미 만료**로 실증한다.

## 관측 설계

1. **기대값은 리터럴로 적는다.** `LEASE_MAX_SECONDS - 240`처럼 상수에서 유도하면
   `900 → 600` mutation이 기대값과 실제값을 **동시에** 움직여 전 테스트가 survivor가 된다.
   상수 자체는 `TestLeaseConstant`가 절대값으로 pin한다.
2. **float 경계는 `math.nextafter`로 집는다.** "만료 직전"을 `-0.001`로 쓰면 `>=` → `>`
   mutation을 잡지 못할 수 있는 여유가 생긴다.
3. **부정 단언에는 positive control**(`tests/test_subscription_clock.py` 규약 계승).
"""
import inspect
import math
import unittest
from datetime import datetime, timezone

from app.clock import Clock
from app.subscription import CACHE_TTL
from app import topic_lease as _lease_mod
from app.topic_lease import (
    LEASE_MAX_SECONDS,
    MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS,
    compute_lease_expiry,
    is_expired,
)

NOW = 1000.0            # monotonic 축의 임의 기준점 (binary64에서 정확)
MINUTE = 60.0


class _Recording:
    """호출 횟수를 세는 결정적 fake — 마지막 값을 이후 호출에서 재사용한다."""

    def __init__(self, *values):
        self._values = list(values)
        self.calls = 0

    def __call__(self):
        value = self._values[min(self.calls, len(self._values) - 1)]
        self.calls += 1
        return value


def _clock(*, wall, mono):
    """`Clock` 조립 단일 지점 — 두 축을 **독립적으로** 제어한다.

    (`tests/test_subscription_clock.py`의 `_clock`과 별개다. 그쪽은 wall 축 전용이고
     mono에 poison을 넣어 "subscription은 mono를 읽지 않는다"를 잠근다.)
    """
    return Clock(wall=_Recording(*wall), mono=_Recording(*mono))


class TestLeaseConstant(unittest.TestCase):
    def test_lease_max_is_pinned_to_15_minutes(self):
        """제품 결정 S5(15분)의 유일한 red. 이게 없으면 상수 변경이 전부 survivor다."""
        self.assertEqual(LEASE_MAX_SECONDS, 900.0)

    def test_future_skew_tolerance_derives_from_lease_cap(self):
        """`now + LEASE`를 넘는 입력은 출력상 상한과 **구별 불가**하므로 거부해도 손실이 없다."""
        self.assertEqual(MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS, LEASE_MAX_SECONDS)

    def test_cache_ttl_is_below_lease_cap(self):
        """A1 불변식. 어기면 lease가 0으로 수렴해 재인증 storm이 된다.

        단위가 다르므로(`timedelta` ⊥ `float`) 변환을 명시한다.
        """
        self.assertLess(CACHE_TTL.total_seconds(), LEASE_MAX_SECONDS)

    def test_premium_term_alone_cannot_shrink_lease_below_10_minutes(self):
        """위 부등식이 **premium(RevenueCat) 항에만** 거는 상한임을 계산기로 실증.

        ⚠️ 이름 주의 — KRX **entitlement**(우리 DB row)는 `compute_gated_lease_expiry` 의
        **별 축**이다. 둘 다 "entitlement" 라 부르면 어느 권위의 상한인지 구분되지 않는다.

        identity가 신선(E2 전제)하면 entitlement가 TTL 끝까지 늙어도 lease ≥ 600s.
        """
        expiry = compute_lease_expiry(
            now_mono=NOW,
            premium_verified_at_mono=NOW - CACHE_TTL.total_seconds(),
            firebase_identity_verified_at_mono=NOW,
        )
        self.assertGreaterEqual(expiry - NOW, 600.0)


class TestHorizonBinding(unittest.TestCase):
    """3-way min에서 **어느 항이 결과를 잡는가**를 항목별로 고정한다."""

    def test_now_binds_when_both_verified_at_are_current(self):
        """E2 직후(둘 다 방금 확인) → 상한 전량."""
        expiry = compute_lease_expiry(
            now_mono=NOW, premium_verified_at_mono=NOW, firebase_identity_verified_at_mono=NOW,
        )
        self.assertEqual(expiry, NOW + 900.0)

    def test_premium_binds(self):
        """캐시 4분 → 약 11분(660s). A1 본문의 예시 그대로."""
        expiry = compute_lease_expiry(
            now_mono=NOW,
            premium_verified_at_mono=NOW - 4 * MINUTE,
            firebase_identity_verified_at_mono=NOW,
        )
        self.assertEqual(expiry - NOW, 660.0)

    def test_identity_binds(self):
        expiry = compute_lease_expiry(
            now_mono=NOW,
            premium_verified_at_mono=NOW,
            firebase_identity_verified_at_mono=NOW - 6 * MINUTE,
        )
        self.assertEqual(expiry - NOW, 540.0)

    def test_older_of_the_two_binds(self):
        """둘 다 stale이면 **더 오래된 쪽**이 잡는다(min이지 평균·최신이 아니다)."""
        expiry = compute_lease_expiry(
            now_mono=NOW,
            premium_verified_at_mono=NOW - 3 * MINUTE,
            firebase_identity_verified_at_mono=NOW - 7 * MINUTE,
        )
        self.assertEqual(expiry - NOW, 480.0)

    def test_matches_canonical_three_term_formula(self):
        """A1 원식 `min(a+L, b+L, c+L)`과 축약형 `min(a,b,c)+L`의 동등성을 잠근다.

        IEEE754 덧셈이 단조라 bit-for-bit 같지만, 그건 **세 항의 horizon 상수가 동일할
        때만** 성립한다 — 항별 horizon이 갈리면 축약형은 조용히 틀린 모양이 된다.
        """
        cases = [
            (NOW, NOW, NOW),
            (NOW, NOW - 4 * MINUTE, NOW),
            (NOW, NOW - 7 * MINUTE, NOW - 3 * MINUTE),
            (NOW, NOW - 3600.0, NOW - 0.5),
            (0.0, -1.0, -2.0),
            (1e9, 1e9 - 1e-6, 1e9 - 1e-7),
        ]
        for now, premium, identity in cases:
            with self.subTest(now=now, premium=premium, identity=identity):
                canonical = min(
                    now + LEASE_MAX_SECONDS,
                    premium + LEASE_MAX_SECONDS,
                    identity + LEASE_MAX_SECONDS,
                )
                self.assertEqual(
                    compute_lease_expiry(
                        now_mono=now,
                        premium_verified_at_mono=premium,
                        firebase_identity_verified_at_mono=identity,
                    ),
                    canonical,
                )

    def test_negative_monotonic_values_are_accepted(self):
        """`time.monotonic()`의 기준점은 정의되지 않는다 — 음수를 배제할 근거가 없다."""
        expiry = compute_lease_expiry(
            now_mono=-5000.0,
            premium_verified_at_mono=-5060.0,
            firebase_identity_verified_at_mono=-5000.0,
        )
        self.assertEqual(expiry, -5060.0 + 900.0)


class TestFutureVerifiedAt(unittest.TestCase):
    """미래 관측 시각은 **상한을 넘기지 못한다**. 단 gross skew는 축 혼동이라 거부한다."""

    def test_future_verified_at_is_clamped_to_now_plus_lease(self):
        """⚠️ **양쪽 모두** 미래여야 한다.

        한쪽만 미래로 두면 나머지 항이 `now`와 같아 clamp 역할을 대신하므로,
        `min`에서 `now` 항을 삭제한 구현도 통과한다(가짜 통과).
        """
        expiry = compute_lease_expiry(
            now_mono=NOW,
            premium_verified_at_mono=NOW + 60.0,
            firebase_identity_verified_at_mono=NOW + 60.0,
        )
        self.assertEqual(expiry, NOW + 900.0)
        self.assertNotEqual(expiry, NOW + 960.0, "미래 관측이 lease를 늘려선 안 된다")

    def test_skew_exactly_at_tolerance_is_accepted(self):
        expiry = compute_lease_expiry(
            now_mono=NOW,
            premium_verified_at_mono=NOW + MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS,
            firebase_identity_verified_at_mono=NOW,
        )
        self.assertEqual(expiry, NOW + 900.0)

    def test_premium_gross_future_skew_is_rejected(self):
        just_over = math.nextafter(NOW + MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS, math.inf)
        with self.assertRaises(ValueError):
            compute_lease_expiry(
                now_mono=NOW,
                premium_verified_at_mono=just_over,
                firebase_identity_verified_at_mono=NOW,
            )

    def test_identity_gross_future_skew_is_rejected(self):
        """항별로 따로 본다 — premium만 검사하는 구현을 잡는다."""
        just_over = math.nextafter(NOW + MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS, math.inf)
        with self.assertRaises(ValueError):
            compute_lease_expiry(
                now_mono=NOW,
                premium_verified_at_mono=NOW,
                firebase_identity_verified_at_mono=just_over,
            )

    def test_wall_epoch_leaking_into_one_term_is_rejected(self):
        """축 혼동의 현실적 형태 — `time.time()` 값이 한 항에 섞이는 경우.

        가드가 없으면 `min`이 `now`를 골라 **항상 상한 전량** lease가 나가고
        A1의 "총 revoke 상한 15분" 증명이 조용히 무효가 된다.
        """
        with self.assertRaises(ValueError):
            compute_lease_expiry(
                now_mono=NOW,
                premium_verified_at_mono=1753000000.0,   # wall epoch 초
                firebase_identity_verified_at_mono=NOW,
            )


class TestNonFiniteRejected(unittest.TestCase):
    """비유한 입력은 `ValueError` — `inf` sentinel이 영구 lease가 되는 경로를 입구에서 막는다.

    `min`은 NaN을 **첫 인자일 때만** 전파한다(`min(1.0, nan, 2.0) == 1.0`) → 가드가 없으면
    NaN horizon이 조용히 무시되고 상한 전량이 나간다. 리포 관례는 fail-closed
    (`app/atomic_retry.py`의 `math.isfinite` → `ValueError`).
    """

    def test_each_position_rejects_each_non_finite(self):
        for bad in (math.nan, math.inf, -math.inf):
            for position in ("now_mono", "premium_verified_at_mono",
                             "firebase_identity_verified_at_mono"):
                kwargs = {
                    "now_mono": NOW,
                    "premium_verified_at_mono": NOW,
                    "firebase_identity_verified_at_mono": NOW,
                    position: bad,
                }
                with self.subTest(bad=bad, position=position):
                    with self.assertRaises(ValueError):
                        compute_lease_expiry(**kwargs)

    def test_all_finite_is_accepted(self):
        """positive control — 항상 raise하는 구현과 구별한다.

        입력을 `TestHorizonBinding`과 **다르게** 잡아(둘 다 stale, 음수 축) 중복 대신
        판별력을 더한다.
        """
        self.assertEqual(
            compute_lease_expiry(
                now_mono=-2000.0,
                premium_verified_at_mono=-2030.0,
                firebase_identity_verified_at_mono=-2010.0,
            ),
            -2030.0 + 900.0,
        )


class TestIsExpired(unittest.TestCase):
    """A2: 만료 판정은 **`now >= expires_at`**(경계 포함 = fail-closed)."""

    def test_boundary_is_expired(self):
        self.assertTrue(is_expired(now_mono=NOW, expires_at_mono=NOW))

    def test_just_before_boundary_is_alive(self):
        """`>=` → `>` mutation을 잡는 것은 위 경계 케이스이고, 이건 그 positive control이다."""
        just_before = math.nextafter(NOW, -math.inf)
        self.assertFalse(is_expired(now_mono=just_before, expires_at_mono=NOW))

    def test_after_boundary_is_expired(self):
        self.assertTrue(is_expired(now_mono=math.nextafter(NOW, math.inf), expires_at_mono=NOW))

    def test_non_finite_is_expired(self):
        """비유한 입력 = 판정 불가 → 만료로 접는다.

        ⚠️ 이 단언이 `is_expired`의 isfinite 선검사를 지키는 **유일한** red다. 유한 구간에서는
        `not (now < exp)`와 `now >= exp`가 동치라 경계 테스트로는 구별되지 않는다.
        특히 `not (now < exp)` 형태는 `now = -inf`·`exp = +inf`에서 **fail-open**한다.
        """
        for now, expires in (
            (math.nan, NOW), (NOW, math.nan),
            (-math.inf, NOW), (math.inf, NOW),
            (NOW, math.inf), (NOW, -math.inf),
        ):
            with self.subTest(now=now, expires=expires):
                self.assertTrue(is_expired(now_mono=now, expires_at_mono=expires))


class TestStaleIdentity(unittest.TestCase):
    """E2 전제가 깨진 경우 — "최소 10분"이 성립하지 않음을 실증."""

    def test_six_minute_stale_identity_yields_less_than_ten_minutes(self):
        expiry = compute_lease_expiry(
            now_mono=NOW,
            premium_verified_at_mono=NOW,
            firebase_identity_verified_at_mono=NOW - 6 * MINUTE,
        )
        self.assertEqual(expiry - NOW, 540.0)
        self.assertLess(expiry - NOW, 600.0)
        self.assertFalse(is_expired(now_mono=NOW, expires_at_mono=expiry))

    def test_identity_older_than_lease_cap_is_already_expired(self):
        expiry = compute_lease_expiry(
            now_mono=NOW,
            premium_verified_at_mono=NOW,
            firebase_identity_verified_at_mono=NOW - 16 * MINUTE,
        )
        self.assertEqual(expiry - NOW, -60.0)
        self.assertTrue(is_expired(now_mono=NOW, expires_at_mono=expiry))


class TestDocumentedCallerPattern(unittest.TestCase):
    """호출부가 **따라야 할** 패턴의 실행 가능한 예시: 요청당 `clock.mono()` 1회 → 전 topic 공유.

    ⚠️ **이 클래스는 호출부 계약을 강제하지 못한다.** 이 슬라이스에 `compute_lease_expiry`의
    프로덕션 소비자가 0개이므로, 훗날 WS 배선이 topic마다 시각을 다시 읽어도 여기는 계속
    green이다 — 패턴을 테스트 코드가 스스로 만들기 때문이다. 실제 강제는 ack/registry
    호출부를 구동하는 **배선 슬라이스의 통합 테스트** 몫이다(G `[server] active_subscriptions가
    lock 아래 단일 snapshot` 행).

    그래서 여기서 실제로 잠기는 것만 적으면:
    1. 문서화된 패턴을 따르면 한 요청의 전 topic이 **같은 expiry**를 받고 값이 정확하다.
    2. lease 경로가 **wall 축을 건드리지 않는다**(wall recorder 호출 0회).
    3. 계산기가 `clock`을 받지 않는다는 **시그니처 결정**(아래 전용 테스트).

    2번을 근거로 G의 `wall clock 역행에도 strict horizon 불변` 행을 닫지 말 것 —
    계산기가 wall을 입력으로 갖지 않는다는 구조적 사실일 뿐이다(파일 docstring 참조).
    """

    def test_calculator_takes_no_clock(self):
        """D1/D2 결정(계산기는 시각을 스스로 만들지 않는다)을 **명시적으로** 잠근다.

        이게 없으면 "clock 주입으로 되돌리기"는 다른 테스트들의 `TypeError`로만 드러나
        *우연히* 잡힌다 — 증상만 보이고 원인은 안 보인다. 되돌리려면 이 테스트를 먼저
        지워야 하고, 그러면 결정을 뒤집었다는 사실이 diff에 남는다.
        """
        for func, expected in (
            (compute_lease_expiry,
             ["now_mono", "premium_verified_at_mono", "firebase_identity_verified_at_mono"]),
            (is_expired, ["now_mono", "expires_at_mono"]),
        ):
            with self.subTest(func=func.__name__):
                params = inspect.signature(func).parameters
                self.assertEqual(list(params), expected, "시각은 값으로 주입한다(§8.1 D1/D2)")
                self.assertTrue(
                    all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values()),
                    "전부 keyword-only — 위치 인자로 두 시각을 뒤바꾸는 사고를 막는다",
                )

    def test_documented_pattern_gives_every_topic_the_same_expiry(self):
        clock = _clock(
            wall=[datetime(2026, 1, 1, tzinfo=timezone.utc)],
            mono=[NOW, NOW + 5.0, NOW + 9.0],       # 두 번째부터 전진 — 재읽기 시 값이 갈린다
        )
        now = clock.mono()                          # 요청 경계에서 **1회**
        expiries = [
            compute_lease_expiry(
                now_mono=now,
                premium_verified_at_mono=now - 4 * MINUTE,
                firebase_identity_verified_at_mono=now,
            )
            for _ in range(3)                       # 한 ack의 topic 3개
        ]
        self.assertEqual(len(set(expiries)), 1, "한 요청의 topic들은 같은 expiry를 받는다")
        # 값까지 고정한다 — 집합 크기만 보면 "상수를 돌려주는" 구현도 통과한다.
        self.assertEqual(expiries[0], NOW + 660.0)
        self.assertEqual(clock.mono.calls, 1)
        self.assertEqual(clock.wall.calls, 0, "lease 경로는 wall 축을 읽지 않는다")

    def test_wall_movement_does_not_change_the_result(self):
        """wall을 역행·전진시켜도 같은 mono snapshot이면 결과가 동일하다."""
        clock = _clock(
            wall=[
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                datetime(2025, 1, 1, tzinfo=timezone.utc),   # 역행
                datetime(2027, 1, 1, tzinfo=timezone.utc),   # 전진
            ],
            mono=[NOW],
        )
        results = []
        for _ in range(3):
            clock.wall()                            # wall이 흔들리는 상황을 재현
            results.append(
                compute_lease_expiry(
                    now_mono=clock.mono(),
                    premium_verified_at_mono=NOW - 4 * MINUTE,
                    firebase_identity_verified_at_mono=NOW,
                )
            )
        self.assertEqual(results, [NOW + 660.0] * 3)
        self.assertEqual(clock.wall.calls, 3, "wall은 테스트가 직접 움직였다(positive control)")


class TestGatedLeaseFourAxes(unittest.TestCase):
    """⛔ 4축 API 를 **테스트 0건**으로 추가했었다(codex High). 기존 스위트는 3축만 본다.

    KRX **entitlement** 는 우리 DB row 라 RevenueCat premium 과 **다른 권위**다 — 독립적인
    철회 축이므로 자기 horizon 을 가져야 한다.
    """

    NOW = 10_000.0

    def _expiry(self, *, identity=None, premium=None, entitlement=None):
        return _lease_mod.compute_gated_lease_expiry(
            now_mono=self.NOW,
            identity_verified_at_mono=self.NOW if identity is None else identity,
            premium_verified_at_mono=self.NOW if premium is None else premium,
            entitlement_verified_at_mono=self.NOW if entitlement is None else entitlement,
        )

    def test_each_axis_binds_independently(self):
        """⛔ 축마다 **단독으로** 최솟값이 될 때 결과가 그 축을 따라야 한다 — 한 축을 계산에서
        빼는 변이는 그 축의 케이스에서만 red 가 된다."""
        for name, kwargs, oldest in (
            ("identity", {"identity": self.NOW - 300}, self.NOW - 300),
            ("premium", {"premium": self.NOW - 200}, self.NOW - 200),
            ("entitlement", {"entitlement": self.NOW - 100}, self.NOW - 100),
        ):
            with self.subTest(axis=name):
                self.assertEqual(self._expiry(**kwargs),
                                 oldest + _lease_mod.LEASE_MAX_SECONDS)

    def test_the_oldest_axis_wins_when_several_are_stale(self):
        self.assertEqual(
            self._expiry(identity=self.NOW - 50, premium=self.NOW - 400,
                         entitlement=self.NOW - 120),
            self.NOW - 400 + _lease_mod.LEASE_MAX_SECONDS,
        )

    def test_entitlement_axis_is_not_dominated_away(self):
        """⛔ "DB 조회가 RC 뒤라 entitlement 는 항상 지배당한다"는 **런타임 순서 논증**이다 —
        조회에 캐시가 붙으면 entitlement 관측이 premium 보다 **과거**가 된다. 그때 축이 없으면
        lease 가 entitlement 지평을 넘는다(= revoke 상한 초과)."""
        cached = self.NOW - 600                     # 캐시 히트: RC 보다 훨씬 과거
        self.assertEqual(
            self._expiry(premium=self.NOW - 10, entitlement=cached),
            cached + _lease_mod.LEASE_MAX_SECONDS,
            "entitlement 축이 계산에서 빠졌다 — 캐시가 붙는 순간 상한이 깨진다",
        )

    def test_every_axis_rejects_non_finite_and_future_skew(self):
        for axis in ("identity", "premium", "entitlement"):
            for bad in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(axis=axis, value=bad), self.assertRaises(ValueError):
                    self._expiry(**{axis: bad})
            with self.subTest(axis=axis, value="future"), self.assertRaises(ValueError):
                self._expiry(**{axis: self.NOW
                                + _lease_mod.MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS + 1})

    def test_empty_observations_is_refused(self):
        """⛔ 관측이 하나도 없으면 lease 근거가 없다. 방치하면 `min(float)` 이
        `TypeError: 'float' object is not iterable` 로 죽는다 — fail-open 은 아니지만
        (값을 돌려주지 않는다) 호출부 실수를 **쓸모없는 메시지**로 알린다."""
        with self.assertRaises(ValueError):
            _lease_mod._lease_expiry_from_observations(now_mono=self.NOW, observations={})

    def test_public_signatures_are_keyword_only_and_distinct(self):
        """⛔ 서명을 **정확한 집합**으로 잠근다 — denylist 면 새 인자가 통과한다.
        identity-only 가 premium 관측을 **요구하지 않는** 것도 여기서 잠긴다."""
        for fn, expected in (
            (_lease_mod.compute_identity_only_lease_expiry,
             {"now_mono", "identity_verified_at_mono"}),
            (_lease_mod.compute_lease_expiry,
             {"now_mono", "premium_verified_at_mono", "firebase_identity_verified_at_mono"}),
            (_lease_mod.compute_gated_lease_expiry,
             {"now_mono", "identity_verified_at_mono", "premium_verified_at_mono",
              "entitlement_verified_at_mono"}),
        ):
            params = inspect.signature(fn).parameters
            with self.subTest(fn=fn.__name__):
                self.assertEqual(set(params), expected)
                self.assertTrue(
                    all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values()),
                    "위치 인자를 허용하면 축이 조용히 뒤바뀔 수 있다",
                )


if __name__ == "__main__":
    unittest.main()
