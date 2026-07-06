# app/crud.py

# 표준 라이브러리
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import List, Dict, Any, Optional, Tuple

# 서드파티 라이브러리
from sqlalchemy.orm import Session
from sqlalchemy.sql import func, and_

# 로컬 애플리케이션
from app import models
from app import atomic_write_runtime  # P1b A2-2: write-mode gate (snapshot read, module import for patchability)
from app import atomic_revision  # P1b A2-4: revision primitives (stdlib-only, dormant)
from app.atomic_write_control import WriterMode  # P1b A2-2: enforced_action 비교

# 로거 설정
logger = logging.getLogger("exchange_rate.db")

# 알림 메시지용 한글 매핑
BANK_NAMES_KR = {
    "investing": "인베스팅",
    "kb": "국민은행",
    "hana": "하나은행",
    "shinhan": "신한은행",
    "woori": "우리은행",
    "ibk": "IBK기업은행",
    "nh": "NH농협",
    "sc": "SC제일은행",
    "bs": "부산은행",
    "citi": "씨티은행",
}

CURRENCY_NAMES_KR = {
    "usd-krw": "달러",
    "jpy-krw": "엔화",
    "eur-krw": "유로",
}

# 지원 통화쌍 (고정값, DB DISTINCT 쿼리 대체)
SUPPORTED_CURRENCY_PAIRS = ["eur-krw", "jpy-krw", "usd-krw"]

# `metadata.banks` dead field 값 고정 집합.
# /api/rates 응답의 metadata.banks/metadata.currencies는 이제 실제 소비처가 없는
# 레거시 호환 필드이고, USDT 도입 후 자동 계산 로직이 이 필드에 거래소 이름을
# 섞어 넣어 시맨틱 오염을 일으켰다. 이 필드는 "레거시 값으로 동결"한다.
# 새 앱은 SourceRegistry를 직접 참조한다. 필드 자체 제거는 iOS/Android가
# Codable/Serializable non-optional 선언을 풀어준 이후 별도로 진행한다.
LEGACY_METADATA_BANKS = frozenset({
    "investing", "kb", "hana", "shinhan", "woori",
    "ibk", "nh", "sc", "bs", "citi",
})

# iOS/Android Bank enum과 맞춘 은행 표시순.
# 미등록 은행은 뒤로 보내고 bank 코드순으로 fallback한다.
BANK_DISPLAY_ORDER = [
    "kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi",
]
_BANK_DISPLAY_ORDER_INDEX = {
    bank: idx for idx, bank in enumerate(BANK_DISPLAY_ORDER)
}


def _bank_display_sort_key(bank: str) -> Tuple[int, str]:
    """은행 표시순 정렬 key. 미등록 은행은 뒤쪽 + 코드순 fallback."""
    return (_BANK_DISPLAY_ORDER_INDEX.get(bank, len(BANK_DISPLAY_ORDER)), bank)


def format_threshold(value: float) -> str:
    """목표값 포맷: 소수점 이하 불필요한 0 제거 (1475.00 → 1475, 1475.50 → 1475.5)"""
    formatted = f"{value:.2f}"
    if '.' in formatted:
        formatted = formatted.rstrip('0').rstrip('.')
    return formatted


# KST 타임존 (UTC+9)
KST = dt_timezone(timedelta(hours=9))


def to_kst_isoformat(dt: Optional[datetime]) -> Optional[str]:
    """
    DB에서 읽은 datetime을 KST ISO 8601 문자열로 변환

    Args:
        dt: datetime 객체 (naive 또는 aware)

    Returns:
        KST ISO 8601 문자열 (예: "2025-01-13T14:30:00+09:00")

    Notes:
        - DB에서 읽은 naive datetime은 UTC로 해석한다.
        - aware datetime은 UTC로 변환 후 KST로 변환한다.
    """
    if dt is None:
        return None
    # PostgreSQL에서 naive datetime은 UTC로 저장됨
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(KST).isoformat()


def _write_changed_bank_rates_to_redis(redis_updates: list) -> list:
    """PR Z-2e Step 3b — bank latest Redis direct write (commit 직후 호출).

    broadcast hot path가 latest:bank:* key를 Redis-first로 읽으므로 mirror cycle
    bypass. Z-2d allowlist 통과한 source라 본 write 실패 시 mirror cycle이
    safety repair (LATEST_MIRROR_INTERVAL_SECONDS 주기, 운영 60s).

    `redis_updates` entry shape: `{"source", "asset", "rate", "timestamp"}`
    (topic-native). 호출자는 db.commit() 성공 후에만 이 함수를 부른다.

    실패는 best-effort — 호출자(FCM alerts) 흐름에 영향 X.

    Returns (§6.6.2 C1 — SET-only trigger gating):
        Redis SET 성공한 update 부분집합 (입력 순서 보존). SET 실패/예외분 제외 →
        호출자가 이 부분집합만 topic trigger (stale publish 방지, axis #2).
        Redis SET 동작·best-effort·mirror 안전망은 불변 — outcome 반환만 추가.
    """
    if not redis_updates:
        return []
    # 함수 내부 import — latest_rates_cache가 crud를 module-level로 import하므로
    # 순환 참조 회피.
    from app import latest_rates_cache
    succeeded: list = []
    for update in redis_updates:
        try:
            if latest_rates_cache.set_latest_bank_rate_from_sync_job(
                bank=update["source"],
                asset=update["asset"],
                rate=update["rate"],
                timestamp=update["timestamp"],
            ):
                succeeded.append(update)
        except Exception:
            logger.exception(
                "bank Redis direct write 예외 (격리, mirror cycle 안전망 의존)",
                extra={"bank": update["source"], "asset": update["asset"]},
            )
    return succeeded


def _write_changed_investing_rates_to_redis(redis_updates: list) -> list:
    """PR Z-2e Step 3b — investing latest Redis direct write (commit 직후).

    bank helper와 동일 정책 (mirror cycle safety net 의존, best-effort 격리).

    Returns (§6.6.2 C1): SET 성공한 update 부분집합 (SET-only trigger gating).
    """
    if not redis_updates:
        return []
    from app import latest_rates_cache
    succeeded: list = []
    for update in redis_updates:
        try:
            if latest_rates_cache.set_latest_investing_rate_from_sync_job(
                asset=update["asset"],
                rate=update["rate"],
                timestamp=update["timestamp"],
            ):
                succeeded.append(update)
        except Exception:
            logger.exception(
                "investing Redis direct write 예외 (격리, mirror cycle 안전망 의존)",
                extra={"asset": update["asset"]},
            )
    return succeeded


@dataclass(frozen=True)
class ChangedRate:
    """변경된 환율 1건의 내부 DTO (PR B — Bank/Investing β observation fanout 1단계).

    staging이 insert-if-changed 통과한 **변경값만** 생성. sink(Redis/Alert) adapter가
    기존 dict shape로 변환. PR B는 changes-only — §4.1.3 `seen_at`(값 변경 무관 수신)
    /`rate_changed_at` 분리는 C2(freshness). `changed_at`은 현행
    `models.get_utc_now()`(save-time) 그대로.
    """
    source: str
    asset: str
    rate: float
    changed_at: datetime


@dataclass(frozen=True)
class StagedRateChange:
    """P1b A2-4 — staging-only transient: ChangedRate(frozen sink DTO) + ORM row ref (§16).

    direct write revision은 flush 후 row.id에서 확보(추가 SELECT 없음). frozen sink DTO를
    SQLAlchemy lifecycle에 결합하지 않기 위해 ORM ref는 별도 transient에 보관 — commit 후
    downstream에는 plain value(revision)만 전달, ORM 객체 미유출 (§16 ownership).

    **producers (C6-5b-3b)**: `_stage_bank_rate_changes`/`_stage_investing_rate_changes`가 생성. legacy branch는
    `.change`(ChangedRate)만 사용(관측 동작 불변), `.row`/`.revision`(flush-row-ref)은 atomic 분기
    (`_insert_bank_rates_atomic`)만 소비 — atomic mode는 C6-FLIP(must-confirm)까지 prod 미발화(behavior-change-0).
    """

    change: ChangedRate
    row: atomic_revision.RevisionRow

    @property
    def revision(self) -> atomic_revision.Revision:
        """flush 후 호출 — `(canonical_epoch_us(row.timestamp), row.id)`. selector와 공유 구성."""
        return atomic_revision.revision_from_row(self.row)


def _stage_bank_rate_changes(db: Session, current_rates: dict, bank_name: str) -> List["StagedRateChange"]:
    """은행 환율 insert-if-changed staging — 변경 row를 `db.add` + `StagedRateChange`(ChangedRate + ORM row) 생성.

    C6-5b-3b: 반환을 `StagedRateChange`로 (ORM row ref 동반 — atomic 분기의 flush-row-ref revision용,
    §16). **legacy 소비자는 `[sc.change for sc in staged]`로 ChangedRate 추출 → 관측 동작 불변**
    (.row/.revision는 atomic 분기만 사용). ⚠️ `_stage_investing_rate_changes`와 **의도적 중복**(model/filter/
    로그 포맷만 차이) 유지 — 통합은 5-source 공통화 phase([§6.6.1](../USDT_TOPIC_MIGRATION_PLAN.md)).
    """
    staged: List["StagedRateChange"] = []
    for pair, current_rate in current_rates.items():
        if current_rate is None:
            continue

        last_record = (
            db.query(models.BankExchangeRate)
            .filter(and_(models.BankExchangeRate.bank == bank_name, models.BankExchangeRate.currency == pair))
            .order_by(models.BankExchangeRate.timestamp.desc(), models.BankExchangeRate.id.desc())
            .first()
        )

        should_save = False

        if last_record is None:
            should_save = True
            logger.info(f"⭐️ [신규] {pair}: {current_rate}", extra={"pair": pair, "rate": current_rate, "type": "new", "bank": bank_name})
        elif last_record.rate != current_rate:
            should_save = True
            logger.info(f"⚡️ [변경] {pair}: {last_record.rate} → {current_rate}", extra={"pair": pair, "old_rate": last_record.rate, "new_rate": current_rate, "change": current_rate - last_record.rate, "type": "change", "bank": bank_name})
        else:
            logger.debug(f"📼 [유지] {pair}: {current_rate}", extra={"pair": pair, "rate": current_rate, "type": "unchanged", "bank": bank_name})

        if should_save:
            ts = models.get_utc_now()
            row = models.BankExchangeRate(
                bank = bank_name,
                currency = pair,
                rate = current_rate,
                timestamp = ts
            )
            db.add(row)
            logger.debug("✅ DB에 새 레코드 저장됨")
            staged.append(StagedRateChange(
                change=ChangedRate(source=bank_name, asset=pair, rate=current_rate, changed_at=ts),
                row=row,
            ))

    return staged


def _changes_to_redis_updates(changes: List[ChangedRate]) -> List[dict]:
    """ChangedRate → topic-native dict (기존 `_write_changed_*_to_redis` 인자 shape 보존).

    timestamp KST iso 변환 책임은 본 mirror adapter (DTO는 raw `changed_at` 보유).
    """
    return [{
        "source": c.source,
        "asset": c.asset,
        "rate": c.rate,
        "timestamp": to_kst_isoformat(c.changed_at),
    } for c in changes]


def _changes_to_fcm(changes: List[ChangedRate]) -> List[dict]:
    """ChangedRate → FCM dict (기존 `process_rate_alerts` 인자 shape 보존, ts/source leak 없음)."""
    return [{
        "bank": c.source,
        "currency": c.asset,
        "rate": c.rate,
    } for c in changes]


# ── §6.6.2 C1 — bank/investing → topic trigger emission ──────────────
# crud worker thread(no running loop)에서 호출 → topic_trigger_bridge로 main loop
# 마샬링. mode 게이트 + SET-only(성공분만) + fx:* / usdt:krw cross-route 라우팅.

def _tether_cross_route_sources() -> set:
    """usd-krw 변경 시 usdt:krw로도 cross-route할 source 집합.

    usdt:krw payload가 읽는 bank source(TETHER_TAB_BANK_SOURCES SSOT) + investing.
    """
    from app.usdt_topic_payload import TETHER_TAB_BANK_SOURCES
    return set(TETHER_TAB_BANK_SOURCES) | {"investing"}


def _run_topic_emission(succeeded: List[dict], mode: str) -> None:
    """main loop 위(bridge callback)에서 실행 — fx:* trigger + 조건부 usdt:krw cross-route.

    Args:
        succeeded: SET 성공한 topic-native dict ({source, asset, rate, timestamp}).
        mode: direct_coalesced면 cross-route 실호출, dual_shadow면 미호출(live tether
            발행 방지). legacy_piggyback은 _emit_topic_triggers에서 이미 걸러짐.
    """
    from app.fx_topic_trigger import (
        FX_TRIGGER_REASON_BANK_CHANGE,
        FX_TRIGGER_REASON_INVESTING_CHANGE,
        request_fx_topic_trigger,
    )
    cross_sources = _tether_cross_route_sources()
    for update in succeeded:
        source = update["source"]
        asset = update["asset"]
        reason = (
            FX_TRIGGER_REASON_INVESTING_CHANGE
            if source == "investing"
            else FX_TRIGGER_REASON_BANK_CHANGE
        )
        request_fx_topic_trigger(source, asset, reason)

        # usdt:krw cross-route — kb/hana/investing의 usd-krw만 (usdt:krw payload 구성).
        if asset == "usd-krw" and source in cross_sources:
            if mode == "direct_coalesced":
                from app import tether_topic_trigger
                from app.tether_topic_trigger import (
                    TETHER_TRIGGER_REASON_BANK_INVESTING_FX_CHANGE,
                )
                tether_topic_trigger.request_tether_topic_trigger(
                    source=source,
                    asset=asset,
                    reason=TETHER_TRIGGER_REASON_BANK_INVESTING_FX_CHANGE,
                )
            elif mode == "dual_shadow":
                # live tether 발행 방지 — 예상 발화량 counter만 (Increment 3).
                from app.fx_topic_trigger import record_tether_route_shadow
                record_tether_route_shadow(asset)


def _emit_topic_triggers(succeeded: List[dict]) -> None:
    """SET 성공 변경 → topic trigger emission (worker thread → bridge 마샬링).

    mode 게이트: legacy_piggyback이면 **bridge 호출 자체 X** (axis #4 — marshal 0,
    zero overhead, land behavior-change-0). 비legacy면 bridge로 _run_topic_emission을
    main loop에 마샬링 (best-effort — bridge skip 시에도 호출자 흐름 영향 X).
    """
    if not succeeded:
        return
    from app import config as app_config
    mode = app_config.BANK_INVESTING_TOPIC_TRIGGER_MODE
    if mode == "legacy_piggyback":
        return
    from app import topic_trigger_bridge
    topic_trigger_bridge.schedule_on_loop(_run_topic_emission, succeeded, mode)


# fanout step 4 S4: FX(bank+investing) alert shadow — worker thread → main loop bridge.
# strong-ref set (asyncio는 task에 weak ref만 → un-referenced task GC 방지, 선례 fx_topic_trigger._telemetry_tasks).
_fx_shadow_tasks: "set[asyncio.Task]" = set()


def _run_fx_alert_shadow(observations: list) -> None:
    """main loop 위 실행 (bridge 마샬링) — async shadow eval을 task로 spawn.

    schedule_on_loop이 sync callback만 받으므로 여기서 create_task (coroutine 직접 전달 ❌
    = un-awaited silent 미실행). _run_guarded는 이 sync wrapper만 격리 — coroutine 예외는
    evaluate_fx_batch_shadow 내부(_evaluate_async per-obs try/except)가 처리(이중 wrap ❌).
    """
    import asyncio
    from app.notifications import fx_alert_shadow
    loop = asyncio.get_running_loop()
    task = loop.create_task(fx_alert_shadow.evaluate_fx_batch_shadow(observations))
    _fx_shadow_tasks.add(task)
    task.add_done_callback(_fx_shadow_tasks.discard)


def _emit_fx_alert_shadow(changes: "List[ChangedRate]") -> None:
    """FX alert shadow 발화 — telemetry-only, FX_ALERT_SHADOW_ENABLED gated, worker thread.

    [ALL_SOURCE_FANOUT_UNIFICATION_PLAN.md §6.1 S4] flag off면 즉시 return (schedule_on_loop
    미호출 = zero overhead). on이면 changes(ChangedRate, changed_at 보유)→AlertObservation 변환
    후 bridge로 main loop 마샬링. legacy process_rate_alerts(authoritative)와 병렬 — persist/FCM
    no-op(2× 발사 없음). timestamp_ms는 changed_at(save-time) 도출 — history-log-only(평가 미참조).
    """
    from app import config as app_config
    if not app_config.FX_ALERT_SHADOW_ENABLED:
        return
    if not changes:
        return
    from app.notifications.alert_evaluator import AlertObservation
    observations = [
        AlertObservation(
            source=c.source,
            asset=c.asset,
            rate=c.rate,
            # changed_at은 get_utc_now() naive UTC (models.py) → 명시적 UTC로 epoch 산출
            # (naive.timestamp()는 로컬 TZ 해석 → 프로세스 TZ 비-UTC 시 오차). history-log-only.
            timestamp_ms=int(c.changed_at.replace(tzinfo=dt_timezone.utc).timestamp() * 1000),
            kind="fx_change",
        )
        for c in changes
    ]
    from app import topic_trigger_bridge
    topic_trigger_bridge.schedule_on_loop(_run_fx_alert_shadow, observations)


def _run_fx_alert_canary(observations: list) -> None:
    """main loop 위 실행 (bridge 마샬링) — canary evaluator.schedule per obs (**real persist+FCM**).

    ev.schedule이 create_task로 ev._tasks에 추적 → shutdown drain은 close_fx_canary_evaluator().
    shadow와 달리 real 발사(FxCanaryBackend). schedule_on_loop이 sync callback만 받으므로 여기서.
    """
    from app.notifications import fx_alert_shadow
    ev = fx_alert_shadow.get_fx_canary_evaluator()
    for obs in observations:
        ev.schedule(obs)


def _emit_fx_alert_canary(changes: "List[ChangedRate]") -> bool:
    """FX alert cutover canary 발화 — **real persist+FCM**, FX_ALERT_CUTOVER_CANARY_ENABLED gated, worker thread.

    [§6.1 canary plan + B1] changes→AlertObservation 변환 후 bridge로 main loop 마샬링 → canary evaluator
    (FxCanaryBackend, allowlist setting만 real 발사). **B1 enqueue-confirmed-skip**: 반환값으로 caller가
    legacy skip 결정 — True(enqueue 성공)면 legacy가 allowlist skip, False(flag off/no changes/enqueue 실패)면
    legacy fallback(no miss). ⚠️ True = bridge `schedule_on_loop`(call_soon_threadsafe) 성공일 뿐 **canary
    FCM 성공 보장 아님**(callback内 예외는 bridge가 삼킴) — 잔여 async eval/FCM 실패 miss는 B1 범위 밖.

    Returns: enqueue 성공 시 True (→ legacy allowlist skip), 아니면 False (→ legacy fallback).
    """
    from app import config as app_config
    if not app_config.FX_ALERT_CUTOVER_CANARY_ENABLED:
        return False
    if not changes:
        return False
    from app.notifications.alert_evaluator import AlertObservation
    observations = [
        AlertObservation(
            source=c.source,
            asset=c.asset,
            rate=c.rate,
            timestamp_ms=int(c.changed_at.replace(tzinfo=dt_timezone.utc).timestamp() * 1000),
            kind="fx_canary",
        )
        for c in changes
    ]
    from app import topic_trigger_bridge
    return topic_trigger_bridge.schedule_on_loop(_run_fx_alert_canary, observations)


# P1b A2-2: write-mode skip 관측 (process-local **approximate** counter, debug용). enforced가
# halt/atomic이면 writer가 staging 전 skip → 여기 누적. 동시 crawler thread의 read-modify-write
# race로 일부 증가가 유실될 수 있음(no-lock — debug 관측이라 정확 카운트 불요). A2엔 0(legacy만).
_write_mode_skip_counts: Dict[Tuple[str, str], int] = {}


def _record_write_mode_skip(source: str, enforced: str) -> None:
    """write-mode가 legacy 아님(halt/atomic)이라 writer가 skip한 것 기록 (approximate counter + debug log)."""
    key = (source, enforced)
    _write_mode_skip_counts[key] = _write_mode_skip_counts.get(key, 0) + 1
    logger.debug("write-mode skip (staging 전 차단)", extra={"source": source, "enforced": enforced})


# C6-5b-3b: atomic v2 compare_write outcome 관측 (process-local **disjoint** counter — bank_investing_redis_stats
# 미오염, behavior-change-0). no-lock(approximate). key=(source, state, structural).
_atomic_write_outcome_counts: Dict[Tuple[str, str, bool], int] = {}
# C7-a: counter 누적 시작 시각 (process-local — 재시작 시 reset, started_at으로 해석). import-time 1회.
_atomic_write_outcome_started_at: str = datetime.now(dt_timezone.utc).isoformat()


def _record_atomic_write_outcome(source: str, outcome) -> None:
    """atomic v2 compare_write outcome 누적 (approximate, debug/go-no-go). key=(source, state.value, **structural**).

    **structural bool로 FAILED 세분** (review MED): migration_required/invalid_schema/unsupported는 모두
    state.value=="failed"로 collapse되므로, structural=True(§17 corruption = go-gate G3 critical)를
    general failed(structural=False = reply-lost 등 benign retry)와 key로 구분한다. conflict는 state="conflict"라
    이미 distinct. crud는 atomic_write_outcome를 직접 import 안 함(trip-wire 경계) — duck-typed
    .state.value/.structural. skipped_newer는 concurrent atomic writer서 정상(경고 X).
    read path: `get_atomic_write_outcome_counts()` (C7-a — /admin/api/atomic-write-outcomes로 노출).
    """
    key = (source, outcome.state.value, getattr(outcome, "structural", False))
    _atomic_write_outcome_counts[key] = _atomic_write_outcome_counts.get(key, 0) + 1


# C7-a observability: writer-side atomic outcome counter의 read accessor (post-flip 건강 신호).
# coordinator-side persisted conflict_counters(atomic_cutover_status future stub)와 **별개** — 여기는
# crud의 process-local writer counter. raw key tuple 미노출 → aggregate/per-source/critical 구조화.
def _empty_outcome_block() -> dict:
    return {
        "total": 0,
        "by_state": {
            "advance": 0,
            "refreshed_equal": 0,
            "skipped_newer": 0,
            "conflict": 0,
            "failed": {"structural": 0, "general": 0},
        },
        "critical": {"total": 0, "conflict": 0, "failed_structural": 0},
    }


def _accumulate_outcome_block(block: dict, state: str, structural: bool, count: int) -> None:
    """block(_empty_outcome_block 산출)에 (state, structural, count) 누적. unknown state는 'other' bucket."""
    block["total"] += count
    by_state = block["by_state"]
    if state == "failed":
        by_state["failed"]["structural" if structural else "general"] += count
    elif state in by_state:
        by_state[state] += count
    else:  # defensive — 미래 state 추가 시 silent drop 방지
        by_state["other"] = by_state.get("other", 0) + count
    # critical rollup = conflict + failed-structural (§17 corruption = G3 critical)
    conflict = by_state["conflict"]
    failed_structural = by_state["failed"]["structural"]
    block["critical"] = {
        "total": conflict + failed_structural,
        "conflict": conflict,
        "failed_structural": failed_structural,
    }


def get_atomic_write_outcome_counts() -> dict:
    """C7-a: writer-side atomic v2 compare_write outcome 누적의 read accessor (process-local 진단).

    flip 후 atomic writer(bank+investing) 건강: advance/refreshed_equal/skipped_newer(정상),
    conflict(cross-process race)·failed-structural(corruption=G3)=**critical**, failed-general=benign retry.
    aggregate(by_state + critical) + per_source + health(ok|critical) + g3_ok. never-raise
    (동시 변이 중 iteration 안전 위해 items snapshot copy). reset route 없음(process-local, 재시작 reset).
    """
    # 동시 변이 중 read snapshot: 현 GIL build에선 list(items())가 단일 C-iteration이라 GIL 미해제 →
    # "dict changed size" 사실상 회피. 단 never-crash 최종 보증은 GIL 세부가 아니라 caller(endpoint)의
    # try/except (free-threaded 3.13t 대비 + 방어적 — 여기 가정에 의존하지 않음).
    snapshot = list(_atomic_write_outcome_counts.items())
    aggregate = _empty_outcome_block()
    per_source: Dict[str, dict] = {}
    for (source, state, structural), count in snapshot:
        _accumulate_outcome_block(aggregate, state, structural, count)
        block = per_source.get(source)
        if block is None:
            block = _empty_outcome_block()
            per_source[source] = block
        _accumulate_outcome_block(block, state, structural, count)
    critical_total = aggregate["critical"]["total"]
    return {
        "started_at": _atomic_write_outcome_started_at,
        "process_local": True,
        "aggregate": aggregate,
        "per_source": per_source,
        "health": "critical" if critical_total > 0 else "ok",
        "g3_ok": critical_total == 0,
    }


def _atomic_write_changes_v2(captured, redis_updates, source_label: str, *, key_kind: str) -> List[dict]:
    """post-commit v2 compare_write loop (best-effort, **FULLY isolated**) → APPLIED subset(topic-native dicts).

    codex guard: writer build 포함 어떤 예외도 caller의 alert/return을 막지 않는다(전체 try/except). DB는 이미
    commit됨 → 어떤 outcome/예외도 rollback 신호 아님(§16:261). per-change `atomic_compare_write_v2`는 no-throw.
    APPLIED(advance/refreshed_equal)만 SET-only trigger 대상(axis #2, C7 — `applied_for_trigger`).
    key_kind: "bank"=latest_key_bank(source, asset) / "investing"=latest_key_investing(asset)(C6-5b-3c 재사용).
    """
    applied: List[dict] = []
    try:
        # lazy import — atomic_direct_write island(crud는 이 island만 import, primitive 직접 import 0 → trip-wire 보존)
        from app import atomic_direct_write, latest_rates_cache
        writer = atomic_direct_write.build_atomic_writer()
        for (change, revision), update in zip(captured, redis_updates):
            if key_kind == "investing":
                key = latest_rates_cache.latest_key_investing(change.asset)
            else:
                key = latest_rates_cache.latest_key_bank(change.source, change.asset)
            outcome = atomic_direct_write.atomic_compare_write_v2(
                writer, key, rate=change.rate, timestamp=update["timestamp"], revision=revision,
                source=change.source, asset=change.asset,
            )
            _record_atomic_write_outcome(change.source, outcome)
            if atomic_direct_write.applied_for_trigger(outcome):
                applied.append(update)
    except Exception:
        logger.exception(
            "atomic v2 write loop 실패 (격리, post-commit best-effort — DB는 commit됨, rollback 아님)",
            extra={"source": source_label},
        )
    return applied


def _insert_bank_rates_atomic(db: Session, current_rates: dict, bank_name: str) -> int:
    """은행 환율 **atomic-mode** writer (C6-5b-3b) — DB commit은 legacy 동일, Redis는 v1 SET 대신 v2 compare_write.

    순서: stage → precommit payload 변환 → db.flush()(id 할당) → flush-row-ref revision capture(commit 전,
    expire_on_commit 회피, §16:229) → db.commit() → post-commit v2 compare_write(best-effort FULLY isolated) →
    process_rate_alerts(ALL changes, DB-authoritative axis #3) → _emit_topic_triggers(APPLIED subset, axis #2).
    banner는 caller(insert_bank_rates_into_db)가 이미 출력. **post-commit Redis 실패/예외는 rollback·alert·
    return을 막지 않는다**(DB 이미 commit, codex guard). atomic mode는 C6-FLIP(must-confirm)까지 prod 미발화.
    """
    staged = _stage_bank_rate_changes(db, current_rates, bank_name)
    new_records_count = len(staged)

    if new_records_count > 0:
        changes = [sc.change for sc in staged]
        redis_updates = _changes_to_redis_updates(changes)   # legacy와 동일 topic-native shape (commit 전)
        changed_rates = _changes_to_fcm(changes)

        db.flush()  # autoflush=False → id 할당 위해 명시 flush
        # flush 후·commit 전 revision capture (expire_on_commit=True면 commit 후 attr 접근이 re-query → 회피)
        captured = [(sc.change, sc.revision) for sc in staged]

        db.commit()
        logger.info(
            f"🎉 총 {new_records_count}개 {bank_name}은행의 새로운 환율 데이터 저장 완료 (atomic v2)",
            extra={"count": new_records_count, "bank": bank_name},
        )

        # post-commit Redis v2 (best-effort, FULLY isolated — 아래 alert/trigger/return 보호)
        applied_updates = _atomic_write_changes_v2(captured, redis_updates, bank_name, key_kind="bank")

        # alert는 DB-authoritative — Redis outcome 무관, 전 committed change 발화 (axis #3, legacy 동일)
        if changed_rates:
            # §6.1 B1: canary enqueue 먼저 — 성공(canary_handled) 시에만 legacy가 allowlist skip
            # (enqueue 실패/예외/flag off → False → legacy fallback, no miss).
            canary_handled = False
            try:
                canary_handled = _emit_fx_alert_canary(changes)
            except Exception:
                logger.exception("FX alert canary 실패 (격리)", extra={"bank": bank_name})
            try:
                sent_count = process_rate_alerts(db, changed_rates, canary_handled=canary_handled)
                if sent_count > 0:
                    logger.info(f"🔔 {sent_count}건 알림 발송 완료", extra={"bank": bank_name, "sent": sent_count})
            except Exception as e:
                logger.exception("알림 처리 중 예외 발생", extra={"bank": bank_name, "error": str(e)})

        # fanout step 4 S4: FX alert shadow (telemetry-only, flag-gated, 격리). canary는 위 B1 reorder로 이동.
        try:
            _emit_fx_alert_shadow(changes)
        except Exception:
            logger.exception("FX alert shadow 실패 (격리)", extra={"bank": bank_name})

        # topic trigger — APPLIED subset만 (axis #2 SET-only gating)
        try:
            _emit_topic_triggers(applied_updates)
        except Exception:
            logger.exception("topic trigger emission 실패 (격리)", extra={"bank": bank_name})

    elif current_rates:
        logger.debug(f"✋ 모든 {bank_name}은행 환율 확인 완료 - 변경사항 없음", extra={"bank": bank_name})
    else:
        logger.warning(f"🈚️ {bank_name}은행 환율 데이터 없음", extra={"bank": bank_name, "source": "crud"})

    return new_records_count


def insert_bank_rates_into_db(db: Session, current_rates: dict, bank_name: str) -> int:
    """은행 환율 DB 저장 + Redis direct write + 알림 조건 체크.

    PR B(β observation fanout 1단계): staging → orchestration 분리. **직렬 순서
    (db.commit() → Redis write → process_rate_alerts) + signature + count==0 게이트
    + sink dict shape 전부 불변** (기존 + PR A characterization 무수정 통과 = spec).

    Returns:
        변경된 레코드 개수 (0: 변경 없음, N: N개 변경됨)
    """
    logger.info(f"|                {bank_name} 환율                 |", extra={"bank": bank_name})

    # P1b A2-2/C6-5b-3b: write-mode gate (snapshot 1-read, no-throw). banner 직후·staging 전.
    #   atomic → 별 분기 _insert_bank_rates_atomic (DB commit + Redis v2 compare_write).
    #   halt(또는 미지값) → fail-closed skip (db.add() 전 return이라 rollback 불요, C2 불변).
    #   legacy → 아래 기존 흐름 불변 (staged에서 .change 추출만 추가, 관측 동작 동일).
    # Bug-fix(incident 2026-06-21): write-mode 미확정(_INITIAL — subprocess refresh transient 실패 등)엔
    # legacy write 금지(post-flip v2를 v1으로 downgrade 방지). _INITIAL.enforced_action=LEGACY라 가드 없으면
    # 아래 legacy 경로로 진입. skip — refresh 확정 후 다음 cycle이 처리(mirror Fix B와 동일 불변).
    if not atomic_write_runtime.is_initialized():
        _record_write_mode_skip(bank_name, "uninitialized")
        return 0
    enforced = atomic_write_runtime.snapshot().enforced_action
    if enforced == WriterMode.ATOMIC:
        return _insert_bank_rates_atomic(db, current_rates, bank_name)
    if enforced != WriterMode.LEGACY:
        _record_write_mode_skip(bank_name, enforced)
        return 0

    staged = _stage_bank_rate_changes(db, current_rates, bank_name)
    new_records_count = len(staged)

    if new_records_count > 0:
        changes = [sc.change for sc in staged]  # legacy: ChangedRate 추출 (관측 동작 불변)
        # sink payload 변환은 commit 전에 — 구 staging-loop 변환과 동일 failure boundary
        # (to_kst_isoformat 실패 시 commit 전 차단 → DB 미커밋 보존, behavior-change-0).
        redis_updates = _changes_to_redis_updates(changes)
        changed_rates = _changes_to_fcm(changes)

        db.commit()
        logger.info(f"🎉 총 {new_records_count}개 {bank_name}은행의 새로운 환율 데이터 저장 완료", extra={"count": new_records_count, "bank": bank_name})

        # commit 성공 후 Redis direct write — broadcast hot path latency 단축.
        # 실패해도 mirror cycle (LATEST_MIRROR_INTERVAL_SECONDS 주기, 운영 60s)이 safety repair, alert 흐름에 영향 X.
        # (PR C) 반환된 SET 성공분만 topic trigger 대상 (SET-only gating, axis #2).
        redis_succeeded = _write_changed_bank_rates_to_redis(redis_updates)

        # 알림 조건 체크 및 FCM 발송 (실패해도 환율 저장에 영향 없음)
        # alert는 DB-authoritative — Redis SET 성공 여부 무관, 전 change 발화 (axis #3).
        if changed_rates:
            # §6.1 B1: canary enqueue 먼저 — 성공(canary_handled) 시에만 legacy가 allowlist skip
            # (enqueue 실패/예외/flag off → False → legacy fallback, no miss).
            canary_handled = False
            try:
                canary_handled = _emit_fx_alert_canary(changes)
            except Exception:
                logger.exception("FX alert canary 실패 (격리)", extra={"bank": bank_name})
            try:
                sent_count = process_rate_alerts(db, changed_rates, canary_handled=canary_handled)
                if sent_count > 0:
                    logger.info(f"🔔 {sent_count}건 알림 발송 완료", extra={"bank": bank_name, "sent": sent_count})
            except Exception as e:
                logger.exception("알림 처리 중 예외 발생", extra={"bank": bank_name, "error": str(e)})

        # fanout step 4 S4: FX alert shadow (telemetry-only, flag-gated, 격리). canary는 위 B1 reorder로 이동.
        try:
            _emit_fx_alert_shadow(changes)
        except Exception:
            logger.exception("FX alert shadow 실패 (격리)", extra={"bank": bank_name})

        # (PR C §6.6.2) topic trigger emission — SET 성공분만, mode-gated, bridge 경유.
        # 직렬 블록 끝 + try/except 격리 (emission 실패가 저장/alert에 영향 X).
        try:
            _emit_topic_triggers(redis_succeeded)
        except Exception:
            logger.exception("topic trigger emission 실패 (격리)", extra={"bank": bank_name})

    elif current_rates:
        logger.debug(f"✋ 모든 {bank_name}은행 환율 확인 완료 - 변경사항 없음", extra={"bank": bank_name})
    else:
        logger.warning(f"🈚️ {bank_name}은행 환율 데이터 없음", extra={"bank": bank_name, "source": "crud"})

    return new_records_count


def get_last_bank_rates_with_ts(db: Session, bank_name: str, pairs: List[str]) -> Dict[str, Dict[str, Optional[Any]]]:
    """
    Returns a map like:
    {
        "usd-krw": {"rate": 1440.0, "timestamp": datetime},
        ...
    }
    """
    results: Dict[str, Dict[str, Optional[Any]]] = {}
    for pair in pairs:
        last_record = (
            db.query(models.BankExchangeRate)
            .filter(and_(models.BankExchangeRate.bank == bank_name, models.BankExchangeRate.currency == pair))
            .order_by(models.BankExchangeRate.timestamp.desc(), models.BankExchangeRate.id.desc())
            .first()
        )
        results[pair] = {
            "rate": last_record.rate if last_record else None,
            "timestamp": last_record.timestamp if last_record else None,
        }
    return results


def _stage_investing_rate_changes(db: Session, current_rates: dict) -> List["StagedRateChange"]:
    """Investing 환율 insert-if-changed staging — 변경 row를 `db.add` + `StagedRateChange`(ChangedRate + ORM row) 생성.

    C6-5b-3b: 반환을 `StagedRateChange`로 (bank과 대칭, flush-row-ref revision용). **legacy 소비자는
    `[sc.change for sc in staged]`로 ChangedRate 추출 → 관측 동작 불변**. ⚠️ `_stage_bank_rate_changes`와
    **의도적 중복**(investing은 currency-only filter + `.2f` 로그 + source="investing" 고정) 유지.
    """
    staged: List["StagedRateChange"] = []
    for pair, current_rate in current_rates.items():
        if current_rate is None:
            continue

        last_record = (
            db.query(models.InvestingExchangeRate)
            .filter(models.InvestingExchangeRate.currency == pair)
            .order_by(models.InvestingExchangeRate.timestamp.desc(), models.InvestingExchangeRate.id.desc())
            .first()
        )

        should_save = False

        if last_record is None:
            should_save = True
            logger.info(f"⭐️ [신규] {pair}: {current_rate:.2f}", extra={"pair": pair, "rate": current_rate, "type": "new", "bank": "investing"})
        elif last_record.rate != current_rate:
            should_save = True
            logger.info(f"⚡️ [변경] {pair}: {last_record.rate:.2f} → {current_rate:.2f}", extra={"pair": pair, "old_rate": last_record.rate, "new_rate": current_rate, "change": current_rate - last_record.rate, "type": "change", "bank": "investing"})
        else:
            logger.debug(f"📼 [유지] {pair}: {current_rate:.2f}", extra={"pair": pair, "rate": current_rate, "type": "unchanged", "bank": "investing"})

        if should_save:
            ts = models.get_utc_now()
            row = models.InvestingExchangeRate(
                currency = pair,
                rate = current_rate,
                timestamp = ts
            )
            db.add(row)
            logger.debug("✅ DB에 새 레코드 저장됨")
            staged.append(StagedRateChange(
                change=ChangedRate(source="investing", asset=pair, rate=current_rate, changed_at=ts),
                row=row,
            ))

    return staged


def _insert_investing_rates_atomic(db: Session, current_rates: dict) -> int:
    """Investing 환율 **atomic-mode** writer (C6-5b-3c) — bank `_insert_bank_rates_atomic` 대칭.

    DB commit은 legacy 동일, Redis는 v1 SET 대신 v2 compare_write (key_kind="investing" →
    latest_key_investing(asset)). 순서/격리/alert(DB-authoritative axis #3)/trigger(APPLIED subset axis #2)는
    bank와 동일 — `_atomic_write_changes_v2` 재사용. banner는 caller(insert_investing_rates_into_db)가 이미 출력.
    **post-commit Redis 실패/예외는 rollback·alert·return 막지 않음**(DB 이미 commit). atomic mode는
    C6-FLIP(must-confirm)까지 prod 미발화.
    """
    staged = _stage_investing_rate_changes(db, current_rates)
    new_records_count = len(staged)

    if new_records_count > 0:
        changes = [sc.change for sc in staged]
        redis_updates = _changes_to_redis_updates(changes)   # legacy와 동일 topic-native shape (commit 전)
        changed_rates = _changes_to_fcm(changes)

        db.flush()  # autoflush=False → id 할당 위해 명시 flush
        # flush 후·commit 전 revision capture (expire_on_commit 회피, §16:229)
        captured = [(sc.change, sc.revision) for sc in staged]

        db.commit()
        logger.info(
            f"🎉 총 {new_records_count}개 Investing의 새로운 환율 데이터 저장 완료 (atomic v2)",
            extra={"count": new_records_count, "bank": "investing"},
        )

        # post-commit Redis v2 (best-effort, FULLY isolated — 아래 alert/trigger/return 보호)
        applied_updates = _atomic_write_changes_v2(captured, redis_updates, "investing", key_kind="investing")

        # alert는 DB-authoritative — Redis outcome 무관, 전 committed change 발화 (axis #3, legacy 동일)
        if changed_rates:
            # §6.1 B1: canary enqueue 먼저 — 성공(canary_handled) 시에만 legacy가 allowlist skip
            # (enqueue 실패/예외/flag off → False → legacy fallback, no miss).
            canary_handled = False
            try:
                canary_handled = _emit_fx_alert_canary(changes)
            except Exception:
                logger.exception("FX alert canary 실패 (격리)", extra={"bank": "investing"})
            try:
                sent_count = process_rate_alerts(db, changed_rates, canary_handled=canary_handled)
                if sent_count > 0:
                    logger.info(f"🔔 {sent_count}건 알림 발송 완료", extra={"bank": "investing", "sent": sent_count})
            except Exception as e:
                logger.exception("알림 처리 중 예외 발생", extra={"bank": "investing", "error": str(e)})

        # fanout step 4 S4: FX alert shadow (telemetry-only, flag-gated, 격리). canary는 위 B1 reorder로 이동.
        try:
            _emit_fx_alert_shadow(changes)
        except Exception:
            logger.exception("FX alert shadow 실패 (격리)", extra={"bank": "investing"})

        # topic trigger — APPLIED subset만 (axis #2 SET-only gating)
        try:
            _emit_topic_triggers(applied_updates)
        except Exception:
            logger.exception("topic trigger emission 실패 (격리)", extra={"bank": "investing"})

    elif current_rates:
        logger.debug("✋ 모든 Investing 환율 확인 완료 - 변경사항 없음", extra={"bank": "investing"})
    else:
        logger.warning("🈚️ Investing 환율 데이터 없음", extra={"bank": "investing", "source": "crud"})

    return new_records_count


def insert_investing_rates_into_db(db: Session, current_rates: dict) -> int:
    """Investing 환율 DB 저장 + Redis direct write + 알림 조건 체크.

    PR B(β observation fanout 1단계): staging → orchestration 분리. bank writer와 동일
    구조 — 직렬 순서(commit→Redis→alert) + signature + count==0 게이트 + sink dict shape
    불변. bank 값은 "investing"으로 통일.

    Returns:
        변경된 레코드 개수 (0: 변경 없음, N: N개 변경됨)
    """
    logger.info("|                Investing 환율                 |", extra={"bank": "investing"})

    # P1b A2-2/C6-5b-3c: write-mode gate (bank과 동일 3-way). banner 직후·staging 전.
    #   atomic → 별 분기 _insert_investing_rates_atomic (bank 대칭) / halt(미지값) → skip+return 0 / legacy 불변.
    # Bug-fix(incident 2026-06-21): write-mode 미확정(_INITIAL)엔 legacy write 금지(v2 downgrade 방지, bank 대칭).
    if not atomic_write_runtime.is_initialized():
        _record_write_mode_skip("investing", "uninitialized")
        return 0
    enforced = atomic_write_runtime.snapshot().enforced_action
    if enforced == WriterMode.ATOMIC:
        return _insert_investing_rates_atomic(db, current_rates)
    if enforced != WriterMode.LEGACY:
        _record_write_mode_skip("investing", enforced)
        return 0

    staged = _stage_investing_rate_changes(db, current_rates)
    new_records_count = len(staged)

    if new_records_count > 0:
        changes = [sc.change for sc in staged]  # legacy: ChangedRate 추출 (관측 동작 불변)
        # sink payload 변환은 commit 전에 — 구 staging-loop 변환과 동일 failure boundary
        # (to_kst_isoformat 실패 시 commit 전 차단 → DB 미커밋 보존, behavior-change-0).
        redis_updates = _changes_to_redis_updates(changes)
        changed_rates = _changes_to_fcm(changes)

        db.commit()
        logger.info(f"🎉 총 {new_records_count}개 Investing의 새로운 환율 데이터 저장 완료", extra={"count": new_records_count, "bank": "investing"})

        # commit 성공 후 Redis direct write — broadcast hot path latency 단축
        # (PR C) 반환된 SET 성공분만 topic trigger 대상 (SET-only gating, axis #2).
        redis_succeeded = _write_changed_investing_rates_to_redis(redis_updates)

        # 알림 조건 체크 및 FCM 발송 (실패해도 환율 저장에 영향 없음)
        # alert는 DB-authoritative — Redis SET 성공 여부 무관, 전 change 발화 (axis #3).
        if changed_rates:
            # §6.1 B1: canary enqueue 먼저 — 성공(canary_handled) 시에만 legacy가 allowlist skip
            # (enqueue 실패/예외/flag off → False → legacy fallback, no miss).
            canary_handled = False
            try:
                canary_handled = _emit_fx_alert_canary(changes)
            except Exception:
                logger.exception("FX alert canary 실패 (격리)", extra={"bank": "investing"})
            try:
                sent_count = process_rate_alerts(db, changed_rates, canary_handled=canary_handled)
                if sent_count > 0:
                    logger.info(f"🔔 {sent_count}건 알림 발송 완료", extra={"bank": "investing", "sent": sent_count})
            except Exception as e:
                logger.exception("알림 처리 중 예외 발생", extra={"bank": "investing", "error": str(e)})

        # fanout step 4 S4: FX alert shadow (telemetry-only, flag-gated, 격리). canary는 위 B1 reorder로 이동.
        try:
            _emit_fx_alert_shadow(changes)
        except Exception:
            logger.exception("FX alert shadow 실패 (격리)", extra={"bank": "investing"})

        # (PR C §6.6.2) topic trigger emission — SET 성공분만, mode-gated, bridge 경유.
        try:
            _emit_topic_triggers(redis_succeeded)
        except Exception:
            logger.exception("topic trigger emission 실패 (격리)", extra={"bank": "investing"})

    elif current_rates:
        logger.debug("✋ 모든 Investing 환율 확인 완료 - 변경사항 없음", extra={"bank": "investing"})
    else:
        logger.warning("🈚️ Investing 환율 데이터 없음", extra={"bank": "investing", "source": "crud"})

    return new_records_count


def select_a_latest_investing_rate_from_db(db: Session, pair: str) -> Optional[Dict[str, Any]]:
    """
    특정 통화쌍의 Investing.com 최신 환율 조회
    
    Args:
        db: 데이터베이스 세션
        pair: 통화쌍 (예: 'usd-krw')
        
    Returns:
        환율 정보 딕셔너리 (데이터가 없으면 None 반환)
    """
    record = (
        db.query(models.InvestingExchangeRate)
        .filter(models.InvestingExchangeRate.currency == pair)
        .order_by(models.InvestingExchangeRate.timestamp.desc(), models.InvestingExchangeRate.id.desc())
        .first()
    )

    if record:
        return {
            "currency": record.currency,
            "bank": "investing",
            "rate": record.rate,
            "timestamp": to_kst_isoformat(record.timestamp)
        }
    else:
        return None


def select_latest_bank_rates_from_db(db: Session, pair: str) -> List[Dict[str, Any]]:
    """
    특정 통화쌍의 모든 은행 최신 환율 조회 (은행 표시순으로 정렬)
    
    Args:
        db: 데이터베이스 세션
        pair: 통화쌍 (예: 'usd-krw')
        
    Returns:
        각 은행의 최신 pair 환율 데이터 리스트.
        BANK_DISPLAY_ORDER 순서이며 미등록 은행은 뒤쪽 + bank 코드순 fallback.
    """
    
    # 각 은행별 최신 1건을 결정적으로 선택 (window function)
    # timestamp DESC, id DESC로 동일 초 중복 방지
    ranked = (
        db.query(
            models.BankExchangeRate.id,
            func.row_number().over(
                partition_by=models.BankExchangeRate.bank,
                order_by=[
                    models.BankExchangeRate.timestamp.desc(),
                    models.BankExchangeRate.id.desc()
                ]
            ).label("rn")
        )
        .filter(models.BankExchangeRate.currency == pair)
        .subquery()
    )

    # rn=1 (각 은행의 최신 1건)만 조회
    records = (
        db.query(models.BankExchangeRate)
        .join(ranked, and_(
            models.BankExchangeRate.id == ranked.c.id,
            ranked.c.rn == 1
        ))
        .all()
    )
    records.sort(key=lambda record: _bank_display_sort_key(record.bank))

    return [
        {
            "currency": record.currency,
            "bank": record.bank,
            "rate": record.rate,
            "timestamp": to_kst_isoformat(record.timestamp)
        }
        for record in records
    ]


# ---------------------------------------------------------------------------
# P1b A2-4 — internal revision-aware selector (§16, dormant, caller 0)
#
# mirror/bootstrap source가 direct write(flush-row-ref)와 **같은 revision**을 산출하도록
# 하는 내부 selector. 공개 selector(select_a_latest_investing_rate_from_db /
# select_latest_bank_rates_from_db)는 **무변경** — dict shape에 id/revision 미노출 유지.
# 반환은 ORM row가 아니라 RevisionedRate(plain DTO, raw timestamp + revision).
# wiring(mirror/bootstrap 호출)은 C6 — A2-4는 정의만, behavior-change-0.
# ---------------------------------------------------------------------------


def _select_latest_investing_rate_with_revision(
    db: Session, pair: str
) -> Optional[atomic_revision.RevisionedRate]:
    """투자 최신 1건 → RevisionedRate (공개 select_a_latest_investing_rate_from_db의 revision 변형).

    tie-break(`timestamp DESC, id DESC`)은 공개 selector와 동일 — 같은 row 선택 보장.
    """
    record = (
        db.query(models.InvestingExchangeRate)
        .filter(models.InvestingExchangeRate.currency == pair)
        .order_by(models.InvestingExchangeRate.timestamp.desc(), models.InvestingExchangeRate.id.desc())
        .first()
    )
    if record is None:
        return None
    return atomic_revision.RevisionedRate(
        source="investing",
        asset=record.currency,
        rate=record.rate,
        timestamp=record.timestamp,
        revision=atomic_revision.revision_from_row(record),
    )


def _select_latest_bank_rates_with_revision(
    db: Session, pair: str
) -> List[atomic_revision.RevisionedRate]:
    """은행별 최신 1건 → RevisionedRate 리스트 (공개 select_latest_bank_rates_from_db의 revision 변형).

    window function(`partition_by=bank, order_by=timestamp DESC, id DESC` → rn=1)은 공개
    selector와 동일 — 각 은행 같은 row 선택 보장. 표시순 정렬은 mirror/bootstrap 비관여라 생략.
    """
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
        .filter(models.BankExchangeRate.currency == pair)
        .subquery()
    )
    records = (
        db.query(models.BankExchangeRate)
        .join(ranked, and_(models.BankExchangeRate.id == ranked.c.id, ranked.c.rn == 1))
        .all()
    )
    return [
        atomic_revision.RevisionedRate(
            source=record.bank,
            asset=record.currency,
            rate=record.rate,
            timestamp=record.timestamp,
            revision=atomic_revision.revision_from_row(record),
        )
        for record in records
    ]


def get_all_rates_flat(db: Session) -> List[Dict[str, Any]]:
    """
    모든 환율을 플랫 배열 구조로 반환하는 함수 (모바일/AJAX용)

    Args:
        db: 데이터베이스 세션

    Returns:
        모든 환율 데이터가 플랫 배열로 구성된 리스트 (ISO 8601 타임스탬프 포함)
    """
    all_rates = []

    pairs = SUPPORTED_CURRENCY_PAIRS

    for pair in pairs:
        # Investing 데이터 추가
        investing_data = select_a_latest_investing_rate_from_db(db=db, pair=pair)
        if investing_data:
            all_rates.append(investing_data)

        # 은행 데이터 추가
        bank_data = select_latest_bank_rates_from_db(db=db, pair=pair)
        all_rates.extend(bank_data)

    # USDT Phase 1: source_rates를 legacy shape로 변환해서 병합
    all_rates.extend(get_source_rates_as_legacy_format(db=db))

    return all_rates


def get_all_rates_flat_with_timings(db: Session) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """get_all_rates_flat의 분해 계측 변형. (rates, timings) tuple을 반환한다.

    payload_build_ms 내부에서 어느 단계가 spike를 만드는지 식별하기 위한 일시적 계측.
    동작 결과(rates)는 get_all_rates_flat과 동일하다.

    Returns:
        all_rates: get_all_rates_flat과 동일한 플랫 배열
        timings: {
            investing_total_ms: 모든 pair의 investing 조회 합계
            bank_total_ms: 모든 pair의 bank 조회 합계
            source_rates_legacy_ms: source_rates → legacy shape 변환 시간
            pair_timings: per-pair (investing+bank) 시간 dict
            pair_count, investing_rows, bank_rows, source_rows
        }
    """
    all_rates: List[Dict[str, Any]] = []
    pairs = SUPPORTED_CURRENCY_PAIRS

    pair_timings: Dict[str, float] = {}
    investing_total = 0.0
    bank_total = 0.0
    investing_rows = 0
    bank_rows = 0

    for pair in pairs:
        pair_t0 = time.perf_counter()

        t0 = time.perf_counter()
        investing_data = select_a_latest_investing_rate_from_db(db=db, pair=pair)
        investing_total += time.perf_counter() - t0
        if investing_data:
            all_rates.append(investing_data)
            investing_rows += 1

        t1 = time.perf_counter()
        bank_data = select_latest_bank_rates_from_db(db=db, pair=pair)
        bank_total += time.perf_counter() - t1
        all_rates.extend(bank_data)
        bank_rows += len(bank_data)

        pair_timings[pair] = round((time.perf_counter() - pair_t0) * 1000, 2)

    t2 = time.perf_counter()
    source_rates = get_source_rates_as_legacy_format(db=db)
    source_rates_legacy_ms = (time.perf_counter() - t2) * 1000
    all_rates.extend(source_rates)

    timings = {
        "investing_total_ms": round(investing_total * 1000, 2),
        "bank_total_ms": round(bank_total * 1000, 2),
        "source_rates_legacy_ms": round(source_rates_legacy_ms, 2),
        "pair_timings": pair_timings,
        "pair_count": len(pairs),
        "investing_rows": investing_rows,
        "bank_rows": bank_rows,
        "source_rows": len(source_rates),
    }
    return all_rates, timings


def get_rates_by_currency(db: Session, currency: str) -> List[Dict[str, Any]]:
    """
    특정 통화쌍의 모든 환율을 플랫 배열로 반환

    Args:
        db: 데이터베이스 세션
        currency: 통화쌍 (예: 'usd-krw', 'usdt-krw')

    Returns:
        해당 통화쌍의 모든 환율 데이터 (ISO 8601 타임스탬프 포함)
    """
    rates = []

    # Investing 데이터
    investing_data = select_a_latest_investing_rate_from_db(db=db, pair=currency)
    if investing_data:
        rates.append(investing_data)

    # 은행 데이터
    bank_data = select_latest_bank_rates_from_db(db=db, pair=currency)
    rates.extend(bank_data)

    # USDT Phase 1: source_rates에서 asset=currency 엔트리도 포함 (usdt-krw 등)
    source_data = get_source_rates_as_legacy_format(db=db, asset=currency)
    rates.extend(source_data)

    return rates


# 추가 유틸리티 함수들


def delete_old_bank_data(db: Session, days: int = 30) -> int:
    """
    지정된 일수 이상 지난 은행 환율 데이터 삭제

    Args:
        db: 데이터베이스 세션
        days: 보관할 일수 (기본값: 30일)

    Returns:
        삭제된 레코드 개수
    """
    cutoff_date = models.get_utc_now() - timedelta(days=days)

    deleted_count = db.query(models.BankExchangeRate).filter(
        models.BankExchangeRate.timestamp < cutoff_date
    ).delete()

    db.commit()

    # PostgreSQL은 autovacuum이 공간 회수를 자동 처리 (SQLite 시절 수동 VACUUM 제거)
    return deleted_count


def has_changes_since(db: Session, since_time: Optional[datetime]) -> bool:
    """
    마지막 시간 이후 변경된 레코드가 있는지 확인 (초경량 쿼리)

    Args:
        db: 데이터베이스 세션
        since_time: 마지막 체크 시간 (None이면 항상 True 반환)

    Returns:
        변경사항 있으면 True, 없으면 False
    """
    if since_time is None:
        return True

    # 은행 환율 변경 체크
    bank_count = db.query(models.BankExchangeRate).filter(
        models.BankExchangeRate.timestamp > since_time
    ).count()

    # 인베스팅 환율 변경 체크
    investing_count = db.query(models.InvestingExchangeRate).filter(
        models.InvestingExchangeRate.timestamp > since_time
    ).count()

    return (bank_count + investing_count) > 0


# ═════════════════════════════════════════════════════════════
# 시장 지수 (DXY 등) CRUD — Phase B
# ═════════════════════════════════════════════════════════════

def insert_market_index_rate_into_db(
    db: Session,
    *,
    instrument: str,
    rate: float,
    source: str,
    granularity: str = "realtime",
) -> bool:
    """
    시장 지수 DB 저장 (변경 시에만 INSERT)

    Args:
        db: 데이터베이스 세션
        instrument: 지수 식별자 ('dxy' | 'dxy_futures')
        rate: 지수 값 (예: 104.52)
        source: 데이터 소스 ('investing' | 'yahoo')
        granularity: 저장 해상도 ('realtime' | 'hourly' | 'daily')

    Returns:
        True: 새 레코드 저장됨, False: 변경 없음
    """
    last_record = (
        db.query(models.MarketIndexRate)
        .filter(
            models.MarketIndexRate.instrument == instrument,
            models.MarketIndexRate.granularity == granularity,
        )
        .order_by(models.MarketIndexRate.timestamp.desc(), models.MarketIndexRate.id.desc())
        .first()
    )

    if last_record is None:
        logger.info(f"⭐️ [{instrument} 신규] {rate:.3f} (source={source})", extra={"instrument": instrument, "rate": rate, "source": source, "type": "new"})
    elif last_record.rate != rate:
        logger.info(f"⚡️ [{instrument} 변경] {last_record.rate:.3f} → {rate:.3f} (source={source})", extra={"instrument": instrument, "old_rate": last_record.rate, "new_rate": rate, "source": source, "type": "change"})
    elif last_record.source != source:
        # 값은 같지만 소스 전환 (예: yahoo→investing 복구) → 새 레코드 필요
        logger.info(f"🔄 [{instrument} 소스 전환] {last_record.source} → {source} (rate={rate:.3f})", extra={"instrument": instrument, "rate": rate, "old_source": last_record.source, "new_source": source, "type": "source_change"})
    else:
        logger.debug(f"📼 [{instrument} 유지] {rate:.3f} (source={source})", extra={"instrument": instrument, "rate": rate, "source": source, "type": "unchanged"})
        return False

    new_entry = models.MarketIndexRate(
        instrument=instrument,
        source=source,
        rate=rate,
        timestamp=models.get_utc_now(),
        granularity=granularity,
    )
    db.add(new_entry)
    db.commit()
    return True


def insert_dxy_rate_into_db(db: Session, rate: float, source: str) -> bool:
    """
    DXY(미국 달러지수) DB 저장 (변경 시에만 INSERT).

    현물/운영 DXY는 instrument='dxy'로 저장한다.
    미국달러지수 선물은 insert_market_index_rate_into_db(..., instrument='dxy_futures')를 사용한다.
    """
    return insert_market_index_rate_into_db(
        db=db,
        instrument="dxy",
        rate=rate,
        source=source,
    )


def get_latest_dxy_rate(db: Session) -> Optional[Dict[str, Any]]:
    """
    최신 DXY 값 조회 (investing 최우선, cnbc 차순위, yahoo 최후)

    동일 timestamp에 여러 source가 있으면 investing > cnbc > yahoo 순서로 선택.
    source 우선순위: investing > cnbc > yahoo (CASE WHEN 정렬, ADR-025)

    Returns:
        {"instrument": "dxy", "rate": 104.52, "source": "investing",
         "timestamp": "2026-03-10T14:30:00+09:00"} 또는 None
    """
    from sqlalchemy import case

    record = (
        db.query(models.MarketIndexRate)
        .filter(
            models.MarketIndexRate.instrument == "dxy",
            models.MarketIndexRate.granularity == "realtime",
        )
        .order_by(
            models.MarketIndexRate.timestamp.desc(),
            case(
                (models.MarketIndexRate.source == "investing", 0),
                (models.MarketIndexRate.source == "cnbc", 1),
                else_=2
            ),
            models.MarketIndexRate.id.desc()
        )
        .first()
    )

    if record:
        return {
            "instrument": record.instrument,
            "rate": record.rate,
            "source": record.source,
            "timestamp": to_kst_isoformat(record.timestamp)
        }
    return None


def get_dxy_rates_for_period(
    db: Session,
    start_time: datetime,
    end_time: Optional[datetime] = None,
    granularities: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    기간별 DXY 데이터 조회 (그래프용, 2-part merge 전략)

    graph_cache.build_period_dxy_series()와 동일한 정책:
      - 과거 구간(오늘 UTC 00:00 이전): backfill granularity만 (daily/hourly)
      - 오늘 구간(UTC 00:00 이후):
        · 1w(hourly): timestamp 단위 realtime > hourly 선택 (hourly 보존)
        · 3m/1y(daily): 날짜 단위 배타적 선택 (realtime 있으면 daily 제외)
      → 1w는 hourly 데이터 활용, 3m/1y는 daily 00:00 오염 방지

    1일 그래프(granularities=["realtime"])는 분리 없이 realtime만 조회.

    동일 timestamp에 investing과 yahoo가 모두 존재하면
    investing만 사용 (ROW_NUMBER 윈도우 함수).

    Args:
        db: 데이터베이스 세션
        start_time: 조회 시작 시간 (UTC)
        end_time: 조회 종료 시간 (UTC, None이면 현재)
        granularities: 조회할 granularity 목록 (기본: ["realtime"])
            - 1일 그래프: ["realtime"]
            - 1주 그래프: ["realtime", "hourly"]
            - 3달/1년 그래프: ["realtime", "daily"]

    Returns:
        [{"rate": 104.52, "source": "investing",
          "timestamp": "2026-03-10T14:30:00+09:00"}, ...]
        timestamp ASC 정렬 (그래프 시계열)
    """
    from sqlalchemy import case

    if end_time is None:
        end_time = models.get_utc_now()

    if granularities is None:
        granularities = ["realtime"]

    backfill_grans = [g for g in granularities if g != "realtime"]
    has_realtime = "realtime" in granularities

    # 1일 그래프(realtime만) — 분리 불필요, 단일 쿼리
    if not backfill_grans:
        return _dxy_query_single(db, start_time, end_time, granularities)

    # 장기 그래프 — 2-part merge
    today_utc = end_time.replace(hour=0, minute=0, second=0, microsecond=0)

    all_records = []

    # Part 1: 과거 구간 — backfill granularity만 (daily/hourly)
    if start_time < today_utc:
        past_end = min(today_utc, end_time)
        all_records.extend(
            _dxy_query_single(db, start_time, past_end, backfill_grans, past_exclusive_end=True)
        )

    # Part 2: 오늘 구간
    # 1w(hourly backfill): timestamp 단위 realtime > hourly 선택 (hourly 보존)
    # 3m/1y(daily backfill): 날짜 단위 배타적 선택 (daily 00:00 오염 방지)
    if end_time >= today_utc:
        use_daily_exclusion = "daily" in backfill_grans

        if use_daily_exclusion and has_realtime:
            # 3m/1y: realtime 있으면 daily 전체 제외
            has_rt = db.query(models.MarketIndexRate.id).filter(
                models.MarketIndexRate.instrument == "dxy",
                models.MarketIndexRate.granularity == "realtime",
                models.MarketIndexRate.timestamp >= today_utc,
                models.MarketIndexRate.timestamp <= end_time,
            ).first() is not None

            today_grans = ["realtime"] if has_rt else backfill_grans
            all_records.extend(
                _dxy_query_single(db, today_utc, end_time, today_grans)
            )
        elif not use_daily_exclusion and has_realtime:
            # 1w: realtime + hourly 공존, timestamp 단위 realtime > hourly 선택
            today_grans = ["realtime"] + backfill_grans
            all_records.extend(
                _dxy_query_single(
                    db, today_utc, end_time, today_grans,
                    dedup_across_granularities=True,
                )
            )
        else:
            all_records.extend(
                _dxy_query_single(db, today_utc, end_time, backfill_grans)
            )

    return all_records


def _dxy_query_single(
    db: Session,
    start_time: datetime,
    end_time: datetime,
    granularities: List[str],
    past_exclusive_end: bool = False,
    dedup_across_granularities: bool = False,
) -> List[Dict[str, Any]]:
    """
    DXY 단일 구간 쿼리. source dedup (investing 우선).

    Args:
        dedup_across_granularities: True면 같은 timestamp의 다른 granularity도 경쟁
            (1w 오늘 구간: realtime > hourly timestamp 단위 선택)
            False면 같은 (timestamp, granularity) 내에서만 source dedup
    """
    from sqlalchemy import case

    source_priority = case(
        (models.MarketIndexRate.source == "investing", 0),
        (models.MarketIndexRate.source == "cnbc", 1),
        else_=2
    )

    end_op = models.MarketIndexRate.timestamp < end_time if past_exclusive_end \
        else models.MarketIndexRate.timestamp <= end_time

    if dedup_across_granularities:
        # 1w 오늘: PARTITION BY timestamp만, granularity 우선순위 포함
        gran_priority = case(
            (models.MarketIndexRate.granularity == "realtime", 0),
            (models.MarketIndexRate.granularity == "hourly", 1),
            (models.MarketIndexRate.granularity == "daily", 2),
            else_=3
        )
        partition_cols = [models.MarketIndexRate.timestamp]
        order_cols = [gran_priority, source_priority, models.MarketIndexRate.id.desc()]
    else:
        # 기본: PARTITION BY (timestamp, granularity), source만 dedup
        partition_cols = [
            models.MarketIndexRate.timestamp,
            models.MarketIndexRate.granularity,
        ]
        order_cols = [source_priority, models.MarketIndexRate.id.desc()]

    ranked = (
        db.query(
            models.MarketIndexRate.id,
            func.row_number().over(
                partition_by=partition_cols,
                order_by=order_cols,
            ).label("rn")
        )
        .filter(
            models.MarketIndexRate.instrument == "dxy",
            models.MarketIndexRate.granularity.in_(granularities),
            models.MarketIndexRate.timestamp >= start_time,
            end_op,
        )
        .subquery()
    )

    records = (
        db.query(models.MarketIndexRate)
        .join(ranked, and_(
            models.MarketIndexRate.id == ranked.c.id,
            ranked.c.rn == 1
        ))
        .order_by(models.MarketIndexRate.timestamp.asc())
        .all()
    )

    return [
        {
            "rate": record.rate,
            "source": record.source,
            "timestamp": to_kst_isoformat(record.timestamp)
        }
        for record in records
    ]


# SELECT * FROM investing_exchange_rates;

# 모든 데이터 삭제 (SQLite, AUTOINCREMENT 초기화 X)
# DELETE FROM table_name;
# UPDATE SQLITE_SEQUENCE SET seq = 0 WHERE name = 'table_name';

# 모든 데이터 삭제 (빠르고 효율적) - 사이즈클때
# TRUNCATE TABLE table_name;


# 테이블 구조, 데이터 모두 삭제
# DROP TABLE table_name;
# ⬇️
# 재 생성
# CREATE TABLE IF NOT EXISTS investing_exchange_rates (
#     id INTEGER PRIMARY KEY AUTOINCREMENT,
#     currency TEXT NOT NULL,
#     rate REAL,
#     timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
# )               
# """);

# 대량의 테이블 데이터 삭제 후, DB 재 정렬
# VACUUM;


# ═════════════════════════════════════════════════════════════
# 크롤러 설정 관리 (Phase 1.8)
# ═════════════════════════════════════════════════════════════

def init_crawler_config(db: Session) -> None:
    """
    crawler_config 테이블 초기화 (서버 시작 시 1회 실행)

    Notes:
        - 모든 크롤러를 enabled=True로 초기화
        - 이미 존재하는 경우 skip (멱등성 보장)
    """
    # 모든 크롤러 이름 정의
    CRAWLER_NAMES = [
        'investing', 'kb', 'hana', 'shinhan', 'woori',
        'ibk', 'nh', 'sc', 'bs', 'citi', 'dxy'
    ]

    for crawler_name in CRAWLER_NAMES:
        # 이미 존재하는지 확인
        existing = db.query(models.CrawlerConfig).filter(
            models.CrawlerConfig.crawler_name == crawler_name
        ).first()

        if not existing:
            # 신규 생성 (기본값: enabled=True)
            config = models.CrawlerConfig(
                crawler_name=crawler_name,
                enabled=True,
                updated_at=models.get_utc_now()
            )
            db.add(config)
            logger.info(f"✅ 크롤러 설정 초기화: {crawler_name} (enabled=True)")

    # 레거시 크롤러 행 삭제 (admin UI 잔존 방지)
    RETIRED_CRAWLERS = []
    for retired_name in RETIRED_CRAWLERS:
        stale = db.query(models.CrawlerConfig).filter(
            models.CrawlerConfig.crawler_name == retired_name
        ).first()
        if stale:
            logger.info(f"🗑️ 레거시 크롤러 설정 삭제: {stale.crawler_name}")
            db.delete(stale)

    db.commit()
    logger.info("✅ crawler_config 테이블 초기화 완료")


def get_all_crawler_configs(db: Session) -> List[Dict[str, Any]]:
    """
    모든 크롤러 설정 조회 (관리자 API용)

    Returns:
        [
            {
                "crawler_name": "investing",
                "enabled": True,
                "updated_at": "2025-11-27T10:30:00+09:00"
            },
            ...
        ]
    """
    configs = db.query(models.CrawlerConfig).order_by(models.CrawlerConfig.crawler_name).all()

    return [
        {
            "crawler_name": config.crawler_name,
            "enabled": config.enabled,
            "updated_at": to_kst_isoformat(config.updated_at)
        }
        for config in configs
    ]


def update_crawler_config(db: Session, crawler_name: str, enabled: bool) -> bool:
    """
    크롤러 설정 업데이트 (토글 API용)

    Args:
        db: 데이터베이스 세션
        crawler_name: 크롤러 이름
        enabled: 활성화 상태

    Returns:
        성공 시 True, 실패 시 False

    Raises:
        ValueError: 존재하지 않는 크롤러 이름
    """
    config = db.query(models.CrawlerConfig).filter(
        models.CrawlerConfig.crawler_name == crawler_name
    ).first()

    if not config:
        raise ValueError(f"Invalid crawler name: {crawler_name}")

    config.enabled = enabled
    config.updated_at = models.get_utc_now()

    db.commit()

    action = "활성화" if enabled else "비활성화"
    logger.info(
        f"✅ 크롤러 설정 업데이트: {crawler_name} → {action}",
        extra={"crawler": crawler_name, "enabled": enabled}
    )

    return True


# ═════════════════════════════════════════════════════════════
# Phase 2: Firebase Auth + FCM 알림 CRUD
# ═════════════════════════════════════════════════════════════

def register_device(
    db: Session,
    user_id: str,
    device_token: str,
    platform: str
) -> models.UserDevice:
    """
    사용자 기기 등록 (FCM Device Token) - UPSERT 방식

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id
        device_token: FCM Device Token
        platform: 'ios' or 'android'

    Returns:
        UserDevice 객체

    Notes:
        - device_token은 전역 유니크 (한 토큰 = 한 사용자)
        - UPSERT: 토큰 존재 시 user_id/platform/updated_at 갱신
        - 다른 계정으로 로그인 시 자동으로 소유권 이전
    """
    # DB Dialect에 맞는 UPSERT 선택
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        raise RuntimeError(f"지원하지 않는 DB dialect: {dialect_name}")

    try:
        # 소유권 이전 로깅용: 기존 소유자 확인
        existing = db.query(models.UserDevice).filter(
            models.UserDevice.device_token == device_token
        ).first()

        transferred_from = None
        if existing and existing.user_id != user_id:
            transferred_from = existing.user_id

        # UPSERT: INSERT OR UPDATE on device_token conflict
        now = models.get_utc_now()
        stmt = dialect_insert(models.UserDevice).values(
            user_id=user_id,
            device_token=device_token,
            platform=platform,
            created_at=now,
            updated_at=now
        )

        stmt = stmt.on_conflict_do_update(
            index_elements=['device_token'],
            set_={
                'user_id': user_id,
                'platform': platform,
                'updated_at': now
            }
        )

        db.execute(stmt)
        db.commit()

        # 결과 조회
        device = db.query(models.UserDevice).filter(
            models.UserDevice.device_token == device_token
        ).first()

        # 로깅
        if transferred_from:
            logger.warning(
                "토큰 소유권 이전",
                extra={
                    "old_user": transferred_from[:8] + "...",
                    "new_user": user_id[:8] + "...",
                    "token": device_token[:20] + "..."
                }
            )
            logger.info(
                "기기 토큰 업데이트 (소유권 이전)",
                extra={"user_id": user_id[:8] + "...", "platform": platform}
            )
        elif existing:
            logger.info(
                "기기 토큰 업데이트",
                extra={"user_id": user_id[:8] + "...", "platform": platform}
            )
        else:
            logger.info(
                "새 기기 등록",
                extra={"user_id": user_id[:8] + "...", "platform": platform, "device_id": device.id}
            )

        return device

    except Exception as e:
        db.rollback()
        logger.error("기기 등록 실패", exc_info=True)
        raise


def delete_device(db: Session, user_id: str, device_token: str) -> bool:
    """
    사용자 기기 삭제

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id
        device_token: FCM Device Token

    Returns:
        삭제 성공 시 True, 해당 레코드 없으면 False
    """
    deleted = db.query(models.UserDevice).filter(
        models.UserDevice.user_id == user_id,
        models.UserDevice.device_token == device_token
    ).delete()

    db.commit()

    if deleted:
        logger.info(
            "기기 삭제",
            extra={"user_id": user_id[:8] + "...", "deleted": deleted}
        )
    return deleted > 0


def delete_devices_by_token(db: Session, device_token: str) -> int:
    """
    무효 토큰 삭제 (FCM 발송 실패 시 호출)

    Args:
        db: 데이터베이스 세션
        device_token: 무효화된 FCM Device Token

    Returns:
        삭제된 레코드 개수
    """
    deleted = db.query(models.UserDevice).filter(
        models.UserDevice.device_token == device_token
    ).delete()

    db.commit()

    if deleted:
        logger.warning(
            "무효 토큰 삭제",
            extra={"token": device_token[:20] + "...", "deleted": deleted}
        )
    return deleted


def get_devices_by_user(db: Session, user_id: str) -> List[models.UserDevice]:
    """
    사용자의 모든 기기 조회

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id

    Returns:
        UserDevice 객체 리스트
    """
    return db.query(models.UserDevice).filter(
        models.UserDevice.user_id == user_id
    ).all()


# ─────────────────────────────────────────────────────────────
# NotificationSetting CRUD
# ─────────────────────────────────────────────────────────────

# B2 (ADR-036): PUT 부분 업데이트에서 repeat_interval_sec의 "미제공"과 "명시적 None(=once)"을
# 구분하기 위한 sentinel. None 자체가 once 설정 의미라 absent 표현에 쓸 수 없음.
# bank/source 양쪽 update 함수가 공유하므로 두 함수보다 먼저 단일 정의 (중복 정의 시 모듈 global
# 재바인딩으로 default-vs-body 비교가 어긋남).
_UNSET = object()


def create_notification_setting(
    db: Session,
    user_id: str,
    bank: str,
    currency: str,
    condition: str,
    threshold: float,
    is_enabled: bool = True,
    repeat_interval_sec: Optional[int] = None,  # B2 (ADR-036): NULL=once / 정수=초 간격
) -> models.NotificationSetting:
    """
    알림 설정 생성 (중복 방지)

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id
        bank: 은행 코드 (예: 'hana', 'kb')
        currency: 통화쌍 (예: 'usd-krw')
        condition: 'above' or 'below'
        threshold: 임계값
        is_enabled: 활성화 여부 (기본: True)

    Returns:
        NotificationSetting 객체

    Notes:
        - 같은 (user_id, bank, currency, condition, threshold) 조합이 있으면
          기존 설정을 업데이트하고 반환 (중복 푸시 방지)
        - is_enabled=True로 재활성화할 때만 triggered 초기화 (재알림 가능)
        - is_enabled=False면 triggered 유지 ("발송됨" 상태 보존)
    """
    # 중복 체크: 같은 조건의 알림 설정이 있는지 확인
    existing = db.query(models.NotificationSetting).filter(
        models.NotificationSetting.user_id == user_id,
        models.NotificationSetting.bank == bank,
        models.NotificationSetting.currency == currency,
        models.NotificationSetting.condition == condition,
        models.NotificationSetting.threshold == threshold
    ).first()

    if existing:
        # 기존 설정 업데이트
        existing.enabled = is_enabled
        existing.repeat_interval_sec = repeat_interval_sec  # B2: dedup 시 interval 갱신
        if is_enabled:
            # True로 재활성화할 때만 triggered 초기화 (재알림 가능)
            existing.triggered = False
            existing.last_notified_at = None
            existing.last_notified_rate = None
        # False면 triggered 유지 ("발송됨" 상태 보존)
        existing.updated_at = models.get_utc_now()
        db.commit()
        db.refresh(existing)
        action = "알림 설정 재활성화 (중복)" if is_enabled else "알림 설정 비활성화 (중복)"
        logger.info(
            action,
            extra={
                "user_id": user_id[:8] + "...",
                "setting_id": existing.id,
                "bank": bank,
                "currency": currency,
                "is_enabled": is_enabled,
                "triggered_reset": is_enabled
            }
        )
        return existing

    # 새 설정 생성
    setting = models.NotificationSetting(
        user_id=user_id,
        bank=bank,
        currency=currency,
        condition=condition,
        threshold=threshold,
        enabled=is_enabled,
        triggered=False,
        repeat_interval_sec=repeat_interval_sec,  # B2 (ADR-036)
    )
    db.add(setting)
    db.commit()
    db.refresh(setting)

    logger.info(
        "알림 설정 생성",
        extra={
            "user_id": user_id[:8] + "...",
            "bank": bank,
            "currency": currency,
            "condition": condition,
            "threshold": threshold,
            "is_enabled": is_enabled,
            "setting_id": setting.id
        }
    )
    return setting


def get_notification_settings(db: Session, user_id: str) -> List[models.NotificationSetting]:
    """
    사용자의 모든 알림 설정 조회

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id

    Returns:
        NotificationSetting 객체 리스트
    """
    return db.query(models.NotificationSetting).filter(
        models.NotificationSetting.user_id == user_id
    ).order_by(models.NotificationSetting.created_at.desc()).all()


def get_notification_setting_by_id(
    db: Session,
    setting_id: int,
    user_id: str
) -> Optional[models.NotificationSetting]:
    """
    특정 알림 설정 조회 (소유권 검증 포함)

    Args:
        db: 데이터베이스 세션
        setting_id: NotificationSetting ID
        user_id: Firebase Auth user_id (소유권 검증용)

    Returns:
        NotificationSetting 객체 (없거나 권한 없으면 None)
    """
    return db.query(models.NotificationSetting).filter(
        models.NotificationSetting.id == setting_id,
        models.NotificationSetting.user_id == user_id
    ).first()


def update_notification_setting(
    db: Session,
    setting_id: int,
    user_id: str,
    bank: Optional[str] = None,
    condition: Optional[str] = None,
    threshold: Optional[float] = None,
    enabled: Optional[bool] = None,
    repeat_interval_sec=_UNSET,  # B2 (ADR-036): sentinel=미제공 / None=once / 정수=repeat
) -> Optional[models.NotificationSetting]:
    """
    알림 설정 수정 (PUT - 부분 업데이트)

    Args:
        db: 데이터베이스 세션
        setting_id: NotificationSetting ID
        user_id: Firebase Auth user_id (소유권 검증용)
        bank: 은행 코드 (선택)
        condition: 조건 (선택)
        threshold: 임계값 (선택)
        enabled: 활성화 상태 (선택)

    Returns:
        수정된 NotificationSetting 객체 (없거나 권한 없으면 None)

    Notes:
        - bank, condition, threshold 중 실제로 값이 변경되면 triggered 초기화
        - enabled: False→True 전환 시에도 triggered 초기화
        - is_enabled는 기존 값 유지 (자동 활성화 안 함)
        - B2 (ADR-036): repeat_interval_sec 변경 시 §7 리셋 + once↔repeat 전환 시 §8 정규화
    """
    setting = get_notification_setting_by_id(db, setting_id, user_id)
    if not setting:
        return None

    # 재알림 조건: 실제 값이 변경될 때만 triggered 초기화
    should_reset_triggered = False

    # bank 변경 감지 및 적용
    if bank is not None:
        if bank != setting.bank:
            should_reset_triggered = True
            logger.debug(f"bank 변경: {setting.bank} → {bank}")
        setting.bank = bank

    # condition 변경 감지 및 적용
    if condition is not None:
        if condition != setting.condition:
            should_reset_triggered = True
            logger.debug(f"condition 변경: {setting.condition} → {condition}")
        setting.condition = condition

    # threshold 변경 감지 및 적용
    if threshold is not None:
        if threshold != setting.threshold:
            should_reset_triggered = True
            logger.debug(f"threshold 변경: {setting.threshold} → {threshold}")
        setting.threshold = threshold

    # enabled 변경 (토글)
    if enabled is not None:
        # False→True 전환 시 재알림 가능하도록
        if enabled and not setting.enabled:
            should_reset_triggered = True
        setting.enabled = enabled

    # B2 (ADR-036): repeat_interval_sec — sentinel=미제공 / None=once / 정수=repeat
    mode_transition = False
    if repeat_interval_sec is not _UNSET:
        if repeat_interval_sec != setting.repeat_interval_sec:
            should_reset_triggered = True  # §7: interval 변경 시 last_notified_at 리셋
            if (setting.repeat_interval_sec is None) != (repeat_interval_sec is None):
                mode_transition = True     # §8: once↔repeat 전환
        setting.repeat_interval_sec = repeat_interval_sec

    # triggered 초기화 (재알림 가능) - last_notified_* 도 함께 초기화
    if should_reset_triggered:
        setting.triggered = False
        setting.last_notified_at = None
        setting.last_notified_rate = None
        logger.info(
            "알림 설정 재활성화 (조건/interval 변경)",
            extra={"setting_id": setting_id, "triggered_reset": True}
        )

    # §8: 모드 전환은 새 모드 clean active 시작 — 이전 once 발사로 남은 enabled=false 해제.
    # 단 같은 PUT에서 사용자가 명시적으로 enabled=false를 주면 그 의도 존중.
    if mode_transition and enabled is not False:
        setting.enabled = True

    setting.updated_at = models.get_utc_now()
    db.commit()
    db.refresh(setting)

    logger.info(
        "알림 설정 수정",
        extra={
            "setting_id": setting_id,
            "bank": setting.bank,
            "condition": setting.condition,
            "threshold": setting.threshold,
            "enabled": setting.enabled,
            "triggered": setting.triggered
        }
    )
    return setting


def delete_notification_setting(db: Session, setting_id: int, user_id: str) -> bool:
    """
    알림 설정 삭제

    Args:
        db: 데이터베이스 세션
        setting_id: NotificationSetting ID
        user_id: Firebase Auth user_id (소유권 검증용)

    Returns:
        삭제 성공 시 True, 해당 레코드 없으면 False
    """
    deleted = db.query(models.NotificationSetting).filter(
        models.NotificationSetting.id == setting_id,
        models.NotificationSetting.user_id == user_id
    ).delete()

    db.commit()

    if deleted:
        logger.info(
            "알림 설정 삭제",
            extra={"setting_id": setting_id, "user_id": user_id[:8] + "..."}
        )
    return deleted > 0


# ─────────────────────────────────────────────────────────────
# FCM 알림 발송용 쿼리
# ─────────────────────────────────────────────────────────────

def get_triggered_settings_for_rate(
    db: Session,
    bank: str,
    currency: str,
    rate: float
) -> List[Dict[str, Any]]:
    """
    특정 환율에 대해 알림 조건이 충족된 설정 목록 조회

    Args:
        db: 데이터베이스 세션
        bank: 은행 코드
        currency: 통화쌍
        rate: 현재 환율

    Returns:
        [
            {
                "setting": NotificationSetting,
                "devices": [UserDevice, ...],
                "user_id": str
            },
            ...
        ]

    Notes:
        - N+1 쿼리 최적화: 2개 쿼리로 모든 데이터 조회
          1. 조건 충족 settings 조회
          2. 해당 user_ids의 모든 devices 일괄 조회
    """
    # 1. 활성화된 알림 설정 조회
    settings = db.query(models.NotificationSetting).filter(
        models.NotificationSetting.bank == bank,
        models.NotificationSetting.currency == currency,
        models.NotificationSetting.enabled == True,
        models.NotificationSetting.triggered == False  # 아직 발송 안 된 것만
    ).all()

    # 2. 조건 충족된 설정 필터링 + user_id 수집
    matched_settings = []
    user_ids = set()

    for setting in settings:
        condition_met = False
        if setting.condition == "above" and rate >= setting.threshold:
            condition_met = True
        elif setting.condition == "below" and rate <= setting.threshold:
            condition_met = True

        if condition_met:
            # B2 (ADR-036): repeat 모드 interval gate. query는 enabled+!triggered만 거르는데
            # repeat는 triggered 항상 false라 매 tick 통과 → last_notified_at+interval 미경과면 skip해
            # 반복 throttle. once는 query의 !triggered가 이미 gate(repeat_interval_sec None이라 미진입).
            # get_utc_now()=naive UTC, last_notified_at도 naive UTC라 뺄셈 안전(소스 aware-now 이슈 없음).
            # bank(get_triggered_settings_for_rate) + source-legacy(get_triggered_source_settings_for_rate)
            # 둘 다 동일 적용 — 후자는 REST polling 비활성이나 재활성 시 spam 방어.
            if setting.repeat_interval_sec is not None and setting.last_notified_at is not None:
                elapsed = (models.get_utc_now() - setting.last_notified_at).total_seconds()
                if elapsed < setting.repeat_interval_sec:
                    continue
            matched_settings.append(setting)
            user_ids.add(setting.user_id)

    if not matched_settings:
        return []

    # 3. 모든 user_ids의 devices 일괄 조회 (N+1 → 2 쿼리로 최적화)
    all_devices = db.query(models.UserDevice).filter(
        models.UserDevice.user_id.in_(user_ids)
    ).all()

    # 4. user_id → devices 매핑 생성
    devices_by_user: Dict[str, List[models.UserDevice]] = {}
    for device in all_devices:
        if device.user_id not in devices_by_user:
            devices_by_user[device.user_id] = []
        devices_by_user[device.user_id].append(device)

    # 5. 결과 생성 (devices 있는 것만)
    results = []
    for setting in matched_settings:
        devices = devices_by_user.get(setting.user_id, [])
        if devices:
            results.append({
                "setting": setting,
                "devices": devices,
                "user_id": setting.user_id
            })

    return results


def mark_setting_triggered(
    db: Session,
    setting_id: int,
    rate: float
) -> None:
    """
    알림 설정을 '발송됨'으로 표시 (멱등성 보장)

    Args:
        db: 데이터베이스 세션
        setting_id: NotificationSetting ID
        rate: 발송 시점의 환율
    """
    setting = db.query(models.NotificationSetting).filter(
        models.NotificationSetting.id == setting_id
    ).first()

    if setting:
        # B2 (ADR-036): mode 분기 — repeat는 enabled 유지 + triggered 미설정(once 종료 전용),
        # last_notified_at만 갱신해 gate가 다음 interval 판단. once는 현행(자동 비활성).
        if setting.repeat_interval_sec is None:
            setting.triggered = True
            setting.enabled = False  # once: 발송 후 자동 비활성화 (1회성)
        setting.last_notified_at = models.get_utc_now()
        setting.last_notified_rate = rate
        db.commit()

        logger.info(
            "알림 발송 완료",
            extra={
                "setting_id": setting_id, "rate": rate,
                "mode": "once" if setting.repeat_interval_sec is None else "repeat",
                "enabled": setting.enabled,
            },
        )


def create_notification_log(
    db: Session,
    user_id: str,
    setting_id: Optional[int],
    bank: str,
    currency: str,
    rate: float,
    success: bool,
    error_message: Optional[str] = None
) -> models.NotificationLog:
    """
    알림 발송 히스토리 기록

    Args:
        db: 데이터베이스 세션
        user_id: Firebase Auth user_id
        setting_id: NotificationSetting ID (선택)
        bank: 은행 코드
        currency: 통화쌍
        rate: 발송 시점의 환율
        success: 발송 성공 여부
        error_message: 에러 메시지 (선택)

    Returns:
        NotificationLog 객체
    """
    log = models.NotificationLog(
        user_id=user_id,
        setting_id=setting_id,
        bank=bank,
        currency=currency,
        rate=rate,
        success=success,
        error_message=error_message
    )
    db.add(log)
    db.commit()

    return log


# ═══════════════════════════════════════════════════════════════════════════════
# 환율 변경 시 알림 처리 (크롤러에서 호출)
# ═══════════════════════════════════════════════════════════════════════════════

# fanout step 4 S6b: legacy FX alert pre-mutation match baseline = **parity 기준선**.
# process_rate_alerts가 mark_setting_triggered(triggered=True + enabled=False)로 mutate하기 전
# 매칭 수(legacy가 실제 발사할 대상)를 (bank,currency)별 누적. telemetry-only(process-local, 재시작 reset).
# FX_ALERT_SHADOW_ENABLED 활성 시에만 기록(관찰 창 정렬). ⚠️ fx_alert_shadow matched_candidates는
# post-legacy async라 신뢰 불가(보조 진단) — parity는 이 baseline이 기준. accessor=/admin/api/fx-shadow-counts.
_fx_legacy_match_counts: Dict[Tuple[str, str], int] = {}
# worker thread(process_rate_alerts in to_thread) write ↔ event-loop(endpoint) read 경계 →
# lock 보호 (dict size 변경 중 dict() 복사 RuntimeError 방지, fx_alert_shadow 카운터와 동일 규율).
_fx_legacy_match_lock = threading.Lock()


def _record_fx_legacy_match(bank: str, currency: str, n: int) -> None:
    if n <= 0:
        return
    key = (bank, currency)
    with _fx_legacy_match_lock:
        _fx_legacy_match_counts[key] = _fx_legacy_match_counts.get(key, 0) + n


def get_fx_legacy_match_counts() -> Dict[Tuple[str, str], int]:
    """S6b baseline snapshot (read accessor, process-local, lock-guarded)."""
    with _fx_legacy_match_lock:
        return dict(_fx_legacy_match_counts)


def process_rate_alerts(
    db: Session,
    changed_rates: List[Dict[str, Any]],
    canary_handled: bool = False,
) -> int:
    """
    변경된 환율에 대해 알림 조건 체크 및 FCM 발송

    크롤러의 환율 저장 함수에서 호출됨.
    알림 발송 실패가 환율 저장에 영향을 주지 않도록 예외 처리.

    Args:
        db: 데이터베이스 세션
        changed_rates: 변경된 환율 목록
            [{"bank": "kb", "currency": "usd-krw", "rate": 1400.0}, ...]

    Returns:
        발송된 알림 수
    """
    # 비교알림 dual-trigger hook (ADR-037 Decision 3 — bank/investing 변경 leg).
    # sync crawler thread → emit 내부 schedule_on_loop 마샬링(best-effort). flag off면 zero-overhead.
    # 함수 내부 1곳 배선으로 전 호출부(bank atomic/legacy + investing) 커버.
    try:
        from app.notifications.comparison_evaluator import emit_comparison_observation
        for _cr in changed_rates:
            emit_comparison_observation(_cr["bank"], _cr["currency"])
    except Exception:
        logger.exception("comparison hook 실패 (격리)")

    # 순환 참조 방지를 위해 함수 내부에서 import
    from app.notifications.fcm import send_fcm_multicast_sync, init_firebase
    from app import config as app_config  # S6b baseline gate (FX_ALERT_SHADOW_ENABLED)

    if not changed_rates:
        return 0

    # Firebase 초기화 시도 (subprocess에서도 초기화 필요)
    if not init_firebase():
        logger.debug("Firebase 초기화 실패 - 알림 스킵")
        return 0

    sent_count = 0
    all_failed_tokens = []  # 일괄 삭제용 무효 토큰 수집

    for rate_info in changed_rates:
        bank = rate_info["bank"]
        currency = rate_info["currency"]
        rate = rate_info["rate"]

        try:
            # 조건 충족 설정 조회 (이미 N+1 최적화됨)
            triggered_items = get_triggered_settings_for_rate(db, bank, currency, rate)

            if not triggered_items:
                continue

            # S6b: legacy pre-mutation match baseline = parity 기준선 (mark_triggered 전 = legacy가
            # 실제 발사할 대상). telemetry-only, shadow 활성 창에만 기록(관찰 창 정렬).
            # ⚠️ B1: canary enqueue 성공(canary_handled) 시에만 allowlist setting을 legacy가 skip(아래) →
            # baseline에서도 그때만 제외해 "legacy 실제 발사분"만 카운트 (enqueue 실패=legacy fallback=포함).
            if app_config.FX_ALERT_SHADOW_ENABLED:
                legacy_fired = [
                    it for it in triggered_items
                    if not (canary_handled
                            and it["setting"].id in app_config.FX_ALERT_CUTOVER_CANARY_SETTING_IDS)
                ]
                _record_fx_legacy_match(bank, currency, len(legacy_fired))

            for item in triggered_items:
                setting = item["setting"]

                # §6.1 canary + B1: canary enqueue 성공(canary_handled)한 allowlist setting만 legacy skip
                # (canary가 real 발사). enqueue 실패/예외/flag off(canary_handled=False)면 legacy fallback
                # (no miss). 비-allowlist는 legacy 그대로.
                if (canary_handled
                        and setting.id in app_config.FX_ALERT_CUTOVER_CANARY_SETTING_IDS):
                    continue

                devices = item["devices"]
                user_id = item["user_id"]

                # 알림 메시지 생성
                bank_kr = BANK_NAMES_KR.get(bank, bank.upper())
                currency_kr = CURRENCY_NAMES_KR.get(currency, currency.upper())
                icon = "📈" if setting.condition == "above" else "📉"

                title = f"{icon}  {bank_kr}  {currency_kr}"

                condition_arrow = "↑" if setting.condition == "above" else "↓"
                condition_text = "이상" if setting.condition == "above" else "이하"
                threshold_str = format_threshold(setting.threshold)
                rate_str = f"{rate:.2f}"

                body = f"[ {threshold_str} {condition_arrow}{condition_text} 도달 ]   {rate_str}"

                # data payload (앱에서 처리용)
                # title/body 포함: 포그라운드에서도 동일한 메시지 표시 보장
                data = {
                    "type": "rate_alert",
                    "title": title,
                    "body": body,
                    "bank": bank,
                    "currency": currency,
                    "rate": str(rate),
                    "threshold": str(setting.threshold),
                    "condition": setting.condition,
                    "setting_id": str(setting.id),
                    # B2 (ADR-036): cross-device race 차단용 권위 플래그. 수신 단말이 stale 로컬 대신
                    # 이 값으로 toggle-off 결정 (repeat="true"면 비활성 금지). FCM data는 문자열만 허용.
                    "is_repeat": "true" if setting.repeat_interval_sec is not None else "false",
                }

                # FCM 발송
                tokens = [d.device_token for d in devices]
                result = send_fcm_multicast_sync(tokens, title, body, data)

                # 발송 결과 처리
                if result["success_count"] > 0:
                    # triggered 플래그 설정
                    mark_setting_triggered(db, setting.id, rate)
                    sent_count += 1

                    # 발송 로그 기록
                    create_notification_log(
                        db=db,
                        user_id=user_id,
                        setting_id=setting.id,
                        bank=bank,
                        currency=currency,
                        rate=rate,
                        success=True
                    )

                    logger.info(
                        "🔔 환율 알림 발송",
                        extra={
                            "event": "rate_alert_sent",
                            "bank": bank,
                            "currency": currency,
                            "rate": rate,
                            "threshold": setting.threshold,
                            "condition": setting.condition,
                            "user_id": user_id[:8] + "...",
                            "devices": len(devices),
                            "success": result["success_count"],
                        }
                    )

                # 무효 토큰 수집 (나중에 일괄 삭제)
                if result["failed_tokens"]:
                    all_failed_tokens.extend(result["failed_tokens"])

        except Exception as e:
            # 알림 처리 실패가 환율 저장에 영향 주지 않도록
            logger.exception(
                "알림 처리 실패",
                extra={
                    "bank": bank,
                    "currency": currency,
                    "rate": rate,
                    "error": str(e)
                }
            )
            continue

    # 무효 토큰 일괄 삭제 (N번 commit → 1번 commit으로 최적화)
    if all_failed_tokens:
        try:
            deleted_count = db.query(models.UserDevice).filter(
                models.UserDevice.device_token.in_(all_failed_tokens)
            ).delete(synchronize_session=False)
            db.commit()
            logger.info(
                "무효 토큰 일괄 삭제",
                extra={"count": deleted_count, "tokens": len(all_failed_tokens)}
            )
        except Exception as e:
            logger.exception("무효 토큰 삭제 실패", extra={"error": str(e)})

    return sent_count


# ═══════════════════════════════════════════════════════════════════════════════
# USDT Phase 1: source_rates CRUD
# ═══════════════════════════════════════════════════════════════════════════════

def insert_source_rate_unconditional(
    db: Session,
    source: str,
    asset: str,
    rate: float,
    timestamp: datetime,
) -> bool:
    """source_rates에 unconditional INSERT — close finalizer 전용 (KRX_CLOSE_SNAPSHOT_PLAN §5.2).

    `insert_source_rate_if_changed`와 동일 스타일:
        - 내부 db.add() + db.commit()
        - 예외는 caller로 전파 (finalizer caller가 격리 + DB/Redis/flag 성공 조건 조합 판단)
        - 성공 시 항상 True (skip 분기 없음)

    Args:
        timestamp: 필수 (UTC naive datetime). close grace event_at_kst → UTC 변환된 값.
            DB DEFAULT 사용 금지 — boundary 의미적 시각이 정확해야 종가 정합 보장.

    Returns:
        True: INSERT 성공.

    설계:
        - dedup 우회 (가격 동일 시에도 row 생성) — KrxDbWriter `insert_if_changed`와 분리
        - DB unique constraint 미보장 시 race로 duplicate 가능 (3차 PR 영역)
    """
    record = models.SourceRate(source=source, asset=asset, rate=rate)
    record.timestamp = timestamp
    db.add(record)
    db.commit()
    return True


def event_ms_to_utc_naive(timestamp_ms: int) -> datetime:
    """exchange event epoch-ms → UTC naive datetime (source_rates.timestamp 저장용).

    §12.9.8 ③ super-lite — USDT WS DB writer가 tick의 exchange 체결/이벤트 시각을
    `insert_source_rate_if_changed(timestamp=)`로 넘길 때 사용. epoch ms는 UTC이므로
    fromtimestamp(tz=utc) 후 tzinfo 제거 — `SourceRate.timestamp`는 naive
    ("KST aware 금지" 계약). 5 writer 공용(DRY) + 변환 규칙 단일 지점.
    """
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=dt_timezone.utc).replace(tzinfo=None)


def insert_source_rate_if_changed(
    db: Session,
    source: str,
    asset: str,
    rate: float,
    timestamp: Optional[datetime] = None,
) -> bool:
    """
    source_rates에 변경 시에만 INSERT.

    기존 bank/investing 저장 패턴과 동일하게 마지막 레코드와 비교 후 변경 시만 저장.

    Args:
        timestamp: 명시 timestamp (UTC naive datetime). None이면 DB DEFAULT
            (`models.get_utc_now`) 사용 — 기존 호출자 동작 그대로.
            KRX close snapshot 등 의미적 boundary 시각 저장 시 명시 전달.
            KST aware datetime 전달 금지 (`SourceRate.timestamp`는 naive).

    Returns:
        True: INSERT 수행, False: 변경 없어 스킵
    """
    last = (
        db.query(models.SourceRate)
        .filter(
            models.SourceRate.source == source,
            models.SourceRate.asset == asset,
        )
        .order_by(
            models.SourceRate.timestamp.desc(),
            models.SourceRate.id.desc(),
        )
        .first()
    )

    if last is not None and last.rate == rate:
        return False

    record = models.SourceRate(source=source, asset=asset, rate=rate)
    if timestamp is not None:
        record.timestamp = timestamp
    db.add(record)
    db.commit()
    return True


def get_latest_source_rate(
    db: Session,
    source: str,
    asset: str,
) -> Optional[Dict[str, Any]]:
    """특정 (source, asset)의 최신 1건 조회. legacy bank/currency shape로 반환."""
    record = (
        db.query(models.SourceRate)
        .filter(
            models.SourceRate.source == source,
            models.SourceRate.asset == asset,
        )
        .order_by(
            models.SourceRate.timestamp.desc(),
            models.SourceRate.id.desc(),
        )
        .first()
    )

    if record is None:
        return None

    return {
        "currency": record.asset,
        "bank": record.source,
        "rate": record.rate,
        "timestamp": to_kst_isoformat(record.timestamp),
    }


def get_latest_source_rates_for_topic(
    db: Session,
    asset: str,
    sources: List[str],
) -> List[Dict[str, Any]]:
    """Topic builder 전용 raw fetcher — Z-2d legacy_policy 적용 X (PR Z-2e B-Step 2).

    **중요**: 이 함수는 USDT/KRX 같은 topic-only source를 그대로 반환한다.
    Legacy `/api/rates*` / WebSocket fallback / Redis mirror seed 경로에서는
    절대 호출하면 안 됨 (그 경로는 `get_source_rates_as_legacy_format`이 정책
    통과 source만 반환한다). Topic API(usdt:krw, fx:* 등) builder만 사용.

    Args:
        db: SQLAlchemy session.
        asset: 통화쌍/상품 (예: "usdt-krw").
        sources: 조회할 source list — 호출자가 명시 (출력 순서도 입력 순서 보존).

    Returns:
        [{"source", "asset", "rate", "timestamp", "rate_changed_at"}, ...] topic-native shape.
        Redis read helper(`get_latest_usdt_rate_from_sync_job`)와 동일 shape —
        builder normalization 단순화. rate_changed_at = timestamp (DB row는 정밀 변경시각).

        해당 (source, asset) 조합이 DB에 없으면 결과 list에서 누락 (호출자가
        부분 결과 처리 — 정상 동작은 모든 source 존재).
    """
    by_source: Dict[str, models.SourceRate] = {}

    # 각 (source, asset)별 최신 1건. source 인자가 작아서 N+1 비용 미세.
    for source in sources:
        record = (
            db.query(models.SourceRate)
            .filter(
                models.SourceRate.source == source,
                models.SourceRate.asset == asset,
            )
            .order_by(
                models.SourceRate.timestamp.desc(),
                models.SourceRate.id.desc(),
            )
            .first()
        )
        if record is not None:
            by_source[source] = record

    # 입력 sources 순서 보존
    return [
        {
            "source": record.source,
            "asset": record.asset,
            "rate": record.rate,
            "timestamp": to_kst_isoformat(record.timestamp),
            # DB row timestamp = 정밀 변경시각 (source_rates insert-if-changed). Redis path
            # (get_latest_usdt_rate_from_sync_job)와 일관되게 rate_changed_at 노출 (codex
            # 019efe0b Option B). usdt:krw builder의 _normalize_entry가 asset=usdt-krw entry
            # 에만 carry — 다른 asset/source는 무시되므로 universal 추가도 안전.
            "rate_changed_at": to_kst_isoformat(record.timestamp),
        }
        for source in sources
        for record in [by_source.get(source)]
        if record is not None
    ]


def _filter_source_entries_by_legacy_policy(
    entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """source_rates → legacy shape entries에서 topic-only source 제거 (Z-2d Step 2).

    `legacy_policy.should_include_source_in_legacy_rates(source, asset)` 통과
    여부로 필터링. 현재 source_rates에는 USDT(5거래소) + KRX만 저장되므로 결과는
    빈 list. 미래에 LEGACY_RATE_SOURCES 통과 source가 source_rates에 들어가면
    중복 위험 (bank/investing 별도 테이블에서 이미 들어옴) — 그땐 별도 dedup
    정책 필요.

    Args:
        entries: get_source_rates_as_legacy_format이 DB에서 변환한 entry list.
                 shape: {"currency", "bank", "rate", "timestamp"}.

    Returns:
        legacy policy 통과 entries만. 빈 list 가능.
    """
    from app.legacy_policy import should_include_source_in_legacy_rates
    return [
        e for e in entries
        if should_include_source_in_legacy_rates(e["bank"], e["currency"])
    ]


def get_source_rates_as_legacy_format(
    db: Session,
    asset: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    source_rates를 기존 bank/currency shape로 변환해서 반환.

    레거시 API 호환성 어댑터. `/api/rates`, `/api/rates/{currency}`, WebSocket
    `rates` 배열, Redis latest mirror seed에서 호출. 각 (source, asset) 조합별
    최신 1건.

    반환 순서는 `source_registry`의 `sort_order` 기준 (기본 표시 순서 보장).
    registry에 없는 소스는 방어적으로 맨 뒤에 배치.

    Z-2d (2026-05-12): `_filter_source_entries_by_legacy_policy` 적용 — topic-only
    source(USDT/KRX)는 legacy 응답에서 제외. 통과한 source만 남음. 결과적으로
    현재 source_rates(USDT/KRX만 저장)는 빈 list 반환 (의도된 동작 — legacy
    노출 차단). topic API는 자체 builder 사용이라 본 함수 미경유.

    Args:
        db: 세션
        asset: 지정 시 해당 asset만 반환. None이면 전체.

    Returns:
        [{"currency": asset, "bank": source, "rate": ..., "timestamp": ...}, ...]
        legacy policy 통과 entries만.
    """
    # 함수 내부 import — 순환 참조 방지 (source_registry는 crud에 의존하지 않음)
    from app import source_registry

    # 각 (source, asset)별 최신 1건을 결정적으로 선택
    query = db.query(
        models.SourceRate.id,
        func.row_number().over(
            partition_by=(models.SourceRate.source, models.SourceRate.asset),
            order_by=[
                models.SourceRate.timestamp.desc(),
                models.SourceRate.id.desc(),
            ],
        ).label("rn"),
    )

    if asset is not None:
        query = query.filter(models.SourceRate.asset == asset)

    ranked = query.subquery()

    records = (
        db.query(models.SourceRate)
        .join(
            ranked,
            and_(
                models.SourceRate.id == ranked.c.id,
                ranked.c.rn == 1,
            ),
        )
        .all()
    )

    entries = [
        {
            "currency": record.asset,
            "bank": record.source,
            "rate": record.rate,
            "timestamp": to_kst_isoformat(record.timestamp),
        }
        for record in records
    ]

    # Z-2d Step 2: legacy 노출 정책 적용 — topic-only source 제거.
    # 모든 호출자(REST `/api/rates*` + WebSocket DB fallback + Redis mirror seed)가
    # 자동 커버됨. policy 통과 source는 그대로 유지.
    entries = _filter_source_entries_by_legacy_policy(entries)

    # registry의 sort_order 기준 정렬 (기본 표시 순서 보장).
    # registry에 없는 소스는 float('inf')로 맨 뒤, 내부적으로는 bank 이름으로 타이 브레이크.
    def sort_key(entry: Dict[str, Any]) -> tuple:
        definition = source_registry.get_source_definition(entry["bank"], entry["currency"])
        if definition is None:
            return (float('inf'), entry["bank"])
        return (definition.sort_order, entry["bank"])

    entries.sort(key=sort_key)
    return entries


def delete_old_source_rates(db: Session, days: int = 30) -> int:
    """
    지정된 일수 이상 지난 source_rates 데이터 삭제 (기존 bank cleanup 패턴과 동일).

    Args:
        db: 세션
        days: 보관할 일수 (기본값: 30일)

    Returns:
        삭제된 레코드 개수
    """
    cutoff_date = models.get_utc_now() - timedelta(days=days)

    deleted_count = db.query(models.SourceRate).filter(
        models.SourceRate.timestamp < cutoff_date
    ).delete()

    db.commit()
    return deleted_count


def delete_old_market_index_rates(
    db: Session,
    days: int = 30,
    instruments: Optional[List[str]] = None,
    granularities: Optional[List[str]] = None,
) -> int:
    """
    지정된 일수 이상 지난 시장 지수 데이터 삭제.

    기본 대상은 현물/운영 DXY와 미국달러지수 선물의 realtime 원본이다.
    hourly/daily rollup은 3m/1y 그래프 보존을 위해 기본 정리 대상에서 제외한다.
    """
    cutoff_date = models.get_utc_now() - timedelta(days=days)
    target_instruments = instruments or ["dxy", "dxy_futures"]
    target_granularities = granularities or ["realtime"]

    deleted_count = db.query(models.MarketIndexRate).filter(
        models.MarketIndexRate.instrument.in_(target_instruments),
        models.MarketIndexRate.granularity.in_(target_granularities),
        models.MarketIndexRate.timestamp < cutoff_date,
    ).delete(synchronize_session=False)

    db.commit()
    return deleted_count


# ═══════════════════════════════════════════════════════════════════════════════
# USDT Phase 1: source_notification_settings CRUD
# ═══════════════════════════════════════════════════════════════════════════════
# (B2 sentinel `_UNSET`는 bank create_notification_setting 앞에서 단일 정의 — bank/source 공유.)


def create_source_notification_setting(
    db: Session,
    user_id: str,
    source: str,
    asset: str,
    condition: str,
    threshold: float,
    is_enabled: bool = True,
    repeat_interval_sec: Optional[int] = None,  # B2 (ADR-036): NULL=once / 정수=초 간격
) -> models.SourceNotificationSetting:
    """
    Source 기반 알림 설정 생성 (중복 방지).

    기존 NotificationSetting과 동일한 멱등성 정책을 따른다:
    - 같은 (user_id, source, asset, condition, threshold) 조합이 있으면 기존 설정 업데이트
    - is_enabled=True 재활성화 시 triggered 초기화 (재알림 가능)
    - is_enabled=False면 triggered 유지 (발송됨 상태 보존)
    """
    existing = db.query(models.SourceNotificationSetting).filter(
        models.SourceNotificationSetting.user_id == user_id,
        models.SourceNotificationSetting.source == source,
        models.SourceNotificationSetting.asset == asset,
        models.SourceNotificationSetting.condition == condition,
        models.SourceNotificationSetting.threshold == threshold,
    ).first()

    if existing:
        existing.enabled = is_enabled
        existing.repeat_interval_sec = repeat_interval_sec  # B2: dedup 시 interval 갱신
        if is_enabled:
            existing.triggered = False
            existing.last_notified_at = None
            existing.last_notified_rate = None
        existing.updated_at = models.get_utc_now()
        db.commit()
        db.refresh(existing)
        logger.info(
            "source 알림 설정 재활성화 (중복)" if is_enabled else "source 알림 설정 비활성화 (중복)",
            extra={
                "user_id": user_id[:8] + "...",
                "setting_id": existing.id,
                "source": source,
                "asset": asset,
                "is_enabled": is_enabled,
            },
        )
        return existing

    setting = models.SourceNotificationSetting(
        user_id=user_id,
        source=source,
        asset=asset,
        condition=condition,
        threshold=threshold,
        enabled=is_enabled,
        triggered=False,
        repeat_interval_sec=repeat_interval_sec,  # B2 (ADR-036)
    )
    db.add(setting)
    db.commit()
    db.refresh(setting)

    logger.info(
        "source 알림 설정 생성",
        extra={
            "user_id": user_id[:8] + "...",
            "source": source,
            "asset": asset,
            "condition": condition,
            "threshold": threshold,
            "is_enabled": is_enabled,
            "setting_id": setting.id,
        },
    )
    return setting


def get_source_notification_settings(
    db: Session,
    user_id: str,
) -> List[models.SourceNotificationSetting]:
    """사용자의 모든 source 기반 알림 설정 조회."""
    return db.query(models.SourceNotificationSetting).filter(
        models.SourceNotificationSetting.user_id == user_id
    ).order_by(models.SourceNotificationSetting.created_at.desc()).all()


def get_source_notification_setting_by_id(
    db: Session,
    setting_id: int,
    user_id: str,
) -> Optional[models.SourceNotificationSetting]:
    """특정 source 알림 설정 조회 (소유권 검증 포함)."""
    return db.query(models.SourceNotificationSetting).filter(
        models.SourceNotificationSetting.id == setting_id,
        models.SourceNotificationSetting.user_id == user_id,
    ).first()


def update_source_notification_setting(
    db: Session,
    setting_id: int,
    user_id: str,
    source: Optional[str] = None,
    asset: Optional[str] = None,
    condition: Optional[str] = None,
    threshold: Optional[float] = None,
    enabled: Optional[bool] = None,
    repeat_interval_sec=_UNSET,  # B2 (ADR-036): sentinel=미제공 / None=once / 정수=repeat
) -> Optional[models.SourceNotificationSetting]:
    """
    Source 기반 알림 설정 수정 (PUT - 부분 업데이트).

    값이 실제로 변경된 경우에만 triggered 초기화. 기존 NotificationSetting 규칙과 동일.
    B2 (ADR-036): repeat_interval_sec 변경 시 §7 리셋 + once↔repeat 전환 시 §8 정규화.
    """
    setting = get_source_notification_setting_by_id(db, setting_id, user_id)
    if not setting:
        return None

    should_reset_triggered = False

    if source is not None:
        if source != setting.source:
            should_reset_triggered = True
        setting.source = source

    if asset is not None:
        if asset != setting.asset:
            should_reset_triggered = True
        setting.asset = asset

    if condition is not None:
        if condition != setting.condition:
            should_reset_triggered = True
        setting.condition = condition

    if threshold is not None:
        if threshold != setting.threshold:
            should_reset_triggered = True
        setting.threshold = threshold

    if enabled is not None:
        if enabled and not setting.enabled:
            should_reset_triggered = True
        setting.enabled = enabled

    # B2 (ADR-036): repeat_interval_sec — sentinel=미제공 / None=once / 정수=repeat
    mode_transition = False
    if repeat_interval_sec is not _UNSET:
        if repeat_interval_sec != setting.repeat_interval_sec:
            should_reset_triggered = True  # §7: interval 변경 시 last_notified_at 리셋
            if (setting.repeat_interval_sec is None) != (repeat_interval_sec is None):
                mode_transition = True     # §8: once↔repeat 전환
        setting.repeat_interval_sec = repeat_interval_sec

    if should_reset_triggered:
        setting.triggered = False
        setting.last_notified_at = None
        setting.last_notified_rate = None
        logger.info(
            "source 알림 설정 재활성화 (조건/interval 변경)",
            extra={"setting_id": setting_id, "triggered_reset": True},
        )

    # §8: 모드 전환은 새 모드 clean active 시작 — 이전 once 발사로 남은 enabled=false 해제.
    # 단 같은 PUT에서 사용자가 명시적으로 enabled=false를 주면 그 의도 존중.
    if mode_transition and enabled is not False:
        setting.enabled = True

    setting.updated_at = models.get_utc_now()
    db.commit()
    db.refresh(setting)

    logger.info(
        "source 알림 설정 수정",
        extra={
            "setting_id": setting_id,
            "source": setting.source,
            "asset": setting.asset,
            "condition": setting.condition,
            "threshold": setting.threshold,
            "enabled": setting.enabled,
            "triggered": setting.triggered,
        },
    )
    return setting


def delete_source_notification_setting(
    db: Session,
    setting_id: int,
    user_id: str,
) -> bool:
    """Source 기반 알림 설정 삭제."""
    deleted = db.query(models.SourceNotificationSetting).filter(
        models.SourceNotificationSetting.id == setting_id,
        models.SourceNotificationSetting.user_id == user_id,
    ).delete()
    db.commit()

    if deleted:
        logger.info(
            "source 알림 설정 삭제",
            extra={"setting_id": setting_id, "user_id": user_id[:8] + "..."},
        )
    return deleted > 0


# ─────────────────────────────────────────────────────────────
# Source 기반 알림 발송용 쿼리 + 처리
# ─────────────────────────────────────────────────────────────

def get_triggered_source_settings_for_rate(
    db: Session,
    source: str,
    asset: str,
    rate: float,
) -> List[Dict[str, Any]]:
    """특정 source/asset 환율에 대해 조건 충족된 알림 설정 목록 + devices 조회."""
    settings = db.query(models.SourceNotificationSetting).filter(
        models.SourceNotificationSetting.source == source,
        models.SourceNotificationSetting.asset == asset,
        models.SourceNotificationSetting.enabled == True,
        models.SourceNotificationSetting.triggered == False,
    ).all()

    matched_settings = []
    user_ids = set()

    for setting in settings:
        condition_met = False
        if setting.condition == "above" and rate >= setting.threshold:
            condition_met = True
        elif setting.condition == "below" and rate <= setting.threshold:
            condition_met = True

        if condition_met:
            # B2 (ADR-036): repeat 모드 interval gate. query는 enabled+!triggered만 거르는데
            # repeat는 triggered 항상 false라 매 tick 통과 → last_notified_at+interval 미경과면 skip해
            # 반복 throttle. once는 query의 !triggered가 이미 gate(repeat_interval_sec None이라 미진입).
            # get_utc_now()=naive UTC, last_notified_at도 naive UTC라 뺄셈 안전(소스 aware-now 이슈 없음).
            # bank(get_triggered_settings_for_rate) + source-legacy(get_triggered_source_settings_for_rate)
            # 둘 다 동일 적용 — 후자는 REST polling 비활성이나 재활성 시 spam 방어.
            if setting.repeat_interval_sec is not None and setting.last_notified_at is not None:
                elapsed = (models.get_utc_now() - setting.last_notified_at).total_seconds()
                if elapsed < setting.repeat_interval_sec:
                    continue
            matched_settings.append(setting)
            user_ids.add(setting.user_id)

    if not matched_settings:
        return []

    all_devices = db.query(models.UserDevice).filter(
        models.UserDevice.user_id.in_(user_ids)
    ).all()

    devices_by_user: Dict[str, List[models.UserDevice]] = {}
    for device in all_devices:
        devices_by_user.setdefault(device.user_id, []).append(device)

    results = []
    for setting in matched_settings:
        devices = devices_by_user.get(setting.user_id, [])
        if devices:
            results.append({
                "setting": setting,
                "devices": devices,
                "user_id": setting.user_id,
            })

    return results


def mark_source_setting_triggered(
    db: Session,
    setting_id: int,
    rate: float,
) -> None:
    """Source 알림 설정을 '발송됨'으로 표시 (1회성 알림 자동 비활성화)."""
    setting = db.query(models.SourceNotificationSetting).filter(
        models.SourceNotificationSetting.id == setting_id
    ).first()

    if setting:
        # B2 (ADR-036): mode 분기 — ORM row를 직접 읽어 repeat_interval_sec로 판단
        # (persist_result/cache로 interval thread 불요).
        if setting.repeat_interval_sec is None:
            # once-only (현행): 발송 후 자동 비활성화 + triggered=종료 플래그.
            setting.triggered = True
            setting.enabled = False
        # else: repeat — enabled 유지 + triggered 미설정(once 종료 전용).
        #       gate(delivery_allowed)가 last_notified_at+interval로 다음 발송 판단.
        setting.last_notified_at = models.get_utc_now()
        setting.last_notified_rate = rate
        db.commit()

        logger.info(
            "source 알림 발송 완료",
            extra={
                "setting_id": setting_id, "rate": rate,
                "mode": "once" if setting.repeat_interval_sec is None else "repeat",
                "enabled": setting.enabled,
            },
        )


def create_comparison_alert(
    db: Session,
    user_id: str,
    tab: str,
    left_source: str,
    left_asset: str,
    right_source: str,
    right_asset: str,
    diff_type: str,
    operator: str,
    threshold: float,
    is_enabled: bool = True,
    repeat_interval_sec: Optional[int] = None,
) -> models.ComparisonAlert:
    """비교 알림 생성 (ADR-037 dedup — 단일 알림 멱등 선례 계승).

    (user_id, tab, left_*, right_*, diff_type, operator, threshold) exact match →
    기존 설정 enabled 갱신. is_enabled=True 재활성화 시 triggered/last_notified 초기화.
    A−B/B−A 순서 뒤집힘은 별개 취급 (v1 — preset이 방향 고정, ADR Open 6).
    """
    existing = db.query(models.ComparisonAlert).filter(
        models.ComparisonAlert.user_id == user_id,
        models.ComparisonAlert.tab == tab,
        models.ComparisonAlert.left_source == left_source,
        models.ComparisonAlert.left_asset == left_asset,
        models.ComparisonAlert.right_source == right_source,
        models.ComparisonAlert.right_asset == right_asset,
        models.ComparisonAlert.diff_type == diff_type,
        models.ComparisonAlert.operator == operator,
        models.ComparisonAlert.threshold == threshold,
    ).first()

    if existing:
        existing.enabled = is_enabled
        existing.repeat_interval_sec = repeat_interval_sec
        if is_enabled:
            existing.triggered = False
            existing.last_notified_at = None
            existing.last_notified_spread = None
        existing.updated_at = models.get_utc_now()
        db.commit()
        db.refresh(existing)
        logger.info("비교 알림 재활성화 (중복)" if is_enabled else "비교 알림 비활성화 (중복)",
                    extra={"setting_id": existing.id, "tab": tab, "is_enabled": is_enabled})
        return existing

    alert = models.ComparisonAlert(
        user_id=user_id, tab=tab,
        left_source=left_source, left_asset=left_asset,
        right_source=right_source, right_asset=right_asset,
        diff_type=diff_type, operator=operator, threshold=threshold,
        enabled=is_enabled, triggered=False,
        repeat_interval_sec=repeat_interval_sec,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def get_comparison_alerts(db: Session, user_id: str) -> List[models.ComparisonAlert]:
    """사용자의 비교 알림 전체 (최신 생성순)."""
    return (db.query(models.ComparisonAlert)
            .filter(models.ComparisonAlert.user_id == user_id)
            .order_by(models.ComparisonAlert.created_at.desc()).all())


def update_comparison_alert(
    db: Session,
    setting_id: int,
    user_id: str,
    is_enabled: Optional[bool] = None,
    repeat_interval_sec=_UNSET,   # sentinel(B2 공용) — 미제공 vs 명시적 null(=once) 구분
) -> Optional[models.ComparisonAlert]:
    """비교 알림 수정 (v1: is_enabled 토글 + repeat_interval_sec만 — ADR-037 Decision 5).

    is_enabled=True 재활성화 → triggered/last_notified 초기화 (단일 알림 §7 선례).
    repeat_interval_sec: sentinel(미제공)=변경 없음 / None=once 전환 / 정수=repeat (B2 3-state).
    """
    alert = db.query(models.ComparisonAlert).filter(
        models.ComparisonAlert.id == setting_id,
        models.ComparisonAlert.user_id == user_id,
    ).first()
    if alert is None:
        return None

    should_reset = False
    if is_enabled is not None:
        if is_enabled and not alert.enabled:
            should_reset = True                    # 재활성화 → 재발화 가능 상태
        alert.enabled = is_enabled

    # B2 (ADR-036) §7/§8 — update_source_notification_setting 미러 (codex S3 blocker):
    # interval 실제 변경 시 last_notified_* 리셋(이전 발화 시각이 새 설정을 suppress하는 것 방지),
    # once↔repeat 모드 전환 시 clean active 시작(단 같은 PUT의 명시적 is_enabled=False 존중).
    mode_transition = False
    if repeat_interval_sec is not _UNSET:
        if repeat_interval_sec != alert.repeat_interval_sec:
            should_reset = True                    # §7: interval 변경 리셋
            if (alert.repeat_interval_sec is None) != (repeat_interval_sec is None):
                mode_transition = True             # §8: once↔repeat 전환
        alert.repeat_interval_sec = repeat_interval_sec

    if should_reset:
        alert.triggered = False
        alert.last_notified_at = None
        alert.last_notified_spread = None
    if mode_transition and is_enabled is not False:
        alert.enabled = True
    alert.updated_at = models.get_utc_now()
    db.commit()
    db.refresh(alert)
    return alert


def delete_comparison_alert(db: Session, setting_id: int, user_id: str) -> bool:
    """비교 알림 삭제 (멱등 — 없으면 False). 로그는 setting_id nullable로 보존."""
    deleted = db.query(models.ComparisonAlert).filter(
        models.ComparisonAlert.id == setting_id,
        models.ComparisonAlert.user_id == user_id,
    ).delete()
    db.commit()
    return deleted > 0


def get_comparison_notification_logs(
    db: Session,
    user_id: str,
    tab: Optional[str] = None,
    diff_type: Optional[str] = None,
    success_only: bool = True,
    limit: int = 100,
) -> List[models.ComparisonNotificationLog]:
    """비교 알림 발송 히스토리 (sent_at DESC, success-only 기본 — source logs 선례).

    diff_type 필터: 'signed'=김프/역프 알림 히스토리 / 'absolute'=일반 비교 알림 히스토리
    (ADR-037 Amendment — 두 섹션이 같은 테이블을 diff_type로 구분).
    """
    q = db.query(models.ComparisonNotificationLog).filter(
        models.ComparisonNotificationLog.user_id == user_id)
    if tab:
        q = q.filter(models.ComparisonNotificationLog.tab == tab)
    if diff_type:
        q = q.filter(models.ComparisonNotificationLog.diff_type == diff_type)
    if success_only:
        q = q.filter(models.ComparisonNotificationLog.success == True)  # noqa: E712
    return q.order_by(models.ComparisonNotificationLog.sent_at.desc()).limit(limit).all()


def mark_comparison_alert_triggered(
    db: Session,
    setting_id: int,
    spread: float,
) -> None:
    """비교 알림 설정을 '발송됨'으로 표시 (ADR-037 — mark_source_setting_triggered 미러).

    once(repeat_interval_sec NULL): triggered=True + enabled=False (1회성 종료).
    repeat: enabled 유지 + triggered 미설정 — gate(delivery_allowed)가 last_notified_at+interval 판단.
    last_notified_spread는 signed raw (진단/히스토리 요약용 — ADR-037 Decision 2).
    """
    setting = db.query(models.ComparisonAlert).filter(
        models.ComparisonAlert.id == setting_id
    ).first()

    if setting:
        if setting.repeat_interval_sec is None:
            setting.triggered = True
            setting.enabled = False
        setting.last_notified_at = models.get_utc_now()
        setting.last_notified_spread = spread
        db.commit()

        logger.info(
            "비교 알림 발송 완료",
            extra={
                "setting_id": setting_id, "spread": spread,
                "mode": "once" if setting.repeat_interval_sec is None else "repeat",
                "enabled": setting.enabled,
            },
        )


def create_source_notification_log(
    db: Session,
    user_id: str,
    setting_id: Optional[int],
    source: str,
    asset: str,
    condition: str,
    threshold: float,
    triggered_rate: float,
    success: bool,
    error_message: Optional[str] = None,
) -> models.SourceNotificationLog:
    """Source 기반 알림 발송 히스토리 기록."""
    log = models.SourceNotificationLog(
        user_id=user_id,
        setting_id=setting_id,
        source=source,
        asset=asset,
        condition=condition,
        threshold=threshold,
        triggered_rate=triggered_rate,
        success=success,
        error_message=error_message,
    )
    db.add(log)
    db.commit()
    return log


def get_source_notification_logs(
    db: Session,
    user_id: str,
    asset: Optional[str] = None,
    success_only: bool = True,
    limit: int = 100,
) -> List[models.SourceNotificationLog]:
    """Source 기반 알림 발송 히스토리 조회 (최신순, 사용자용).

    제품 의미: 사용자에게 '받은(발송 성공) 알림 히스토리'를 보여주는 read 경로
    (이 함수가 source_notification_logs의 최초 reader — 기존엔 create-only).
    실패 row(success=False)는 운영 진단(telemetry)용이라 success_only=True 기본 제외.
    성공 경로만 mark_source_setting_triggered로 setting을 닫으므로
    (create_source_notification_log 호출부 참조), 성공 row가 사용자가 실제 통지받은
    이벤트와 일치한다.

    스코프/안전:
        - user_id는 호출자가 token에서 파생한 값만 전달 (cross-user 격리).
        - asset은 SQL WHERE로 필터 (append-only 무한 증가 테이블이라 fetch-all 회피).
        - 단일 writer 전제: 현재 prod는 evaluator 경로만 활성
          (USDT_LEGACY_REST_POLLING_ENABLED=false). legacy polling 재활성 시 동일
          fire가 2 row가 될 수 있음 (dedup key 없음) → 그 경우 중복 노출 가능.
        - limit은 호출자가 cap (main.py 1..200). offset 없음 — one-shot 알림이라
          per-user 볼륨 작음. 필요 시 cursor 페이지네이션 후속.
    """
    query = db.query(models.SourceNotificationLog).filter(
        models.SourceNotificationLog.user_id == user_id
    )
    if asset:
        query = query.filter(models.SourceNotificationLog.asset == asset)
    if success_only:
        query = query.filter(models.SourceNotificationLog.success.is_(True))
    return (
        query.order_by(models.SourceNotificationLog.sent_at.desc())
        .limit(limit)
        .all()
    )


# Source 표시명은 app.source_registry.get_source_definition에서 얻는다.
# BANK_NAMES_KR와 중복을 피하고 단일 진실 소스 유지.


def process_source_rate_alerts(
    db: Session,
    changed_rates: List[Dict[str, Any]],
) -> int:
    """
    변경된 source 환율에 대해 알림 조건 체크 및 FCM 발송.

    usdt_sources.collect_usdt_rates 에서 호출된다. 기존 process_rate_alerts와
    구조는 동일하지만 source + asset + source_registry 기반으로 동작한다.

    Args:
        changed_rates: [{"source": "upbit", "asset": "usdt-krw", "rate": 1485.0, ...}, ...]

    Returns:
        발송된 알림 수
    """
    from app import source_registry
    from app.notifications.fcm import send_fcm_multicast_sync, init_firebase

    if not changed_rates:
        return 0

    if not init_firebase():
        logger.debug("Firebase 초기화 실패 - source 알림 스킵")
        return 0

    sent_count = 0
    all_failed_tokens: List[str] = []

    for rate_info in changed_rates:
        source = rate_info.get("source") or rate_info.get("bank")
        asset = rate_info.get("asset") or rate_info.get("currency")
        rate = rate_info["rate"]

        if source is None or asset is None:
            continue

        try:
            triggered_items = get_triggered_source_settings_for_rate(db, source, asset, rate)
            if not triggered_items:
                continue

            definition = source_registry.get_source_definition(source, asset)
            source_display = definition.display_name if definition else source.upper()
            asset_display = asset.upper()

            for item in triggered_items:
                setting = item["setting"]
                devices = item["devices"]
                user_id = item["user_id"]

                icon = "📈" if setting.condition == "above" else "📉"
                title = f"{icon}  {source_display}  {asset_display}"

                condition_arrow = "↑" if setting.condition == "above" else "↓"
                condition_text = "이상" if setting.condition == "above" else "이하"
                threshold_str = format_threshold(setting.threshold)
                rate_str = f"{rate:.2f}"

                body = f"[ {threshold_str} {condition_arrow}{condition_text} 도달 ]   {rate_str}"

                # FCM data payload. 기존 앱이 모르는 type이어도 무해하게 무시할 수 있도록
                # 필드 타입을 string으로 유지 (기존 rate_alert와 동일 컨벤션).
                data = {
                    "type": "source_rate_alert",
                    "title": title,
                    "body": body,
                    "source": source,
                    "asset": asset,
                    "rate": str(rate),
                    "threshold": str(setting.threshold),
                    "condition": setting.condition,
                    "setting_id": str(setting.id),
                    # B2 (ADR-036): cross-device race 차단용 권위 플래그 (legacy REST polling 경로,
                    # 현재 비활성이나 backend.build_payload와 일관성 유지).
                    "is_repeat": "true" if setting.repeat_interval_sec is not None else "false",
                }

                tokens = [d.device_token for d in devices]
                result = send_fcm_multicast_sync(tokens, title, body, data)

                if result["success_count"] > 0:
                    mark_source_setting_triggered(db, setting.id, rate)
                    sent_count += 1

                    create_source_notification_log(
                        db=db,
                        user_id=user_id,
                        setting_id=setting.id,
                        source=source,
                        asset=asset,
                        condition=setting.condition,
                        threshold=setting.threshold,
                        triggered_rate=rate,
                        success=True,
                    )

                    logger.info(
                        "🔔 source 알림 발송",
                        extra={
                            "event": "source_rate_alert_sent",
                            "source": source,
                            "asset": asset,
                            "rate": rate,
                            "threshold": setting.threshold,
                            "condition": setting.condition,
                            "user_id": user_id[:8] + "...",
                            "devices": len(devices),
                            "success": result["success_count"],
                        },
                    )
                else:
                    # 발송 실패 시에도 운영 추적을 위해 로그 기록
                    err_msg = result.get("error") or "no successful sends"
                    create_source_notification_log(
                        db=db,
                        user_id=user_id,
                        setting_id=setting.id,
                        source=source,
                        asset=asset,
                        condition=setting.condition,
                        threshold=setting.threshold,
                        triggered_rate=rate,
                        success=False,
                        error_message=err_msg,
                    )

                if result["failed_tokens"]:
                    all_failed_tokens.extend(result["failed_tokens"])

        except Exception:
            logger.exception(
                "source 알림 처리 실패",
                extra={"source": source, "asset": asset, "rate": rate},
            )
            continue

    # 무효 토큰 일괄 삭제
    if all_failed_tokens:
        try:
            deleted_count = db.query(models.UserDevice).filter(
                models.UserDevice.device_token.in_(all_failed_tokens)
            ).delete(synchronize_session=False)
            db.commit()
            logger.info(
                "source 알림: 무효 토큰 일괄 삭제",
                extra={"count": deleted_count, "tokens": len(all_failed_tokens)},
            )
        except Exception:
            logger.exception("source 알림: 무효 토큰 삭제 실패")

    return sent_count
