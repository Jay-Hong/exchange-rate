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
from datetime import datetime, timedelta

import pytz
from pydantic import BaseModel, ConfigDict
from pytz import timezone
from sqlalchemy import and_, func

from app import crud, graph_v2, models
from app.database import get_db_context

logger = logging.getLogger("exchange_rate.free_snapshot")

KST = timezone("Asia/Seoul")

# 무료 스냅샷 basis = 매시 HH:30 (사용자 2026-07-18 — 09:00 개장 후 09:30 첫 정보로 30분 만에
# 장 파악 가능 + as_of가 데이터보다 뒤(>=)라 "HH:30 기준" 라벨과 값이 정합). cron도 :30 발화.
FREE_SNAPSHOT_BASIS_MINUTE = 30
# precompute cron 발화 초 — scheduler(CronTrigger)와 refresh_not_before 계산이 **공유**(ETC, codex): cron 타이밍
# 변경 시 한 곳만 수정하면 응답 정책이 함께 따라감. :19 선택 근거(동시-시작 회피)는 scheduler 등록부 주석 참조.
FREE_SNAPSHOT_PRECOMPUTE_SECOND = 19
# refresh_not_before = basis slot(:30) + precompute_second(:19) + 이 여유(cron 완료 + Redis 전파). 합 60초 = :31:00.
FREE_SNAPSHOT_REFRESH_READY_MARGIN_SECONDS = 41

# MVP = usd only (달러 주력 탭 — investing.usd + hana.usd + dxy로 sdr/market_index 양 reader 검증).
# 확장: FREE_SNAPSHOT_TABS 추가. 단 non-FX 탭(tether=usdt-krw)은 legacy_policy가 rate에서 usdt를 배제하므로
# source_rates 기반 별도 rate reader가 필요(N4) — "튜플 추가만"으로는 rate가 빈다.
FREE_SNAPSHOT_TABS = ("usd",)
# 1d = intraday(10min closed-bucket) — 무료는 premium live-tail/in_progress 미포함, cron 시점 고정(ADR-039 A).
FREE_SNAPSHOT_PERIODS = ("1d", "1w", "3m", "1y")
TAB_ASSET = {"usd": "usd-krw", "jpy": "jpy-krw", "eur": "eur-krw", "tether": "usdt-krw"}

# cron이 매시간 갱신 → 정상 시 항상 warm. long-TTL = cron 장애 시에도 Redis canonical 최대 ~25h 유지.
# (serve의 process-local last-good은 별개로 프로세스 수명 동안 유지 — Redis TTL과 무관, availability 우선.)
FREE_SNAPSHOT_TTL_SECONDS = 90000


def basis_as_of(now_kst: datetime) -> datetime:
    """마지막 HH:30 경계 (무료 스냅샷 basis). now.minute>=30이면 이번 시 HH:30, 아니면 직전 시 HH:30."""
    if now_kst.minute >= FREE_SNAPSHOT_BASIS_MINUTE:
        return now_kst.replace(minute=FREE_SNAPSHOT_BASIS_MINUTE, second=0, microsecond=0)
    return (now_kst - timedelta(hours=1)).replace(minute=FREE_SNAPSHOT_BASIS_MINUTE, second=0, microsecond=0)


def fetch_rate_entries_until(db, asset: str, as_of_kst: datetime) -> list:
    """as_of cutoff rate 조회 — `timestamp <= as_of`인 각 소스 최신 1건 (codex 시간 계약).

    get_all_rates_flat(무조건 최신)을 쓰면 build 중 커밋된 as_of 초과 값이 유입돼 "HH:30 기준" 라벨과
    데이터가 어긋난다(21:00 라벨에 21:19 값 — 사용자 실측 2026-07-18). cutoff을 쿼리 불변식으로 강제.
    반환 shape은 legacy flat과 동일({currency, bank, rate, timestamp[KST ISO]}) — 기존 hot-path 함수 무접촉.
    FX asset(usd/jpy/eur-krw) 전용 — investing + banks. 테더(usdt-krw)는 N4에서 source_rates 기반 별도.
    """
    cutoff_utc = as_of_kst.astimezone(pytz.utc).replace(tzinfo=None)   # DB timestamp = UTC naive
    entries = []

    inv = (
        db.query(models.InvestingExchangeRate)
        .filter(
            models.InvestingExchangeRate.currency == asset,
            models.InvestingExchangeRate.timestamp <= cutoff_utc,
        )
        .order_by(models.InvestingExchangeRate.timestamp.desc(), models.InvestingExchangeRate.id.desc())
        .first()
    )
    if inv:
        entries.append({
            "currency": inv.currency,
            "bank": "investing",
            "rate": inv.rate,
            "timestamp": crud.to_kst_isoformat(inv.timestamp),
        })

    ranked = (
        db.query(
            models.BankExchangeRate.id,
            func.row_number().over(
                partition_by=models.BankExchangeRate.bank,
                order_by=[
                    models.BankExchangeRate.timestamp.desc(),
                    models.BankExchangeRate.id.desc(),
                ],
            ).label("rn"),
        )
        .filter(
            models.BankExchangeRate.currency == asset,
            models.BankExchangeRate.timestamp <= cutoff_utc,
        )
        .subquery()
    )
    records = (
        db.query(models.BankExchangeRate)
        .join(ranked, and_(models.BankExchangeRate.id == ranked.c.id, ranked.c.rn == 1))
        .all()
    )
    records.sort(key=lambda r: crud._bank_display_sort_key(r.bank))
    entries.extend({
        "currency": r.currency,
        "bank": r.bank,
        "rate": r.rate,
        "timestamp": crud.to_kst_isoformat(r.timestamp),
    } for r in records)
    return entries


def free_snapshot_key(tab: str, period: str) -> str:
    """무료 전용 Redis 키 (premium graph_v2:tab:* / latest:* 와 네임스페이스 분리 — §4.2/§3.1)."""
    return f"free:snapshot:{tab}:{period}"


def compute_refresh_not_before(now_kst: datetime) -> datetime:
    """serve-time "이 시각 이후 재요청 권장" — 다음 HH:30 publish slot(now 초과) + 60초 마진(=:31:00, cron :30:19+41s).

    **serve-time now 기준**(as_of 무관)이라 stale canonical에도 항상 **미래** slot 반환 — client의 5분 폴링을
    one-shot 스케줄로 대체하는 축. **데이터 존재 보장이 아니라 재요청 권장 시각**(stale 판단·recovery는 client가
    as_of로 별도 처리). client는 여기에 설치별·탭별 결정적 jitter(10~30s)를 더해 매시 :31:10~:31:30로 부하 분산.
    now.minute>=30이면 이번 시 publish는 지났으니 다음 시 :30, 아니면 이번 시 :30 (basis_as_of와 대칭 경계).
    **+09:00 출력 계약(codex)**: naive 입력 거부, aware(UTC 등) 입력은 KST 정규화 → :30 grid·출력 offset 정확.
    """
    if now_kst.tzinfo is None:
        raise ValueError("compute_refresh_not_before: now_kst must be timezone-aware (+09:00 출력 계약)")
    now_kst = now_kst.astimezone(KST)   # UTC 등 aware 입력도 KST로 정규화(:30 경계 판정·+09:00 출력 정확)
    slot = now_kst.replace(minute=FREE_SNAPSHOT_BASIS_MINUTE, second=0, microsecond=0)
    if slot <= now_kst:
        slot += timedelta(hours=1)
    return slot + timedelta(
        seconds=FREE_SNAPSHOT_PRECOMPUTE_SECOND + FREE_SNAPSHOT_REFRESH_READY_MARGIN_SECONDS
    )


def attach_refresh_not_before(payload: dict, now_kst: datetime | None = None) -> dict:
    """serve-time top-level `refresh_not_before`(ISO8601 +09:00) 부착 — canonical(Redis/last-good) 불변(copy에만).

    성공 2경로(Redis canonical / local last-good) 모두에 부착. **항상 present**(stale canonical이어도 생략 안 함 —
    stale은 client가 as_of로 판단). 구 client는 미인지 top-level 필드를 무시(additive, 무영향). now_kst 미지정 시
    serve-time now(테스트는 compute_refresh_not_before에 고정 now 주입).
    """
    now_kst = now_kst or datetime.now(KST)
    return {
        **payload,
        "refresh_not_before": compute_refresh_not_before(now_kst).isoformat(),
    }


def attach_free_snapshot_domain(payload: dict, period: str) -> dict:
    """무료 스냅샷 응답 **graph** 블록에 serve-time X축 domain 부착(anchor=as_of). 입력 payload 불변(중첩 graph 복사).

    프리미엄과 envelope가 달라 별도 adapter — 프리미엄=metadata / 무료=flat graph(FreeGraphBlock). serve 시점에
    부착하므로 canonical(Redis/last-good)은 domain 없이 깨끗하게 유지(검증 후 copy에만 부착 → 재검증 회귀 0).
    as_of가 naive/파싱 실패면 domain 없이 canonical 그대로 반환 — 스냅샷 자체는 유효하므로 503 아님(클라가 data-derived로 fallback).
    """
    try:
        as_of = datetime.fromisoformat(payload["as_of"])
        if as_of.tzinfo is None:
            raise ValueError("as_of timezone-naive")
        domain = graph_v2.period_domain(period, as_of)   # anchor tz-aware → +09:00 정규화 출력
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("free snapshot domain 부착 실패 — canonical 그대로 반환 (period=%s, err=%s)", period, e)
        return payload
    return {
        **payload,
        "graph": {
            **payload.get("graph", {}),
            **domain,
        },
    }


def _snapshot_is_nonempty(payload: dict) -> bool:
    """rate entry 1개 이상 AND graph series 중 data 있는 것 1개 이상.

    N2 — 빈 build가 이전 정상값을 setex로 덮어쓰는 것을 방지(성공-반환이어도 내용이 비면 SET 생략).
    """
    rate_entries = payload.get("rate", {}).get("entries", [])
    series = payload.get("graph", {}).get("series", [])
    return bool(rate_entries) and any(s.get("data") for s in series)


def _assert_graph_within_as_of(payload: dict) -> None:
    """fail-closed — 모든 graph point ts <= as_of (시간 계약 §4.2). 위반 시 raise → precompute가 SET 생략(keep-last-good).

    정상 rollup(hourly/daily append + 1d closed-bucket now_kst=as_of)은 as_of 초과 bucket을 만들지 않지만,
    1w 조회가 당일 23:59:59 inclusive라 백필 실수/오염 데이터의 우회가 구조적으로 가능(codex 2026-07-18) —
    쿼리 신뢰가 아니라 최종 payload 검증으로 불변식을 잠근다.
    """
    as_of = datetime.fromisoformat(payload["as_of"])
    for s in payload.get("graph", {}).get("series", []):
        for p in s.get("data", []):
            if not isinstance(p, dict):
                continue   # 비-dict point는 이 timestamp 검사의 명시적 범위 밖(이 함수는 as_of 초과만 차단)
            ts = p.get("ts")
            if ts is None:
                continue
            if datetime.fromisoformat(ts) > as_of:
                raise ValueError(
                    f"graph point가 as_of 초과: series={s.get('id')!r} ts={ts} as_of={payload['as_of']}")


def _assert_rates_within_as_of(payload: dict) -> None:
    """fail-closed — 모든 rate entry timestamp <= as_of (시간 계약 §4.2, graph와 대칭). build query가
    cutoff을 강제하지만, serve-time에 배포 전/오염 canonical(구 get_all_rates_flat = 무조건 최신이라 as_of
    초과 가능)이 유입되는 창을 차단(codex 2026-07-18 — validate가 graph만 검사하던 비대칭 해소).
    timestamp는 **필수**(시간 계약 완전 closure — ts 없는 rate entry는 as_of 판정 불가라 fail-closed 거부,
    codex 2026-07-18. 실 canonical은 fetch_rate_entries_until이 항상 ts 부여라 false-503 없음).
    비-dict는 Pydantic(entries:list[dict])이 이미 거부."""
    as_of = datetime.fromisoformat(payload["as_of"])
    for e in payload.get("rate", {}).get("entries", []):
        if not isinstance(e, dict):
            continue
        ts = e.get("timestamp")
        if ts is None:
            raise ValueError(f"rate entry에 timestamp 없음(시간 계약 필수): bank={e.get('bank')!r}")
        if datetime.fromisoformat(ts) > as_of:
            raise ValueError(
                f"rate entry가 as_of 초과: bank={e.get('bank')!r} ts={ts} as_of={payload['as_of']}")


def _assert_as_of_on_grid(payload: dict) -> None:
    """fail-closed — as_of가 timezone-aware + HH:30:00 grid에 정렬됐는지 검증(무료 cron은 aware KST :30만 생성).
    시간 계약을 grid 수준까지 닫는다 — off-grid canonical(구 :00 floor 배포 이전 / 오염)을 serve에서 거부(codex 2026-07-18).
    cutoff(rate/graph <= as_of)만으론 as_of=14:00·rate=13:59 같은 off-grid consistent canonical이 통과.
    **tz-aware 강제(codex 2026-07-19)**: as_of가 naive면 cutoff assert의 timestamp 비교가 무의미(naive-vs-naive면 예외
    없이 통과)해 fully-naive 오염 canonical이 새어나감 → cutoff 토대인 as_of aware를 여기서 명시 거부(→ 503/last-good)."""
    as_of = datetime.fromisoformat(payload["as_of"])
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError(f"as_of가 timezone-naive: {payload['as_of']}")
    # grid는 KST HH:30 개념(cron이 KST :30 발화) — offset 무관 정규화 후 판정(입력 offset raw minute 아님, codex 2026-07-19).
    # cutoff assert들은 aware instant 비교라 이미 offset-무관이지만, minute은 offset 의존이라 여기서 KST로 변환.
    as_of_kst = as_of.astimezone(KST)
    if (as_of_kst.minute, as_of_kst.second, as_of_kst.microsecond) != (FREE_SNAPSHOT_BASIS_MINUTE, 0, 0):
        raise ValueError(f"as_of가 :{FREE_SNAPSHOT_BASIS_MINUTE} grid 밖(KST): {payload['as_of']}")


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

    시간 계약(codex 2026-07-18): as_of = 마지막 HH:30 경계 / rate·graph = `timestamp <= as_of`인
    마지막 데이터만 / generated_at = 조립 완료 시각. 라벨("HH:30 기준")과 데이터가 쿼리 불변식으로 정합.
    rate = fetch_rate_entries_until(cutoff, KRX-free는 legacy allowlist + _assert_krx_free).
    graph: **1w·3m·1y** = build_tab(exclude_krx=True, today_kst=as_of.date()) — hourly/daily canonical이라
    as_of(HH:30) 초과 bucket 자체가 없음 / **1d** = build_tab_1d_payload(now_kst=as_of 주입) —
    잘라낼 경계=as_of 고정이라 cron 지연/재빌드에도 as_of 초과 봉 유입 불가. 조립 후 _assert_krx_free.
    """
    now_kst = now_kst or datetime.now(KST)
    as_of = basis_as_of(now_kst)

    asset = TAB_ASSET[tab]
    rate_entries = fetch_rate_entries_until(db, asset, as_of)

    # graph: 1d=intraday(10min closed-bucket, as_of cutoff) / 1w·3m·1y=build_tab(daily/hourly). 둘 다 exclude_krx=True.
    if period == "1d":
        # 무료 1d = premium live-tail/in_progress 미포함 closed-bucket. free cron(:30)이 hourly-frozen 담당
        # (premium intraday :12 */10과 별개 — 무료는 다음 free cron[:30]까지 1h 불변). build가 자체 세션 사용(db 인자 불요).
        from app import graph_v2_intraday
        graph = graph_v2_intraday.build_tab_1d_payload(tab, exclude_krx=True, now_kst=as_of)
    else:
        # N5: graph range를 as_of.date()에 고정 → as_of와 graph range.end가 항상 일관.
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
    _assert_as_of_on_grid(payload)       # fail-closed: as_of가 :30 grid 아니면 raise (basis_as_of가 이미 보장, 방어적)
    _assert_krx_free(payload)            # fail-closed: KRX 유입 시 raise → caller가 setex 스킵
    _assert_graph_within_as_of(payload)  # fail-closed: as_of 초과 graph point 유입 시 raise → setex 스킵
    _assert_rates_within_as_of(payload)  # fail-closed: as_of 초과 rate entry 유입 시 raise (build는 방어적, cutoff query가 이미 보장)
    return payload


# ── serve-time 캐시 재검증용 응답 스키마 (Pydantic, extra 필드 허용) ──
# entry/point 내부는 검증 안 함(변동성 — valid 거부 회귀 회피). 구조+날짜형식+bucket_size+range만 엄격.
class _FreeGraphRange(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: str
    end: str


class _FreeGraph(BaseModel):
    model_config = ConfigDict(extra="ignore")
    series: list[dict]
    bucket_size: str
    range: _FreeGraphRange


class _FreeRate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    asset: str
    entries: list[dict]


class _FreeSnapshotModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tab: str
    period: str
    as_of: datetime          # ISO 파싱 → 'banana' 등 invalid date 거부
    generated_at: datetime
    rate: _FreeRate
    graph: _FreeGraph


def validate_snapshot_payload(payload, tab: str, period: str) -> bool:
    """cache-hit serve-time 재검증(B1 fail-closed) — 오염된 캐시(KRX/null/list/wrong-tab)를 그대로 반환하지 않게.

    검증: _FreeSnapshotModel(Pydantic) 스키마 — 구조 + as_of/generated_at **날짜 형식**(datetime 파싱 →
    'banana' 등 거부) + rate.asset/entries(list) + graph.series(list) + **bucket_size**(str) + **range(start/end)**.
    이후 tab/period 일치 + rate.asset==TAB_ASSET[tab] + nonempty + KRX-free. 하나라도 실패(또는 예외) → False → last-good/503.
    ⚠️ **residual(정직)**: entry.rate 값 타입(문자열 rate 강제변환 통과)·graph point 내부는 미검증 —
       valid canonical(변동성 큰 point 필드)을 실수로 거부해 실사용자 503 회귀하는 위험을 피하기 위함. 완전 value-level은 후속.
    _assert_krx_free는 build+serve 양쪽 적용(단일 fail-closed 지점).
    """
    try:
        if not isinstance(payload, dict):
            return False
        _FreeSnapshotModel.model_validate(payload)   # 스키마+날짜형식+bucket_size+range (ValidationError→except→False)
        if payload.get("tab") != tab or payload.get("period") != period:
            return False
        if payload.get("rate", {}).get("asset") != TAB_ASSET.get(tab):
            return False
        if not _snapshot_is_nonempty(payload):
            return False
        _assert_as_of_on_grid(payload)        # off-grid(구 :00 floor / 오염) canonical 거부 — grid 수준 시간 계약
        _assert_krx_free(payload)
        _assert_graph_within_as_of(payload)   # 배포 전/오염 canonical의 as_of 초과 graph point가 last-good으로 seed되는 창 차단
        _assert_rates_within_as_of(payload)   # rate도 대칭(build query만 cutoff이라 구/오염 canonical의 초과 rate 차단)
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
