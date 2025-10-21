# app/bank_hana_crawler.py

# 표준 라이브러리
import logging

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from sqlalchemy.orm import Session
from webdriver_manager.chrome import ChromeDriverManager

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal

BANK_NAME = 'hana'

# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

HANA_MIBANK_CODE = '005'

HANA_BANK_URL = 'https://www.hanabank.com/cms/rate/wpfxd651_01i_01.do?pbldDvCd=0'
SECOND_HANA_BANK_URL = 'https://www.kebhana.com/cont/mall/mall15/mall1501/index.jsp'
MIBANK_HANA_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + HANA_MIBANK_CODE

HANA_BANK_SELECTORS = {
    # '미국 USD': 'div.printdiv > table > tbody > tr:nth-child(1) > td.tc > a > u',
    'usd-krw': 'div.printdiv > table > tbody > tr:nth-child(1) > td:nth-child(9)',
    'jpy-krw': 'div.printdiv > table > tbody > tr:nth-child(2) > td:nth-child(9)',
    'eur-krw': 'div.printdiv > table > tbody > tr:nth-child(3) > td:nth-child(9)',
    # 'cny-krw': 'div.printdiv > table > tbody > tr:nth-child(4) > td:nth-child(9)',
}

SECOND_HANA_BANK_SELECTORS = {
    # '미국 USD': 'div.printdiv > table > tbody > tr:nth-child(1) > td.tc > a > u',
    'usd-krw': '#searchContentDiv > div.printdiv > table > tbody > tr:nth-child(1) > td:nth-child(9)',
    'jpy-krw': '#searchContentDiv > div.printdiv > table > tbody > tr:nth-child(2) > td:nth-child(9)',
    'eur-krw': '#searchContentDiv > div.printdiv > table > tbody > tr:nth-child(3) > td:nth-child(9)',
    # 'cny-krw': 'div.printdiv > table > tbody > tr:nth-child(4) > td:nth-child(9)',
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

def crawl_and_save_hana_bank_exchange_rates():
    """하나은행 환율 크롤링"""
    db = SessionLocal()
    try:
        crawl_and_save_routine(HANA_BANK_URL, HANA_BANK_SELECTORS, db)
    except Exception as e:
        logger.exception("HANA_BANK_URL 크롤링 실패", extra={"url": HANA_BANK_URL})
        try:
            logger.info("SECOND_HANA_BANK_URL 시도")
            crawl_and_save_hana_routine_selenium(SECOND_HANA_BANK_URL, SECOND_HANA_BANK_SELECTORS, db)
        except Exception as e:
            logger.exception("SECOND_HANA_BANK_URL 크롤링 실패", extra={"url": SECOND_HANA_BANK_URL})
            try:
                logger.info("MIBANK_HANA_URL 시도")
                crawl_and_save_routine(MIBANK_HANA_URL, MIBANK_SELECTORS, db)
            except Exception as e:
                logger.exception("MIBANK_HANA_URL 크롤링 실패", extra={"url": MIBANK_HANA_URL})
                error_msg = f"모든 URL 실패: {str(e)[:100]}"
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

    # db 저장
    if current_rates:
        return crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
    else:
        raise Exception(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception")


# 하나은행 전용 함수
def crawl_and_save_hana_routine_selenium(url: str, selectors: dict, db: Session) -> int:
    """하나은행 Selenium 크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    headlessoptions = webdriver.ChromeOptions()
    headlessoptions.add_argument("--headless=new");headlessoptions.add_argument("--window-size=1280x720")
    headlessoptions.add_argument("--disable-gpu");headlessoptions.add_argument("--disable-dev-shm-usage");headlessoptions.add_argument("--lang=ko_KR")
    headlessoptions.add_argument("--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=headlessoptions)

    current_rates = {}
    try:
        driver.get(url) # url 오류면 여기서 에러남
        wait = WebDriverWait(driver, 5)

        # iframe이 있다면 먼저 전환
        iframe = wait.until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
        driver.switch_to.frame(iframe)

        for pair, selector in selectors.items():
            try:
                rate_element = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, selector)))
                rate_text = rate_element.text.strip().replace(',', '')
            except Exception as e:
                logger.warning(f"⚠️ SELECTOR 오류: {pair}", extra={"pair": pair, "selector": selector, "bank": BANK_NAME})
                continue

            try:
                current_rate = float(rate_text)
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