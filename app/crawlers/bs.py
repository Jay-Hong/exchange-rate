# app/crawlers/bs.py

# 표준 라이브러리
import logging

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud, models
from app.database import SessionLocal
from app.crawlers import bank_report
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
    validate_rate_ranges,
    extract_selector_rates,
    selector_routine_events,
)

BANK_NAME = 'bs'

BS_BANK_URL = 'https://ibank.busanbank.co.kr/ib20/mnu/PEBFRX006001001'
BS_BANK_SELECTORS = {
    'usd-krw': '#resultTable > tbody > tr:nth-child(1) > td:nth-child(2)',
    'jpy-krw': '#resultTable > tbody > tr:nth-child(2) > td:nth-child(2)',
    'eur-krw': '#resultTable > tbody > tr:nth-child(3) > td:nth-child(2)',
}

# SECOND_BS_BANK_URL = 'https://m.busanbank.co.kr/ib20/mnu/MWPFRX4000FRX20'   # 페이지가 존재하지 않습니다 (크롤링시)
# SECOND_BS_BANK_SELECTORS = {
#     'usd-krw': '#MWPFRX420000V00M_contents > div.ctg_frx.acc__wrap > div.info_wrap.type_full.fnAccoInfo > div:nth-child(1) > button > span:nth-child(2)',
#     'jpy-krw': '#MWPFRX420000V00M_contents > div.ctg_frx.acc__wrap > div.info_wrap.type_full.fnAccoInfo > div:nth-child(2) > button > span:nth-child(2)',
#     'eur-krw': '#MWPFRX420000V00M_contents > div.ctg_frx.acc__wrap > div.info_wrap.type_full.fnAccoInfo > div:nth-child(3) > button > span:nth-child(2)',
#     # 'cny-krw': '#MWPFRX420000V00M_contents > div.ctg_frx.acc__wrap > div.info_wrap.type_full.fnAccoInfo > div:nth-child(4) > button > span:nth-child(2)',
# }

MIBANK_BS_CODE = '032'
MIBANK_BS_URL = 'https://exchange.mibank.me/bank?bank_cd=' + MIBANK_BS_CODE

# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")


def _crawl_mibank_bs(db: Session, observer=None) -> tuple[dict, dict]:
    """`observer` 는 보고 전용 — 범위 검사·편차 평가의 입력·결과를 그대로 받아 적기만 한다."""
    rates = crawl_mibank_rates(
        MIBANK_BS_URL,
        BANK_NAME,
        required_codes=MIBANK_REQUIRED_CODES,
        require_all=True,
        observer=observer,
    )

    bank_report.record_range_check(
        observer, rates, lambda: validate_rate_ranges(rates, MIBANK_RATE_RANGES))

    last_info = crud.get_last_bank_rates_with_ts(db, BANK_NAME, MIBANK_REQUIRED_PAIRS)
    eval_result = evaluate_rate_deviation(rates, last_info, models.get_utc_now())
    if observer is not None:
        observer.deviation_evaluated(rates, eval_result)
    return rates, eval_result


def crawl_and_save_bs_bank_exchange_rates():
    """부산은행 환율 크롤링

    보고(`bank_report`)는 회차 시작·종료만 감싼다 — 반환·예외 전파·폴백 순서는 본문 그대로다.
    """
    report = bank_report.start_report(
        logger, BANK_NAME, tuple(BS_BANK_SELECTORS),
        (bank_report.OFFICIAL_PRIMARY, bank_report.MIBANK))
    try:
        _crawl_and_save_bs(report)
    except BaseException as error:
        bank_report.safely_report(report, "finish", error=error)
        raise
    bank_report.safely_report(report, "finish")


def _crawl_and_save_bs(report):
    official = bank_report.PathObserver(report, bank_report.OFFICIAL_PRIMARY)
    mibank = bank_report.PathObserver(report, bank_report.MIBANK)
    db = SessionLocal()
    try:
        logger.info(f"BS_BANK_URL 시도", extra={"bank": BANK_NAME})
        official.start()
        crawl_and_save_routine(BS_BANK_URL, BS_BANK_SELECTORS, db, observer=official)
        official.finish()
    except Exception as e:
        official.finish(error=e)
        logger.exception("BS_BANK_URL 크롤링 실패", extra={"url": BS_BANK_URL})
        
        # 2차 시도: MIBANK (자정/주말 차단, 일반 공휴일은 고려하지 못함)
        if is_mibank_rate_reliable():
            mibank.start()
            try:
                logger.info(
                    "MIBANK_BS_URL 시도 (평일 10:00 ~ 23:59 / 00:00~09:59,주말 제외)",
                    extra={"bank": BANK_NAME},
                )
                rates, eval_result = _crawl_mibank_bs(db, observer=mibank)

                if eval_result["hard_fail"]:
                    mibank.adoption("withheld", "deviation_hard_fail")
                    logger.error(
                        "mibank hard_fail → 저장 보류",
                        extra={"bank": BANK_NAME, "details": eval_result["details"]},
                    )
                else:
                    # soft_fail 여부는 편차 기록에 있다 — 여기서 결과를 다시 읽어 분기하지 않는다.
                    mibank.adoption("submitted", "deviation_not_hard_fail")
                    if eval_result["soft_fail"]:
                        logger.warning(
                            "mibank soft_fail → 마지막 폴백이므로 저장",
                            extra={"bank": BANK_NAME, "details": eval_result["details"]},
                        )
                    bank_report.record_writer_call(
                        mibank, rates,
                        lambda: crud.insert_bank_rates_into_db(db=db, current_rates=rates, bank_name=BANK_NAME,
                                                                observer=mibank))
            except Exception as e2:
                mibank.finish(error=e2)
                logger.exception("MIBANK_BS_URL 크롤링 실패", extra={"url": MIBANK_BS_URL})
                error_msg = f"모든 URL 실패: {str(e2)[:100]}"
                logger.error(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
            except BaseException as interrupted:
                # 취소 등은 기존대로 전파한다 — 종료 원인만 이 경로에 결속한다.
                mibank.finish(error=interrupted)
                raise
            else:
                mibank.finish()
        else:
            bank_report.safely_report(report, "policy_skipped", bank_report.MIBANK,
                                      "mibank_untrusted_window")
            logger.warning(
                f"⏰ MIBANK - {BANK_NAME} - 크롤링 건너뜀 (자정/주말)",
                extra={
                    "reason": "is_mibank_rate_reliable failed",
                    "action": "DB 마지막 환율 데이터 유지 (클라이언트가 재사용)"
                }
            )
            # 아무것도 하지 않음 → DB에 INSERT 없음 → 클라이언트가 마지막 BS 환율 표시
    except BaseException as interrupted:
        # 취소 등은 기존대로 폴백 없이 전파한다(`except Exception` 을 넓히지 않는다) — 종료 원인만 결속한다.
        official.finish(error=interrupted)
        raise
    finally:
        db.close()


def crawl_and_save_routine(url: str, selectors: dict, db: Session, observer=None) -> int:
    """크롤링 + DB 저장 루틴 (변경 개수 반환)

    `observer` 는 보고 전용(`bank_report.PathObserver`) — 실제 추출에 쓴 요소·값만 넘긴다.
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        current_rates = extract_selector_rates(
            soup, selectors, selector_routine_events(logger, BANK_NAME, observer))

    except Exception as e:
        logger.exception("⚠️ URL 오류", extra={"url": url, "bank": BANK_NAME})
        raise

    # db 저장
    if current_rates:
        return bank_report.record_writer_call(
            observer, current_rates,
            lambda: crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME,
                                                    observer=observer))
    else:
        raise Exception(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception")
