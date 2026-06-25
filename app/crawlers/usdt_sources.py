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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
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

_KST = timezone(timedelta(hours=9))

# Bithumb REST `trade_timestamp` +9h KST-as-UTC 결함(2026-06-22 실측) 재발 fail-closed 임계.
# 정상 체결시각은 ≈now 이하 — now+5min 초과 미래는 결함/오염으로 판정(+9h=32400s 결함과
# 초 단위 정상 clock skew를 명확히 가르는 여유 임계). `_bithumb_rest_event_ms`의 최후
# raw 폴백이 미래 schema 변화로 결함값을 흘릴 때를 보강 (Codex review 산물).
_BITHUMB_MAX_FUTURE_SKEW_MS = 5 * 60 * 1000

# 거래소별 USDT/KRW 퍼블릭 엔드포인트 및 응답 price 필드 파서
# 검증일: 2026-04-12 (USDT_TAB_PROPOSAL.md의 Confirmed Public Crypto Endpoints)


def fetch_upbit_usdt_tick(timeout: float = PER_SOURCE_TIMEOUT_SECONDS) -> Optional[dict]:
    """Upbit USDT/KRW REST ticker → normalized tick dict (PR2 parser 계약과 동일).

    PR7 fallback controller (`app/crawlers/usdt_ws/upbit.py`)가 stale 감지 시
    호출. 기존 `_fetch_upbit()` polling helper도 본 함수 재사용 (additive
    refactor, REST polling 동작 보존).

    Returns:
        {"source": "upbit", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}
        또는 REST/parse 실패 시 None (caller 격리).

    timestamp_ms:
        Upbit REST 응답의 `trade_timestamp` 우선, 없으면 `timestamp` 폴백
        (PR2 _parse_ticker_message 동일 패턴, guide §3 spec). time.time() 사용
        시 wallclock at probe execution = exchange quote time이 아님.
    """
    url = "https://api.upbit.com/v1/ticker?markets=KRW-USDT"
    try:
        response = requests.get(url, timeout=timeout, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        item = data[0]
        rate = float(item["trade_price"])
        # PR2 _parse_ticker_message 동일 보호 (Codex Finding 2):
        # 0/음수 rate가 fallback fanout (Redis/DB/Alert) 통과 시 잘못된 알림 위험.
        if rate <= 0:
            return None
        ts_ms = int(item.get("trade_timestamp") or item["timestamp"])
        return {
            "source": "upbit",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }
    except (requests.RequestException, KeyError, ValueError, TypeError, IndexError):
        # caller(fallback controller / polling)가 None 처리. propagate X.
        return None


def _fetch_upbit() -> Optional[float]:
    """기존 polling helper — fetch_upbit_usdt_tick 재사용 (rate만 반환)."""
    tick = fetch_upbit_usdt_tick()
    return tick["rate"] if tick else None


def _bithumb_rest_event_ms(item: dict) -> Optional[int]:
    """Bithumb REST v1/ticker item → true UTC epoch ms (true UTC 기준).

    ⚠️ Bithumb REST v1/ticker의 `trade_timestamp`/`timestamp`는 KST 벽시계를
    UTC epoch인 것처럼 인코딩해 실제 UTC epoch보다 +9h 크다 (2026-06-22 실측,
    REST diff=+9.0h / WS diff≈0h — 같은 v1 스키마인데 REST만의 결함). 파이프라인
    계약은 timestamp_ms = true UTC epoch ms (`crud.event_ms_to_utc_naive`,
    Redis `seen_at`/regression guard 모두 이를 전제)이라, 그대로 쓰면 +9h 미래
    시각이 저장되고 regression guard가 이후 정상 tick을 ~9h 동안 차단(freeze)한다.
    → 명시적으로 라벨된 벽시계 필드에서 epoch을 재구성한다 (`trade_timestamp` 결함
    비의존, self-correcting). WS 경로(`bithumb._parse_ticker_message`)는 `trade_timestamp`가
    정상이고 `*_kst` 필드도 없으므로 본 helper 미적용 — Bithumb 엔드포인트 간 비대칭
    그대로 반영.

    우선순위:
      1. trade_date_kst + trade_time_kst (명시 KST) → KST datetime → epoch
      2. trade_date + trade_time (명시 UTC) → UTC datetime → epoch
      3. 둘 다 없으면 raw trade_timestamp/timestamp (최후 폴백; 결함 가능하나 None 회피)
    초 단위 정밀도 (벽시계 필드에 ms 없음) — seen_at 5s floor라 무해.
    """
    def _epoch_ms(date_str, time_str, tz) -> Optional[int]:
        if not date_str or not time_str:
            return None
        try:
            dt = datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M%S")
        except (TypeError, ValueError):
            return None
        return int(dt.replace(tzinfo=tz).timestamp() * 1000)

    kst_ms = _epoch_ms(item.get("trade_date_kst"), item.get("trade_time_kst"), _KST)
    if kst_ms is not None:
        return kst_ms
    utc_ms = _epoch_ms(item.get("trade_date"), item.get("trade_time"), timezone.utc)
    if utc_ms is not None:
        return utc_ms
    raw = item.get("trade_timestamp") or item.get("timestamp")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def fetch_bithumb_usdt_tick(timeout: float = PER_SOURCE_TIMEOUT_SECONDS) -> Optional[dict]:
    """Bithumb USDT/KRW REST ticker → normalized tick dict.

    Upbit `fetch_upbit_usdt_tick()` (line 40) shape mirror.
    Phase B.3 Stage U6 (USDT_WS_DESIGN_PLAN §12.5.2): BithumbRestFallbackController가
    WS silence 감지 시 호출. 기존 `_fetch_bithumb()` polling helper도 본 함수 재사용
    (additive refactor, REST polling 동작 보존).

    Returns:
        {"source": "bithumb", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}
        또는 REST/parse 실패 시 None (caller 격리).

    timestamp_ms:
        `_bithumb_rest_event_ms`로 true UTC epoch ms 재구성. ⚠️ Bithumb REST의
        `trade_timestamp`/`timestamp`는 +9h KST-as-UTC 결함이라 raw로 쓰면 미래
        시각이 저장돼 regression guard freeze를 유발 → 벽시계 필드 기반 정규화
        (상세는 `_bithumb_rest_event_ms` docstring). WS 경로는 정상이라 미적용.

    Guard (Upbit Codex Finding 2 mirror — A11):
        - 0/음수 rate가 fallback fanout (Redis/DB) 통과 시 잘못된 알림 위험
        - rate <= 0 → None
    """
    url = "https://api.bithumb.com/v1/ticker?markets=KRW-USDT"
    try:
        response = requests.get(url, timeout=timeout, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        item = data[0]
        rate = float(item["trade_price"])
        if rate <= 0:
            return None
        ts_ms = _bithumb_rest_event_ms(item)
        if ts_ms is None:
            return None
        # +9h 결함 재발 fail-closed: wall-clock 필드가 사라지고 raw trade_timestamp만
        # 남는 미래 schema 변화에도 미래값을 latest로 흘리지 않게 (regression guard
        # ~9h freeze 차단). 정상 tick은 과거 체결시각이라 미영향. skip된 probe는
        # WS primary + 다음 probe로 자연 회복.
        if ts_ms > int(time.time() * 1000) + _BITHUMB_MAX_FUTURE_SKEW_MS:
            logger.warning(
                "[usdt_sources.bithumb] REST timestamp_ms 비정상 미래(%dms) — "
                "+9h 결함 의심, fail-closed (tick None)", ts_ms,
            )
            return None
        return {
            "source": "bithumb",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }
    except (requests.RequestException, KeyError, ValueError, TypeError, IndexError):
        # caller(fallback controller / polling)가 None 처리. propagate X.
        return None


def _fetch_bithumb() -> Optional[float]:
    """기존 polling helper — fetch_bithumb_usdt_tick 재사용 (rate만 반환).

    A2: 기존 rate-only 호환 유지. `FETCHERS` registry는 그대로 동작.
    """
    tick = fetch_bithumb_usdt_tick()
    return tick["rate"] if tick else None


def fetch_coinone_usdt_tick(timeout: float = PER_SOURCE_TIMEOUT_SECONDS) -> Optional[dict]:
    """Coinone USDT/KRW REST ticker → normalized tick dict.

    Bithumb `fetch_bithumb_usdt_tick()` (line 85) shape mirror.
    Phase B.4 Stage C6b (USDT_WS_DESIGN_PLAN §12.6): CoinoneRestFallbackController가
    ticker freshness degraded (DATA frame age > 300s provisional) 시 호출.
    기존 `_fetch_coinone()` polling helper도 본 함수 재사용 (additive refactor).

    Returns:
        {"source": "coinone", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}
        또는 REST/parse 실패 시 None (caller 격리).

    Coinone REST response shape (실측 2026-05-19):
        {"result": "success", "tickers": [{"quote_currency": "krw", "target_currency": "usdt",
         "timestamp": 1779179058028, "last": "1488.0", ...}]}
        - quote/target_currency는 **소문자** ("krw"/"usdt") — REST 한정 (WS는 대문자).
        - timestamp: epoch ms (Bithumb/Upbit과 같은 단위)

    Guard:
        - result != "success" → None
        - quote_currency != "krw" or target_currency != "usdt" → None
        - 0/음수 rate → None
        - timestamp 부재 / parse 실패 → None
    """
    url = "https://api.coinone.co.kr/public/v2/ticker_utc_new/KRW/USDT"
    try:
        response = requests.get(url, timeout=timeout, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        if data.get("result") != "success":
            return None
        ticker = data["tickers"][0]
        # REST 응답의 quote/target_currency는 lowercase. case-insensitive 검증.
        if ticker.get("quote_currency", "").lower() != "krw":
            return None
        if ticker.get("target_currency", "").lower() != "usdt":
            return None
        rate = float(ticker["last"])
        if rate <= 0:
            return None
        ts_ms = int(ticker["timestamp"])
        return {
            "source": "coinone",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }
    except (requests.RequestException, KeyError, ValueError, TypeError, IndexError):
        # caller(fallback controller / polling)가 None 처리. propagate X.
        return None


def _fetch_coinone() -> Optional[float]:
    """기존 polling helper — fetch_coinone_usdt_tick 재사용 (rate만 반환).

    Phase B.4 C6b: 기존 rate-only 호환 유지. `FETCHERS` registry는 그대로 동작.
    """
    tick = fetch_coinone_usdt_tick()
    return tick["rate"] if tick else None


def fetch_korbit_usdt_tick(timeout: float = PER_SOURCE_TIMEOUT_SECONDS) -> Optional[dict]:
    """Korbit USDT/KRW REST ticker → normalized tick dict.

    Coinone `fetch_coinone_usdt_tick()` (line 135) + Bithumb `fetch_bithumb_usdt_tick()`
    (line 85) shape mirror. Phase B.5 Stage K6b (USDT_WS_DESIGN_PLAN §12.7):
    KorbitRestFallbackController가 ticker freshness degraded (DATA frame age > 120s
    provisional) 시 호출. 기존 `_fetch_korbit()` polling helper도 본 함수 재사용
    (additive refactor).

    Returns:
        {"source": "korbit", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}
        또는 REST/parse 실패 시 None (caller 격리).

    Korbit REST response shape (실측 K-2 smoke 2026-05-19):
        {"success": True, "data": [{"symbol": "usdt_krw", "close": "1488",
         "lastTradedAt": 1779194593306, ...}]}
        - success: boolean (Coinone "success" 문자열과 다른 strict bool, Codex 강조)
        - symbol: 소문자 underscore (WS와 동일)
        - close: string price
        - lastTradedAt: epoch ms (top-level timestamp 없음, K-1 audit 확인)

    Guard (Codex 6 + 1 + 후속 정정 통합):
        - payload가 dict 아님 (list/str/null/etc.) → None
          (Codex 후속 정정 — blocker급, top-level malformed payload 격리)
        - success is not True → None (boolean strict, Coinone 스타일 문자열과 분리)
        - data가 list 아님 or 빈 list → None (malformed container, Codex 추가)
        - data[0]가 dict 아님 → None
        - symbol missing 또는 != "usdt_krw" → None
        - close 부재 / parse 실패 / <= 0 → None
        - lastTradedAt 부재 / parse 실패 → None
    """
    url = "https://api.korbit.co.kr/v2/tickers?symbol=usdt_krw"
    try:
        response = requests.get(url, timeout=timeout, headers=HEADERS)
        response.raise_for_status()
        payload = response.json()
        # Codex Point 1 (blocker급 정정): malformed top-level payload — list/str/null/etc.
        # payload.get() 호출 시 AttributeError 차단. helper contract ("parse 실패 시 None") 유지.
        if not isinstance(payload, dict):
            return None
        if payload.get("success") is not True:
            return None
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return None
        item = data[0]
        if not isinstance(item, dict):
            return None
        if item.get("symbol") != "usdt_krw":
            return None
        close_raw = item.get("close")
        if close_raw is None:
            return None
        rate = float(close_raw)
        if rate <= 0:
            return None
        ts_raw = item.get("lastTradedAt")
        if ts_raw is None:
            return None
        ts_ms = int(ts_raw)
        return {
            "source": "korbit",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }
    except (requests.RequestException, KeyError, ValueError, TypeError, IndexError):
        # caller(fallback controller / polling)가 None 처리. propagate X.
        return None


def _fetch_korbit() -> Optional[float]:
    """기존 polling helper — fetch_korbit_usdt_tick 재사용 (rate만 반환).

    Phase B.5 K6b: 기존 rate-only 호환 유지. `FETCHERS` registry는 그대로 동작.
    """
    tick = fetch_korbit_usdt_tick()
    return tick["rate"] if tick else None


def fetch_gopax_usdt_tick(timeout: float = PER_SOURCE_TIMEOUT_SECONDS) -> Optional[dict]:
    """Gopax USDT/KRW REST ticker → normalized tick dict.

    Bithumb/Korbit `fetch_*_usdt_tick()` shape mirror. Phase B.6 Stage G6b
    (USDT_WS_DESIGN_PLAN §12.9): GopaxRestFallbackController가 ticker freshness
    degraded 시 호출. 기존 `_fetch_gopax()` polling helper도 본 함수 재사용
    (additive refactor, REST polling 동작 보존 — Bithumb/Korbit 패턴).

    Returns:
        {"source": "gopax", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}
        또는 REST/parse 실패 시 None (caller 격리).

    Gopax REST response shape (Codex production curl 실측 2026-05-21):
        {"price": 1487, ..., "time": "2026-05-21T10:53:14.604Z"}
        - price: number (float 변환)
        - time: ISO 8601 UTC string (Z suffix) — int(...) 직접 호출 불가

    Timestamp 변환 (Codex 정정 — production 실측 기반):
        time ISO string → datetime.fromisoformat (Z를 +00:00로 변환) → epoch ms.

    Guard:
        - payload가 dict 아님 → None
        - rate <= 0 → None
        - time missing 또는 parse 실패 → None
        - timestamp <= 0 → None
    """
    url = "https://api.gopax.co.kr/trading-pairs/USDT-KRW/ticker"
    try:
        response = requests.get(url, timeout=timeout, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return None
        rate = float(data["price"])
        if rate <= 0:
            return None
        # Codex 정정: time ISO 8601 string → epoch ms. Z를 +00:00로 변환.
        time_text = data["time"]
        dt = datetime.fromisoformat(time_text.replace("Z", "+00:00"))
        ts_ms = int(dt.timestamp() * 1000)
        if ts_ms <= 0:
            return None
        return {
            "source": "gopax",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }
    except (requests.RequestException, KeyError, ValueError, TypeError):
        # caller(fallback controller / polling)가 None 처리. propagate X.
        return None


def fetch_gopax_last_traded_ms(timeout: float = PER_SOURCE_TIMEOUT_SECONDS) -> Optional[int]:
    """Gopax /tickers bulk → USDT-KRW lastTraded(체결시각 epoch ms) 조회.

    §12.9.8 ① heartbeat-alive·ticker-dead detector 전용. WS frame의 timestamp_ms도
    lastTraded 기반(`gopax.py` `_normalize_tick`)이라 **동일 필드·단위(ms)·clock domain**
    → 변환 없이 직결 비교. `fetch_gopax_usdt_tick`(/ticker, write-path)과 **별 함수로
    분리** → 기존 fetch shape / Redis `<` regression guard / DB·Alert timestamp 의미
    무접촉 (Note 2 차단).

    Returns:
        lastTraded epoch ms (int > 0) — USDT-KRW 항목.
        None — HTTP/parse 실패 / list 아님 / USDT-KRW 항목 부재 / lastTraded 누락·0
        (caller가 None 처리, propagate X).
    """
    url = "https://api.gopax.co.kr/tickers"
    try:
        response = requests.get(url, timeout=timeout, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            return None
        for item in data:
            if item.get("tradingPairName") == "USDT-KRW":
                last_traded = int(item.get("lastTraded", 0) or 0)
                return last_traded if last_traded > 0 else None
        return None
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return None


def _fetch_gopax() -> Optional[float]:
    """기존 polling helper — fetch_gopax_usdt_tick 재사용 (rate만 반환).

    Phase B.6 G6b: 기존 rate-only 호환 유지. `FETCHERS` registry는 그대로 동작.
    Bithumb/Korbit 패턴 mirror (additive refactor).
    """
    tick = fetch_gopax_usdt_tick()
    return tick["rate"] if tick else None


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
        True: Redis SET 성공 (helper SET) 또는 5s grain coalesce (helper SKIPPED) —
            둘 다 정상 latest 상태이므로 caller 관점 success.
        False: latest 조회 None / sync helper FAILED outcome / 예외.

    Best-effort: 실패는 logger.warning만. **USDT source는 Z-2d allowlist 미통과로
    mirror cycle skip** — 즉 본 direct write가 실패하면 mirror cycle이 safety
    repair하지 않는다. Miss는 B-Step 2 read path가 `get_latest_usdt_rate_from_sync_job`
    None 감지 후 DB fallback(`crud.get_latest_source_rates_for_topic`)로 처리.
    async circuit_breaker 오염 X (broadcast/mirror 격리).
    """
    latest = crud.get_latest_source_rate(db, source, asset)
    if latest is None:
        logger.warning(
            "USDT Redis direct write 실패 — latest 재조회 None",
            extra={"source": source, "asset": asset},
        )
        return False
    try:
        outcome = latest_rates_cache.set_latest_usdt_rate_from_sync_job(
            source=source,
            asset=asset,
            rate=latest["rate"],
            timestamp=latest["timestamp"],
        )
    except Exception:
        logger.exception(
            "USDT Redis direct write 예외 (격리, read path DB fallback에 의존)",
            extra={"source": source, "asset": asset},
        )
        return False
    # (5d-a) SKIPPED (5s grain coalesce)는 정상 — bool interface에서 success.
    # FAILED만 false-positive 회피하며 warning 발사.
    if outcome is latest_rates_cache.UsdtLatestWriteOutcome.FAILED:
        logger.warning(
            "USDT Redis direct write FAILED (best-effort, read path DB fallback에 의존)",
            extra={"source": source, "asset": asset, "rate": latest["rate"]},
        )
        return False
    # BLOCKED은 dormant — setter가 mode-independent라 더 이상 반환하지 않음(구 전역 FX
    # write-mode gate 제거). 방어적 분기로 보존: USDT/KRX 자체 cutover가 per-source gate를
    # 도입하면 그때 다시 활성. (도달 시엔 success로 오인 금지 — Redis latest 미갱신.)
    if outcome is latest_rates_cache.UsdtLatestWriteOutcome.BLOCKED:
        return False
    return True


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
                    # latest 즉시 갱신 (mirror cycle bypass).
                    # 실패는 best-effort — USDT는 Z-2d로 mirror cycle 미경유.
                    # Miss는 B-Step 2 read path가 DB fallback으로 처리.
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
