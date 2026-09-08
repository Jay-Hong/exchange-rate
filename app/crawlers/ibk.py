# app/crawlers/ibk.py

# 표준 라이브러리
import datetime
from dataclasses import dataclass
from enum import Enum
import logging
import math
import re
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
from app.ibk_result_protocol import IbkReason, IbkResult, IbkSource
from app.ibk_run_context import SERVICE_DATE_ROLLOVER_TIME
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
                write_return_count = crawl_and_save_ibk_routine_selenium(IBK_BANK_URL, IBK_BANK_SELECTORS, db)
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
    first_days_back = 0 if reference_time.time() >= IBK_SERVICE_DATE_ROLLOVER_TIME else 1
    candidate_rank = 0
    saw_explicit_no_notice = False
    saw_preopen_pending = False
    deadline = time.monotonic() + IBK_DATED_REQUEST_SOFT_BUDGET_SECONDS
    for days_back in range(first_days_back, MAX_DAYS_LOOKBACK + 1):
        query_date = reference_date - datetime.timedelta(days=days_back)

        # 주말에 새 조회기준일 세션은 없다. 금요일 세션의 토요일 새벽 고시는
        # 금요일 날짜를 조회하면 나오므로 이 skip으로 손실되지 않는다.
        if query_date.weekday() >= 5:
            continue

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

    rate_table = None
    for table in soup.find_all('table'):
        caption = table.find('caption')
        if caption and caption.get_text(' ', strip=True) == '일반고시환율 표':
            rate_table = table
            break

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

    code_to_pair = {
        'USD': 'usd-krw',
        'JPY': 'jpy-krw',
        'EUR': 'eur-krw',
    }
    current_rates = {}
    for row in rate_table.find_all('tr')[1:]:
        cells = row.find_all(['th', 'td'])
        if not cells:
            continue
        code = cells[0].get_text(' ', strip=True).upper()
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


def crawl_and_save_ibk_routine_selenium(url: str, selectors: dict, db: Session) -> int:
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


def produce_ibk_dated_result(db, context, *, timeout=DEFAULT_TIMEOUT, now=None) -> IbkResult:
    """한 서비스일의 공식 후보를 검증부터 최종 판정까지 연결한다. 아직 연결하지 않는다.

    범위 밖: lookback, 당일 GET 우선순위, Selenium, MIBANK, 부모 경보.

    표 부재는 두 가지다. 개장 전 창 안(당일 조회기준일 + 08:00~08:34:59)에서는 첫 고시
    전이라 표가 없는 것이 정상이므로 **보존 승인**(PREOPEN_PENDING)으로 접는다. 그 밖의
    표 부재는 정상 개장 전 화면과 구분할 수 없으므로 실패 원인으로 남긴다.
    ⛔ 이 보존은 "기존 DB 값을 유지한다" 는 뜻이지 이전 기준일 후보를 검증했다는 뜻이
       아니다 — 그 lookback 은 아직 없다.
    """
    service_date = datetime.date.fromisoformat(context.expected_service_date)
    observed_at = now if now is not None else datetime.datetime.now(datetime.timezone.utc)
    official = preservation = failure = None
    # 공식 후보가 없을 때 쓸 보존 사유. 승인 자체는 DB 사실을 확보한 뒤에 만든다.
    preservation_reason = IbkReason.OFFICIAL_NO_SESSION
    intended = None
    write = IbkWriteAttempt.none()

    try:
        fetched = _fetch_ibk_rates_for_date(
            service_date, reference_time=context.reference_time, timeout=timeout
        )
    except IbkRateTableAbsentError:
        if _is_preopen_pending_window(service_date, context.reference_time):
            # 이번 실행에 공식 후보가 없다는 점은 무고시와 같다. 아래 분기가 만드는
            # 보존 상태를 그대로 쓰고 사유만 개장 전으로 남긴다.
            # ⛔ 승인을 여기서 미리 만들지 않는다. 그러면 뒤따르는 DB 실패와 공존해
            #    어댑터의 배타성(FAILURE_IS_EXCLUSIVE)을 깨고 결과가 예외로 끝난다.
            fetched = None
            preservation_reason = IbkReason.PREOPEN_PENDING
        else:
            failure = IbkObservationFailure(IbkReason.AMBIGUOUS_TABLE_ABSENT)
    except requests.RequestException:
        failure = IbkObservationFailure(IbkReason.TRANSPORT_ERROR)
    except ValueError:
        failure = IbkObservationFailure(IbkReason.CONTRACT_ERROR)

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
                IbkSource.OFFICIAL_POST, service_date, dict(current_rates), completion
            )
            regressing = find_regressing_pairs(
                current_rates, last_info, completion, IBK_DB_SAVE_LAG_TOLERANCE_SECONDS
            )
            intended = _intended_from_snapshot(before, regressing, current_rates)
            if intended is None:
                # 유지하려는 기존 값을 쓸 수 없다. 관측을 승격하지 않고 결과로 종결한다.
                official = None
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
        expected_service_date=service_date,
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

    ⛔ 범위 밖(생성기와 동일): lookback, 당일 GET 우선순위, Selenium, MIBANK,
       개장 전 보존 창 정책, 부모 경보. 이 함수는 세션 수명만 책임진다.
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
