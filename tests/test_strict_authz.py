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
from app.topic_lease import LEASE_MAX_SECONDS

NOW = 1000.0
IAT = 1_700_000_000          # 초 epoch
WATERMARK_BEFORE = (IAT - 60) * 1000   # 토큰 발급 **전**에 revoke → 이 토큰은 유효
WATERMARK_AFTER = (IAT + 60) * 1000    # 토큰 발급 **후**에 revoke → 이 토큰은 revoked


def _identity(**kw):
    base = dict(disabled=False, tokens_valid_after_ms=WATERMARK_BEFORE, verified_at_mono=NOW, epoch=1)
    base.update(kw)
    return IdentityAuthorityFound(**base)


def _premium(**kw):
    base = dict(active=True, verified_at_mono=NOW, epoch=1)
    base.update(kw)
    return PremiumObservation(**base)


def _derive(**kw):
    base = dict(now_mono=NOW, premium=_premium(), identity=_identity(), token_iat_seconds=IAT)
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
        v = _derive(identity=IdentityAuthorityNotFound(verified_at_mono=NOW, epoch=1),
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
        stale = IdentityAuthorityNotFound(verified_at_mono=NOW - IDENTITY_NEGATIVE_RECHECK_SECONDS, epoch=1)
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

    def test_non_finite_is_stale(self):
        """비유한 입력은 판정 불가 → stale(fail-closed)."""
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(bad=bad):
                self.assertFalse(premium_observation_is_fresh(_premium(verified_at_mono=bad), now_mono=NOW))
                self.assertFalse(premium_observation_is_fresh(_premium(), now_mono=bad))


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


if __name__ == "__main__":
    unittest.main()
