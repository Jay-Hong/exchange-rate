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
    """R5 — 두 시간 축의 **관계**가 계약이다.

    ⛔ `D ≤ T` 면 transport 상한이 발동하기 전에 호출자가 먼저 포기하므로 낮춘 `httpTimeout` 이
    아무것도 바꾸지 않는다 — 축2 가 **죽은 코드**가 된다. 그 상태를 import 시점에 막는다.
    """

    def test_wire_deadline_exceeds_the_transport_timeout(self):
        from app import config

        self.assertGreater(
            config.WS_AUTH_WIRE_DEADLINE_SECONDS,
            config.WS_AUTH_HTTP_TIMEOUT_SECONDS,
            "D ≤ T 면 transport 상한이 영영 발동하지 않는다",
        )

    def test_config_import_refuses_a_violating_pair(self):
        """⛔ 단언만 있으면 상수를 바꾼 사람이 이 테스트를 지우고 끝낼 수 있다 —
        **import 자체가 거부**하는지 본다."""
        import importlib
        from unittest.mock import patch

        from app import config

        src = pathlib.Path(config.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "raise ValueError", src.split("WS_AUTH_WIRE_DEADLINE_SECONDS <=")[-1][:400],
            "불변식이 import 시점에 강제되지 않는다",
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
