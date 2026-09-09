# app/crawlers/ibk.py

# 표준 라이브러리
import dataclasses
import datetime
import types
from dataclasses import dataclass
from enum import Enum
import logging
import math
import re
import threading
import time

# 서드파티 라이브러리
import requests
from bs4 import BeautifulSoup
from pytz import timezone
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud, models
from app import ibk_selenium_config
from app.ibk_selenium_adapter import build_operations as build_selenium_operations
from app.ibk_selenium_strict import (
    ACCEPTED as SELENIUM_ACCEPTED,
    NO_SESSION as SELENIUM_NO_SESSION,
    REJECTED as SELENIUM_REJECTED,
    UNAVAILABLE as SELENIUM_UNAVAILABLE,
    IbkSeleniumStrictCapture,
    IbkSeleniumStrictCapturer,
    read_page_source_contract_error,
)
from app.ibk_regression_guard import find_regressing_pairs
from app.ibk_result_builder import (
    IbkIntendedState,
    IbkObservationFailure,
    IbkOfficialObservation,
    IbkPreservationGrant,
    IbkRetentionReason,
    IbkWriteAttempt,
    build_ibk_result,
    capture_write_mode,
    read_ibk_db_snapshot,
)
from app.ibk_candidate_policy import (
    CandidateBudgetExhausted,
    CandidateSearchStop,
    plan_candidate_dates,
    search_candidates,
)
from app.ibk_result_protocol import IbkReason, IbkResult, IbkSource
from app.ibk_run_context import SERVICE_DATE_ROLLOVER_TIME
from app.ibk_selenium_observation import (
    IbkSeleniumObservation,
    compare_mappings,
)
from app.database import SessionLocal
from app.crawlers.constants import (
    DEFAULT_TIMEOUT,
    HEADERS,
    MAX_DAYS_LOOKBACK,
    MIBANK_RATE_RANGES,
    MIBANK_REQUIRED_CODES,
    MIBANK_REQUIRED_PAIRS,
    SELENIUM_WAIT_TIMEOUT_SHORT,
)
from app.crawlers.utils import (
    crawl_mibank_rates,
    evaluate_rate_deviation,
    is_mibank_rate_reliable,
    parse_rate_text,
    selenium_driver_context,
    validate_rate_ranges,
)

# 한국 시간대
KST = timezone('Asia/Seoul')

BANK_NAME = 'ibk'

IBK_BANK_URL = 'https://www.ibk.co.kr/fxtr/excRateList.ibk'
IBK_PAGE_ID = 'SM03020100'
IBK_DATE_REQUEST_DEFAULTS = {
    'pageId': IBK_PAGE_ID,
    'dsCd': '',
    'curCd': '',
    'ecrtInqyDscd': '01',
    'efpsId': '',
}
# 새 lookback 후보를 시작할 수 있는 soft budget이다. requests의 timeout은
# connect/read inactivity 기준이므로 이미 시작한 단일 요청의 wall time을 강제 종료하는
# hard deadline은 아니다.
IBK_DATED_REQUEST_SOFT_BUDGET_SECONDS = 12.0
#: 요청 하나를 **시작할 만한** 최소 잔여(초).
#: ⛔ 이 값을 timeout 의 **하한**으로 쓰면 안 된다. 그러면 (a) 기한이 이미 27초 지난
#:    상태에서도 0.5초짜리 요청을 걸어 "초과는 최대 0.5초" 가 거짓이 되고, (b) 호출자가
#:    준 `timeout=0.1` 이 0.5 로 **늘어난다**(둘 다 실측). 부족하면 늘리지 말고 **멈춘다**.
IBK_MIN_REQUEST_TIMEOUT_SECONDS = 0.5
#: Selenium 안전망을 **시작할 만한** 최소 잔여(초). 드라이버 생성·항해·연속 제출 가드
#: 3.5초·문서 교체 대기·읽기·정리를 합친 보수적 값이다.
#: ⛔ 이것은 **시도를 허용하는 기준**이지 완료 보장이 아니다. 미달이면 드라이버를 만들지
#:    않는다 — 예산이 없어 멈춘 자리에서 더 비싼 경로를 여는 것은 모순이다.
IBK_SELENIUM_ENTRY_BUDGET_SECONDS = 12.0



IBK_COMPLETION_CLOCK_SKEW_SECONDS = 120
IBK_DB_SAVE_LAG_TOLERANCE_SECONDS = 120
# 공식 상세 화면의 1회차가 08:26대에도 시작한 실측이 있어 08:30을 경계로
# 쓰면 신규 고시를 놓칠 수 있다. 야간 꼬리 종료(06:00)와 주간 시작 사이의
# 명확한 공백인 08:00을 조회기준일 rollover 경계로 사용한다.
# ⛔ 값을 여기 다시 적지 않는다. 부모의 expected_service_date 와 갈라지면 결과가
#    SERVICE_DATE_MISMATCH 로 전량 거부된다(app/ibk_run_context.py 가 단일 소유).
IBK_SERVICE_DATE_ROLLOVER_TIME = SERVICE_DATE_ROLLOVER_TIME
# 당일 조회기준일 화면은 첫 고시 전까지 날짜 readback만 있고 환율표가 없을 수
# 있다. 이 짧은 구간에 한해서만 직전 서비스 기준일을 검증하며, 이후에는 같은
# 표 부재를 계약 이상으로 보고 Selenium 안전망을 유지한다.
IBK_PREOPEN_PENDING_END_TIME = datetime.time(8, 35)
IBK_BANK_SELECTORS = {
    'usd-krw': '#contents_in > div.section_last > div.table_view_section2 > table > tbody > tr:nth-child(1) > td:nth-child(3)',
    'jpy-krw': '#contents_in > div.section_last > div.table_view_section2 > table > tbody > tr:nth-child(2) > td:nth-child(3)',
    'eur-krw': '#contents_in > div.section_last > div.table_view_section2 > table > tbody > tr:nth-child(3) > td:nth-child(3)',
    # '#contents_in > div.section_last > div.table_view_section2 > table > tbody > tr:nth-child(4) > td:nth-child(3)',
}

# 공식 표의 통화 코드 → 우리 pair. **파서와 관측이 같은 매핑을 쓴다** — 갈라지면
# 관측이 파서와 다른 것을 보게 되고 shadow 수치가 예측력을 잃는다.
IBK_CODE_TO_PAIR = {
    'USD': 'usd-krw',
'JPY': 'jpy-krw',
'EUR': 'eur-krw',
}
INPUT_SELECTOR = "#inDate"

MIBANK_IBK_CODE = '003'
MIBANK_IBK_URL = 'https://exchange.mibank.me/bank?bank_cd=' + MIBANK_IBK_CODE


# 로거 설정
logger = logging.getLogger(f"exchange_rate.crawler.{BANK_NAME}")


class DatedRequestOutcome(Enum):
    """날짜 지정 공식 요청의 처리 결과.

    ``PRESERVED``는 완전한 DB snapshot이 있는 공식 무고시·개장 전 표 준비
    상태 또는 과거값 회귀 위험을 확인해 현재 DB 값을 의도적으로 유지한
    경우다. 이 상태를 ``FALLBACK``과 분리해야 Selenium이 같은 과거값을
    다시 저장하며 회귀 차단을 우회하지 않는다.
    """

    OBSERVED = "observed"
    PRESERVED = "preserved"
    FALLBACK = "fallback"


class IbkLegacyDisposition(Enum):
    """기존 실행이 끝난 분기. 최종 OBSERVED/DEGRADED 의미 판정이 아니다."""

    CURRENT_REQUEST_RETURNED = "current_request_returned"
    DATED_OBSERVED = "dated_observed"
    DATED_PRESERVED = "dated_preserved"
    MIDNIGHT_SUPPRESSED = "midnight_suppressed"
    SELENIUM_RETURNED = "selenium_returned"
    MIBANK_WRITE_RETURNED = "mibank_write_returned"
    MIBANK_REJECTED = "mibank_rejected"
    MIBANK_FAILED = "mibank_failed"
    MIBANK_SKIPPED = "mibank_skipped"


@dataclass(frozen=True)
class IbkLegacyResult:
    """행동보존 리팩터의 로컬 provisional 결과 (부모 wire 계약 아님).

    분기 종료와 DB/신선도 검증을 혼동하지 않는다. 특히 write_return_count=0은
    unchanged 또는 쓰기정책 차단이며, None은 반환 개수를 수집하지 않았다는 뜻이다.
    DATED_OBSERVED도 기존 enum의 이름일 뿐 기대 서비스일 관측을 보증하지 않는다.
    최종 의미 계측 전까지 공개 진입 함수는 이 결과를 버리고 기존 None을 반환한다.
    """

    disposition: IbkLegacyDisposition
    selenium_attempts: int = 0
    write_return_count: int | None = None


class IbkRateTableAbsentError(ValueError):
    """요청 날짜 readback은 맞지만 환율표와 공식 무고시 코드가 모두 없는 상태.

    이 HTML 형태만으로 개장 전 대기와 계약 이상을 완전히 구분할 수는 없다.
    호출자는 당일의 짧은 개장 전 구간에서만 이전 기준일 조회를 허용하고, 그
    밖에서는 fail-closed로 Selenium 안전망에 넘겨야 한다.
    """


def _crawl_mibank_ibk(db: Session) -> tuple[dict, dict]:
    rates = crawl_mibank_rates(
        MIBANK_IBK_URL,
        BANK_NAME,
        required_codes=MIBANK_REQUIRED_CODES,
        require_all=True,
    )

    validate_rate_ranges(rates, MIBANK_RATE_RANGES)

    last_info = crud.get_last_bank_rates_with_ts(db, BANK_NAME, MIBANK_REQUIRED_PAIRS)
    eval_result = evaluate_rate_deviation(rates, last_info, models.get_utc_now())
    return rates, eval_result


def crawl_and_save_ibk_bank_exchange_rates():
    """기존 runner 계약 유지: 예외/None 반환 및 부모 계측 의미는 아직 변경하지 않는다."""
    crawl_ibk_legacy_result()


def crawl_ibk_legacy_result() -> IbkLegacyResult:
    """기업은행 기존 수집 동작을 실행하고 종료 분기만 타입으로 반환한다."""

    # 이 회차의 시작. 검증 관측(shadow)의 예산 가드가 이 기준으로 남은 여유를 잰다.
    run_started_at = time.monotonic()

    # ═══════════════════════════════════════════════════════════════════
    # 자정 전환기 Selenium 스킵 (00:00~00:05)
    # ═══════════════════════════════════════════════════════════════════
    # 날짜 지정 Request는 이 구간에도 안전하게 실행한다. 그 공식 Request가
    # 실패한 경우에만, UI가 불안정한 Selenium을 띄우지 않고 마지막 값을 유지한다.
    # 문제: 자정 직후 Selenium 날짜 변경 시 UI 불안정
    #   - 45초 타임아웃 발생
    #   - 캘린더가 랜덤한 날짜까지 이동하여 잘못된 환율 수집
    # ───────────────────────────────────────────────────────────────────
    now = datetime.datetime.now(KST)
    midnight_transition = now.hour == 0 and now.minute < 5
    selenium_attempts = 0

    db = SessionLocal()
    try:
        # 1차 시도: 08:00 이후만 조회 당일 GET.
        # 00:00~07:59 고시는 전 조회기준일 화면에 누적되므로 당일의
        # 빈 GET을 먼저 요청하지 않고 아래 날짜 지정 POST로 바로 간다.
        if now.time() >= IBK_SERVICE_DATE_ROLLOVER_TIME and try_crawl_with_requests(
            db,
            reference_time=now,
        ):
            logger.debug(f"✅ {BANK_NAME} Requests 크롤링 성공")
            return IbkLegacyResult(IbkLegacyDisposition.CURRENT_REQUEST_RETURNED)

        # 2차 시도: 공식 페이지의 날짜 지정 Request.
        # 00시 이후 고시는 조회기준일(전 영업일) 화면에 계속 누적되므로 브라우저로
        # 달력을 조작할 필요가 없다. 요청 날짜 readback과 3개 통화 완전성을 검증하고,
        # OBSERVED/PRESERVED가 아닌 FALLBACK일 때만 기존 Selenium 경로로 내려간다.
        dated_outcome = try_crawl_with_dated_requests(db, reference_time=now)
        if dated_outcome is DatedRequestOutcome.OBSERVED:
            logger.info(
                f"✅ {BANK_NAME} 날짜 지정 Requests 크롤링 성공",
                extra={"bank": BANK_NAME},
            )
            return IbkLegacyResult(IbkLegacyDisposition.DATED_OBSERVED)
        if dated_outcome is DatedRequestOutcome.PRESERVED:
            return IbkLegacyResult(IbkLegacyDisposition.DATED_PRESERVED)

        if midnight_transition:
            logger.warning(
                "⏸️ IBK 자정 전환기 Selenium 스킵 (날짜 지정 Request 실패)",
                extra={
                    "bank": BANK_NAME,
                    "reason": "midnight_transition_request_failed",
                    "time": now.strftime("%H:%M:%S"),
                    "action": "DB 마지막 환율 유지",
                },
            )
            return IbkLegacyResult(IbkLegacyDisposition.MIDNIGHT_SUPPRESSED)

        # 3차 시도: Selenium 날짜 변경 - 최대 3회 재시도 (Request 계약 변경 시 안전망)
        logger.info(f"➡️ {BANK_NAME} Selenium으로 전환 (환율 데이터 없음)", extra={"bank": BANK_NAME})
        for attempt in range(3):
            try:
                selenium_attempts += 1
                write_return_count = crawl_and_save_ibk_routine_selenium(
                    IBK_BANK_URL, IBK_BANK_SELECTORS, db, run_started_at=run_started_at
                )
                logger.info(f"✅ {BANK_NAME} Selenium 성공 (시도 {attempt+1}/3)")
                return IbkLegacyResult(
                    IbkLegacyDisposition.SELENIUM_RETURNED,
                    selenium_attempts=selenium_attempts,
                    write_return_count=write_return_count,
                )
            except Exception as e:
                logger.warning(f"⚠️ {BANK_NAME} Selenium 실패 (시도 {attempt+1}/3): {str(e)[:50]}")
                if attempt < 2:  # 마지막 시도 전이면
                    time.sleep(2)  # 2초 대기 후 재시도
                else:
                    raise  # 3회 실패 시 예외 발생

    except Exception as e:
        logger.exception("IBK_BANK_URL 크롤링 실패", extra={"url": IBK_BANK_URL})

        # 4차 시도: MIBANK (자정/주말 차단, 일반 공휴일은 고려하지 못함 ← Selenium 3회 재시도로 커버)
        if is_mibank_rate_reliable():
            try:
                logger.info(
                    "MIBANK_IBK_URL 시도 (평일 10:00 ~ 23:59 / 00:00~09:59,주말 제외)",
                    extra={"bank": BANK_NAME},
                )
                rates, eval_result = _crawl_mibank_ibk(db)

                if eval_result["hard_fail"]:
                    logger.error(
                        "mibank hard_fail → 저장 보류",
                        extra={"bank": BANK_NAME, "details": eval_result["details"]},
                    )
                    return IbkLegacyResult(
                        IbkLegacyDisposition.MIBANK_REJECTED,
                        selenium_attempts=selenium_attempts,
                    )
                else:
                    if eval_result["soft_fail"]:
                        logger.warning(
                            "mibank soft_fail → 마지막 폴백이므로 저장",
                            extra={"bank": BANK_NAME, "details": eval_result["details"]},
                        )
                    write_return_count = crud.insert_bank_rates_into_db(db=db, current_rates=rates, bank_name=BANK_NAME)
                    return IbkLegacyResult(
                        IbkLegacyDisposition.MIBANK_WRITE_RETURNED,
                        selenium_attempts=selenium_attempts,
                        write_return_count=write_return_count,
                    )
            except Exception as e2:
                logger.exception("MIBANK_IBK_URL 크롤링 실패", extra={"url": MIBANK_IBK_URL})
                error_msg = f"모든 URL 실패: {str(e2)[:100]}"
                logger.error(f"❌ {BANK_NAME} 크롤링 실패 (모든 URL)", extra={"error": error_msg})
                return IbkLegacyResult(
                    IbkLegacyDisposition.MIBANK_FAILED,
                    selenium_attempts=selenium_attempts,
                )
        else:
            logger.warning(
                f"⏰ MIBANK - {BANK_NAME} - 크롤링 건너뜀 (자정/주말 + Selenium 실패)",
                extra={
                    "reason": "is_mibank_rate_reliable & selenium failed",
                    "action": "DB 마지막 환율 데이터 유지 (클라이언트가 재사용)"
                }
            )
            # 아무것도 하지 않음 → DB에 INSERT 없음 → 클라이언트가 마지막 IBK 환율 표시
            return IbkLegacyResult(
                IbkLegacyDisposition.MIBANK_SKIPPED,
                selenium_attempts=selenium_attempts,
            )
    finally:
        db.close()


def try_crawl_with_dated_requests(
    db: Session,
    reference_time: datetime.datetime,
) -> DatedRequestOutcome:
    """공식 IBK 페이지에서 최신 서비스 기준일을 날짜 지정 Request로 조회한다.

    IBK의 00시 이후 고시는 달력상 오늘이 아니라 직전 조회기준일 화면에 누적된다.
    08:00 전에는 전날부터, 이후에는 오늘부터 최대 ``MAX_DAYS_LOOKBACK``일을 확인한다.
    이렇게 해야 영업 중 기본 GET이 일시 실패했을 때 전일 값으로 되돌리는 일을 막는다.
    08:00~08:35에는 날짜 readback이 맞는 당일 표 부재만 개장 전 대기로 분류해 이전
    기준일을 검증하고, 그 결과에도 기존 완료시각 기반 회귀 방지를 적용한다.
    주말 조회기준일은 공식적으로 고시가 없으므로 HTTP 요청 자체를 생략하고, 공휴일처럼
    평일인데 공식 무고시 응답이면 더 이전 날짜로 계속 진행한다.

    12초는 다음 후보 요청을 시작할지 판정하는 soft budget이다. 선행 GET과
    이미 시작한 ``requests`` 호출의 hard wall-clock deadline은 아니다.
    """
    if reference_time.tzinfo is None:
        reference_time = KST.localize(reference_time)
    else:
        reference_time = reference_time.astimezone(KST)

    reference_date = reference_time.date()
    candidate_rank = 0
    saw_explicit_no_notice = False
    saw_preopen_pending = False
    deadline = time.monotonic() + IBK_DATED_REQUEST_SOFT_BUDGET_SECONDS
    # 후보 날짜 계획(rollover 시작점·주말 skip·달력 범위)은 app/ibk_candidate_policy.py 가
    # 소유한다. 새 결과 생성기가 같은 순서를 쓰게 하려고 분리했다 — 두 경로가 각자
    # 계산하면 "왜 그 날짜를 조회했나" 를 사후에 설명할 수 없다.
    for query_date in plan_candidate_dates(reference_time, max_days_back=MAX_DAYS_LOOKBACK):

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            logger.warning(
                f"⚠️ {BANK_NAME} 날짜 지정 Request soft budget 초과",
                extra={
                    "bank": BANK_NAME,
                    "budget_seconds": IBK_DATED_REQUEST_SOFT_BUDGET_SECONDS,
                    "candidate_rank": candidate_rank + 1,
                    "outcome": "soft_budget_exhausted",
                },
            )
            return DatedRequestOutcome.FALLBACK

        candidate_rank += 1
        started_at = time.perf_counter()
        try:
            result = _fetch_ibk_rates_for_date(
                query_date,
                reference_time=reference_time,
                timeout=min(DEFAULT_TIMEOUT, remaining_seconds),
            )
        except IbkRateTableAbsentError as exc:
            is_current_preopen_pending = (
                query_date == reference_date
                and IBK_SERVICE_DATE_ROLLOVER_TIME
                <= reference_time.time()
                < IBK_PREOPEN_PENDING_END_TIME
            )
            if not is_current_preopen_pending:
                logger.warning(
                    f"⚠️ {BANK_NAME} 날짜 지정 Request 응답 이상",
                    extra={
                        "bank": BANK_NAME,
                        "query_date": query_date.isoformat(),
                        "error": str(exc)[:200],
                        "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
                        "candidate_rank": candidate_rank,
                        "outcome": "response_error",
                    },
                )
                return DatedRequestOutcome.FALLBACK

            # 08:00 직후 당일 화면은 날짜만 전환되고 첫 고시 표가 아직 없을 수
            # 있다. 이 상태만 직전 서비스 기준일 조회로 이어가되, 아래의 과거값
            # 회귀 가드를 반드시 활성화한다.
            saw_preopen_pending = True
            logger.info(
                f"ℹ️ {BANK_NAME} 개장 전 당일 환율표 준비 중 — 이전 기준일 검증",
                extra={
                    "bank": BANK_NAME,
                    "query_date": query_date.isoformat(),
                    "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
                    "candidate_rank": candidate_rank,
                    "outcome": "preopen_table_pending",
                },
            )
            continue
        except Exception as exc:
            # timeout/WAF/DOM 계약 변경을 공휴일로 오인해 더 오래된 값을 저장하지 않는다.
            # 즉시 FALLBACK을 반환해 기존 Selenium 안전망으로 넘긴다.
            logger.warning(
                f"⚠️ {BANK_NAME} 날짜 지정 Request 응답 이상",
                extra={
                    "bank": BANK_NAME,
                    "query_date": query_date.isoformat(),
                    "error": str(exc)[:200],
                    "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
                    "candidate_rank": candidate_rank,
                    "outcome": "response_error",
                },
            )
            return DatedRequestOutcome.FALLBACK

        # 정확한 공식 무고시이거나, 위에서 제한적으로 인정한 당일 개장 전 표
        # 준비 상태일 때만 더 이전 서비스 기준일로 진행한다.
        if result is None:
            saw_explicit_no_notice = True
            continue

        current_rates, completed_at = result
        candidate_completion_kst = _ibk_completion_kst(query_date, completed_at)
        rates_to_store = current_rates
        preserved_pairs = []

        # 개장 전 당일 표 미생성이나 명시적 무고시 후 더 오래된 서비스 기준일로
        # lookback한 경우, DB의 더 최근 값을 과거 스냅샷으로 되돌리지 않는다.
        # 반대로 후보의 공식 완료시각이 DB 저장시각보다 뒤라면 놓친 최종 고시
        # catch-up이므로 허용한다.
        if saw_explicit_no_notice or saw_preopen_pending:
            last_info = crud.get_last_bank_rates_with_ts(
                db,
                BANK_NAME,
                MIBANK_REQUIRED_PAIRS,
            )
            regressing_pairs = find_regressing_pairs(
                current_rates,
                last_info,
                candidate_completion_kst,
                IBK_DB_SAVE_LAG_TOLERANCE_SECONDS,
            )

            if regressing_pairs:
                rates_to_store = {
                    pair: rate
                    for pair, rate in current_rates.items()
                    if pair not in regressing_pairs
                }
                preserved_pairs = regressing_pairs
                logger.warning(
                    f"⚠️ {BANK_NAME} 과거 서비스 기준일 회귀 차단",
                    extra={
                        "bank": BANK_NAME,
                        "path": "official_dated_request",
                        "query_date": query_date.isoformat(),
                        "candidate_completed_kst": candidate_completion_kst.isoformat(),
                        "candidate_rank": candidate_rank,
                        "regressing_pairs": regressing_pairs,
                        "outcome": (
                            "stale_regression_partially_preserved"
                            if rates_to_store
                            else "stale_regression_blocked"
                        ),
                    },
                )
                if not rates_to_store:
                    return DatedRequestOutcome.PRESERVED

        changed_count = crud.insert_bank_rates_into_db(
            db=db,
            current_rates=rates_to_store,
            bank_name=BANK_NAME,
        )
        logger.info(
            f"📅 {BANK_NAME} 날짜 지정 Request 조회 성공: {query_date:%Y.%m.%d}",
            extra={
                "bank": BANK_NAME,
                "path": "official_dated_request",
                "query_date": query_date.isoformat(),
                "completed_at": completed_at,
                "changed_count": changed_count,
                "observed_pairs": sorted(rates_to_store),
                "preserved_pairs": sorted(preserved_pairs),
                "candidate_rank": candidate_rank,
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 1),
                "outcome": "partial_success" if preserved_pairs else "success",
            },
        )
        return DatedRequestOutcome.OBSERVED

    last_info = crud.get_last_bank_rates_with_ts(
        db,
        BANK_NAME,
        MIBANK_REQUIRED_PAIRS,
    )
    missing_pairs = [
        pair
        for pair in MIBANK_REQUIRED_PAIRS
        if last_info.get(pair, {}).get("rate") is None
        or last_info.get(pair, {}).get("timestamp") is None
    ]
    has_complete_snapshot = not missing_pairs
    if not has_complete_snapshot:
        bootstrap_outcome = (
            "preopen_pending_bootstrap_fallback"
            if saw_preopen_pending
            else "official_no_notice_bootstrap_fallback"
        )
        bootstrap_message = (
            f"⚠️ {BANK_NAME} 개장 전 당일 환율표 준비 중이지만 DB bootstrap 필요"
            if saw_preopen_pending
            else f"⚠️ {BANK_NAME} 공식 무고시이지만 DB bootstrap 필요"
        )
        logger.warning(
            bootstrap_message,
            extra={
                "bank": BANK_NAME,
                "reference_date": reference_date.isoformat(),
                "max_days_lookback": MAX_DAYS_LOOKBACK,
                "candidate_count": candidate_rank,
                "missing_pairs": missing_pairs,
                "outcome": bootstrap_outcome,
            },
        )
        return DatedRequestOutcome.FALLBACK

    preserve_outcome = (
        "preopen_pending_preserved"
        if saw_preopen_pending
        else "official_no_notice_preserved"
    )
    preserve_message = (
        f"ℹ️ {BANK_NAME} 개장 전 당일 환율표 준비 중 — 기존값 유지"
        if saw_preopen_pending
        else f"ℹ️ {BANK_NAME} 날짜 지정 Request 공식 무고시 — 기존값 유지"
    )
    logger.info(
        preserve_message,
        extra={
            "bank": BANK_NAME,
            "reference_date": reference_date.isoformat(),
            "max_days_lookback": MAX_DAYS_LOOKBACK,
            "candidate_count": candidate_rank,
            "outcome": preserve_outcome,
        },
    )
    # 요청한 후보가 개장 전 미생성/공식 무고시이고 DB에 완전한 정상값이 있을
    # 때만 Selenium으로 같은 날짜를 다시 훑지 않고 현재값을 유지한다.
    return DatedRequestOutcome.PRESERVED


def _fetch_ibk_rates_for_date(
    query_date: datetime.date,
    *,
    reference_time: datetime.datetime,
    timeout: float = DEFAULT_TIMEOUT,
):
    """IBK 공식 날짜 지정 POST 응답을 검증해 ``(rates, 완료시각)``을 반환한다.

    정확한 요청 날짜의 공식 무고시 응답(``ECBKFEX01589``)만 ``None``으로 표현한다.
    날짜 readback은 맞지만 표와 공식 코드가 모두 없는 상태는 전용 예외로 분리한다.
    네트워크 오류, 날짜 불일치, 그 밖의 DOM/값 계약 위반은 일반 예외로 올려 호출자가
    Selenium으로 전환하게 한다.
    """
    request_data = {
        **IBK_DATE_REQUEST_DEFAULTS,
        'inDate': query_date.strftime('%Y.%m.%d'),
    }
    response = requests.post(
        IBK_BANK_URL,
        data=request_data,
        headers=HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()
    return _parse_ibk_official_response(
        response,
        query_date=query_date,
        reference_time=reference_time,
    )


OFFICIAL_RATE_TABLE_CAPTION = '일반고시환율 표'


def _find_official_rate_table(soup):
    """공식 고시 표를 caption 으로 특정한다.

    ⛔ parser 와 관측이 **같은 표**를 봐야 대조가 성립한다. 문서 전체를 훑으면 페이지의
       다른 표(안내·환전 수수료 등)가 섞여 들어와, 차이가 데이터 문제인지 범위 문제인지
       사후에 가릴 수 없다(상호 검토에서 별도 표 주입으로 실증).
    """
    for table in soup.find_all('table'):
        caption = table.find('caption')
        if caption and caption.get_text(' ', strip=True) == OFFICIAL_RATE_TABLE_CAPTION:
            return table
    return None


def _row_cells(row):
    """행의 데이터 칸들. `th` 와 `td` 를 구분하지 않는다."""
    return row.find_all(['th', 'td'])


def _cell_currency_code(cells) -> str:
    """첫 칸에서 통화 코드를 읽는다. **이 규칙의 단일 정의 위치**다.

    ⛔ `th` 만 보면 코드 칸이 `td` 인 표에서 통화를 하나도 못 본다 — parser 는 통과시키는데
       관측만 빈 목록이 되어 "표가 비었다" 로 오독된다(상호 검토에서 실증).
    """
    return cells[0].get_text(' ', strip=True).upper() if cells else ''


def _row_currency_code(row) -> str:
    """행에서 통화 코드를 읽는다(칸 분해까지 한 번에)."""
    return _cell_currency_code(_row_cells(row))


def _parse_ibk_official_response(
    response,
    *,
    query_date: datetime.date,
    reference_time: datetime.datetime,
):
    """공식 조회 응답의 날짜·3통화·완료시각 계약을 검증한다."""
    if reference_time.tzinfo is None:
        reference_time = KST.localize(reference_time)
    else:
        reference_time = reference_time.astimezone(KST)

    soup = BeautifulSoup(response.text, 'html.parser')

    expected_date = query_date.strftime('%Y.%m.%d')
    date_input = soup.select_one(INPUT_SELECTOR)
    actual_date = date_input.get('value', '').strip() if date_input else ''
    if actual_date != expected_date:
        raise ValueError(
            f"IBK 요청 날짜 readback 불일치: expected={expected_date}, actual={actual_date or 'none'}"
        )

    rate_table = _find_official_rate_table(soup)

    if rate_table is None:
        if 'ECBKFEX01589' in response.text:
            return None
        # ⚠️ 이 문구는 실제 IBK 응답 캡처로 확정된 **계약이 아니다**. 앞선 커밋의 테스트
        #    fixture 가 "깨진 응답"을 표현하려고 쓴 문자열이 여기로 승격된 **방어적
        #    heuristic** 이다. 실제 오류 화면 문구가 다르면 이 분기는 발화하지 않고 아래
        #    표 부재 상태로 분류된다. 그 상태를 개장 전 준비로 허용하는 범위는 제한 창으로
        #    한정된다.
        #    실제 오류 응답 본문을 확보하면 이 판별을 그 근거로 갱신할 것.
        page_text = soup.get_text(' ', strip=True)
        if '일시적인 오류가 발생했습니다' in page_text:
            raise ValueError("IBK 공식 응답 오류 페이지")
        raise IbkRateTableAbsentError("IBK 일반고시환율 표 누락")

    header = rate_table.find('tr')
    if header is None:
        raise ValueError("IBK 일반고시환율 표 헤더 행 누락")
    header_names = [cell.get_text(' ', strip=True) for cell in header.find_all(['th', 'td'])]
    try:
        rate_column = header_names.index('매매기준율')
    except ValueError as exc:
        raise ValueError("IBK 매매기준율 헤더 누락") from exc

    code_to_pair = IBK_CODE_TO_PAIR
    current_rates = {}
    for row in rate_table.find_all('tr')[1:]:
        cells = _row_cells(row)
        if not cells:
            continue
        code = _cell_currency_code(cells)
        pair = code_to_pair.get(code)
        if pair is None:
            continue
        if pair in current_rates:
            raise ValueError(f"IBK 통화 중복: {code}")
        if rate_column >= len(cells):
            raise ValueError(f"IBK 매매기준율 셀 누락: {code}")
        rate_text = cells[rate_column].get_text(' ', strip=True)
        if not rate_text or rate_text == '-':
            continue
        rate = parse_rate_text(rate_text)
        if not math.isfinite(rate):
            raise ValueError(f"IBK 유한하지 않은 환율: {code}={rate}")
        current_rates[pair] = rate

    expected_pairs = set(code_to_pair.values())
    if set(current_rates) != expected_pairs:
        missing = sorted(expected_pairs - set(current_rates))
        raise ValueError(f"IBK 날짜 지정 Request 필수 통화 누락: {missing}")
    validate_rate_ranges(current_rates, MIBANK_RATE_RANGES)

    standard = soup.select_one('p.standard')
    standard_text = standard.get_text(' ', strip=True) if standard else ''
    completed_match = re.search(r'고시완료\s*시각\s*[:：]\s*(\d{2}:\d{2}:\d{2})', standard_text)
    if completed_match is None:
        raise ValueError("IBK 고시완료 시각 누락")

    completed_at = completed_match.group(1)
    try:
        completed_time = datetime.time.fromisoformat(completed_at)
    except ValueError as exc:
        raise ValueError(f"IBK 고시완료 시각 형식 오류: {completed_at}") from exc

    completion_kst = _ibk_completion_kst(query_date, completed_at)
    if completion_kst > reference_time + datetime.timedelta(
        seconds=IBK_COMPLETION_CLOCK_SKEW_SECONDS
    ):
        raise ValueError(
            "IBK 고시완료 시각이 조회 시각보다 미래: "
            f"completed={completion_kst.isoformat()}, reference={reference_time.isoformat()}"
        )

    return current_rates, completed_at


def _ibk_completion_kst(
    query_date: datetime.date,
    completed_at: str,
) -> datetime.datetime:
    """조회기준일과 화면 완료시각을 실제 KST datetime으로 조합한다."""
    completed_time = datetime.time.fromisoformat(completed_at)
    completion_date = query_date
    # 조회기준일 D의 00:00~07:59 고시는 D+1의 야간 꼬리다. 공식 상세
    # 화면에서 주간 1회차가 08:26대에도 관측되어 08:30을 쓰지 않는다.
    if completed_time < IBK_SERVICE_DATE_ROLLOVER_TIME:
        completion_date += datetime.timedelta(days=1)
    return KST.localize(datetime.datetime.combine(completion_date, completed_time))


def try_crawl_with_requests(
    db: Session,
    reference_time: datetime.datetime,
) -> bool:
    """조회 당일을 Requests로 크롤링한다.

    날짜 지정 조회와 같은 엄격 파서(날짜 readback, 3통화 완전성/범위,
    고시완료시각)를 사용한다.
    """
    try:
        logger.info("IBK_BANK_URL 시도", extra={"bank": BANK_NAME})
        response = requests.get(IBK_BANK_URL, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        if reference_time.tzinfo is None:
            reference_time = KST.localize(reference_time)
        else:
            reference_time = reference_time.astimezone(KST)
        result = _parse_ibk_official_response(
            response,
            query_date=reference_time.date(),
            reference_time=reference_time,
        )
        if result is None:
            logger.debug("IBK 조회 당일 공식 무고시")
            return False

        current_rates, _ = result
        crud.insert_bank_rates_into_db(
            db=db,
            current_rates=current_rates,
            bank_name=BANK_NAME,
        )
        return True

    except Exception as e:
        logger.debug(f"IBK Requests 크롤링 실패: {str(e)}")
        return False


#: 관측용 `page_source` 읽기에서 **내가 기다리는 시간**의 상한(초).
#: ⛔ 이 값은 명령을 **취소하지 않는다.** `join()` 대기를 끝낼 뿐이고, 멈춘 명령은 세션에
#:    그대로 남아 이어지는 `driver.quit()` 을 막을 수 있다. 즉 "멈춘 읽기를 잘라 준다" 가
#:    아니라 "멈춘 읽기에 무한정 매달리지 않는다" 까지가 이 값이 하는 일이다.
SHADOW_PAGE_SOURCE_TIMEOUT = 3.0

#: 부모 수집 제한(IBK 45초) 중 이만큼을 **넘겨** 썼으면(`spent > 값`) 관측을 건너뛴다.
#: 정확히 이 값만큼 썼을 때는 관측한다 — 경계는 운영 의미가 없어 엄격 부등호로 고정한다.
#: ⛔ 저장은 관측 앞에서 이미 commit 되지만, 멈춘 읽기가 `driver.quit()` 을 막아 부모 kill 을
#:    부르면 그 회차가 실패로 **집계**된다. 시간이 빠듯할 때는 관측을 포기하는 쪽이 맞다.
#: ⛔ 기준 시각은 **모듈 import 가 아니라 그 회차의 시작**이다. import 시각으로 재면 오래 산
#:    프로세스에서 항상 건너뛴다 — 전체 스위트에서 실측으로 드러났다(격리 실행은 통과해
#:    거짓 초록이었다). 기준을 모르면(`run_started_at=None`) 가드를 적용하지 않는다.
SHADOW_BUDGET_GUARD_SECONDS = 30.0


def _read_page_source_bounded(driver, *, timeout=None):
    """`page_source` 를 시간 제한 안에서 읽는다. `(html, 실패사유)` 를 돌려준다.

    ⛔ **hang 은 예외가 아니라서 try/except 로 못 막는다.** 이 읽기는 legacy 경로가 하지 않는
       새 WebDriver 왕복(DOM 전체 직렬화)이므로, 제한 없이 두면 관측이 수집 제한시간을 먹고
       부모가 자식을 죽여 legacy 라면 저장됐을 값이 사라진다(상호 검토에서 실증).

    ⛔ 못 읽은 이유를 **시간 초과와 실패로 나눠** 남긴다. 합치면 "관측이 느리다" 와
       "driver 가 죽었다" 를 사후에 구분할 수 없다.

    ⛔ 상한을 넘긴 읽기 스레드는 daemon 으로 남는다 — 취소할 방법이 없다. 프로세스는
       수집 1회짜리 subprocess 라 곧 끝나고, 그 스레드의 어떤 결과도 쓰지 않는다.
    """
    # ⛔ 기본 인자로 상수를 묶지 않는다 — 정의 시점에 값이 고정돼 모듈 상수를 바꿔도
    #    반영되지 않고, 그 사실을 모른 채 "상한을 줄여 확인했다" 고 착각하게 된다(실측).
    limit = SHADOW_PAGE_SOURCE_TIMEOUT if timeout is None else timeout
    outcome = {}

    def _read():
        try:
            outcome["html"] = driver.page_source
        except BaseException:  # noqa: BLE001 — 별도 스레드다. 무엇이 나오든 여기서 끝낸다.
            outcome["failed"] = True

    worker = threading.Thread(target=_read, daemon=True)
    worker.start()
    worker.join(limit)
    if "html" in outcome:
        return outcome["html"], None
    if outcome.get("failed"):
        return None, "page_source_failed"
    return None, "page_source_timeout"


def _emit_selenium_shadow_observation(driver, *, query_date, legacy_rates, run_started_at=None):
    """관측하고 기록한다. **어떤 실패도 호출자에게 전달하지 않는다.**

    ⛔ 관측기 내부 방어만으로는 부족하다 — 비교 함수, 예외의 `__str__`, 로그 직렬화,
       sink 전달 어디서든 터질 수 있고, 하나라도 새면 legacy 저장이 MIBANK 로 바뀐다(실측).
       그래서 경계에서 한 번 더 삼킨다.
    """
    try:
        # ⛔ 건너뛸 때는 **반드시 남긴다.** 조용히 꺼지면 "켜 뒀다" 고 믿은 채 표본이 비고,
        #    그 공백을 "관측 대상이 없었다" 로 오독하게 된다.
        spent = None if run_started_at is None else time.monotonic() - run_started_at
        if spent is not None and spent > SHADOW_BUDGET_GUARD_SECONDS:
            logger.info(
                "IBK_SELENIUM_SHADOW_OBSERVATION_SKIPPED",
                extra={"bank": BANK_NAME, "ibk_selenium_skip_reason": "budget_guard",
                       "ibk_selenium_spent_seconds": round(spent, 1)},
            )
            return
        observation = _observe_selenium_capture(
            driver,
            query_date=query_date,
            reference_time=datetime.datetime.now(KST),
            legacy_rates=dict(legacy_rates) if legacy_rates is not None else None,
        )
        logger.info(
            "IBK_SELENIUM_SHADOW_OBSERVATION",
            extra={"bank": BANK_NAME, **observation.as_log_extra()},
        )
    except Exception:  # noqa: BLE001 — 관측이 수집을 바꾸지 않는다는 것이 이 슬라이스의 계약이다
        try:
            logger.warning("IBK_SELENIUM_SHADOW_OBSERVATION_FAILED", extra={"bank": BANK_NAME})
        except Exception:
            pass


def _observe_selenium_capture(driver, *, query_date, reference_time, legacy_rates):
    """같은 캡처로 공식 parser 를 돌려 **관측만** 한다. 어떤 예외도 밖으로 내지 않는다.

    ⛔ 이 함수가 실패해도 legacy 수집이 영향받으면 안 된다 — shadow 의 존재 이유가 사라진다.
       그래서 모든 경로를 삼키고 관측을 "unavailable" 로 남긴다. 그 실패를 정상 판정으로
       세지 않기 위해 verdict 를 따로 둔다.

    ⛔ 브라우저 인스턴스나 HTTP 요청을 늘리지 않는다. 이미 열린 driver 의 현재 DOM 만 쓴다.
    """
    captured_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    observation = IbkSeleniumObservation(
        requested_date=query_date.strftime('%Y.%m.%d') if query_date else None,
        captured_at=captured_at,
        parser_verdict="unavailable",
    )
    html, read_failure = _read_page_source_bounded(driver)
    if read_failure is not None:
        return dataclasses.replace(observation, notes=(read_failure,))

    # 필드별로 독립 수집한다 — 하나가 실패해도 나머지 사실이 사라지지 않게.
    input_value = completed_at = None
    pairs: tuple[str, ...] = ()
    soup = None
    try:
        soup = BeautifulSoup(html, 'html.parser')
    except Exception:
        pass
    if soup is not None:
        try:
            node = soup.select_one(INPUT_SELECTOR)
            input_value = node.get('value', '').strip() if node else None
        except Exception:
            pass
        try:
            marker = soup.select_one('p.standard')
            completed_at = marker.get_text(strip=True) if marker else None
        except Exception:
            pass
        try:
            # ⛔ 통화 목록은 parser 전체 성공과 **독립**으로 모은다. 완료시각 형식 하나가
            #    틀렸다고 "표에 어떤 통화가 있었나" 를 못 보면 원인을 못 가린다(실측).
            #    행 번호가 아니라 **통화 코드**로 식별한다 — 순서가 바뀌어도 정직하다.
            # ⛔ 다만 **표 특정과 코드 칸 판독은 parser 와 같은 규칙**을 쓴다. 독립이어야 하는
            #    것은 "parser 가 최종 통과했는가" 이지 "어디를 보는가" 가 아니다.
            rate_table = _find_official_rate_table(soup)
            if rate_table is not None:
                pairs = tuple(sorted({
                    IBK_CODE_TO_PAIR[code]
                    for row in rate_table.find_all('tr')[1:]
                    for code in [_row_currency_code(row)]
                    if code in IBK_CODE_TO_PAIR
                }))
        except Exception:
            pass

    verdict, reject_reason, validated = "unavailable", None, None
    try:
        parsed = _parse_ibk_official_response(
            types.SimpleNamespace(text=html),
            query_date=query_date,
            reference_time=reference_time,
        )
        if parsed is None:
            # ⛔ 정상 무고시다. 관측 실패로 세면 분모가 오염된다.
            verdict = "no_session"
        else:
            rates, completed = parsed
            verdict, validated = "accept", dict(rates)
            completed_at = completed_at or completed
    except ValueError as exc:
        verdict, reject_reason = "reject", str(exc)[:200]
    except Exception as exc:  # noqa: BLE001 — 관측이 수집을 깨뜨리지 않는다
        reject_reason = f"{type(exc).__name__}: {str(exc)[:120]}"

    comparison, diff = compare_mappings(legacy_rates, validated)
    return dataclasses.replace(
        observation,
        input_value_date=input_value,
        completed_at_text=completed_at,
        observed_pairs=pairs,
        parser_verdict=verdict,
        parser_reject_reason=reject_reason,
        mapping_comparison=comparison,
        mapping_diff_pairs=diff,
    )


def crawl_and_save_ibk_routine_selenium(
    url: str, selectors: dict, db: Session, *, run_started_at: float | None = None
) -> int:
    """IBK 전용 Selenium 크롤링 + DB 저장 루틴 (변경 개수 반환)"""
    current_rates = {}
    try:
        with selenium_driver_context() as driver:
            driver.get(url) # url 오류면 여기서 에러남
            wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT_SHORT)

            selected_date = datetime.date.today()

            for i in range(MAX_DAYS_LOOKBACK):
                try:
                    input_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, INPUT_SELECTOR)))
                    test_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, IBK_BANK_SELECTORS['usd-krw'])))
                    for pair, selector in IBK_BANK_SELECTORS.items():
                        try:
                            rate_element = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, selector)))
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
                    break   # 크롤링 되면 종료

                except Exception as e:
                    selected_date = selected_date - datetime.timedelta(days=1)
                    logger.info(f"📅 날짜 변경 {selected_date.strftime('%Y.%m.%d')}", extra={"selector": INPUT_SELECTOR, "bank": BANK_NAME})
                    try:
                        input_element.clear()
                        input_element.send_keys(selected_date.strftime('%Y.%m.%d'))
                        input_element.send_keys(Keys.ENTER)
                    except Exception as e:
                        logger.warning(f"⚠️ 날짜 변경 실패", extra={"selector": INPUT_SELECTOR, "bank": BANK_NAME})
                        break

            # ⛔ **추출 실패 분기에서는 관측하지 않는다.** 이 분기의 다음 단계는 Selenium
            #    재시도 3회와 MIBANK 폴백인데, 관측의 읽기 대기가 시도마다 쌓여(3초 × 3회)
            #    부모의 45초 제한 안에서 폴백 저장 기회를 통째로 없앨 수 있다(상호 검토에서
            #    실증: 부모 제한 0.4초 대역에서 legacy 는 MIBANK 저장, shadow 는 저장 0회).
            #    강제 적용 판단에 필요한 모집단은 "Selenium 은 값을 냈는데 공식 계약은 거절"
            #    쪽이므로, 이 분기를 빼도 목적한 신호는 남는다.
            if not current_rates:
                raise Exception(f"환율 데이터 추출 실패 (셀렉터 오류 또는 데이터 없음)")

            # db 저장
            written = crud.insert_bank_rates_into_db(db=db, current_rates=current_rates, bank_name=BANK_NAME)

            # 검증 관측(shadow)은 저장이 **commit 된 뒤**에만 돈다.
            # ⛔ 관측의 `page_source` 는 legacy 가 하지 않는 새 WebDriver 왕복이라, 멈추면
            #    예외가 아니어서 경계 wrapper 가 못 막는다. 저장 앞에 두면 부모의 45초 제한이
            #    자식을 죽여 legacy 라면 저장됐을 값이 사라진다(상호 검토에서 실증).
            #    `insert_bank_rates_into_db` 가 내부에서 `db.commit()` 하므로 이 지점에서
            #    저장은 이미 확정이다.
            # ⛔ **남는 위험은 집계에서 끝나지 않는다.** 멈춘 읽기가 `driver.quit()` 을 막으면
            #    (ChromeDriver 는 세션마다 스레드 하나로 그 세션 명령을 큐 순서대로 처리한다)
            #    부모가 자식을 kill 하고 `execute_with_timeout` 이 False 를 돌려주며, worker 는
            #    `should_retry = not success` 로 **IBK 회차를 통째로 한 번 더 실행**한다
            #    (추가 HTTP·Selenium 시도와 저장 호출, Selenium 큐 점유). 재시도 작업은 다시
            #    재시도되지 않으므로 **원래 큐 항목당** 여분 실행이 최대 1회다(관측 기간
            #    전체의 상한이 아니다 — 매 회차가 각자 이 상한을 가진다).
            #    이미 commit 된 값은 남는다.
            #    ⛔ 예산 가드(`SHADOW_BUDGET_GUARD_SECONDS`)는 **느린 읽기**에만 듣는다.
            #       명령이 아예 멈추면 남은 예산과 무관하게 위 경로가 발생할 수 있다.
            # ⛔ 관측 **과 로그 전달까지** 통째로 격리한다. 로그 sink 하나가 실패하는 것만으로
            #    수집이 재시도·MIBANK 로 흘러가면 shadow 의 존재 이유가 사라진다(실측).
            #    legacy 모드에서는 호출조차 하지 않는다.
            if ibk_selenium_config.VALIDATION_MODE == ibk_selenium_config.MODE_SHADOW:
                _emit_selenium_shadow_observation(
                    driver, query_date=selected_date, legacy_rates=current_rates,
                    run_started_at=run_started_at,
                )
            return written

    except Exception as e:
        error_msg = str(e)
        if "환율 데이터 추출 실패" in error_msg:
            logger.error(f"🈚️ {BANK_NAME}은행 환율 데이터 없음 from CRAWLER Exception",
                extra={"url": url, "bank": BANK_NAME, "selectors": list(selectors.keys()), "error": error_msg})
        else:
            logger.exception("⚠️ URL 접속 또는 처리 오류",
                extra={"url": url, "bank": BANK_NAME, "error": error_msg})
        raise


# ─────────────────────────────────────────────────────────────
# [DEPRECATED] 아래 함수는 현재 사용되지 않음
# - IBK는 try_crawl_with_requests() 함수로 대체됨 (데이터 유효성 검사 강화)
# - 자정/주말 환율 없음 처리 로직이 추가된 새 함수 사용
# - 추후 삭제 예정
# ─────────────────────────────────────────────────────────────
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


# ═════════════════════════════════════════════════════════════
# 미연결 결과 생성기 (운영 진입점·runner 바인딩에 연결하지 않음)
# ═════════════════════════════════════════════════════════════
def _intended_from_snapshot(before, regressing, current_rates):
    """제출할 값과 유지할 값을 구성한다. 유지 대상은 사용 가능한 값이어야 한다.

    회귀로 제외한 통화의 기존 값이 사용 가능하지 않으면(비유한·범위 밖·시각 없음)
    유지하겠다고 선언할 수 없다. 그 경우 None을 돌려 호출자가 결과로 종결하게 한다.
    제출 대상은 이 검사와 무관하므로 빈·부분 DB의 정상 보충은 막지 않는다.
    """
    if not regressing:
        return IbkIntendedState(submitted=dict(current_rates))
    usable = before.rates or {}
    if any(pair not in usable for pair in regressing):
        return None
    submitted = {
        pair: rate for pair, rate in current_rates.items() if pair not in regressing
    }
    return IbkIntendedState(
        submitted=submitted,
        retained={pair: usable[pair] for pair in regressing},
        retention={pair: IbkRetentionReason.REGRESSION_GUARD for pair in regressing},
    )


def _is_preopen_pending_window(service_date, reference_time) -> bool:
    """표가 없는 것이 정상인 짧은 창인가 — 조회 당일 08:00~08:34:59.

    legacy 가 try_crawl_with_dated_requests 에서 쓰는 것과 같은 판정이다(:360-366).
    ⛔ 과거 기준일 후보에는 적용하지 않는다. 그 날짜의 표가 없는 것은 개장 전이 아니라
       계약 이상이다 — service_date 가 조회 시점의 기준일과 같을 때만 창 안으로 본다.
    """
    local = reference_time.astimezone(KST)
    return (
        service_date == local.date() - datetime.timedelta(
            days=int(local.time() < IBK_SERVICE_DATE_ROLLOVER_TIME)
        )
        and IBK_SERVICE_DATE_ROLLOVER_TIME <= local.time() < IBK_PREOPEN_PENDING_END_TIME
    )


#: 안전망이 **관측 자체를 시작하지 못한** 사유. 이 경우는 HTTP 진단을 덮지 않는다.
#: ⛔ 드라이버를 못 얻거나 기한이 지난 것은 우리 쪽 사정이지 은행 응답에 대한 관측이 아니다.
#:    HTTP 는 실제로 무언가를 보고 진단을 냈으므로(예: 과거 날짜의 표 부재) 그것이 남아야 한다.
#: ⛔ 반면 제출 차단·확인 실패·요소 부재는 안전망이 **돌면서** 관측에 실패한 것이므로
#:    그 사유가 남아야 한다 — HTTP 사유로 덮으면 원인이 사라진다.
IBK_SELENIUM_NEVER_OBSERVED = ("driver_failed", "deadline_passed")

#: Selenium 관측 불가 사유 → 결과 사유. 합의한 분류다.
#: 조작 콜백이 예외로 끝난 사유는 `<라벨>:<예외 종류>` 꼴이다. `_guarded` 가 알려주는 것은
#: **어느 콜백에서 났는지**뿐이고, 브라우저 통신까지 갔는지는 증명하지 않는다(실측: 어댑터의
#: `clock() + timeout` 에 `None` 을 주면 드라이버 호출 0회로 `confirm_failed:TypeError` 가
#: 된다). 그래서 조작 예외는 라벨과 무관하게 **출처 미확정**으로 본다 — 라벨마다 다르게
#: 접으면 같은 예외가 발생 위치만으로 다른 분류가 된다.
#:
#: 요소를 **찾는** 조작만 예외다 — 거기서 난 `NoSuchElementException` 은 "문서에 그 요소가
#: 없다" 는 관측이므로 계약 이상이다. 같은 자리의 다른 예외는 역시 출처를 모른다.
IBK_SELENIUM_LOOKUP_LABELS = ("document_root:", "find_input:", "served_date_unreadable:")
IBK_SELENIUM_OPERATION_LABELS = ("submit_failed:", "alert_check:", "confirm_failed:",
                                 "page_source:")
IBK_SELENIUM_ELEMENT_ABSENT = "NoSuchElementException"

#: **확정된 관측** 사유 → 결과 사유. 접두사가 아니라 **정확히 일치**할 때만 쓴다 — 넓은
#: 접두사는 새 사유를 조용히 삼킨다(`submit` 이 `submit_failed:TypeError` 를 삼켜 출처 미확정
#: 예외를 통신 오류로 만들던 것이 그 예다).
IBK_SELENIUM_CONFIRMED_REASONS = {
    # 문서에 요소가 없다고 관측했다.
    "document_root_absent": IbkReason.CONTRACT_ERROR,
    "input_absent": IbkReason.CONTRACT_ERROR,
    # 브라우저와 주고받은 결과를 실제로 보고 판정했다.
    "submit_blocked_by_alert": IbkReason.TRANSPORT_ERROR,
    "submit_not_confirmed": IbkReason.TRANSPORT_ERROR,
    "alert_present": IbkReason.TRANSPORT_ERROR,
    # 우리 쪽 시계·입력이 못 쓸 값이라고 캡처러가 이름 붙였다.
    "clock_unusable": IbkReason.UNATTRIBUTED_ERROR,
    "clock_went_backwards": IbkReason.UNATTRIBUTED_ERROR,
    "page_loaded_at_unusable": IbkReason.UNATTRIBUTED_ERROR,
    "document_ready_at_unusable": IbkReason.UNATTRIBUTED_ERROR,
    # 읽기 실패는 종류가 버려지고, 시간 초과는 **우리가 정한 상한** 안에 결과가 없었다는
    # 사실만 증명한다. 어느 쪽도 원인을 확정하지 못한다.
    "page_source_failed": IbkReason.UNATTRIBUTED_ERROR,
    "page_source_timeout": IbkReason.UNATTRIBUTED_ERROR,
}

#: 뒤에 단계 이름이 붙어 정확히 일치할 수 없는 것들.
IBK_SELENIUM_PREFIXED_REASONS = (
    ("deadline_passed", IbkReason.BUDGET_EXHAUSTED),   # deadline_passed:before_read 등
    ("page_too_old", IbkReason.BUDGET_EXHAUSTED),      # page_too_old_after_read
    # 안전망 바깥에서 잡은 것. produce 는 이 사유에서 HTTP 진단을 유지하므로 이 분류에
    # 실제로 닿지 않는다(`IBK_SELENIUM_NEVER_OBSERVED`).
    ("driver_failed", IbkReason.TRANSPORT_ERROR),
)


def _selenium_unavailable_reason(detail):
    """세부 사유를 결과 사유로 접는다. **확정하지 못한 것은 확정하지 않는다.**

    ⛔ 기본값이 CONTRACT_ERROR 이면 안 된다. 그건 "읽을 수 있는 문서의 요소·계약이 어긋났다"
       는 주장이라, 우리 시계가 NaN 이거나 읽기 스레드가 종류도 없이 죽은 회차까지 문서
       탓으로 적게 된다.
    """
    text = str(detail or "")
    for label in IBK_SELENIUM_LOOKUP_LABELS:
        if text.startswith(label):
            # 요소를 못 찾은 것만 문서 탓이다. 같은 자리의 다른 예외는 출처를 모른다.
            return (IbkReason.CONTRACT_ERROR
                    if text[len(label):] == IBK_SELENIUM_ELEMENT_ABSENT
                    else IbkReason.UNATTRIBUTED_ERROR)
    if text.startswith(IBK_SELENIUM_OPERATION_LABELS):
        # 어느 콜백에서 났는지만 알 뿐 브라우저까지 갔는지는 모른다.
        return IbkReason.UNATTRIBUTED_ERROR
    confirmed = IBK_SELENIUM_CONFIRMED_REASONS.get(text)
    if confirmed is not None:
        return confirmed
    for prefix, reason in IBK_SELENIUM_PREFIXED_REASONS:
        if text.startswith(prefix):
            return reason
    return IbkReason.UNATTRIBUTED_ERROR


def _historical_preservation_reason(search):
    """과거 후보의 보존 사유. POST·Selenium 경로가 같은 규칙을 쓴다."""
    if search.saw_preopen_pending:
        return IbkReason.PREOPEN_PENDING
    return IbkReason.OFFICIAL_NO_SESSION


class IbkSeleniumWiringError(RuntimeError):
    """안전망의 **배선** 이 틀렸다. 관측 실패가 아니다.

    ⛔ 관측 실패로 접지 않는다. 접으면 `driver_failed:<종류>` 가 되고 그 사유는 HTTP 진단을
       유지시켜 원인이 사라진다(실측). 이건 우리 쪽 오류이므로 결과를 만들지 않고 올린다 —
       runner 의 기존 기술 오류 계약(exit 1)을 그대로 쓴다.
    ⛔ 부모의 1회 재시도는 같은 구성으로 한 번 더 실행한다. 안전망은 공식 HTTP 후보 검색
       **뒤에** 열리므로 **공식 HTTP 요청은 반복될 수 있다**. 재시도가 안전망에 닿으면 같은
       배선 오류가 드라이버 생성·항해 전에 실패하므로 **Selenium 조회는 시작하지 않는다**.
    """


def _selenium_safety_net(context, query_date, *, deadline,
                         driver_context=None, capturer_factory=None):
    """공식 HTTP 가 **기술적으로** 실패했을 때만 여는 안전망. 저장하지 않는다.

    돌려주는 것은 관측(`IbkSeleniumStrictCapture`)뿐이다. 저장 여부·등급은 호출자가 정하고,
    저장 경로는 POST 성공과 **완전히 같은 것**을 쓴다 — 저장 규칙을 두 벌 만들지 않는다.

    ⛔ **이번 실행에서 새로 항해한 문서만** 본다. 캡처러는 제출이 필요 없을 때 문서 나이로만
       신선도를 보는데, 그 나이의 기준이 되는 `page_loaded_at` 을 여기서 만든다. 기준은
       **항해 시작 시각**이다 — `get()` 반환 시각을 쓰면 로딩에 걸린 시간만큼 문서 나이를
       실제보다 작게 계산한다.
    ⛔ 한 세션에 **한 번만** 관측한다. 거부·무고시·불가 어느 경우에도 재항해하지 않는다.
    ⛔ 예산이 모자라면 **드라이버를 만들지 않는다**. 반환 None 은 "시도하지 않았다" 는 뜻이다.
    ⛔ 시계는 **호출 시점에** 읽는다. `monotonic=time.monotonic` 처럼 기본 인자로 묶으면
       정의 시점의 함수가 박혀 패치가 듣지 않는다(이 리포에서 두 번째 재발).
    """
    # ⛔ 배선 검사는 **여기**, `opener()` 앞·아래 `try` 밖이어야 한다. 캡처러 생성은
    #    `driver.get()` **뒤**라서 생성자 검사만으로는 이미 Chrome 을 띄우고 은행 페이지를
    #    조회한 뒤가 되고, 그 예외는 바깥 `except Exception` 이 `driver_failed:` 로 접어
    #    HTTP 진단을 유지시킨다 — 배선 오류가 상류 통신 장애로 기록된다(실측).
    # ⛔ 예산 검사보다 **먼저** 본다. 뒤에 두면 예산이 모자란 회차에서 배선 오류가
    #    조용히 숨는다 — 그 회차는 `None`(시도하지 않았다)로 끝난다.
    # ⛔ 검사 대상은 **운영이 실제로 주입하는** 읽기 함수다. `capturer_factory` 주입은
    #    시험용 이음매이고, 이 검사는 모듈 자신의 배선을 본다.
    contract = read_page_source_contract_error(_read_page_source_bounded)
    if contract is not None:
        raise IbkSeleniumWiringError(f"read_page_source:{contract}")

    remaining = deadline - time.monotonic()
    if remaining < IBK_SELENIUM_ENTRY_BUDGET_SECONDS:
        logger.info("IBK_SELENIUM_NET_SKIPPED", extra={
            "bank": BANK_NAME, "reason": "budget", "remaining_seconds": round(remaining, 2)})
        return None

    opener = driver_context or selenium_driver_context
    build = capturer_factory or _build_selenium_capturer

    def _expired(stage):
        """단계 사이마다 기한을 다시 본다. 만료 뒤 **다음 작업을 시작하지 않는다**.

        ⛔ 진입 시 한 번만 보면 드라이버 생성이 예산을 다 먹은 뒤에도 항해와 관측을 시작한다
           (실측: 잔여 12초에서 생성에 20초를 쓰고도 get·capture 를 각각 1회 실행).
        ⛔ 이것과 "이미 시작한 호출이 기한 안에 끝난다" 는 별개다. 후자는 보장하지 않는다.
        """
        left = deadline - time.monotonic()
        if left > 0:
            return None
        logger.info("IBK_SELENIUM_NET_EXPIRED", extra={
            "bank": BANK_NAME, "stage": stage, "overrun_seconds": round(-left, 2)})
        return IbkSeleniumStrictCapture(
            SELENIUM_UNAVAILABLE, reason=f"deadline_passed:{stage}")

    try:
        with opener() as driver:
            expired = _expired("after_driver")
            if expired is not None:
                return expired
            # 남은 예산 안에서만 문서를 기다린다. 기존 42초는 그 자체로 부모 45초를 넘지
            # 않지만, 앞선 HTTP 와 드라이버 생성 시간을 합치면 넘길 수 있다.
            _limit_driver_to_deadline(driver, deadline)
            expired = _expired("after_limits")   # 제한 설정도 시간을 먹는다(실측)
            if expired is not None:
                return expired
            navigated_at = time.monotonic()     # 항해 **시작** 시각이 문서 나이의 기준이다
            driver.get(IBK_BANK_URL)
            document_ready_at = time.monotonic()   # 연속 제출 가드의 기준
            expired = _expired("after_navigation")
            if expired is not None:
                return expired
            capturer = build(driver)
            return capturer.capture(
                driver, query_date=query_date, reference_time=context.reference_time,
                page_loaded_at=navigated_at, document_ready_at=document_ready_at,
                deadline=deadline,
            )
    except Exception as exc:  # noqa: BLE001 — 안전망 실패가 결과 구성을 깨뜨리지 않는다
        logger.warning("IBK_SELENIUM_NET_UNAVAILABLE", extra={
            "bank": BANK_NAME, "error_type": type(exc).__name__})
        # ⛔ None 은 "시도하지 않았다" 로 예약한다. 여기는 시도했고 실패한 것이므로 구분한다.
        return IbkSeleniumStrictCapture(
            SELENIUM_UNAVAILABLE, reason=f"driver_failed:{type(exc).__name__}")


def _limit_driver_to_deadline(driver, deadline):
    """남은 예산을 드라이버 명령 제한으로 내린다. 실패해도 관측을 막지 않는다."""
    left = max(deadline - time.monotonic(), 0.0)
    for setter in ("set_page_load_timeout", "set_script_timeout"):
        try:
            getattr(driver, setter)(left)
        except Exception:  # noqa: BLE001 — 제한을 못 걸어도 위 단계 검사가 남는다
            logger.debug("IBK_SELENIUM_NET_LIMIT_SKIPPED", extra={"setter": setter})


def _build_selenium_capturer(driver):
    """운영 조작을 묶어 캡처러를 만든다. 읽기는 기존 시간제한 읽기를 그대로 쓴다.

    읽기 상한은 여기서 한 번만 정해 캡처러에 넘긴다. 캡처러가 남은 예산과 비교해 작은 쪽을
    주므로, 상수를 읽기 함수와 캡처러 양쪽에 적어 두 값이 갈라지는 일을 만들지 않는다.
    """
    operations = build_selenium_operations(
        driver, input_selector=INPUT_SELECTOR,
        read_page_source=_read_page_source_bounded,
    )
    return IbkSeleniumStrictCapturer(
        parse=lambda html, *, query_date, reference_time: _parse_ibk_official_response(
            types.SimpleNamespace(text=html),
            query_date=query_date, reference_time=reference_time),
        read_timeout=SHADOW_PAGE_SOURCE_TIMEOUT,
        **operations,
    )


def produce_ibk_dated_result(db, context, *, timeout=DEFAULT_TIMEOUT, now=None) -> IbkResult:
    """공식 후보를 찾아 검증부터 최종 판정까지 연결한다. 아직 운영에 연결하지 않는다.

    범위 밖: 당일 GET 우선순위, MIBANK, 부모 경보. Selenium 은 **범위 안**이다 —
    공식 HTTP 가 기술적으로 실패했을 때만 안전망을 연다.

    후보 탐색은 app/ibk_candidate_policy.py 가 계획하고, 의미적 거부(개장 전·무고시)에서만
    더 과거로 간다. 기술적 실패에서는 그 자리에서 멈춘다 — 오래된 값을 쓰지 않기 위해서다.

    ⛔ 과거 후보를 찾아도 **당일 관측으로 승격하지 않는다**. `official` 과 보존 승인을 함께
       넘겨 PRESERVED 로 접고, 기대 서비스일은 부모 값을 그대로 둔다. 여기에 관측일을
       넣으면 부모의 validate_result 가 결과를 전량 거부한다.
    ⛔ 보존은 "기대 날짜의 고시를 못 봤다" 는 뜻이지 no-write 가 아니다. 회귀하지 않는
       통화는 계속 catch-up 저장된다.

    표 부재는 **조회한 날짜 기준**으로 가른다. 그 날짜의 개장 전 창(08:00~08:34:59) 안이면
    보존 승인(PREOPEN_PENDING), 밖이면 실패 원인이다. 기대 날짜로 고정해 판정하면 과거
    후보의 표 부재까지 개장 전으로 오인해 더 과거로 새어 나간다.
    """
    expected_date = datetime.date.fromisoformat(context.expected_service_date)
    observed_at = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    official = preservation = failure = None
    # 공식 후보가 없을 때 쓸 보존 사유. 승인 자체는 DB 사실을 확보한 뒤에 만든다.
    preservation_reason = IbkReason.OFFICIAL_NO_SESSION
    intended = None
    write = IbkWriteAttempt.none()
    # ⛔ 여기서 예산을 **새로 시작하지 않는다**. spawn·app import·설정 캐시 갱신에 쓴 시간도
    #    부모의 실행 제한을 소비했으므로, 부모가 넘긴 작업 기한과 묶는다. 12초는 "HTTP 후보
    #    검색에 쓸 몫" 이지 이 실행 전체의 예산이 아니다.
    # ⛔ `context.work_deadline` 을 직접 읽으면 과대 기한 검증을 **우회한다**(실측: 잘못된
    #    기한으로 10초짜리 요청 8회 + 저장까지 진행됐다). 검증된 잔여를 먼저 구한다.
    _started = time.monotonic()
    deadline = _started + min(IBK_DATED_REQUEST_SOFT_BUDGET_SECONDS,
                              context.remaining_work_seconds(_started))

    def _classify(exc, query_date):
        """예외를 '다음 후보로 계속'(None) 과 기술적 실패(사유) 로 가른다.

        ⛔ 개장 전 창은 **실제 조회한 날짜**로 판정한다. 기대 날짜로 고정하면 과거 후보의
           표 부재까지 개장 전으로 오인해 더 과거로 새어 나간다 — 과거 날짜의 표가 없는
           것은 개장 전이 아니라 계약 이상이다.
        """
        if isinstance(exc, IbkRateTableAbsentError):
            if _is_preopen_pending_window(query_date, context.reference_time):
                return None
            return IbkReason.AMBIGUOUS_TABLE_ABSENT
        if isinstance(exc, requests.RequestException):
            return IbkReason.TRANSPORT_ERROR
        if isinstance(exc, ValueError):
            return IbkReason.CONTRACT_ERROR
        raise exc

    def _budgeted_fetch(query_date):
        """요청 **직전에** 잔여를 다시 본다. 부족하면 호출하지 않고 예산 소진으로 접는다.

        ⛔ 시작 판정과 이 지점 사이에 선점이 끼면 기한이 이미 지났을 수 있다. 그때 하한으로
           상한을 만들어 호출하면 초과가 무한정 커진다(실측: 27.5초 지난 뒤에도 호출).
        ⛔ 호출자가 준 상한을 **늘리지 않는다** — `min` 만 쓴다.
        """
        remaining = deadline - time.monotonic()
        if remaining < IBK_MIN_REQUEST_TIMEOUT_SECONDS:
            raise CandidateBudgetExhausted()
        return _fetch_ibk_rates_for_date(
            query_date, reference_time=context.reference_time,
            # ⛔ requests 의 timeout 은 연결·읽기 무응답 상한이지 **전체 응답의 벽시계 상한이
            #    아니다.** 이 결속만으로 기한 내 반환이 보장되지는 않는다.
            timeout=min(timeout, remaining),
        )

    search = search_candidates(
        context.reference_time,
        max_days_back=MAX_DAYS_LOOKBACK,
        fetch=_budgeted_fetch,
        # 잔여가 요청 하나를 시작할 만큼도 안 되면 시작하지 않는다.
        has_budget=lambda: deadline - time.monotonic() >= IBK_MIN_REQUEST_TIMEOUT_SECONDS,
        classify=_classify,
    )

    historical = False
    selenium_source = False
    if search.stop is CandidateSearchStop.ACCEPTED:
        service_date, fetched = search.service_date, search.payload
        # ⛔ 기대 날짜보다 과거의 세션을 본 것이면 그 관측을 당일 OBSERVED 로 승격하지
        #    않는다. 어댑터는 official + preservation 을 함께 받아 PRESERVED 로 접고
        #    관측 메타데이터(날짜·완료시각)는 그대로 보존한다.
        historical = service_date < expected_date
        if historical and not (search.saw_no_session or search.saw_preopen_pending):
            # 달력상 비세션일만 건너뛰어 닿은 후보다. HTTP 무고시 응답을 받은 적이
            # 없으므로 그 사실을 지어내지 않고 근거를 달력에 둔다.
            preservation_reason = IbkReason.OFFICIAL_NO_SESSION
        elif historical and search.saw_preopen_pending:
            preservation_reason = IbkReason.PREOPEN_PENDING
    else:
        service_date, fetched = expected_date, None
        if search.stop is CandidateSearchStop.TECHNICAL_FAILURE:
            # ⛔ 안전망은 **기술적 실패에서만** 연다. 무고시·개장 전 같은 의미적 거부는
            #    정상 관측이므로 브라우저를 띄우지 않는다. 예산 소진에서도 열지 않는다 —
            #    예산이 없어 멈춘 자리에서 더 비싼 경로를 여는 것은 모순이다.
            #    (검색 정책이 예산 소진을 TECHNICAL_FAILURE 로 섞지 않도록 이미 분리했다.)
            capture = _selenium_safety_net(
                context, search.failure_date or expected_date, deadline=context.work_deadline)
            if capture is None:
                # 시도하지 않았다(예산 미달). HTTP 실패 사유를 그대로 둔다.
                failure = IbkObservationFailure(search.failure_reason)
            elif capture.usable:
                service_date = capture.service_date
                fetched = (capture.rates, capture.completed_at)
                selenium_source = True
                historical = service_date < expected_date
                if historical:
                    # ⛔ 보존 사유는 POST 경로와 **같은 규칙**으로 정한다. 여기만 무고시로
                    #    고정하면 같은 상황에서 source 만 다른데 사유가 갈린다(실측:
                    #    POST 는 PREOPEN_PENDING, Selenium 은 OFFICIAL_NO_SESSION).
                    preservation_reason = _historical_preservation_reason(search)
            elif capture.verdict == SELENIUM_NO_SESSION:
                # ⛔ 무고시는 거부가 아니다. 기존 보존 정책으로 접는다.
                preservation_reason = _historical_preservation_reason(search)
            elif capture.verdict == SELENIUM_REJECTED:
                failure = IbkObservationFailure(IbkReason.SELENIUM_STRICT_REJECTED)
            elif str(capture.reason or "").startswith(IBK_SELENIUM_NEVER_OBSERVED):
                # 안전망이 돌지 못했다. HTTP 가 낸 진단이 이 회차의 유일한 관측이다.
                failure = IbkObservationFailure(search.failure_reason)
                logger.info("IBK_SELENIUM_NET_NOT_OBSERVED", extra={
                    "bank": BANK_NAME, "detail": capture.reason,
                    "kept_reason": getattr(search.failure_reason, "value",
                                           search.failure_reason)})
            else:
                # ⛔ Selenium 이 관측하지 못한 사유를 HTTP 사유로 덮지 않는다(실측: alert 차단이
                #    CONTRACT_ERROR 로, 요소 부재가 TRANSPORT_ERROR 로 기록됐다).
                failure = IbkObservationFailure(
                    _selenium_unavailable_reason(capture.reason))
                logger.info("IBK_SELENIUM_NET_UNUSABLE", extra={
                    "bank": BANK_NAME, "verdict": capture.verdict,
                    "detail": capture.reason})
        elif search.stop is CandidateSearchStop.BUDGET_EXHAUSTED:
            failure = IbkObservationFailure(IbkReason.BUDGET_EXHAUSTED)
        elif search.saw_preopen_pending:
            # 기대 날짜가 개장 전이었다는 근거. 이후 무고시가 이 사유를 덮지 않는다.
            preservation_reason = IbkReason.PREOPEN_PENDING

    if failure is None:
        try:
            last_info = crud.get_last_bank_rates_with_ts(db, BANK_NAME, MIBANK_REQUIRED_PAIRS)
        except Exception:
            # 사전 조회가 없으면 회귀 판정을 할 수 없어 안전하게 쓰지 않는다.
            last_info = None
            failure = IbkObservationFailure(IbkReason.DB_ERROR)

    if failure is None:
        before = read_ibk_db_snapshot(
            db,
            bank_name=BANK_NAME,
            ranges=MIBANK_RATE_RANGES,
            reader=lambda *_args, **_kwargs: last_info,
        )
        if fetched is None:
            preservation = IbkPreservationGrant(preservation_reason)
            keep = dict(before.rates or {})
            intended = IbkIntendedState(
                retained=keep,
                retention={pair: IbkRetentionReason.NOT_OBSERVED for pair in keep},
                unavailable=tuple(before.missing or ()),
            )
        else:
            current_rates, completed_at = fetched
            completion = _ibk_completion_kst(service_date, completed_at)
            official = IbkOfficialObservation(
                IbkSource.OFFICIAL_SELENIUM if selenium_source else IbkSource.OFFICIAL_POST,
                service_date, dict(current_rates), completion
            )
            if historical:
                # 관측은 사실로 남기되 당일 고시로 승격하지 않는다. 저장은 계속 일어날 수
                # 있다 — 보존은 "기대 날짜의 고시를 못 봤다" 는 뜻이지 no-write 가 아니다.
                preservation = IbkPreservationGrant(preservation_reason)
            regressing = find_regressing_pairs(
                current_rates, last_info, completion, IBK_DB_SAVE_LAG_TOLERANCE_SECONDS
            )
            intended = _intended_from_snapshot(before, regressing, current_rates)
            if intended is None:
                # 유지하려는 기존 값을 쓸 수 없다. 관측을 승격하지 않고 결과로 종결한다.
                # ⛔ 역사 후보였다면 보존 승인도 함께 거둔다. 실패와 승인이 공존하면
                #    어댑터의 배타성(FAILURE_IS_EXCLUSIVE)을 깨고 결과가 예외로 끝난다.
                official = preservation = None
                failure = IbkObservationFailure(IbkReason.DB_ERROR)

        if failure is None and intended.submitted:
            mode = capture_write_mode()
            try:
                changed = crud.insert_bank_rates_into_db(
                    db=db, current_rates=dict(intended.submitted), bank_name=BANK_NAME
                )
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                write = IbkWriteAttempt(True, mode, None, failed=True)
            else:
                write = IbkWriteAttempt(True, mode, changed)

    db_after = read_ibk_db_snapshot(db, bank_name=BANK_NAME, ranges=MIBANK_RATE_RANGES)
    return build_ibk_result(
        run_id=context.run_id,
        # ⛔ 부모가 정한 기대 서비스일 그대로다. 역사 후보를 관측해도 여기에 그 날짜를
        #    넣으면 부모의 validate_result 가 SERVICE_DATE_MISMATCH 로 결과를 전량
        #    거부한다. 실제 관측일은 official 이 따로 들고 간다.
        expected_service_date=expected_date,
        observed_at=observed_at,
        db_after=db_after,
        intended=intended,
        official=official,
        preservation=preservation,
        failure=failure,
        write=write,
    )


def run_ibk_dated_result(context, *, timeout=DEFAULT_TIMEOUT, now=None) -> IbkResult:
    """`crawler(context)` 1-인자 계약에 맞춰 생성기에 **DB 세션만** 붙여 준다.

    생성기(`produce_ibk_dated_result`)는 세션을 만들지도 닫지도 않는다 — 그 소유권은
    legacy 경로가 :188/:300 에서 하듯 호출자에게 있다. runner 가 crawler 호출 전에 부르는
    `refresh_write_mode_cache` 는 제 세션만 열고 닫으므로 이 자리를 대신하지 않는다.

    ⛔ 결과의 의미를 바꾸지 않는다. 예상 밖 예외를 가짜 FAILED 프레임으로 포장하지도
       않는다 — semantic 프레임은 "관측하고 판정했다"는 주장이라, 판정 기구 자체가 실패한
       경우에 그것을 내면 거짓이 된다. 예외는 그대로 올려 runner 의 기존 기술 오류·재시도
       계약(app/crawlers/runner.py:79-81 → exit 1)을 그대로 쓴다.

    ⛔ 범위 밖(생성기와 동일): 당일 GET 우선순위, MIBANK, 부모 경보. 이 함수는 세션 수명만
       책임진다. lookback·Selenium 안전망·개장 전 보존 창 정책은 생성기가 **수행한다** —
       예전에 범위 밖이었으나 지금은 아니다.
    """
    db = SessionLocal()
    try:
        return produce_ibk_dated_result(db, context, timeout=timeout, now=now)
    finally:
        # close()가 던진 Exception은 잡아서 경고를 기록한다.
        # KeyboardInterrupt·SystemExit은 잡지 않아 본문의 결과나 예외를 가릴 수 있다.
        # 정리를 시도하며, close 실패 시 자원 반환 완료를 보장하지 않는다.
        try:
            db.close()
        except Exception:
            logger.warning("IBK_RESULT_SESSION_CLOSE_FAILED", extra={"bank": BANK_NAME})
