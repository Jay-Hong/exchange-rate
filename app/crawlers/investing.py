# app/crawlers/investing.py

# 표준 라이브러리
import logging
import random
import threading
import time

# 서드파티 라이브러리
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

try:
    from curl_cffi import requests as cffi_requests
    _USE_CFFI = True
except Exception:
    import requests as cffi_requests
    _USE_CFFI = False

# 로컬 애플리케이션
from app import crud
from app.config import DXY_MODE
from app.database import SessionLocal
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT
from app.crawlers.utils import parse_rate_text

# 크롤러 이름
CRAWLER_NAME = "investing"

FIRST_INVESTING_URL = 'https://kr.investing.com/currencies/exchange-rates-table'              # Main
SECOND_INVESTING_URL = 'https://sslfxrates.investing.com/index_exchange.php?force_lang=18'    # API
INVESTING_SELECTORS = {
    'usd-krw': '#last_12_28',
    'jpy-krw': '#last_2_28',
    'eur-krw': '#last_17_28',
    # 'usd-jpy': '#last_12_2',
    # 'TES-EST' : '#exchange_rates_1 > thead > tr > th.left.first'
}
SCALED_CURRENCY_PAIRS = {"jpy-krw": 100}

# DXY (달러지수) — exchange-rates-table에서 동반 추출
DXY_SELECTOR = '#sb_last_8827'
DXY_RATE_RANGE = (80.0, 130.0)
DXY_FALLBACK_COOLDOWN_SECONDS = 60  # 셀렉터 실패 시 폴백 호출 간격 제한
_dxy_fallback_last_called = 0.0     # time.monotonic() 기준

# Investing 전용 UA 풀 (전역 HEADERS는 유지)
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

# Circuit breaker 상태
_state_lock = threading.Lock()
_consecutive_403 = 0
_cooldown_until = 0.0
_blocked = False
_last_block_summary = 0.0

# Jitter 설정 (초)
JITTER_MAX_SECONDS = 2.0

# 차단 상태 요약 로그 주기 (초)
BLOCK_LOG_INTERVAL_SECONDS = 300

# 로거 설정
logger = logging.getLogger("exchange_rate.crawler.investing")

class InvestingForbidden(Exception):
    """Investing Cloudflare 403 차단"""


def _get_cooldown_seconds(count: int) -> int:
    if count >= 20:
        return 15 * 60
    if count >= 10:
        return 5 * 60
    if count >= 5:
        return 60
    return 0


def _build_headers(impersonate: str) -> dict:
    headers = dict(HEADERS)
    if impersonate.startswith("safari"):
        headers["User-Agent"] = random.choice(SAFARI_UA_POOL)
    else:
        headers["User-Agent"] = random.choice(UA_POOL)
    return headers


def _http_get(url: str, headers: dict):
    if _USE_CFFI:
        return cffi_requests.get(
            url,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            impersonate=CFFI_IMPERSONATE,
        )
    return cffi_requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)


def _mark_blocked(now: float) -> None:
    global _consecutive_403, _cooldown_until, _blocked, _last_block_summary
    _consecutive_403 += 1
    cooldown = _get_cooldown_seconds(_consecutive_403)
    if cooldown:
        _cooldown_until = max(_cooldown_until, now + cooldown)

    if not _blocked:
        logger.error(
            "🔴 Investing 차단 시작",
            extra={"count": _consecutive_403, "cooldown_seconds": cooldown},
        )
        _blocked = True
        _last_block_summary = now
        return

    if now - _last_block_summary >= BLOCK_LOG_INTERVAL_SECONDS:
        remaining = max(0, int(_cooldown_until - now))
        logger.warning(
            "⚠️ Investing 차단 지속",
            extra={
                "count": _consecutive_403,
                "cooldown_seconds": cooldown,
                "cooldown_remaining_seconds": remaining,
            },
        )
        _last_block_summary = now


def _mark_success() -> None:
    global _consecutive_403, _cooldown_until, _blocked, _last_block_summary
    if _blocked:
        logger.warning("🟢 Investing 차단 해제", extra={"previous_403_count": _consecutive_403})
    _consecutive_403 = 0
    _cooldown_until = 0.0
    _blocked = False
    _last_block_summary = 0.0


def _should_skip_due_to_cooldown(now: float) -> bool:
    global _last_block_summary
    if now < _cooldown_until:
        if now - _last_block_summary >= BLOCK_LOG_INTERVAL_SECONDS:
            remaining = max(0, int(_cooldown_until - now))
            logger.warning(
                "⏸️ Investing 쿨다운 중",
                extra={"cooldown_remaining_seconds": remaining, "consecutive_403": _consecutive_403},
            )
            _last_block_summary = now
        return True
    return False


def crawl_and_save_investing_exchange_rates():
    """Investing.com 환율 크롤링"""
    db = SessionLocal()

    try:
        now = time.monotonic()
        with _state_lock:
            if _should_skip_due_to_cooldown(now):
                return

        time.sleep(random.uniform(0, JITTER_MAX_SECONDS))

        first_403 = False
        second_403 = False

        try:
            logger.info("FIRST_INVESTING_URL 시도", extra={"bank": CRAWLER_NAME})
            crawl_and_save_routine(FIRST_INVESTING_URL, INVESTING_SELECTORS, db, headers=_build_headers(CFFI_IMPERSONATE))
            with _state_lock:
                _mark_success()
            return
        except InvestingForbidden:
            first_403 = True
        except Exception:
            logger.exception("FIRST_INVESTING_URL 크롤링 실패", extra={"url": FIRST_INVESTING_URL})

        try:
            logger.info("SECOND_INVESTING_URL 시도", extra={"bank": CRAWLER_NAME})
            crawl_and_save_routine(SECOND_INVESTING_URL, INVESTING_SELECTORS, db, headers=_build_headers(CFFI_IMPERSONATE))
            with _state_lock:
                _mark_success()
            return
        except InvestingForbidden:
            second_403 = True
        except Exception as e2:
            logger.exception("SECOND_INVESTING_URL 크롤링 실패", extra={"url": SECOND_INVESTING_URL})

        if first_403 and second_403:
            with _state_lock:
                _mark_blocked(time.monotonic())
            return

    finally:
        db.close()


def crawl_and_save_routine(url: str, selectors: dict, db: Session, headers: dict) -> int:
    """
    크롤링 + DB 저장 루틴

    Returns:
        변경된 레코드 개수
    """
    current_rates = {}
    dxy_rate = None

    try:
        response = _http_get(url, headers=headers)
        if response.status_code == 403:
            raise InvestingForbidden(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        for pair, selector in selectors.items():
            rate_element = soup.select_one(selector)

            if not rate_element:
                logger.warning(f"⚠️ SELECTOR 오류: {pair}", extra={"pair": pair, "selector": selector})
                continue

            rate_text = rate_element.get_text(strip=True)

            try:
                current_rate = parse_rate_text(rate_text)
                if pair in SCALED_CURRENCY_PAIRS:
                    current_rate *= SCALED_CURRENCY_PAIRS[pair]
                current_rates[pair] = current_rate
            except ValueError:
                logger.warning(f"⚠️ 유효하지 않은 환율: {pair}", extra={"pair": pair, "rate_text": rate_text})
                continue

        if DXY_MODE == "futures_coupled":
            # DXY 동반 추출 (exchange-rates-table에서만 존재)
            dxy_element = soup.select_one(DXY_SELECTOR)
            if dxy_element:
                try:
                    dxy_text = dxy_element.get_text(strip=True).replace(",", "")
                    dxy_rate = float(dxy_text)
                    if not (DXY_RATE_RANGE[0] <= dxy_rate <= DXY_RATE_RANGE[1]):
                        logger.warning("⚠️ DXY 범위 초과", extra={"rate": dxy_rate, "range": DXY_RATE_RANGE})
                        dxy_rate = None
                except (ValueError, AttributeError):
                    logger.warning("⚠️ DXY 파싱 실패", extra={"selector": DXY_SELECTOR})

    except InvestingForbidden:
        raise
    except Exception:
        logger.exception("⚠️ URL 오류", extra={"url": url})
        raise

    # DB 저장: 환율
    if current_rates:
        count = crud.insert_investing_rates_into_db(db=db, current_rates=current_rates)
    else:
        raise Exception("🈚️ Investing 환율 데이터 없음")

    if DXY_MODE == "futures_coupled":
        # DB 저장: DXY (환율 저장 성공 후)
        if dxy_rate is not None:
            crud.insert_dxy_rate_into_db(db=db, rate=dxy_rate, source="investing")
        else:
            # DXY 셀렉터가 없는 페이지 (예: sslfxrates)이거나 파싱 실패 시 폴백
            _try_dxy_fallback(db)

    return count


def _try_dxy_fallback(db: Session) -> None:
    """
    DXY 1차 추출(exchange-rates-table) 실패 시 2차/3차 폴백 시도

    2차: /currencies/us-dollar-index (같은 선물/CFD 상품)
    3차: Yahoo Finance (yfinance)

    셀렉터 장기 파손 시 retry storm 방지를 위해 60초 cooldown 적용.
    """
    global _dxy_fallback_last_called
    now = time.monotonic()
    if now - _dxy_fallback_last_called < DXY_FALLBACK_COOLDOWN_SECONDS:
        return
    _dxy_fallback_last_called = now

    try:
        # 순환 참조 방지 + on-demand 호출이므로 함수 내부 import
        from app.crawlers.dxy import fetch_dxy_from_investing_fallback, fetch_dxy_from_yahoo

        # 2차: Investing.com /currencies/us-dollar-index
        try:
            rate = fetch_dxy_from_investing_fallback()
            crud.insert_dxy_rate_into_db(db=db, rate=rate, source="investing")
            logger.info("📦 DXY 2차 폴백 저장", extra={"rate": rate, "source": "investing", "fallback_url": "/currencies/us-dollar-index"})
            return
        except Exception:
            logger.warning("⚠️ DXY 2차 폴백 실패", extra={"crawler": CRAWLER_NAME})

        # 3차: Yahoo Finance
        rate = fetch_dxy_from_yahoo()
        crud.insert_dxy_rate_into_db(db=db, rate=rate, source="yahoo")
        logger.info("📦 DXY Yahoo 폴백 저장", extra={"rate": rate})

    except Exception:
        # DXY는 보조지표 → 실패해도 환율 크롤링에 영향 없음
        logger.warning("⚠️ DXY 전체 fallback 실패 (무시)", extra={"crawler": CRAWLER_NAME})
