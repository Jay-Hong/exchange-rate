# app/notifications/fcm.py
"""
Firebase Cloud Messaging (FCM) 서비스
- 푸시 알림 전송
- Firebase Admin SDK 초기화
- 재시도 정책 및 토큰 관리
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from app import config
import firebase_admin
from firebase_admin import credentials, messaging
from firebase_admin.exceptions import FirebaseError

from app.config import FIREBASE_CREDENTIALS_PATH

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

    사용 예시:
        @app.post("/api/admin/notify-user/{user_id}")
        async def notify_user(user_id: str):
            token = get_user_fcm_token(user_id)
            success, error = await send_fcm_notification(
                token=token,
                title="구독 알림",
                body="프리미엄 구독이 3일 후 만료됩니다.",
                data={"type": "subscription_expiry"}
            )
            if error in ['UNREGISTERED', 'INVALID_ARGUMENT']:
                delete_invalid_token(user_id)
            return {"success": success}

    Args:
        token: FCM Device Token (단일)
        title: 알림 제목
        body: 알림 내용
        data: 추가 데이터 (선택)
        max_retries: 최대 재시도 횟수 (서버 오류 시)

    Returns:
        (성공 여부, 에러 코드 또는 None)
        에러 코드: UNREGISTERED, INVALID_ARGUMENT, NOT_FOUND (무효 토큰)
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
                    "token": token[:20] + "...",
                    "success": True,
                    "latency_ms": round(latency_ms, 2),
                    "message_id": response,
                }
            )
            return True, None

        except FirebaseError as e:
            error_code = e.code if hasattr(e, 'code') else str(type(e).__name__)

            # 토큰 오류: 재시도 무의미
            if error_code in ['UNREGISTERED', 'INVALID_ARGUMENT', 'NOT_FOUND']:
                latency_ms = (time.time() - start_time) * 1000
                logger.warning(
                    "FCM 무효 토큰",
                    extra={
                        "event": "fcm_send",
                        "token": token[:20] + "...",
                        "success": False,
                        "error_code": error_code,
                        "latency_ms": round(latency_ms, 2),
                    }
                )
                return False, error_code

            # 서버 오류: 재시도
            if attempt < max_retries:
                await asyncio.sleep(delays[attempt])
                continue

            # 최대 재시도 초과
            latency_ms = (time.time() - start_time) * 1000
            logger.error(
                "FCM 전송 실패",
                extra={
                    "event": "fcm_send",
                    "token": token[:20] + "...",
                    "success": False,
                    "error_code": error_code,
                    "latency_ms": round(latency_ms, 2),
                    "attempts": attempt + 1,
                }
            )
            return False, error_code

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            logger.exception(
                "FCM 전송 예외",
                extra={
                    "event": "fcm_send",
                    "token": token[:20] + "...",
                    "success": False,
                    "error": str(e),
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

    사용 예시:
        @app.post("/api/admin/broadcast")
        async def broadcast(title: str, body: str):
            tokens = get_all_active_tokens()  # DB에서 조회
            result = await send_fcm_multicast(
                tokens=tokens[:500],  # 최대 500개
                title=title,
                body=body,
                data={"type": "announcement"}
            )
            # 무효 토큰 정리
            if result["failed_tokens"]:
                delete_invalid_tokens(result["failed_tokens"])
            return result

    Args:
        tokens: FCM Device Token 목록 (Firebase API 제한: 최대 500개)
        title: 알림 제목
        body: 알림 내용
        data: 추가 데이터 (선택)

    Returns:
        {
            "success_count": int,
            "failure_count": int,
            "failed_tokens": list[str]  # 무효 토큰 목록 (삭제 대상)
        }
    """
    if not _firebase_initialized:
        if not init_firebase():
            return {
                "success_count": 0,
                "failure_count": len(tokens),
                "failed_tokens": tokens
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

        failed_tokens = []
        for idx, result in enumerate(response.responses):
            if not result.success:
                error_code = (
                    result.exception.code
                    if hasattr(result.exception, 'code')
                    else "UNKNOWN"
                )
                # 무효 토큰만 수집
                if error_code in ['UNREGISTERED', 'INVALID_ARGUMENT', 'NOT_FOUND']:
                    failed_tokens.append(tokens[idx])

        logger.info(
            "FCM 멀티캐스트 완료",
            extra={
                "event": "fcm_multicast",
                "total": len(tokens),
                "success": response.success_count,
                "failure": response.failure_count,
                "invalid_tokens": len(failed_tokens),
            }
        )

        return {
            "success_count": response.success_count,
            "failure_count": response.failure_count,
            "failed_tokens": failed_tokens
        }

    except Exception as e:
        logger.exception(
            "FCM 멀티캐스트 예외",
            extra={"event": "fcm_multicast", "error": str(e)}
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
            "failed_tokens": list[str]  # 무효 토큰 목록 (삭제 대상)
        }
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

        failed_tokens = []
        for idx, result in enumerate(response.responses):
            if not result.success:
                error_code = (
                    result.exception.code
                    if hasattr(result.exception, 'code')
                    else "UNKNOWN"
                )
                # 무효 토큰만 수집 (삭제 대상)
                if error_code in ['UNREGISTERED', 'INVALID_ARGUMENT', 'NOT_FOUND']:
                    failed_tokens.append(tokens[idx])

        latency_ms = (time.time() - start_time) * 1000
        logger.info(
            "FCM 멀티캐스트 완료 (sync)",
            extra={
                "event": "fcm_multicast_sync",
                "total": len(tokens),
                "success": response.success_count,
                "failure": response.failure_count,
                "invalid_tokens": len(failed_tokens),
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
        logger.exception(
            "FCM 멀티캐스트 예외 (sync)",
            extra={
                "event": "fcm_multicast_sync",
                "error": str(e),
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
            "failed_tokens": list[str]  # 무효 토큰 목록 (삭제 대상)
        }
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

        failed_tokens = []
        for idx, result in enumerate(response.responses):
            if not result.success:
                error_code = (
                    result.exception.code
                    if hasattr(result.exception, 'code')
                    else "UNKNOWN"
                )
                # 무효 토큰만 수집 (삭제 대상)
                if error_code in ['UNREGISTERED', 'INVALID_ARGUMENT', 'NOT_FOUND']:
                    failed_tokens.append(tokens[idx])

        logger.info(
            "FCM data-only 전송 완료",
            extra={
                "event": "fcm_data_only",
                "total": len(tokens),
                "success": response.success_count,
                "failure": response.failure_count,
                "invalid_tokens": len(failed_tokens),
            }
        )

        return {
            "success_count": response.success_count,
            "failure_count": response.failure_count,
            "failed_tokens": failed_tokens
        }

    except Exception:
        logger.warning("FCM data-only 전송 실패", exc_info=True)
        return {
            "success_count": 0,
            "failure_count": len(tokens),
            "failed_tokens": []
        }
