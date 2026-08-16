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
import logging
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

    async def test_unknown_exception_is_reraised_not_401(self):
        """S1b-0 — 미뤄뒀던 정책의 **결정**이다.

        이 자리에는 `ValueError → 401 "Token verification failed"` 가 있었고, 그 docstring 이
        "그 정책은 2b에서 결정한다" 라고 명시적으로 유예했다. 결정: **재전파(→500)**.

        401 은 클라에 "네 토큰이 잘못됐다"는 **거짓**을 준다 — 우리 버그는 재인증으로 낫지 않고,
        운영 신호도 지워진다. `CLAUDE.md` 의 `500(서버 처리 오류)` 계약이 맞다. 접근이 거부된다는
        **결과는 401 과 동일**하므로 보안이 느슨해지지 않는다.
        """
        with self.assertRaises(ValueError):
            await self._verify(raises=ValueError("boom"))

    async def test_missing_uid_in_payload_is_reraised(self):
        """검증은 성공했는데 payload 에 `uid` 가 없다 = 우리/공급자 계약 위반이지 자격 실패가 아니다."""
        with self.assertRaises(KeyError):
            await self._verify(returns={"not_uid": "x"})

    async def test_cancellation_propagates(self):
        """취소는 HTTP 상태로 접히지 않는다.

        ⚠️ 이 단언 **하나로는** `except Exception` → `BaseException` 변이를 잡지 못한다(실측:
        SURVIVED). 넓혀도 분류기가 `None` 을 주고 `raise` 가 그대로 올려서 **관측 동등**이기
        때문이다. 이 테스트가 무는 것은 *조합* 이다 — 누군가 unknown 삼키기를 되살리고 catch 를
        `BaseException` 으로 넓히면 그때 취소가 401 이 된다. catch 경계 자체는 아래
        `TestCatchBoundaryIsStructural` 이 구조로 잠근다.
        """
        import asyncio

        with self.assertRaises(asyncio.CancelledError):
            await self._verify(raises=asyncio.CancelledError())


class TestLogWiring(_MappingBase):
    """분류기가 **돌려준** category·severity 를 helper 가 실제로 쓰는지.

    ⛔ 분류기 반환 tuple 만 단언하면 배선이 통째로 망가져도 초록이다(codex): 항상 WARNING 을
       쓰거나 · category 를 상수로 박거나 · `extra` 에서 빼거나 · 401 계열까지 로그를 남겨
       공격자가 로그를 증폭시켜도 잡히지 않는다. 여기서는 `logger.log` 를 붙잡아 **실제 인자**를 본다.
    """

    async def _logged(self, exc) -> list[tuple]:
        calls: list[tuple] = []
        with patch("app.main.logger") as log:
            log.log.side_effect = lambda *a, **kw: calls.append((a, kw))
            with self.assertRaises(HTTPException):
                await self._verify(raises=exc)
        return calls

    # ⛔ 로그를 남기는 rung 은 **하나도 빠짐없이** 여기 있어야 결속이 닫힌다. 처음엔 아래 셋이
    #    빠져 있었고(codex), 그러면 "UserDisabled 의 log_level 을 None 으로" · "google_auth 만
    #    WARNING 으로" · "network 만 category 변경" 변이가 **생존한다**. 빠짐 자체는 사람이 못
    #    지키므로 `test_every_logging_rung_is_in_the_table` 이 기계로 검문한다.
    LOG_CASES = [
        (lambda: fb_auth.UserNotFoundError("gone"), logging.WARNING, "user_lookup_indeterminate"),
        (lambda: fb_auth.ConfigurationNotFoundError("cfg"), logging.ERROR, "firebase_not_found"),
        (lambda: fb_exceptions.UnauthenticatedError("key"), logging.ERROR, "firebase_server_credential"),
        (lambda: fb_exceptions.PermissionDeniedError("perm"), logging.ERROR, "firebase_server_credential"),
        (lambda: fb_auth.CertificateFetchError("cert", None), logging.WARNING, "certificate_fetch"),
        (lambda: fb_exceptions.UnavailableError("down"), logging.WARNING, "firebase_error"),
        (lambda: fb_exceptions.DeadlineExceededError("slow"), logging.WARNING, "firebase_error"),
        (lambda: fb_auth.UserDisabledError("disabled"), logging.WARNING, "user_disabled"),
        (lambda: google_auth_exceptions.RefreshError("refresh"), logging.ERROR, "google_auth"),
        (lambda: google_auth_exceptions.TransportError("t"), logging.ERROR, "google_auth"),
        (lambda: requests.exceptions.RequestException("net"), logging.WARNING, "network"),
    ]

    async def test_every_emitting_category_is_represented_in_the_table(self):
        """새 **emitting category** 가 표 없이 추가되면 여기서 걸린다.

        ⚠️ 증명 범위를 이름보다 넓게 말하지 않는다(codex): 이 검사는 "모든 logging rung" 이
           아니라 **모든 emitting category 가 표에 최소 한 번 있다** 를 증명한다. 새 branch 가
           기존 category 를 재사용하면 여기서는 안 잡힌다 — 그건 결함이 아니라 경계다. branch
           별 의미는 `LOG_CASES` 와 부모 확장 테스트가 진다.

        ⛔ **프로브를 표에서 뽑으면 안 된다.** 처음엔 `LOG_CASES` 로 프로브를 만들었는데, 그러면
           표에서 한 줄을 지울 때 프로브에서도 함께 사라져 **영원히 통과한다**(실측 SURVIVED).
           자기참조 검사기는 검사기가 아니다. 그래서 기대집합을 **구현 소스에서** 도출한다 —
           분류기의 4-튜플 `return` 중 `log_level` 이 `None` 이 아닌 것들의 category 리터럴.
        """
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("app/main.py").read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_classify_rest_firebase_auth_failure")
        emitting = set()
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)):
                continue
            elts = node.value.elts
            if len(elts) != 4:
                continue
            category, log_level = elts[2], elts[3]
            is_none = isinstance(log_level, ast.Constant) and log_level.value is None
            if not is_none and isinstance(category, ast.Constant):
                emitting.add(category.value)
        self.assertTrue(emitting, "분류기에서 category 를 하나도 못 읽었다 — 검사기가 죽었다")
        self.assertEqual(emitting - {c for _, _, c in self.LOG_CASES}, set(),
                         "로그를 남기는 rung 이 표에 없다")

    async def test_severity_and_category_come_from_the_verdict(self):
        for make, level, category in self.LOG_CASES:
            exc = make()
            with self.subTest(exc=type(exc).__name__):
                calls = await self._logged(exc)
                self.assertEqual(len(calls), 1, "정확히 한 번 남겨야 한다")
                args, kwargs = calls[0]
                self.assertEqual(args[0], level, "severity 가 verdict 에서 오지 않는다")
                self.assertEqual(kwargs["extra"]["category"], category)
                self.assertEqual(kwargs["extra"]["exc_type"], type(exc).__name__)

    async def test_invalid_token_family_is_not_logged(self):
        """⛔ 만료·무효·revoke 는 **정상 사건**이다. 로그를 남기면 공격자가 로그를 증폭시킨다.

        ⚠️ "자격 실패는 로그 안 남긴다" 로 부르면 **거짓**이다(codex) — `UserDisabledError` 도
           401 자격 실패지만 WARNING 을 남긴다. 범위는 `InvalidIdTokenError` **계열**뿐이다.
        """
        for exc in (fb_auth.RevokedIdTokenError("r"),
                    fb_auth.ExpiredIdTokenError("e", None),
                    fb_auth.InvalidIdTokenError("i")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(await self._logged(exc), [], "이 계열은 로그를 남기지 않는다")

    async def test_exception_message_never_reaches_the_log(self):
        """⛔ `UserNotFoundError` 메시지는 **uid 를 본문에 담는다**. 구 코드의 `error=str(e)` 가 그것을 흘렸다."""
        secret = "uid-1234567890-should-not-appear"
        calls = await self._logged(fb_auth.UserNotFoundError(f"No user record found: {secret}"))
        self.assertEqual(len(calls), 1)
        self.assertNotIn(secret, repr(calls[0]))


class TestCatchBoundaryIsStructural(unittest.TestCase):
    """`verify_firebase_token` 의 catch 경계를 **구조로** 잠근다.

    ⛔ 행동 단언으로는 불가능하다 — unknown 재전파가 있는 한 `Exception` 과 `BaseException` 은
       관측 동등이다(실측). 그렇다고 계약이 없는 것은 아니다: 미래에 삼키기가 되살아나면 그 폭이
       곧 취소 삼키기가 된다. 관측 불가능한 계약은 **소스로** 잠그는 게 정직하다.
    """

    def _handler_types(self) -> list[str]:
        # ⛔ `inspect.getsource` + `cleandoc` 은 본문 들여쓰기를 망가뜨린다(실측). 파일을 통째
        #    파싱해 함수 노드를 집는다 — 재작성에도 안 깨지고 이름으로만 결속된다.
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path("app/main.py").read_text())
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "verify_firebase_token"
        )
        # ⛔ 함수 안 **모든** try 를 세지 않는다 — 나중에 무관한 try 가 생기면 이 단언이
        #    엉뚱하게 깨지거나(더 나쁘게) 겨냥이 흐려진다. `verify_id_token` 호출을 **포함한**
        #    try 만 집는다(codex).
        targets = [
            node for node in ast.walk(fn)
            if isinstance(node, ast.Try)
            and any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "verify_id_token"
                for c in ast.walk(node)
            )
        ]
        self.assertEqual(len(targets), 1, "verify_id_token 을 감싼 try 가 정확히 하나여야 한다")
        return [
            ast.unparse(h.type) if h.type is not None else "<bare except>"
            for h in targets[0].handlers
        ]

    def test_helper_catches_exception_not_base_exception(self):
        handlers = self._handler_types()
        self.assertEqual(handlers, ["Exception"], f"catch 경계가 바뀌었다: {handlers}")


class TestRestLadderRungsAddedByS1b0(_MappingBase):
    """S1b-0 이 새로 세운 절들.

    ⚠️ **도달 경로를 정확히 적는다**(codex 교정). v6.9.0 `_token_gen.py` `verify()` 실측상
    `check_revoked=False` 는 `ValueError` · `Invalid` · `Expired` · `CertificateFetchError` **만**
    던진다. 아래 Firebase 인프라 계열(`UserNotFound` / 기타 `NotFound` / `Unauthenticated` /
    `PermissionDenied` / `Unavailable` / `DeadlineExceeded` / `RefreshError`)은 `check_revoked=True`
    가 여는 `accounts:lookup` 에서 온다 — 즉 오늘 실제 도달 경로는 **`DELETE /api/user/me` 하나**다.
    "장애 중 전 사용자가 401 을 받는다" 는 과장이었다.

    그래도 교정 가치는 셋이다: ① 그 하나가 App Store 계정 삭제 경로다 ② 21곳 전부에서 우리
    버그가 거짓 401 대신 500 이 된다 ③ S1b 가 named app 으로 timeout 을 낮추면 이 계열의
    도달 빈도가 오르므로 **선행 조건**이다.

    ⛔ 아래 503 들은 **status 만 보면 서로 구별되지 않는다**. 절을 위아래로 옮기는 변이가
       생존하므로 `category` 와 로그 severity 까지 함께 단언한다(codex).
    """

    async def _verdict(self, exc) -> tuple[int, str, str, int | None]:
        from app.main import _classify_rest_firebase_auth_failure

        verdict = _classify_rest_firebase_auth_failure(exc)
        self.assertIsNotNone(verdict, f"{type(exc).__name__} 이 분류되지 않았다")
        return verdict

    async def test_accounts_lookup_unavailable_is_503(self):
        """**이 슬라이스의 핵심 결함**이다.

        SDK 는 requests 오류를 `FirebaseError` 로 **감싸므로** 기존 `RequestException` 절에
        절대 걸리지 않았고, 마지막 `except Exception → 401` 로 떨어졌다.
        ⚠️ 범위: `check_revoked=True` 경로(계정 삭제)에서 도달한다 — 나머지 20곳이 아니다.
        """
        self.assertEqual(await self._status(raises=fb_exceptions.UnavailableError("down")),
                         (503, "Firebase auth unavailable"))
        self.assertEqual((await self._verdict(fb_exceptions.UnavailableError("down")))[2],
                         "firebase_error")

    async def test_deadline_exceeded_is_503(self):
        """S1b 가 REST timeout 을 낮추면 **빈도가 오르는** 타입이다 — 그래서 분류 교정이 선행이다."""
        self.assertEqual(await self._status(raises=fb_exceptions.DeadlineExceededError("slow")),
                         (503, "Firebase auth unavailable"))

    async def test_user_disabled_is_401_and_not_503(self):
        """terminal 이라 401 이 least-bad — 503 이면 클라가 영원히 재시도한다(WS 와 같은 판단)."""
        self.assertEqual(await self._status(raises=fb_auth.UserDisabledError("disabled")),
                         (401, "Invalid token"))
        self.assertEqual((await self._verdict(fb_auth.UserDisabledError("d")))[2], "user_disabled")

    async def test_user_not_found_is_503_and_warns(self):
        """계정 삭제와 공급자 응답 손상을 **구별할 수 없다** → 자격 오류로 접지 않는다."""
        self.assertEqual(await self._status(raises=fb_auth.UserNotFoundError("gone")),
                         (503, "Firebase auth unavailable"))
        _, _, category, level = await self._verdict(fb_auth.UserNotFoundError("gone"))
        self.assertEqual(category, "user_lookup_indeterminate")
        self.assertEqual(level, logging.WARNING, "계정 삭제는 정상 사건이라 ERROR 는 소음이다")

    async def test_sibling_not_found_is_503_but_a_different_category_and_errors(self):
        """⛔ `UserNotFound` 와 **같은 503** 이다. category·severity 가 없으면 둘을 합치는 변이가 산다.

        `ConfigurationNotFoundError` 는 우리 프로젝트 설정 결함이므로 운영자 신호(ERROR)가 필요하다.
        """
        self.assertEqual(await self._status(raises=fb_auth.ConfigurationNotFoundError("cfg")),
                         (503, "Firebase auth unavailable"))
        _, _, category, level = await self._verdict(fb_auth.ConfigurationNotFoundError("cfg"))
        self.assertEqual(category, "firebase_not_found")
        self.assertEqual(level, logging.ERROR)

    async def test_server_credential_faults_are_503_and_error(self):
        """죽은 서비스계정 키·회수된 권한 — 재시도로 낫지 않으니 운영자 신호를 낸다."""
        for exc in (fb_exceptions.UnauthenticatedError("k"), fb_exceptions.PermissionDeniedError("p")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(await self._status(raises=exc),
                                 (503, "Firebase auth unavailable"))
                _, _, category, level = await self._verdict(exc)
                self.assertEqual(category, "firebase_server_credential")
                self.assertEqual(level, logging.ERROR)

    async def test_certificate_fetch_keeps_its_own_category(self):
        """⛔ 일반 `FirebaseError` 와 **같은 503** 이지만 category 를 나눈다.

        S1b 에서 REST timeout 을 낮춘 뒤 "콜드 인증서 fetch" 를 다른 Firebase 장애와 구별할
        유일한 계측이다. 이 절을 generic 아래로 미는 변이는 status 만 보면 **생존한다**.
        """
        _, _, category, _ = await self._verdict(fb_auth.CertificateFetchError("c", None))
        self.assertEqual(category, "certificate_fetch")
        self.assertNotEqual(category, (await self._verdict(fb_exceptions.UnavailableError("u")))[2])

    async def test_refresh_error_is_503_because_the_parent_is_caught(self):
        """`RefreshError` 는 `TransportError` 의 **형제**다 — 부모(`GoogleAuthError`)를 잡아야 걸린다.

        SDK 변환 그물을 통과하므로 `FirebaseError` 도 `RequestException` 도 아니다.
        """
        self.assertTrue(issubclass(google_auth_exceptions.RefreshError,
                                   google_auth_exceptions.GoogleAuthError))
        self.assertFalse(issubclass(google_auth_exceptions.RefreshError,
                                    google_auth_exceptions.TransportError),
                         "형제여야 한다 — 하위로 만들면 부모 확장 회귀가 가짜 green 이 된다")
        self.assertEqual(await self._status(raises=google_auth_exceptions.RefreshError("refresh")),
                         (503, "Firebase auth unavailable"))

    async def test_invalid_family_stays_401_when_the_generic_rung_exists(self):
        """⛔ **순서가 load-bearing 하다.** 나열한 타입이 전부 `FirebaseError` 하위다.

        generic 절을 위로 올리면 **모든 무효 토큰이 503** 이 되어 클라는 재인증 대신 영원히
        재시도한다. 세 타입이 여전히 401 임을 한 자리에서 잠근다.
        """
        for exc, detail in ((fb_auth.RevokedIdTokenError("r"), "Token has been revoked"),
                            (fb_auth.ExpiredIdTokenError("e", None), "Token expired"),
                            (fb_auth.InvalidIdTokenError("i"), "Invalid token")):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(isinstance(exc, fb_exceptions.FirebaseError))
                self.assertEqual(await self._status(raises=exc), (401, detail))


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

class TestWsAuthAppInitialization(unittest.TestCase):
    """R4 — 인증 전용 app 이 **낮춘 httpTimeout 을 싣고** 만들어지고, **1회만** 만들어진다.

    ⛔ 지연 초기화 금지의 이유: 검증은 `asyncio.to_thread` 안에서 돌아 동시 진입 시
    `initialize_app` 이 중복 호출돼 `ValueError` 가 난다. 기동 시 1회여야 한다.
    """

    def setUp(self):
        from app.notifications import fcm

        self._fcm = fcm
        self._saved_app = fcm._ws_auth_app
        self._saved_cred = fcm._credential
        fcm._ws_auth_app = None
        fcm._credential = object()

    def tearDown(self):
        self._fcm._ws_auth_app = self._saved_app
        self._fcm._credential = self._saved_cred

    def test_named_app_carries_the_configured_http_timeout(self):
        import firebase_admin

        from app import config

        created = object()
        with patch.object(self._fcm, "init_firebase", return_value=True), \
             patch.object(firebase_admin, "get_app", side_effect=ValueError("none")), \
             patch.object(firebase_admin, "initialize_app", return_value=created) as init:
            self.assertTrue(self._fcm.init_ws_auth_app())
        self.assertIs(self._fcm.ws_auth_app(), created)
        args, kwargs = init.call_args
        self.assertEqual(
            args[1], {"httpTimeout": config.WS_AUTH_HTTP_TIMEOUT_SECONDS},
            "httpTimeout 이 실리지 않으면 transport 상한이 기본 120초 그대로다",
        )
        self.assertEqual(kwargs.get("name"), self._fcm.WS_AUTH_APP_NAME)

    def test_initialization_is_idempotent(self):
        import firebase_admin

        created = object()
        with patch.object(self._fcm, "init_firebase", return_value=True), \
             patch.object(firebase_admin, "get_app", side_effect=ValueError("none")), \
             patch.object(firebase_admin, "initialize_app", return_value=created) as init:
            self.assertTrue(self._fcm.init_ws_auth_app())
            self.assertTrue(self._fcm.init_ws_auth_app())
        self.assertEqual(init.call_count, 1, "중복 초기화는 ValueError 를 낸다")

    def test_accessor_is_none_before_initialization(self):
        """⛔ `None` 을 돌려줘야 호출부가 §8-C 프레임으로 접을 수 있다 — 예외를 던지면
        분류기가 모르는 상태가 되어 **연결이 끊긴다**."""
        self.assertIsNone(self._fcm.ws_auth_app())


if __name__ == "__main__":
    unittest.main()
