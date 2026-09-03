"""FCM sender의 확정 미등록 분류·index·초기화 실패 계약."""

import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from firebase_admin import exceptions, messaging

from app.notifications import fcm


TOKENS = [
    "FCM_RAW_SECRET_OK_A91",
    "FCM_RAW_SECRET_DEAD_B82",
    "FCM_RAW_SECRET_NOT_FOUND_C73",
    "FCM_RAW_SECRET_INVALID_D64",
]


def _mixed_response():
    """각 실패 유형을 서로 다른 token index에 배치한다."""
    return SimpleNamespace(
        responses=[
            SimpleNamespace(success=True, exception=None),
            SimpleNamespace(
                success=False,
                exception=messaging.UnregisteredError("gone"),
            ),
            SimpleNamespace(
                success=False,
                exception=exceptions.NotFoundError("generic"),
            ),
            SimpleNamespace(
                success=False,
                exception=exceptions.FirebaseError(
                    "INVALID_ARGUMENT", "payload"
                ),
            ),
        ],
        success_count=1,
        failure_count=3,
    )


def _assert_mixed_result(case, result, log, *, event: str):
    # response[1]만 확정 미등록이다. code가 같은 response[2]와 payload 오류인
    # response[3]까지 들어오거나 index가 어긋나면 반드시 실패한다.
    case.assertEqual(result["failed_tokens"], [TOKENS[1]])
    case.assertEqual(result["success_count"], 1)
    case.assertEqual(result["failure_count"], 3)
    extra = log.call_args.kwargs["extra"]
    case.assertEqual(extra["event"], event)
    case.assertEqual(extra["total"], len(TOKENS))
    case.assertEqual(extra["success"], 1)
    case.assertEqual(extra["failure"], 3)
    case.assertEqual(extra["invalid_tokens"], 1)
    case.assertEqual(extra["retained_failures"], 2)
    case.assertEqual(
        extra["retained_by_reason"],
        {
            "ambiguous-retained-not-found": 1,
            "ambiguous-retained-invalid-argument": 1,
        },
    )
    # 성공 관측에는 원문 token이나 전체 UID가 들어갈 이유가 없다.
    rendered = repr(log.call_args)
    for token in TOKENS:
        case.assertNotIn(token, rendered)
        case.assertNotIn(token[:20], rendered)


class TestUnregisteredClassification(unittest.TestCase):
    def test_actual_consumer_uses_the_shared_messaging_stub(self):
        self.assertIs(fcm.messaging, messaging)

    def test_only_unregistered_subtype_is_cleanup_eligible(self):
        self.assertTrue(fcm.is_unregistered(messaging.UnregisteredError("gone")))
        self.assertFalse(fcm.is_unregistered(exceptions.NotFoundError("generic")))
        self.assertFalse(
            fcm.is_unregistered(
                exceptions.FirebaseError("INVALID_ARGUMENT", "payload")
            )
        )


class TestSyncMulticastClassification(unittest.TestCase):
    def test_type_and_response_index_determine_cleanup_candidate(self):
        with patch.object(fcm, "_firebase_initialized", True), \
             patch.object(
                 fcm.messaging,
                 "send_each_for_multicast",
                 return_value=_mixed_response(),
             ), \
             patch.object(fcm.logger, "info") as log:
            result = fcm.send_fcm_multicast_sync(TOKENS, "title", "body")
        _assert_mixed_result(self, result, log, event="fcm_multicast_sync")

    def test_init_failure_never_returns_cleanup_candidates(self):
        with patch.object(fcm, "_firebase_initialized", False), \
             patch.object(fcm, "init_firebase", return_value=False), \
             patch.object(fcm.messaging, "send_each_for_multicast") as send:
            result = fcm.send_fcm_multicast_sync(TOKENS, "title", "body")
        self.assertEqual(result["failure_count"], len(TOKENS))
        self.assertEqual(result["failed_tokens"], [])
        send.assert_not_called()

    def test_transport_exception_never_returns_cleanup_candidates(self):
        with patch.object(fcm, "_firebase_initialized", True), \
             patch.object(
                 fcm.messaging,
                 "send_each_for_multicast",
                 side_effect=RuntimeError("transport down"),
             ):
            result = fcm.send_fcm_multicast_sync(TOKENS, "title", "body")
        self.assertEqual(result["failure_count"], len(TOKENS))
        self.assertEqual(result["failed_tokens"], [])


class TestAsyncMulticastClassification(unittest.IsolatedAsyncioTestCase):
    async def test_visible_multicast_type_and_index_mapping(self):
        with patch.object(fcm, "_firebase_initialized", True), \
             patch.object(
                 fcm.messaging,
                 "send_each_for_multicast",
                 return_value=_mixed_response(),
             ), \
             patch.object(fcm.logger, "info") as log:
            result = await fcm.send_fcm_multicast(TOKENS, "title", "body")
        _assert_mixed_result(self, result, log, event="fcm_multicast")

    async def test_data_only_type_and_index_mapping(self):
        with patch.object(fcm, "_firebase_initialized", True), \
             patch.object(
                 fcm.messaging,
                 "send_each_for_multicast",
                 return_value=_mixed_response(),
             ), \
             patch.object(fcm.logger, "info") as log:
            result = await fcm.send_fcm_data_only(TOKENS, {"type": "sync"})
        _assert_mixed_result(self, result, log, event="fcm_data_only")

    async def test_async_init_failures_never_return_cleanup_candidates(self):
        for sender, args in (
            (fcm.send_fcm_multicast, (TOKENS, "title", "body")),
            (fcm.send_fcm_data_only, (TOKENS, {"type": "sync"})),
        ):
            with self.subTest(sender=sender.__name__), \
                 patch.object(fcm, "_firebase_initialized", False), \
                 patch.object(fcm, "init_firebase", return_value=False), \
                 patch.object(fcm.messaging, "send_each_for_multicast") as send:
                result = await sender(*args)
            self.assertEqual(result["failure_count"], len(TOKENS))
            self.assertEqual(result["failed_tokens"], [])
            send.assert_not_called()

    async def test_async_transport_failures_never_return_cleanup_candidates(self):
        for sender, args in (
            (fcm.send_fcm_multicast, (TOKENS, "title", "body")),
            (fcm.send_fcm_data_only, (TOKENS, {"type": "sync"})),
        ):
            with self.subTest(sender=sender.__name__), \
                 patch.object(fcm, "_firebase_initialized", True), \
                 patch.object(
                     fcm.messaging,
                     "send_each_for_multicast",
                     side_effect=RuntimeError("transport down"),
                 ):
                result = await sender(*args)
            self.assertEqual(result["failure_count"], len(TOKENS))
            self.assertEqual(result["failed_tokens"], [])


class TestSingleSenderRetryClassification(unittest.IsolatedAsyncioTestCase):
    """단일 sender의 비재시도 분류와 기존 transient 재시도를 분리해 잠근다."""

    async def _send_with(self, exc, *, max_retries=2):
        send = MagicMock(side_effect=exc)
        sleep = AsyncMock()
        with patch.object(fcm, "_firebase_initialized", True), \
             patch.object(fcm.messaging, "send", send), \
             patch.object(fcm.asyncio, "sleep", sleep):
            result = await fcm.send_fcm_notification(
                "secret-device-token", "title", "body", max_retries=max_retries
            )
        return result, send, sleep

    async def test_actual_unregistered_is_not_retried(self):
        result, send, sleep = await self._send_with(
            messaging.UnregisteredError("secret-device-token")
        )
        self.assertEqual(result, (False, "NOT_FOUND"))
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_awaited()

    async def test_generic_not_found_same_code_is_not_retried(self):
        result, send, sleep = await self._send_with(
            exceptions.NotFoundError("secret-device-token")
        )
        self.assertEqual(result, (False, "NOT_FOUND"))
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_awaited()

    async def test_invalid_argument_is_not_retried(self):
        result, send, sleep = await self._send_with(
            exceptions.InvalidArgumentError("payload contains secret-device-token")
        )
        self.assertEqual(result, (False, "INVALID_ARGUMENT"))
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_awaited()

    async def test_transient_error_keeps_existing_retry_count(self):
        result, send, sleep = await self._send_with(
            exceptions.UnavailableError("temporary")
        )
        self.assertEqual(result, (False, "UNAVAILABLE"))
        self.assertEqual(send.call_count, 3)
        self.assertEqual(sleep.await_args_list, [call(1), call(2)])


class TestSenderLogPrivacy(unittest.IsolatedAsyncioTestCase):
    def _assert_token_log(self, log, token: str) -> None:
        expected = "fp:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(log.call_args.kwargs["extra"]["token_fp"], expected)
        rendered = repr(log.call_args_list)
        self.assertNotIn(token, rendered)
        self.assertNotIn(token[:20], rendered)

    def test_token_fingerprint_and_error_descriptor_do_not_contain_secrets(self):
        secret_token = "FCM_SECRET_TOKEN_xyz"
        secret_code = "UID_SECRET_7f9"

        class SecretError(Exception):
            code = secret_code

        fingerprint = fcm.token_fingerprint(secret_token)
        descriptor = fcm.error_descriptor(SecretError(secret_token))
        self.assertEqual(
            fingerprint,
            "fp:" + hashlib.sha256(secret_token.encode("utf-8")).hexdigest()[:12],
        )
        self.assertNotIn(secret_token, fingerprint)
        self.assertNotIn(secret_token[:20], fingerprint)
        self.assertEqual(
            descriptor,
            {
                "error_type": "SecretError",
                "error_code": "unknown",
                "error_chain": None,
            },
        )
        rendered = repr(descriptor)
        self.assertNotIn(secret_token, rendered)
        self.assertNotIn(secret_code, rendered)

    async def test_single_success_log_uses_exact_fingerprint(self):
        token = "FCM_SECRET_TOKEN_success_123456"
        with patch.object(fcm, "_firebase_initialized", True), \
             patch.object(fcm.messaging, "send", return_value="message-id"), \
             patch.object(fcm.logger, "info") as info:
            result = await fcm.send_fcm_notification(token, "title", "body")
        self.assertEqual(result, (True, None))
        self._assert_token_log(info, token)

    async def test_single_nonretry_log_uses_exact_fingerprint(self):
        token = "FCM_SECRET_TOKEN_notfound_123456"
        with patch.object(fcm, "_firebase_initialized", True), \
             patch.object(
                 fcm.messaging,
                 "send",
                 side_effect=exceptions.NotFoundError("generic"),
             ), \
             patch.object(fcm.logger, "warning") as warning:
            result = await fcm.send_fcm_notification(token, "title", "body")
        self.assertEqual(result, (False, "NOT_FOUND"))
        self._assert_token_log(warning, token)

    async def test_single_retry_exhaustion_log_uses_exact_fingerprint(self):
        token = "FCM_SECRET_TOKEN_retry_123456"
        with patch.object(fcm, "_firebase_initialized", True), \
             patch.object(
                 fcm.messaging,
                 "send",
                 side_effect=exceptions.UnavailableError("temporary"),
             ), \
             patch.object(fcm.asyncio, "sleep", AsyncMock()), \
             patch.object(fcm.logger, "error") as error:
            result = await fcm.send_fcm_notification(token, "title", "body")
        self.assertEqual(result, (False, "UNAVAILABLE"))
        self._assert_token_log(error, token)

    async def test_single_sender_exception_log_omits_exception_secrets(self):
        secret = "UID_SECRET_7f9/FCM_SECRET_TOKEN_xyz"
        send = MagicMock(side_effect=RuntimeError(secret))
        with patch.object(fcm, "_firebase_initialized", True), \
             patch.object(fcm.messaging, "send", send), \
             patch.object(fcm.logger, "error") as error:
            result = await fcm.send_fcm_notification(
                "FCM_SECRET_TOKEN_xyz", "title", "body"
            )
        self.assertEqual(result, (False, "EXCEPTION"))
        rendered = repr(error.call_args_list)
        self.assertNotIn("UID_SECRET_7f9", rendered)
        self.assertNotIn("FCM_SECRET_TOKEN_xyz", rendered)
        self.assertFalse(error.call_args.kwargs.get("exc_info", False))
        self.assertEqual(error.call_args.kwargs["extra"]["error_type"], "RuntimeError")

    async def test_batch_transport_logs_omit_exception_and_token_secrets(self):
        secret = "UID_SECRET_7f9/FCM_SECRET_TOKEN_xyz"
        senders = (
            (fcm.send_fcm_multicast_sync, (["FCM_SECRET_TOKEN_xyz"], "t", "b")),
            (fcm.send_fcm_multicast, (["FCM_SECRET_TOKEN_xyz"], "t", "b")),
            (fcm.send_fcm_data_only, (["FCM_SECRET_TOKEN_xyz"], {"type": "sync"})),
        )
        for sender, args in senders:
            with self.subTest(sender=sender.__name__), \
                 patch.object(fcm, "_firebase_initialized", True), \
                 patch.object(
                     fcm.messaging,
                     "send_each_for_multicast",
                     side_effect=RuntimeError(secret),
                 ), \
                 patch.object(fcm.logger, "error") as error, \
                 patch.object(fcm.logger, "warning") as warning:
                result = sender(*args)
                if hasattr(result, "__await__"):
                    result = await result
            log = error if error.called else warning
            self.assertTrue(log.called)
            rendered = repr(log.call_args_list)
            self.assertNotIn("UID_SECRET_7f9", rendered)
            self.assertNotIn("FCM_SECRET_TOKEN_xyz", rendered)
            self.assertFalse(log.call_args.kwargs.get("exc_info", False))
            self.assertEqual(
                log.call_args.kwargs["extra"]["error_type"], "RuntimeError"
            )
            self.assertEqual(result["failed_tokens"], [])


if __name__ == "__main__":
    unittest.main()
