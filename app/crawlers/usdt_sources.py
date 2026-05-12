"""USDT/KRW 크롤러 — 국내 가상자산 거래소 5종 통합 수집.

Phase 1 대상:
- 업비트 (upbit)
- 빗썸 (bithumb)
- 코인원 (coinone)
- 코빗 (korbit)
- 고팍스 (gopax)

설계:
- 단일 scheduler job `task_usdt_sources`에서 5개 거래소를 병렬 fan-out
- 거래소별 개별 timeout (2초), 실패한 소스는 격리 (전체 job 실패 만들지 않음)
- 변경 시에만 source_rates INSERT (crud.insert_source_rate_if_changed)
- 알림 처리도 같은 job에서 이어서 수행
"""

# 표준 라이브러리
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

# 서드파티 라이브러리
import requests
from sqlalchemy.orm import Session

# 로컬 애플리케이션
from app import crud, latest_rates_cache
from app.crawlers.constants import HEADERS
from app.database import get_db_context
from app.source_registry import get_usdt_exchange_entries

logger = logging.getLogger("exchange_rate.crawler.usdt_sources")

PER_SOURCE_TIMEOUT_SECONDS = 2.0

# 거래소별 USDT/KRW 퍼블릭 엔드포인트 및 응답 price 필드 파서
# 검증일: 2026-04-12 (USDT_TAB_PROPOSAL.md의 Confirmed Public Crypto Endpoints)


def _fetch_upbit() -> Optional[float]:
    url = "https://api.upbit.com/v1/ticker?markets=KRW-USDT"
    response = requests.get(url, timeout=PER_SOURCE_TIMEOUT_SECONDS, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    return float(data[0]["trade_price"])


def _fetch_bithumb() -> Optional[float]:
    url = "https://api.bithumb.com/v1/ticker?markets=KRW-USDT"
    response = requests.get(url, timeout=PER_SOURCE_TIMEOUT_SECONDS, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    return float(data[0]["trade_price"])


def _fetch_coinone() -> Optional[float]:
    url = "https://api.coinone.co.kr/public/v2/ticker_utc_new/KRW/USDT"
    response = requests.get(url, timeout=PER_SOURCE_TIMEOUT_SECONDS, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    return float(data["tickers"][0]["last"])


def _fetch_korbit() -> Optional[float]:
    url = "https://api.korbit.co.kr/v2/tickers?symbol=usdt_krw"
    response = requests.get(url, timeout=PER_SOURCE_TIMEOUT_SECONDS, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    return float(data["data"][0]["close"])


def _fetch_gopax() -> Optional[float]:
    url = "https://api.gopax.co.kr/trading-pairs/USDT-KRW/ticker"
    response = requests.get(url, timeout=PER_SOURCE_TIMEOUT_SECONDS, headers=HEADERS)
    response.raise_for_status()
    data = response.json()
    return float(data["price"])


FETCHERS: dict[str, Callable[[], Optional[float]]] = {
    "upbit": _fetch_upbit,
    "bithumb": _fetch_bithumb,
    "coinone": _fetch_coinone,
    "korbit": _fetch_korbit,
    "gopax": _fetch_gopax,
}


def _fetch_one(source: str) -> tuple[str, Optional[float], Optional[str]]:
    """하나의 거래소에서 최신 USDT/KRW 가격을 가져온다.

    Returns:
        (source, rate_or_none, error_message_or_none)
    """
    fetcher = FETCHERS.get(source)
    if fetcher is None:
        return (source, None, "no fetcher registered")

    try:
        rate = fetcher()
        if rate is None or rate <= 0:
            return (source, None, f"invalid rate: {rate}")
        return (source, rate, None)
    except requests.Timeout:
        return (source, None, "timeout")
    except requests.RequestException as exc:
        return (source, None, f"request failed: {exc}")
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        return (source, None, f"parse failed: {exc}")


def _mirror_changed_source_to_redis(db: Session, source: str, asset: str) -> bool:
    """USDT direct write helper — INSERT 성공 시점에 Redis latest 즉시 갱신
    (PR Z-2e B-Step 1 Foundation, 재시도 spec).

    `insert_source_rate_if_changed`가 True 반환 직후 호출. DB에서 latest를
    재조회해 timestamp 확보 후 sync direct writer 호출.

    이번 spec(재시도)은 sync `redis.Redis` client 사용으로 직전 실패 원인(event
    loop binding mismatch + async circuit_breaker 오염)을 정면 해결.

    Args:
        db: SQLAlchemy session (INSERT commit 완료 상태).
        source: 거래소 식별자.
        asset: 통화쌍 ("usdt-krw").

    Returns:
        True: Redis SET 성공.
        False: latest 조회 실패 / sync helper 실패.

    Best-effort: 실패는 logger.warning만. mirror cycle(3초) safety repair에 의존
    → 사용자-facing 영향 0, async circuit_breaker 오염 X (broadcast/mirror 격리).
    """
    latest = crud.get_latest_source_rate(db, source, asset)
    if latest is None:
        logger.warning(
            "USDT Redis direct write 실패 — latest 재조회 None",
            extra={"source": source, "asset": asset},
        )
        return False
    try:
        success = latest_rates_cache.set_latest_source_rate_from_sync_job(
            source=source,
            asset=asset,
            rate=latest["rate"],
            timestamp=latest["timestamp"],
        )
    except Exception:
        logger.exception(
            "USDT Redis direct write 예외 (격리, mirror cycle 복구 의존)",
            extra={"source": source, "asset": asset},
        )
        return False
    if not success:
        logger.warning(
            "USDT Redis direct write False (best-effort, mirror cycle 복구 의존)",
            extra={"source": source, "asset": asset, "rate": latest["rate"]},
        )
    return success


def collect_usdt_rates() -> None:
    """스케줄러 job entry point.

    5개 거래소에서 USDT/KRW을 병렬 수집 → source_rates에 변경 시 저장 →
    알림 조건 체크.

    개별 소스 실패는 로그만 남기고 전체 job은 계속 진행한다.
    """
    entries = get_usdt_exchange_entries()
    sources = [entry.source for entry in entries]

    if not sources:
        logger.debug("USDT exchange 소스 없음, 수집 스킵")
        return

    results: dict[str, Optional[float]] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        futures = {executor.submit(_fetch_one, src): src for src in sources}
        for future in as_completed(futures):
            source, rate, error = future.result()
            if rate is not None:
                results[source] = rate
            if error is not None:
                errors[source] = error

    if errors:
        logger.warning(
            "USDT 일부 소스 수집 실패",
            extra={"failed_sources": errors},
        )

    if not results:
        logger.error("USDT 수집 전체 실패, 저장 스킵")
        return

    changed_rates: list[dict] = []
    with get_db_context() as db:
        for entry in entries:
            source = entry.source
            asset = entry.asset
            rate = results.get(source)
            if rate is None:
                continue

            try:
                inserted = crud.insert_source_rate_if_changed(
                    db=db,
                    source=source,
                    asset=asset,
                    rate=rate,
                )
                if inserted:
                    changed_rates.append({
                        "bank": source,      # 레거시 alert 호환을 위해 bank 키로 전달 가능
                        "source": source,
                        "currency": asset,
                        "asset": asset,
                        "rate": rate,
                    })
                    logger.info(
                        "⚡️ USDT 환율 변경",
                        extra={
                            "source": source,
                            "asset": asset,
                            "rate": rate,
                        },
                    )
                    # PR Z-2e B-Step 1 Foundation 재시도: sync Redis client로
                    # latest 즉시 갱신 (mirror cycle 3초 bypass).
                    # 실패는 best-effort — mirror cycle이 safety repair.
                    _mirror_changed_source_to_redis(db=db, source=source, asset=asset)
            except Exception:
                logger.exception(
                    "USDT 저장 실패",
                    extra={"source": source, "asset": asset, "rate": rate},
                )
                continue

        # 알림 처리는 source_notification_settings 전용 함수에서 (Phase 1에서 추가)
        if changed_rates:
            try:
                _process_source_alerts_safe(db, changed_rates)
            except Exception:
                logger.exception("source 알림 처리 실패")


def _process_source_alerts_safe(db: Session, changed_rates: list[dict]) -> None:
    """source 기반 알림 처리. 아직 CRUD 함수가 없으면 조용히 스킵."""
    processor = getattr(crud, "process_source_rate_alerts", None)
    if processor is None:
        # Phase 1 알림 CRUD는 후속 단계에서 추가. 지금은 수집만 동작.
        return
    processor(db, changed_rates)
