# app/crawlers/dxy.py

"""
달러지수(DXY) 크롤러

Primary: Investing.com (kr.investing.com/indices/usdollar)
Fallback: Yahoo Finance (yfinance, DX-Y.NYB)

폴백 전환 조건 (설계 문서 §4.1):
1. Investing 연속 5회 실패 (403 + 파싱 + 네트워크 모두 포함)
2. 마지막 성공 시각이 5분 이상 stale
→ 둘 중 하나 충족 시 Yahoo 사용

복구 정책:
- 쿨다운 해제 후 Investing 재시도
- Investing 성공 시 즉시 원소스로 복귀
"""

# 표준 라이브러리
import logging
import random
import threading
import time

# 서드파티 라이브러리
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    _USE_CFFI = True
except Exception:
    import requests as cffi_requests
    _USE_CFFI = False

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT

# 크롤러 이름
CRAWLER_NAME = "dxy"

# ═════════════════════════════════════════════════════════════
# Investing.com 설정
# ═════════════════════════════════════════════════════════════

DXY_URL = "https://kr.investing.com/indices/usdollar"

# DXY 값 CSS Selector 후보 (우선순위 순)
DXY_SELECTORS = [
    '[data-test="instrument-price-last"]',
    '#last_last',
]

# DXY 값 유효 범위 (이상치 필터링)
DXY_RATE_RANGE = (80.0, 130.0)

# Investing 전용 UA 풀
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

SAFARI_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

# curl_cffi TLS 지문 위장 (Cloudflare 우회 목적)
CFFI_IMPERSONATE = "safari17_0"

# ═════════════════════════════════════════════════════════════
# Yahoo Finance 설정
# ═════════════════════════════════════════════════════════════

YAHOO_DXY_TICKER = "DX-Y.NYB"
# NOTE: yfinance는 내부적으로 requests를 사용하며 timeout 직접 제어 불가.
# 스케줄러 작업 타임아웃(45초)이 상위 보호 역할을 함.

# ═════════════════════════════════════════════════════════════
# 폴백 정책 상수
# ═════════════════════════════════════════════════════════════

FALLBACK_FAILURE_THRESHOLD = 5      # 연속 N회 실패 시 Yahoo 전환
FALLBACK_STALE_SECONDS = 300        # 마지막 성공 후 N초 경과 시 Yahoo 전환 (5분)

# ═════════════════════════════════════════════════════════════
# Circuit breaker 상태 (DXY 전용, investing.py와 독립)
# ═════════════════════════════════════════════════════════════

_state_lock = threading.Lock()

# 403 전용 (쿨다운 계산용)
_consecutive_403 = 0
_cooldown_until = 0.0
_blocked = False
_last_block_summary = 0.0

# 통합 실패 카운트 (403 + 파싱 + 네트워크 모두 포함, 폴백 판단용)
_consecutive_failures = 0
_last_success_at = 0.0  # time.monotonic() 기준, 0이면 아직 성공한 적 없음

# Jitter 설정 (초)
JITTER_MAX_SECONDS = 2.0

# 차단 상태 요약 로그 주기 (초)
BLOCK_LOG_INTERVAL_SECONDS = 300

# 로거 설정
logger = logging.getLogger("exchange_rate.crawler.dxy")


class DxyForbidden(Exception):
    """DXY Cloudflare 403 차단"""


# ─────────────────────────────────────────────────────────────
# Circuit breaker 내부 함수
# ─────────────────────────────────────────────────────────────

def _get_cooldown_seconds(count: int) -> int:
    """연속 403 횟수에 따른 쿨다운 시간"""
    if count >= 20:
        return 15 * 60
    if count >= 10:
        return 5 * 60
    if count >= 5:
        return 60
    return 0


def _build_headers() -> dict:
    """Safari UA + 공통 헤더 생성"""
    headers = dict(HEADERS)
    headers["User-Agent"] = random.choice(SAFARI_UA_POOL)
    return headers


def _http_get(url: str, headers: dict):
    """curl_cffi로 TLS 지문 위장 요청"""
    if _USE_CFFI:
        return cffi_requests.get(
            url,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            impersonate=CFFI_IMPERSONATE,
        )
    return cffi_requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)


def _mark_blocked(now: float) -> None:
    """403 차단 상태 기록 + 통합 실패 카운트 증가"""
    global _consecutive_403, _cooldown_until, _blocked, _last_block_summary
    global _consecutive_failures
    _consecutive_403 += 1
    _consecutive_failures += 1
    cooldown = _get_cooldown_seconds(_consecutive_403)
    if cooldown:
        _cooldown_until = max(_cooldown_until, now + cooldown)

    if not _blocked:
        logger.error(
            "🔴 DXY 차단 시작",
            extra={"count": _consecutive_403, "cooldown_seconds": cooldown},
        )
        _blocked = True
        _last_block_summary = now
        return

    if now - _last_block_summary >= BLOCK_LOG_INTERVAL_SECONDS:
        remaining = max(0, int(_cooldown_until - now))
        logger.warning(
            "⚠️ DXY 차단 지속",
            extra={
                "count": _consecutive_403,
                "cooldown_seconds": cooldown,
                "cooldown_remaining_seconds": remaining,
            },
        )
        _last_block_summary = now


def _mark_failure() -> None:
    """403 외 실패 (파싱, 네트워크 등) — 통합 실패 카운트만 증가"""
    global _consecutive_failures
    _consecutive_failures += 1


def _mark_success() -> None:
    """Investing 성공 — 모든 카운터 초기화"""
    global _consecutive_403, _cooldown_until, _blocked, _last_block_summary
    global _consecutive_failures, _last_success_at
    if _blocked:
        logger.warning("🟢 DXY 차단 해제", extra={"previous_403_count": _consecutive_403})
    _consecutive_403 = 0
    _cooldown_until = 0.0
    _blocked = False
    _last_block_summary = 0.0
    _consecutive_failures = 0
    _last_success_at = time.monotonic()


def _should_skip_due_to_cooldown(now: float) -> bool:
    """403 쿨다운 중이면 True (Investing 요청 스킵, Yahoo 폴백은 별도 판단)"""
    global _last_block_summary
    if now < _cooldown_until:
        if now - _last_block_summary >= BLOCK_LOG_INTERVAL_SECONDS:
            remaining = max(0, int(_cooldown_until - now))
            logger.warning(
                "⏸️ DXY 쿨다운 중",
                extra={"cooldown_remaining_seconds": remaining, "consecutive_403": _consecutive_403},
            )
            _last_block_summary = now
        return True
    return False


def _should_use_yahoo(now: float) -> bool:
    """
    Yahoo 폴백 필요 여부 판단

    조건 (둘 중 하나 충족 시 True):
    1. 연속 실패 >= FALLBACK_FAILURE_THRESHOLD (5회)
    2. 마지막 성공 > FALLBACK_STALE_SECONDS (5분) 경과 (성공 이력이 있는 경우)
    """
    if _consecutive_failures >= FALLBACK_FAILURE_THRESHOLD:
        return True
    if _last_success_at > 0 and (now - _last_success_at) > FALLBACK_STALE_SECONDS:
        return True
    return False


def get_dxy_circuit_state() -> dict:
    """DXY circuit breaker 상태 조회 (관리자 모니터링용)"""
    with _state_lock:
        now = time.monotonic()
        stale_seconds = (now - _last_success_at) if _last_success_at > 0 else None
        return {
            "consecutive_403": _consecutive_403,
            "consecutive_failures": _consecutive_failures,
            "blocked": _blocked,
            "cooldown_until": _cooldown_until,
            "last_success_age_seconds": round(stale_seconds, 1) if stale_seconds is not None else None,
            "would_use_yahoo": _should_use_yahoo(now),
        }


# ─────────────────────────────────────────────────────────────
# Investing.com 파싱
# ─────────────────────────────────────────────────────────────

def _parse_dxy_rate(html: str) -> float:
    """
    HTML에서 DXY 현재가 파싱

    여러 CSS Selector를 순서대로 시도하여 첫 번째 유효한 값 반환.
    유효 범위(80~130) 밖이면 ValueError.
    """
    soup = BeautifulSoup(html, 'html.parser')

    for selector in DXY_SELECTORS:
        element = soup.select_one(selector)
        if element:
            rate_text = element.get_text(strip=True).replace(",", "")
            try:
                rate = float(rate_text)
                if DXY_RATE_RANGE[0] <= rate <= DXY_RATE_RANGE[1]:
                    return rate
                logger.warning(
                    f"⚠️ DXY 값 범위 초과: {rate}",
                    extra={"rate": rate, "selector": selector, "range": DXY_RATE_RANGE}
                )
            except ValueError:
                logger.warning(
                    f"⚠️ DXY 파싱 실패: '{rate_text}'",
                    extra={"rate_text": rate_text, "selector": selector}
                )
                continue

    raise ValueError("DXY 유효한 값을 찾을 수 없음")


def crawl_dxy_from_investing() -> float:
    """
    Investing.com에서 DXY 현재가 크롤링 (DB 저장 없이 값만 반환)

    Raises:
        DxyForbidden: 403 차단
        ValueError: 유효한 값 파싱 실패
        Exception: 네트워크 등 기타 오류
    """
    headers = _build_headers()
    response = _http_get(DXY_URL, headers=headers)

    if response.status_code == 403:
        raise DxyForbidden(DXY_URL)
    response.raise_for_status()

    return _parse_dxy_rate(response.text)


# ─────────────────────────────────────────────────────────────
# Yahoo Finance 폴백
# ─────────────────────────────────────────────────────────────

def fetch_dxy_from_yahoo() -> float:
    """
    Yahoo Finance에서 DXY 현재가 조회

    Returns:
        DXY 현재가 (예: 104.52)

    Raises:
        ValueError: 유효한 값을 가져올 수 없음
    """
    # 무거운 라이브러리이므로 함수 내부에서 import (폴백 시에만 로드)
    import yfinance as yf

    ticker = yf.Ticker(YAHOO_DXY_TICKER)
    # fast_info로 현재가 조회 (가장 경량)
    price = ticker.fast_info.get("lastPrice")

    if price is None:
        raise ValueError(f"Yahoo DXY 가격 없음 (ticker={YAHOO_DXY_TICKER})")

    rate = float(price)
    if not (DXY_RATE_RANGE[0] <= rate <= DXY_RATE_RANGE[1]):
        raise ValueError(f"Yahoo DXY 범위 초과: {rate}")

    return rate


# ─────────────────────────────────────────────────────────────
# 메인 크롤링 함수 (스케줄러에서 호출)
# ─────────────────────────────────────────────────────────────

def crawl_and_save_dxy():
    """
    DXY 크롤링 + DB 저장 (스케줄러에서 호출)

    동작 흐름:
    1. 403 쿨다운 중이 아니면 Investing 시도
    2. Investing 성공 → source='investing'으로 저장, 모든 카운터 초기화
    3. Investing 실패 → 통합 실패 카운트 증가
    4. 폴백 조건 충족 시 (연속 5회 실패 OR 5분 stale) → Yahoo 시도
    5. Yahoo 성공 → source='yahoo'로 저장 (실패 카운터는 유지)
    """
    db = SessionLocal()
    saved = False

    try:
        now = time.monotonic()
        investing_attempted = False
        last_error = None

        # Step 1: 403 쿨다운 중이 아니면 Investing 시도
        with _state_lock:
            skip_investing = _should_skip_due_to_cooldown(now)

        if not skip_investing:
            time.sleep(random.uniform(0, JITTER_MAX_SECONDS))
            investing_attempted = True

            try:
                rate = crawl_dxy_from_investing()
                crud.insert_dxy_rate_into_db(db=db, rate=rate, source="investing")

                # DB 저장 성공 후에만 상태 초기화 (저장 실패 시 폴백 보호 유지)
                with _state_lock:
                    _mark_success()

                saved = True
                return

            except DxyForbidden as e:
                last_error = e
                with _state_lock:
                    _mark_blocked(time.monotonic())

            except Exception as e:
                last_error = e
                logger.exception("DXY Investing 크롤링 실패", extra={"crawler": CRAWLER_NAME})
                with _state_lock:
                    _mark_failure()

        # Step 2: Yahoo 폴백 판단
        if not saved:
            with _state_lock:
                use_yahoo = _should_use_yahoo(time.monotonic())

            if use_yahoo:
                try:
                    rate = fetch_dxy_from_yahoo()
                    crud.insert_dxy_rate_into_db(db=db, rate=rate, source="yahoo")
                    logger.info(
                        "📦 DXY Yahoo 폴백 저장",
                        extra={"rate": rate, "source": "yahoo", "crawler": CRAWLER_NAME}
                    )
                    saved = True
                except Exception as e:
                    last_error = e
                    logger.exception("DXY Yahoo 폴백 실패", extra={"crawler": CRAWLER_NAME})

        # 데이터 미저장 시 예외 전파 → make_request_crawler_wrapper가 실패 기록
        if not saved:
            raise last_error or RuntimeError("DXY 크롤링 실패: 데이터 미저장")

    finally:
        db.close()
