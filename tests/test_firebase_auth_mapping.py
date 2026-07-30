"""`verify_firebase_token`의 Firebase 예외 → HTTP 401/503 매핑 baseline.

ADR-039 §8.1 harness 선행 요건 (2)의 **2a**. 계획 문서가 지목한 문제: conftest가
`firebase_admin.auth`를 MagicMock으로 강제 치환해 **`except` 절이 실행조차 되지 않는다**
(`except <MagicMock 속성>` → `TypeError: catching classes that do not inherit from BaseException`).
그래서 테스트가 `verify_firebase_token`을 통째로 patch할 수밖에 없었다 —
`tests/test_free_snapshot_endpoint.py`에 그 사정을 인정하는 주석이 이미 있었다.

## 왜 예외 **계층**까지 재현해야 하나

firebase-admin v6.9.0 실측(`_token_gen.py` / `_auth_utils.py` / `exceptions.py`):

    Exception
    └── FirebaseError
        ├── InvalidArgumentError
        │   └── InvalidIdTokenError
        │       ├── ExpiredIdTokenError      ← Invalid의 **하위**
        │       └── RevokedIdTokenError      ← Invalid의 **하위**
        └── UnknownError
            └── CertificateFetchError

`Expired`·`Revoked`가 `Invalid`의 하위라 `verify_firebase_token`의 except 순서
(Revoked → Expired → Invalid)가 **load-bearing**이다. Invalid를 앞으로 옮기면 셋 다 "Invalid token"이
된다. stub이 이들을 **형제**로 만들면 그 회귀가 통과한다 — 그래서 계층 자체를 잠그는 테스트
(`TestStubFidelity`)가 매핑 테스트의 **전제**다.

## 이 파일이 보장하지 않는 것 (codex)

D5 2단계의 WS verdict(`invalid_token` / `temporarily_unavailable`)는 **여기서 검증되지 않는다**.
`app/topic_dispatcher.py`의 subscribe 경로는 아직 **토큰을 읽지 않는다** — 이 파일은 그 매핑의
필요조건(예외→의미 경계)을 잠글 뿐이고, wire verdict 번역은 2b에서 WS verifier와 함께 온다.
"""
import unittest
from unittest.mock import MagicMock, patch

import firebase_admin
from fastapi import HTTPException
from firebase_admin import auth as fb_auth
from firebase_admin import exceptions as fb_exceptions
from google.auth import exceptions as google_auth_exceptions
import requests

from app.main import verify_firebase_token


def _request(auth_header: str | None = "Bearer tok") -> MagicMock:
    request = MagicMock()
    request.headers = {} if auth_header is None else {"Authorization": auth_header}
    return request


class TestStubFidelity(unittest.TestCase):
    """stub이 실제 SDK와 어긋나면 아래 매핑 테스트가 **통째로 무의미**해진다(codex).

    firebase_admin이 로컬에 미설치라 실물과 대조할 수 없으므로, v6.9.0 소스에서 읽은
    계약을 여기 못 박는다.
    """

    def test_mro_matches_sdk(self):
        self.assertTrue(issubclass(fb_auth.ExpiredIdTokenError, fb_auth.InvalidIdTokenError))
        self.assertTrue(issubclass(fb_auth.RevokedIdTokenError, fb_auth.InvalidIdTokenError))
        self.assertTrue(issubclass(fb_auth.InvalidIdTokenError, fb_exceptions.FirebaseError))
        self.assertTrue(issubclass(fb_auth.CertificateFetchError, fb_exceptions.FirebaseError))
        # 형제로 만들면 except 순서 회귀를 못 잡는다 — 그 실패를 여기서 먼저 드러낸다
        self.assertFalse(issubclass(fb_auth.CertificateFetchError, fb_auth.InvalidIdTokenError))

    def test_module_identity_is_shared(self):
        """`firebase_admin.auth`와 `firebase_admin.exceptions`가 같은 루트를 공유해야 한다.

        따로 만들면 `except FirebaseError`(fcm.py)와 `except auth.X`(main.py)가 서로 다른
        계층을 보게 되어, 한쪽 테스트가 다른 쪽을 보장하지 못한다.
        """
        self.assertIs(firebase_admin.auth, fb_auth)
        self.assertIs(firebase_admin.exceptions, fb_exceptions)
        self.assertTrue(issubclass(fb_auth.InvalidIdTokenError, fb_exceptions.FirebaseError))

    def test_constructor_arity_matches_sdk(self):
        """SDK 생성자 시그니처가 서로 다르다 — stub이 `*args`로 뭉개면 실물과 어긋난다."""
        fb_auth.RevokedIdTokenError("revoked")                       # (message)
        fb_auth.ExpiredIdTokenError("expired", None)                 # (message, cause)
        fb_auth.CertificateFetchError("cert", None)                  # (message, cause)
        fb_auth.InvalidIdTokenError("invalid")                       # (message, cause=None, ...)

    def test_code_attribute_matches_sdk(self):
        """`FirebaseError.code` — fcm.py가 catch 직후 읽는다. 없으면 그 분기가 검증 불가."""
        self.assertEqual(fb_auth.InvalidIdTokenError("x").code, "INVALID_ARGUMENT")
        self.assertEqual(fb_auth.RevokedIdTokenError("x").code, "INVALID_ARGUMENT")
        self.assertEqual(fb_auth.CertificateFetchError("x", None).code, "UNKNOWN")
        self.assertEqual(fb_exceptions.FirebaseError("UNREGISTERED", "gone").code, "UNREGISTERED")

    def test_google_transport_error_is_real_exception(self):
        self.assertTrue(issubclass(google_auth_exceptions.TransportError, BaseException))


class _MappingBase(unittest.IsolatedAsyncioTestCase):
    async def _verify(self, *, raises=None, header="Bearer tok", initialized=True,
                      returns=None, check_revoked=False):
        verify_id_token = MagicMock()
        if raises is not None:
            verify_id_token.side_effect = raises
        else:
            verify_id_token.return_value = returns if returns is not None else {"uid": "u1"}
        with patch.object(fb_auth, "verify_id_token", verify_id_token), \
             patch("app.main.is_firebase_initialized", return_value=initialized):
            result = await verify_firebase_token(_request(header), check_revoked=check_revoked)
        return result, verify_id_token

    async def _status(self, **kwargs) -> tuple[int, str]:
        with self.assertRaises(HTTPException) as ctx:
            await self._verify(**kwargs)
        return ctx.exception.status_code, ctx.exception.detail


class TestFirebaseExceptionMapping(_MappingBase):
    """예외 → 401(인증 실패) / 503(판정 불가) 의미 경계."""

    async def test_revoked_is_401(self):
        self.assertEqual(await self._status(raises=fb_auth.RevokedIdTokenError("r")),
                         (401, "Token has been revoked"))

    async def test_expired_is_401(self):
        self.assertEqual(await self._status(raises=fb_auth.ExpiredIdTokenError("e", None)),
                         (401, "Token expired"))

    async def test_invalid_is_401(self):
        self.assertEqual(await self._status(raises=fb_auth.InvalidIdTokenError("i")),
                         (401, "Invalid token"))

    async def test_certificate_fetch_is_503(self):
        """인증서 조회 실패는 **인증 실패가 아니라 판정 불가**다 — 401로 바꾸면 정상 토큰이 거부된다."""
        self.assertEqual(await self._status(raises=fb_auth.CertificateFetchError("c", None)),
                         (503, "Firebase auth unavailable"))

    async def test_transport_error_is_503(self):
        self.assertEqual(await self._status(raises=google_auth_exceptions.TransportError("t")),
                         (503, "Firebase auth unavailable"))

    async def test_requests_exception_is_503(self):
        self.assertEqual(await self._status(raises=requests.exceptions.RequestException("net")),
                         (503, "Firebase auth unavailable"))

    async def test_non_firebase_exception_is_401_fail_closed(self):
        """⚠️ 범위를 **비-Firebase 예외**로 좁힌다(codex).

        `check_revoked=True` 경로는 SDK 계약상 `UserDisabledError`/`TenantIdMismatchError` 등
        Firebase 계열 오류도 낼 수 있고, 그중 서비스 장애성 오류를 generic 401로 고정하면
        1C의 "판정 불가 → temporarily_unavailable"과 충돌한다. **그 정책은 2b에서 결정**한다.
        여기서는 SDK와 무관한 예외의 현행 fail-closed만 잠근다.
        """
        self.assertEqual(await self._status(raises=ValueError("boom")),
                         (401, "Token verification failed"))


class TestPreflightAndSuccess(_MappingBase):
    """토큰 검증 **이전** 단계 + 성공 경로."""

    async def test_uninitialized_is_503(self):
        self.assertEqual(await self._status(initialized=False), (503, "Firebase not initialized"))

    async def test_missing_header_is_401(self):
        self.assertEqual(await self._status(header=None),
                         (401, "Missing or invalid Authorization header"))

    async def test_wrong_scheme_is_401(self):
        self.assertEqual(await self._status(header="Basic dXNlcjpwdw=="),
                         (401, "Missing or invalid Authorization header"))

    async def test_empty_bearer_token_is_delegated_to_sdk(self):
        """⚠️ 현행 동작 기록 — 빈 토큰을 **로컬에서 거부하지 않고** SDK로 넘긴다.

        `startswith("Bearer ")`만 보므로 `"Bearer "`는 통과하고 빈 문자열이 전달된다.
        (실서비스에서는 SDK가 거부한다. 여기서 잠그는 건 "누가 거부하는가"의 현행 경계다.)
        """
        _, verify_id_token = await self._verify(header="Bearer ")
        self.assertEqual(verify_id_token.call_args.args[0], "")

    async def test_success_returns_uid_and_forwards_check_revoked(self):
        result, verify_id_token = await self._verify(returns={"uid": "abc"}, check_revoked=True)
        self.assertEqual(result, "abc")
        self.assertIs(verify_id_token.call_args.kwargs["check_revoked"], True)

    async def test_check_revoked_defaults_false(self):
        _, verify_id_token = await self._verify()
        self.assertIs(verify_id_token.call_args.kwargs["check_revoked"], False)


class TestFcmFirebaseErrorBranch(unittest.IsolatedAsyncioTestCase):
    """stub의 `.code`가 실제로 쓸모 있는지 — 없으면 이 분기는 여전히 검증 불가다(codex).

    `fcm.py`는 `except FirebaseError` 직후 `e.code`를 읽어 무효 토큰을 판정한다.
    `class _FirebaseError(Exception): pass` 수준의 stub은 catch만 실행 가능하게 할 뿐
    `hasattr(e, 'code')`가 False라 UNREGISTERED 분기에 도달하지 못한다.
    """

    async def test_unregistered_token_is_not_retried(self):
        """⚠️ 게이트는 `is_firebase_initialized()`가 아니라 **모듈 전역 `_firebase_initialized`**다.

        초안은 helper를 patch했는데 `send_fcm_notification`은 그 helper를 **호출하지 않는다**
        (`if not _firebase_initialized:` → `init_firebase()`). 그런데도 로컬에서는 통과했다 —
        gitignore된 `firebase-service-account.json`이 **개발자 머신에만 있어** `init_firebase()`가
        성공했기 때문이다. CI에는 그 파일이 없어 early return하고 `send`가 0회 호출됐다.
        → 테스트가 **로컬 인증 파일 존재 여부에 의존**하고 있었다. 실제 게이트를 patch해 환경 독립으로 만든다.
        """
        from app.notifications import fcm
        send = MagicMock(side_effect=fb_exceptions.FirebaseError("UNREGISTERED", "gone"))
        with patch.object(fcm, "messaging", MagicMock(send=send)), \
             patch.object(fcm, "_firebase_initialized", True):
            success, error = await fcm.send_fcm_notification("tok", "t", "b")
        self.assertFalse(success)
        self.assertEqual(send.call_count, 1, "무효 토큰은 재시도 무의미 — 1회로 끝나야 한다")
        self.assertIn("UNREGISTERED", str(error))


class TestStubHierarchyFidelityForWsMapping(unittest.TestCase):
    """stub 의 **형제 관계**를 잠근다 — 계층이 틀리면 WS 매핑 테스트가 전부 가짜 green 이 된다.

    근거: firebase-admin **6.9.0 원문**(requirements.lock.txt 의 pin)에서 직접 확인했다.
    로컬에는 firebase_admin 이 설치돼 있지 않아 **stub 계층이 곧 테스트의 진실**이므로,
    그 계층이 실물과 어긋나면 매핑이 의미를 잃는다.

        _auth_utils.py:331  InvalidIdTokenError(exceptions.InvalidArgumentError)
        _auth_utils.py:399  UserDisabledError(exceptions.InvalidArgumentError)   ← 형제
        _auth_utils.py:356  UserNotFoundError(exceptions.NotFoundError)
        _auth_utils.py:390  ConfigurationNotFoundError(exceptions.NotFoundError) ← 형제
        _token_gen.py:435   ExpiredIdTokenError(InvalidIdTokenError)             ← 하위
        _token_gen.py:442   RevokedIdTokenError(InvalidIdTokenError)             ← 하위
    """

    def test_user_disabled_is_a_sibling_of_invalid_id_token(self):
        from firebase_admin import auth

        self.assertFalse(
            issubclass(auth.UserDisabledError, auth.InvalidIdTokenError),
            "stub 이 형제를 하위로 만들었다 — `except InvalidIdTokenError` 가 계정 비활성화를 "
            "잡아 버려 매핑 테스트가 가짜 green 이 된다",
        )

    def test_configuration_not_found_is_a_sibling_of_user_not_found(self):
        from firebase_admin import auth

        self.assertFalse(
            issubclass(auth.ConfigurationNotFoundError, auth.UserNotFoundError),
            "stub 이 형제를 하위로 만들었다 — 우리 설정 결함이 '계정 없음'으로 보고된다",
        )
        self.assertTrue(
            issubclass(auth.ConfigurationNotFoundError, auth.NotFoundError),
            "부모 관계가 없으면 NotFound 계열 절이 아무것도 잡지 않는다",
        )

    def test_expired_and_revoked_are_subclasses_of_invalid(self):
        """이 관계가 있어야 WS 가 세 타입을 **한 절**로 접을 수 있다(순서 무관)."""
        from firebase_admin import auth

        for name in ("ExpiredIdTokenError", "RevokedIdTokenError"):
            with self.subTest(name=name):
                self.assertTrue(issubclass(getattr(auth, name), auth.InvalidIdTokenError))

if __name__ == "__main__":
    unittest.main()
