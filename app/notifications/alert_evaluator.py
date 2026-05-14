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
from typing import Optional

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


@dataclass(frozen=True)
class CachedAlertSetting:
    """알림 설정 cache snapshot (ORM-free).

    PR6는 once 정책 (enabled+triggered=false 평가만). 미래 B2 확장 자리:
        # repeat_interval_sec: Optional[int] = None
        # last_notified_at: Optional[datetime] = None
    """
    setting_id: int
    user_id: str
    source: str
    asset: str
    condition: str          # "above" | "below"
    threshold: float
    device_tokens: tuple[str, ...]  # FCM multicast 입력


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


# ---------------------------------------------------------------------------
# Pure functions (조건 평가 / 발송 정책)
# ---------------------------------------------------------------------------

def condition_matches(setting: CachedAlertSetting, observation: AlertObservation) -> bool:
    """가격 조건 만족 여부 (above/below 경계 ≥/≤).

    기존 `crud.get_triggered_source_settings_for_rate` 조건 보존.
    """
    if setting.condition == "above" and observation.rate >= setting.threshold:
        return True
    if setting.condition == "below" and observation.rate <= setting.threshold:
        return True
    return False


def delivery_allowed(snapshot: FreshSettingSnapshot, now: datetime) -> bool:
    """발송 가능 여부.

    PR6: once 정책 — `enabled=true and triggered=false`.

    미래 확장 자리 (B2 `repeat_interval_sec` 도입 시):
        if snapshot.repeat_interval_sec is None:
            return snapshot.enabled and not snapshot.triggered
        return (
            snapshot.enabled
            and (snapshot.last_notified_at is None
                 or (now - snapshot.last_notified_at).total_seconds()
                    >= snapshot.repeat_interval_sec)
        )
    """
    return snapshot.enabled and not snapshot.triggered


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

    def __init__(self, *, cache: Optional[AlertSettingsCache] = None) -> None:
        # default = module-level singleton (Settings CRUD cache invalidation
        # 위해 API endpoint와 공유). test 시 cache=AlertSettingsCache() inject로 격리.
        self._cache = cache if cache is not None else get_default_alert_settings_cache()
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
        """
        if len(self._tasks) >= PENDING_TASKS_WARNING_THRESHOLD:
            logger.warning(
                "[alert_evaluator] pending tasks backlog high: %d (no drop)",
                len(self._tasks),
            )
        task = asyncio.create_task(self._evaluate_async(observation))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self, timeout: float = ALERT_CLOSE_TIMEOUT_SEC) -> None:
        """drain-first: pending alert tasks 완료 대기, timeout 후 cancel.

        FCM in-flight 보호 (Codex Finding 3). reconnect / shutdown 양쪽
        시나리오 모두 사용.
        """
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
        """평가 main flow. cache → 조건 → 후보 → in-flight guard → send_one."""
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
                    await self._send_one(candidate, observation)
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

    async def _get_settings(
        self, observation: AlertObservation
    ) -> tuple[CachedAlertSetting, ...]:
        """cache hit는 event loop, miss는 to_thread + per-key loading guard.

        TTL 만료 순간 동시 cache miss → 진행 중 loading task share → DB
        loader 1번만 호출 (thundering herd 방지).
        """
        bucket = self._cache.get_if_fresh(observation.source, observation.asset, time.time())
        if bucket is not None:
            return bucket.settings

        key = (observation.source, observation.asset)
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

    async def _load_and_cache(
        self, key: tuple[str, str]
    ) -> tuple[CachedAlertSetting, ...]:
        """DB query via to_thread → cache populate."""
        source, asset = key
        settings = await asyncio.to_thread(self._load_settings_from_db, source, asset)
        self._cache.put(source, asset, settings, time.time())
        return settings

    @staticmethod
    def _load_settings_from_db(
        source: str, asset: str
    ) -> tuple[CachedAlertSetting, ...]:
        """sync DB query — to_thread 내부 실행. ORM → CachedAlertSetting snapshot 변환.

        Empty device_tokens settings는 제외 (조건 평가 의미 없음).
        """
        from app import crud, models
        from app.database import get_db_context

        with get_db_context() as db:
            settings = db.query(models.SourceNotificationSetting).filter(
                models.SourceNotificationSetting.source == source,
                models.SourceNotificationSetting.asset == asset,
                models.SourceNotificationSetting.enabled == True,  # noqa: E712
                models.SourceNotificationSetting.triggered == False,  # noqa: E712
            ).all()

            if not settings:
                return tuple()

            # devices 일괄 조회 (user_id 기반)
            user_ids = {s.user_id for s in settings}
            devices = db.query(models.UserDevice).filter(
                models.UserDevice.user_id.in_(user_ids),
            ).all()

            tokens_by_user: dict[str, list[str]] = {}
            for dev in devices:
                tokens_by_user.setdefault(dev.user_id, []).append(dev.device_token)

            snapshots = []
            for setting in settings:
                tokens = tuple(tokens_by_user.get(setting.user_id, ()))
                if not tokens:
                    # empty device_tokens 제외 (조건 평가 + FCM 의미 없음)
                    continue
                snapshots.append(CachedAlertSetting(
                    setting_id=setting.id,
                    user_id=setting.user_id,
                    source=setting.source,
                    asset=setting.asset,
                    condition=setting.condition,
                    threshold=setting.threshold,
                    device_tokens=tokens,
                ))
            return tuple(snapshots)

    async def _send_one(
        self, candidate: CachedAlertSetting, observation: AlertObservation
    ) -> None:
        """단일 candidate에 대한 발송 — DB session 3-step 분리.

        Session 1 (refetch): enabled/triggered 재확인 (cache stale 보호) →
                             FreshSettingSnapshot → session close
        No session:          FCM send (network I/O, DB connection 미보유)
        Session 2 (persist): mark_triggered + log + failed_tokens cleanup
        """
        snapshot = await asyncio.to_thread(
            self._refetch_setting_snapshot, candidate.setting_id, candidate.user_id,
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
            return

        # Codex Finding (Medium): cache TTL 동안 사용자가 source/asset/condition/
        # threshold를 변경했어도 refetch로 최종 보호. cache의 옛 값으로 발송 차단.
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
            return

        # Refresh candidate with snapshot의 최신 condition/threshold (device_tokens는
        # cache 신뢰 — token cleanup은 failed_tokens cleanup이 처리)
        fresh_candidate = CachedAlertSetting(
            setting_id=candidate.setting_id,
            user_id=candidate.user_id,
            source=snapshot.source,
            asset=snapshot.asset,
            condition=snapshot.condition,
            threshold=snapshot.threshold,
            device_tokens=candidate.device_tokens,
        )
        if not condition_matches(fresh_candidate, observation):
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
            return

        # FCM 발송 (no DB session). fresh_candidate 기준 payload.
        title, body, data = self._build_fcm_payload(fresh_candidate, observation)
        result = await asyncio.to_thread(
            self._send_fcm_multicast,
            list(fresh_candidate.device_tokens), title, body, data,
        )

        # mark_triggered + log + failed_tokens cleanup. fresh_candidate 기준 log.
        await asyncio.to_thread(
            self._persist_result, fresh_candidate, observation.rate, result,
        )

    @staticmethod
    def _refetch_setting_snapshot(
        setting_id: int, user_id: str
    ) -> Optional[FreshSettingSnapshot]:
        """Session 1 — refetch + snapshot 추출 (ORM 객체 escape 차단)."""
        from app import crud
        from app.database import get_db_context

        with get_db_context() as db:
            setting = crud.get_source_notification_setting_by_id(
                db=db, setting_id=setting_id, user_id=user_id,
            )
            if setting is None:
                return None
            return FreshSettingSnapshot(
                setting_id=setting.id,
                enabled=setting.enabled,
                triggered=setting.triggered,
                source=setting.source,
                asset=setting.asset,
                condition=setting.condition,
                threshold=setting.threshold,
            )

    @staticmethod
    def _send_fcm_multicast(
        tokens: list[str], title: str, body: str, data: dict,
    ) -> dict:
        """FCM 발송 wrapper — 함수 내부 import (alert_evaluator 로드 영향 차단)."""
        from app.notifications.fcm import send_fcm_multicast_sync
        return send_fcm_multicast_sync(tokens, title, body, data)

    @staticmethod
    def _persist_result(
        candidate: CachedAlertSetting, rate: float, fcm_result: dict,
    ) -> None:
        """Session 2 — success → mark_triggered + log success / fail → log failure only.

        기존 `process_source_rate_alerts` 순서 보존. FCM 실패가 setting을
        disabled 처리하면 사용자 알림 영영 X.
        """
        from app import crud, models
        from app.database import get_db_context

        with get_db_context() as db:
            if fcm_result["success_count"] > 0:
                crud.mark_source_setting_triggered(
                    db=db, setting_id=candidate.setting_id, rate=rate,
                )
                crud.create_source_notification_log(
                    db=db,
                    user_id=candidate.user_id,
                    setting_id=candidate.setting_id,
                    source=candidate.source,
                    asset=candidate.asset,
                    condition=candidate.condition,
                    threshold=candidate.threshold,
                    triggered_rate=rate,
                    success=True,
                )
                logger.info(
                    "🔔 alert_evaluator FCM sent",
                    extra={
                        "event": "alert_fcm_sent",
                        "source": candidate.source,
                        "asset": candidate.asset,
                        "rate": rate,
                        "threshold": candidate.threshold,
                        "setting_id": candidate.setting_id,
                        "user_id": candidate.user_id[:8] + "...",
                        "success_count": fcm_result["success_count"],
                    },
                )
            else:
                err_msg = fcm_result.get("error") or "no successful sends"
                crud.create_source_notification_log(
                    db=db,
                    user_id=candidate.user_id,
                    setting_id=candidate.setting_id,
                    source=candidate.source,
                    asset=candidate.asset,
                    condition=candidate.condition,
                    threshold=candidate.threshold,
                    triggered_rate=rate,
                    success=False,
                    error_message=err_msg,
                )

            # failed_tokens cleanup (기존 패턴 동일)
            failed_tokens = fcm_result.get("failed_tokens") or []
            if failed_tokens:
                try:
                    deleted = db.query(models.UserDevice).filter(
                        models.UserDevice.device_token.in_(failed_tokens),
                    ).delete(synchronize_session=False)
                    db.commit()
                    logger.info(
                        "[alert_evaluator] failed_tokens cleanup",
                        extra={"deleted_count": deleted, "tokens": len(failed_tokens)},
                    )
                except Exception:
                    logger.exception("[alert_evaluator] failed_tokens cleanup 실패")

    @staticmethod
    def _build_fcm_payload(
        candidate: CachedAlertSetting, observation: AlertObservation,
    ) -> tuple[str, str, dict]:
        """FCM title/body/data 생성 — 기존 `process_source_rate_alerts` 패턴 보존.

        format_threshold + source_registry display_name 사용.
        """
        from app import source_registry
        from app.crud import format_threshold

        definition = source_registry.get_source_definition(candidate.source, candidate.asset)
        source_display = definition.display_name if definition else candidate.source.upper()
        asset_display = candidate.asset.upper()

        icon = "📈" if candidate.condition == "above" else "📉"
        title = f"{icon}  {source_display}  {asset_display}"

        condition_arrow = "↑" if candidate.condition == "above" else "↓"
        condition_text = "이상" if candidate.condition == "above" else "이하"
        threshold_str = format_threshold(candidate.threshold)
        rate_str = f"{observation.rate:.2f}"
        body = f"[ {threshold_str} {condition_arrow}{condition_text} 도달 ]   {rate_str}"

        data = {
            "type": "source_rate_alert",
            "title": title,
            "body": body,
            "source": candidate.source,
            "asset": candidate.asset,
            "rate": str(observation.rate),
            "threshold": str(candidate.threshold),
            "condition": candidate.condition,
            "setting_id": str(candidate.setting_id),
        }
        return title, body, data
