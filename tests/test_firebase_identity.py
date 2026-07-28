"""Firebase identity provider — A6 검증기가 쓰는 identity authority adapter.

계약 근거: `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A6-1 / A6-2, `app/strict_verifier.py`.

## 이 adapter가 넘어야 할 함정 (전부 고정 SDK 소스로 확인, firebase-admin==6.9.0)

    UserNotFoundError  → exceptions.NotFoundError
    ConfigurationNotFoundError / TenantNotFoundError → **같은 부모의 형제**
      ⇒ `except NotFoundError → NotFound`로 접으면 프로젝트 오설정이
        "전 계정 삭제됨"이 된다.

    _user_mgt.py:599  `if not body or not body.get('users'): raise UserNotFoundError`
      ⇒ 404가 아니라 **빈/이상한 200 body**에서도 난다. 중간 프록시가 `{}`를 200으로
        돌려주는 동안 재접속하는 전 사용자가 `account_deleted`로 축출되고, 그 부정 관측이
        `IDENTITY_NEGATIVE_RECHECK_SECONDS`(300s) 동안 캐시된다.

    _http_client.py:50  DEFAULT_TIMEOUT_SECONDS = 120
      ⇒ 검증기의 전체 deadline(8초)보다 훨씬 길다. 낮추지 않으면 멈춘 provider가 flight를
        오래 점유하고, 그 키는 그동안 계속 `temporarily_unavailable`이다.

    _auth_utils.py  validate_uid → 평범한 `ValueError`
      ⇒ `derive_verdict`의 축 가드와 **타입이 겹친다**. 그대로 새어 나가면 "우리 코드 버그"로
        오분류된다.

    google.auth 자격증명 예외(RefreshError / DefaultCredentialsError)는 `FirebaseError`가 **아니다**
      ⇒ `except FirebaseError`만 쓰면 서비스 계정 키 폐기가 통째로 새어 나간다.
"""
import asyncio
import unittest

from app.strict_verifier import (
    IdentityFound,
    IdentityMisconfigured,
    IdentityNotFound,
    IdentityUnavailable,
)
from app.firebase_identity import (
    FIREBASE_HTTP_TIMEOUT_SECONDS,
    FirebaseIdentityProvider,
)

UID = "uid-1"


class _FakeUser:
    def __init__(self, disabled=False, tokens_valid_after_timestamp=1_699_999_900_000):
        self.disabled = disabled
        self.tokens_valid_after_timestamp = tokens_valid_after_timestamp


def _raiser(exc):
    def _get_user(uid, app=None):
        raise exc
    return _get_user


def _run(get_user, uid=UID):
    provider = FirebaseIdentityProvider(get_user=get_user)
    return asyncio.run(provider(uid))


class TestHappyPath(unittest.TestCase):
    def test_enabled_user_maps_to_found(self):
        result = _run(lambda uid, app=None: _FakeUser())
        self.assertEqual(
            result, IdentityFound(disabled=False, tokens_valid_after_ms=1_699_999_900_000)
        )

    def test_disabled_flag_is_carried_through(self):
        result = _run(lambda uid, app=None: _FakeUser(disabled=True))
        self.assertTrue(result.disabled)

    def test_watermark_is_passed_as_milliseconds_unchanged(self):
        """⚠️ SDK가 이미 ms다(`_user_mgt.py`: `1000 * int(valid_since)`). 여기서 또 곱하면
        **모든 토큰이 revoked로 오판**된다."""
        result = _run(lambda uid, app=None: _FakeUser(tokens_valid_after_timestamp=1_700_000_000_000))
        self.assertEqual(result.tokens_valid_after_ms, 1_700_000_000_000)


class TestNotFoundIsNarrow(unittest.TestCase):
    """⛔ `UserNotFoundError` **정확히 그것만** NotFound다."""

    def test_user_not_found_maps_to_not_found(self):
        from firebase_admin import auth

        self.assertIsInstance(_run(_raiser(auth.UserNotFoundError("gone"))), IdentityNotFound)

    def test_sibling_not_found_errors_are_misconfigured_not_deleted(self):
        """⛔ 형제 예외를 NotFound로 접으면 **프로젝트 오설정이 "전 계정 삭제"**가 된다."""
        from firebase_admin import auth

        for name in ("ConfigurationNotFoundError", "TenantNotFoundError"):
            exc_cls = getattr(auth, name, None)
            if exc_cls is None:
                continue
            with self.subTest(exception=name):
                result = _run(_raiser(exc_cls("boom")))
                self.assertIsInstance(result, IdentityMisconfigured)

    def test_bare_not_found_error_is_misconfigured(self):
        """파싱 불가 404가 접히는 평범한 `NotFoundError`도 '계정 삭제'가 아니다."""
        from firebase_admin import exceptions

        result = _run(_raiser(exceptions.NotFoundError("unparseable", cause=None)))
        self.assertIsInstance(result, IdentityMisconfigured)


class TestErrorBuckets(unittest.TestCase):
    def test_permission_denied_is_misconfigured(self):
        """서비스 계정이 권한을 잃은 것 — 재시도해도 낫지 않는다."""
        from firebase_admin import auth

        self.assertIsInstance(
            _run(_raiser(auth.InsufficientPermissionError("denied", cause=None, http_response=None))),
            IdentityMisconfigured,
        )

    def test_rate_limit_is_unavailable(self):
        """⚠️ 4xx라고 영구 오류로 접으면 안 된다 — 백오프하면 낫는다."""
        from firebase_admin import auth

        self.assertIsInstance(
            _run(_raiser(auth.TooManyAttemptsTryLaterError("slow down", cause=None, http_response=None))),
            IdentityUnavailable,
        )

    def test_transient_firebase_error_is_unavailable(self):
        from firebase_admin import exceptions

        self.assertIsInstance(
            _run(_raiser(exceptions.UnavailableError("503", cause=None))), IdentityUnavailable
        )

    def test_google_auth_credential_errors_are_misconfigured(self):
        """⛔ 이것들은 `FirebaseError`가 **아니다** — `except FirebaseError`만 쓰면 새어 나간다."""
        from google.auth.exceptions import DefaultCredentialsError, RefreshError

        for exc in (RefreshError("key revoked"), DefaultCredentialsError("no creds")):
            with self.subTest(exception=type(exc).__name__):
                self.assertIsInstance(_run(_raiser(exc)), IdentityMisconfigured)

    def test_google_auth_transport_error_is_unavailable(self):
        from google.auth.exceptions import TransportError

        self.assertIsInstance(_run(_raiser(TransportError("network"))), IdentityUnavailable)

    def test_malformed_uid_value_error_does_not_escape_as_our_bug(self):
        """⛔ `validate_uid`의 `ValueError`는 `derive_verdict`의 축 가드와 **타입이 겹친다**.

        그대로 새어 나가면 "우리 코드 버그"로 오분류된다.
        """
        result = _run(_raiser(ValueError('Invalid uid: ""')))
        self.assertIsInstance(result, IdentityMisconfigured)

    def test_unknown_exception_is_not_silently_swallowed(self):
        """모르는 예외를 transient로 접으면 진짜 버그가 영원히 재시도된다."""
        with self.assertRaises(RuntimeError):
            _run(_raiser(RuntimeError("unexpected")))

    def test_error_codes_are_bounded_not_free_text(self):
        """카운터 cardinality — `code`는 SDK의 경계 있는 값이어야 한다."""
        from firebase_admin import exceptions

        result = _run(_raiser(exceptions.UnavailableError("503", cause=None)))
        self.assertTrue(result.code)
        self.assertNotIn("503", result.code, "원문 메시지가 code로 새면 cardinality가 열린다")


class TestTransportTimeout(unittest.TestCase):
    """⛔ High 1의 나머지 절반 — SDK 자체 timeout이 없으면 flight가 오래 점유된다."""

    def test_timeout_is_below_the_verifier_deadline(self):
        from app.strict_verifier import VERIFY_DEADLINE_SECONDS

        self.assertLess(
            FIREBASE_HTTP_TIMEOUT_SECONDS,
            VERIFY_DEADLINE_SECONDS,
            "SDK timeout이 검증 전체 상한보다 길면 flight가 deadline 뒤에도 남는다",
        )

    def test_timeout_is_far_below_the_sdk_default(self):
        """SDK 기본값은 120초(`_http_client.py:50`)다."""
        self.assertLess(FIREBASE_HTTP_TIMEOUT_SECONDS, 120)

    def test_provider_does_not_touch_the_default_app(self):
        """⛔ FCM이 쓰는 전역 앱의 timeout을 바꾸면 안 된다 — 별도 이름 있는 app을 쓴다."""
        provider = FirebaseIdentityProvider(get_user=lambda uid, app=None: _FakeUser())
        self.assertIsNotNone(provider.app_name)
        self.assertNotEqual(provider.app_name, "[DEFAULT]")


if __name__ == "__main__":
    unittest.main()
