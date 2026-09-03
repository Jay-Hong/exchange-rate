"""tests 공통 setup — collection 시작 시(모든 test import 전) 환경 격리.

main.py top-level은 firebase_admin import + DB engine 연결을 한다
(memory: project_main_py_helper_placement). 어떤 test file이든 app.database를 import하기
전에 DATABASE_URL을 sqlite로 override해야 RDS 연결 시도(timeout) 없이 동작한다.

conftest.py는 pytest가 test file들보다 먼저 import하므로, 여기서 설정하면
collection 순서(알파벳)와 무관하게 app.database.engine이 sqlite로 생성된다.

기존 in-memory SQLite 테스트(writer/orchestrator 등)는 각자 engine을 만들고
app.database.SessionLocal/engine을 patch하므로 본 설정에 영향받지 않는다.
"""
import atexit
import os
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

# 1. firebase_admin chain stub (main.py import 시 실제 init/creds 회피) — force(설치 여부 무관)
#
# ⚠️ 예외 속성만은 **진짜 클래스**여야 한다 (ADR-039 §8.1 harness 선행 (2)).
#    `except <MagicMock 속성>`은 `TypeError: catching classes that do not inherit from
#    BaseException`을 내므로, MagicMock 일변도 stub에서는 `verify_firebase_token`의 6개 except와
#    `fcm.py`의 `except FirebaseError`가 **실행조차 되지 않는다** → 매핑 검증 불가.
#
# 계층까지 재현하는 이유: firebase-admin v6.9.0에서 `Expired`·`Revoked`가 `InvalidIdToken`의
# **하위**라, `verify_firebase_token`의 except 순서(Revoked → Expired → Invalid)가 load-bearing이다.
# 형제로 만들면 순서 뒤바꿈 회귀가 통과한다. `.code`는 `fcm.py`가 catch 직후 읽는다.
# (실물 대조는 tests/test_firebase_auth_mapping.py::TestStubFidelity가 담당.)


class _StubFirebaseError(Exception):
    """firebase_admin.exceptions.FirebaseError 대응 — `code`/`cause`/`http_response` 보유."""

    def __init__(self, code, message, cause=None, http_response=None):
        Exception.__init__(self, message)
        self._code, self._cause, self._http_response = code, cause, http_response

    @property
    def code(self):
        return self._code

    @property
    def cause(self):
        return self._cause

    @property
    def http_response(self):
        return self._http_response


class _StubInvalidArgumentError(_StubFirebaseError):
    def __init__(self, message, cause=None, http_response=None):
        _StubFirebaseError.__init__(self, "INVALID_ARGUMENT", message, cause, http_response)


class _StubUnknownError(_StubFirebaseError):
    def __init__(self, message, cause=None, http_response=None):
        _StubFirebaseError.__init__(self, "UNKNOWN", message, cause, http_response)


class _StubInvalidIdTokenError(_StubInvalidArgumentError):
    pass


class _StubExpiredIdTokenError(_StubInvalidIdTokenError):
    def __init__(self, message, cause):          # SDK는 cause가 **필수**
        _StubInvalidIdTokenError.__init__(self, message, cause)


class _StubRevokedIdTokenError(_StubInvalidIdTokenError):
    def __init__(self, message):                  # SDK는 message 하나
        _StubInvalidIdTokenError.__init__(self, message)


class _StubNotFoundError(_StubFirebaseError):
    """firebase-admin 6.9.0 `exceptions.NotFoundError` — `FirebaseError` **직하**."""

    def __init__(self, message, cause=None, http_response=None):
        _StubFirebaseError.__init__(self, "NOT_FOUND", message, cause, http_response)


class _StubUnregisteredError(_StubNotFoundError):
    """firebase-admin 6.9.0 `messaging.UnregisteredError` — `NotFoundError` **직하**.

    ⛔ 이 계층이 hotfix 의 전제다. `.code` 는 부모를 그대로 물려받아 **"NOT_FOUND"** 이며
       generic `NotFoundError` 와 **문자열로 구분되지 않는다**. stub 이 이를 형제로 만들거나
       `.code` 를 "UNREGISTERED" 로 두면, "타입으로만 삭제 자격을 판정한다" 는 계약이
       가짜 green 이 된다 — 문자열로도 구분되는 세계에서 테스트하는 셈이기 때문이다.
    """


class _StubUserNotFoundError(_StubNotFoundError):
    """`_auth_utils.UserNotFoundError` — `NotFoundError` 직하."""


class _StubConfigurationNotFoundError(_StubNotFoundError):
    """`_auth_utils.ConfigurationNotFoundError` — `UserNotFoundError` 의 **형제**다.

    ⛔ 이 형제 관계가 매핑의 핵심이다. 부모 `NotFoundError` 로 뭉치면 **우리 프로젝트 설정
    결함**이 "계정 없음"(자격 오류)으로 보고된다. stub 이 이걸 하위로 만들면 매핑 테스트가
    전부 가짜 green 이 되므로 계층 자체를 별도 단언으로 잠근다.
    """


class _StubUserDisabledError(_StubInvalidArgumentError):
    """`_auth_utils.UserDisabledError` — `InvalidIdTokenError` 의 **형제**다.

    둘 다 `InvalidArgumentError` 직하이므로 `except InvalidIdTokenError` 로는 잡히지 않는다.
    """


class _StubUnavailableError(_StubFirebaseError):
    """`exceptions.UnavailableError` — `check_revoked=True` 가 여는 lookup 실패군의 대표."""

    def __init__(self, message, cause=None, http_response=None):
        _StubFirebaseError.__init__(self, "UNAVAILABLE", message, cause, http_response)


class _StubDeadlineExceededError(_StubFirebaseError):
    """`exceptions.DeadlineExceededError` — `UnavailableError` 의 **형제**(둘 다 `FirebaseError` 직하).

    ⛔ S1b 가 REST `httpTimeout` 을 낮추면 **이 타입의 빈도가 오른다**. 그래서 "인프라 장애를
       401 로 접지 않는다" 가 S1b 의 **선행 조건**이다 — 분류가 틀린 채 timeout 을 낮추면
       잘못된 401 이 함께 늘어난다.
    """

    def __init__(self, message, cause=None, http_response=None):
        _StubFirebaseError.__init__(self, "DEADLINE_EXCEEDED", message, cause, http_response)


class _StubUnauthenticatedError(_StubFirebaseError):
    """`exceptions.UnauthenticatedError` — HTTP 401. **재시도로 낫지 않는** 서버측 자격 오류."""

    def __init__(self, message, cause=None, http_response=None):
        _StubFirebaseError.__init__(self, "UNAUTHENTICATED", message, cause, http_response)


class _StubPermissionDeniedError(_StubFirebaseError):
    """`exceptions.PermissionDeniedError` — HTTP 403. 위와 같은 부류."""

    def __init__(self, message, cause=None, http_response=None):
        _StubFirebaseError.__init__(self, "PERMISSION_DENIED", message, cause, http_response)


class _StubCertificateFetchError(_StubUnknownError):
    def __init__(self, message, cause):
        _StubUnknownError.__init__(self, message, cause)


_firebase = MagicMock()
_fb_auth = MagicMock()
_fb_exceptions = MagicMock()
_fb_auth.RevokedIdTokenError = _StubRevokedIdTokenError
_fb_auth.ExpiredIdTokenError = _StubExpiredIdTokenError
_fb_auth.InvalidIdTokenError = _StubInvalidIdTokenError
_fb_auth.CertificateFetchError = _StubCertificateFetchError
_fb_exceptions.FirebaseError = _StubFirebaseError
# ⚠️ 계층은 wire 에서 보이지 않는다 — 위 형제 관계가 틀리면 매핑 테스트가 가짜 green 이 되므로
#    `tests/test_firebase_auth_mapping.py` 의 fidelity 단언이 이 계층을 직접 잠근다.
_fb_exceptions.InvalidArgumentError = _StubInvalidArgumentError
_fb_exceptions.NotFoundError = _StubNotFoundError
_fb_exceptions.UnavailableError = _StubUnavailableError

class _StubResourceExhaustedError(_StubFirebaseError):
    """`exceptions.ResourceExhaustedError` — `.code == "RESOURCE_EXHAUSTED"`."""

    def __init__(self, message, cause=None, http_response=None):
        _StubFirebaseError.__init__(self, "RESOURCE_EXHAUSTED", message, cause, http_response)


class _StubQuotaExceededError(_StubResourceExhaustedError):
    """`messaging.QuotaExceededError` — `.code` 는 **RESOURCE_EXHAUSTED** (QUOTA_EXCEEDED 아님)."""


class _StubSenderIdMismatchError(_StubPermissionDeniedError):
    """`messaging.SenderIdMismatchError` — `.code` 는 **PERMISSION_DENIED**."""


class _StubThirdPartyAuthError(_StubUnauthenticatedError):
    """`messaging.ThirdPartyAuthError` — `.code` 는 **UNAUTHENTICATED**."""


_fb_exceptions.ResourceExhaustedError = _StubResourceExhaustedError
_fb_exceptions.DeadlineExceededError = _StubDeadlineExceededError
_fb_exceptions.UnauthenticatedError = _StubUnauthenticatedError
_fb_exceptions.PermissionDeniedError = _StubPermissionDeniedError
_fb_auth.NotFoundError = _StubNotFoundError
_fb_auth.UserNotFoundError = _StubUserNotFoundError
_fb_auth.ConfigurationNotFoundError = _StubConfigurationNotFoundError
_fb_auth.UserDisabledError = _StubUserDisabledError

# ⚠️ sys.modules 등록만으로는 부족하다: `from firebase_admin import auth`(app/main.py)는 부모
#    MagicMock의 **자동 생성 속성**을 돌려줘 sys.modules 항목과 **다른 객체**가 된다(실증).
#    반면 `from firebase_admin.exceptions import FirebaseError`(fcm.py)는 sys.modules를 탄다.
#    두 경로가 같은 객체를 보도록 부모 속성을 명시 배선한다.
_firebase.auth = _fb_auth
_firebase.exceptions = _fb_exceptions
sys.modules["firebase_admin"] = _firebase
sys.modules["firebase_admin.auth"] = _fb_auth
sys.modules["firebase_admin.exceptions"] = _fb_exceptions
for _m in ("firebase_admin.credentials", "firebase_admin.messaging"):
    _sub = MagicMock()
    sys.modules[_m] = _sub
    setattr(_firebase, _m.rsplit(".", 1)[1], _sub)

# messaging 은 MagicMock 이라 `UnregisteredError` 도 **자동 생성 속성**(MagicMock)이 된다.
# `isinstance(exc, messaging.UnregisteredError)` 는 그 상태에서 TypeError 를 던지거나,
# `fcm.is_unregistered` 의 `isinstance(unregistered, type)` 가드에 걸려 **영원히 False** 가
# 된다 — 삭제가 한 번도 일어나지 않는 세계에서 테스트하게 된다. 실제 클래스를 심는다.
_fb_messaging = sys.modules["firebase_admin.messaging"]
_fb_messaging.UnregisteredError = _StubUnregisteredError
_fb_messaging.QuotaExceededError = _StubQuotaExceededError
_fb_messaging.SenderIdMismatchError = _StubSenderIdMismatchError
_fb_messaging.ThirdPartyAuthError = _StubThirdPartyAuthError

# 1b. google.auth는 conftest가 stub하지 않아 **로컬에서 verify_firebase_token 본문 진입 자체가
#     ImportError**였다(app/main.py가 함수 안에서 import). CI(lock)에는 설치돼 있어 로컬/CI가
#     갈렸다. 설치돼 있으면 실물을 쓰고(최대 충실도), 없을 때만 stub한다 — `TransportError`가
#     진짜 예외 클래스이기만 하면 계약(→503)은 두 환경에서 동일하다.
try:  # pragma: no cover - 환경에 따라 갈림
    import google.auth.exceptions  # noqa: F401
except ImportError:
    _google = sys.modules.get("google") or MagicMock()
    _google_auth = MagicMock()
    _google_auth_exceptions = MagicMock()

    class _StubGoogleAuthError(Exception):
        pass

    class _StubTransportError(_StubGoogleAuthError):
        pass

    class _StubRefreshError(_StubGoogleAuthError):
        """`RefreshError` — `TransportError` 의 **형제**(둘 다 `GoogleAuthError` 직하).

        ⛔ 형제 관계가 계약을 만든다: `TransportError` 만 잡으면 서비스계정 토큰 갱신 실패가
           **SDK 변환 그물을 통과**한다(FirebaseError 도 requests 예외도 아니다). 부모를 잡아야
           한다. stub 이 이것을 `TransportError` **하위**로 만들면 그 회귀가 가짜 green 이 된다.
        """

    _google_auth_exceptions.GoogleAuthError = _StubGoogleAuthError
    _google_auth_exceptions.TransportError = _StubTransportError
    _google_auth_exceptions.RefreshError = _StubRefreshError
    _google_auth.exceptions = _google_auth_exceptions
    _google.auth = _google_auth
    sys.modules["google"] = _google
    sys.modules["google.auth"] = _google_auth
    sys.modules["google.auth.exceptions"] = _google_auth_exceptions

# 2. DATABASE_URL → file-backed sqlite tempfile 강제 (RDS 연결 회피).
#    sqlite:///:memory:는 connection/thread별 분리 DB → endpoint(asyncio.to_thread 새 connection)에서
#    create_all(engine) 테이블이 안 보임("no such table") → 항상 file sqlite로 강제 (in-memory도 override).
_fd, _path = tempfile.mkstemp(suffix="_pytest.db")
os.close(_fd)  # mkstemp fd 즉시 닫기 (path만 사용)
os.environ["DATABASE_URL"] = f"sqlite:///{_path}"
atexit.register(lambda: os.path.exists(_path) and os.remove(_path))


# 3. write-mode cache 기본 초기화 (incident 2026-06-21 fix 후속).
#    fix로 write-mode 미확정(_INITIAL)은 모든 FX writer/mirror가 skip(legacy v1 write 금지 — post-flip
#    v2 downgrade 방지). 대부분의 writer 테스트는 production steady-state(initialized legacy)를 가정하므로
#    각 test 전 cache를 legacy로 초기화한다. uninitialized/특정 mode를 명시 테스트하는 케이스는 자신의
#    setUp `_reset_for_test()` 또는 `snapshot` patch로 override한다(이 autouse fixture보다 나중에 적용됨).
@pytest.fixture(autouse=True)
def _default_legacy_write_mode():
    try:
        from app import atomic_write_runtime as _awr
        _awr._current = _awr.WriteModeSnapshot(
            diagnostic_effective_mode="legacy", activation_latched=False,
            enforced_action="legacy", mode_generation=0,
        )
    except Exception:
        pass
    yield
