# app/crawlers/bs.py

# 표준 라이브러리
import logging

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT, SELENIUM_WAIT_TIMEOUT
from app.crawlers.utils import parse_rate_text, create_selenium_driver

BANK_NAME = 'bs'

BS_BANK_URL = 'https://ibank.busanbank.co.kr/ib20/mnu/PEBFRX006001001'
BS_BANK_SELECTORS = {
    'usd-krw': '#resultTable > tbody > tr:nth-child(1) > td:nth-child(2)',
    'jpy-krw': '#resultTable > tbody > tr:nth-child(2) > td:nth-child(2)',
    'eur-krw': '#resultTable > tbody > tr:nth-child(3) > td:nth-child(2)',
}

SECOND_BS_BANK_URL = 'https://m.busanbank.co.kr/ib20/mnu/MWPFRX4000FRX20'   # 페이지가 존재하지 않습니다 (크롤링시)
SECOND_BS_BANK_SELECTORS = {
    'usd-krw': '#MWPFRX420000V00M_contents > div.ctg_frx.acc__wrap > div.info_wrap.type_full.fnAccoInfo > div:nth-child(1) > button > span:nth-child(2)',
    'jpy-krw': '#MWPFRX420000V00M_contents > div.ctg_frx.acc__wrap > div.info_wrap.type_full.fnAccoInfo > div:nth-child(2) > button > span:nth-child(2)',
    'eur-krw': '#MWPFRX420000V00M_contents > div.ctg_frx.acc__wrap > div.info_wrap.type_full.fnAccoInfo > div:nth-child(3) > button > span:nth-child(2)',
    # 'cny-krw': '#MWPFRX420000V00M_contents > div.ctg_frx.acc__wrap > div.info_wrap.type_full.fnAccoInfo > div:nth-child(4) > button > span:nth-child(2)',
}

MIBANK_BS_CODE = '032'
MIBANK_BS_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + MIBANK_BS_CODE
MIBANK_SELECTORS = {
    'usd-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(3) > td.right.counter.rollsty01',
    'jpy-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(2) > td.right.counter.rollsty01',
    'eur-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(4) > td.right.counter.rollsty01',
    # 'cny-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(1) > td.right.counter.rollsty01',
}

# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

def crawl_and_save_bs_bank_exchange_rates():
    """부산은행 환율 크롤링"""
    db = SessionLocal()
    try:
        crawl_and_save_routine(BS_BANK_URL, BS_BANK_SELECTORS, db)
    except Exception as e:
        logger.exception("BS_BANK_URL 크롤링 실패", extra={"url": BS_BANK_URL})
        try:
            logger.info("SECOND_BS_BANK_URL 시도")
            crawl_and_save_routine_selenium(SECOND_BS_BANK_URL, SECOND_BS_BANK_SELECTORS, db)
        except Exception as e:
            logger.exception("SECOND_BS_BANK_URL 크롤링 실패", extra={"url": SECOND_BS_BANK_URL})
            try:
                logger.info("MIBANK_BS_URL 시도")
                crawl_and_save_routine(MIBANK_BS_URL, MIBANK_SELECTORS, db)
            except Exception as e:
                logger.exception("MIBANK_BS_URL 크롤링 실패", extra={"url": MIBANK_BS_URL})
                error_msg = f"모든 URL 실패: {str(e)[:100]}"
                logger.exception(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
    finally:
        db.close()    


def crawl_and_save_routine(url: str, selectors: dict, db: Session) -> int:
    """크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    current_rates = {}
    try:
        response = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        for pair, selector in selectors.items():
            rate_element = soup.select_one(selector)

            if not rate_element:
                logger.warning(f"⚠️ SELECTOR 오류: {pair}", extra={"pair": pair, "selector": selector, "bank": BANK_NAME})
                continue

            rate_text = rate_element.get_text(strip=True)

            try:
                current_rate = parse_rate_text(rate_text)
                current_rates[pair] = current_rate
            except ValueError:
                logger.warning(f"⚠️ 유효하지 않은 환율: {pair}", extra={"pair": pair, "rate_text": rate_text, "bank": BANK_NAME})
                continue

    except Exception as e:
        logger.exception("⚠️ URL 오류", extra={"url": url, "bank": BANK_NAME})
        raise

    # db 저장
    if current_rates:
        return crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
    else:
        raise Exception(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception")


def crawl_and_save_routine_selenium(url: str, selectors: dict, db: Session) -> int:
    """Selenium 크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    driver = create_selenium_driver()

    current_rates = {}
    try:
        driver.get(url) # url 오류면 여기서 에러남
        wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT)

        for pair, selector in selectors.items():
            try:
                rate_element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                rate_text = rate_element.text.strip().replace(',', '')
            except Exception as e:
                logger.warning(f"⚠️ SELECTOR 오류: {pair}", extra={"pair": pair, "selector": selector, "bank": BANK_NAME})
                continue

            try:
                current_rate = parse_rate_text(rate_text)
                current_rates[pair] = current_rate
            except ValueError:
                logger.warning(f"⚠️ 유효하지 않은 환율: {pair}", extra={"pair": pair, "rate_text": rate_text, "bank": BANK_NAME})
                continue

        # db 저장
        if current_rates:
            return crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
        else:
            raise Exception(f"환율 데이터 추출 실패 (셀렉터 오류 또는 데이터 없음)")

    except Exception as e:
        error_msg = str(e)
        if "환율 데이터 추출 실패" in error_msg:
            logger.error(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception",
                extra={"url": url, "bank": BANK_NAME, "selectors": list(selectors.keys()), "error": error_msg})
        else:
            logger.exception("⚠️ URL 접속 또는 처리 오류",
                extra={"url": url, "bank": BANK_NAME, "error": error_msg})
        raise
    finally:
        driver.quit()
