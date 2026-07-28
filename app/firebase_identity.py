"""Firebase identity authority adapter — A6 검증기의 `identity_provider`.

계약 근거: `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A6-1 / A6-2 + `app/strict_verifier.py`.

`auth.get_user(uid)` 한 번을 `IdentityFound | IdentityNotFound | IdentityUnavailable |
IdentityMisconfigured` 로 접는다. 판정은 하지 않는다 — 그건 `derive_verdict`의 일이다.

## 함정 (전부 firebase-admin==6.9.0 **소스에서 직접 확인**)

1. **`UserNotFoundError`는 404가 아니라 빈 200 body에서도 난다**
   `_user_mgt.py:599` `if not body or not body.get('users'): raise UserNotFoundError`.
   중간 프록시가 `{}`를 200으로 돌려주는 60초 동안 재접속하는 **전 사용자**가
   `account_deleted`로 축출되고, 그 부정 관측이 `IDENTITY_NEGATIVE_RECHECK_SECONDS`(300s)
   동안 캐시된다. premium 쪽이 malformed 200을 `ProtocolViolation`으로 따로 빼는 것과 같은 위험이다.
   ⚠️ 이 계층에서 "정말 삭제됐는가"는 **원리적으로 확인 불가**하다 — 그래서 타입을 더 만들지 않고,
   `NotFound` 발생률 급증을 **운영 경보**로 잡는 것이 맞다(대량 NotFound = 장애이지 대량 탈퇴가 아니다).

2. **`ConfigurationNotFoundError` / `TenantNotFoundError`가 `UserNotFoundError`의 형제**다
   (셋 다 `exceptions.NotFoundError` 직계). `except NotFoundError → NotFound`로 접으면
   **프로젝트 오설정이 "플랫폼 전 계정 삭제"**가 된다. 그래서 `UserNotFoundError`만 **정확히**
   먼저 잡고, 나머지 `NotFoundError`는 오설정으로 보낸다.

3. **`google.auth` 자격증명 예외는 `FirebaseError`가 아니다**
   `RefreshError` / `DefaultCredentialsError` / `TransportError`는 `GoogleAuthError` 직계라
   `except FirebaseError`만 쓰면 **서비스 계정 키 폐기가 통째로 새어 나간다**.

4. **`validate_uid`는 평범한 `ValueError`를 낸다**
   `derive_verdict`의 축 가드와 **타입이 겹쳐** 그대로 새면 "우리 코드 버그"로 오분류된다.

5. **transport 기본 timeout이 120초**(`_http_client.py:50`)
   검증기의 전체 deadline(8초)보다 훨씬 길다. 낮추지 않으면 멈춘 호출이 flight를 오래 점유하고
   그 키는 그동안 계속 `temporarily_unavailable`이다 — 호출자 쪽 상한은 **스레드를 멈추지 못한다**.
   ⚠️ `httpTimeout`은 `initialize_app`에서만 준다. FCM이 쓰는 **기본 app을 건드리지 않도록**
   이름 있는 별도 app을 쓴다(`get_user(uid, app=...)`).
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.strict_verifier import (
    IdentityFound,
    IdentityMisconfigured,
    IdentityNotFound,
    IdentityResult,
    IdentityUnavailable,
)

# 검증기의 전체 deadline(8초)보다 **작아야** 한다 — 그래야 멈춘 호출이 deadline 뒤까지 flight를
# 붙들지 않는다. SDK 기본값 120초는 두 자릿수 배 크다.
FIREBASE_HTTP_TIMEOUT_SECONDS = 5

# FCM이 쓰는 기본 app과 **분리**한다. 기본 app의 timeout을 바꾸면 푸시 발송 동작까지 바뀐다.
STRICT_AUTH_APP_NAME = "fxi-strict-auth"


class FirebaseIdentityProvider:
    """`await provider(uid)` → `IdentityResult`.

    `get_user`를 주입받는다 — 이 모듈이 `firebase_admin`을 최상위에서 import하면 단위 테스트가
    import chain으로 깨진다(리포 관용구, `app/entitlements.py:3`). 주입이 없으면 첫 호출에서
    lazy로 실물을 묶는다.
    """

    def __init__(self, *, get_user=None, app_name: str = STRICT_AUTH_APP_NAME) -> None:
        self._get_user = get_user
        self.app_name = app_name

    async def __call__(self, uid: str) -> IdentityResult:
        get_user = self._get_user or _lazy_get_user(self.app_name)
        try:
            # ⚠️ 동기 SDK다. `to_thread`의 실제 작업은 취소되지 않으므로 **상한은 SDK 쪽**에 있어야
            # 한다(위 함정 5). 여기 `wait_for`를 얹어도 스레드는 안 멈춘다.
            user = await asyncio.to_thread(get_user, uid)
        except BaseException as exc:  # noqa: BLE001 — 아래에서 버킷별로 되던지거나 접는다
            return _classify(exc)
        return IdentityFound(
            disabled=bool(user.disabled),
            # ⚠️ SDK가 이미 ms다(`_user_mgt.py`: `1000 * int(valid_since)`). 다시 곱하면
            # **모든 토큰이 revoked로 오판**된다.
            tokens_valid_after_ms=int(user.tokens_valid_after_timestamp),
        )


def _classify(exc: BaseException) -> IdentityResult:
    """예외 → 버킷. **순서가 계약이다.**"""
    from firebase_admin import auth, exceptions

    # (1) 계정 없음은 **정확히 `UserNotFoundError`만**. 형제를 여기 넣으면 오설정이 "전 계정 삭제"가 된다.
    if isinstance(exc, auth.UserNotFoundError):
        return IdentityNotFound()

    # (2) 나머지 NotFoundError(프로젝트·테넌트 오설정, 파싱 불가 404)는 **영구 결함**이다.
    if isinstance(exc, exceptions.NotFoundError):
        return IdentityMisconfigured(code=_code_of(exc))

    # (3) 권한 상실 = 영구. 재시도해도 낫지 않는다.
    if isinstance(exc, exceptions.PermissionDeniedError):
        return IdentityMisconfigured(code=_code_of(exc))

    # (4) rate limit은 4xx지만 **transient**다 — 영구로 접으면 백오프로 나을 것을 영영 못 쓴다.
    if isinstance(exc, exceptions.ResourceExhaustedError):
        return IdentityUnavailable(code=_code_of(exc))

    # (5) 그 밖의 FirebaseError는 transient로 본다(5xx·네트워크).
    if isinstance(exc, exceptions.FirebaseError):
        return IdentityUnavailable(code=_code_of(exc))

    # (6) google.auth — **FirebaseError가 아니다**. 자격증명은 영구, 전송은 transient.
    google_auth_exceptions = _google_auth_exceptions()
    if google_auth_exceptions is not None:
        if isinstance(exc, google_auth_exceptions.TransportError):
            return IdentityUnavailable(code="TRANSPORT_ERROR")
        if isinstance(
            exc,
            (google_auth_exceptions.RefreshError, google_auth_exceptions.DefaultCredentialsError),
        ):
            return IdentityMisconfigured(code="CREDENTIALS_ERROR")

    # (7) `validate_uid`의 `ValueError` — `derive_verdict`의 축 가드와 타입이 겹치므로
    #     여기서 **반드시** 접는다. 안 접으면 우리 코드 버그로 오분류된다.
    if isinstance(exc, ValueError):
        return IdentityMisconfigured(code="INVALID_UID")

    # (8) 모르는 예외는 **삼키지 않는다**. transient로 접으면 진짜 버그가 영원히 재시도된다.
    raise exc


def _code_of(exc) -> str:
    """SDK의 경계 있는 `code`. 원문 메시지를 쓰면 카운터 cardinality가 열린다."""
    code = getattr(exc, "code", None)
    return str(code) if code else type(exc).__name__


def _google_auth_exceptions():
    try:
        from google.auth import exceptions as google_auth_exceptions
    except ImportError:  # pragma: no cover - 배포 환경엔 항상 있다
        return None
    return google_auth_exceptions


def _lazy_get_user(app_name: str):
    """실물 SDK 바인딩. **함수 안에서** import한다(리포 관용구)."""

    def _get_user(uid: str):
        import firebase_admin
        from firebase_admin import auth

        try:
            app = firebase_admin.get_app(app_name)
        except ValueError:
            # ⚠️ 기본 app을 재사용하지 않는다 — FCM의 timeout까지 바뀐다.
            app = firebase_admin.initialize_app(
                _default_credential(),
                {"httpTimeout": FIREBASE_HTTP_TIMEOUT_SECONDS},
                name=app_name,
            )
        return auth.get_user(uid, app=app)

    return _get_user


def _default_credential():
    import firebase_admin

    return firebase_admin.get_app().credential
