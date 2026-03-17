# app/crawlers/dxy_spot.py

"""
DXY(달러지수) spot 독립 크롤러

Primary: /indices/usdollar 의 __NEXT_DATA__
Fallback1: 같은 페이지의 CSS selector
Fallback2: Yahoo Finance
"""

# 표준 라이브러리
import json
import logging
import random
from typing import Tuple

# 서드파티 라이브러리
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    _USE_CFFI = True
except Exception:
    import requests as cffi_requests
    _USE_CFFI = False

# 로컬 애플리케이션
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT
from app.crawlers.dxy import fetch_dxy_from_yahoo

# 크롤러 이름
CRAWLER_NAME = "dxy"

# Spot 소스
DXY_SPOT_URL = "https://kr.investing.com/indices/usdollar"
DXY_SPOT_SELECTORS = [
    '[data-test="instrument-price-last"]',
    '[class*="text-5xl"]',
]
DXY_PRICE_PATH = ("props", "pageProps", "state", "indexStore", "instrument", "price")

# DXY 유효 범위
DXY_RATE_RANGE = (80.0, 130.0)

# curl_cffi TLS 지문 위장
CFFI_IMPERSONATE = "safari17_0"

SAFARI_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

# 마지막 원천 timestamp 추적 (spot primary stale 판정용)
_last_source_ts_ms = 0

# 로거 설정
logger = logging.getLogger("exchange_rate.crawler.dxy_spot")


def _build_headers() -> dict:
    headers = dict(HEADERS)
    headers["User-Agent"] = random.choice(SAFARI_UA_POOL)
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


def _is_valid_rate(rate: float) -> bool:
    return DXY_RATE_RANGE[0] <= rate <= DXY_RATE_RANGE[1]


def _fetch_spot_page() -> BeautifulSoup:
    response = _http_get(DXY_SPOT_URL, headers=_build_headers())
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _extract_next_data_price(soup: BeautifulSoup) -> Tuple[float, int]:
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise ValueError("__NEXT_DATA__ script not found")

    payload_raw = script.string or script.get_text()
    if not payload_raw:
        raise ValueError("__NEXT_DATA__ payload empty")

    payload = json.loads(payload_raw)
    price = payload
    for key in DXY_PRICE_PATH:
        price = price[key]

    rate = float(price["last"])
    if not _is_valid_rate(rate):
        raise ValueError(f"DXY spot 범위 초과: {rate}")

    source_ts_ms = int(price["lastUpdateTime"])
    return rate, source_ts_ms


def _extract_selector_price(soup: BeautifulSoup) -> float:
    for selector in DXY_SPOT_SELECTORS:
        element = soup.select_one(selector)
        if not element:
            continue

        rate_text = element.get_text(strip=True).replace(",", "")
        try:
            rate = float(rate_text)
        except ValueError:
            logger.warning(
                "⚠️ DXY spot CSS 파싱 실패",
                extra={"rate_text": rate_text, "selector": selector},
            )
            continue

        if _is_valid_rate(rate):
            return rate

        logger.warning(
            "⚠️ DXY spot CSS 값 범위 초과",
            extra={"rate": rate, "selector": selector, "range": DXY_RATE_RANGE},
        )

    raise ValueError("DXY spot CSS selector: 유효한 값을 찾을 수 없음")


def fetch_dxy_from_investing_spot() -> Tuple[float, int]:
    """Primary: /indices/usdollar __NEXT_DATA__."""
    soup = _fetch_spot_page()
    return _extract_next_data_price(soup)


def fetch_dxy_from_investing_spot_fallback() -> float:
    """Fallback1: /indices/usdollar 동일 페이지 CSS selector."""
    soup = _fetch_spot_page()
    return _extract_selector_price(soup)


def crawl_and_save_dxy_spot() -> None:
    """
    DXY spot 독립 수집 + DB 저장

    - Primary 성공 시 lastUpdateTime 동일 여부로 stale 판정
    - Primary 실패 시 같은 페이지 CSS selector 폴백
    - 최종 실패 시 Yahoo 폴백
    """
    global _last_source_ts_ms
    from app import crud
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        try:
            soup = _fetch_spot_page()

            try:
                rate, source_ts_ms = _extract_next_data_price(soup)
                if source_ts_ms == _last_source_ts_ms:
                    logger.debug(
                        "📼 DXY spot source timestamp 유지",
                        extra={"source_ts_ms": source_ts_ms, "source": "investing"},
                    )
                    return

                _last_source_ts_ms = source_ts_ms
                crud.insert_dxy_rate_into_db(db=db, rate=rate, source="investing")
                logger.info(
                    "📦 DXY spot primary 저장",
                    extra={"rate": rate, "source": "investing", "source_ts_ms": source_ts_ms},
                )
                return
            except Exception:
                logger.warning("⚠️ DXY spot primary 파싱 실패", exc_info=True)

            rate = _extract_selector_price(soup)
            crud.insert_dxy_rate_into_db(db=db, rate=rate, source="investing")
            logger.info("📦 DXY spot CSS 폴백 저장", extra={"rate": rate, "source": "investing"})
            return
        except Exception:
            logger.warning("⚠️ DXY spot Investing 수집 실패", exc_info=True)

        rate = fetch_dxy_from_yahoo()
        crud.insert_dxy_rate_into_db(db=db, rate=rate, source="yahoo")
        logger.info("📦 DXY Yahoo 폴백 저장", extra={"rate": rate, "source": "yahoo"})

    except Exception:
        logger.warning("⚠️ DXY spot 전체 fallback 실패 (무시)", extra={"crawler": CRAWLER_NAME}, exc_info=True)
    finally:
        db.close()
