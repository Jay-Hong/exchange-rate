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


class TestAcceptedIsAnAuthorizationToken(unittest.TestCase):
    """`Accepted`는 wire 결과이면서 lease 발급의 **authorization 토큰**이다 (2026-07-30).

    ## 왜 생겼는가

    구 `apply_subscribe`는 `uid`·`snapshot`·`premium_verified_at_mono`·
    `identity_verified_at_mono`를 **각각** 받았다. 그래서 두 실수가 표현 가능했다:

      ① 이미 배포된 `verify_premium_status`(stale hit이 `PremiumStatus.ACTIVE`) + 지금 시각을
         `premium_verified_at_mono`로 → A1 노출 상한 무력화. `Determined`도 `PremiumObservation`도
         등장하지 않으므로 **어떤 타입 가드도 발화하지 않았다**.
      ② "A의 snapshot + uid=B" → fence(`cache.is_current`)는 A의 epoch를 보는데 lease는 B로 발급.

    ⛔ **이 토큰이 닫는 것은 ②뿐이다.** ①은 관측 스탬프 축(§C-INV 1: `verified_at_mono`를
    *변환 시점*에 찍는 문제)의 일이고 **아직 열려 있다** — 여기를 "C-INV 1 닫힘"으로 세지 말 것.
    """

    def test_map_verification_derives_uid_from_the_snapshot(self):
        """⛔ 파생이 요점 — 별도 인자로 받으면 불일치가 다시 표현 가능해진다.

        ⚠️ **fixture와 다른 uid로 검사한다.** 실측(2026-07-30, codex): `_Snapshot.uid` 하나만
        쓰면 `uid=verification.snapshot.uid`를 **상수 `"uid-1"`로 바꿔도 생존**한다 — 그 테스트는
        "파생"이 아니라 "그 fixture와 같은 값"만 잠근다. 두 개를 비교해야 파생이 잠긴다.
        """
        other = type("_Other", (), {"uid": "uid-derived-from-snapshot", "epoch": 11})()
        derived = map_verification(
            VerifiedActive(snapshot=other, premium_verified_at_mono=3.0,
                           identity_verified_at_mono=4.0),
            rng=_Rng(),
        )
        self.assertEqual(derived.uid, other.uid)
        self.assertEqual(map_verification(ACTIVE, rng=_Rng()).uid, _Snapshot.uid)
        self.assertNotEqual(other.uid, _Snapshot.uid, "두 fixture가 같으면 이 테스트는 공허하다")

    def test_uid_and_snapshot_uid_must_agree(self):
        with self.assertRaises(ValueError) as caught:
            Accepted(snapshot=_Snapshot(), premium_verified_at_mono=1.0,
                     identity_verified_at_mono=2.0, uid="someone-else")
        self.assertIn("snapshot.uid", str(caught.exception))

    def test_snapshot_without_uid_is_refused_not_ignored(self):
        """⛔ `getattr(snapshot, "uid", None)`로 넘기면 검사가 **있는 척만** 한다.

        그 형태는 uid를 확인할 수 없는 객체를 조용히 통과시키고, 그때 불일치 검사는
        `None != uid`로 우연히 걸리거나(운) 안 걸린다(uid가 None이면). fail-closed가 유일한 답이다.
        """
        class _NoUid:
            epoch = 1

        with self.assertRaises(ValueError) as caught:
            Accepted(snapshot=_NoUid(), premium_verified_at_mono=1.0,
                     identity_verified_at_mono=2.0, uid=_Snapshot.uid)
        self.assertIn("uid가 없다", str(caught.exception))

    def test_uid_must_be_a_non_empty_string(self):
        """⛔ snapshot이 **같은 나쁜 값**을 갖는 경우로 검사한다 — 그러지 않으면 불일치 검사에
        먼저 걸려 이 단언이 **다른 코드 때문에** 통과한다.

        실측(2026-07-30): 처음엔 `snapshot.uid="uid-1"` 고정으로 썼더니 non-empty 검사를 지워도
        전부 green이었다(불일치가 대신 잡았다). 즉 그 테스트는 잠근다고 주장한 것을 잠그지 않았다.
        `uid=None`이 `snapshot.uid=None`과 짝을 이루면 **주체 없는 authorization**이 통과한다 —
        그게 이 검사가 막는 실제 상태다.
        """
        for bad in ("", None, 0, b"uid-1", ["uid-1"]):
            mirror = type("_Mirror", (), {"uid": bad, "epoch": 7})()
            with self.subTest(uid=bad), self.assertRaises(ValueError) as caught:
                Accepted(snapshot=mirror, premium_verified_at_mono=1.0,
                         identity_verified_at_mono=2.0, uid=bad)
            self.assertIn("비어 있지 않은 str", str(caught.exception),
                          "불일치 검사가 대신 잡았다 — 이 단언은 non-empty를 잠그지 않는다")

    def test_observation_times_must_be_finite_numbers(self):
        """⛔ `nan`은 `min()`에서 **첫 인자일 때만** 전파된다 — 조용히 무시되면 상한 전량 lease다.

        ⚠️ `True`도 거부한다(`isinstance(True, int)`가 참이라 bool이 시각으로 흘러들 수 있다).
        """
        bad_values = [float("nan"), float("inf"), float("-inf"), True, "1.0", None]
        for field in ("premium_verified_at_mono", "identity_verified_at_mono"):
            for bad in bad_values:
                kwargs = {"snapshot": _Snapshot(), "uid": _Snapshot.uid,
                          "premium_verified_at_mono": 1.0, "identity_verified_at_mono": 2.0}
                kwargs[field] = bad
                with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                    Accepted(**kwargs)


class TestAuthorizationsAreMintedInOnePlace(unittest.TestCase):
    """`Accepted` 생성 지점을 `app/strict_wire.py` 하나로 못박는다.

    ⚠️ **이 트립와이어가 사 주는 것과 사 주지 않는 것**을 정확히 적는다. 사 주는 것:
    "authorization은 검증 매핑에서만 나온다"가 배선에서 우연히 깨지지 않는다. 사 주지 **않는**
    것: 이 모듈 **안에서** 잘못 만드는 것(`map_verification`이 엉뚱한 값을 싣는 경우)은 못 잡는다.

    ⛔ 같은 형태의 트립와이어를 `Determined`에 걸자는 안은 **기각**됐다(2026-07-30 검토):
    허용 파일(`app/subscription.py`)이 **1시간 stale 캐시를 소유한 바로 그 모듈**이라 허용 범위
    안에서의 stale 승격이 정의상 미검출이고, 게다가 그 provider는 N-3에서 leaf 모듈로 옮기기로
    이미 적혀 있다. `Accepted`는 두 조건 모두 해당하지 않아 같은 비판이 전이되지 않는다.
    """

    def test_accepted_is_constructed_only_in_strict_wire(self):
        import ast
        import pathlib

        app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
        offenders = []
        for path in sorted(app_dir.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # ⚠️ bare name **과** attribute 형태를 모두 본다 — `strict_wire.Accepted(...)`는
                #    `ast.Name`이 아니라 `ast.Attribute`라 name-only 판정기를 통과했다(codex).
                #    ⛔ 남은 구멍: `from app.strict_wire import Accepted as Authorization` 같은
                #    별칭은 호출 이름이 달라 못 잡는다. 별칭 import 자체를 아래에서 함께 막는다.
                func = node.func
                called = (
                    func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else None
                )
                if called == "Accepted":
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            [o for o in offenders if not o.startswith("strict_wire.py")], [],
            f"`Accepted`를 strict_wire 밖에서 만들고 있다: {offenders}",
        )
        self.assertTrue(offenders, "생성 지점이 0개다 — 트립와이어가 아무것도 지키지 않는다")

        # 별칭 import 차단 — 이름을 바꿔 위 판정기를 우회하는 가장 쉬운 길이다.
        aliased = []
        for path in sorted(app_dir.rglob("*.py")):
            if path.name == "strict_wire.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "Accepted" and alias.asname:
                            aliased.append(f"{path.name}:{node.lineno} as {alias.asname}")
        self.assertEqual(aliased, [], f"`Accepted`를 별칭으로 import했다: {aliased}")


if __name__ == "__main__":
    unittest.main()
