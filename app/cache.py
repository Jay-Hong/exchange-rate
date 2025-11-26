# app/cache.py

"""Redis 캐시 & 회로차단기 유틸 (Phase 1.7)

역할
- Redis 비동기 클라이언트 단일 인스턴스 관리
- 간단한 회로차단기: 연속 실패 시 일정 시간 시도 중단
- get/set/hget/hset 래퍼 제공 (예외 발생 시 None 반환, 회로차단기 갱신)

주의
- 네트워크/Redis 가용성에 따라 실패할 수 있으므로 항상 폴백 로직(DB)이 필요
- 회로차단기 상태는 Redis에 기록하지만, Redis가 완전히 죽어도 로컬 상태로 동작
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional, Any

import redis.asyncio as redis

logger = logging.getLogger("exchange_rate.cache")

# 환경 변수
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None  # 빈 문자열 → None 변환

# 키 상수
BROADCAST_CACHE_KEY = "broadcast:latest"


class RedisCircuitBreaker:
    """간단한 회로차단기 (연속 실패 시 일정 시간 오픈)."""

    def __init__(self, redis_client: "RedisCache", failure_threshold: int = 5, timeout_seconds: int = 30):
        self.redis_client = redis_client
        self.failure_threshold = failure_threshold
        self.timeout = timedelta(seconds=timeout_seconds)
        self.failure_count = 0
        self.state = "closed"
        self.state_key = "circuit:redis"
        self.open_until: Optional[datetime] = None

    async def _persist_state(self, state: str, until: Optional[datetime] = None):
        """Redis에 상태를 기록 (best-effort)."""
        if not self.redis_client.client:
            return
        payload = {"state": state, "updated_at": datetime.now().timestamp()}
        if until:
            payload["until"] = until.timestamp()
        try:
            await self.redis_client.client.set(self.state_key, json.dumps(payload))
        except Exception:
            # 회로차단기 로깅만 남기고 진행
            logger.debug("circuit state persist failed", exc_info=True)

    async def record_success(self):
        self.failure_count = 0
        self.state = "closed"
        self.open_until = None
        await self._persist_state("closed")

    async def record_failure(self):
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            self.open_until = datetime.now() + self.timeout
            await self._persist_state("open", self.open_until)
            logger.warning(
                "🔴 Redis 회로차단기 열림",
                extra={"failures": self.failure_count, "timeout_seconds": self.timeout.seconds},
            )

    async def can_attempt(self) -> bool:
        if self.state == "closed":
            return True

        # 오픈 상태라면 타임아웃 경과 여부 확인
        if self.open_until and datetime.now() > self.open_until:
            self.state = "closed"
            self.failure_count = 0
            self.open_until = None
            await self._persist_state("closed")
            return True
        return False


class RedisCache:
    def __init__(self):
        self.client: Optional[redis.Redis] = None
        self.circuit = RedisCircuitBreaker(self)

    async def connect(self):
        if self.client:
            return
        try:
            self.client = redis.from_url(REDIS_URL, password=REDIS_PASSWORD, decode_responses=False)
            await self.client.ping()
            logger.info("✅ Redis 연결 성공", extra={"url": REDIS_URL})
        except Exception as e:
            self.client = None
            # 연결 실패도 회로차단기에 반영해 과도한 재시도를 방지
            await self.circuit.record_failure()
            logger.error("❌ Redis 연결 실패", exc_info=True, extra={"url": REDIS_URL})

    async def _decode(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    async def get(self, key: str) -> Optional[str]:
        if not self.client or not await self.circuit.can_attempt():
            return None
        try:
            value = await self.client.get(key)
            await self.circuit.record_success()
            return await self._decode(value)
        except Exception:
            await self.circuit.record_failure()
            logger.debug("Redis GET 실패", exc_info=True, extra={"key": key})
            return None

    async def set(self, key: str, value: str, ex: Optional[int] = None):
        if not self.client or not await self.circuit.can_attempt():
            return
        try:
            await self.client.set(key, value, ex=ex)
            await self.circuit.record_success()
        except Exception:
            await self.circuit.record_failure()
            logger.debug("Redis SET 실패", exc_info=True, extra={"key": key})

    async def hget(self, key: str, field: str) -> Optional[str]:
        if not self.client or not await self.circuit.can_attempt():
            return None
        try:
            value = await self.client.hget(key, field)
            await self.circuit.record_success()
            return await self._decode(value)
        except Exception:
            await self.circuit.record_failure()
            logger.debug("Redis HGET 실패", exc_info=True, extra={"key": key, "field": field})
            return None

    async def hset(self, key: str, field: str, value: str):
        if not self.client or not await self.circuit.can_attempt():
            return
        try:
            await self.client.hset(key, field, value)
            await self.circuit.record_success()
        except Exception:
            await self.circuit.record_failure()
            logger.debug("Redis HSET 실패", exc_info=True, extra={"key": key, "field": field})


redis_cache = RedisCache()


__all__ = [
    "redis_cache",
    "BROADCAST_CACHE_KEY",
    "RedisCircuitBreaker",
    "RedisCache",
]
