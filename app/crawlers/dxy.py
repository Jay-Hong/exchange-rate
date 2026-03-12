# app/crawlers/dxy.py

"""
DXY(달러지수) 폴백 모듈

Primary 수집은 investing.py에서 exchange-rates-table과 함께 처리.
이 모듈은 1차 실패 시 on-demand 호출되는 2차/3차 폴백을 담당.

2차: /currencies/us-dollar-index (같은 선물/CFD 계열)
3차: Yahoo Finance (yfinance, DX-Y.NYB)
"""

# 표준 라이브러리
import logging

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

# 크롤러 이름
CRAWLER_NAME = "dxy"

# 2차 폴백 URL (같은 선물/CFD 상품)
DXY_FALLBACK_URL = "https://kr.investing.com/currencies/us-dollar-index"
DXY_FALLBACK_SELECTORS = [
    '[data-test="instrument-price-last"]',
    '#last_last',
]

# DXY 유효 범위
DXY_RATE_RANGE = (80.0, 130.0)

# Yahoo 설정
YAHOO_DXY_TICKER = "DX-Y.NYB"

# curl_cffi TLS 지문 위장
CFFI_IMPERSONATE = "safari17_0"

SAFARI_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

# 로거 설정
logger = logging.getLogger("exchange_rate.crawler.dxy")


def fetch_dxy_from_investing_fallback() -> float:
    """
    2차 폴백: /currencies/us-dollar-index에서 DXY 파싱

    Raises:
        ValueError: 유효한 값 파싱 실패
        Exception: 네트워크 등 기타 오류
    """
    # on-demand 호출이므로 함수 내부 import
    import random

    headers = dict(HEADERS)
    headers["User-Agent"] = random.choice(SAFARI_UA_POOL)

    if _USE_CFFI:
        response = cffi_requests.get(
            DXY_FALLBACK_URL, headers=headers,
            timeout=DEFAULT_TIMEOUT, impersonate=CFFI_IMPERSONATE,
        )
    else:
        response = cffi_requests.get(
            DXY_FALLBACK_URL, headers=headers, timeout=DEFAULT_TIMEOUT,
        )

    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')

    for selector in DXY_FALLBACK_SELECTORS:
        element = soup.select_one(selector)
        if element:
            rate_text = element.get_text(strip=True).replace(",", "")
            try:
                rate = float(rate_text)
                if DXY_RATE_RANGE[0] <= rate <= DXY_RATE_RANGE[1]:
                    return rate
                logger.warning(
                    "⚠️ DXY fallback 값 범위 초과",
                    extra={"rate": rate, "selector": selector, "range": DXY_RATE_RANGE}
                )
            except ValueError:
                logger.warning(
                    "⚠️ DXY fallback 파싱 실패",
                    extra={"rate_text": rate_text, "selector": selector}
                )
                continue

    raise ValueError("DXY fallback: 유효한 값을 찾을 수 없음")


def fetch_dxy_from_yahoo() -> float:
    """
    3차 폴백: Yahoo Finance에서 DXY 현재가 조회

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
