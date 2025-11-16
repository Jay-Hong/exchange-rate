# app/crawlers/hana.py

# 표준 라이브러리
import logging
import sys

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

BANK_NAME = 'hana'

HANA_BANK_URL = 'https://www.hanabank.com/cms/rate/wpfxd651_01i_01.do?pbldDvCd=0'
HANA_BANK_SELECTORS = {
    # '미국 USD': 'div.printdiv > table > tbody > tr:nth-child(1) > td.tc > a > u',
    'usd-krw': 'div.printdiv > table > tbody > tr:nth-child(1) > td:nth-child(9)',
    'jpy-krw': 'div.printdiv > table > tbody > tr:nth-child(2) > td:nth-child(9)',
    'eur-krw': 'div.printdiv > table > tbody > tr:nth-child(3) > td:nth-child(9)',
    # 'cny-krw': 'div.printdiv > table > tbody > tr:nth-child(4) > td:nth-child(9)',
}

SECOND_HANA_BANK_URL = 'https://www.kebhana.com/cont/mall/mall15/mall1501/index.jsp'
SECOND_HANA_BANK_SELECTORS = {
    # '미국 USD': 'div.printdiv > table > tbody > tr:nth-child(1) > td.tc > a > u',
    'usd-krw': '#searchContentDiv > div.printdiv > table > tbody > tr:nth-child(1) > td:nth-child(9)',
    'jpy-krw': '#searchContentDiv > div.printdiv > table > tbody > tr:nth-child(2) > td:nth-child(9)',
    'eur-krw': '#searchContentDiv > div.printdiv > table > tbody > tr:nth-child(3) > td:nth-child(9)',
    # 'cny-krw': 'div.printdiv > table > tbody > tr:nth-child(4) > td:nth-child(9)',
}

MIBANK_HANA_CODE = '005'
MIBANK_HANA_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + MIBANK_HANA_CODE
MIBANK_SELECTORS = {
    'usd-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(3) > td.right.counter.rollsty01',
    'jpy-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(2) > td.right.counter.rollsty01',
    'eur-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(4) > td.right.counter.rollsty01',
    # 'cny-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(1) > td.right.counter.rollsty01',
}

# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

def crawl_and_save_hana_bank_exchange_rates():
    """하나은행 환율 크롤링 (Request → Selenium subprocess 폴백)"""
    db = SessionLocal()
    try:
        logger.info(f"HANA_BANK_URL 시도", extra={"bank": BANK_NAME})
        # 1차 시도: Request 기반 (빠름)
        crawl_and_save_routine(HANA_BANK_URL, HANA_BANK_SELECTORS, db)
    except Exception as e:
        logger.exception("HANA_BANK_URL 크롤링 실패", extra={"url": HANA_BANK_URL})
        try:
            # 2차 시도: Selenium → subprocess로 격리 실행 (타임아웃 보장)
            logger.info(f"SECOND_HANA_BANK_URL 시도 (Selenium subprocess)", extra={"bank": BANK_NAME})
            _run_selenium_subprocess_fallback('hana_selenium', timeout=45)
            logger.info("✅ Selenium subprocess 성공")
        except Exception as e:
            logger.exception("SECOND_HANA_BANK_URL 크롤링 실패", extra={"url": SECOND_HANA_BANK_URL})
            try:
                # 3차 시도: MIBANK (Request 기반)
                logger.info(f"MIBANK_HANA_URL 시도", extra={"bank": BANK_NAME})
                crawl_and_save_routine(MIBANK_HANA_URL, MIBANK_SELECTORS, db)
            except Exception as e:
                logger.exception("MIBANK_HANA_URL 크롤링 실패", extra={"url": MIBANK_HANA_URL})
                error_msg = f"모든 URL 실패: {str(e)[:100]}"
                logger.exception(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
    finally:
        db.close()


def _run_selenium_subprocess_fallback(subprocess_name: str, timeout: int):
    """
    Selenium 폴백을 subprocess로 실행 (동기 함수)

    Args:
        subprocess_name: runner.py의 CRAWLER_MAP 키 (예: 'hana_selenium')
        timeout: 타임아웃 (초)

    Raises:
        RuntimeError: subprocess 실패 시
    """
    import subprocess

    try:
        # subprocess 실행 (타임아웃 제어)
        result = subprocess.run(
            [sys.executable, "-m", "app.crawlers.runner", subprocess_name],
            capture_output=True,
            timeout=timeout,
            text=True
        )

        # Exit code 확인
        if result.returncode == 0:
            logger.debug(f"✅ Selenium subprocess 성공: {subprocess_name}")
        else:
            stderr = result.stderr[:500] if result.stderr else "No error output"
            logger.error(f"❌ Selenium subprocess 실패 (exit code: {result.returncode}): {stderr}")
            raise RuntimeError(f"Selenium subprocess failed with exit code {result.returncode}")

    except subprocess.TimeoutExpired:
        logger.warning(f"⏱️ Selenium subprocess 타임아웃 ({timeout}초): {subprocess_name}")
        raise RuntimeError(f"Selenium subprocess timeout after {timeout}s")
    except Exception as e:
        logger.exception(f"❌ Selenium subprocess 실행 오류: {subprocess_name}")
        raise


def crawl_and_save_hana_routine_selenium_entrypoint():
    """
    Selenium 폴백 엔트리포인트 (subprocess에서 호출)

    Notes:
        - runner.py에서 호출됨
        - DB 세션 자체 생성 및 관리
        - 독립 프로세스이므로 타임아웃 시 Chrome 포함 전체 종료
    """
    db = SessionLocal()
    try:
        return crawl_and_save_hana_routine_selenium(SECOND_HANA_BANK_URL, SECOND_HANA_BANK_SELECTORS, db)
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


# 하나은행 전용 함수
def crawl_and_save_hana_routine_selenium(url: str, selectors: dict, db: Session) -> int:
    """하나은행 Selenium 크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    driver = create_selenium_driver()

    current_rates = {}
    try:
        driver.get(url) # url 오류면 여기서 에러남
        wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT)

        # iframe이 있다면 먼저 전환
        iframe = wait.until(EC.presence_of_element_located((By.TAG_NAME, "iframe")))
        driver.switch_to.frame(iframe)

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
    finally:
        driver.quit()