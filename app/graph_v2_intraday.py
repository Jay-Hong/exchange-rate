"""
Intraday 1d graph v2 — 10min closed-bucket precompute cache (builder + payload, per-tab).

ADR-035 / GRAPH_API_V2_CONTRACT §3:49-54 · §4. 테더 1d로 4-round 설계 합의 후 FX 3탭(usd/jpy/eur)로
per-tab 일반화 (Slice A — "1d 모든 은행" 계약):

- 1d는 1w/3m/1y(source_daily/hourly_rates read-through, graph_v2.build_tab)와 **데이터 경로가
  다름**: raw 테이블(source_rates / bank_exchange_rates / investing_exchange_rates /
  market_index_rates realtime)을 10분 버킷으로 집계 + carry-forward. 그래서 별 모듈로 분리.
- 1d는 최다 트래픽 + 무거운 집계 → request-path read-through가 아니라 **10분 경계 precompute**:
  scheduler cron(*/10 +offset)이 `precompute_intraday_1d()`로 INTRADAY_TABS를 순회하며 탭별 전체
  series를 만들어 Redis SET. 요청(main.py)은 Redis만 read, miss/stale-boundary 시에만
  single-flight rebuild(`build_tab_1d_payload`).
- **완료된 10분봉만** 서버가 제공(closed-bucket). 진행 중(현재) 버킷은 iOS live-tail이 현재값으로
  담당 — 서버가 partial 봉을 주면 live-tail과 "now"가 이중 표현됨. (+additive in_progress seed)
- carry-forward: source_rates/bank/investing/market_index는 change-only insert라 어떤 10분
  버킷엔 row가 없을 수 있음 → "데이터 없음"이 아니라 직전 close 유지(graph_cache._bucketize 내장
  + 윈도우 시작 이전 last-before 조회).
- per-tab series (TAB_1D_SERIES): tether 11 (§4 line 54 — 거래소 5 + KRX + investing/kb/hana USD
  + dxy + dxy_futures) / usd 10 (8 banks[Citi 제외] + investing + dxy) / jpy·eur 9 (8 banks +
  investing — DXY 미노출 §9:521).

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


def cache_key_1d(tab: str) -> str:
    """탭별 1d closed payload 캐시 키."""
    return f"graph_v2:tab:{tab}:1d"


def cache_key_1d_in_progress(tab: str) -> str:
    """탭별 진행 중(현재) 10분봉 seed 캐시 키 — closed와 별개 short-TTL cache-aside."""
    return f"graph_v2:tab:{tab}:1d:in_progress"


# 테더 키 별칭 (per-tab helper 도입 전 상수 — 문자열 동일, 기존 참조 호환)
CACHE_KEY_TETHER_1D = cache_key_1d("tether")
CACHE_KEY_TETHER_1D_IN_PROGRESS = cache_key_1d_in_progress("tether")
# cron */10이 신선도 책임 → TTL은 안전망(>10분). cron 사망 시 ~20분 후 만료 → miss rebuild로 자연 복구.
CACHE_TTL_SECONDS = 1200
# in_progress seed는 실시간성 필요(현재 봉) → 짧은 TTL. serve 시 cache-aside(miss면 현재 봉만 재계산).
# fetch가 드물어(cold-open/resync) 계산 빈도 = min(TTL, serve 빈도). 트래픽 급증 시 별도 cron으로 전환 여지.
IN_PROGRESS_TTL_SECONDS = 15

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

# ─────────────────────────────────────────────────────────────
# FX 탭(usd/jpy/eur) 1d series — §3:51-53 + §4:82-83.
#   USD: 8 banks(Citi 제외, ADR-033 Decision 4 — 수집은 유지) + investing + DXY(현물만 —
#        dxy_futures는 테더 1d 전용, ADR-033 Decision 8).
#   JPY/EUR: 8 banks + investing — DXY 계열 미노출(§9:521, 단일 KRW axis).
#   은행 순서 = 앱 은행순서설정(Bank.displayCases) 순서 — investing 먼저 + kb/hana/shinhan/woori/
#   ibk/nh/sc/bs + (usd만) dxy. 사용자 지정(2026-07-03): 토글/차트/은행별환율 섹션 순서 통일.
# ─────────────────────────────────────────────────────────────

_FX_1D_BANKS = [
    ("kb", "KB국민은행"), ("hana", "하나은행"), ("shinhan", "신한은행"), ("woori", "우리은행"),
    ("ibk", "IBK기업은행"), ("nh", "NH농협은행"), ("sc", "SC제일은행"), ("bs", "부산은행"),
]


def _fx_1d_series(currency: str) -> List[dict]:
    """FX 탭 1d spec 목록 — investing + 8 banks(Citi 제외). currency 예: 'usd-krw'.
    reader는 kind='fx'로 build_graph_series 재사용(전 은행 일반화 확인 — bank allowlist 없음)."""
    short = currency.split("-")[0]   # "usd-krw" → "usd" (series id suffix)
    specs = [
        {"id": f"investing.{short}", "kind": "fx", "fx_source": "investing", "currency": currency,
         "axis_group": "krw", "label": "인베스팅", "unit": "KRW", "decimals": 2},
    ]
    for bank, label in _FX_1D_BANKS:
        specs.append(
            {"id": f"{bank}.{short}", "kind": "fx", "fx_source": bank, "currency": currency,
             "axis_group": "krw", "label": label, "unit": "KRW", "decimals": 2}
        )
    return specs


_DXY_1D_SPEC = {"id": "dxy", "kind": "market_index", "instrument": "dxy",
                "axis_group": "index", "label": "달러지수", "unit": "INDEX", "decimals": 3}

# per-tab 1d series 정의 — 테더 11 / usd 10 / jpy 9 / eur 9 (계약 §3 매트릭스).
TAB_1D_SERIES: dict = {
    "tether": _TETHER_1D_SERIES,
    "usd": _fx_1d_series("usd-krw") + [dict(_DXY_1D_SPEC)],
    "jpy": _fx_1d_series("jpy-krw"),
    "eur": _fx_1d_series("eur-krw"),
}
TAB_1D_ALL_SERIES = {tab: [s["id"] for s in specs] for tab, specs in TAB_1D_SERIES.items()}
# 첫 진입 default 체크 ON (§4 default_visible_series). FX 3탭은 사용자 지정(2026-07-03):
# 인베스팅 + 하나은행만 (usd의 dxy·kb도 기본 OFF — 소스가 많아 최소 2개로 시작, 나머지는 유저 토글).
# 유저 토글 상태는 UserDefaults(graphv2_visible_<tab>)에 persist — 재시작해도 유지(user-off도 기억).
TAB_1D_DEFAULT_VISIBLE = {
    "tether": ["bithumb.usdt-krw", "upbit.usdt-krw", "krx.usd-krw-futures", "dxy"],
    "usd": ["investing.usd", "hana.usd"],
    "jpy": ["investing.jpy", "hana.jpy"],
    "eur": ["investing.eur", "hana.eur"],
}
# 1d(intraday) 지원 탭 — main.py endpoint 분기/scheduler precompute 루프 기준.
INTRADAY_TABS = tuple(TAB_1D_SERIES)

# 테더 별칭 (per-tab dict 도입 전 export — 기존 참조 호환)
TETHER_1D_DEFAULT_VISIBLE = TAB_1D_DEFAULT_VISIBLE["tether"]
TETHER_1D_ALL_SERIES = TAB_1D_ALL_SERIES["tether"]


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


def _build_raw_1d(spec: dict, now_kst: datetime) -> Tuple[List[List[float]], str]:
    """spec → (raw series [ts,max,min,close] — 진행 중 봉 포함, src_label). reader dispatch 공용.
    closed payload(_build_series_1d, trim 후)와 in_progress seed(build_tab_1d_in_progress, 현재 봉만)가
    같은 reader를 재사용해 query drift 0. now_kst 주입으로 탭 전체 series가 동일 버킷 경계(경계 race 제거)."""
    if spec["kind"] == "source":
        return _build_source_series_1d(spec["source"], spec["asset"], spec["decimals"], now_kst), spec["source"]
    if spec["kind"] == "fx":
        raw, _ = build_graph_series(spec["fx_source"], spec["currency"], now_kst)
        return raw, spec["fx_source"]
    if spec["kind"] == "market_index":
        if spec["instrument"] == "dxy":
            raw, _ = build_dxy_graph_series(now_kst)   # investing>yahoo dedup 내장 + now_kst 일관
        else:
            raw = _build_market_index_series_1d(spec["instrument"], spec["decimals"], now_kst)
        return raw, spec["instrument"]
    raise ValueError(f"미지원 1d series kind: {spec['kind']!r} (series={spec['id']})")


def _build_series_1d(spec: dict, now_kst: datetime, in_progress_start_ts: int) -> dict:
    """spec 1개 → v2 series dict (graph_v2 _read_*_series와 동일 출력 shape). closed-bucket only."""
    raw, src_label = _build_raw_1d(spec, now_kst)
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


def build_tab_1d_payload(tab: str) -> dict:
    """탭 1d 전체 series 조립(테더 11/usd 10/jpy·eur 9) → /api/v2/graph/tab 응답 shape.

    closed-bucket only(진행 중 10분봉 제외). 동기(get_db_context) — precompute cron + endpoint
    miss-rebuild(to_thread)에서 호출. tab은 INTRADAY_TABS 검증 후 진입 가정(KeyError=호출부 버그).
    """
    now_kst = datetime.now(KST)
    in_progress_start_ts = _bucket_align(int(now_kst.timestamp()))   # 진행 중 버킷 시작 = 잘라낼 경계
    window_start_kst = now_kst - timedelta(hours=WINDOW_HOURS)

    series_out = [_build_series_1d(spec, now_kst, in_progress_start_ts) for spec in TAB_1D_SERIES[tab]]

    return {
        "tab": tab,
        "period": "1d",
        "series": series_out,
        # 이 payload가 build된 진행중 버킷 경계(epoch, 잘라낸 지점). serve(_get_intraday_1d_closed)가
        # "캐시 경계 < 현재 경계"면 경계 통과 후 precompute(:12) 전 창이라 방금 닫힌 봉이 아직 캐시에
        # 없다고 판단 → 온디맨드 rebuild(cold-open ~12초 gap 제거). 클라(GraphV2TabResponse)는 미인지 필드
        # 무시(전방호환). `_` prefix = 내부 메타(클라 계약 아님).
        "_in_progress_start_ts": in_progress_start_ts,
        "metadata": {
            "fetched_at": now_kst.isoformat(),
            "bucket_size": "10min",
            "range": {
                "start": window_start_kst.date().isoformat(),
                "end": now_kst.date().isoformat(),
            },
        },
    }


def build_tab_1d_in_progress(tab: str, now_kst: Optional[datetime] = None) -> dict:
    """탭의 진행 중(현재) 10분봉만 per-series 추출 — cold-open/resync seed용. closed payload와 별개 short-TTL.

    readers는 full 24h를 만들지만 in_progress_start_ts(align(now)) 버킷 1개만 취함(readers 재사용 =
    query drift 0, carry-forward 포함해 현재 봉이 empty여도 직전 close로 seed). 현재 봉이 아예 없으면
    (데이터 0) 해당 series 생략 → 클라는 그 소스에 seed 없이 client-only 누적 fallback.
    반환: { "<series.id>": {bucket_start(KST ISO), high, low, close, sampled_at(KST ISO)} }.
    now_kst: 테스트 결정성용 주입(기본 datetime.now(KST)). prod endpoint는 인자 없이 호출."""
    now_kst = now_kst or datetime.now(KST)
    ip_start = _bucket_align(int(now_kst.timestamp()))
    ip_iso = datetime.fromtimestamp(ip_start, tz=timezone.utc).astimezone(KST).isoformat()
    sampled_at = now_kst.isoformat()
    out: dict = {}
    for spec in TAB_1D_SERIES[tab]:
        raw, _ = _build_raw_1d(spec, now_kst)
        bucket = next((b for b in raw if int(b[0]) == ip_start), None)
        if bucket is None:
            continue   # 현재 봉 데이터 없음 → seed 생략(client-only fallback)
        _ts, mx, mn, close = bucket
        out[spec["id"]] = {
            "bucket_start": ip_iso,
            "high": mx,
            "low": mn,
            "close": close,
            "sampled_at": sampled_at,
        }
    return out


def precompute_intraday_1d() -> None:
    """scheduler cron(*/10 +offset)용 동기 job — INTRADAY_TABS 순회하며 탭별 build → Redis SET(sync).

    refresh_graph_cache와 동일 sync redis 패턴. 요청 경로(main.py)는 이 키를 read만 한다.
    탭별 build+SET 즉시(전체 build 후 일괄 SET 아님) — 선순위 탭(테더)이 먼저 fresh + 실패는 per-tab
    격리(한 탭 reader 장애가 다른 탭 갱신을 막지 않음). Redis 연결 실패만 전체 스킵.
    """
    import redis as sync_redis

    from app.config import REDIS_PASSWORD, REDIS_URL

    redis_client = None
    try:
        redis_client = sync_redis.from_url(REDIS_URL, password=REDIS_PASSWORD or None, decode_responses=True)
        redis_client.ping()
    except Exception:
        logger.exception("graph_v2 intraday 1d Redis 연결 실패 (precompute)")
        if redis_client is not None:
            try:
                redis_client.close()
            except Exception:
                pass
        return

    series_counts: dict = {}
    try:
        for tab in INTRADAY_TABS:
            try:
                payload = build_tab_1d_payload(tab)
                redis_client.setex(cache_key_1d(tab), CACHE_TTL_SECONDS, json.dumps(payload))
                series_counts[tab] = len(payload["series"])
            except Exception:
                logger.exception("graph_v2 %s 1d precompute 실패 — 탭 격리", tab)
        logger.info("✅ graph_v2 intraday 1d precompute", extra={"series_counts": series_counts})
    finally:
        try:
            redis_client.close()
        except Exception:
            pass
