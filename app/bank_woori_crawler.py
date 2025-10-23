# app/bank_woori_crawler.py

# 표준 라이브러리
import datetime
import logging

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from sqlalchemy.orm import Session
from webdriver_manager.chrome import ChromeDriverManager

# 로컬 애플리케이션
from app import crud
from app.database import SessionLocal

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
MIBANK_KB_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + MIBANK_WOORI_CODE
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
            try:
                logger.info("MIBANK_WOORI_URL 시도")
                crawl_and_save_routine(MIBANK_KB_URL, MIBANK_SELECTORS, db)
            except Exception as e:
                logger.exception("MIBANK_WOORI_URL 크롤링 실패", extra={"url": MIBANK_KB_URL})
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


def crawl_and_save_woori_routine_selenium(url: str, selectors: dict, db: Session) -> int:
    """우리은행 Selenium 크롤링 + DB 저장 루틴 (날짜 변경 지원)

    - 평일 영업시간: 현재 날짜 환율 크롤링
    - 자정 이후/주말: 과거 날짜로 조회 (최대 12일)
    SC 크롤러 방식 적용 (Alert 없음, selector별 개별 확인)
    """
    headlessoptions = webdriver.ChromeOptions()
    headlessoptions.add_argument("--headless=new");headlessoptions.add_argument("--window-size=1280x720")
    headlessoptions.add_argument("--disable-gpu");headlessoptions.add_argument("--disable-dev-shm-usage");headlessoptions.add_argument("--lang=ko_KR")
    headlessoptions.add_argument("--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=headlessoptions)

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
            rate_text = rate_element.text.strip().replace(',', '')

            if not rate_text or rate_text == '-':
                logger.debug(f"{BANK_NAME} 환율 데이터 없음: {pair}", extra={"pair": pair, "bank": BANK_NAME})
                continue

            current_rate = float(rate_text)
            current_rates[pair] = current_rate

        except ValueError:
            logger.warning(f"⚠️ 유효하지 않은 환율: {pair}",
                extra={"pair": pair, "rate_text": rate_text, "bank": BANK_NAME})
        except Exception as e:
            logger.warning(f"⚠️ SELECTOR 오류: {pair}",
                extra={"pair": pair, "selector": selector, "bank": BANK_NAME})

    return current_rates


def crawl_woori_past_date_rates(driver, wait, selectors: dict) -> dict:
    """과거 날짜 환율 크롤링 (자정 이후/주말)

    최대 MAX_DAYS_LOOKBACK일까지 과거로 이동하며 환율 조회
    우리은행은 년/월/일 select 박스를 각각 선택하는 방식
    SC 크롤러 방식 적용: WebDriverWait로 페이지 로드 자동 감지, Alert 없음
    """
    current_rates = {}
    selected_date = datetime.date.today()

    for i in range(MAX_DAYS_LOOKBACK):
        try:
            # ✅ 핵심: 테이블 행 개수로 데이터 유무 판단 (WebDriverWait)
            wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, '#fxprint')))

            tr_elements = driver.find_elements(By.CSS_SELECTOR, '#fxprint > table > tbody > tr')
            row_count = len(tr_elements)

            # 행이 2개 이상이면 데이터 있음
            if row_count > 1:
                current_rates = crawl_woori_current_date_rates(driver, wait, selectors)

                if current_rates:
                    logger.info(f"✅ {BANK_NAME} 환율 조회 성공 (날짜: {selected_date.strftime('%Y.%m.%d')}, 행 {row_count}개)",
                        extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME, "row_count": row_count})
                    break  # 성공하면 종료
                else:
                    raise Exception("환율 데이터 없음")
            else:
                # 행이 1개 이하면 데이터 없음 → 다음 날짜로
                logger.debug(f"⚠️ {BANK_NAME} 환율 데이터 없음 (테이블 행 {row_count}개) - 다음 날짜로",
                    extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME, "row_count": row_count})
                raise Exception("환율 데이터 없음")

        except Exception as e:
            # 현재 날짜에 환율이 없으면 1일 전으로 이동
            selected_date = selected_date - datetime.timedelta(days=1)
            logger.info(f"📅 날짜 변경 {selected_date.strftime('%Y.%m.%d')}",
                extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME})

            try:
                # 년/월/일 select 박스에서 날짜 선택
                year_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, YEAR_SELECTOR))))
                month_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, MONTH_SELECTOR))))
                day_select = Select(wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, DAY_SELECTOR))))

                # 년도 선택 (value로 선택, 4자리: "2025")
                year_select.select_by_value(str(selected_date.year))

                # 월 선택 (value로 선택, 2자리: "01"~"12")
                month_select.select_by_value(f"{selected_date.month:02d}")

                # 일 선택 (value로 선택, 2자리: "01"~"31")
                day_select.select_by_value(f"{selected_date.day:02d}")

                # ✅ 클릭 전 테이블 행 개수 저장 (AJAX 응답 감지용)
                old_row_count = len(driver.find_elements(By.CSS_SELECTOR, '#fxprint > table > tbody > tr'))

                # "조회" 버튼 클릭 (input 태그, id="searchSubmit")
                submit_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "#searchSubmit")))
                submit_button.click()

                # ⭐ 테이블 행 개수 변화 감지: 페이지가 갱신될 때까지 대기 (AJAX 응답 완료)
                # 최대 3초 대기 (일반적으로 1-2초 내 완료)
                WebDriverWait(driver, 3).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, '#fxprint > table > tbody > tr')) != old_row_count
                )

            except Exception as e:
                logger.warning(f"⚠️ 날짜 변경 실패",
                    extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME, "error": str(e)})
                break

    return current_rates
