"""Graph API v2 — catalog + tab graph (ADR-035 Phase 2e MVP).

설계: [GRAPH_API_V2_CONTRACT.md](../GRAPH_API_V2_CONTRACT.md) §4/§5/§6/§9/§10.

MVP 범위 (Codex/Claude 수렴 — 이후 1d intraday로 확장):
  - **supported period = 3m, 1y, 1w** (전역 MVP_PERIODS). 1d는 별도 intraday 경로(graph_v2_intraday,
    per-tab 10min precompute — 테더+usd/jpy/eur 4탭, GRAPH §13 item 5-6)로 endpoint에서 직접 serve.
  - 3m/1y hot path = source_daily_rates 단일 조회 / 1w = source_hourly_rates (period→granularity 분기, 외부/raw read 제거).
  - DXY는 source_daily/hourly_rates 아님 → market_index_rates(.daily/.hourly) 별도 reader (kind="market_index").
  - Hana 1w gap = 실관측 bucket 그대로 반환 (carry-forward/step render 미적용 — write 정책 일치, frontend render).

문서=full target / 구현=MVP subset (비강제):
  - GRAPH §4 catalog matrix는 1d(은행 8개) 포함한 제품 full target.
  - 본 모듈 상수는 **3m/1y/1w 인코딩** — 1d 구성은 graph_v2_intraday.TAB_1D_*가 소유(catalog에서 lookup).

엔드포인트(main.py thin wiring):
  - GET /api/v2/graph/catalog → build_catalog()
  - GET /api/v2/graph/tab?tab=&period= → build_tab(db, tab, period) (period∈{3m,1y,1w};
    period=1d는 main.py가 graph_v2_intraday serve 경로로 분기)

v1 (/api/graph/{currency})는 변경 0 (legacy 공존, §12).
"""

# 표준 라이브러리
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# MVP 지원 period (1d=v1 realtime / 1w=source_hourly_rates hourly / 3m·1y=source_daily_rates daily)
MVP_PERIODS = ("3m", "1y", "1w")
PERIOD_DAYS = {"3m": 90, "1y": 365, "1w": 7}
# period → granularity (3m/1y=daily reader, 1w=hourly reader). bucket_size = data point 간격.
PERIOD_GRANULARITY = {"3m": "daily", "1y": "daily", "1w": "hourly"}
_BUCKET_SIZE = {"daily": "1d", "hourly": "1h"}

# insufficient_history 기준 (§6 "요청 period를 채울 수 없으면 true"):
# 빈 series 또는 첫 데이터가 period 시작보다 tolerance(일) 초과 늦으면 period 미충족(신규 자산/coverage 부족).
# granularity별 tolerance (Codex finding — 단일 7d면 1w window(7일)에서 partial coverage[마지막 1~2일만]도 sufficient 오판):
#   daily(3m/1y) = 7일 (주말/휴일 + source 소폭 gap 허용, 90/365일 window에서 작은 비율).
#   hourly(1w)  = 2일 (주말 시작 Sat/Sun→Mon FX 허용 + partial은 insufficient로 잡음.
#                 3일 연휴 시작은 드문 false-positive지만 soft flag라 under-flagging보다 안전.)
_COVERAGE_TOLERANCE_DAYS = {"daily": 7, "hourly": 2}

# DXY(market_index_rates) source dedup 우선순위 — crud.py `_dxy_query_single`과 동일 (investing>cnbc>else).
_DXY_SOURCE_PRIORITY = {"investing": 0, "cnbc": 1}

CATALOG_VERSION = "2026-05-27"


def _is_insufficient_history(first_date, start_date, tolerance_days) -> bool:
    """series가 요청 period 시작을 못 채우면 true (빈 series 또는 첫 데이터가 start+tolerance 초과).

    tolerance_days = granularity별 (_COVERAGE_TOLERANCE_DAYS[granularity]) — daily 7 / hourly 2.
    """
    if first_date is None:
        return True
    return (first_date - start_date).days > tolerance_days

# ─────────────────────────────────────────────────────────────
# Series registry — id ↔ DB key 분리 (Codex 보탬)
#   kind="source_daily_rates": source+asset (get_range read 키)
#   kind="market_index": instrument (granularity는 period 파생 — daily/hourly)
#   id는 client 표시용 (FX는 short "hana.usd", KRX/Bithumb은 full asset)
# ─────────────────────────────────────────────────────────────

SERIES_REGISTRY = {
    # FX — Investing (내부 investing_exchange_rates rollup, single close_basis)
    "investing.usd": {"kind": "source_daily_rates", "source": "investing", "asset": "usd-krw",
                      "axis_group": "krw", "label": "인베스팅", "unit": "KRW", "decimals": 2,
                      "history_policy": "rollup_based"},
    "investing.jpy": {"kind": "source_daily_rates", "source": "investing", "asset": "jpy-krw",
                      "axis_group": "krw", "label": "인베스팅", "unit": "KRW", "decimals": 2,
                      "history_policy": "rollup_based"},
    "investing.eur": {"kind": "source_daily_rates", "source": "investing", "asset": "eur-krw",
                      "axis_group": "krw", "label": "인베스팅", "unit": "KRW", "decimals": 2,
                      "history_policy": "rollup_based"},
    # FX — Hana (observed_eod + official_historical_backfill 2-source → mixed 가능)
    "hana.usd": {"kind": "source_daily_rates", "source": "hana", "asset": "usd-krw",
                 "axis_group": "krw", "label": "하나은행", "unit": "KRW", "decimals": 2,
                 "history_policy": "external_historical"},
    "hana.jpy": {"kind": "source_daily_rates", "source": "hana", "asset": "jpy-krw",
                 "axis_group": "krw", "label": "하나은행", "unit": "KRW", "decimals": 2,
                 "history_policy": "external_historical"},
    "hana.eur": {"kind": "source_daily_rates", "source": "hana", "asset": "eur-krw",
                 "axis_group": "krw", "label": "하나은행", "unit": "KRW", "decimals": 2,
                 "history_policy": "external_historical"},
    # Tether group — Bithumb (candlestick external_historical, single close_basis)
    "bithumb.usdt-krw": {"kind": "source_daily_rates", "source": "bithumb", "asset": "usdt-krw",
                         "axis_group": "krw", "label": "빗썸", "unit": "KRW", "decimals": 2,
                         "history_policy": "external_historical"},
    # Tether group — KRX 미국달러선물 (KIS contract chain, per-point contract_code)
    "krx.usd-krw-futures": {"kind": "source_daily_rates", "source": "krx", "asset": "usd-krw-futures",
                            "axis_group": "krw", "label": "KRX 미국달러선물", "unit": "KRW", "decimals": 1,
                            "history_policy": "external_historical"},
    # DXY — market_index_rates .daily(3m/1y)/.hourly(1w) (source_daily_rates 아님). granularity는 period 파생(reader) — entry에 없음.
    "dxy": {"kind": "market_index", "instrument": "dxy",
            "axis_group": "index", "label": "달러지수", "unit": "INDEX", "decimals": 3,
            "history_policy": "rollup_based"},
}

# axis_group 정의 (§4/§9 — DXY 노출 탭만 index group 추가)
_AXIS_KRW = {"unit": "KRW", "decimals": 2, "side": "left"}
_AXIS_INDEX = {"unit": "INDEX", "decimals": 3, "side": "right"}

# ─────────────────────────────────────────────────────────────
# Tab × period 구성 (MVP 3m/1y subset — §3/§9 근거)
#   usd/jpy/eur: FX 탭 / tether: USDT group
#   §9: JPY/EUR는 DXY 미노출 / KB USD 등 은행은 1d only(3m/1y canonical은 Hana만)
# ─────────────────────────────────────────────────────────────

_TAB_SERIES = {
    # usd의 krx: ADR-038 D4 ② (2026-07-08) — 달러 탭 전 기간 KRX 편입 (default OFF,
    # G2/G3 게이트는 _effective_tab_series의 krx. prefix 필터가 자동 적용).
    "usd": ["investing.usd", "krx.usd-krw-futures", "hana.usd", "dxy"],
    "jpy": ["investing.jpy", "hana.jpy"],
    "eur": ["investing.eur", "hana.eur"],
    "tether": ["bithumb.usdt-krw", "krx.usd-krw-futures", "investing.usd", "hana.usd", "dxy"],
}

_TAB_LABEL = {"usd": "달러", "jpy": "엔", "eur": "유로", "tether": "테더"}

# 첫 진입 default 체크 ON (§4 default_visible_series) — DXY + 대표 1-2개
_TAB_DEFAULT_VISIBLE = {
    "usd": ["investing.usd", "hana.usd", "dxy"],
    "jpy": ["investing.jpy", "hana.jpy"],
    "eur": ["investing.eur", "hana.eur"],
    "tether": ["bithumb.usdt-krw", "krx.usd-krw-futures", "dxy"],
}


def _krx_distribution_open() -> bool:
    """ADR-038 G2/G3 — client-facing KRX 배포 허용 여부 (graph_v2_intraday와 동일 규칙)."""
    from app import config
    return config.KRX_FUTURES_ENABLED and config.KRX_CLIENT_DISTRIBUTION_ENABLED


def _effective_tab_series(tab: str) -> list:
    """탭 series id 목록 (3m/1y/1w 공유) — G2/G3 닫히면 krx 계열 제외 (ADR-038)."""
    ids = _TAB_SERIES[tab]
    if _krx_distribution_open():
        return list(ids)
    return [sid for sid in ids if not sid.startswith("krx.")]


def _effective_default_visible(tab: str) -> list:
    """default visible 목록 — G2/G3 반영 (catalog가 그대로 복사하므로 함께 필터)."""
    if _krx_distribution_open():
        return list(_TAB_DEFAULT_VISIBLE[tab])
    return [sid for sid in _TAB_DEFAULT_VISIBLE[tab] if not sid.startswith("krx.")]


def _tab_axis_groups(tab: str) -> dict:
    """탭의 axis_groups — DXY 포함 탭만 index group 추가 (§9)."""
    has_index = any(SERIES_REGISTRY[s]["axis_group"] == "index" for s in _TAB_SERIES[tab])
    groups = {"krw": dict(_AXIS_KRW)}
    if has_index:
        groups["index"] = dict(_AXIS_INDEX)
    return groups


# ─────────────────────────────────────────────────────────────
# Period 검증 / range
# ─────────────────────────────────────────────────────────────

def is_supported_period(period: str) -> bool:
    """v2 MVP 지원 period (3m, 1y, 1w)."""
    return period in MVP_PERIODS


def period_range(period: str, today_kst: date) -> tuple[date, date]:
    """period → (start_date, end_date) KST. end=today(최신 available까지), start=today-N일.

    inclusive [today-N, today] = N+1 calendar day (1w=today-7~today ≈ 8일, "정확히 168h" 아님).
    3m/1y와 동일 uniform 패턴 — 의도된 결정 (Codex caveat). "정확 168h rolling"은 전 period semantics 변경 필요.
    """
    days = PERIOD_DAYS[period]
    return today_kst - timedelta(days=days), today_kst


# ─────────────────────────────────────────────────────────────
# Provenance 조립 (source_daily_rates series — single/mixed 동적)
# ─────────────────────────────────────────────────────────────

def _assemble_sdr_provenance(entry: dict, rows: list, start_date, bucket_date, tolerance_days) -> dict:
    """rows에서 series-level provenance 동적 조립 (§6).

    - close_basis_mode: distinct close_basis 1개 → single / 2개+ → mixed (Hana만 예상)
    - default_close_basis: 가장 최근(rows[-1]) close_basis (canonical/going-forward 기준)
    - per_point_metadata: mixed면 [close_basis, source_method] / KRX(contract_code 존재)면 [contract_code]
    - bucket_date: row → KST date 추출 (daily=date_kst / hourly=bucket_ts_kst.date()) — coverage/insufficient 판정
    """
    first_d = bucket_date(rows[0]) if rows else None
    last_d = bucket_date(rows[-1]) if rows else None
    prov = {
        "actual_source": entry["source"],
        "fallback_source": None,          # Amendment 후 external_historical/rollup series는 null
        "fallback_after_days": None,
        "history_policy": entry["history_policy"],
        "coverage_days": (last_d - first_d).days + 1 if rows else 0,
        "insufficient_history": _is_insufficient_history(first_d, start_date, tolerance_days),
    }
    if not rows:
        prov["close_basis_mode"] = "single"
        prov["per_point_metadata"] = []
        return prov

    distinct_cb = {r.close_basis for r in rows}
    distinct_sm = {r.source_method for r in rows}
    has_contract = any(r.contract_code for r in rows)

    prov["close_basis_mode"] = "mixed" if len(distinct_cb) > 1 else "single"
    prov["default_close_basis"] = rows[-1].close_basis        # 최신 = going-forward canonical
    prov["default_source_method"] = rows[-1].source_method
    if prov["close_basis_mode"] == "mixed":
        prov["close_basis_values"] = sorted(distinct_cb)
        prov["source_method_values"] = sorted(distinct_sm)

    per_point = []
    if prov["close_basis_mode"] == "mixed":
        per_point += ["close_basis", "source_method"]
    if has_contract:
        per_point.append("contract_code")
    prov["per_point_metadata"] = per_point
    return prov


def _sdr_data_point(row, per_point_metadata: list, bucket_ts) -> dict:
    """SourceDailyRate/SourceHourlyRate row → v2 data point. per_point_metadata에 따라 조건부 필드 포함.

    ts = bucket_ts(row) — daily=date_kst 00:00:00+09:00 / hourly=bucket_ts_kst 시각+09:00 (§5).
    """
    point = {"ts": bucket_ts(row), "rate": float(row.close), "source": row.source}
    # high/low: 단일 소스 음영 밴드용 (client GraphV2Point.high/low). close_only row는 high=low=close.
    # additive — 기존 client는 무시. None이면 생략(client nil → 밴드 미표시).
    if row.high is not None:
        point["high"] = float(row.high)
    if row.low is not None:
        point["low"] = float(row.low)
    if "close_basis" in per_point_metadata:
        point["close_basis"] = row.close_basis
        point["source_method"] = row.source_method
    if "contract_code" in per_point_metadata:
        point["contract_code"] = row.contract_code
    return point


# ─────────────────────────────────────────────────────────────
# Reader — source_daily_rates series
# ─────────────────────────────────────────────────────────────

def _read_sdr_series(db, series_id: str, entry: dict, start: date, end: date, granularity: str) -> dict:
    """source_daily/hourly_rates series 1개 → {id, label, axis_group, unit, decimals, data, provenance}.

    granularity=daily(3m/1y): source_daily_rates.get_range(date) + date_kst bucket.
    granularity=hourly(1w): source_hourly_rates.get_range(datetime) + bucket_ts_kst(시 단위) bucket.
    """
    if granularity == "hourly":
        from app.source_hourly_rates import get_range
        # bucket_ts_kst는 KST naive — KST [start 00:00, end 23:59:59] inclusive 경계로 조회
        start_ts = datetime.combine(start, time(0, 0))
        end_ts = datetime.combine(end, time(23, 59, 59))
        rows = get_range(db, entry["source"], entry["asset"], start_ts, end_ts)
        def _bucket_date(r): return r.bucket_ts_kst.date()
        def _bucket_ts(r): return r.bucket_ts_kst.replace(tzinfo=KST).isoformat()
    else:
        from app.source_daily_rates import get_range
        rows = get_range(db, entry["source"], entry["asset"], start, end)
        def _bucket_date(r): return r.date_kst
        def _bucket_ts(r): return datetime.combine(r.date_kst, time(0, 0), tzinfo=KST).isoformat()

    provenance = _assemble_sdr_provenance(entry, rows, start, _bucket_date, _COVERAGE_TOLERANCE_DAYS[granularity])
    per_point = provenance["per_point_metadata"]
    data = [_sdr_data_point(r, per_point, _bucket_ts) for r in rows]
    return {
        "id": series_id,
        "label": entry["label"],
        "axis_group": entry["axis_group"],
        "unit": entry["unit"],
        "decimals": entry["decimals"],
        "data": data,
        "provenance": provenance,
    }


# ─────────────────────────────────────────────────────────────
# Reader — DXY (market_index_rates.daily)
# ─────────────────────────────────────────────────────────────

def _read_market_index_series(db, series_id: str, entry: dict, start: date, end: date, granularity: str) -> dict:
    """market_index_rates series (DXY) → {id, ..., data, provenance}.

    granularity=daily(3m/1y): timestamp(UTC)→KST date bucket / granularity=hourly(1w): KST 시각(시 floor) bucket.
    같은 bucket 다수 source면 source priority(investing>cnbc>else, crud.py `_dxy_query_single` 일치) → 동순위는 최신 timestamp.
    """
    from app.models import MarketIndexRate

    # KST [start 00:00, (end+1) 00:00) → UTC naive 경계로 row 조회 (timestamp는 naive UTC)
    start_utc = datetime.combine(start, time(0, 0), tzinfo=KST).astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = datetime.combine(end + timedelta(days=1), time(0, 0), tzinfo=KST).astimezone(timezone.utc).replace(tzinfo=None)
    rows = (
        db.query(MarketIndexRate)
        .filter(
            MarketIndexRate.instrument == entry["instrument"],
            MarketIndexRate.granularity == granularity,
            MarketIndexRate.timestamp >= start_utc,
            MarketIndexRate.timestamp < end_utc,
        )
        .order_by(MarketIndexRate.timestamp.asc())
        .all()
    )
    # bucket key — daily=KST date / hourly=KST 시각(시 floor). 같은 bucket 다수 source면 priority dedup.
    # (단순 last-row dedup은 yahoo가 investing보다 늦으면 yahoo 선택 → v1 source priority 회귀)
    def _rank(r):
        return (_DXY_SOURCE_PRIORITY.get(r.source, 2), -r.timestamp.timestamp())

    def _kst(r):
        return r.timestamp.replace(tzinfo=timezone.utc).astimezone(KST)

    if granularity == "hourly":
        def _bucket_key(r): return _kst(r).replace(minute=0, second=0, microsecond=0)
        def _bucket_ts(k): return k.isoformat()
        def _bucket_date(k): return k.date()
    else:
        def _bucket_key(r): return _kst(r).date()
        def _bucket_ts(k): return datetime.combine(k, time(0, 0), tzinfo=KST).isoformat()
        def _bucket_date(k): return k

    by_bucket: dict = {}
    for r in rows:
        k = _bucket_key(r)
        cur = by_bucket.get(k)
        if cur is None or _rank(r) < _rank(cur):
            by_bucket[k] = r
    data = [
        {"ts": _bucket_ts(k), "rate": float(by_bucket[k].rate), "source": by_bucket[k].source}
        for k in sorted(by_bucket)
    ]
    bucket_dates = [_bucket_date(k) for k in by_bucket]
    provenance = {
        "actual_source": "market_index",
        "fallback_source": None,
        "fallback_after_days": None,
        "history_policy": entry["history_policy"],
        "coverage_days": (max(bucket_dates) - min(bucket_dates)).days + 1 if bucket_dates else 0,
        "insufficient_history": _is_insufficient_history(min(bucket_dates) if bucket_dates else None, start, _COVERAGE_TOLERANCE_DAYS[granularity]),
        "close_basis_mode": "single",   # market_index는 close_basis 개념 없음 — single 고정
        "per_point_metadata": [],
    }
    return {
        "id": series_id,
        "label": entry["label"],
        "axis_group": entry["axis_group"],
        "unit": entry["unit"],
        "decimals": entry["decimals"],
        "data": data,
        "provenance": provenance,
    }


# ─────────────────────────────────────────────────────────────
# build_catalog / build_tab
# ─────────────────────────────────────────────────────────────

def build_catalog() -> dict:
    """전체 catalog (3m/1y/1w MVP + 전 탭 1d). DB 불필요 (정적 상수).

    1d는 10min intraday 구성(테더 11 / usd 11[krx, ADR-038 D4 ②] / jpy·eur 9 — §3:49-54)으로 장기(3m/1y/1w)와 series
    구성이 달라 별도 정의(섞지 않음 — graph_v2_intraday.TAB_1D_*). axis_groups는 장기(_TAB_SERIES)
    기준이나 1d와 정합: usd는 장기에도 dxy(index) 포함, jpy/eur는 1d에도 DXY 미노출(§9:521).
    supported_periods(전역)는 MVP_PERIODS 유지 — 1d는 tab-specific(periods dict에만 존재).
    """
    from app.graph_v2_intraday import INTRADAY_TABS, tab_1d_all_series, tab_1d_default_visible

    tabs = []
    for tab in _TAB_SERIES:
        # ADR-038 G2/G3 — 무인증 catalog는 전역 게이트만 반영 (per-user는 클라 krx_visible gate)
        series_ids = _effective_tab_series(tab)
        periods = {}
        for period in MVP_PERIODS:
            periods[period] = {
                "all_series": list(series_ids),
                "default_visible_series": _effective_default_visible(tab),
            }
        if tab in INTRADAY_TABS:
            periods["1d"] = {
                "all_series": tab_1d_all_series(tab),
                "default_visible_series": tab_1d_default_visible(tab),
            }
        tabs.append({
            "id": tab,
            "label": _TAB_LABEL[tab],
            "axis_groups": _tab_axis_groups(tab),
            "periods": periods,
        })
    return {
        "tabs": tabs,
        "version": CATALOG_VERSION,
        "supported_periods": list(MVP_PERIODS),
        "cache_ttl_seconds": 3600,
    }


def build_tab(db, tab: str, period: str, today_kst: date | None = None) -> dict:
    """탭×기간 모든 series 데이터 (§10 /api/v2/graph/tab 응답).

    호출 전 endpoint가 tab 존재 + period 지원(is_supported_period) 검증 가정.
    period→granularity (3m/1y=daily=source_daily_rates / 1w=hourly=source_hourly_rates).
    """
    if today_kst is None:
        today_kst = datetime.now(tz=KST).date()
    start, end = period_range(period, today_kst)
    granularity = PERIOD_GRANULARITY[period]

    series_out = []
    for series_id in _effective_tab_series(tab):   # ADR-038 G2 accessor
        entry = SERIES_REGISTRY[series_id]
        if entry["kind"] == "source_daily_rates":
            series_out.append(_read_sdr_series(db, series_id, entry, start, end, granularity))
        elif entry["kind"] == "market_index":
            series_out.append(_read_market_index_series(db, series_id, entry, start, end, granularity))
        else:
            raise ValueError(f"미지원 series kind: {entry['kind']!r} (series={series_id})")

    return {
        "tab": tab,
        "period": period,
        "series": series_out,
        "metadata": {
            "fetched_at": datetime.now(tz=KST).isoformat(),
            "bucket_size": _BUCKET_SIZE[granularity],
            "range": {"start": start.isoformat(), "end": end.isoformat()},
        },
    }


def known_tabs() -> list[str]:
    """catalog에 존재하는 tab id 목록 (endpoint 404 판정용)."""
    return list(_TAB_SERIES.keys())
