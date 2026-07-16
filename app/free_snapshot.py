"""ADR-039 Step 3 — 무료(비구독) 매시간 고정 스냅샷 endpoint 지원 모듈.

인증 필수·premium 불요·KRX 항상 제외. rate + real hourly graph를 매시간 경계로 고정 갱신한다.
**cron(precompute)이 Redis 단독 canonical writer.** serve(main.py)는 canonical만 반환하고 **DB로 재생성하지
않는다**(무료=1시간 고정 불변식 — serve가 DB 최신값으로 rebuild하면 같은 시간대에도 값이 바뀌어 유료 실시간
차등이 깨짐). Redis 성공값을 process-local last-good 보존 → 장애 시 마지막 canonical → 전무 시 503.
graph_v2_intraday의 sync precompute 관용구 이식.

KRX 2중 제외: rate = legacy_policy allowlist가 이미 배제(krx∉LEGACY_RATE_SOURCES) / graph =
graph_v2.build_tab(exclude_krx=True)의 _free_tab_series 무조건 필터. 조립 후 _assert_krx_free로 fail-closed 재확인.
"""

import json
import logging
from datetime import datetime

from pytz import timezone

from app import crud, graph_v2
from app.database import get_db_context

logger = logging.getLogger("exchange_rate.free_snapshot")

KST = timezone("Asia/Seoul")

# MVP = usd only (달러 주력 탭 — investing.usd + hana.usd + dxy로 sdr/market_index 양 reader 검증).
# 확장: FREE_SNAPSHOT_TABS 추가. 단 non-FX 탭(tether=usdt-krw)은 legacy_policy가 rate에서 usdt를 배제하므로
# source_rates 기반 별도 rate reader가 필요(N4) — "튜플 추가만"으로는 rate가 빈다.
FREE_SNAPSHOT_TABS = ("usd",)
FREE_SNAPSHOT_PERIODS = ("1w", "3m", "1y")
TAB_ASSET = {"usd": "usd-krw", "jpy": "jpy-krw", "eur": "eur-krw", "tether": "usdt-krw"}

# cron이 매시간 갱신 → 정상 시 항상 warm. long-TTL = cron 장애 시에도 Redis canonical 최대 ~25h 유지.
# (serve의 process-local last-good은 별개로 프로세스 수명 동안 유지 — Redis TTL과 무관, availability 우선.)
FREE_SNAPSHOT_TTL_SECONDS = 90000


def free_snapshot_key(tab: str, period: str) -> str:
    """무료 전용 Redis 키 (premium graph_v2:tab:* / latest:* 와 네임스페이스 분리 — §4.2/§3.1)."""
    return f"free:snapshot:{tab}:{period}"


def _snapshot_is_nonempty(payload: dict) -> bool:
    """rate entry 1개 이상 AND graph series 중 data 있는 것 1개 이상.

    N2 — 빈 build가 이전 정상값을 setex로 덮어쓰는 것을 방지(성공-반환이어도 내용이 비면 SET 생략).
    """
    rate_entries = payload.get("rate", {}).get("entries", [])
    series = payload.get("graph", {}).get("series", [])
    return bool(rate_entries) and any(s.get("data") for s in series)


def _assert_krx_free(payload: dict) -> None:
    """fail-closed — KRX가 rate/graph 어느 쪽에도 없어야 한다(N6, 무료엔 KRX 절대 불가). 위반 시 raise."""
    for s in payload.get("graph", {}).get("series", []):
        if str(s.get("id", "")).startswith("krx."):
            raise ValueError(f"KRX series가 무료 snapshot에 유입: {s.get('id')!r}")
    for e in payload.get("rate", {}).get("entries", []):
        if str(e.get("bank", "")) == "krx" or str(e.get("currency", "")) == "usd-krw-futures":
            raise ValueError(f"KRX rate가 무료 snapshot에 유입: {e!r}")


def build_free_snapshot_payload(db, tab: str, period: str, *, now_kst=None) -> dict:
    """무료 스냅샷 payload 조립.

    as_of = 시(hour) 경계 clamp(매시간 고정 갱신 계약, §4.2 — 데이터 recency 아닌 cadence 마커).
    graph range도 **as_of.date() 한 기준시각**에서 파생(N5 — 자정 경계 build가 as_of·range를 갈라놓지 않게).
    generated_at = 조립 **완료** 시각(build 후). rate = get_all_rates_flat(KRX-free) 중 TAB_ASSET[tab] 필터.
    graph = build_tab(exclude_krx=True). 조립 후 _assert_krx_free로 fail-closed 재확인(KRX 유입 시 raise).
    """
    now_kst = now_kst or datetime.now(KST)
    as_of = now_kst.replace(minute=0, second=0, microsecond=0)

    asset = TAB_ASSET[tab]
    rate_entries = [r for r in crud.get_all_rates_flat(db) if r.get("currency") == asset]

    # N5: graph range를 as_of.date()에 고정 → as_of(시경계)와 graph range.end가 항상 일관.
    graph = graph_v2.build_tab(db, tab, period, today_kst=as_of.date(), exclude_krx=True)

    payload = {
        "tab": tab,
        "period": period,
        "as_of": as_of.isoformat(),
        "generated_at": datetime.now(KST).isoformat(),   # N5: 조립 완료 시점
        "rate": {"asset": asset, "entries": rate_entries},
        "graph": {
            "series": graph["series"],
            "bucket_size": graph["metadata"]["bucket_size"],
            "range": graph["metadata"]["range"],
        },
    }
    _assert_krx_free(payload)   # fail-closed: KRX 유입 시 raise → caller가 setex 스킵
    return payload


def validate_snapshot_payload(payload, tab: str, period: str) -> bool:
    """cache-hit serve-time 재검증(B1 fail-closed) — 오염된 캐시(KRX/null/list/wrong-tab)를 그대로 반환하지 않게.

    dict + tab/period 일치 + nonempty + KRX-free. 하나라도 실패(또는 malformed schema로 예외) → False.
    _assert_krx_free는 이제 build 시점뿐 아니라 **serve 시점에도** 적용되어 진짜 단일 fail-closed 지점이 됨.
    전체를 try/except로 감싼다 — nested schema가 깨진 캐시(예: rate가 list)도 예외(500) 아닌 False→last-good/503으로 폴백.
    (완전한 schema validator는 아님 — as_of/range 누락처럼 접근 시 예외 없는 shape는 통과 가능. empty/타입예외/KRX 방어 목적.)
    """
    try:
        if not isinstance(payload, dict):
            return False
        if payload.get("tab") != tab or payload.get("period") != period:
            return False
        if not _snapshot_is_nonempty(payload):
            return False
        _assert_krx_free(payload)
        return True
    except Exception:
        return False


def precompute_free_snapshots() -> None:
    """scheduler cron용 sync job — FREE_SNAPSHOT_TABS×PERIODS 순회, 성공+non-empty+KRX-free일 때만 Redis SET.

    graph_v2_intraday.precompute_intraday_1d 관용구 이식. per-(tab,period) 격리 + 개별 세션(N3 — abort 전파 방지).
    실패/빈 build/KRX 유입(assert raise) 시 setex 생략 → 이전 정상값(long-TTL) 보존(keep-last-good).
    Redis 연결 실패만 전체 스킵.
    """
    import redis as sync_redis

    from app.config import REDIS_PASSWORD, REDIS_URL

    redis_client = None
    try:
        redis_client = sync_redis.from_url(REDIS_URL, password=REDIS_PASSWORD or None, decode_responses=True)
        redis_client.ping()
    except Exception:
        logger.exception("free_snapshot Redis 연결 실패 (precompute)")
        if redis_client is not None:
            try:
                redis_client.close()
            except Exception:
                pass
        return

    written = {}
    try:
        for tab in FREE_SNAPSHOT_TABS:
            for period in FREE_SNAPSHOT_PERIODS:
                try:
                    with get_db_context() as db:   # per-unit 세션(N3)
                        payload = build_free_snapshot_payload(db, tab, period)
                    if not _snapshot_is_nonempty(payload):
                        logger.warning("free_snapshot %s/%s 빈 build — SET 생략(keep-last-good)", tab, period)
                        continue
                    redis_client.setex(
                        free_snapshot_key(tab, period),
                        FREE_SNAPSHOT_TTL_SECONDS,
                        json.dumps(payload),
                    )
                    written[f"{tab}/{period}"] = len(payload["rate"]["entries"])
                except Exception:
                    logger.exception("free_snapshot %s/%s precompute 실패 — 격리(keep-last-good)", tab, period)
        logger.info("✅ free_snapshot precompute", extra={"written": written})
    finally:
        try:
            redis_client.close()
        except Exception:
            pass
