# app/crawlers/woori.py

# 표준 라이브러리
import datetime
import logging
import time

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT, SELENIUM_OPTIONS
from app.crawlers.utils import parse_rate_text, create_selenium_driver, is_mibank_rate_reliable

BANK_NAME = 'woori'

WOORI_BANK_URL = 'https://m.wooribank.com/mw/mws?withyou=MWFCE0005'
WOORI_BANK_SELECTORS = {
    'usd-krw': 'body > div.wrap > div.conts-wrap.p1 > div.list-wrap > ul > li:nth-child(1) > a > dl > div:nth-child(2) > dd > span',
    'jpy-krw': 'body > div.wrap > div.conts-wrap.p1 > div.list-wrap > ul > li:nth-child(2) > a > dl > div:nth-child(2) > dd > span',
    'eur-krw': 'body > div.wrap > div.conts-wrap.p1 > div.list-wrap > ul > li:nth-child(3) > a > dl > div:nth-child(2) > dd > span',
    # 'cny-krw': 'body > div.wrap > div.conts-wrap.p1 > div.list-wrap > ul > li:nth-child(8) > a > dl > div:nth-child(2) > dd > span',
}

SECOND_WOORI_BANK_URL = 'https://spib.wooribank.com/pib/Dream?withyou=CMCOM0184'    # 자정이후, 주말에는 날짜변경 후 조회
SECOND_WOORI_BANK_SELECTORS = {
    'usd-krw': '#fxprint > table > tbody > tr:nth-child(1) > td:nth-child(9)',
    'jpy-krw': '#fxprint > table > tbody > tr:nth-child(2) > td:nth-child(9)',
    'eur-krw': '#fxprint > table > tbody > tr:nth-child(3) > td:nth-child(9)',
    # 'cny-krw': '#fxprint > table > tbody > tr:nth-child(8) > td:nth-child(9)',
}
MAX_DAYS_LOOKBACK = 12  # 최대 조회 가능한 과거 날짜 수
# 우리은행 날짜 선택 selector (년/월/일 select 박스)
YEAR_SELECTOR = "#SELECT_DATE_601Y"
MONTH_SELECTOR = "#SELECT_DATE_601M"
DAY_SELECTOR = "#SELECT_DATE_601D"

MIBANK_WOORI_CODE = '020'
MIBANK_WOORI_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + MIBANK_WOORI_CODE
MIBANK_SELECTORS = {
    'usd-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(3) > td.right.counter.rollsty01',
    'jpy-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(2) > td.right.counter.rollsty01',
    'eur-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(4) > td.right.counter.rollsty01',
    # 'cny-krw': 'body > div.container_sub_banks_saving > div.right_contents > div.box_contents1 > table > tbody > tr:nth-child(1) > td.right.counter.rollsty01',
}


# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")

def crawl_and_save_woori_bank_exchange_rates():
    """우리은행 환율 크롤링"""
    db = SessionLocal()
    try:
        crawl_and_save_routine(WOORI_BANK_URL, WOORI_BANK_SELECTORS, db)
    except Exception as e:
        logger.exception("WOORI_BANK_URL 크롤링 실패", extra={"url": WOORI_BANK_URL})
        try:
            logger.info("SECOND_WOORI_BANK_URL 시도")
            crawl_and_save_woori_routine_selenium(SECOND_WOORI_BANK_URL, SECOND_WOORI_BANK_SELECTORS, db)
        except Exception as e:
            logger.exception("SECOND_WOORI_BANK_URL 크롤링 실패", extra={"url": SECOND_WOORI_BANK_URL})

            # 3차 시도: MIBANK (자정/주말 차단, 일반 공휴일은 고려하지 못함)
            if is_mibank_rate_reliable():
                try:
                    logger.info("MIBANK_WOORI_URL 시도 (평일 09:00 ~ 24:00 / 자정,주말 제외)")
                    crawl_and_save_routine(MIBANK_WOORI_URL, MIBANK_SELECTORS, db)
                except Exception as e2:
                    logger.exception("MIBANK_WOORI_URL 크롤링 실패", extra={"url": MIBANK_WOORI_URL})
                    error_msg = f"모든 URL 실패: {str(e2)[:100]}"
                    logger.error(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
            else:
                logger.warning(
                    f"⏰ {BANK_NAME} 크롤링 건너뜀 (자정/주말 + Selenium 실패)",
                    extra={
                        "reason": "is_mibank_rate_reliable & selenium failed",
                        "action": "DB 마지막 환율 데이터 유지 (클라이언트가 재사용)"
                    }
                )
                # 아무것도 하지 않음 → DB에 INSERT 없음 → 클라이언트가 마지막 WOORI 환율 표시
    finally:
        db.close()    


def crawl_and_save_routine(url: str, selectors: dict, db: Session) -> int:
    """크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    current_rates = {}
    try:
        response = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

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


def crawl_and_save_woori_routine_selenium(url: str, selectors: dict, db: Session) -> int:
    """우리은행 Selenium 크롤링 + DB 저장 루틴 (날짜 변경 지원)

    - 평일 영업시간: 현재 날짜 환율 크롤링
    - 자정 이후/주말: 과거 날짜로 조회 (최대 12일)
    SC 크롤러 방식 적용 (Alert 없음, selector별 개별 확인)
    """
    driver = create_selenium_driver()

    current_rates = {}
    try:
        driver.get(url)
        wait = WebDriverWait(driver, 3)

        # 현재 날짜 환율 확인 (테이블 행 개수로 판단)
        # 자정/주말: 테이블 행 1개(헤더만), 평일 영업시간: 여러 개(데이터 포함)
        try:
            # #fxprint 테이블이 로드될 때까지 대기
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '#fxprint')))

            # 테이블 행 개수 확인
            tr_elements = driver.find_elements(By.CSS_SELECTOR, '#fxprint > table > tbody > tr')
            row_count = len(tr_elements)

            # 행이 2개 이상이면 데이터 있음 (1개는 헤더)
            if row_count > 1:
                logger.debug(f"✅ {BANK_NAME} 현재 날짜 환율 존재 (행 {row_count}개)", extra={"bank": BANK_NAME, "row_count": row_count})
                current_rates = crawl_woori_current_date_rates(driver, wait, selectors)
            else:
                # 행이 1개 이하면 데이터 없음 → 과거 날짜 조회
                logger.info(f"📅 {BANK_NAME} 과거 날짜 조회 시작 (테이블 행 {row_count}개 - 데이터 없음)",
                    extra={"bank": BANK_NAME, "row_count": row_count})
                current_rates = crawl_woori_past_date_rates(driver, wait, selectors)

        except Exception as e:
            # 테이블 로드 실패 → 과거 날짜 조회
            logger.info(f"📅 {BANK_NAME} 과거 날짜 조회 시작 (테이블 로드 실패)", extra={"bank": BANK_NAME})
            current_rates = crawl_woori_past_date_rates(driver, wait, selectors)

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


def crawl_woori_current_date_rates(driver, wait, selectors: dict) -> dict:
    """현재 날짜 환율 크롤링 (평일 영업시간)"""
    current_rates = {}

    for pair, selector in selectors.items():
        try:
            rate_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
            rate_text = rate_element.text.strip()

            if not rate_text or rate_text == '-':
                logger.debug(f"{BANK_NAME} 환율 데이터 없음: {pair}", extra={"pair": pair, "bank": BANK_NAME})
                continue

            current_rate = parse_rate_text(rate_text)
            current_rates[pair] = current_rate

        except ValueError:
            logger.warning(f"⚠️ 유효하지 않은 환율: {pair}",
                extra={"pair": pair, "rate_text": rate_text, "bank": BANK_NAME})
        except Exception as e:
            logger.warning(f"⚠️ SELECTOR 오류: {pair}",
                extra={"pair": pair, "selector": selector, "bank": BANK_NAME})

    return current_rates


def crawl_woori_past_date_rates(driver, wait, selectors: dict) -> dict:
    """과거 날짜 환율 크롤링 (어제부터 시작)

    호출 조건: 오늘 날짜에 테이블 행 1개만 존재 (데이터 없음)
    동작: 어제(today-1)부터 최대 MAX_DAYS_LOOKBACK일 전까지 순회
    SC 크롤러 방식 완전 적용: 루프 시작 시 무조건 -1일, Alert 없음
    """
    current_rates = {}
    selected_date = datetime.date.today()

    for i in range(MAX_DAYS_LOOKBACK):
        # 어제부터 과거로 이동 (오늘은 이미 parent에서 확인)
        selected_date = selected_date - datetime.timedelta(days=1)
        logger.info(f"📅 날짜 변경 {selected_date.strftime('%Y.%m.%d')}",
            extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME})

        try:
            # 1. 날짜 선택 (년/월/일 select 박스)
            year_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, YEAR_SELECTOR))))
            year_select.select_by_value(str(selected_date.year))

            month_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, MONTH_SELECTOR))))
            month_select.select_by_value(f"{selected_date.month:02d}")

            day_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, DAY_SELECTOR))))
            day_select.select_by_value(f"{selected_date.day:02d}")

            logger.debug(f"📅 날짜 선택 완료: {selected_date.strftime('%Y.%m.%d')}", extra={"bank": BANK_NAME})

            # 2. 조회 버튼 클릭
            submit_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#searchSubmit")))
            logger.debug(f"🖱️ 조회 버튼 클릭", extra={"bank": BANK_NAME})
            submit_button.click()
            
            time.sleep(0.5)  # Table이 뜨기까지 짧은 대기

            # 3. AJAX 대기: 테이블이 다시 로드될 때까지 대기
            logger.debug(f"⏳ AJAX 응답 대기 중...", extra={"bank": BANK_NAME})
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '#fxprint')))

            # 4. 테이블 행 개수로 데이터 유무 확인
            tr_elements = driver.find_elements(By.CSS_SELECTOR, '#fxprint > table > tbody > tr')
            row_count = len(tr_elements)
            logger.debug(f"✅ AJAX 응답 완료 (테이블 행 {row_count}개)", extra={"bank": BANK_NAME})

            # 5. 환율 크롤링
            if row_count > 1:
                current_rates = crawl_woori_current_date_rates(driver, wait, selectors)

                if current_rates:
                    logger.info(f"✅ {BANK_NAME} 환율 조회 성공 (날짜: {selected_date.strftime('%Y.%m.%d')}, 행 {row_count}개)",
                        extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME, "row_count": row_count})
                    break  # 성공하면 종료
                else:
                    # 크롤링 실패 → 다음 날짜로
                    logger.debug(f"⚠️ {BANK_NAME} 환율 크롤링 실패 - 다음 날짜로",
                        extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME})
                    continue
            else:
                # 행이 1개 이하면 데이터 없음 → 다음 날짜로
                logger.debug(f"⚠️ {BANK_NAME} 환율 데이터 없음 (테이블 행 {row_count}개) - 다음 날짜로",
                    extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME, "row_count": row_count})
                continue

        except Exception as e:
            # 날짜 변경 실패 시에도 다음 날짜 계속 시도
            logger.warning(f"⚠️ 날짜 변경 또는 조회 실패 - 다음 날짜 시도",
                extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME, "error": str(e)[:100]})
            continue

    return current_rates
