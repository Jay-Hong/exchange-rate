# app/subscription.py

# 표준 라이브러리
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from enum import Enum
import logging
from typing import Optional

# 서드파티 라이브러리
import httpx

# 로컬 애플리케이션
from app.config import REVENUECAT_API_KEY

logger = logging.getLogger("exchange_rate.subscription")

REVENUECAT_API_URL = "https://api.revenuecat.com/v1"

# LRU 캐시: 크기 제한 + TTL + Stale fallback
CACHE_MAX_SIZE = 1000
CACHE_TTL = timedelta(minutes=5)
CACHE_STALE_TTL = timedelta(hours=1)
PENDING_TTL = timedelta(seconds=12)


class EntitlementCache:
    """TTL + LRU + Stale fallback 기반 구독 상태 캐시."""

    def __init__(self, max_size: int = CACHE_MAX_SIZE):
        self._cache: OrderedDict[str, tuple[bool, datetime]] = OrderedDict()
        self._max_size = max_size

    def get(self, user_id: str) -> tuple[Optional[bool], bool]:
        """
        캐시 조회
        Returns: (cached_value, is_fresh)
          - (None, False): 캐시 없음
          - (value, True): 신선한 캐시 (TTL 내)
          - (value, False): Stale 캐시 (TTL 만료, API 오류 시 fallback용)
        """
        if user_id not in self._cache:
            return (None, False)

        is_premium, cached_at = self._cache[user_id]
        age = datetime.now(timezone.utc) - cached_at

        # Stale TTL도 초과하면 완전 삭제
        if age >= CACHE_STALE_TTL:
            del self._cache[user_id]
            return (None, False)

        # LRU: 최근 사용으로 이동
        self._cache.move_to_end(user_id)

        if age < CACHE_TTL:
            return (is_premium, True)  # 신선한 캐시
        return (is_premium, False)  # Stale 캐시 (fallback용)

    def set(self, user_id: str, is_premium: bool) -> None:
        """캐시 저장 (LRU 초과 시 가장 오래된 항목 제거)."""
        if user_id in self._cache:
            self._cache.move_to_end(user_id)
        self._cache[user_id] = (is_premium, datetime.now(timezone.utc))

        # 크기 초과 시 가장 오래된 항목 제거
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def invalidate(self, user_id: str) -> None:
        """Webhook 수신 시 캐시 무효화."""
        self._cache.pop(user_id, None)


_cache = EntitlementCache()


class PendingCache:
    """구독 상태 확인 중(pending) TTL 캐시."""

    def __init__(self):
        self._cache: dict[str, datetime] = {}

    def state(self, user_id: str) -> str:
        expires_at = self._cache.get(user_id)
        if not expires_at:
            return "none"
        if datetime.now(timezone.utc) <= expires_at:
            return "active"
        return "expired"

    def mark(self, user_id: str) -> None:
        self._cache[user_id] = datetime.now(timezone.utc) + PENDING_TTL

    def clear(self, user_id: str) -> None:
        self._cache.pop(user_id, None)


_pending = PendingCache()


class PremiumStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING = "pending"


def invalidate_user_cache(user_id: str) -> None:
    """
    사용자 캐시 무효화 (Webhook에서 호출).

    RevenueCat Webhook으로 구독 상태 변경 이벤트 수신 시 호출.
    다음 verify_premium() 호출 시 API에서 최신 상태를 가져옴.
    """
    if not user_id:
        return
    _cache.invalidate(user_id)
    _pending.clear(user_id)


async def _check_revenuecat_entitlement(user_id: str) -> tuple[bool, bool]:
    """
    RevenueCat REST API로 구독 상태 확인 (Async)
    Returns: (is_premium, should_cache)
      - should_cache=True: 정상 응답, 캐시해도 안전
      - should_cache=False: 일시적 오류, 캐시하면 유료 사용자 차단 위험
    """
    if not REVENUECAT_API_KEY:
        logger.error("REVENUECAT_API_KEY 미설정")
        return (False, False)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{REVENUECAT_API_URL}/subscribers/{user_id}",
                headers={
                    "Authorization": f"Bearer {REVENUECAT_API_KEY}",
                    "Content-Type": "application/json",
                },
            )

        if response.status_code == 200:
            # ✅ 성공: 구독 상태 확인 후 캐시
            try:
                data = response.json()
            except ValueError:
                logger.warning("RevenueCat API JSON 파싱 실패", extra={"user_id": user_id})
                return (False, False)

            entitlements = data.get("subscriber", {}).get("entitlements", {})
            premium = entitlements.get("premium")

            if not premium:
                return (False, True)  # 구독 없음 (정상 확인)

            expires = premium.get("expires_date")
            if not expires:
                # lifetime entitlement
                return (True, True)

            try:
                expires_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except Exception:
                logger.warning("RevenueCat expires_date 파싱 실패", extra={"user_id": user_id})
                return (False, False)

            return (expires_dt > datetime.now(timezone.utc), True)

        if response.status_code == 404:
            # ✅ 사용자 없음: 새 사용자, "비구독"으로 캐시 OK
            return (False, True)

        # ❌ 4xx/5xx 오류: 캐시하지 않음 (일시적 장애 가능성)
        logger.warning(
            "RevenueCat API 응답 오류",
            extra={"status": response.status_code, "user_id": user_id},
        )
        return (False, False)

    except Exception as e:
        # ❌ 네트워크 오류: 캐시하지 않음
        logger.warning("RevenueCat API 호출 실패", extra={"error": str(e)})
        return (False, False)


async def verify_premium_status(user_id: str) -> PremiumStatus:
    """
    Premium 구독 상태 확인 (5분 캐시, LRU 1000명, Stale fallback).

    Stale cache 정책으로 장애 시에도 기존 유료 사용자 보호:
    - 신선한 캐시 → 즉시 반환
    - Stale 캐시 + API 성공 → 새 값 반환 및 캐시 갱신
    - Stale 캐시 + API 오류 → stale 값 반환 (유료 사용자 보호)
    - 캐시 없음 + API 오류 → PENDING (재시도 필요)
    """
    if not user_id:
        return PremiumStatus.INACTIVE

    # 1. 캐시 확인
    cached_value, is_fresh = _cache.get(user_id)

    # 신선한 캐시가 있으면 즉시 반환
    if cached_value is not None and is_fresh:
        return PremiumStatus.ACTIVE if cached_value else PremiumStatus.INACTIVE

    # 2. RevenueCat API 호출 (캐시 없거나 stale인 경우)
    is_premium, should_cache = await _check_revenuecat_entitlement(user_id)

    # 3. API 성공 시 캐시 갱신
    if should_cache:
        if is_premium:
            _pending.clear(user_id)
            _cache.set(user_id, True)
            return PremiumStatus.ACTIVE

        # is_premium == False
        if cached_value is not None:
            _pending.clear(user_id)
            _cache.set(user_id, False)
            return PremiumStatus.INACTIVE

        pending_state = _pending.state(user_id)
        if pending_state == "active":
            return PremiumStatus.PENDING
        if pending_state == "expired":
            _pending.clear(user_id)
            _cache.set(user_id, False)
            return PremiumStatus.INACTIVE

        _pending.mark(user_id)
        return PremiumStatus.PENDING

    # 4. API 오류 시: stale 캐시가 있으면 사용 (유료 사용자 보호)
    if cached_value is not None:
        logger.info(
            "API 오류, stale 캐시 사용",
            extra={"user_id": user_id, "stale_value": cached_value},
        )
        return PremiumStatus.ACTIVE if cached_value else PremiumStatus.INACTIVE

    # 5. 캐시도 없고 API도 실패 → PENDING
    if _pending.state(user_id) == "none":
        _pending.mark(user_id)
    return PremiumStatus.PENDING


async def verify_premium(user_id: str) -> bool:
    return await verify_premium_status(user_id) == PremiumStatus.ACTIVE


__all__ = [
    "verify_premium",
    "verify_premium_status",
    "PremiumStatus",
    "invalidate_user_cache",
    "EntitlementCache",
]
