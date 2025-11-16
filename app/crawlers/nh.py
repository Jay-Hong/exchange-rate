# app/crawlers/nh.py

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
from app.crawlers.utils import parse_rate_text, create_selenium_driver, selenium_driver_context

BANK_NAME = 'nh'

NH_BANK_URL = 'https://branch.nonghyup.com/servlet/content/ip/ef/IPEF0002M.thtml'   # 메인 페이지로 우회하여 환율페이지 접속
# SECOND_NH_BANK_URL = 'https://branch.nonghyup.com/servlet/IPEFP0011I.view'    # 25/9/20 현재 URL 직접 접근시 크롤링 불가 (환율정보 제공X)
NH_MAIN_TO_EXCHANGE_RATES_PAGE = "#content_load_section > div.sec.sec_02.bg_sec_darkgray.pdt60.pdb36 > div > ul > li:nth-child(1) > dl > dd > ul > li:nth-child(2) > a > span"
NH_BANK_SELECTORS = {
    'usd-krw': '#result > div > table.tb_col.tb_pd5.t_center > tbody > tr:nth-child(1) > td:nth-child(9)',
    'jpy-krw': '#result > div > table.tb_col.tb_pd5.t_center > tbody > tr:nth-child(2) > td:nth-child(9)',
    'eur-krw': '#result > div > table.tb_col.tb_pd5.t_center > tbody > tr:nth-child(3) > td:nth-child(9)',
    # 'cny-krw': '#result > div > table.tb_col.tb_pd5.t_center > tbody > tr:nth-child(4) > td:nth-child(9)',
}

MIBANK_NH_CODE = '011'
MIBANK_NH_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + MIBANK_NH_CODE
MIBANK_SELECTORS = {
    'usd-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(3) > td.right.counter.rollsty01',
    'jpy-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(2) > td.right.counter.rollsty01',
    'eur-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(4) > td.right.counter.rollsty01',
    # 'cny-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(1) > td.right.counter.rollsty01',
}


# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

def crawl_and_save_nh_bank_exchange_rates():
    """농협은행 환율 크롤링"""
    db = SessionLocal()
    try:
        logger.info("MIBANK_NH_URL 시도")
        crawl_and_save_routine(MIBANK_NH_URL, MIBANK_SELECTORS, db)
    except Exception as e:
        logger.exception("MIBANK_NH_URL 크롤링 실패", extra={"url": MIBANK_NH_URL})
        try:
            logger.info("NH_BANK_URL 시도")
            crawl_and_save_nh_routine_selenium(NH_BANK_URL, NH_BANK_SELECTORS, db)
        except Exception as e:
            logger.exception("NH_BANK_URL 크롤링 실패", extra={"url": NH_BANK_URL})
            error_msg = f"모든 URL 실패: {str(e)[:100]}"
            logger.exception(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
    finally:
        db.close()


def crawl_and_save_nh_routine_selenium(url: str, selectors: dict, db: Session) -> int:
    """농협 전용 Selenium 크롤링"""
    current_rates = {}
    try:
        with selenium_driver_context() as driver:
            driver.get(url) # url 오류면 여기서 에러남
            wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT)

            # element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, NH_MAIN_TO_EXCHANGE_RATES_PAGE)))
            element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, NH_MAIN_TO_EXCHANGE_RATES_PAGE)))
            element.click()

            for pair, selector in selectors.items():
                try:
                    rate_element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                    rate_text = rate_element.text.strip()
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
