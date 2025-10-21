# app/investing_crawler.py

# 표준 라이브러리
import logging

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal

# 로거 설정
logger = logging.getLogger("exchange_rate.crawler.investing")

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


HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Cache-Control': 'max-age=0'
}

def crawl_and_save_investing_exchange_rates():
    """Investing.com 환율 크롤링"""
    db = SessionLocal()

    try:
        crawl_and_save_routine(FIRST_INVESTING_URL, INVESTING_SELECTORS, db)

    except Exception as e:
        # 첫 번째 URL 실패 → 두 번째 URL 시도
        try:
            crawl_and_save_routine(SECOND_INVESTING_URL, INVESTING_SELECTORS, db)
        except Exception as e2:
            # 두 번째 URL도 실패 → 에러 기록
            error_msg = f"모든 URL 실패: {str(e2)[:100]}"
            logger.exception("❌ Investing 크롤링 실패 (모든 URL)", extra={"error": error_msg})

    finally:
        db.close()    


def crawl_and_save_routine(url: str, selectors: dict, db: Session) -> int:
    """
    크롤링 + DB 저장 루틴

    Returns:
        변경된 레코드 개수
    """
    current_rates = {}
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        for pair, selector in selectors.items():
            rate_element = soup.select_one(selector)

            if not rate_element:
                logger.warning(f"⚠️ SELECTOR 오류: {pair}", extra={"pair": pair, "selector": selector})
                continue

            rate_text = rate_element.get_text(strip=True).replace(',', '')

            try:
                current_rate = float(rate_text)
                if pair in SCALED_CURRENCY_PAIRS:
                    current_rate *= SCALED_CURRENCY_PAIRS[pair]
                current_rates[pair] = current_rate
            except ValueError:
                logger.warning(f"⚠️ 유효하지 않은 환율: {pair}", extra={"pair": pair, "rate_text": rate_text})
                continue

    except Exception as e:
        logger.exception("⚠️ URL 오류", extra={"url": url})
        raise

    # DB 저장 및 변경 개수 반환
    if current_rates:
        return crud.insert_investing_rates_into_db(db=db, current_rates=current_rates)
    else:
        raise Exception("🈚️ Investing 환율 데이터 없음")    
