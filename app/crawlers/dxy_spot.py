# app/crawlers/dxy_spot.py

"""
DXY(달러지수) spot 독립 크롤러

Primary: /indices/usdollar 의 __NEXT_DATA__
Fallback1: 같은 페이지의 CSS selector
Fallback2: Yahoo Finance

Yahoo fallback 시간 가드:
- ICE DX 주간 세션 OFF 구간에는 Yahoo 저장 차단
- 일요일 18:00 ET 이전, 금요일 17:00 ET 이후, 토요일 전체
- 화~금 일일 휴장(17:00~20:00 ET)은 아직 차단하지 않음

Yahoo fallback 보존 정책 (market mode 기반):
- OUT 모드 (주말): 72h 이내 Investing DB 값 존재 시 Yahoo 차단
- IN/BREAK 모드: 아래 두 조건이 모두 충족될 때만 Yahoo 차단 (하나라도 깨지면 Yahoo 허용)
  - fresh 성공이 grace 이내 (IN: 15분, BREAK: 30분)
  - 연속 실패가 임계값 미만 (IN: 3회, BREAK: 5회)

Yahoo 허용 경로:
- hard failure: 연속 실패 >= 임계값 → 즉시 허용 (fresh age 무관, ~30초)
- silent stale: fresh age >= grace → 허용 (failures=0이어도, 페이지는 열리지만 데이터 안 바뀜)
"""

# 표준 라이브러리
import json
import logging
import random
from datetime import datetime, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

# 서드파티 라이브러리
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    _USE_CFFI = True
except Exception:
    import requests as cffi_requests
    _USE_CFFI = False

# 로컬 애플리케이션
from app.crawlers.constants import HEADERS, DEFAULT_TIMEOUT
from app.crawlers.dxy import fetch_dxy_from_cnbc, fetch_dxy_from_yahoo
from app.market_mode import get_market_mode, KST

# 크롤러 이름
CRAWLER_NAME = "dxy"

# Spot 소스
DXY_SPOT_URL = "https://kr.investing.com/indices/usdollar"
DXY_SPOT_SELECTORS = [
    '[data-test="instrument-price-last"]',
    '[class*="text-5xl"]',
]
DXY_PRICE_PATH = ("props", "pageProps", "state", "indexStore", "instrument", "price")

# DXY 유효 범위
DXY_RATE_RANGE = (80.0, 130.0)

# curl_cffi TLS 지문 위장
CFFI_IMPERSONATE = "safari17_0"

SAFARI_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

# --- Yahoo fallback 보존 정책 상수 ---
NY = ZoneInfo("America/New_York")

IN_SUCCESS_GRACE_SECONDS = 15 * 60       # IN 모드: 15분
BREAK_SUCCESS_GRACE_SECONDS = 30 * 60    # BREAK 모드: 30분
OUT_PRESERVE_SECONDS = 72 * 3600         # OUT 모드: 72시간

IN_FAILURE_THRESHOLD = 3                 # IN 모드: 연속 3회 실패
BREAK_FAILURE_THRESHOLD = 5              # BREAK 모드: 연속 5회 실패
DXY_YAHOO_DIFF_THRESHOLD = 0.07          # Yahoo 저장 전 마지막 Investing 값과 허용 차이

# --- 프로세스 메모리 상태 변수 ---
_last_source_ts_ms = 0                                      # spot primary stale 판정용
_consecutive_investing_failures = 0                          # 연속 Investing fetch 실패 횟수
_last_investing_fetch_ok_at: Optional[datetime] = None       # 마지막 Investing fresh 성공 시각 (UTC)

# 로거 설정
logger = logging.getLogger("exchange_rate.crawler.dxy_spot")


# --- Investing 성공/실패 마커 ---

def _mark_investing_fetch_ok() -> None:
    """Investing 페이지 fetch 성공 시 호출 (stale 포함). 실패 카운터만 리셋."""
    global _consecutive_investing_failures
    _consecutive_investing_failures = 0


def _mark_investing_fresh_success(now_utc: datetime) -> None:
    """Investing에서 fresh 데이터 확보 시 호출 (source_ts_ms 변경 또는 CSS 저장)."""
    global _consecutive_investing_failures, _last_investing_fetch_ok_at
    _consecutive_investing_failures = 0
    _last_investing_fetch_ok_at = now_utc


def _mark_investing_failure() -> int:
    """Investing fetch 실패 시 호출. 현재 연속 실패 횟수 반환."""
    global _consecutive_investing_failures
    _consecutive_investing_failures += 1
    return _consecutive_investing_failures


# --- Fresh age 계산 ---

def _fresh_age_seconds(now_utc: datetime, db=None) -> float:
    """
    마지막 fresh success로부터의 경과 시간 (초).

    메모리 우선, 없으면(재시작 직후) DB의 최신 Investing realtime timestamp로 보강.
    둘 다 없으면 inf.
    """
    ts = _last_investing_fetch_ok_at
    if ts is None and db is not None:
        from app import models
        latest = (
            db.query(models.MarketIndexRate.timestamp)
            .filter(
                models.MarketIndexRate.instrument == "dxy",
                models.MarketIndexRate.granularity == "realtime",
                models.MarketIndexRate.source == "investing",
            )
            .order_by(models.MarketIndexRate.timestamp.desc())
            .first()
        )
        if latest:
            ts = latest[0]
    if ts is None:
        return float("inf")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now_utc - ts).total_seconds()


# --- Yahoo fallback 허용 판정 ---

def _is_dxy_weekly_session_open(now_utc: datetime) -> bool:
    """
    DXY Yahoo fallback 저장을 허용할 주간 세션인지 판정.

    기준은 ICE DX 주간 단위 세션(일 18:00 ET ~ 금 17:00 ET)이다.
    DST/표준시는 America/New_York ZoneInfo가 자동 처리한다.

    의도적으로 화~금 일일 휴장(17:00~20:00 ET)은 차단하지 않는다.
    운영 DXY 피드가 해당 시간대에도 갱신된 관측이 있어 별도 검증 전까지
    주말/주간 휴장 구간만 막는다.
    """
    now_ny = now_utc.astimezone(NY)
    weekday = now_ny.weekday()  # Monday=0 ... Sunday=6

    if weekday == 6 and now_ny.hour < 18:
        return False
    if weekday == 4 and now_ny.hour >= 17:
        return False
    if weekday == 5:
        return False
    return True

def _should_use_yahoo_fallback(
    *,
    now_utc: datetime,
    mode: str,
    latest_investing_ts: Optional[datetime],
) -> Tuple[bool, dict]:
    """
    Yahoo fallback을 허용할지 판정.

    IN/BREAK 판정 기준은 _last_investing_fetch_ok_at (fresh success만 갱신).
    stale(주말 페이지 열림)는 실패 카운터만 리셋하고 이 시각은 갱신하지 않으므로,
    월요일 장 개시 후 Investing 장애 시 즉시 grace period가 만료된 상태로 시작.

    Returns:
        (허용 여부, 로그용 메타데이터)
    """
    failures = _consecutive_investing_failures
    last_fresh = _last_investing_fetch_ok_at

    # 마지막 fresh 성공 시각: 메모리 우선, 없으면 DB 보강 (재시작 직후)
    effective_last_fresh = last_fresh
    if effective_last_fresh is None and latest_investing_ts is not None:
        ts = latest_investing_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        effective_last_fresh = ts

    meta = {
        "mode": mode,
        "consecutive_failures": failures,
        "last_fresh_source": "memory" if last_fresh is not None else ("db" if effective_last_fresh is not None else "none"),
    }

    if effective_last_fresh is not None:
        if effective_last_fresh.tzinfo is None:
            effective_last_fresh = effective_last_fresh.replace(tzinfo=timezone.utc)
        age = (now_utc - effective_last_fresh).total_seconds()
        meta["last_fresh_age_seconds"] = int(age)
    else:
        age = float("inf")
        meta["last_fresh_age_seconds"] = -1

    if latest_investing_ts is not None:
        ts = latest_investing_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        meta["investing_rate_age_seconds"] = int((now_utc - ts).total_seconds())

    if mode == "OUT":
        # 주말: 72h DB 가드 (기존 정책)
        if latest_investing_ts is not None:
            ts = latest_investing_ts
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            db_age = (now_utc - ts).total_seconds()
            if db_age < OUT_PRESERVE_SECONDS:
                meta["reason"] = "OUT_preserve"
                return False, meta
        meta["reason"] = "OUT_expired_or_no_data"
        return True, meta

    elif mode == "IN":
        # 장중: 15분 이내 fresh 성공 AND 연속 실패 3회 미만일 때만 보호
        # - hard failure: failures >= 3 → Yahoo 허용
        # - silent stale: age >= 15분 → Yahoo 허용
        if age < IN_SUCCESS_GRACE_SECONDS and failures < IN_FAILURE_THRESHOLD:
            meta["reason"] = "IN_protected"
            return False, meta
        meta["reason"] = "IN_stale_or_failure"
        return True, meta

    else:
        # BREAK1/BREAK2: 30분 이내 fresh 성공 AND 연속 실패 5회 미만일 때만 보호
        if age < BREAK_SUCCESS_GRACE_SECONDS and failures < BREAK_FAILURE_THRESHOLD:
            meta["reason"] = "BREAK_protected"
            return False, meta
        meta["reason"] = "BREAK_stale_or_failure"
        return True, meta


# --- 외부 fallback 실행 (공용 헬퍼) ---

# 외부 fallback chain: (source 이름, fetch 함수)
# 우선순위: CNBC(신선) > Yahoo(10분 stale, 최후 보루)
# 정책:
#   - 시간 가드 / mode 보존 정책 / fresh-age diff guard 모두 공통 적용
#   - chain 내 한 source가 fetch 성공 + diff guard 차단 → 이후 source도 차단
#     (fresh Investing 기준 outlier 신호로 간주)
#   - chain 내 한 source가 fetch 실패 → 다음 source로 진행
_FALLBACK_CHAIN = [
    ("cnbc", fetch_dxy_from_cnbc),
    ("yahoo", fetch_dxy_from_yahoo),
]


def _try_external_fallback(db, now_utc: datetime) -> None:
    """
    외부 fallback chain 시도 (CNBC > Yahoo).

    market mode 기반 보존 정책 + 시간 가드 + fresh-age 기반 diff guard 적용.
    hard failure 경로와 silent stale 경로에서 공용으로 호출.
    """
    from app import crud, models

    if not _is_dxy_weekly_session_open(now_utc):
        now_ny = now_utc.astimezone(NY)
        logger.info(
            f"🛡️ DXY 외부 폴백 차단 (주간 세션 OFF, ny={now_ny.strftime('%Y-%m-%d %H:%M:%S')} {now_ny.strftime('%a')})",
            extra={
                "now_utc": now_utc.isoformat(),
                "now_ny": now_ny.isoformat(),
            },
        )
        return

    now_kst = now_utc.astimezone(KST)
    mode = get_market_mode(now_kst)

    latest_investing = (
        db.query(models.MarketIndexRate)
        .filter(
            models.MarketIndexRate.instrument == "dxy",
            models.MarketIndexRate.granularity == "realtime",
            models.MarketIndexRate.source == "investing",
        )
        .order_by(models.MarketIndexRate.timestamp.desc())
        .first()
    )

    use_yahoo, meta = _should_use_yahoo_fallback(
        now_utc=now_utc,
        mode=mode,
        latest_investing_ts=latest_investing.timestamp if latest_investing else None,
    )

    if not use_yahoo:
        logger.info(
            f"🛡️ DXY 외부 폴백 스킵 (Investing 값 보존, "
            f"reason={meta.get('reason')}, "
            f"mode={meta.get('mode')}, "
            f"fresh_age={meta.get('last_fresh_age_seconds')}s, "
            f"failures={meta.get('consecutive_failures')}, "
            f"investing_age={meta.get('investing_rate_age_seconds')}s)",
            extra={
                **meta,
                "investing_rate": latest_investing.rate if latest_investing else None,
            },
        )
        return

    diff_guard_grace_seconds = (
        IN_SUCCESS_GRACE_SECONDS if mode == "IN" else BREAK_SUCCESS_GRACE_SECONDS
    )

    for source_name, fetch_fn in _FALLBACK_CHAIN:
        try:
            rate = fetch_fn()
        except Exception as exc:
            logger.warning(
                f"⚠️ DXY {source_name} 폴백 fetch 실패 "
                f"(mode={meta.get('mode')}, failures={meta.get('consecutive_failures')}, "
                f"err={type(exc).__name__}: {str(exc)[:120]})",
                extra={**meta, "fallback_source": source_name, "error": str(exc)},
            )
            continue  # 다음 source 시도

        # diff guard: latest_investing fresh일 때만 적용
        if latest_investing is not None:
            investing_age_seconds = meta.get("investing_rate_age_seconds")
            if (
                investing_age_seconds is not None
                and investing_age_seconds <= diff_guard_grace_seconds
            ):
                diff = abs(rate - latest_investing.rate)
                if diff > DXY_YAHOO_DIFF_THRESHOLD:
                    logger.info(
                        f"🛡️ DXY {source_name} 임계값 초과 — chain 전체 저장 보류 "
                        f"(rate={rate}, inv={latest_investing.rate}, "
                        f"diff={round(diff, 4)}, threshold={DXY_YAHOO_DIFF_THRESHOLD}, "
                        f"inv_age={investing_age_seconds}s, mode={mode})",
                        extra={
                            **meta,
                            "fallback_source": source_name,
                            "fetched_rate": rate,
                            "investing_rate": latest_investing.rate,
                            "diff": round(diff, 4),
                            "threshold": DXY_YAHOO_DIFF_THRESHOLD,
                            "diff_guard_grace_seconds": diff_guard_grace_seconds,
                        },
                    )
                    # 외부값이 fresh Investing과 충돌 → 다음 source도 outlier 가능성 → chain 종료
                    return

        # 저장 성공 → chain 종료 (Yahoo까지 안 감)
        crud.insert_dxy_rate_into_db(db=db, rate=rate, source=source_name)
        logger.info(
            f"📦 DXY {source_name} 폴백 저장 "
            f"(rate={rate}, mode={meta.get('mode')}, "
            f"fresh_age={meta.get('last_fresh_age_seconds')}s, "
            f"failures={meta.get('consecutive_failures')})",
            extra={**meta, "rate": rate, "source": source_name, "fallback_source": source_name},
        )
        return

    # chain 전체 실패
    logger.warning(
        f"⚠️ DXY 외부 폴백 chain 전체 실패 "
        f"(tried={[s for s, _ in _FALLBACK_CHAIN]}, mode={meta.get('mode')}, "
        f"failures={meta.get('consecutive_failures')})",
        extra={**meta, "tried_sources": [s for s, _ in _FALLBACK_CHAIN]},
    )


# 하위 호환: 기존 호출 위치는 유지
_try_yahoo_fallback = _try_external_fallback


# --- HTTP / 파싱 헬퍼 ---

def _build_headers() -> dict:
    headers = dict(HEADERS)
    headers["User-Agent"] = random.choice(SAFARI_UA_POOL)
    return headers


def _http_get(url: str, headers: dict):
    if _USE_CFFI:
        return cffi_requests.get(
            url,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            impersonate=CFFI_IMPERSONATE,
        )
    return cffi_requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)


def _is_valid_rate(rate: float) -> bool:
    return DXY_RATE_RANGE[0] <= rate <= DXY_RATE_RANGE[1]


def _fetch_spot_page() -> BeautifulSoup:
    response = _http_get(DXY_SPOT_URL, headers=_build_headers())
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def _extract_next_data_price(soup: BeautifulSoup) -> Tuple[float, int]:
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise ValueError("__NEXT_DATA__ script not found")

    payload_raw = script.string or script.get_text()
    if not payload_raw:
        raise ValueError("__NEXT_DATA__ payload empty")

    payload = json.loads(payload_raw)
    price = payload
    for key in DXY_PRICE_PATH:
        price = price[key]

    rate = float(price["last"])
    if not _is_valid_rate(rate):
        raise ValueError(f"DXY spot 범위 초과: {rate}")

    source_ts_ms = int(price["lastUpdateTime"])
    return rate, source_ts_ms


def _extract_selector_price(soup: BeautifulSoup) -> float:
    for selector in DXY_SPOT_SELECTORS:
        element = soup.select_one(selector)
        if not element:
            continue

        rate_text = element.get_text(strip=True).replace(",", "")
        try:
            rate = float(rate_text)
        except ValueError:
            logger.warning(
                "⚠️ DXY spot CSS 파싱 실패",
                extra={"rate_text": rate_text, "selector": selector},
            )
            continue

        if _is_valid_rate(rate):
            return rate

        logger.warning(
            "⚠️ DXY spot CSS 값 범위 초과",
            extra={"rate": rate, "selector": selector, "range": DXY_RATE_RANGE},
        )

    raise ValueError("DXY spot CSS selector: 유효한 값을 찾을 수 없음")


def fetch_dxy_from_investing_spot() -> Tuple[float, int]:
    """Primary: /indices/usdollar __NEXT_DATA__."""
    soup = _fetch_spot_page()
    return _extract_next_data_price(soup)


def fetch_dxy_from_investing_spot_fallback() -> float:
    """Fallback1: /indices/usdollar 동일 페이지 CSS selector."""
    soup = _fetch_spot_page()
    return _extract_selector_price(soup)


# --- 메인 크롤링 함수 ---

def crawl_and_save_dxy_spot() -> None:
    """
    DXY spot 독립 수집 + DB 저장

    - Primary 성공 시 lastUpdateTime 동일 여부로 stale 판정
    - Primary 실패 시 같은 페이지 CSS selector 폴백
    - 최종 실패 시 Yahoo 폴백 (market mode 기반 보존 정책 적용)
    - Silent stale 시 IN/BREAK 모드에서 grace 초과하면 Yahoo 폴백
    """
    global _last_source_ts_ms
    from app import crud
    from app.database import SessionLocal

    now_utc = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        try:
            soup = _fetch_spot_page()

            try:
                rate, source_ts_ms = _extract_next_data_price(soup)

                if source_ts_ms == _last_source_ts_ms:
                    # stale: 페이지는 열리지만 데이터 변화 없음
                    _mark_investing_fetch_ok()  # 실패 카운터만 리셋

                    # IN/BREAK에서 silent stale이 grace 초과하면 Yahoo 판정
                    now_kst = now_utc.astimezone(KST)
                    mode = get_market_mode(now_kst)
                    if mode != "OUT":
                        fresh_age = _fresh_age_seconds(now_utc, db=db)
                        grace = IN_SUCCESS_GRACE_SECONDS if mode == "IN" else BREAK_SUCCESS_GRACE_SECONDS
                        if fresh_age >= grace:
                            logger.warning(
                                "⚠️ DXY silent stale 감지 (Yahoo 판정 진행)",
                                extra={
                                    "mode": mode,
                                    "fresh_age_seconds": int(fresh_age),
                                    "source_ts_ms": source_ts_ms,
                                },
                            )
                            _try_yahoo_fallback(db, now_utc)
                            return

                    logger.debug(
                        "📼 DXY spot source timestamp 유지",
                        extra={"source_ts_ms": source_ts_ms, "source": "investing"},
                    )
                    return

                # fresh: 실제 새 데이터 확보
                _mark_investing_fresh_success(now_utc)
                _last_source_ts_ms = source_ts_ms
                crud.insert_dxy_rate_into_db(db=db, rate=rate, source="investing")
                logger.info(
                    "📦 DXY spot primary 저장",
                    extra={"rate": rate, "source": "investing", "source_ts_ms": source_ts_ms},
                )
                return
            except Exception:
                logger.warning("⚠️ DXY spot primary 파싱 실패", exc_info=True)

            # CSS fallback 성공 = fresh 데이터 확보
            rate = _extract_selector_price(soup)
            _mark_investing_fresh_success(now_utc)
            # CSS 경로에서는 source_ts_ms를 모르므로 리셋 (다음 primary 복구 시 정상 동작)
            _last_source_ts_ms = 0
            crud.insert_dxy_rate_into_db(db=db, rate=rate, source="investing")
            logger.info("📦 DXY spot CSS 폴백 저장", extra={"rate": rate, "source": "investing"})
            return
        except Exception:
            failures = _mark_investing_failure()
            logger.warning(
                "⚠️ DXY spot Investing 수집 실패",
                extra={"consecutive_failures": failures},
                exc_info=True,
            )

        # Hard failure → Yahoo fallback (보존 정책 적용)
        _try_yahoo_fallback(db, now_utc)

    except Exception:
        logger.warning("⚠️ DXY spot 전체 fallback 실패 (무시)", extra={"crawler": CRAWLER_NAME}, exc_info=True)
    finally:
        db.close()
