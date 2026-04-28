# app/crawlers/dxy.py

"""
DXY(미국 달러지수) 폴백 유틸리티

- `fetch_dxy_from_investing_fallback()`: 미국달러지수 선물/CFD 계열 폴백
- `fetch_dxy_from_cnbc()`: 현물/운영 DXY CNBC 외부 폴백 (Yahoo보다 신선)
- `fetch_dxy_from_yahoo()`: 현물/운영 DXY Yahoo 최후 폴백
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

# 미국달러지수 선물/CFD 계열 폴백 URL
DXY_FALLBACK_URL = "https://kr.investing.com/currencies/us-dollar-index"
DXY_FALLBACK_SELECTORS = [
    '[data-test="instrument-price-last"]',
    '#last_last',
]

# DXY 유효 범위
DXY_RATE_RANGE = (80.0, 130.0)

# CNBC 설정 (ICE U.S. Dollar Index 공개 quote endpoint)
CNBC_DXY_URL = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
CNBC_DXY_SYMBOL = ".DXY"
CNBC_DXY_TIMEOUT = 5  # CNBC는 빠르므로 짧은 timeout

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
    Investing 폴백: /currencies/us-dollar-index에서 미국달러지수 선물/CFD 계열 값 파싱

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


def fetch_dxy_from_cnbc() -> float:
    """
    CNBC 외부 폴백: ICE U.S. Dollar Index 공개 quote endpoint에서 DXY 조회.

    응답 구조: {"FormattedQuoteResult": {"FormattedQuote": [{"last": "98.51", ...}]}}

    Returns:
        DXY 현재가 (예: 98.51)

    Raises:
        ValueError: 응답 파싱 실패 / 범위 초과
        Exception: 네트워크 오류 (호출자가 catch)
    """
    # on-demand 호출이므로 함수 내부 import
    import json
    import random

    headers = dict(HEADERS)
    headers["User-Agent"] = random.choice(SAFARI_UA_POOL)

    if _USE_CFFI:
        response = cffi_requests.get(
            CNBC_DXY_URL,
            params={"symbols": CNBC_DXY_SYMBOL, "requestMethod": "itv", "output": "json"},
            headers=headers,
            timeout=CNBC_DXY_TIMEOUT,
            impersonate=CFFI_IMPERSONATE,
        )
    else:
        response = cffi_requests.get(
            CNBC_DXY_URL,
            params={"symbols": CNBC_DXY_SYMBOL, "requestMethod": "itv", "output": "json"},
            headers=headers,
            timeout=CNBC_DXY_TIMEOUT,
        )

    response.raise_for_status()
    data = response.json() if hasattr(response, "json") else json.loads(response.text)

    try:
        quote = data["FormattedQuoteResult"]["FormattedQuote"][0]
        last_str = quote["last"]
    except (KeyError, IndexError, TypeError) as e:
        raise ValueError(f"CNBC 응답 구조 파싱 실패: {type(e).__name__}: {e}")

    try:
        rate = float(last_str)
    except (ValueError, TypeError):
        raise ValueError(f"CNBC last 필드 숫자 변환 실패: {last_str!r}")

    if not (DXY_RATE_RANGE[0] <= rate <= DXY_RATE_RANGE[1]):
        raise ValueError(f"CNBC DXY 범위 초과: {rate}")

    return rate


def fetch_dxy_from_yahoo() -> float:
    """
    최후 폴백: Yahoo Finance에서 DXY 현재가 조회 (10분 stale 한계)

    Returns:
        DXY 현재가 (예: 104.52)

    Raises:
        ValueError: 유효한 값을 가져올 수 없음
    """
    # 무거운 라이브러리이므로 함수 내부에서 import (폴백 시에만 로드)
    import yfinance as yf

    ticker = yf.Ticker(YAHOO_DXY_TICKER)
    fi = ticker.fast_info

    # 정규장 종가 우선, 없으면 lastPrice 폴백
    # (장 마감/주말에 lastPrice는 마지막 틱 값으로 정규장 종가와 다를 수 있음)
    price = fi.get("regularMarketPreviousClose") or fi.get("lastPrice")

    if price is None:
        raise ValueError(f"Yahoo DXY 가격 없음 (ticker={YAHOO_DXY_TICKER})")

    rate = float(price)
    if not (DXY_RATE_RANGE[0] <= rate <= DXY_RATE_RANGE[1]):
        raise ValueError(f"Yahoo DXY 범위 초과: {rate}")

    return rate
