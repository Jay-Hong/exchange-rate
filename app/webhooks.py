# app/webhooks.py

# 표준 라이브러리
import hmac
import logging
from typing import Optional

# 서드파티 라이브러리
from fastapi import APIRouter, Request, HTTPException, Header

# 로컬 애플리케이션
from app.config import REVENUECAT_WEBHOOK_AUTH_KEY
from app.subscription import invalidate_user_cache

logger = logging.getLogger("exchange_rate.webhooks")
router = APIRouter()


def verify_webhook_auth(auth_header: str) -> bool:
    """RevenueCat Webhook Authorization 헤더 검증."""
    if not REVENUECAT_WEBHOOK_AUTH_KEY:
        logger.error("REVENUECAT_WEBHOOK_AUTH_KEY 미설정")
        return False
    if not auth_header:
        return False
    return hmac.compare_digest(auth_header, REVENUECAT_WEBHOOK_AUTH_KEY)


@router.post("/webhooks/revenuecat")
async def revenuecat_webhook(
    request: Request,
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    # 1. Authorization 헤더 검증 (필수)
    if not authorization:
        logger.warning("Webhook Authorization 헤더 누락")
        raise HTTPException(status_code=401, detail="Missing Authorization")

    if not verify_webhook_auth(authorization):
        logger.warning("Webhook Authorization 검증 실패")
        raise HTTPException(status_code=401, detail="Invalid Authorization")

    # 2. 이벤트 처리
    payload = await request.json()
    event = payload.get("event", {})
    event_type = event.get("type")

    user_ids = set()
    for key in ("app_user_id", "original_app_user_id"):
        value = event.get(key)
        if value:
            user_ids.add(value)

    aliases = event.get("aliases") or []
    if isinstance(aliases, str):
        user_ids.add(aliases)
    else:
        for alias in aliases:
            if alias:
                user_ids.add(alias)

    for key in ("transferred_from", "transferred_to"):
        value = event.get(key)
        if not value:
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if item:
                    user_ids.add(item)
        else:
            user_ids.add(value)

    if not event_type:
        logger.warning("Webhook payload 누락", extra={"event_type": event_type})
        return {"status": "ignored"}

    invalidate_events = {
        "INITIAL_PURCHASE",
        "RENEWAL",
        "EXPIRATION",
        "CANCELLATION",
        "TRANSFER",
    }

    if event_type in invalidate_events:
        if not user_ids:
            logger.warning("Webhook 사용자 정보 누락", extra={"event": event_type})
            return {"status": "ignored"}
        for uid in user_ids:
            invalidate_user_cache(uid)
        logger.info(
            "Webhook 캐시 무효화",
            extra={"event": event_type, "user_id_count": len(user_ids)},
        )
    else:
        logger.info("Webhook 이벤트 무시", extra={"event": event_type, "user_id_count": len(user_ids)})

    return {"status": "ok"}


__all__ = [
    "router",
    "verify_webhook_auth",
]
