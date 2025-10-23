# app/bank_sc_crawler.py

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

BANK_NAME = 'sc'

SC_BANK_URL = 'https://www.standardchartered.co.kr/np/kr/pl/pn/ForeignExchange.jsp'    # 자정이후, 주말에는 환율정보 제공안함
SC_BANK_SELECTORS = {
    'usd-krw': '#tdUSD',
    'jpy-krw': '#tdJPY',
    'eur-krw': '#tdEUR',
    # 'cny-krw': 'tdCNY',
}

SECOND_SC_BANK_URL = 'https://www.standardchartered.co.kr/np/kr/pl/et/ExchangeRateP1.jsp'   # 자정이후, 주말에는 날짜변경 후 조회
SECOND_SC_BANK_SELECTOR = '#TMP_RATE' # usd-krw, jpy-krw, eru-krw 모두 selector 같음 (2,3,4번째 값)
SECOND_SC_BANK_PAIRS = ['usd-krw', 'jpy-krw', 'eur-krw']
MAX_DAYS_LOOKBACK = 12  # 최대 조회 가능한 과거 날짜 수
# SC제일은행 날짜 선택 selector (년/월/일 select 박스)
YEAR_SELECTOR = "#_CUR_YEAR"
MONTH_SELECTOR = "#_CUR_MONTH"
DAY_SELECTOR = "#_CUR_DAY"
SUBMIT_BUTTON_SELECTOR = "input[type='button'][value='조회 시작일']"  # 녹색 조회 버튼

MIBANK_SC_CODE = '023'
MIBANK_SC_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + MIBANK_SC_CODE
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

def crawl_and_save_sc_bank_exchange_rates():
    """SC제일은행 환율 크롤링"""
    db = SessionLocal()
    try:
        crawl_and_save_routine_selenium(SC_BANK_URL, SC_BANK_SELECTORS, db)
    except Exception as e:
        # 아래를 logger.exception으로 하지 않은이유 : 자정 이후/주말에는 이 URL이 안됨
        logger.info("SC_BANK_URL 크롤링 실패", extra={"url": SECOND_SC_BANK_URL})
        try:
            logger.info("SECOND_SC_BANK_URL 시도")
            # 아래를 두번째로 시도하는 이유 : Main으로 두었을때 가끔 환율조회가 안되어 - '#TMP_RATE' selector가 하나만 나타나 - 전날 환율이 저장 됨
            crawl_and_save_sc_first_routine_selenium(SECOND_SC_BANK_URL, SECOND_SC_BANK_SELECTOR, db)
        except Exception as e:
            logger.exception("SECOND_SC_BANK_URL 크롤링 실패", extra={"url": SC_BANK_URL})
            try:
                logger.info("MIBANK_SC_URL 시도")
                crawl_and_save_routine(MIBANK_SC_URL, MIBANK_SELECTORS, db)
            except Exception as e:
                logger.exception("MIBANK_SC_URL 크롤링 실패", extra={"url": MIBANK_SC_URL})
                error_msg = f"모든 URL 실패: {str(e)[:100]}"
                logger.exception(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
    finally:
        db.close()    


# SC제일은행 전용함수 for "https://www.standardchartered.co.kr/np/kr/pl/et/ExchangeRateP1.jsp"
def crawl_and_save_sc_first_routine_selenium(url: str, selector: str, db: Session) -> int:
    """SC제일은행 Selenium 크롤링 (날짜 변경 지원)

    - 평일 09:00~24:00: #TMP_RATE selector 존재 → 현재 날짜 환율 크롤링
    - 자정 이후/주말: #TMP_RATE selector 없음 → 과거 날짜로 조회 (최대 12일)
    """
    headlessoptions = webdriver.ChromeOptions()
    headlessoptions.add_argument("--headless=new");headlessoptions.add_argument("--window-size=1280x720")
    headlessoptions.add_argument("--disable-gpu");headlessoptions.add_argument("--disable-dev-shm-usage");headlessoptions.add_argument("--lang=ko_KR")
    headlessoptions.add_argument("--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=headlessoptions)

    current_rates = {}
    try:
        driver.get(url) # url 오류면 여기서 에러남
        wait = WebDriverWait(driver, 3)

        # Alert 처리 (자정 이후/주말에 뜨는 메시지)
        try:
            alert = driver.switch_to.alert
            alert_text = alert.text
            logger.debug(f"Alert 감지: {alert_text}", extra={"bank": BANK_NAME})
            alert.accept()
        except Exception:
            pass  # Alert 없으면 무시

        # #TMP_RATE selector 존재 여부 및 개수 확인
        try:
            rate_elements = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector)))
            element_count = len(rate_elements)

            # 여러 개 존재하면 평일 영업시간 → 현재 날짜 환율 크롤링
            if element_count > 1:
                logger.debug(f"✅ {BANK_NAME} 현재 날짜 환율 존재 (요소 {element_count}개)", extra={"bank": BANK_NAME, "element_count": element_count})
                current_rates = crawl_current_date_rates(driver, wait, selector)
            else:
                # 1개만 있으면 자정 이후/주말 → 과거 날짜로 조회
                logger.info(f"📅 {BANK_NAME} 과거 날짜 조회 시작 (#TMP_RATE 1개만 존재 - 자정/주말)", extra={"bank": BANK_NAME})
                current_rates = crawl_past_date_rates(driver, wait, selector)

        except Exception as e:
            # Selector가 아예 없으면 과거 날짜로 조회
            logger.info(f"📅 {BANK_NAME} 과거 날짜 조회 시작 (현재 날짜 환율 없음)", extra={"bank": BANK_NAME})
            current_rates = crawl_past_date_rates(driver, wait, selector)
        # db 저장
        if current_rates:
            return crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
        else:
            raise Exception(f"환율 데이터 추출 실패 (셀렉터 오류 또는 데이터 없음)")

    except Exception as e:
        error_msg = str(e)
        if "환율 데이터 추출 실패" in error_msg:
            logger.error(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception",
                extra={"url": url, "bank": BANK_NAME, "selector": selector, "error": error_msg})
        else:
            logger.exception("⚠️ URL 접속 또는 처리 오류",
                extra={"url": url, "bank": BANK_NAME, "error": error_msg})
        raise
    finally:
        driver.quit()


def crawl_current_date_rates(driver, wait, selector: str) -> dict:
    """현재 날짜 환율 크롤링 (평일 09:00~24:00)"""
    current_rates = {}
    try:
        rate_elements = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector)))
        for index, rate_element in enumerate(rate_elements):
            if index in (1, 2, 3):  # usd-krw 2, jpy-krw 3, eur-krw 4 번째 값
                rate_text = rate_element.text.strip().replace(',', '')
                try:
                    current_rate = float(rate_text)
                    current_rates[SECOND_SC_BANK_PAIRS[index-1]] = current_rate
                except ValueError:
                    logger.warning(f"⚠️ 유효하지 않은 환율: {SECOND_SC_BANK_PAIRS[index-1]}",
                        extra={"pair": SECOND_SC_BANK_PAIRS[index-1], "rate_text": rate_text, "bank": BANK_NAME})
                    continue
    except Exception as e:
        logger.warning(f"⚠️ SELECTOR 오류: {selector}", extra={"selector": selector, "bank": BANK_NAME})

    return current_rates


def crawl_past_date_rates(driver, wait, selector: str) -> dict:
    """과거 날짜 환율 크롤링 (자정 이후/주말)

    최대 MAX_DAYS_LOOKBACK일까지 과거로 이동하며 환율 조회
    SC제일은행은 년/월/일 select 박스를 각각 선택하는 방식
    IBK 크롤러 방식 적용: WebDriverWait로 페이지 로드 자동 감지
    """
    current_rates = {}
    selected_date = datetime.date.today()

    for i in range(MAX_DAYS_LOOKBACK):
        try:
            # ✅ 핵심: #TMP_RATE가 여러 개 로드될 때까지 대기 (IBK 방식)
            # WebDriverWait가 요소가 로드될 때까지 자동으로 대기
            rate_elements = wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector)))
            element_count = len(rate_elements)

            # Alert 처리 (#TMP_RATE 로드 후, 환율 크롤링 전에 처리)
            try:
                alert = driver.switch_to.alert
                alert.accept()
            except Exception:
                pass

            # 여러 개 존재하면 환율 크롤링 (1개면 의미 없는 값)
            if element_count > 1:
                current_rates = crawl_current_date_rates(driver, wait, selector)

                if current_rates:
                    logger.info(f"✅ {BANK_NAME} 환율 조회 성공 (날짜: {selected_date.strftime('%Y.%m.%d')}, 요소 {element_count}개)",
                        extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME, "element_count": element_count})
                    break  # 성공하면 종료
                else:
                    raise Exception("환율 데이터 없음")
            else:
                # 1개만 있으면 의미 없는 값 → 다음 날짜로
                logger.debug(f"⚠️ #TMP_RATE 1개만 존재 (의미 없음) - 다음 날짜로",
                    extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME})
                raise Exception("#TMP_RATE 1개만 존재")

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

                # ✅ 클릭 전 #TMP_RATE 개수 저장 (AJAX 응답 감지용)
                old_count = len(driver.find_elements(By.CSS_SELECTOR, selector))

                # "조회" 버튼 클릭 (<a> 태그, onclick="doList()")
                submit_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.button[title='조회']")))
                submit_button.click()

                # ⭐ #TMP_RATE 개수 변화 감지: 페이지가 갱신될 때까지 대기 (AJAX 응답 완료)
                # 최대 10초 대기 (일반적으로 1-2초 내 완료)
                WebDriverWait(driver, 3).until(
                    lambda d: len(d.find_elements(By.CSS_SELECTOR, selector)) != old_count
                )

            except Exception as e:
                logger.warning(f"⚠️ 날짜 변경 실패",
                    extra={"date": selected_date.strftime('%Y.%m.%d'), "bank": BANK_NAME, "error": str(e)})
                break

    return current_rates


def crawl_and_save_routine_selenium(url: str, selectors: dict, db: Session) -> int:
    """Selenium 크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    headlessoptions = webdriver.ChromeOptions()
    headlessoptions.add_argument("--headless=new");headlessoptions.add_argument("--window-size=1280x720")
    headlessoptions.add_argument("--disable-gpu");headlessoptions.add_argument("--disable-dev-shm-usage");headlessoptions.add_argument("--lang=ko_KR")
    headlessoptions.add_argument("--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=headlessoptions)

    current_rates = {}
    try:
        driver.get(url) # url 오류면 여기서 에러남
        wait = WebDriverWait(driver, 5)

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
            # 아래를 logger.exception으로 하지 않은이유 : 자정 이후/주말에는 이 URL이 안됨
            logger.info(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception",
                extra={"url": url, "bank": BANK_NAME, "selectors": list(selectors.keys()), "error": error_msg})
        else:
            logger.exception("⚠️ URL 접속 또는 처리 오류",
                extra={"url": url, "bank": BANK_NAME, "error": error_msg})
        raise
    finally:
        driver.quit()


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
