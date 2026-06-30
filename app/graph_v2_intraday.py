"""
Tether 1d graph v2 — 10min closed-bucket precompute cache (builder + payload).

ADR-035 / GRAPH_API_V2_CONTRACT §4 line 54 · 4-round 설계 합의:

- 1d는 1w/3m/1y(source_daily/hourly_rates read-through, graph_v2.build_tab)와 **데이터 경로가
  다름**: raw 테이블(source_rates / bank_exchange_rates / investing_exchange_rates /
  market_index_rates realtime)을 10분 버킷으로 집계 + carry-forward. 그래서 별 모듈로 분리.
- 1d는 최다 트래픽 + 무거운 집계 → request-path read-through가 아니라 **10분 경계 precompute**:
  scheduler cron(*/10 +offset)이 `precompute_tether_1d()`로 전체 11 series를 만들어 Redis에
  SET. 요청(main.py)은 Redis만 read, miss 시에만 single-flight rebuild(`build_tether_1d_payload`).
- **완료된 10분봉만** 서버가 제공(closed-bucket). 진행 중(현재) 버킷은 iOS live-tail이 현재값으로
  담당 — 서버가 partial 봉을 주면 live-tail과 "now"가 이중 표현됨.
- carry-forward: source_rates/bank/investing/market_index는 change-only insert라 어떤 10분
  버킷엔 row가 없을 수 있음 → "데이터 없음"이 아니라 직전 close 유지(graph_cache._bucketize 내장
  + 윈도우 시작 이전 last-before 조회).
- 11 series (§4 line 54): upbit/bithumb/coinone/korbit/gopax(usdt-krw) + krx(usd-krw-futures)
  + investing.usd + kb.usd + hana.usd + dxy + dxy_futures.

TTL은 freshness 책임자가 아니라 **안전망**: cron이 매 10분 갱신하므로 정상 운영 중엔 항상 warm.
TTL은 cron 사망 시 무한 stale을 막는 용도(>10분 + 여유).
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy import text

from app.admin.graph_cache import (
    KST,
    _bucketize,
    _to_utc_datetime,
    build_dxy_graph_series,
    build_graph_series,
)
from app.database import get_db_context

logger = logging.getLogger("exchange_rate.graph_v2_intraday")

BUCKET_SECONDS = 600          # 10분봉
WINDOW_HOURS = 24             # 1d = 최근 24시간
CACHE_KEY_TETHER_1D = "graph_v2:tab:tether:1d"
# cron */10이 신선도 책임 → TTL은 안전망(>10분). cron 사망 시 ~20분 후 만료 → miss rebuild로 자연 복구.
CACHE_TTL_SECONDS = 1200

# ─────────────────────────────────────────────────────────────
# 1d 전용 series 메타데이터 (§4 line 54).
#   daily/hourly용 graph_v2.SERIES_REGISTRY와 분리 — 1d-only series(거래소 5개 개별, KB, DXY_futures)가
#   포함되고 데이터 경로(raw + 10min bucketize)도 달라서 섞지 않음.
#   kind: "source"(source_rates) / "fx"(bank·investing, build_graph_series 재사용) /
#         "market_index"(realtime; dxy는 build_dxy_graph_series 재사용, dxy_futures는 단일 source reader)
# ─────────────────────────────────────────────────────────────

_TETHER_1D_SERIES: List[dict] = [
    {"id": "upbit.usdt-krw",      "kind": "source", "source": "upbit",   "asset": "usdt-krw",        "axis_group": "krw",   "label": "업비트",          "unit": "KRW",   "decimals": 2},
    {"id": "bithumb.usdt-krw",    "kind": "source", "source": "bithumb", "asset": "usdt-krw",        "axis_group": "krw",   "label": "빗썸",            "unit": "KRW",   "decimals": 2},
    {"id": "coinone.usdt-krw",    "kind": "source", "source": "coinone", "asset": "usdt-krw",        "axis_group": "krw",   "label": "코인원",          "unit": "KRW",   "decimals": 2},
    {"id": "korbit.usdt-krw",     "kind": "source", "source": "korbit",  "asset": "usdt-krw",        "axis_group": "krw",   "label": "코빗",            "unit": "KRW",   "decimals": 2},
    {"id": "gopax.usdt-krw",      "kind": "source", "source": "gopax",   "asset": "usdt-krw",        "axis_group": "krw",   "label": "고팍스",          "unit": "KRW",   "decimals": 2},
    {"id": "krx.usd-krw-futures", "kind": "source", "source": "krx",     "asset": "usd-krw-futures", "axis_group": "krw",   "label": "KRX 미국달러선물", "unit": "KRW",   "decimals": 1},
    {"id": "investing.usd",       "kind": "fx",     "fx_source": "investing", "currency": "usd-krw", "axis_group": "krw",   "label": "인베스팅",        "unit": "KRW",   "decimals": 2},
    {"id": "kb.usd",              "kind": "fx",     "fx_source": "kb",        "currency": "usd-krw", "axis_group": "krw",   "label": "KB국민은행",      "unit": "KRW",   "decimals": 2},
    {"id": "hana.usd",            "kind": "fx",     "fx_source": "hana",      "currency": "usd-krw", "axis_group": "krw",   "label": "하나은행",        "unit": "KRW",   "decimals": 2},
    {"id": "dxy",                 "kind": "market_index", "instrument": "dxy",         "axis_group": "index", "label": "달러지수",      "unit": "INDEX", "decimals": 3},
    {"id": "dxy_futures",         "kind": "market_index", "instrument": "dxy_futures", "axis_group": "index", "label": "달러지수 선물", "unit": "INDEX", "decimals": 3},
]

_AXIS_KRW = {"unit": "KRW", "decimals": 2, "side": "left"}
_AXIS_INDEX = {"unit": "INDEX", "decimals": 3, "side": "right"}

# 첫 진입 default 체크 ON (§4 default_visible). 테더 1d: 대표 거래소(빗썸·업비트) + KRX + DXY.
TETHER_1D_DEFAULT_VISIBLE = ["bithumb.usdt-krw", "upbit.usdt-krw", "krx.usd-krw-futures", "dxy"]
TETHER_1D_ALL_SERIES = [s["id"] for s in _TETHER_1D_SERIES]


# ─────────────────────────────────────────────────────────────
# 시간/버킷 헬퍼
# ─────────────────────────────────────────────────────────────

def _bucket_align(ts: int) -> int:
    """10분 경계로 내림 정렬 (BUCKET_SECONDS=600은 KST 오프셋 3600의 약수라 UTC/KST 동일)."""
    return ts - (ts % BUCKET_SECONDS)


def _trim_in_progress(series: List[List[float]], in_progress_start_ts: int) -> List[List[float]]:
    """진행 중(현재) 10분 버킷 제외 — ts >= in_progress_start_ts drop (closed-bucket only).

    build_graph_series/build_dxy_graph_series/_build_*는 now까지 버킷을 만들어 진행 중 봉을
    포함하므로, 그 버킷(시작 ts == align(now))을 잘라 완료봉만 남긴다. 진행 중 현재값은 iOS live-tail.
    """
    return [p for p in series if p[0] < in_progress_start_ts]


# ─────────────────────────────────────────────────────────────
# Reader — source_rates (USDT 5 + KRX). build_graph_series의 source_rates 버전.
# ─────────────────────────────────────────────────────────────

def _fetch_source_last_before(db, source: str, asset: str, cutoff_ts: int) -> Optional[float]:
    """cutoff_ts 이전/동일 최신 source_rates 1건의 rate (carry-forward 초기값)."""
    cutoff = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        text(
            """
            SELECT rate FROM source_rates
            WHERE timestamp <= :cutoff AND source = :source AND asset = :asset
            ORDER BY timestamp DESC LIMIT 1
            """
        ),
        {"cutoff": cutoff, "source": source, "asset": asset},
    ).fetchone()
    if not row:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def _build_source_series_1d(source: str, asset: str, decimals: int, now_kst: datetime) -> List[List[float]]:
    """source_rates 24h → 10분 버킷 [ts, max, min, close] (carry-forward). now까지 포함(trim은 호출부)."""
    window_start_kst = now_kst - timedelta(hours=WINDOW_HOURS)
    bucket_start_ts = _bucket_align(int(window_start_kst.timestamp()))

    with get_db_context() as db:
        rows = db.execute(
            text(
                """
                SELECT timestamp, rate FROM source_rates
                WHERE timestamp >= :start AND timestamp <= :end
                  AND source = :source AND asset = :asset
                ORDER BY timestamp ASC
                """
            ),
            {
                "start": window_start_kst.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "end": now_kst.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "source": source,
                "asset": asset,
            },
        ).fetchall()
        prev_close = _fetch_source_last_before(db, source, asset, bucket_start_ts)

    points: List[Tuple[int, float]] = []
    for ts_val, rate in rows:
        try:
            points.append((int(_to_utc_datetime(ts_val).timestamp()), float(rate)))
        except Exception:
            continue

    series, _ = _bucketize(points, bucket_start_ts, int(now_kst.timestamp()), BUCKET_SECONDS, prev_close, decimals)
    return series


# ─────────────────────────────────────────────────────────────
# Reader — market_index_rates realtime (dxy_futures 단일 source).
#   dxy는 source priority(investing>yahoo) dedup이 필요해 build_dxy_graph_series 재사용,
#   dxy_futures는 단일 source(investing)라 단순 집계.
# ─────────────────────────────────────────────────────────────

def _build_market_index_series_1d(instrument: str, decimals: int, now_kst: datetime) -> List[List[float]]:
    """market_index_rates realtime(instrument) 24h → 10분 버킷 (carry-forward)."""
    window_start_kst = now_kst - timedelta(hours=WINDOW_HOURS)
    bucket_start_ts = _bucket_align(int(window_start_kst.timestamp()))
    cutoff = datetime.fromtimestamp(bucket_start_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with get_db_context() as db:
        rows = db.execute(
            text(
                """
                SELECT timestamp, rate FROM market_index_rates
                WHERE instrument = :instrument AND granularity = 'realtime'
                  AND timestamp >= :start AND timestamp <= :end
                ORDER BY timestamp ASC
                """
            ),
            {
                "instrument": instrument,
                "start": window_start_kst.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "end": now_kst.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            },
        ).fetchall()
        before = db.execute(
            text(
                """
                SELECT rate FROM market_index_rates
                WHERE instrument = :instrument AND granularity = 'realtime' AND timestamp <= :cutoff
                ORDER BY timestamp DESC LIMIT 1
                """
            ),
            {"instrument": instrument, "cutoff": cutoff},
        ).fetchone()

    prev_close = None
    if before:
        try:
            prev_close = float(before[0])
        except (TypeError, ValueError):
            prev_close = None

    points: List[Tuple[int, float]] = []
    for ts_val, rate in rows:
        try:
            points.append((int(_to_utc_datetime(ts_val).timestamp()), float(rate)))
        except Exception:
            continue

    series, _ = _bucketize(points, bucket_start_ts, int(now_kst.timestamp()), BUCKET_SECONDS, prev_close, decimals)
    return series


# ─────────────────────────────────────────────────────────────
# Series 조립 ([ts,max,min,close] → v2 data point + provenance)
# ─────────────────────────────────────────────────────────────

def _to_v2_points(series: List[List[float]], source: str) -> List[dict]:
    """[ts_epoch, max, min, close] → {ts(KST ISO), rate=close, high=max, low=min, source}.

    high/low: 단일 소스 음영 밴드용(client GraphV2Point.high/low) — graph_v2 _sdr_data_point과 동일 schema.
    """
    out = []
    for ts_epoch, mx, mn, close in series:
        dt = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).astimezone(KST)
        out.append({"ts": dt.isoformat(), "rate": close, "source": source, "high": mx, "low": mn})
    return out


def _provenance_1d(source: str, n_points: int) -> dict:
    """1d realtime series provenance — graph_v2 provenance와 동일 key shape(client 호환).

    1d는 realtime 10min 집계라 close_basis/contract 개념 없음(single, per_point 없음).
    insufficient_history: 데이터 0개면 true(신규 자산/일시 장애). 있으면 1d는 충분.
    """
    return {
        "actual_source": source,
        "fallback_source": None,
        "fallback_after_days": None,
        "history_policy": "realtime",
        "coverage_days": 1 if n_points else 0,
        "insufficient_history": n_points == 0,
        "close_basis_mode": "single",
        "per_point_metadata": [],
    }


def _build_series_1d(spec: dict, now_kst: datetime, in_progress_start_ts: int) -> dict:
    """spec 1개 → v2 series dict (graph_v2 _read_*_series와 동일 출력 shape)."""
    if spec["kind"] == "source":
        raw = _build_source_series_1d(spec["source"], spec["asset"], spec["decimals"], now_kst)
        src_label = spec["source"]
    elif spec["kind"] == "fx":
        # now_kst 주입 — 11 series가 동일 now 기준 버킷 경계를 갖도록(경계 race 제거, codex finding)
        raw, _ = build_graph_series(spec["fx_source"], spec["currency"], now_kst)
        src_label = spec["fx_source"]
    elif spec["kind"] == "market_index":
        if spec["instrument"] == "dxy":
            raw, _ = build_dxy_graph_series(now_kst)   # investing>yahoo dedup 내장 + now_kst 일관
        else:
            raw = _build_market_index_series_1d(spec["instrument"], spec["decimals"], now_kst)
        src_label = spec["instrument"]
    else:
        raise ValueError(f"미지원 1d series kind: {spec['kind']!r} (series={spec['id']})")

    raw = _trim_in_progress(raw, in_progress_start_ts)   # 완료봉만(closed-bucket)
    points = _to_v2_points(raw, src_label)
    return {
        "id": spec["id"],
        "label": spec["label"],
        "axis_group": spec["axis_group"],
        "unit": spec["unit"],
        "decimals": spec["decimals"],
        "data": points,
        "provenance": _provenance_1d(src_label, len(points)),
    }


def build_tether_1d_payload() -> dict:
    """테더 1d 전체 11 series 조립 → /api/v2/graph/tab 응답 shape (graph_v2.build_tab과 동일).

    closed-bucket only(진행 중 10분봉 제외). 동기(get_db_context) — precompute cron + endpoint
    miss-rebuild(to_thread)에서 호출.
    """
    now_kst = datetime.now(KST)
    in_progress_start_ts = _bucket_align(int(now_kst.timestamp()))   # 진행 중 버킷 시작 = 잘라낼 경계
    window_start_kst = now_kst - timedelta(hours=WINDOW_HOURS)

    series_out = [_build_series_1d(spec, now_kst, in_progress_start_ts) for spec in _TETHER_1D_SERIES]

    return {
        "tab": "tether",
        "period": "1d",
        "series": series_out,
        "metadata": {
            "fetched_at": now_kst.isoformat(),
            "bucket_size": "10min",
            "range": {
                "start": window_start_kst.date().isoformat(),
                "end": now_kst.date().isoformat(),
            },
        },
    }


def precompute_tether_1d() -> None:
    """scheduler cron(*/10 +offset)용 동기 job — build → Redis SET(sync). 실패는 격리(로그만).

    refresh_graph_cache와 동일 sync redis 패턴. 요청 경로(main.py)는 이 키를 read만 한다.
    """
    import redis as sync_redis

    from app.config import REDIS_PASSWORD, REDIS_URL

    try:
        payload = build_tether_1d_payload()
    except Exception:
        logger.exception("graph_v2 tether 1d build 실패 (precompute)")
        return

    redis_client = None
    try:
        redis_client = sync_redis.from_url(REDIS_URL, password=REDIS_PASSWORD or None, decode_responses=True)
        redis_client.ping()
        redis_client.setex(CACHE_KEY_TETHER_1D, CACHE_TTL_SECONDS, json.dumps(payload))
        logger.info(
            "✅ graph_v2 tether 1d precompute",
            extra={"series_count": len(payload["series"]),
                   "point_counts": {s["id"]: len(s["data"]) for s in payload["series"]}},
        )
    except Exception:
        logger.exception("graph_v2 tether 1d Redis write 실패 (precompute)")
    finally:
        if redis_client is not None:
            try:
                redis_client.close()
            except Exception:
                pass
