# app/bank_kb_crawler.py

# 표준 라이브러리
import logging

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal

BANK_NAME = 'kb'
KB_MIBANK_CODE = '004'

# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

KB_BANK_URL = 'https://obank.kbstar.com/quics?chgCompId=b101827&page=C101423'
SECOND_KB_BANK_URL = 'https://obank.kbstar.com/quics?page=C101423'
MIBANK_KB_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + KB_MIBANK_CODE

KB_BANK_SELECTORS = {   # SECOND_KB_BANK_SELECTORS 도 같음
    'usd-krw': '#inqueryTable > table:nth-child(2) > tbody > tr:nth-child(1) > td:nth-child(3)',
    'jpy-krw': '#inqueryTable > table:nth-child(2) > tbody > tr:nth-child(2) > td:nth-child(3)',
    'eur-krw': '#inqueryTable > table:nth-child(2) > tbody > tr:nth-child(3) > td:nth-child(3)',
    # 'cny-krw': '#inqueryTable > table:nth-child(2) > tbody > tr:nth-child(10) > td:nth-child(3)',
}

MIBANK_SELECTORS = {
    'usd-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(3) > td.right.counter.rollsty01',
    'jpy-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(2) > td.right.counter.rollsty01',
    'eur-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(4) > td.right.counter.rollsty01',
    # 'cny-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(1) > td.right.counter.rollsty01',
}

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

def crawl_and_save_kb_bank_exchange_rates():
    """KB국민은행 환율 크롤링"""
    db = SessionLocal()

    try:
        crawl_and_save_routine(KB_BANK_URL, KB_BANK_SELECTORS, db)
    except Exception as e:
        try:
            crawl_and_save_routine(SECOND_KB_BANK_URL, KB_BANK_SELECTORS, db)
        except Exception as e2:
            try:
                crawl_and_save_routine(MIBANK_KB_URL, MIBANK_SELECTORS, db)
            except Exception as e3:
                error_msg = f"모든 URL 실패: {str(e3)[:100]}"
                logger.exception(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
    finally:
        db.close()    


def crawl_and_save_routine(url: str, selectors: dict, db: Session) -> int:
    """크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    current_rates = {}
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        for pair, selector in selectors.items():
            rate_element = soup.select_one(selector)

            if not rate_element:
                logger.warning(f"⚠️ SELECTOR 오류: {pair}", extra={"pair": pair, "selector": selector, "bank": BANK_NAME})
                continue

            rate_text = rate_element.get_text(strip=True).replace(',', '')

            try:
                current_rate = float(rate_text)
                current_rates[pair] = current_rate
            except ValueError:
                logger.warning(f"⚠️ 유효하지 않은 환율: {pair}", extra={"pair": pair, "rate_text": rate_text, "bank": BANK_NAME})
                continue

    except Exception as e:
        logger.exception("⚠️ URL 오류", extra={"url": url, "bank": BANK_NAME})
        raise

    # DB 저장 및 변경 개수 반환
    if current_rates:
        return crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
    else:
        raise Exception(f"🈚️ {BANK_NAME}은행 환율 데이터 없음")
