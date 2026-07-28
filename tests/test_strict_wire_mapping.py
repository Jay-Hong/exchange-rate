"""strict 판정 → wire 결과 매핑 (§8-C / §8.1 D5).

배선의 첫 조각. registry·lease가 없어도 **독립으로** 잠글 수 있는 두 게이트를 여기서 닫는다:
전용 오류 카운터 + ERROR 로그 / `retry_after` 하향 jitter.

## §8.1 D5 — 오류는 단일 전순서가 아니라 **처리 단계**다

    1) payload validation      → 형식 오류
    2) token                   → invalid_token | temporarily_unavailable
    3) capability / topic 존재 → topics_disabled | unknown_topic
    4) premium                 → temporarily_unavailable | premium_required
    5) KRX entitlement         → krx_entitlement_required
    6) state transition        → UID 바인딩·lease CAS·registry 반영

1~2는 **전체-요청**, 3~5는 **per-topic**이다. ⚠️ 단 `temporarily_unavailable`은 **어느 단계에서
발생하든 전체-요청으로 승격**된다(§8-C) — per-topic으로 두면 "일부 topic만 조용히 빠진" 상태가 된다.
"""
import unittest

from app.strict_authz import Concern, InactiveReason
from app.strict_verifier import (
    StrictVerifierConfigError,
    TemporarilyUnavailable,
    VerifiedActive,
    VerifiedInactive,
)
from app.strict_wire import (
    RETRY_AFTER_MAX_SECONDS,
    RETRY_AFTER_MIN_SECONDS,
    Accepted,
    PerTopicRejection,
    WholeRequestFailure,
    map_verification,
    map_config_error,
    reset_wire_counters,
    wire_counters,
)


class _Snapshot:
    uid = "uid-1"
    epoch = 7


ACTIVE = VerifiedActive(snapshot=_Snapshot(), premium_verified_at_mono=1.0,
                        identity_verified_at_mono=2.0)


class _Rng:
    """결정론적 jitter — `random`을 주입해 테스트가 흔들리지 않게."""

    def __init__(self, value=0.0):
        self.value = value
        self.calls = []

    def uniform(self, low, high):
        self.calls.append((low, high))
        return low + (high - low) * self.value


class TestActiveAndInactive(unittest.TestCase):
    def setUp(self):
        reset_wire_counters()

    def test_active_is_accepted_and_carries_the_snapshot(self):
        """⛔ §A4 소비 fence는 **판정을 만든 그 snapshot**을 넘겨야 성립한다 —
        매핑이 그걸 떨어뜨리면 배선이 다시 뜰 수밖에 없고, 그러면 fence가 무의미해진다."""
        result = map_verification(ACTIVE, rng=_Rng())
        self.assertIsInstance(result, Accepted)
        self.assertIs(result.snapshot, ACTIVE.snapshot)

    def test_identity_verdicts_are_whole_request_invalid_token(self):
        """D5 2단계 — 토큰 축 실패는 **전체-요청**이다. per-topic으로 두면 일부만 조용히 빠진다."""
        for reason in (InactiveReason.TOKEN_REVOKED, InactiveReason.ACCOUNT_DISABLED,
                       InactiveReason.ACCOUNT_DELETED):
            with self.subTest(reason=reason.value):
                result = map_verification(VerifiedInactive(reason=reason), rng=_Rng())
                self.assertIsInstance(result, WholeRequestFailure)
                self.assertEqual(result.error, "invalid_token")

    def test_premium_inactive_is_per_topic(self):
        """D5 4단계 — 구독 없음은 **per-topic**이다(다른 topic은 받을 수 있다)."""
        result = map_verification(
            VerifiedInactive(reason=InactiveReason.PREMIUM_INACTIVE), rng=_Rng()
        )
        self.assertIsInstance(result, PerTopicRejection)
        self.assertEqual(result.error, "premium_required")

    def test_identity_and_premium_scopes_differ(self):
        """⛔ 둘을 같은 범위로 접으면 D5의 단계 구분이 사라진다."""
        identity = map_verification(
            VerifiedInactive(reason=InactiveReason.TOKEN_REVOKED), rng=_Rng()
        )
        premium = map_verification(
            VerifiedInactive(reason=InactiveReason.PREMIUM_INACTIVE), rng=_Rng()
        )
        self.assertNotEqual(type(identity), type(premium))


class TestTemporarilyUnavailableIsAlwaysWholeRequest(unittest.TestCase):
    def setUp(self):
        reset_wire_counters()

    def test_promoted_to_whole_request_from_either_concern(self):
        """⛔ §8-C — **어느 단계에서 발생하든** 전체-요청으로 승격된다."""
        for concern in (Concern.IDENTITY, Concern.PREMIUM):
            with self.subTest(concern=concern.value):
                result = map_verification(
                    TemporarilyUnavailable(concern=concern), rng=_Rng()
                )
                self.assertIsInstance(result, WholeRequestFailure)
                self.assertEqual(result.error, "temporarily_unavailable")

    def test_retry_after_is_always_present(self):
        result = map_verification(TemporarilyUnavailable(concern=Concern.PREMIUM), rng=_Rng())
        self.assertIsNotNone(result.retry_after_seconds)


class TestRetryAfterJitter(unittest.TestCase):
    """⛔ 게이트 3 — 전역 장애에서 **전원이 같은 초에 복귀**하면 herd가 유지된다.

    §D6이 재인증 타이머에 `U(0,60)` **하향** jitter를 넣은 것과 같은 이유다.
    """

    def setUp(self):
        reset_wire_counters()

    def test_jitter_is_downward_only(self):
        """⛔ 상향 jitter는 **deadline을 침범**한다(§D6가 명시적으로 금지)."""
        base = TemporarilyUnavailable(concern=Concern.PREMIUM).retry_after_seconds
        highest = map_verification(
            TemporarilyUnavailable(concern=Concern.PREMIUM), rng=_Rng(value=1.0)
        ).retry_after_seconds
        self.assertLessEqual(highest, base, "jitter가 상향으로 갔다")

    def test_jitter_actually_varies(self):
        """고정값이면 herd가 그대로 유지된다."""
        low = map_verification(
            TemporarilyUnavailable(concern=Concern.PREMIUM), rng=_Rng(value=0.0)
        ).retry_after_seconds
        high = map_verification(
            TemporarilyUnavailable(concern=Concern.PREMIUM), rng=_Rng(value=1.0)
        ).retry_after_seconds
        self.assertNotEqual(low, high, "jitter가 없다")

    def test_result_is_an_integer_in_contract_range(self):
        """§D-const — 정수 초, 1~30. 클라는 Int로 디코드한다."""
        for value in (0.0, 0.5, 1.0):
            with self.subTest(rng=value):
                seconds = map_verification(
                    TemporarilyUnavailable(concern=Concern.PREMIUM), rng=_Rng(value=value)
                ).retry_after_seconds
                self.assertIsInstance(seconds, int)
                self.assertGreaterEqual(seconds, RETRY_AFTER_MIN_SECONDS)
                self.assertLessEqual(seconds, RETRY_AFTER_MAX_SECONDS)

    def test_jitter_never_goes_below_the_floor(self):
        """⛔ 0초로 내려가면 즉시 재시도 herd가 된다."""
        seconds = map_verification(
            TemporarilyUnavailable(concern=Concern.PREMIUM, retry_after_seconds=1),
            rng=_Rng(value=1.0),
        ).retry_after_seconds
        self.assertGreaterEqual(seconds, RETRY_AFTER_MIN_SECONDS)


class TestJitterClampIsAGuardNotDecoration(unittest.TestCase):
    """clamp는 **현재 상수 조합에서는 도달하지 않는다**(base=1, jitter 0.4 → 최소 0.6 → 반올림 1).

    ⚠️ 그래서 매핑을 통해서만 보면 clamp를 지워도 통과한다(mutation 생존으로 확인). 헬퍼를
    직접 경계에서 시험해, **상수를 조정했을 때** 계약이 깨지지 않도록 잠근다 — 계획이
    `JITTER_FRACTION` 같은 값을 측정 후 조정하라고 열어 두었기 때문이다.
    """

    def test_floor_holds_when_the_jitter_fraction_is_widened(self):
        from unittest.mock import patch

        from app.strict_wire import _jitter_down

        with patch("app.strict_wire.JITTER_FRACTION", 1.5):   # spread > base
            seconds = _jitter_down(2, _Rng(value=1.0))
        self.assertGreaterEqual(seconds, RETRY_AFTER_MIN_SECONDS, "0초 이하로 내려가면 즉시 herd")

    def test_ceiling_holds_for_an_oversized_base(self):
        from app.strict_wire import _jitter_down

        self.assertLessEqual(_jitter_down(600, _Rng(value=0.0)), RETRY_AFTER_MAX_SECONDS)


class TestConfigErrorMapping(unittest.TestCase):
    """⛔ 게이트 1 — 전용 카운터 + ERROR 로그.

    설정 결함은 **사람이 봐야** 한다. wire로는 `temporarily_unavailable`이지만(클라가 할 수 있는
    게 그것뿐이다), 그 신호가 transient와 **같은 카운터에 섞이면** 죽은 API key를 영영 못 찾는다.
    """

    def setUp(self):
        reset_wire_counters()

    def _error(self, kind_name="PREMIUM_MISCONFIGURED"):
        from app.strict_verifier import ConfigFaultKind

        return StrictVerifierConfigError("dead key", kind=getattr(ConfigFaultKind, kind_name))

    def test_maps_to_temporarily_unavailable_not_a_new_code(self):
        """§8-C에 internal_error가 없다 — 새 코드를 만들면 클라가 재시도 여부를 못 정한다."""
        result = map_config_error(self._error(), rng=_Rng())
        self.assertIsInstance(result, WholeRequestFailure)
        self.assertEqual(result.error, "temporarily_unavailable")

    def test_uses_the_top_of_the_retry_range(self):
        """재시도해도 낫지 않으므로 **가장 길게** 미룬다."""
        result = map_config_error(self._error(), rng=_Rng(value=0.0))
        self.assertGreater(result.retry_after_seconds, 5)

    def test_counter_is_separate_from_transient(self):
        map_verification(TemporarilyUnavailable(concern=Concern.PREMIUM), rng=_Rng())
        map_config_error(self._error(), rng=_Rng())
        counters = wire_counters()
        self.assertEqual(counters["transient"], 1)
        self.assertEqual(counters["config_fault"], 1)

    def test_counter_keys_on_bounded_kind_not_message(self):
        """⛔ 메시지는 `repr(result)`를 담아 cardinality가 열려 있다."""
        map_config_error(self._error("PREMIUM_MISCONFIGURED"), rng=_Rng())
        map_config_error(self._error("PREMIUM_PROTOCOL_VIOLATION"), rng=_Rng())
        by_kind = wire_counters()["config_fault_by_kind"]
        self.assertEqual(by_kind["premium_misconfigured"], 1)
        self.assertEqual(by_kind["premium_protocol_violation"], 1)

    def test_error_log_carries_the_kind_as_a_structured_field(self):
        """이 리포는 구조화 로깅을 쓴다 — 포맷된 문자열이 아니라 **레코드 필드**가 계약이다."""
        with self.assertLogs("exchange_rate.strict_wire", level="ERROR") as captured:
            map_config_error(self._error(), rng=_Rng())
        record = captured.records[0]
        self.assertEqual(getattr(record, "config_fault_kind", None), "premium_misconfigured")
        self.assertIn("premium_misconfigured", record.getMessage(), "운영자 grep용 텍스트도 필요하다")

    def test_transient_does_not_emit_an_error_log(self):
        """⛔ 공급자 장애마다 ERROR가 찍히면 진짜 설정 결함이 묻힌다."""
        import logging

        with self.assertNoLogs("exchange_rate.strict_wire", level="ERROR"):
            map_verification(TemporarilyUnavailable(concern=Concern.PREMIUM), rng=_Rng())


class TestRegistryIsUntouched(unittest.TestCase):
    """⛔ 게이트 2의 절반 — 이 계층은 **순수**해야 registry 불변을 논할 수 있다."""

    def test_mapping_has_no_side_effects_beyond_counters(self):
        reset_wire_counters()
        before = dict(wire_counters())
        map_verification(ACTIVE, rng=_Rng())
        after = wire_counters()
        self.assertEqual(after["transient"], before["transient"])
        self.assertEqual(after["config_fault"], before["config_fault"])


if __name__ == "__main__":
    unittest.main()
