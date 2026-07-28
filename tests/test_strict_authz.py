"""`app/strict_authz.py` — 관측/verdict 2계층 + 순수 파생 (ADR-039 §8.1 A6-1/A6-2).

## 이 파일이 잠그는 것

1. **verdict는 저장물이 아니다** — 같은 authority record에서 revoked 구 토큰과 새 토큰이
   **서로 다른 verdict**를 얻는다(A1이 요구하는 성질. UID 캐시의 권한 혼입을 막는 구조).
2. **`iat` × ms/초 단위** — `auth_time` 대체 금지, `iat * 1000` 축 맞춤, strict `<`.
3. **disabled가 revoked보다 먼저**(SDK와 동일), **identity가 premium보다 먼저**(§8.1 D5).
4. **신선도를 우회할 수 없다** — 낡거나 없는 관측은 `NeedsVerification`.
5. `NeedsVerification` ≠ 판정 불가 — "아직 안 물어봤다"와 "물어봤는데 답을 못 얻었다"는 다르다.

## 이 파일이 잠그지 **않는** 것

저장소·epoch CAS·single-flight·lock·실제 verifier 배선은 후속 슬라이스다. 따라서 §8.1 G의
`[server] wall clock 역행에도 strict horizon 불변` / `stale fallback으로 연장 안 됨` 행은
**여전히 열려 있다** — 여기서 증명되는 건 순수 파생 규칙뿐이다.
"""
import math
import unittest

from app.strict_authz import (
    IDENTITY_NEGATIVE_RECHECK_SECONDS,
    PREMIUM_INACTIVE_RECHECK_SECONDS,
    Active,
    Concern,
    IdentityAuthorityFound,
    IdentityAuthorityNotFound,
    Inactive,
    InactiveReason,
    NeedsVerification,
    PremiumObservation,
    derive_verdict,
    identity_observation_is_fresh,
    premium_observation_is_fresh,
)
from app.strict_authz import MAX_OBSERVATION_FUTURE_SKEW_SECONDS
from app.topic_lease import LEASE_MAX_SECONDS

UID = "uid-alice"
NOW = 1000.0
IAT = 1_700_000_000          # 초 epoch
WATERMARK_BEFORE = (IAT - 60) * 1000   # 토큰 발급 **전**에 revoke → 이 토큰은 유효
WATERMARK_AFTER = (IAT + 60) * 1000    # 토큰 발급 **후**에 revoke → 이 토큰은 revoked


def _identity(**kw):
    base = dict(uid=UID, disabled=False, tokens_valid_after_ms=WATERMARK_BEFORE,
                verified_at_mono=NOW, epoch=1)
    base.update(kw)
    return IdentityAuthorityFound(**base)


def _premium(**kw):
    base = dict(uid=UID, active=True, verified_at_mono=NOW, epoch=1)
    base.update(kw)
    return PremiumObservation(**base)


def _not_found(**kw):
    base = dict(uid=UID, verified_at_mono=NOW, epoch=1)
    base.update(kw)
    return IdentityAuthorityNotFound(**base)


def _derive(**kw):
    base = dict(uid=UID, now_mono=NOW, premium=_premium(), identity=_identity(), token_iat_seconds=IAT)
    base.update(kw)
    return derive_verdict(**base)


class TestHappyPath(unittest.TestCase):
    def test_active_carries_both_verified_at(self):
        """A1 3-way horizon의 두 인자가 정확히 이 두 필드다."""
        v = _derive(
            premium=_premium(verified_at_mono=NOW - 10.0),
            identity=_identity(verified_at_mono=NOW - 20.0),
        )
        self.assertEqual(v, Active(premium_verified_at_mono=NOW - 10.0, identity_verified_at_mono=NOW - 20.0))

    def test_watermark_equal_to_iat_is_not_revoked(self):
        """strict `<` — 같은 값이면 revoked 아니다(SDK와 동일)."""
        self.assertIsInstance(_derive(identity=_identity(tokens_valid_after_ms=IAT * 1000)), Active)


class TestUnitAxis(unittest.TestCase):
    """watermark(ms) × `iat`(초) 축 맞춤 — 이걸 틀리면 **모든 토큰이 revoked**가 된다."""

    def test_ms_and_seconds_are_reconciled(self):
        """`iat * 1000` 없이 그냥 비교하면 1.7e9 < 1.7e12라 전부 revoked로 오판된다."""
        self.assertIsInstance(_derive(identity=_identity(tokens_valid_after_ms=WATERMARK_BEFORE)), Active)

    def test_revoke_after_issuance_is_detected(self):
        v = _derive(identity=_identity(tokens_valid_after_ms=WATERMARK_AFTER))
        self.assertEqual(v, Inactive(reason=InactiveReason.TOKEN_REVOKED))

    def test_one_second_granularity_around_boundary(self):
        """경계 1초 전후가 정확히 갈린다 — 곱셈 계수가 틀리면 여기서 깨진다."""
        just_after = (IAT + 1) * 1000
        just_before = (IAT - 1) * 1000
        self.assertEqual(_derive(identity=_identity(tokens_valid_after_ms=just_after)).reason,
                         InactiveReason.TOKEN_REVOKED)
        self.assertIsInstance(_derive(identity=_identity(tokens_valid_after_ms=just_before)), Active)


class TestVerdictIsDerivedNotStored(unittest.TestCase):
    """§8.1 A1의 핵심 — **같은 record에서 토큰마다 다른 verdict**가 나와야 한다.

    verdict를 UID 단위로 저장했다면 revoked 구 토큰이 새 토큰의 `Active`를 상속했을 것이다.
    """

    def test_same_record_yields_different_verdicts_per_token(self):
        record = _identity(tokens_valid_after_ms=IAT * 1000)   # IAT 시점에 revoke
        old_token = _derive(identity=record, token_iat_seconds=IAT - 100)   # 그 전에 발급 → revoked
        new_token = _derive(identity=record, token_iat_seconds=IAT + 100)   # 그 후에 발급 → 유효
        self.assertEqual(old_token, Inactive(reason=InactiveReason.TOKEN_REVOKED))
        self.assertIsInstance(new_token, Active)


class TestDecisionOrder(unittest.TestCase):
    """순서가 곧 계약이다 — 어느 reason이 나오는지가 wire 오류코드를 정한다."""

    def test_deleted_beats_everything(self):
        v = _derive(identity=_not_found(),
                    premium=_premium(active=False))
        self.assertEqual(v, Inactive(reason=InactiveReason.ACCOUNT_DELETED))

    def test_disabled_beats_revoked(self):
        """SDK가 disabled를 먼저 검사한다 — 둘 다 해당하면 disabled가 이긴다."""
        v = _derive(identity=_identity(disabled=True, tokens_valid_after_ms=WATERMARK_AFTER))
        self.assertEqual(v, Inactive(reason=InactiveReason.ACCOUNT_DISABLED))

    def test_identity_beats_premium(self):
        """§8.1 D5 — 인증이 인가보다 앞선다."""
        v = _derive(identity=_identity(tokens_valid_after_ms=WATERMARK_AFTER), premium=_premium(active=False))
        self.assertEqual(v, Inactive(reason=InactiveReason.TOKEN_REVOKED))

    def test_premium_inactive_when_identity_ok(self):
        self.assertEqual(_derive(premium=_premium(active=False)),
                         Inactive(reason=InactiveReason.PREMIUM_INACTIVE))


class TestFreshnessCannotBeBypassed(unittest.TestCase):
    """낡거나 없는 관측은 **verdict를 만들지 못한다** — 호출부 약속이 아니라 구조로 강제."""

    def test_missing_observation_needs_verification(self):
        self.assertEqual(_derive(identity=None), NeedsVerification(concern=Concern.IDENTITY))
        self.assertEqual(_derive(premium=None), NeedsVerification(concern=Concern.PREMIUM))

    def test_identity_checked_before_premium_even_when_both_missing(self):
        self.assertEqual(_derive(identity=None, premium=None), NeedsVerification(concern=Concern.IDENTITY))

    def test_stale_positive_premium_needs_verification(self):
        """양성 관측 상한은 lease 상한 — 그보다 오래되면 어차피 만료된 lease밖에 못 준다."""
        stale = _premium(verified_at_mono=NOW - LEASE_MAX_SECONDS)
        self.assertEqual(_derive(premium=stale), NeedsVerification(concern=Concern.PREMIUM))

    def test_stale_negative_premium_needs_verification(self):
        """결제한 사용자가 webhook 유실로 **영구 거부**되지 않게 하는 상한."""
        stale = _premium(active=False, verified_at_mono=NOW - PREMIUM_INACTIVE_RECHECK_SECONDS)
        self.assertEqual(_derive(premium=stale), NeedsVerification(concern=Concern.PREMIUM))

    def test_negative_premium_within_horizon_still_rejects(self):
        """positive control — 신선한 음성 관측은 여전히 거부한다(전부 재검증으로 도망가면 안 된다)."""
        fresh = _premium(active=False, verified_at_mono=NOW - PREMIUM_INACTIVE_RECHECK_SECONDS + 1)
        self.assertEqual(_derive(premium=fresh), Inactive(reason=InactiveReason.PREMIUM_INACTIVE))

    def test_stale_disabled_identity_needs_verification(self):
        """`disabled`는 monotone이 아니다 — 재활성화된 계정이 영구 거부되면 안 된다."""
        stale = _identity(disabled=True, verified_at_mono=NOW - IDENTITY_NEGATIVE_RECHECK_SECONDS)
        self.assertEqual(_derive(identity=stale), NeedsVerification(concern=Concern.IDENTITY))

    def test_stale_not_found_identity_needs_verification(self):
        """uid는 재생성될 수 있다."""
        stale = _not_found(verified_at_mono=NOW - IDENTITY_NEGATIVE_RECHECK_SECONDS)
        self.assertEqual(_derive(identity=stale), NeedsVerification(concern=Concern.IDENTITY))


class TestFreshnessPredicates(unittest.TestCase):
    """경계 규약은 `topic_lease.is_expired`와 대칭 — **경계는 stale**이다."""

    def test_positive_premium_boundary_is_stale(self):
        at = _premium(verified_at_mono=NOW - LEASE_MAX_SECONDS)
        just_inside = _premium(verified_at_mono=NOW - LEASE_MAX_SECONDS + 1e-9)
        self.assertFalse(premium_observation_is_fresh(at, now_mono=NOW))
        self.assertTrue(premium_observation_is_fresh(just_inside, now_mono=NOW))

    def test_negative_premium_uses_its_own_constant(self):
        """양성·음성이 같은 상한을 쓰면 이 단언이 깨진다."""
        obs = _premium(active=False, verified_at_mono=NOW - PREMIUM_INACTIVE_RECHECK_SECONDS - 1)
        self.assertFalse(premium_observation_is_fresh(obs, now_mono=NOW))
        still_ok_if_positive = _premium(active=True, verified_at_mono=NOW - PREMIUM_INACTIVE_RECHECK_SECONDS - 1)
        self.assertTrue(premium_observation_is_fresh(still_ok_if_positive, now_mono=NOW))

    def test_identity_positive_vs_negative_horizons_differ(self):
        old = NOW - IDENTITY_NEGATIVE_RECHECK_SECONDS - 1
        self.assertTrue(identity_observation_is_fresh(_identity(verified_at_mono=old), now_mono=NOW))
        self.assertFalse(identity_observation_is_fresh(_identity(disabled=True, verified_at_mono=old), now_mono=NOW))

    def test_non_finite_raises(self):
        """비유한 입력은 **계약 위반**이므로 verdict로 접지 않고 전파한다(A6-1).

        ⚠️ `topic_lease.is_expired`는 같은 상황에서 만료로 접는다 — 그건 B1 전송 직전 hot path라
        예외가 더 나쁘기 때문이다. 여기는 subscribe당 1회 파생 경로라 시끄러운 편이 낫다.
        """
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    premium_observation_is_fresh(_premium(verified_at_mono=bad), now_mono=NOW)
                with self.assertRaises(ValueError):
                    premium_observation_is_fresh(_premium(), now_mono=bad)


class TestAxisViolationIsNotSilent(unittest.TestCase):
    """⚠️ **음성 관측의 무성(無聲) 영구 거부**를 막는다.

    wall epoch(~1.7e9)가 `verified_at_mono`로 새면 `now(~1e5) < 1.7e9 + 300`이 항상 참이라
    음성 관측이 **영구 fresh**가 되고, 재검증 상한이 통째로 우회돼 정상 사용자가 영원히
    거부된다. 실측(수정 전):

        premium 음성 fresh?   True
        identity disabled fresh? True
        identity notfound fresh? True
        → verdict: Inactive(premium_inactive)   ← 300초가 지나도 재검증되지 않음

    ⚠️ `topic_lease.compute_lease_expiry`의 gross-skew 가드는 **이 경로를 보호하지 못한다** —
    음성 verdict는 lease 계산을 아예 거치지 않는다. 같은 위험을 한쪽에만 걸어 둔 비대칭이었다.
    """

    WALL_EPOCH = 1_753_000_000.0

    def test_future_negative_premium_raises(self):
        with self.assertRaises(ValueError):
            premium_observation_is_fresh(
                _premium(active=False, verified_at_mono=self.WALL_EPOCH), now_mono=NOW
            )

    def test_future_disabled_identity_raises(self):
        with self.assertRaises(ValueError):
            identity_observation_is_fresh(
                _identity(disabled=True, verified_at_mono=self.WALL_EPOCH), now_mono=NOW
            )

    def test_future_not_found_identity_raises(self):
        with self.assertRaises(ValueError):
            identity_observation_is_fresh(
                _not_found(verified_at_mono=self.WALL_EPOCH), now_mono=NOW
            )

    def test_derive_verdict_propagates_instead_of_denying_forever(self):
        """파생 경로에서도 삼키지 않는다 — 조용한 `Inactive` 대신 예외."""
        with self.assertRaises(ValueError):
            _derive(premium=_premium(active=False, verified_at_mono=self.WALL_EPOCH))

    def test_future_positive_observation_also_raises(self):
        """양성도 마찬가지다 — 축 위반은 polarity와 무관하다."""
        with self.assertRaises(ValueError):
            premium_observation_is_fresh(_premium(verified_at_mono=self.WALL_EPOCH), now_mono=NOW)

    def test_small_future_skew_is_tolerated(self):
        """positive control — 미세한 미래(샘플링 지터)까지 막으면 과차단이다."""
        self.assertTrue(premium_observation_is_fresh(
            _premium(verified_at_mono=NOW + MAX_OBSERVATION_FUTURE_SKEW_SECONDS), now_mono=NOW))

    def test_tolerance_band_is_small_because_it_is_additive(self):
        """⚠️ 허용 skew는 **각 horizon에 그대로 가산**되므로 작아야 한다.

        `topic_lease.MAX_VERIFIED_AT_FUTURE_SKEW_SECONDS`(=900)를 재사용했을 때 음성 관측 상한이
        문서값 300초가 아니라 **1200초**가 됐다(실측). clamp로도 안 고쳐진다 — 미래 구간에서
        `now < now + horizon`이 항상 참이라 창이 그대로 s+horizon이다.
        """
        self.assertLessEqual(MAX_OBSERVATION_FUTURE_SKEW_SECONDS, 5.0,
                             "가산되는 값이므로 horizon 대비 무시할 수준이어야")
        at = NOW + MAX_OBSERVATION_FUTURE_SKEW_SECONDS
        self.assertTrue(premium_observation_is_fresh(_premium(verified_at_mono=at), now_mono=NOW))
        with self.assertRaises(ValueError):
            premium_observation_is_fresh(
                _premium(verified_at_mono=math.nextafter(at, math.inf)), now_mono=NOW
            )


class TestConstants(unittest.TestCase):
    def test_pinned_values(self):
        """제품 결정 값 — 상수에서 유도한 기대값만 쓰면 변경이 전부 survivor가 된다."""
        self.assertEqual(PREMIUM_INACTIVE_RECHECK_SECONDS, 300.0)
        self.assertEqual(IDENTITY_NEGATIVE_RECHECK_SECONDS, 300.0)

    def test_constants_are_independent_literals(self):
        """값이 같아도 **파생 관계로 묶지 말 것** — 위험 프로파일이 다르다.

        한쪽을 바꿨을 때 다른 쪽이 조용히 따라 움직이면 안 된다. 소스에서 참조 형태를 직접 본다.
        """
        import inspect

        import app.strict_authz as m

        src = inspect.getsource(m)
        self.assertNotIn("IDENTITY_NEGATIVE_RECHECK_SECONDS = PREMIUM_INACTIVE_RECHECK_SECONDS", src)
        self.assertNotIn("PREMIUM_INACTIVE_RECHECK_SECONDS = IDENTITY_NEGATIVE_RECHECK_SECONDS", src)


class TestNeedsVerificationIsNotAWireVerdict(unittest.TestCase):
    def test_it_is_not_confused_with_inactive_or_active(self):
        """"아직 안 물어봤다"가 "권한 없음"이나 "판정 불가"로 새면 계약이 무너진다."""
        v = _derive(identity=None)
        self.assertIsInstance(v, NeedsVerification)
        self.assertNotIsInstance(v, (Active, Inactive))

    def test_it_names_the_concern_to_verify(self):
        self.assertEqual(_derive(premium=None).concern, Concern.PREMIUM)




class TestRevocationAxesAreValidated(unittest.TestCase):
    """⚠️ **감사에서 나온 fail-open 4종** — revoke 판정의 유일한 비교가 무방비였다.

    수정 전 실측(전부 `Active` = revoke 무력화):

        A 정상(revoked여야)  -> Inactive   ← 대조군만 정상
        B watermark를 초로   -> Active
        C iat를 ms로         -> Active
        D watermark NaN      -> Active
        E iat NaN            -> Active

    같은 함수의 시각 축(`_within`)은 이미 `ValueError`를 던지고 있었는데, 정작 **인증 취소를
    결정하는 비교**만 검증이 없었다. 게다가 그 결과 관측은 *양성*(found+enabled)이라 신선도가
    유지돼 재검증 트리거조차 없고, 재검증해도 같은 값이 다시 기록돼 **영구적**이었다.
    """

    REVOKED_MS = (IAT + 500) * 1000

    def test_watermark_in_seconds_raises(self):
        with self.assertRaises(ValueError):
            _derive(identity=_identity(tokens_valid_after_ms=self.REVOKED_MS // 1000))

    def test_iat_in_milliseconds_raises(self):
        with self.assertRaises(ValueError):
            _derive(token_iat_seconds=IAT * 1000)

    def test_non_int_operands_raise(self):
        with self.assertRaises(ValueError):
            _derive(token_iat_seconds=float(IAT))
        with self.assertRaises(ValueError):
            _identity(tokens_valid_after_ms=float(self.REVOKED_MS))

    def test_never_revoked_zero_is_allowed(self):
        """positive control — SDK는 `validSince` 부재 시 **0**을 준다. 막으면 안 된다."""
        self.assertIsInstance(_derive(identity=_identity(tokens_valid_after_ms=0)), Active)

    def test_correct_axes_still_work(self):
        """positive control — 정상 축은 그대로 통과·거부한다."""
        self.assertEqual(_derive(identity=_identity(tokens_valid_after_ms=self.REVOKED_MS)),
                         Inactive(reason=InactiveReason.TOKEN_REVOKED))


class TestSubjectBinding(unittest.TestCase):
    """관측이 **이 요청의 uid 것인지** 확인한다.

    없으면 A의 identity + B의 premium을 섞어 넣어도 `Active`가 나온다 — 무료 사용자가 타인의
    구독으로 인가된다. 이 계층은 신선도를 "호출부 약속으로는 강제할 수 없다"며 내재화했으면서,
    더 직접적인 **주체 결속**은 약속으로 남겨 뒀었다(검사할 필드조차 없었다).
    """

    def test_mixed_uid_raises(self):
        with self.assertRaises(ValueError):
            _derive(premium=_premium(uid="uid-bob"))
        with self.assertRaises(ValueError):
            _derive(identity=_identity(uid="uid-bob"))

    def test_matching_uid_passes(self):
        self.assertIsInstance(_derive(), Active)

    def test_empty_uid_rejected(self):
        with self.assertRaises(ValueError):
            _premium(uid="")
        with self.assertRaises(ValueError):
            _derive(uid="")


class TestConstructionSafety(unittest.TestCase):
    """`kw_only` — 위치 인자 생성이 **인접한 두 int**를 조용히 뒤바꿀 수 있었다.

    `IdentityAuthorityFound(False, watermark, now, epoch)`에서 watermark ↔ epoch을 바꾸면
    `iat*1000 < 7`이 항상 False라 **어떤 토큰도 revoked로 판정되지 않았다**(fail-open).
    프로덕션 소비자가 0인 지금이 **깨질 코드 없이 강제할 수 있는 유일한 시점**이었다.
    """

    def test_positional_construction_is_rejected(self):
        with self.assertRaises(TypeError):
            IdentityAuthorityFound(UID, False, 0, NOW, 1)
        with self.assertRaises(TypeError):
            PremiumObservation(UID, True, NOW, 1)

    def test_truthy_strings_are_rejected(self):
        """저장 슬라이스가 Redis hash를 쓰면 `"false"`가 참으로 접혀 비구독자가 통과한다."""
        for bad in ("false", "0", 1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _premium(active=bad)
                with self.assertRaises(ValueError):
                    _identity(disabled=bad)


class TestEpochIsInertHere(unittest.TestCase):
    """⚠️ `epoch`은 이 계층의 **어떤 판정에도 쓰이지 않는다** — 계약으로 못 박는다.

    필드가 관측에 실려 있어 "여기서 검사되겠거니" 오해하기 쉽다. 소비(lease 발급) 시점 fence는
    호출부/저장소 몫이므로(§8.1 A4), 다음 슬라이스가 **"이미 방어됨"으로 가정하지 못하게** 한다.
    """

    def test_verdict_is_identical_regardless_of_epoch(self):
        outs = {
            _derive(premium=_premium(epoch=e), identity=_identity(epoch=-e))
            for e in (0, -1, 1, 7, 10**9)
        }
        self.assertEqual(len(outs), 1, "epoch은 파생에 영향을 주지 않는다(의도)")
        self.assertEqual(outs.pop(), _derive())


class TestStaleRecordDoesNotShortCircuitRevoke(unittest.TestCase):
    """현재 동작을 **명시적으로** 못 박는다 — 우연이 아니라 결정이다.

    stale record가 이미 revoke를 증명해도 신선도 검사가 먼저라 `NeedsVerification`이 나온다.
    결과는 재검증 후 `Inactive`로 같고 RTT 1회를 더 쓸 뿐이다.

    ⚠️ 이걸 최적화하려면 **한 방향으로만 건전**함을 지켜야 한다 — watermark는 단조 증가하므로
    *stale이 "revoked"면 fresh도 revoked*(건전)지만 *stale이 "not revoked"라 해도 fresh는
    revoked일 수 있다*(불건전). 같은 record의 `disabled`/`NotFound`는 monotone이 아니므로
    최적화는 **revoke 술어에만** 적용해야 한다. 이 테스트가 red가 되면 그 최적화를 넣은 것이니,
    위 비대칭이 지켜졌는지 함께 확인할 것.

    ⚠️ 실측(2026-07-28 mutation): freshness 게이트 앞에 revoke 단락을 넣으면 이 테스트뿐 아니라
    `TestDecisionOrder::test_disabled_beats_revoked`도 red가 된다 — disabled 검사가 revoke보다
    **뒤로 밀리기** 때문이다. 즉 그 최적화는 SDK와 맞춘 `disabled > revoked` 우선순위도 함께
    보존해야 하며, 공짜가 아니다.
    """

    def test_stale_record_with_revoke_proof_still_needs_verification(self):
        revoked_ms = (IAT + 500) * 1000
        stale = _identity(tokens_valid_after_ms=revoked_ms,
                          verified_at_mono=NOW - LEASE_MAX_SECONDS - 1)
        self.assertEqual(_derive(identity=stale), NeedsVerification(concern=Concern.IDENTITY))

    def test_fresh_record_with_same_proof_rejects(self):
        """positive control — 신선하면 같은 증명으로 즉시 거부한다."""
        revoked_ms = (IAT + 500) * 1000
        self.assertEqual(_derive(identity=_identity(tokens_valid_after_ms=revoked_ms)),
                         Inactive(reason=InactiveReason.TOKEN_REVOKED))


if __name__ == "__main__":
    unittest.main()
