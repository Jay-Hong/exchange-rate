# app/notifications/fcm.py
"""
Firebase Cloud Messaging (FCM) 서비스
- 푸시 알림 전송
- Firebase Admin SDK 초기화
- 재시도 정책 및 토큰 관리
"""

import asyncio
from collections import Counter
import hashlib
import logging
import time
from pathlib import Path
from typing import Optional

from app import config
import firebase_admin
from firebase_admin import credentials, messaging
from firebase_admin.exceptions import FirebaseError

from app.config import FIREBASE_CREDENTIALS_PATH


# ─────────────────────────────────────────────────────────────────────────
# 오류 분류 — **삭제 자격 ⊥ 재시도 판정**
#
# 구 코드는 문자열 집합 `['UNREGISTERED','INVALID_ARGUMENT','NOT_FOUND']` 을 4곳에서
# 반복 사용했다. 한 곳(단일 sender)은 **재시도 여부**를, 나머지 세 곳(batch sender)은
# **삭제 후보 수집**을 결정했다. 같은 기준을 서로 다른 결정에 쓴 탓에
# `INVALID_ARGUMENT`(payload 오류일 수 있음)와 generic `NOT_FOUND` 까지
# 삭제 후보로 수집되어 **살아 있는 토큰의 등록이 지워질 수 있었다**
# (운영에서 실제 발생했다는 증거는 확인하지 않았다 — 코드 경로상의 가능성이다).
#
# 문자열로는 구분할 수 없다: firebase-admin 6.9.0 에서
# `messaging.UnregisteredError <: NotFoundError <: FirebaseError` 이고
# **둘 다 `.code == "NOT_FOUND"`** 다. 따라서 pinned SDK 의 **정상 FCM 매핑 경로**에서
# `'UNREGISTERED'` 리터럴은 UnregisteredError 와 매칭되지 않고, 실제 수집은
# `'NOT_FOUND'` 로 이뤄진다 — 확정 미등록과 generic NotFound 가 같은 문으로 걸린
# 이유가 바로 이것이다. `FirebaseError(code, ...)` 를 직접 만든 custom/legacy 경로는
# 임의 code 를 넣을 수 있으므로 `'UNREGISTERED'` 가 전역적으로 불가능하다고 단정하지 않는다.
# 그래서 삭제 자격은 타입으로만 판정한다
# (`is_unregistered`). sender 종류와 무관하게 동일하다:
#
#   UnregisteredError            삭제 O   (토큰이 확정적으로 죽음)
#   InvalidArgumentError         삭제 X   (payload 오류일 수 있음 — 관측만)
#   generic NotFoundError        삭제 X   (Unregistered 아님)
#   Internal/Unavailable/…       삭제 X
#   QuotaExceeded                삭제 X
#
# 재시도는 이 hotfix 의 관심사가 아니며 기존 동작을 그대로 둔다. sender 별 차이는
# 각 함수 docstring 의 '재시도:' 줄을 볼 것 — 여기 요약해 두면 코드와 어긋난다
# (실제로 어긋났다).
# ─────────────────────────────────────────────────────────────────────────

def token_fingerprint(token: str) -> str:
    """로그 상관용 **안정적 가명 식별자**. 원문 조각을 남기지 않는다.

    구 코드의 `token[:20]` 은 원문 앞부분을 그대로 노출했다. FCM 토큰은 기기
    식별자이므로 로그·transcript·외부 반출 경로에 원문 조각을 남기지 않는다.

    ⚠️ **"비가역"이 아니다.** 키 없는 SHA-256 이므로 정상 FCM 토큰(고엔트로피)은
    복원이 현실적으로 어렵지만, 저엔트로피 입력은 후보 대입이 가능하다. 강한
    보장이 필요하면 서버 비밀키 기반 HMAC 으로 바꿔야 하며 그건 config 추가를
    동반하는 별도 작업이다. 앞 12 hex(48-bit)만 쓰므로 충돌 없음이나 전역 고유성도
    보장하지 않는다 — 이 값은 로그 상관용일 뿐 토큰 identity가 아니다.
    """
    if not isinstance(token, str) or not token:
        return "invalid"
    return "fp:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _unregistered_type():
    """`messaging.UnregisteredError` 를 있으면 돌려준다. 없으면 None.

    구버전 SDK 나 stub 에서 타입이 없을 수 있다. 그 경우 `is_unregistered()` 는
    **False** 를 돌려준다 — 즉 삭제하지 않는다(fail-closed). 문자열 code 로
    되돌아가면 generic NOT_FOUND 와 구분할 수 없으므로 그 fallback 은 두지 않는다.
    """
    return getattr(messaging, "UnregisteredError", None)


def is_unregistered(exc) -> bool:
    """이 예외가 **확정된 미등록 토큰**인가. 삭제 자격의 유일한 근거다."""
    unregistered = _unregistered_type()
    if unregistered is None or not isinstance(unregistered, type):
        return False
    return isinstance(exc, unregistered)


# 로그에 남겨도 되는 오류 코드 고정 allowlist. 형태 검사(대문자·숫자·밑줄)로는 부족하다
# — `SECRET_TOKEN_ABC123` 도 그 형태를 통과한다. 두 종류가 섞여 있다:
#
#   · platform 16 — `exceptions.py` 모듈 상수. **실제로 `.code` 로 관측되는 값**이다.
#   · fcm 5 — `messaging.py FCM_ERROR_TYPES` 의 **mapping key**. pinned SDK 의 정상
#     FCM 매핑에서는 응답 문자열을 예외 타입으로 고르는 데 쓰이고 `.code` 로는
#     아래 platform 값으로 변환된다 (6.9.0 소스 확인:
#     UNREGISTERED→NOT_FOUND / SENDER_ID_MISMATCH→PERMISSION_DENIED /
#     QUOTA_EXCEEDED→RESOURCE_EXHAUSTED / THIRD_PARTY_AUTH_ERROR·APNS_AUTH_ERROR→
#     UNAUTHENTICATED). 방어적 superset 으로만 포함한다.
_KNOWN_ERROR_CODES = frozenset({
    "ABORTED", "ALREADY_EXISTS", "APNS_AUTH_ERROR", "CANCELLED", "CONFLICT",
    "DATA_LOSS", "DEADLINE_EXCEEDED", "FAILED_PRECONDITION", "INTERNAL",
    "INVALID_ARGUMENT", "NOT_FOUND", "OUT_OF_RANGE", "PERMISSION_DENIED",
    "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED", "SENDER_ID_MISMATCH",
    "THIRD_PARTY_AUTH_ERROR", "UNAUTHENTICATED", "UNAVAILABLE", "UNKNOWN",
    "UNREGISTERED",
})


def _normalize_error_code(code) -> Optional[str]:
    """`.code` 를 **알려진 집합으로만** 접는다. 그 밖은 형태만 남긴다.

    firebase-admin 은 `Exception.code` 에 무엇이든 담을 수 있고(`exceptions.py:100`
    `self._code = code`), 우리 코드가 firebase 예외만 받는다는 보장도 없다. 임의
    문자열을 통과시키면 자유 텍스트가 구조 필드로 새는 통로가 된다.
    """
    if code is None:
        return None
    if not isinstance(code, str):
        return "non-string"
    if code in _KNOWN_ERROR_CODES:
        return code
    return "unknown"


def error_descriptor(exc) -> dict:
    """예외를 **자유 텍스트 없이** 구조화한다 (type + code).

    ⚠️ 호출부가 `logger.exception()`/`exc_info=True`를 쓰면 traceback의 SDK
       메시지에 토큰이 실릴 수 있다. 따라서 이 파일의 sender 실패
       로그는 `error_descriptor()`만 넘기고 `exc_info`를 설정하지 않는다.
    """
    chain, cur, seen = [], exc, set()
    while cur is not None and id(cur) not in seen and len(chain) < 5:
        seen.add(id(cur))
        chain.append(type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
    return {
        "error_type": chain[0] if chain else "unknown",
        "error_code": _normalize_error_code(getattr(exc, "code", None)),
        # send_each 는 미분류 예외를 UnknownError 로 감싸 다시 던진다
        # (firebase_admin/messaging.py:523). 최외곽만 보면 근본 원인 타입이 사라진다.
        "error_chain": chain[1:] or None,
    }


def classify_send_error(exc) -> str:
    """구조화 관측용 라벨. 삭제 자격은 `is_unregistered()` 만이 정한다."""
    if is_unregistered(exc):
        return "confirmed-unregistered"
    code = getattr(exc, "code", None)
    if code == "INVALID_ARGUMENT":
        return "ambiguous-retained-invalid-argument"
    if code == "NOT_FOUND":
        return "ambiguous-retained-not-found"
    if code in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"):
        return "transient-exhausted"
    # QUOTA_EXCEEDED 는 mapping key 라 `.code` 로는 오지 않는다(방어적 유지)
    if code in ("RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"):
        return "quota-exhausted"
    return "unclassified-retained"

logger = logging.getLogger("exchange_rate.notifications.fcm")

# Firebase 초기화 상태 (**DEFAULT app 전용** — named app 은 아래 `_ws_auth_app` 이 따로 추적한다)
_firebase_initialized = False

# ⚠️ credential 을 모듈에 보관한다. 구 코드는 `init_firebase` 안의 **지역 변수**였고, named app 이
#    같은 자격증명을 재사용하려면 보관이 선행돼야 한다(안 하면 Certificate 재파싱 + 토큰 캐시 이중화).
_credential = None

# WS subscribe 인증 전용 app. **왜 별 app 인가**: `httpTimeout` 은 firebase-admin 이
# `app.options` 에서 **app 단위**로 읽는다(6.9.0 `_auth_client.py:42`, `messaging.py:470` 이
# 각각 자기 app 을 본다). 그래서 인증 transport 상한을 낮춰도 FCM 발송은 영향이 없다.
WS_AUTH_APP_NAME = "ws-auth"
_ws_auth_app = None

# S1b — REST 인증 전용 app. 같은 이유(`httpTimeout` 이 app 단위)로 REST 도 자기 app 을 갖는다.
# ⛔ DEFAULT 를 낮추면 FCM 까지 낮아진다 — 그래서 DEFAULT 는 **건드리지 않는다**.
REST_AUTH_APP_NAME = "rest-auth"
_rest_auth_app = None


def init_firebase() -> bool:
    """Firebase Admin SDK 초기화"""
    global _firebase_initialized

    if _firebase_initialized:
        return True

    cred_path = Path(FIREBASE_CREDENTIALS_PATH)
    if not cred_path.exists():
        logger.warning(
            "Firebase 서비스 계정 키 파일 없음",
            extra={"path": str(cred_path)}
        )
        return False

    try:
        global _credential
        cred = credentials.Certificate(str(cred_path))
        firebase_admin.initialize_app(cred)
        _credential = cred
        _firebase_initialized = True
        logger.info("Firebase Admin SDK 초기화 완료")
        return True
    except Exception as e:
        logger.error(
            "Firebase 초기화 실패",
            extra={"error": str(e)}
        )
        return False


def init_ws_auth_app() -> bool:
    """WS subscribe 인증 전용 named app 초기화 — 낮은 `httpTimeout` 을 이 app 에만 건다.

    ⛔ **지연 초기화 금지.** 검증은 `asyncio.to_thread` 안에서 도는데, 거기서 처음 만들면 동시
    진입 시 `initialize_app` 이 중복 호출돼 `ValueError` 가 난다. 기동 시 `init_firebase()`
    직후 **1회** 부른다.

    ⚠️ 재진입 안전: 이미 있으면 `get_app` 으로 집는다(테스트가 모듈을 여러 번 초기화한다).
    """
    global _ws_auth_app

    if _ws_auth_app is not None:
        return True
    if not init_firebase() or _credential is None:
        return False
    try:
        try:
            _ws_auth_app = firebase_admin.get_app(WS_AUTH_APP_NAME)
        except ValueError:
            _ws_auth_app = firebase_admin.initialize_app(
                _credential,
                {"httpTimeout": config.WS_AUTH_HTTP_TIMEOUT_SECONDS},
                name=WS_AUTH_APP_NAME,
            )
        logger.info(
            "WS 인증 전용 Firebase app 초기화 완료",
            extra={"http_timeout": config.WS_AUTH_HTTP_TIMEOUT_SECONDS},
        )
        return True
    except Exception:
        logger.exception("WS 인증 전용 Firebase app 초기화 실패")
        _ws_auth_app = None
        return False


def ws_auth_app():
    """인증 전용 app 또는 `None`(미준비).

    ⛔ 호출부는 **`None` 을 반드시 처리**해야 한다. `is_firebase_initialized()` 는 DEFAULT app 만
    추적하므로, 그것만 보고 `get_app(WS_AUTH_APP_NAME)` 을 부르면 미준비 상태에서 `ValueError` 가
    나고 그건 분류기가 모르는 예외라 **연결이 끊긴다**(§8-C 프레임이 아니라).
    """
    return _ws_auth_app


def init_rest_auth_app() -> bool:
    """S1b — REST 인증 전용 named app. `init_ws_auth_app` 과 **같은 계약**이다.

    ⛔ 왜 DEFAULT app 을 쓰지 않는가: `httpTimeout` 은 **app 단위**라, DEFAULT 를 낮추면 FCM 등
    같은 app 을 쓰는 다른 SDK client 까지 함께 낮아진다. 결합을 끊는 방법은 named app 뿐이다.
    ⚠️ 그래도 **완전 분리는 아니다** — credential·project·Google endpoint 는 공유한다. 갈리는
       것은 verifier·인증서 client·timeout·큐 상태다.
    ⛔ 지연 초기화 금지 — 검증은 executor thread 에서 도는데 거기서 처음 만들면 동시 진입 시
       `initialize_app` 이 중복 호출돼 `ValueError` 가 난다. 기동 시 **1회** 부른다.
    """
    global _rest_auth_app

    if _rest_auth_app is not None:
        return True
    if not init_firebase() or _credential is None:
        return False
    try:
        try:
            _rest_auth_app = firebase_admin.get_app(REST_AUTH_APP_NAME)
        except ValueError:
            _rest_auth_app = firebase_admin.initialize_app(
                # ⛔ DEFAULT 와 **같은 credential** 이다 — 다른 자격을 주면 같은 프로젝트를 본다는
                #    보장이 사라진다(project id 는 credential 에서 해석된다).
                _credential,
                {"httpTimeout": config.REST_AUTH_HTTP_TIMEOUT_SECONDS},
                name=REST_AUTH_APP_NAME,
            )
        logger.info(
            "REST 인증 전용 Firebase app 초기화 완료",
            extra={"http_timeout": config.REST_AUTH_HTTP_TIMEOUT_SECONDS},
        )
        return True
    except Exception:
        logger.exception("REST 인증 전용 Firebase app 초기화 실패")
        _rest_auth_app = None
        return False


def rest_auth_app():
    """REST 인증 전용 app 또는 `None`(미준비).

    ⛔ 호출부는 **`None` 을 반드시 처리**해야 한다 — `is_firebase_initialized()` 는 DEFAULT app 만
    추적하므로 그것만 보고 이 app 이 있다고 가정하면 안 된다. `verify_firebase_token` 은 `None` 을
    **503** 으로 접는다(자격 실패가 아니라 판정 불가다).
    """
    return _rest_auth_app


def is_firebase_initialized() -> bool:
    """Firebase 초기화 상태 확인"""
    return _firebase_initialized


def _normalize_data_payload(
    title: str,
    body: str,
    data: Optional[dict] = None
) -> dict:
    """
    data 페이로드에 title/body 강제 주입 (포그라운드/백그라운드 메시지 일관성 보장)

    FCM 메시지 구조:
    - notification 페이로드: 백그라운드/종료 상태에서 시스템 트레이가 표시
    - data 페이로드: 포그라운드에서 앱이 직접 처리

    이 함수는 data에도 title/body를 포함시켜 앱 상태와 무관하게
    동일한 메시지를 표시할 수 있도록 보장함.

    Note:
        - FCM data 페이로드의 키/값은 모두 string 타입이어야 함.
          이 함수는 모든 키/값을 str()로 변환하여 타입 안전성 보장.
        - title/body가 빈 문자열("")이거나 없으면 함수 인자의 기본값으로 대체됨.

    Args:
        title: 알림 제목
        body: 알림 내용
        data: 추가 데이터 (선택)

    Returns:
        title/body가 포함된 정규화된 data 딕셔너리 (모든 값이 string)
    """
    # FCM data 페이로드는 키/값 모두 string이어야 함
    normalized = {str(k): str(v) for k, v in (data or {}).items()}
    # title/body가 이미 있고 비어있지 않으면 덮어쓰지 않음 (caller 우선)
    if not normalized.get("title"):
        normalized["title"] = title
    if not normalized.get("body"):
        normalized["body"] = body
    return normalized


async def send_fcm_notification(
    token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    max_retries: int = 2
) -> tuple[bool, Optional[str]]:
    """
    단일 기기에 FCM 푸시 알림 전송 (재시도 정책 포함)

    현재 미사용. 향후 다음 용도로 사용 예정:
    - 개별 사용자 알림 (계정 관련, 구독 만료 D-3 등)
    - 관리자가 특정 사용자에게 1:1 메시지 발송
    - FastAPI async 엔드포인트에서 직접 호출

    ⚠️ 사용 예시를 두지 않는다. 구 예시는 반환 코드 `INVALID_ARGUMENT` 로 토큰을
       삭제하라고 권했는데(존재하지 않는 `delete_invalid_token()` 호출), 그것이
       바로 아래 ⛔ 가 금지하는 오배선이다.

    ⛔ **이 함수의 반환값으로 토큰을 삭제하지 말 것.** 반환은 (bool, code) 이고
       예외 객체가 밖으로 나오지 않으므로, 호출자는 확정 미등록(UnregisteredError)과
       generic NotFoundError 를 구분할 수 없다 — **둘 다 code 가 "NOT_FOUND"** 다.
       `INVALID_ARGUMENT` 도 payload 오류일 수 있다. 토큰 정리가 필요한 경로는
       예외 타입을 직접 볼 수 있는 batch sender 를 쓰고, 그 결과를
       `crud.purge_unregistered_devices()` 로 넘긴다.
       (아래 `error_code in [...]` 는 **재시도 여부** 판정이지 삭제 자격이 아니다.)

    Args:
        token: FCM Device Token (단일)
        title: 알림 제목
        body: 알림 내용
        data: 추가 데이터 (선택)
        max_retries: 최대 재시도 횟수. 현재 고정 지연표가 `[1, 2]`라 **0..2만 안전**하다.
                     3 이상을 검증·지원하는 일반 backoff 계약은 별도 작업이다.

    Returns:
        (성공 여부, 에러 코드 또는 None)
        에러 코드: 실제로는 INVALID_ARGUMENT · NOT_FOUND 등 platform code 다
                   (정상 SDK 경로에서 UNREGISTERED 는 `.code` 로 나오지 않는다
                    — 상단 allowlist 주석 참조).
                   **비재시도**일 뿐 삭제 자격이 아니다 — 위 ⛔ 참조.
    재시도: `max_retries=2` + 1·2초 sleep 을 **유지**한다(기존 계약, production 호출 0건).
            조건문에는 비재시도 literal 3개가 있지만 pinned SDK 정상 경로에서 실효
            `.code` 는 `INVALID_ARGUMENT`·`NOT_FOUND` 2개다. 이를 뺀 **그 밖의 모든
            FirebaseError** 를 재시도한다 — 인증
            (UNAUTHENTICATED) · 권한(PERMISSION_DENIED) · quota(RESOURCE_EXHAUSTED)
            계열도 포함된다. SDK 자체 재시도 위에 얹히는 중첩 구조이며, 증폭 여부
            검토는 별도 작업이다.
    """
    if not _firebase_initialized:
        if not init_firebase():
            return False, "FIREBASE_NOT_INITIALIZED"

    # data 페이로드에 title/body 강제 주입 (포그라운드 메시지 일관성)
    normalized_data = _normalize_data_payload(title, body, data)

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=normalized_data,
        token=token,
        # iOS 설정
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(
                        title=title,
                        body=body,
                    ),
                    sound="default",
                )
            )
        ),
        # Android 설정
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                title=title,
                body=body,
                sound="default",
            )
        )
    )

    delays = [1, 2]  # 재시도 딜레이 (초)
    start_time = time.time()

    for attempt in range(max_retries + 1):
        try:
            # 동기 함수를 비동기로 실행
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, messaging.send, message
            )

            latency_ms = (time.time() - start_time) * 1000
            logger.info(
                "FCM 알림 발송 성공",
                extra={
                    "event": "fcm_send",
                    "token_fp": token_fingerprint(token),
                    "success": True,
                    "latency_ms": round(latency_ms, 2),
                    "message_id": response,
                }
            )
            return True, None

        except FirebaseError as e:
            raw_code = e.code if hasattr(e, 'code') else None
            # 재시도·반환 판정은 원문으로, **로그 기록은 정규화된 값**으로 한다
            error_code = raw_code if isinstance(raw_code, str) else type(e).__name__
            safe_code = _normalize_error_code(raw_code)

            # 비재시도 오류 (토큰 오류가 아니다 — 상단 분류 참조)
            if error_code in ['UNREGISTERED', 'INVALID_ARGUMENT', 'NOT_FOUND']:
                latency_ms = (time.time() - start_time) * 1000
                logger.warning(
                    "FCM 비재시도 오류",
                    extra={
                        "event": "fcm_send",
                        "token_fp": token_fingerprint(token),
                        "success": False,
                        "error_code": safe_code,
                        "latency_ms": round(latency_ms, 2),
                    }
                )
                return False, error_code

            # 그 밖의 모든 FirebaseError: 재시도 (인증·권한·quota 포함)
            if attempt < max_retries:
                await asyncio.sleep(delays[attempt])
                continue

            # 최대 재시도 초과
            latency_ms = (time.time() - start_time) * 1000
            logger.error(
                "FCM 전송 실패",
                extra={
                    "event": "fcm_send",
                    "token_fp": token_fingerprint(token),
                    "success": False,
                    "error_code": safe_code,
                    "latency_ms": round(latency_ms, 2),
                    "attempts": attempt + 1,
                }
            )
            return False, error_code

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            logger.error(
                "FCM 전송 예외",
                extra={
                    "event": "fcm_send",
                    "token_fp": token_fingerprint(token),
                    "success": False,
                    **error_descriptor(e),
                    "latency_ms": round(latency_ms, 2),
                }
            )
            return False, "EXCEPTION"

    return False, "MAX_RETRIES_EXCEEDED"



async def send_fcm_multicast(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None
) -> dict:
    """
    여러 기기에 FCM 알림 일괄 전송 (async 버전)

    현재 미사용. 향후 다음 용도로 사용 예정:
    - 관리자 공지 발송 (앱 업데이트, 이벤트 안내)
    - FastAPI async 엔드포인트에서 다수 사용자에게 알림
    - Firebase Console 대신 API로 공지 발송 시

    Note:
        Firebase API 제한으로 1회 호출당 최대 500개 토큰만 허용.
        500명 초과 시 배치 처리 필요:
            for i in range(0, len(all_tokens), 500):
                await send_fcm_multicast(all_tokens[i:i+500], ...)

    ⚠️ 현재 production 호출 0건이다. 사용 예시를 두지 않는다 — 전 사용자 토큰을
       한 배열로 모으는 흔한 예시 형태가 바로 아래 금지 규칙과 충돌한다.

    ⛔ **여러 사용자의 토큰을 한 배열로 합쳐 보낸 뒤 한꺼번에 정리하지 말 것.**
       `crud.purge_unregistered_devices()` 는 `owner_uid` 를 요구하는데, 평탄화된
       배열에는 소유자가 하나로 정해지지 않는다. 정리가 필요하면 **소유자별로**
       발송과 정리를 나누고 각 호출에 그 소유자의 `sent_tokens` 를 넘긴다.
       (helper 는 commit 하지 않는다 — 호출자가 기존 자리에서 commit 한다.)

    Args:
        tokens: FCM Device Token 목록 (Firebase API 제한: 최대 500개)
        title: 알림 제목
        body: 알림 내용
        data: 추가 데이터 (선택)

    Returns:
        {
            "success_count": int,
            "failure_count": int,
            "failed_tokens": list[str]  # **확정된 미등록** 토큰만 (cleanup 후보).
                                        # key 이름은 호환 위해 유지. "모든 실패" 가 아니다 —
                                        # payload 오류·일시 오류는 여기 들어오지 않는다.
        }
    재시도: 앱 레이어 재시도 없음 (one-shot). firebase-admin 이 HTTP 500/503 을
            자체 재시도하므로 그 위에 앱 루프를 얹으면 요청이 증폭된다.
            한계: SDK 재시도가 소진된 일시적 오류는 그 발송을 포기한다.
    """
    if not _firebase_initialized:
        if not init_firebase():
            return {
                "success_count": 0,
                "failure_count": len(tokens),
                # init 실패는 토큰이 죽었다는 증거가 아니다 — 다른 5개 반환점과
                # 동일하게 **삭제 후보 0** 을 돌려준다(이 경로만 어긋나 있었다).
                "failed_tokens": []
            }

    if not tokens:
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    # data 페이로드에 title/body 강제 주입 (포그라운드 메시지 일관성)
    normalized_data = _normalize_data_payload(title, body, data)

    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=normalized_data,
        tokens=tokens,
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(
                        title=title,
                        body=body,
                    ),
                    sound="default",
                )
            )
        ),
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                title=title,
                body=body,
                sound="default",
            )
        )
    )

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, messaging.send_each_for_multicast, message
        )

        failed_tokens = []          # 확정된 미등록 = cleanup 후보
        retained_reasons = []       # 삭제하지 않고 보존한 실패의 타입별 라벨
        for idx, result in enumerate(response.responses):
            if not result.success:
                # 무효 토큰만 수집
                # 타입으로만 판정한다 — `.code` 는 Unregistered 와 generic
                # NotFound 를 구분하지 못한다(둘 다 "NOT_FOUND").
                if is_unregistered(result.exception):
                    failed_tokens.append(tokens[idx])
                else:
                    retained_reasons.append(classify_send_error(result.exception))

        logger.info(
            "FCM 멀티캐스트 완료",
            extra={
                "event": "fcm_multicast",
                "total": len(tokens),
                "success": response.success_count,
                "failure": response.failure_count,
                # `invalid_tokens` 는 외부 운영 쿼리 호환을 위해 이름을 유지한다.
                # 의미는 이제 "확정된 미등록 cleanup 후보 수" 다.
                "invalid_tokens": len(failed_tokens),
                "retained_failures": len(retained_reasons),
                # 유형별 개수를 남긴다 — set 으로 접으면 "타입별 관측" 이 되지 않는다.
                "retained_by_reason": dict(Counter(retained_reasons)),
            }
        )

        return {
            "success_count": response.success_count,
            "failure_count": response.failure_count,
            "failed_tokens": failed_tokens
        }

    except Exception as e:
        logger.error(
            "FCM 멀티캐스트 예외",
            extra={"event": "fcm_multicast", **error_descriptor(e)}
        )
        return {
            "success_count": 0,
            "failure_count": len(tokens),
            "failed_tokens": []
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 동기 버전 FCM 발송 함수 (크롤러에서 사용)
# ═══════════════════════════════════════════════════════════════════════════════

def send_fcm_multicast_sync(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None
) -> dict:
    """
    여러 기기에 FCM 알림 일괄 전송 (동기 버전) - 현재 사용 중

    환율 변동 알림에 사용. 크롤러(동기 함수)에서 호출되므로 동기로 실행.
    crud.py의 process_rate_alerts()에서 호출됨.

    Note:
        Firebase API 제한으로 1회 호출당 최대 500개 토큰만 허용.
        현재 서비스 규모(200-500명)에서는 문제없음.
        500명 초과 시 crud.py에서 배치 처리 구현 필요.

    Args:
        tokens: FCM Device Token 목록 (Firebase API 제한: 최대 500개)
        title: 알림 제목
        body: 알림 내용
        data: 추가 데이터 (선택)

    Returns:
        {
            "success_count": int,
            "failure_count": int,
            "failed_tokens": list[str]  # **확정된 미등록** 토큰만 (cleanup 후보).
                                        # key 이름은 호환 위해 유지. "모든 실패" 가 아니다 —
                                        # payload 오류·일시 오류는 여기 들어오지 않는다.
        }
    재시도: 앱 레이어 재시도 없음 (one-shot). firebase-admin 이 HTTP 500/503 을
            자체 재시도하므로 그 위에 앱 루프를 얹으면 요청이 증폭된다.
            한계: SDK 재시도가 소진된 일시적 오류는 그 발송을 포기한다.
    """
    if not _firebase_initialized:
        if not init_firebase():
            return {
                "success_count": 0,
                "failure_count": len(tokens),
                "failed_tokens": []
            }

    if not tokens:
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    # data 페이로드에 title/body 강제 주입 (포그라운드 메시지 일관성)
    normalized_data = _normalize_data_payload(title, body, data)

    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=normalized_data,
        tokens=tokens,
        apns=messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(
                    alert=messaging.ApsAlert(
                        title=title,
                        body=body,
                    ),
                    sound="default",
                )
            )
        ),
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                title=title,
                body=body,
                sound="default",
            )
        )
    )

    start_time = time.time()

    try:
        # 동기 호출 (Firebase Admin SDK는 원래 동기)
        response = messaging.send_each_for_multicast(message)

        failed_tokens = []          # 확정된 미등록 = cleanup 후보
        retained_reasons = []       # 삭제하지 않고 보존한 실패의 타입별 라벨
        for idx, result in enumerate(response.responses):
            if not result.success:
                # 무효 토큰만 수집 (삭제 대상)
                # 타입으로만 판정한다 — `.code` 는 Unregistered 와 generic
                # NotFound 를 구분하지 못한다(둘 다 "NOT_FOUND").
                if is_unregistered(result.exception):
                    failed_tokens.append(tokens[idx])
                else:
                    retained_reasons.append(classify_send_error(result.exception))

        latency_ms = (time.time() - start_time) * 1000
        logger.info(
            "FCM 멀티캐스트 완료 (sync)",
            extra={
                "event": "fcm_multicast_sync",
                "total": len(tokens),
                "success": response.success_count,
                "failure": response.failure_count,
                # `invalid_tokens` 는 외부 운영 쿼리 호환을 위해 이름을 유지한다.
                # 의미는 이제 "확정된 미등록 cleanup 후보 수" 다.
                "invalid_tokens": len(failed_tokens),
                "retained_failures": len(retained_reasons),
                # 유형별 개수를 남긴다 — set 으로 접으면 "타입별 관측" 이 되지 않는다.
                "retained_by_reason": dict(Counter(retained_reasons)),
                "latency_ms": round(latency_ms, 2),
            }
        )

        return {
            "success_count": response.success_count,
            "failure_count": response.failure_count,
            "failed_tokens": failed_tokens
        }

    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.error(
            "FCM 멀티캐스트 예외 (sync)",
            extra={
                "event": "fcm_multicast_sync",
                **error_descriptor(e),
                "latency_ms": round(latency_ms, 2),
            }
        )
        return {
            "success_count": 0,
            "failure_count": len(tokens),
            "failed_tokens": []
        }


# ═══════════════════════════════════════════════════════════════════════════════
# data-only FCM 발송 함수 (사일런트 동기화용)
# ═══════════════════════════════════════════════════════════════════════════════

async def send_fcm_data_only(
    tokens: list[str],
    data: dict
) -> dict:
    """
    data-only FCM 전송 (사일런트 동기화용)

    notification 페이로드 없이 data만 전송하여 사용자에게 알림 배너가
    표시되지 않도록 함. 다중 기기 알림 설정 동기화에 사용.

    Note:
        - notification/apns alert/android notification 필드 없음
        - iOS: content_available + apns-push-type: background
        - Android: priority high만 설정

    Args:
        tokens: FCM Device Token 목록 (Firebase API 제한: 최대 500개)
        data: 전송할 데이터 (예: {"type": "sync_alerts"})

    Returns:
        {
            "success_count": int,
            "failure_count": int,
            "failed_tokens": list[str]  # **확정된 미등록** 토큰만 (cleanup 후보).
                                        # key 이름은 호환 위해 유지. "모든 실패" 가 아니다 —
                                        # payload 오류·일시 오류는 여기 들어오지 않는다.
        }
    재시도: 앱 레이어 재시도 없음 (one-shot). firebase-admin 이 HTTP 500/503 을
            자체 재시도하므로 그 위에 앱 루프를 얹으면 요청이 증폭된다.
            한계: SDK 재시도가 소진된 일시적 오류는 그 발송을 포기한다.
    """
    if not _firebase_initialized:
        if not init_firebase():
            return {
                "success_count": 0,
                "failure_count": len(tokens),
                "failed_tokens": []
            }

    if not tokens:
        return {"success_count": 0, "failure_count": 0, "failed_tokens": []}

    # FCM data 페이로드는 키/값 모두 string이어야 함
    normalized_data = {str(k): str(v) for k, v in data.items()}

    # data-only 메시지 (notification 필드 없음 = 사일런트)
    message = messaging.MulticastMessage(
        data=normalized_data,
        tokens=tokens,
        # iOS: 사일런트 백그라운드 푸시
        apns=messaging.APNSConfig(
            headers={
                "apns-priority": "5",
                "apns-push-type": "background"
            },
            payload=messaging.APNSPayload(
                aps=messaging.Aps(content_available=True)
            )
        ),
        # Android: priority만 설정 (notification 없음)
        android=messaging.AndroidConfig(priority="high")
    )

    try:
        loop = asyncio.get_running_loop()  # Python 3.10+ 권장
        response = await loop.run_in_executor(
            None, messaging.send_each_for_multicast, message
        )

        failed_tokens = []          # 확정된 미등록 = cleanup 후보
        retained_reasons = []       # 삭제하지 않고 보존한 실패의 타입별 라벨
        for idx, result in enumerate(response.responses):
            if not result.success:
                # 무효 토큰만 수집 (삭제 대상)
                # 타입으로만 판정한다 — `.code` 는 Unregistered 와 generic
                # NotFound 를 구분하지 못한다(둘 다 "NOT_FOUND").
                if is_unregistered(result.exception):
                    failed_tokens.append(tokens[idx])
                else:
                    retained_reasons.append(classify_send_error(result.exception))

        logger.info(
            "FCM data-only 전송 완료",
            extra={
                "event": "fcm_data_only",
                "total": len(tokens),
                "success": response.success_count,
                "failure": response.failure_count,
                # `invalid_tokens` 는 외부 운영 쿼리 호환을 위해 이름을 유지한다.
                # 의미는 이제 "확정된 미등록 cleanup 후보 수" 다.
                "invalid_tokens": len(failed_tokens),
                "retained_failures": len(retained_reasons),
                # 유형별 개수를 남긴다 — set 으로 접으면 "타입별 관측" 이 되지 않는다.
                "retained_by_reason": dict(Counter(retained_reasons)),
            }
        )

        return {
            "success_count": response.success_count,
            "failure_count": response.failure_count,
            "failed_tokens": failed_tokens
        }

    except Exception as e:
        logger.warning(
            "FCM data-only 전송 실패",
            extra={"event": "fcm_data_only", **error_descriptor(e)},
        )
        return {
            "success_count": 0,
            "failure_count": len(tokens),
            "failed_tokens": []
        }
