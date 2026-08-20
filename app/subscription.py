# app/subscription.py

# 표준 라이브러리
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import logging
from typing import Optional, Union
import urllib.parse

# 서드파티 라이브러리
import httpx

# 로컬 애플리케이션
from app.clock import Clock, system_clock
from app.config import REVENUECAT_API_KEY

logger = logging.getLogger("exchange_rate.subscription")

REVENUECAT_API_URL = "https://api.revenuecat.com/v1"

# ⚠️ `Clock`/`system_clock`의 **정본은 `app/clock.py`**다 (ADR-039 §8.1 harness 선행 (1)).
#    여기서는 아래 5개 TTL 판정 지점이 쓰기 위해 가져올 뿐이며 re-export가 아니다 —
#    `__all__`에도 없고, 다른 모듈은 `app.clock`에서 직접 가져와야 한다
#    (`tests/test_clock.py::TestCanonicalImportPath`가 AST로 잠근다).
#    이 모듈의 판정 지점은 `clock.wall()`만 읽는다. `clock.mono()`는 lease horizon 관측
#    (`verified_at_monotonic`)가 들어오는 날 처음 쓰이며, 그때까지 테스트의 `_clock`이
#    poison callable로 그 사실을 잠근다.

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

    def get(self, user_id: str, *, clock: Clock) -> tuple[Optional[bool], bool]:
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
        age = clock.wall() - cached_at

        # Stale TTL도 초과하면 완전 삭제
        if age >= CACHE_STALE_TTL:
            del self._cache[user_id]
            return (None, False)

        # LRU: 최근 사용으로 이동
        self._cache.move_to_end(user_id)

        if age < CACHE_TTL:
            return (is_premium, True)  # 신선한 캐시
        return (is_premium, False)  # Stale 캐시 (fallback용)

    def set(self, user_id: str, is_premium: bool, *, clock: Clock) -> None:
        """캐시 저장 (LRU 초과 시 가장 오래된 항목 제거)."""
        if user_id in self._cache:
            self._cache.move_to_end(user_id)
        self._cache[user_id] = (is_premium, clock.wall())

        # 크기 초과 시 가장 오래된 항목 제거
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def invalidate(self, user_id: str) -> None:
        """Webhook 수신 시 캐시 무효화."""
        self._cache.pop(user_id, None)

    def clear(self) -> None:
        """전체 삭제 — 모듈 싱글턴이 테스트 간 누수되는 것을 끊는다(test 용도).

        (`AlertSettingsCache.invalidate(None, None)`과 같은 역할.)
        """
        self._cache.clear()


_cache = EntitlementCache()


class PendingCache:
    """구독 상태 확인 중(pending) TTL 캐시."""

    def __init__(self):
        self._cache: dict[str, datetime] = {}

    def state(self, user_id: str, *, clock: Clock) -> str:
        expires_at = self._cache.get(user_id)
        if not expires_at:
            return "none"
        if clock.wall() <= expires_at:
            return "active"
        return "expired"

    def mark(self, user_id: str, *, clock: Clock) -> None:
        self._cache[user_id] = clock.wall() + PENDING_TTL

    def clear_all(self) -> None:
        """전체 삭제 (test 용도) — `EntitlementCache.clear()`와 짝."""
        self._cache.clear()

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


@dataclass(frozen=True)
class Determined:
    """RevenueCat이 **authoritative하게 판정**했다 (200/201 파싱 성공 — Get-or-Create 동일 본문).

    ⛔ 구 계약의 "404=신규 사용자" 는 여기 속하지 않는다 — 신규 고객은 201 을 받는다(2026-08-14 교정).
    """

    is_premium: bool


@dataclass(frozen=True)
class ProviderUnavailable:
    """일시적 장애 — 재시도로 나을 수 있다. 408 / 429 / 5xx / 네트워크 예외.

    ⚠️ `status`는 HTTP 응답이 있었을 때만 채운다. 네트워크 예외는 응답 자체가 없으므로 `None` —
    있는 척하면 텔레메트리가 거짓이 된다.
    """

    status: Optional[int] = None


@dataclass(frozen=True)
class ProviderMisconfigured:
    """**우리 설정** 문제 — 401 / 403 / API key 미설정. 재시도로 낫지 않는다.

    ⚠️ 이걸 `ProviderUnavailable`로 접으면 클라가 영원히 재시도하고 운영자는 알람을 못 받는다.
    반대로 429를 여기 넣으면 rate limit이 **영구 설정 오류로 오분류**된다.
    """

    status: Optional[int] = None


@dataclass(frozen=True)
class BadRequest:
    """400 — 우리가 잘못 보냈다. 프로그래밍 오류이지 사용자 상태가 아니다."""

    status: int = 400


@dataclass(frozen=True)
class ProtocolViolation:
    """응답이 계약을 벗어났다 — JSON/날짜 파싱 실패, 예상 밖 4xx. 계약 변경 신호."""

    detail: str


# **WS 인가 경로가 소비하는 축.** REST는 아래 adapter로 되접는다.
# ⛔ 구 번호 `§8.1 A6` 는 ADR-040 에서 **폐기된 설계**의 것이다 — 아래 주석들의 그 표기도
#    같다. 다만 **3-bucket 계약(terminal / transient / 내부 예외 재전파) 자체는 살아 있다**:
#    현재 소비자는 `app/topic_authorization.py` 의 `classify_premium` /
#    `authorize_subscription_plan` 이고, 각각 per-topic `Denied` / 전체-요청 `Unavailable` /
#    재전파로 접는다.
RevenueCatResult = Union[
    Determined, ProviderUnavailable, ProviderMisconfigured, BadRequest, ProtocolViolation
]

# 재시도로 나을 수 있는 상태 코드 (§8.1 A4-1).
# ⚠️ "4xx 대 5xx"로 나누지 말 것 — 408·429가 4xx라 config 오류로 오분류된다.
_TRANSIENT_STATUSES = frozenset({408, 429})
_MISCONFIGURED_STATUSES = frozenset({401, 403})


def _shape_violation(user_id: str, field: str) -> "ProtocolViolation":
    """응답 형식 위반을 한 곳에서 로깅·분류한다. `field`가 곧 위반 지점이다."""
    logger.warning("RevenueCat 응답 형식 오류", extra={"user_id": user_id, "field": field})
    return ProtocolViolation(detail=field)


async def fetch_revenuecat_result(user_id: str, *, clock: Clock) -> RevenueCatResult:
    """RevenueCat 조회 결과를 **분류해서** 돌려준다 (ADR-039 §8.1 A4-1).

    구 `_check_revenuecat_entitlement`는 성격이 다른 5개 실패를 전부 `(False, False)`로
    평탄화했다 — config 오류 / 계약 위반 / transient가 구분되지 않아, **WS 인가 경로**
    (`app/topic_authorization.py`)가 그대로 소비하면 3-bucket 계약
    (terminal / transient / 내부 예외)이 깨진다.

    ⚠️ **축 주의**: `expires_dt > clock.wall()`의 wall 축은 **구독 유효성** 판정이라 맞다.
    lease horizon의 monotonic 축(§8.1 A2)과 목적이 다르므로 "일관성" 명목으로 바꾸지 말 것.
    비교는 **strict `>`** — 정확히 만료 시각이면 만료다(characterization으로 잠금).

    ⚠️ **판정 기준은 truthiness가 아니라 "키 존재 + 타입"이다** (2026-07-28 hardening).
    lifetime은 **명시적 `"expires_date": null`**이고, 키 누락 / `premium == {}` / `""` / 숫자는
    전부 `ProtocolViolation`이다. 구 코드는 truthiness로 분기해 `{}`를 "구독 없음"으로,
    `expires_date` 누락과 `""`를 **lifetime으로** 통과시켰다 — malformed 입력에 프리미엄이
    부여되는 경로였다. ⛔ 그 서술로 되돌리지 말 것(정확한 규칙은 아래 본문 주석).

    ⚠️ **leaf 이관은 보류다** (구 "N-3 이관 예정"). WS 소비자는 이미 생겼지만
    (`app/topic_authorization.py`) 순환은 **아직 없다** — 그 모듈이 이 함수를 **함수 본문에서
    지연 import** 하기 때문이다(`_observe_premium` 안의 `from app.subscription
    import fetch_revenuecat_result`). 순환 위험을 만들던 구 근거(`invalidate_user_cache` 가
    strict cache 를 무효화하는 경로)는 ADR-040 에서 **폐기**됐다.
    ⚠️ 다만 지연 import 는 **순환을 없앤 게 아니라 미룬 것**이다 — 소비자가 module-level import
    로 바꾸거나 관측 캐시를 넣어 역방향 엣지가 생기면 그때 이 provider 를 leaf 로 옮긴다.
    이관 자체는 순수 이동이라 위 계약 테스트가 그대로 잠근다.
    """
    if not REVENUECAT_API_KEY:
        logger.error("REVENUECAT_API_KEY 미설정")
        return ProviderMisconfigured()

    # ⚠️ **전송 예외만** 여기서 잡는다. 광범위 `except Exception`으로 감싸면 응답 **형식 오류**와
    #    우리 쪽 **프로그래밍 오류**까지 retryable로 접혀 A6-1의 3-bucket 계약이 무너진다
    #    (실측: JSON이 list / `premium`이 문자열 / `subscriber`가 문자열 → 전부 ProviderUnavailable이었다).
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                # RC 계약: app_user_id 는 URL 인코딩 필수 — '/'·'#' 이 든 custom UID 가
                # 경로를 바꾸거나 잘리는 것을 막는다 (safe="" 로 '/' 도 인코딩).
                f"{REVENUECAT_API_URL}/subscribers/{urllib.parse.quote(user_id, safe='')}",
                headers={
                    "Authorization": f"Bearer {REVENUECAT_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.RequestError as e:
        # 연결·타임아웃·읽기 실패 등 실제 전송 계층 오류 (`ConnectError`/`ReadTimeout` 등의 상위 타입)
        logger.warning("RevenueCat API 호출 실패", extra={"error": str(e)})
        return ProviderUnavailable()

    # ⚠️ v1 `GET /subscribers/{id}` 는 **Get-or-Create** 다 — 기존 고객은 200, 방금 생성된
    #    고객은 **201** 로 같은 Customer Info 본문을 돌려준다(공식 API v1 계약, 2026-08-14 확인).
    #    201 을 별도 분기(예: 즉시 비구독)로 두면 같은 본문을 두 규칙으로 읽게 된다 — 동일 파서.
    if response.status_code in (200, 201):
        try:
            data = response.json()
        except ValueError:
            logger.warning("RevenueCat API JSON 파싱 실패", extra={"user_id": user_id})
            return ProtocolViolation(detail="json")

        # ── 응답 형식 검증 (§8.1 A4-1) ─────────────────────────────────────────
        # 규칙과 1:1 대응하도록 **명시적으로** 검사한다. 구 코드는 누락 키를 `{}`로 접고
        # truthiness로 분기해 malformed를 authoritative로 통과시켰다.
        #
        # ⚠️ 왜 중요한가(실측): stale 캐시를 가진 **유료 사용자**에게
        #   - 503 장애가 오면      → A4 stale fallback이 돌아 ACTIVE 유지 ✅
        #   - malformed 200이 오면 → `should_cache=True`로 `False`가 덮어써져 **INACTIVE로 강등** ❌
        # 즉 쓰레기 데이터가 진짜 장애보다 더 신뢰받고 있었다. malformed를 ProtocolViolation으로
        # 돌려 `(False, False)` 경로에 태우면 **stale fallback이 복원**된다(A4 제거가 아니라 복원).
        #
        # 스키마 근거(RevenueCat API v1 `GET /subscribers` 응답 예시):
        #   "entitlements": {"pro_cat": {"expires_date": null, "product_identifier": "onetime", ...}}
        # → lifetime은 **키 누락이 아니라 명시적 `null`**이다. 따라서 키 누락은 계약 위반으로 본다.
        if not isinstance(data, dict):
            return _shape_violation(user_id, "body")
        subscriber = data.get("subscriber")
        if not isinstance(subscriber, dict):
            return _shape_violation(user_id, "subscriber")
        entitlements = subscriber.get("entitlements")
        if not isinstance(entitlements, dict):
            return _shape_violation(user_id, "entitlements")

        # 구독 없음의 정상 표현: `entitlements`에 premium 키가 **없다**.
        if "premium" not in entitlements:
            return Determined(is_premium=False)

        premium = entitlements["premium"]
        if not isinstance(premium, dict) or not premium:
            # null / [] / "yes" / 0 / **{}** — 전부 malformed entitlement 객체다.
            # ⚠️ `{}`를 lifetime으로 읽으면 **빈 객체에 프리미엄이 부여**된다.
            return _shape_violation(user_id, "premium")

        if "expires_date" not in premium:
            # 위 스키마상 lifetime도 키를 갖는다 → 누락은 위반.
            # 운영에서 이게 뜨면 스키마 변경 신호이므로 detail을 구분해 남긴다.
            return _shape_violation(user_id, "expires_date_missing")

        expires = premium["expires_date"]
        if expires is None:
            return Determined(is_premium=True)  # lifetime (명시적 null)

        if not isinstance(expires, str) or not expires:
            # "" 또는 숫자 — 구 코드는 falsy라 **lifetime으로 통과**시켰다.
            return _shape_violation(user_id, "expires_date_value")

        try:
            expires_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            # ⚠️ `except Exception`으로 넓히지 말 것: 프로그래밍 오류를 삼킨다.
            logger.warning("RevenueCat expires_date 파싱 실패", extra={"user_id": user_id})
            return ProtocolViolation(detail="expires_date")

        # ⚠️ 샘플링 지점 유지 — 파싱 **후**에 한 번 읽는다(구 코드의 `expires_dt > clock.wall()`와 동일).
        now = clock.wall()
        try:
            return Determined(is_premium=expires_dt > now)
        except TypeError:
            # offset 없는 날짜(`"2026-06-01T00:00:00"`)면 aware/naive 비교라 TypeError다.
            # RevenueCat 형식은 `Z`를 포함하므로 이건 **계약 위반**이지 우리 버그가 아니다.
            # REST 결과는 `(False, False)`로 이전과 동일 — 예외가 전파되든 여기서 접히든 같다.
            logger.warning("RevenueCat expires_date tz 누락", extra={"user_id": user_id})
            return ProtocolViolation(detail="expires_date_naive")

    # ⛔ 404 특수 분기를 **의도적으로 두지 않는다** (2026-08-14 계약 교정). 구 코드는
    #    "404 = 새 사용자, 비구독 캐시 OK" 였는데 Get-or-Create 계약상 신규 고객은 404 가
    #    아니라 201 을 받는다 — 404 의 의미는 공식 계약에 정의돼 있지 않으므로 우리가
    #    "설정 결함" 으로 승격하지도 않는다. 아래 공통 처리로 낙하해 status 가 로그에 남고
    #    `ProtocolViolation("unexpected status 404")` 로 fail-closed 된다
    #    (WS: persistent unavailable, REST: 미캐시 → stale fallback/PENDING).
    status = response.status_code
    logger.warning(
        "RevenueCat API 응답 오류",
        extra={"status": status, "user_id": user_id},
    )
    if status in _TRANSIENT_STATUSES or status >= 500:
        return ProviderUnavailable(status=status)
    if status in _MISCONFIGURED_STATUSES:
        return ProviderMisconfigured(status=status)
    if status == 400:
        return BadRequest()
    return ProtocolViolation(detail=f"unexpected status {status}")


def _result_to_legacy_tuple(result: RevenueCatResult) -> tuple[bool, bool]:
    """REST adapter — typed 결과를 `(is_premium, should_cache)`로 되접는 **단일 지점**.

    `Determined`만 `should_cache=True`이고 나머지 4변종은 전부 `(False, False)`다.
    구분력은 **WS 인가 경로**(`app/topic_authorization.py`)가 쓴다.

    ⚠️ **범위**: 이 매핑으로 **예외·전송 경로의 REST 동작은 구 코드와 동일**하다. 반면 두
    곳은 의도적으로 바꿨다 — **malformed 200**(§8.1 A4-1 hardening: 구 `(False, True)`/
    `(True, True)` → 신 `(False, False)`)과 **404**(2026-08-14 계약 교정: 구 `(False, True)`
    비구독 캐시 → 신 `(False, False)` 미캐시).
    ⛔ 따라서 "REST 동작이 한 비트도 바뀌지 않는다"고 적지 말 것 — 더는 사실이 아니다.
    """
    if isinstance(result, Determined):
        return (result.is_premium, True)
    return (False, False)


async def _check_revenuecat_entitlement(user_id: str, *, clock: Clock) -> tuple[bool, bool]:
    """
    RevenueCat REST API로 구독 상태 확인 (Async)
    Returns: (is_premium, should_cache)
      - should_cache=True: **authoritative 판정**(200/201 파싱 성공), 캐시해도 안전 —
        404 는 계약상 신규 사용자가 아니므로 여기 속하지 않는다(계약 밖 상태, 2026-08-14 교정)
      - should_cache=False: **판정을 신뢰할 수 없음** — 일시 장애(408·429·5xx·전송) / 설정 오류
        (401·403·API key 미설정) / 응답 형식 위반 / 400 / 예상 밖 내부 예외.
        캐시하면 유료 사용자 차단 위험이라 stale fallback·PENDING 경로로 보낸다.
        ⚠️ 구 docstring은 이걸 "일시적 오류"로만 적었는데, 지금은 성격이 다른 5개 부류를 포함한다
        (그 구분은 `fetch_revenuecat_result`의 typed 결과가 갖고 있고
         **WS 인가 경로**(`app/topic_authorization.py`)가 쓴다).

    ⚠️ 이 함수는 **REST 전용 adapter**로 남는다. `tests/test_subscription_clock.py`가
    `patch.object(subscription, "_check_revenuecat_entitlement", ...)`로 이 이름을 잡으므로
    모듈 레벨 함수 위치를 옮기지 말 것. **WS 인가 경로**(`app/topic_authorization.py`)는
    `fetch_revenuecat_result`를 쓴다.

    ⚠️ **광범위 `except`가 여기에만 있는 이유**: provider는 예상 밖 예외를 **그대로 전파**해야
    **WS 인가 경로**(`app/topic_authorization.py` — 일반 `Exception` 을 의도적으로 잡지 않는다)가
    programming 오류를 retryable로 오인하지 않는다. 반면 REST는 구 동작이
    "무슨 예외든 `(False, False)`"였으므로 **그 되접기를 이 adapter가 떠안는다**.
    ⚠️ 범위 주의 — 이 adapter가 보존하는 것은 **예외 경로**의 구 동작이다. malformed 200의
    분류(§8.1 A4-1 hardening)는 **의도적으로 승인된 REST 동작 변경**이라 여기서 되돌리지 않는다.
    """
    try:
        result = await fetch_revenuecat_result(user_id, clock=clock)
    except Exception as e:  # noqa: BLE001 — REST 호환 경계(위 docstring). strict는 이걸 하지 않는다.
        logger.warning("RevenueCat API 호출 실패", extra={"error": str(e)})
        return (False, False)
    return _result_to_legacy_tuple(result)


async def verify_premium_status(user_id: str, *, clock: Optional[Clock] = None) -> PremiumStatus:
    """
    Premium 구독 상태 확인 (5분 캐시, LRU 1000명, Stale fallback).

    Stale cache 정책으로 장애 시에도 기존 유료 사용자 보호:
    - 신선한 캐시 → 즉시 반환
    - Stale 캐시 + API 성공 → 새 값 반환 및 캐시 갱신
    - Stale 캐시 + API 오류 → stale 값 반환 (유료 사용자 보호)
    - 캐시 없음 + API 오류 → PENDING (재시도 필요)
    """
    if clock is None:
        clock = system_clock()

    if not user_id:
        return PremiumStatus.INACTIVE

    # 1. 캐시 확인
    cached_value, is_fresh = _cache.get(user_id, clock=clock)

    # 신선한 캐시가 있으면 즉시 반환
    if cached_value is not None and is_fresh:
        return PremiumStatus.ACTIVE if cached_value else PremiumStatus.INACTIVE

    # 2. RevenueCat API 호출 (캐시 없거나 stale인 경우)
    is_premium, should_cache = await _check_revenuecat_entitlement(user_id, clock=clock)

    # 3. API 성공 시 캐시 갱신
    if should_cache:
        if is_premium:
            _pending.clear(user_id)
            _cache.set(user_id, True, clock=clock)
            return PremiumStatus.ACTIVE

        # is_premium == False
        if cached_value is not None:
            _pending.clear(user_id)
            _cache.set(user_id, False, clock=clock)
            return PremiumStatus.INACTIVE

        pending_state = _pending.state(user_id, clock=clock)
        if pending_state == "active":
            return PremiumStatus.PENDING
        if pending_state == "expired":
            _pending.clear(user_id)
            _cache.set(user_id, False, clock=clock)
            return PremiumStatus.INACTIVE

        _pending.mark(user_id, clock=clock)
        return PremiumStatus.PENDING

    # 4. API 오류 시: stale 캐시가 있으면 사용 (유료 사용자 보호)
    if cached_value is not None:
        logger.info(
            "API 오류, stale 캐시 사용",
            extra={"user_id": user_id, "stale_value": cached_value},
        )
        return PremiumStatus.ACTIVE if cached_value else PremiumStatus.INACTIVE

    # 5. 캐시도 없고 API도 실패 → PENDING
    if _pending.state(user_id, clock=clock) == "none":
        _pending.mark(user_id, clock=clock)
    return PremiumStatus.PENDING


async def verify_premium_status_fresh(
    user_id: str, *, clock: Optional[Clock] = None
) -> PremiumStatus:
    """Cache-free premium verdict for recovery after a fresh WS denial.

    The regular REST path intentionally protects availability with a five-minute stale
    cache. Reusing that value after the topic arbiter just returned ``premium_required``
    can reopen a user from an older ACTIVE entry. A determined provider result repairs
    the regular cache; an unavailable or malformed result remains PENDING and never
    falls back to stale authority.
    """
    if clock is None:
        clock = system_clock()
    if not user_id:
        return PremiumStatus.INACTIVE

    result = await fetch_revenuecat_result(user_id, clock=clock)
    if not isinstance(result, Determined):
        return PremiumStatus.PENDING

    _pending.clear(user_id)
    _cache.set(user_id, result.is_premium, clock=clock)
    return PremiumStatus.ACTIVE if result.is_premium else PremiumStatus.INACTIVE


async def verify_premium(user_id: str, *, clock: Optional[Clock] = None) -> bool:
    return await verify_premium_status(user_id, clock=clock) == PremiumStatus.ACTIVE


__all__ = [
    # ⚠️ `Clock`/`system_clock`은 의도적으로 없다 — 정본은 `app/clock.py`.
    "verify_premium",
    "verify_premium_status",
    "verify_premium_status_fresh",
    "PremiumStatus",
    "invalidate_user_cache",
    "EntitlementCache",
]
