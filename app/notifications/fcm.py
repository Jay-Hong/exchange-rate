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

import firebase_admin
from firebase_admin import credentials, messaging
from firebase_admin.exceptions import FirebaseError

from app.config import FIREBASE_CREDENTIALS_PATH

logger = logging.getLogger("exchange_rate.notifications.fcm")

# Firebase 초기화 상태
_firebase_initialized = False


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
        cred = credentials.Certificate(str(cred_path))
        firebase_admin.initialize_app(cred)
        _firebase_initialized = True
        logger.info("Firebase Admin SDK 초기화 완료")
        return True
    except Exception as e:
        logger.error(
            "Firebase 초기화 실패",
            extra={"error": str(e)}
        )
        return False


def is_firebase_initialized() -> bool:
    """Firebase 초기화 상태 확인"""
    return _firebase_initialized


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

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=data or {},
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

    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=data or {},
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

    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=data or {},
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
