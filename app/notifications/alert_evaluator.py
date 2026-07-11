"""Alert observation-based evaluator (USDT_WS_DESIGN_PLAN §6 B1, PR6).

Source-neutral interface (`AlertObservation`, `CachedAlertSetting`,
`AlertSettingsCache`, `condition_matches`, `delivery_allowed`) + Upbit-only
구현 (`UsdtAlertEvaluator`). 향후 Bithumb 확장 / KRX/bank/investing 이전 시
같은 helper 재사용.

핵심 설계 (Codex/Claude 합의 누적):
    - **Redis-first**: 가격값은 observation.rate 또는 Redis latest. DB에서
      최신 가격 조회 금지. DB는 settings/devices/triggered/log 저장소.
    - **Settings cache (TTL=10s)**: tick마다 DB query 회피. cache hit는
      event loop dict lookup, cache miss만 to_thread로 DB query.
    - **Per-key loading guard (`_loading`)**: TTL 만료 순간 동시 cache miss
      들어와도 DB loader 1번만 호출 (thundering herd 차단).
    - **DB session 3-step 분리**: refetch session (snapshot) → close → FCM
      (no session) → mark/log new session. FCM 중 DB connection 보유 X.
    - **ORM-free snapshot**: cache value(`CachedAlertSetting`)와 refetch
      결과(`FreshSettingSnapshot`) 모두 frozen dataclass. session detached
      object 위험 차단.
    - **In-flight setting guard (`_in_flight_settings`)**: cache TTL 동안
      동일 setting_id에 대한 동시 observation 처리 시 중복 FCM 차단
      (in-process atomic claim).
    - **Observation drop 금지**: 알림은 이벤트 보존이 핵심 (짧은 threshold
      crossing 놓치면 영영 누락). pending cap 없음, 100+ pending이면
      warning log만.
    - **close drain-first**: 진행 중 FCM 완료까지 대기, timeout 후 cancel
      (Redis writer 패턴 동일, FCM 더 느려서 timeout 5s).
    - **FCM 순서**: 발송 → success_count > 0 → mark_triggered + log success
      / 실패 → log failure only, setting 변경 X (기존 process_source_rate_alerts
      순서 보존). FCM 실패가 setting을 disabled 처리하면 사용자 알림 영영 X.
    - **Empty device_tokens 제외**: cache populate 시점에 device token 없는
      settings 제외 — 조건 평가 자체 의미 없음.
    - **Failure isolation**: cache/DB/FCM 모두 격리. WS session 영향 X.

PR6 non-scope:
    - CRUD invalidation (PR6 직후 별 PR — settings 수정 즉시 cache update)
    - Redis ZSET threshold index (Phase 3)
    - comparison alert (Phase E)
    - `repeat_interval_sec` 실제 구현 (B2, 별 ADR/PR)
    - direction crossing (B3, 별 ADR/PR)
    - Multi-process atomic claim (Redis SETNX, 별 PR)
    - KRX/bank/investing 알림 구조 변경 (Phase C/D)
    - Alert worker process 분리 (Phase 4)
    - History retention cleanup (별 commit)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from app.notifications.price_alert_coalescer import (
    PriceAlertCoalescer,
    PriceAlertEvaluationInput,
)
# fanout step 4 S1: storage/IO coupling을 backend로 추출 (load/refetch/persist/payload).
# alert_storage_backend는 alert_evaluator를 TYPE_CHECKING/lazy로만 참조 → 순환 없음.
from app.notifications.alert_storage_backend import (
    AlertStorageBackend,
    SourceAlertBackend,
)

logger = logging.getLogger("exchange_rate.notifications.alert_evaluator")

# Settings cache
ALERT_CACHE_TTL_SEC = 10.0

# Evaluator
PENDING_TASKS_WARNING_THRESHOLD = 100
ALERT_CLOSE_TIMEOUT_SEC = 5.0


# ---------------------------------------------------------------------------
# Dataclasses (source-neutral, ORM-free, frozen)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AlertObservation:
    """알림 평가 입력. tick 또는 REST probe 등 source agnostic.

    PR6: USDT WS tick → 변환 (kind="tick").
    미래: comparison alert input은 multi-source observation으로 확장 가능.
    """
    source: str          # "upbit" | "bithumb" | ...
    asset: str           # "usdt-krw" | "usd-krw-futures" | ...
    rate: float          # 평가 대상 가격
    timestamp_ms: int    # exchange-provided epoch ms (history log용)
    kind: str            # "tick" | "rest_probe" | future


def observation_from_tick(tick: dict, kind: str = "tick") -> AlertObservation:
    """source-neutral tick dict → AlertObservation 매핑 (fanout step 3, behavior-change-0).

    [ALL_SOURCE_FANOUT_UNIFICATION_PLAN.md §6 step 3] USDT 5소스 WS + KRX tick handler가
    각자 반복하던 AlertObservation 생성을 단일 adapter로 통합. tick은 {source, asset, rate,
    timestamp_ms} 키 보유. **timestamp_ms 도출은 caller 책임** — USDT는 거래소 event-ts를
    passthrough, KRX는 received_at를 변환(의미가 달라 helper에 안 둠, never-unify §4-3/§5-6).
    kind: "tick" | "rest_probe".
    """
    return AlertObservation(
        source=tick["source"],
        asset=tick["asset"],
        rate=tick["rate"],
        timestamp_ms=tick["timestamp_ms"],
        kind=kind,
    )


@dataclass(frozen=True)
class CachedAlertSetting:
    """알림 설정 cache snapshot (ORM-free).

    PR6는 once 정책 (enabled+triggered=false 평가만). gate(delivery_allowed)는 refetch
    snapshot에서 실행하므로 cache 단계엔 repeat_interval_sec 불요였으나, B2 payload-flag
    (ADR-036)에서 **build_payload의 is_repeat 도출용**으로 fresh_candidate에 carry한다
    (snapshot.repeat_interval_sec → fresh_candidate). load_settings 단계 default None은
    그대로 — gate/payload 모두 refetch 경유 fresh_candidate만 사용.
    """
    setting_id: int
    user_id: str
    source: str
    asset: str
    condition: str          # "above" | "below"
    threshold: float
    device_tokens: tuple[str, ...]  # FCM multicast 입력
    repeat_interval_sec: Optional[int] = None  # B2 (ADR-036): None=once / 정수=repeat (payload is_repeat 도출)


@dataclass(frozen=True)
class CachedBucket:
    """(source, asset) bucket — cache 자료구조 단위."""
    settings: tuple[CachedAlertSetting, ...]
    populated_at: float     # epoch sec (TTL 비교 기준)


@dataclass(frozen=True)
class FreshSettingSnapshot:
    """refetch 결과 ORM-free snapshot.

    Session 1 (refetch)에서 만들어 session close. 이후 FCM/log 단계에서 사용.

    Codex Finding (Medium): condition/threshold/source/asset 모두 refetch에
    포함되어 cache TTL 10s 동안 사용자가 변경한 경우의 stale 발송 차단.
    refetch는 enabled/triggered 외에도 조건/임계값 stale 보호의 최종 방어선.
    """
    setting_id: int
    enabled: bool
    triggered: bool
    source: str
    asset: str
    condition: str
    threshold: float
    # B2 (ADR-036): repeat 모드 gate(delivery_allowed)는 refetch된 이 snapshot에서 실행.
    # repeat_interval_sec NULL=once / last_notified_at=마지막 발송(naive UTC, gate 기준).
    # default 부여 — 기존 snapshot 생성부/테스트 factory 회귀 안전.
    repeat_interval_sec: Optional[int] = None
    last_notified_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Pure functions (조건 평가 / 발송 정책)
# ---------------------------------------------------------------------------

def condition_matches_observation(setting: CachedAlertSetting, observation: AlertObservation) -> bool:
    """가격 조건 만족 여부 — raw observation rate 기준 (above/below 경계 ≥/≤).

    기존 `crud.get_triggered_source_settings_for_rate` 조건 보존.
    """
    if setting.condition == "above" and observation.rate >= setting.threshold:
        return True
    if setting.condition == "below" and observation.rate <= setting.threshold:
        return True
    return False


# Backward-compat alias — 기존 호출처(`_evaluate_async`, `_send_one`)는 alias로 동작 보존.
# 점진 마이그레이션은 5b-3 composition 단계에서 (호출처 변경 없이 evaluator 내부에서 dispatch).
condition_matches = condition_matches_observation


def matched_triggered_rate(
    setting: CachedAlertSetting, price_input: PriceAlertEvaluationInput
) -> Optional[Decimal]:
    """조건 만족 시 trigger 가격 산출 — 조건 + 가격을 같은 기준에서 통합 결정.

    above 조건 만족 → ``max_rate`` 반환 (window 동안 최고가, 실제 crossing한 가격)
    below 조건 만족 → ``min_rate`` 반환 (window 동안 최저가)
    조건 불만족 → ``None``

    §12.8.3 결정 #11 정합 — FCM/log payload ``triggered_rate`` field 산출 source.
    """
    threshold = Decimal(str(setting.threshold))
    if setting.condition == "above" and price_input.max_rate >= threshold:
        return price_input.max_rate
    if setting.condition == "below" and price_input.min_rate <= threshold:
        return price_input.min_rate
    return None


def condition_matches_price_input(
    setting: CachedAlertSetting, price_input: PriceAlertEvaluationInput
) -> bool:
    """Window-aware 조건 평가 — crossing 보존.

    above: ``max_rate >= threshold`` (window 동안 최고가)
    below: ``min_rate <= threshold`` (window 동안 최저가)

    §12.8.3 결정 #10/#11 정합. ``matched_triggered_rate is not None``과 동일 —
    조건 만족 기준과 ``triggered_rate`` 산출이 drift 없이 자동 일치.
    """
    return matched_triggered_rate(setting, price_input) is not None


def _emit_comparison(source: str, asset: str) -> None:
    """비교알림 hook forward (ADR-037) — lazy import + 예외 격리 (tick 경로 영향 0)."""
    try:
        from app.notifications.comparison_evaluator import emit_comparison_observation
        emit_comparison_observation(source, asset)
    except Exception:
        logger.exception("[alert_evaluator] comparison hook 실패 (격리)")


def delivery_allowed(snapshot: FreshSettingSnapshot, now: datetime) -> bool:
    """발송 가능 여부 (ADR-036 B2).

    - once (`repeat_interval_sec IS NULL`): `enabled and not triggered` (PR6 현행).
    - repeat (정수 간격): `enabled and (last_notified_at is None or now-last >= interval)`.
      triggered는 repeat 게이트에 미사용(once 종료 상태 전용).

    timezone (ADR-036 §4, load-bearing): now/last_notified_at 모두 **naive UTC**로 비교.
    호출부가 aware now(`datetime.now(timezone.utc)`)를 넘겨도 여기서 naive 정규화 —
    last_notified_at은 naive UTC(`models.get_utc_now`)라, 정규화 없으면 aware-naive 뺄셈이
    TypeError → `_evaluate_*_async`의 except가 삼켜 repeat가 영영 silent 미발화.
    """
    if snapshot.repeat_interval_sec is None:
        return snapshot.enabled and not snapshot.triggered
    if not snapshot.enabled:
        return False
    if snapshot.last_notified_at is None:
        return True
    # aware면 UTC로 변환 후 strip(비-UTC aware도 정확, codex review). naive면 UTC 가정 유지
    # (naive에 astimezone 호출 시 시스템 로컬tz 가정 버그 → 분기 분리).
    if now.tzinfo is not None:
        now_naive = now.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        now_naive = now
    elapsed = (now_naive - snapshot.last_notified_at).total_seconds()
    return elapsed >= snapshot.repeat_interval_sec


# ---------------------------------------------------------------------------
# AlertSettingsCache
# ---------------------------------------------------------------------------

class AlertSettingsCache:
    """In-process (source, asset)별 settings cache (TTL=10s).

    Event loop 단일 thread에서 접근 → Lock 불필요. ORM 객체 저장 X,
    `CachedAlertSetting` snapshot만.

    Cache populate 시점에 empty device_tokens settings는 제외 (조건 평가
    자체 의미 없음).

    Future:
        - CRUD invalidation: PR6 직후 별 PR (settings 수정/삭제 시 즉시 갱신)
        - Redis-backed cache: Phase 2 (multi-process)
        - ZSET threshold index: Phase 3 (대규모 settings)
    """

    TTL_SEC = ALERT_CACHE_TTL_SEC

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], CachedBucket] = {}

    def get_if_fresh(self, source: str, asset: str, now: float) -> Optional[CachedBucket]:
        """cache hit + TTL 안이면 bucket 반환, 아니면 None.

        Sync, event loop 안에서 호출. dict lookup ~microsecond.
        """
        bucket = self._cache.get((source, asset))
        if bucket is None:
            return None
        if now - bucket.populated_at >= self.TTL_SEC:
            return None
        return bucket

    def put(
        self,
        source: str,
        asset: str,
        settings: tuple[CachedAlertSetting, ...],
        now: float,
    ) -> None:
        """cache populate. Sync, event loop 안에서 호출."""
        self._cache[(source, asset)] = CachedBucket(settings=settings, populated_at=now)

    def invalidate(self, source: Optional[str] = None, asset: Optional[str] = None) -> None:
        """Cache 항목 삭제.

        - source/asset 모두 지정: 해당 키만 제거 (CRUD invalidation 경로)
        - 둘 다 None: 전체 cache 제거 (test 용도)
        """
        if source is not None and asset is not None:
            self._cache.pop((source, asset), None)
        elif source is None and asset is None:
            self._cache.clear()
        else:
            # 일부만 지정한 경우는 정의 안 함 — 명시적으로 None/None 또는 둘 다 필요.
            raise ValueError("invalidate: source/asset 둘 다 지정 또는 둘 다 None")


# ---------------------------------------------------------------------------
# Module-level singleton (Settings CRUD cache invalidation, Follow-up PR)
# ---------------------------------------------------------------------------
#
# API endpoint (POST/PUT/DELETE source-notification-settings)와 evaluator가
# 같은 cache 인스턴스를 공유 — 사용자가 settings 변경 시 즉시 cache invalidate.
# 단 evaluator runtime state (_tasks, _in_flight_settings, _loading)는
# per-instance 유지 — cache singleton만 공유 (Codex review).
#
# Multi-process 환경에서는 process별로 cache 별도 존재 — Redis pub/sub
# invalidation은 future Phase 2 (multi-process 도입 시 별 PR).

_default_alert_settings_cache: AlertSettingsCache = AlertSettingsCache()


def get_default_alert_settings_cache() -> AlertSettingsCache:
    """Process-wide shared cache. evaluator와 API endpoint 둘 다 호출."""
    return _default_alert_settings_cache


def invalidate_alert_settings_cache(source: str, asset: str) -> None:
    """Settings CRUD endpoint가 호출 — 특정 (source, asset) cache 즉시 무효화.

    PUT에서 source/asset이 바뀐 경우 old + new 둘 다 호출 권장.
    """
    _default_alert_settings_cache.invalidate(source, asset)
    logger.debug(
        "[alert_evaluator] cache invalidated",
        extra={"source": source, "asset": asset},
    )


# ---------------------------------------------------------------------------
# UsdtAlertEvaluator (Upbit-only B1)
# ---------------------------------------------------------------------------

class UsdtAlertEvaluator:
    """Upbit USDT/KRW 알림 evaluator (PR6, B1 once 정책).

    Flow:
        schedule(observation) → background task → _evaluate_async:
            1. cache hit/miss → settings 후보 조회 (per-key loading guard)
            2. condition_matches로 trigger 후보 필터
            3. 각 후보: in-flight guard → _send_one:
                a. Session 1: refetch → FreshSettingSnapshot → close
                b. delivery_allowed 확인 (snapshot 기준)
                c. No session: FCM 발송
                d. Session 2: success → mark_triggered + log success /
                              failure → log failure only
                e. failed_tokens cleanup
    """

    def __init__(
        self,
        *,
        cache: Optional[AlertSettingsCache] = None,
        coalescer: Optional[PriceAlertCoalescer] = None,
        backend: Optional[AlertStorageBackend] = None,
        sender: Optional[Callable[[list[str], str, str, dict], dict]] = None,
    ) -> None:
        # default = module-level singleton (Settings CRUD cache invalidation
        # 위해 API endpoint와 공유). test 시 cache=AlertSettingsCache() inject로 격리.
        self._cache = cache if cache is not None else get_default_alert_settings_cache()
        # 5b-3b: PriceAlertCoalescer composition (§12.8.3 결정 #4).
        # USDT 5 source 공통 instance라 5초 wall-clock A-3 grain coalescing이 5 source
        # 자동 동시 적용. 테스트용 DI 열어둠 (Codex 권장).
        self._coalescer = coalescer if coalescer is not None else PriceAlertCoalescer(window_sec=5)
        # fanout step 4 S1: storage backend (load/refetch/persist/payload). default =
        # source_notification_settings (USDT/KRX byte-identical). FX shadow(S3)는 FxNotificationBackend 주입.
        self._backend = backend if backend is not None else SourceAlertBackend()
        # fanout step 4 S3: FCM delivery seam (backend ABC 밖). default None →
        # _do_send_and_persist에서 (self._sender or self._send_fcm_multicast) per-call late-bind
        # → USDT/KRX byte-identical + 11 patch.object(_send_fcm_multicast) 보존.
        # ⚠️ __init__ pre-resolve 금지 (class-level patch 동결 → 11 patch 우회).
        self._sender = sender
        self._tasks: set[asyncio.Task] = set()
        # In-flight setting guard — 동일 setting_id 동시 평가 시 중복 FCM 차단
        self._in_flight_settings: set[int] = set()
        # Per-key loading guard — TTL 만료 순간 동시 cache miss 시 DB loader 1번만
        self._loading: dict[tuple[str, str], asyncio.Task] = {}

    # -- Public API ---------------------------------------------------------

    def schedule(self, observation: AlertObservation) -> None:
        """observation 입력 → background task. Sync, recv loop blocking 방지.

        observation drop 금지 (Codex Finding 1 — 짧은 threshold crossing
        보호). 100+ pending이면 warning log만, drop 안 함.

        5b-3b: kind 분기 (§12.8.3 결정 #12).
            - kind="tick" → coalescer 적용 (5s wall-clock window), flush된
              PriceAlertEvaluationInput마다 task 생성.
            - kind="rest_probe" (및 미래 kind) → 기존 raw observation path
              즉시 평가 (복구 신호 지연 회피).

        Flush timing — *tick-driven*:
            coalescer는 *다음 tick의 bucket boundary 넘김* 또는 *close()*에서
            flush. 별도 5초 timer cron 없음 (§13.10 "5초마다 검사" 아니라
            "5초 안 관측 묶어 1회 평가" 정책 정합). Upbit 같은 high-traffic
            source는 영향 미미하나, fpm 낮은 source (예: Gopax fpm≈2)는 alert
            평가가 다음 tick 도래까지 지연될 수 있음 — source-specific window
            정밀화는 §12.8.3 후속 결정 영역.
        """
        if len(self._tasks) >= PENDING_TASKS_WARNING_THRESHOLD:
            logger.warning(
                "[alert_evaluator] pending tasks backlog high: %d (no drop)",
                len(self._tasks),
            )

        if observation.kind == "tick":
            flushed = self._coalescer.add(observation)
            for price_input in flushed:
                task = asyncio.create_task(self._evaluate_price_input_async(price_input))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            if flushed:
                # 비교알림 dual-trigger hook (ADR-037 Decision 3 — coalescer 5s grain 뒤,
                # 단일알림과 동일 절제). flag off면 zero-overhead. USDT 5 + KRX(상속) 커버.
                _emit_comparison(observation.source, observation.asset)
        else:
            # rest_probe + 미래 kind — coalescer 우회, 즉시 raw observation 평가
            task = asyncio.create_task(self._evaluate_async(observation))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            _emit_comparison(observation.source, observation.asset)   # 복구 신호도 비교 재평가

    async def close(self, timeout: float = ALERT_CLOSE_TIMEOUT_SEC) -> None:
        """drain-first: pending alert tasks 완료 대기, timeout 후 cancel.

        FCM in-flight 보호 (Codex Finding 3). reconnect / shutdown 양쪽
        시나리오 모두 사용.

        5b-3b: close 진입 시 pending coalescer bucket을 먼저 flush + schedule
        해서 같은 drain cycle 안에 포함. shutdown/reconnect drain은 window
        boundary 보존보다 alert 손실 방지를 우선 (§12.8.3 결정 #4).
        """
        flushed = self._coalescer.flush_pending()
        emitted_pairs: set[tuple[str, str]] = set()
        for price_input in flushed:
            task = asyncio.create_task(self._evaluate_price_input_async(price_input))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            # 비교알림 hook — tick 경로(schedule, line ~434)와 대칭. close/reconnect/KRX 세션 경계
            # drain 시 마지막 coalescer bucket의 비교알림 crossing 누락 방지 (codex 2026-07-11).
            # 이 flush는 KRX `_drain_alert_tick_handlers`(CF 15:45 / CF→CM 갭)가 마지막 bucket 손실을
            # 막으려 존재하는데, 그동안 단일알림만 재평가하고 비교알림은 빠져 있었음. (source,asset)
            # 중복 emit 회피. flag off면 _emit_comparison 내부 zero-overhead. fire-and-forget —
            # 비교 evaluator 자체 loop bridge라 세션 경계(loop 생존) 실행 보장, shutdown은 best-effort.
            pair = (price_input.source, price_input.asset)
            if pair not in emitted_pairs:
                emitted_pairs.add(pair)
                _emit_comparison(price_input.source, price_input.asset)

        if not self._tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[alert_evaluator] close timeout (%ss), cancel %d pending",
                timeout, len(self._tasks),
            )
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            self._tasks.clear()

    # -- Internal -----------------------------------------------------------

    async def _evaluate_async(self, observation: AlertObservation) -> None:
        """평가 main flow — raw observation path. cache → 조건 → 후보 → in-flight guard → send_one_observation.

        rest_probe + 미래 kind 경로. tick은 _evaluate_price_input_async가 처리.
        """
        try:
            settings = await self._get_settings(observation)
            candidates = [s for s in settings if condition_matches(s, observation)]
            if not candidates:
                return
            for candidate in candidates:
                if candidate.setting_id in self._in_flight_settings:
                    logger.debug(
                        "[alert_evaluator] setting %d in-flight, skip duplicate",
                        candidate.setting_id,
                    )
                    continue
                self._in_flight_settings.add(candidate.setting_id)
                try:
                    await self._send_one_observation(candidate, observation)
                finally:
                    self._in_flight_settings.discard(candidate.setting_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[alert_evaluator] evaluator failed (격리, WS session 유지)",
                extra={
                    "source": observation.source,
                    "asset": observation.asset,
                    "rate": observation.rate,
                },
            )

    async def _evaluate_price_input_async(
        self, price_input: PriceAlertEvaluationInput
    ) -> None:
        """평가 main flow — window-aware path (§12.8.3 결정 #9/#10).

        condition_matches_price_input(max/min 기준)으로 1차 필터 후 _send_one_price_input.
        crossing 보존 — last_rate가 아니라 max_rate/min_rate가 threshold를 cross했는지로 평가.
        """
        try:
            settings = await self._get_settings_for_key(
                price_input.source, price_input.asset
            )
            candidates = [
                s for s in settings if condition_matches_price_input(s, price_input)
            ]
            if not candidates:
                return
            for candidate in candidates:
                if candidate.setting_id in self._in_flight_settings:
                    logger.debug(
                        "[alert_evaluator] setting %d in-flight, skip duplicate",
                        candidate.setting_id,
                    )
                    continue
                self._in_flight_settings.add(candidate.setting_id)
                try:
                    await self._send_one_price_input(candidate, price_input)
                finally:
                    self._in_flight_settings.discard(candidate.setting_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "[alert_evaluator] price_input evaluator failed (격리, WS session 유지)",
                extra={
                    "source": price_input.source,
                    "asset": price_input.asset,
                    "min_rate": str(price_input.min_rate),
                    "max_rate": str(price_input.max_rate),
                    "tick_count": price_input.tick_count,
                },
            )

    async def _get_settings_for_key(
        self, source: str, asset: str
    ) -> tuple[CachedAlertSetting, ...]:
        """cache hit는 event loop, miss는 to_thread + per-key loading guard.

        TTL 만료 순간 동시 cache miss → 진행 중 loading task share → DB
        loader 1번만 호출 (thundering herd 방지).

        5b-3b: (source, asset) 기반 — raw observation / price_input 양쪽 path 공통.
        """
        bucket = self._cache.get_if_fresh(source, asset, time.time())
        if bucket is not None:
            return bucket.settings

        key = (source, asset)
        existing = self._loading.get(key)
        if existing is not None and not existing.done():
            return await existing

        task = asyncio.create_task(self._load_and_cache(key))
        self._loading[key] = task
        try:
            return await task
        finally:
            # 다른 task가 같은 key를 새로 register했을 가능성 → 자기 자신일 때만 pop
            if self._loading.get(key) is task:
                self._loading.pop(key, None)

    async def _get_settings(
        self, observation: AlertObservation
    ) -> tuple[CachedAlertSetting, ...]:
        """Backward-compat wrapper — observation에서 source/asset 추출."""
        return await self._get_settings_for_key(observation.source, observation.asset)

    async def _load_and_cache(
        self, key: tuple[str, str]
    ) -> tuple[CachedAlertSetting, ...]:
        """DB query via to_thread → cache populate."""
        source, asset = key
        settings = await asyncio.to_thread(self._backend.load_settings, source, asset)
        self._cache.put(source, asset, settings, time.time())
        return settings

    async def _send_one_observation(
        self, candidate: CachedAlertSetting, observation: AlertObservation
    ) -> None:
        """Raw observation path — refetch + revalidate + send + persist.

        observation.rate 자체를 triggered_rate로 사용 (단일 tick 평가).
        rest_probe + 미래 kind가 이 path 거침.
        """
        fresh_candidate = await self._refetch_and_revalidate_observation(
            candidate, observation
        )
        if fresh_candidate is None:
            return
        triggered_rate = Decimal(str(observation.rate))
        await self._do_send_and_persist(fresh_candidate, triggered_rate)

    async def _send_one_price_input(
        self, candidate: CachedAlertSetting, price_input: PriceAlertEvaluationInput
    ) -> None:
        """Window path (§12.8.3 결정 #11) — refetch + revalidate + matched_triggered_rate + send + persist.

        crossing 보존 — refetch threshold 기준 ``matched_triggered_rate`` 재산출.
        threshold 변경으로 더 이상 만족하지 않으면 발송 차단.
        """
        fresh_candidate = await self._refetch_and_revalidate_price_input(
            candidate, price_input
        )
        if fresh_candidate is None:
            return
        # refetch threshold 기준 triggered_rate 재산출 (cache 옛 값 무시)
        triggered_rate = matched_triggered_rate(fresh_candidate, price_input)
        if triggered_rate is None:
            logger.info(
                "[alert_evaluator] stale cache (window crossing no longer matches), skip",
                extra={
                    "setting_id": candidate.setting_id,
                    "cached_threshold": candidate.threshold,
                    "fresh_threshold": fresh_candidate.threshold,
                    "min_rate": str(price_input.min_rate),
                    "max_rate": str(price_input.max_rate),
                },
            )
            return
        await self._do_send_and_persist(fresh_candidate, triggered_rate)

    async def _refetch_and_revalidate_observation(
        self, candidate: CachedAlertSetting, observation: AlertObservation
    ) -> Optional[CachedAlertSetting]:
        """DB session 1 — refetch + raw observation 재검증. fresh_candidate 또는 None.

        Codex Finding (Medium): cache TTL 동안 source/asset/condition/threshold
        변경 시 refetch로 stale 발송 차단.
        """
        snapshot = await asyncio.to_thread(
            self._backend.refetch_snapshot, candidate.setting_id, candidate.user_id,
        )
        if snapshot is None or not delivery_allowed(snapshot, datetime.now(timezone.utc)):
            logger.info(
                "[alert_evaluator] stale cache (enabled/triggered), skip",
                extra={
                    "setting_id": candidate.setting_id,
                    "snapshot_enabled": snapshot.enabled if snapshot else None,
                    "snapshot_triggered": snapshot.triggered if snapshot else None,
                },
            )
            return None
        if snapshot.source != observation.source or snapshot.asset != observation.asset:
            logger.info(
                "[alert_evaluator] stale cache (source/asset mismatch), skip",
                extra={
                    "setting_id": candidate.setting_id,
                    "cached_source": candidate.source,
                    "cached_asset": candidate.asset,
                    "fresh_source": snapshot.source,
                    "fresh_asset": snapshot.asset,
                },
            )
            return None
        fresh_candidate = CachedAlertSetting(
            setting_id=candidate.setting_id,
            user_id=candidate.user_id,
            source=snapshot.source,
            asset=snapshot.asset,
            condition=snapshot.condition,
            threshold=snapshot.threshold,
            device_tokens=candidate.device_tokens,
            repeat_interval_sec=snapshot.repeat_interval_sec,  # B2 (ADR-036): payload is_repeat 도출
        )
        if not condition_matches_observation(fresh_candidate, observation):
            logger.info(
                "[alert_evaluator] stale cache (condition/threshold no longer matches), skip",
                extra={
                    "setting_id": candidate.setting_id,
                    "cached_condition": candidate.condition,
                    "cached_threshold": candidate.threshold,
                    "fresh_condition": snapshot.condition,
                    "fresh_threshold": snapshot.threshold,
                    "observation_rate": observation.rate,
                },
            )
            return None
        return fresh_candidate

    async def _refetch_and_revalidate_price_input(
        self, candidate: CachedAlertSetting, price_input: PriceAlertEvaluationInput
    ) -> Optional[CachedAlertSetting]:
        """DB session 1 — refetch + window 재검증. fresh_candidate 또는 None.

        source/asset/enabled/triggered stale 검증은 observation path와 동일,
        condition 재검증은 window 기반 (max_rate/min_rate).
        """
        snapshot = await asyncio.to_thread(
            self._backend.refetch_snapshot, candidate.setting_id, candidate.user_id,
        )
        if snapshot is None or not delivery_allowed(snapshot, datetime.now(timezone.utc)):
            logger.info(
                "[alert_evaluator] stale cache (enabled/triggered), skip",
                extra={
                    "setting_id": candidate.setting_id,
                    "snapshot_enabled": snapshot.enabled if snapshot else None,
                    "snapshot_triggered": snapshot.triggered if snapshot else None,
                },
            )
            return None
        if snapshot.source != price_input.source or snapshot.asset != price_input.asset:
            logger.info(
                "[alert_evaluator] stale cache (source/asset mismatch), skip",
                extra={
                    "setting_id": candidate.setting_id,
                    "cached_source": candidate.source,
                    "cached_asset": candidate.asset,
                    "fresh_source": snapshot.source,
                    "fresh_asset": snapshot.asset,
                },
            )
            return None
        return CachedAlertSetting(
            setting_id=candidate.setting_id,
            user_id=candidate.user_id,
            source=snapshot.source,
            asset=snapshot.asset,
            condition=snapshot.condition,
            threshold=snapshot.threshold,
            device_tokens=candidate.device_tokens,
            repeat_interval_sec=snapshot.repeat_interval_sec,  # B2 (ADR-036): payload is_repeat 도출
        )

    async def _do_send_and_persist(
        self, fresh_candidate: CachedAlertSetting, triggered_rate: Decimal
    ) -> None:
        """공통 send + persist — observation/price_input 양쪽 path 끝단.

        payload/log 모두 triggered_rate 기준. _persist_result는 legacy schema
        호환을 위해 float 변환 후 저장.
        """
        title, body, data = self._backend.build_payload(fresh_candidate, triggered_rate)
        result = await asyncio.to_thread(
            (self._sender or self._send_fcm_multicast),
            list(fresh_candidate.device_tokens), title, body, data,
        )
        await asyncio.to_thread(
            self._backend.persist_result, fresh_candidate, float(triggered_rate), result,
        )

    @staticmethod
    def _send_fcm_multicast(
        tokens: list[str], title: str, body: str, data: dict,
    ) -> dict:
        """FCM 발송 wrapper — 함수 내부 import (alert_evaluator 로드 영향 차단)."""
        from app.notifications.fcm import send_fcm_multicast_sync
        return send_fcm_multicast_sync(tokens, title, body, data)


# ---------------------------------------------------------------------------
# KrxAlertEvaluator — KRX 가격 알림 thin wrapper (F-1, 2026-05-26)
# ---------------------------------------------------------------------------

class KrxAlertEvaluator(UsdtAlertEvaluator):
    """KRX 미국달러선물 가격 알림 evaluator — `UsdtAlertEvaluator` thin subclass.

    UsdtAlertEvaluator는 이미 source-neutral (observation.source/asset 기반
    settings cache + condition_matches + refetch + FCM). KRX 전용 별도 evaluator
    class 분리는 코드 중복만 늘리므로 thin wrapper 패턴 채택 (Codex 정정 반영).

    Subclass 분리 이유:
        - 로깅/타이핑 명확성 — log filter / isinstance 검사 / 향후 KRX 전용
          분기가 필요해질 때 자리 마련.
        - F-1 단계 동작 차이 없음 — body는 `pass`. 부모 클래스 로직 그대로 사용.

    정책 anchor (F-1 설계, KRX_ALERT_EVALUATOR_ENABLED flag 참조):
        - SET-only ❌: 모든 tick이 평가 대상 (Stage E SET/SKIPPED 분기는
          mirror layer 영역, alert는 별 계층).
        - Close grace skip ❌: close grace tick도 평가 (종가 crossing 보존).
          mirror layer(`KrxRedisLatestWriter.__call__`)는 close grace skip이지만,
          alert는 사용자 알림 누락 방지 위해 모든 tick 평가.
        - Adapter는 `KrxAlertTickHandler` (`app/crawlers/krx_kis.py`)가 담당 —
          payload → `AlertObservation` 변환 + `schedule(observation)` 호출.

    F-2/F-3 후속:
        - F-2: source_registry KRX phase1_enabled=True + API category="derivative"
          허용. 현재 API endpoint는 KRX 등록 차단 — F-2 진입 전 사용자 등록 불가.
        - F-3: env=true 활성 + 테스트 iOS canary 관찰.
    """
    pass
