# app/crawlers/citi.py

# 표준 라이브러리
import logging

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud, models
from app.database import SessionLocal
from app.crawlers.constants import (
    DEFAULT_TIMEOUT,
    HEADERS,
    MIBANK_RATE_RANGES,
    MIBANK_REQUIRED_CODES,
    MIBANK_REQUIRED_PAIRS,
)
from app.crawlers.utils import (
    crawl_mibank_rates,
    evaluate_rate_deviation,
    is_mibank_rate_reliable,
    parse_rate_text,
    validate_rate_ranges,
)

BANK_NAME = 'citi'

CITI_BANK_URL = 'https://www.citibank.co.kr/FxdExrt0100.act'
CITI_BANK_SELECTORS = {     # 아래 selector의 국가 순서가 계속 바뀐다
    '1st': '#content > ul > li:nth-child(1) > div', # 미국(USD)1,393.50하락-5.95
    '2nd': '#content > ul > li:nth-child(2) > div', # 중국(CNY)195.72하락-0.83
    '3rd': '#content > ul > li:nth-child(3) > div', # 유럽(EUR)1,641.12하락-2.53
    '4th': '#content > ul > li:nth-child(4) > div' # 일본(JPY)942.64하락-3.16
}
AFTER_CITI_BANK_SELECTORS = 'div:nth-child(2) > span'
PAIRS = ['usd-krw', 'jpy-krw', 'eur-krw']
CURRENCY_TEXTS = ['USD', 'JPY', 'EUR']

SECOND_CITI_BANK_URL = 'https://www.citibank.co.kr/FxdExrtFxrt0100.act'   # 주말에는 아예 안됨
SECOND_CITI_BANK_SELECTORS = {
    'usd-krw': '#tab01 > table > tbody > tr:nth-child(1) > td:nth-child(2)',
    'jpy-krw': '#tab01 > table > tbody > tr:nth-child(2) > td:nth-child(2)',
    'eur-krw': '#tab01 > table > tbody > tr:nth-child(3) > td:nth-child(2)',
    # 'cny-krw': '',
}

MIBANK_CITI_CODE = '027'
MIBANK_CITI_URL = 'https://www.mibank.me/exchange/bank/index.php?search_code=' + MIBANK_CITI_CODE

# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")


def _crawl_mibank_citi(db: Session) -> tuple[dict, dict]:
    rates = crawl_mibank_rates(
        MIBANK_CITI_URL,
        BANK_NAME,
        required_codes=MIBANK_REQUIRED_CODES,
        require_all=True,
    )

    validate_rate_ranges(rates, MIBANK_RATE_RANGES)

    last_info = crud.get_last_bank_rates_with_ts(db, BANK_NAME, MIBANK_REQUIRED_PAIRS)
    eval_result = evaluate_rate_deviation(rates, last_info, models.get_kst_now())
    return rates, eval_result


def crawl_and_save_citi_bank_exchange_rates():
    """씨티은행 환율 크롤링"""
    db = SessionLocal()
    try:
        logger.info(f"CITI_BANK_URL 시도", extra={"bank": BANK_NAME})
        crawl_and_save_citi_first_routine(CITI_BANK_URL, CITI_BANK_SELECTORS, db)
    except Exception as e:
        logger.exception("CITI_BANK_URL 크롤링 실패", extra={"url": CITI_BANK_URL})
        
        try:
            logger.info(f"SECOND_CITI_BANK_URL 시도", extra={"bank": BANK_NAME})
            crawl_and_save_routine(SECOND_CITI_BANK_URL, SECOND_CITI_BANK_SELECTORS, db)
        except Exception as e:
            logger.exception("SECOND_CITI_BANK_URL 크롤링 실패", extra={"url": SECOND_CITI_BANK_URL})
            
            # 3차 시도: MIBANK (자정/주말 차단, 일반 공휴일은 고려하지 못함)
            if is_mibank_rate_reliable():
                try:
                    logger.info(
                        "MIBANK_CITI_URL 시도 (평일 09:00 ~ 24:00 / 자정,주말 제외)",
                        extra={"bank": BANK_NAME},
                    )
                    rates, eval_result = _crawl_mibank_citi(db)

                    if eval_result["hard_fail"]:
                        logger.error(
                            "mibank hard_fail → 저장 보류",
                            extra={"bank": BANK_NAME, "details": eval_result["details"]},
                        )
                    else:
                        if eval_result["soft_fail"]:
                            logger.warning(
                                "mibank soft_fail → 마지막 폴백이므로 저장",
                                extra={"bank": BANK_NAME, "details": eval_result["details"]},
                            )
                        crud.insert_bank_rates_into_db(db=db, current_rates=rates, bank_name=BANK_NAME)
                except Exception as e3:
                    logger.exception("MIBANK_CITI_URL 크롤링 실패", extra={"url": MIBANK_CITI_URL})
                    error_msg = f"모든 URL 실패: {str(e3)[:100]}"
                    logger.error(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
            else:
                logger.warning(
                    f"⏰ MIBANK - {BANK_NAME} - 크롤링 건너뜀 (자정/주말)",
                    extra={
                        "reason": "is_mibank_rate_reliable failed",
                        "action": "DB 마지막 환율 데이터 유지 (클라이언트가 재사용)"
                    }
                )
                # 아무것도 하지 않음 → DB에 INSERT 없음 → 클라이언트가 마지막 CITI 환율 표시
    finally:
        db.close()


def crawl_and_save_citi_first_routine(url: str, selectors: dict, db: Session) -> int:
    """시티은행 첫번째 크롤링 루틴 (변경 개수 반환)"""
    current_rates = {}
    try:
        response = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        for order, selector in selectors.items():
            item = soup.select_one(selector)    # 미국(USD)1,393.50하락-5.95

            if not item:
                logger.warning(f"⚠️ SELECTOR 오류: {order}", extra={"order": order, "selector": selector, "bank": BANK_NAME})
                continue

            for index, currency in enumerate(CURRENCY_TEXTS):   # 환율찾아 매칭 'USD', 'JPY', 'EUR'
                if currency in item.get_text(): # 문자열 검색 - 'item.get_text()`가 curency를 포함하는지
                    rate_element = item.select_one(AFTER_CITI_BANK_SELECTORS)   # 1,393.50  ⬅ 환율만 가져오기
                    if not rate_element:
                        logger.warning(f"⚠️ SELECTOR 오류: {order}", extra={"order": order, "selector": f"{selector}{AFTER_CITI_BANK_SELECTORS}", "bank": BANK_NAME})
                        continue
                    rate_text = rate_element.get_text(strip=True)

                    try:
                        current_rate = parse_rate_text(rate_text)
                        current_rates[PAIRS[index]] = current_rate
                    except ValueError:
                        logger.warning(f"⚠️ 유효하지 않은 환율: {order}", extra={"order": order, "rate_text": rate_text, "bank": BANK_NAME})
                        continue

    except Exception as e:
        logger.exception("⚠️ URL 오류", extra={"url": url, "bank": BANK_NAME})
        raise

    # db 저장
    if current_rates:
        return crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)
    else:
        raise Exception(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception")


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
