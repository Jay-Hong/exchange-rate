"""WS 오류 프레임 **단일 생성 지점**의 계약 (`app/topic_wire.py`).

## 왜 별 파일인가

`build_subscription_error` 는 firebase-free 순수 함수이고, 그 존재 이유가 **§8-C 의 결합 규칙을
생성 시점에 강제**하는 것이다. 그 강제는 `/ws` 경계에서 관측되지 않는다 — 호출부가 전부 올바른
조합만 넘기면 강제를 통째로 지워도 스위트가 green 이기 때문이다(변이 실측: `emitter: 필수 조합
검사 제거` / `금지 조합 검사 제거` 둘 다 **생존**했다). 그래서 여기서 직접 잠근다.
"""
import pathlib
import unittest

from app.topic_wire import (
    WHOLE_REQUEST_ERRORS,
    FirebaseNotInitialized,
    SubscribeAuthFailed,
    build_subscription_error,
)


class TestRetryAfterCoupling(unittest.TestCase):
    """§8-C: `temporarily_unavailable` 은 `retry_after_seconds` 를 **동반**한다."""

    def test_temporarily_unavailable_requires_retry_after(self):
        """⛔ 없으면 클라가 재시도 시점을 유도할 수 없다 — 조용한 실패가 된다."""
        with self.assertRaises(ValueError):
            build_subscription_error(request_id="r", error="temporarily_unavailable")

    def test_retry_after_must_be_a_positive_int(self):
        """⚠️ `bool` 도 거부한다 — `isinstance(True, int)` 가 참이라 흘러들 수 있다."""
        for bad in (0, -1, 1.5, "5", True, None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                build_subscription_error(
                    request_id="r",
                    error="temporarily_unavailable",
                    retry_after_seconds=bad,
                )

    def test_other_errors_must_not_carry_retry_after(self):
        """⛔ `invalid_token` 에 붙으면 **자격이 죽은 채 영구 재시도 루프**가 된다."""
        for code in ("invalid_token", "invalid_request"):
            with self.subTest(error=code), self.assertRaises(ValueError):
                build_subscription_error(
                    request_id="r", error=code, retry_after_seconds=5
                )

    def test_valid_frames_have_exactly_the_expected_keys(self):
        unavailable = build_subscription_error(
            request_id="r1", error="temporarily_unavailable", retry_after_seconds=5
        )
        self.assertEqual(
            unavailable,
            {
                "type": "subscription_error",
                "request_id": "r1",
                "error": "temporarily_unavailable",
                "retry_after_seconds": 5,
            },
        )
        terminal = build_subscription_error(request_id="r2", error="invalid_token")
        self.assertEqual(
            terminal,
            {"type": "subscription_error", "request_id": "r2", "error": "invalid_token"},
        )
        self.assertNotIn("retry_after_seconds", terminal)

    def test_no_upper_bound_is_enforced(self):
        """⚠️ 상한을 **의도적으로** 강제하지 않는다 — 정본 §8-C 는 "동반"만 요구한다.

        여기서 임의 상한을 박으면 값·jitter 정책을 **코드가 먼저 결정**해 버린다. 그 정책이
        정해지면 이 테스트를 그 결정으로 교체할 것(지금 지우면 근거 없이 되돌린 것이 된다).
        """
        frame = build_subscription_error(
            request_id="r", error="temporarily_unavailable", retry_after_seconds=3600
        )
        self.assertEqual(frame["retry_after_seconds"], 3600)


class TestErrorVocabulary(unittest.TestCase):
    """§8-C 의 **전체-요청 코드**만 프레임이 된다.

    ⛔ 어휘를 강제하지 않으면 오타나 즉흥 코드가 정상 프레임으로 나가 코드가 **제3의 계약**을
    만든다(실측: `error="typo_not_in_section_8"` 이 그대로 통과했다 — codex Medium).
    """

    def test_unknown_codes_are_rejected(self):
        for bad in ("typo_not_in_section_8", "", "INVALID_TOKEN", None):
            with self.subTest(error=bad), self.assertRaises(ValueError):
                build_subscription_error(request_id="r", error=bad)

    def test_per_topic_codes_are_rejected(self):
        """per-topic 코드는 ack 의 `rejected_topics` 에 실린다 — 여기 오면 계약 혼선이다."""
        for per_topic in (
            "unknown_topic", "topic_unavailable", "premium_required",
            "krx_entitlement_required", "topics_disabled",
        ):
            with self.subTest(error=per_topic), self.assertRaises(ValueError):
                build_subscription_error(request_id="r", error=per_topic)

    def test_every_allowed_code_can_actually_build_a_frame(self):
        """⛔ 자기검사 — 허용 목록이 비거나 좁아지면 이 테스트가 먼저 깨진다."""
        self.assertEqual(
            WHOLE_REQUEST_ERRORS,
            {"invalid_token", "temporarily_unavailable", "invalid_request", "request_too_large"},
        )
        for code in sorted(WHOLE_REQUEST_ERRORS):
            with self.subTest(error=code):
                retry = 5 if code == "temporarily_unavailable" else None
                frame = build_subscription_error(
                    request_id="r", error=code, retry_after_seconds=retry
                )
                self.assertEqual(frame["error"], code)


class TestSubscribeAuthFailed(unittest.TestCase):
    def test_carries_the_wire_verdict(self):
        failure = SubscribeAuthFailed("temporarily_unavailable", 5)
        self.assertEqual(failure.error, "temporarily_unavailable")
        self.assertEqual(failure.retry_after_seconds, 5)

    def test_retry_after_defaults_to_absent(self):
        self.assertIsNone(SubscribeAuthFailed("invalid_token").retry_after_seconds)

    def test_firebase_not_initialized_is_a_dedicated_type(self):
        """⛔ 전용 타입이라야 `RuntimeError` 를 통째로 잡는 실수를 피할 수 있다."""
        self.assertTrue(issubclass(FirebaseNotInitialized, RuntimeError))
        self.assertIsNot(FirebaseNotInitialized, RuntimeError)


class TestAuthTimeConstants(unittest.TestCase):
    """두 시간 축은 **독립**이다 — 관계를 불변식으로 강제하지 않는다.

    ⛔ 한때 `D > T` 를 import 시점에 강제했고 근거를 "D ≤ T 면 transport 상한이 죽은 코드"라고
    적었는데 **그 근거가 거짓이었다**(codex High, 재현 확인): 호출자가 포기해도 `to_thread`
    worker 는 취소되지 않고 **SDK 자체 상한 T 에 종료된다**(D=0.05/T=0.20 → worker 0.205s).
    즉 유효한 설정을 막는 게이트였다. 그래서 이 클래스는 **관계가 아니라 현재 선택**을 기록한다.
    """

    def test_current_choice_preserves_the_sdk_error_taxonomy(self):
        """현재 D > T 를 택한 이유는 불변식이 아니라 **의미**다.

        D > T 면 SDK 가 먼저 끝나 그 오류 분류(`invalid_token` / 일시 장애)가 클라에 도달한다.
        D < T 면 느린 검증이 전부 deadline 으로 뭉쳐져 §8-C 타입 경계가 그 구간에서 사라진다.
        ⚠️ 이 단언이 red 가 되면 **버그가 아니라 정책 변경**이다 — 위 trade-off 를 다시 판단하고
        이 docstring 을 고칠 것. 값만 맞추고 지나가지 말 것.
        """
        from app import config

        self.assertGreater(
            config.WS_AUTH_WIRE_DEADLINE_SECONDS,
            config.WS_AUTH_HTTP_TIMEOUT_SECONDS,
        )

    def test_persistent_faults_wait_longer_than_transient_ones(self):
        from app import config

        self.assertGreater(
            config.WS_AUTH_PERSISTENT_FAULT_RETRY_AFTER_SECONDS,
            config.WS_AUTH_RETRY_AFTER_SECONDS,
            "재시도로 낫지 않는 결함에 같은 간격을 주면 retry storm 이 된다",
        )


if __name__ == "__main__":
    unittest.main()
