"""USDT WebSocket — Bithumb canary client (Phase B.3 Stage U2-U7).

USDT_WS_DESIGN_PLAN §12.5 Phase B.3 Stage U2-U7.
Upbit Phase B.1 PR1-PR7 패턴 작은 복제 (KRX close finalizer 분할 학습 — 큰 추상화 금지).

U2 누적 (lifecycle skeleton):
    - `_stop_event` + `_running` (network/Redis/DB import 없음)
    - flag=false 시 scheduler가 BithumbWsClient 자체를 생성 안 함 (acceptance)

U3 누적 (connect/subscribe/parse + log only):
    - Bithumb WS endpoint + ticket + target code 상수
    - `_build_subscribe_payload` (Upbit-compatible)
    - `_parse_ticker_message` (Upbit-compatible payload format)
    - `_handle_message` (parse + first_tick log + tick log)
    - `_run_one_session` (single connect + subscribe + recv loop)

U4 누적 (UsdtLivenessMonitor 재사용 + reconnect + ping/pong heartbeat):
    - `UsdtLivenessMonitor` 재사용 (source-neutral, app/crawlers/usdt_ws/upbit.py:125)
    - `_status` + `_status_transition_count` + `_reconnect_attempt_count`
    - `_set_status` — U4 당시 counter + log only (fallback hook은 U6에서 추가)
    - `_compute_backoff` — Upbit 동일 sequence (1, 2, 4, 8, 16, 30s tail)
    - `_ping_loop` — 20s ping + pong → `_liveness.observe_heartbeat` (§5 provisional 30s)
    - `_run_one_session` 확장 — liveness reset + stale check + ping_task lifecycle
    - `start` reconnect loop — Upbit PR3 backoff sequence mirror

U7 (현재): UsdtAlertEvaluator wiring + AlertObservation schedule.
    - `UsdtAlertEvaluator` (source-neutral, app/notifications/alert_evaluator.py 재사용)
      `BithumbWsClient` 자체 instance 보유 (Upbit upbit.py:618 mirror)
    - `BithumbRestFallbackController` `alert_evaluator` 인자 추가 (inject 방식,
      Upbit upbit.py:621-625 mirror)
    - `_run_one_session` valid tick path → `alert_evaluator.schedule(AlertObservation(...,
      kind="tick"))` 추가
    - REST fallback probe success → `alert_evaluator.schedule(AlertObservation(...,
      kind="rest_probe"))` (Upbit upbit.py:539-546 mirror)
    - `_run_one_session` finally close 순서: fallback → DB → Alert → Redis (4개)
    - alert evaluator close 예외 격리 (try/except로 다른 close 호출 보호)

U7 핵심 acceptance (8개, Codex 합의):
    1. AlertObservation kind="tick" / "rest_probe" 분기 schedule
    2. WS valid tick → Redis + DB + Alert schedule
    3. REST probe success → Redis + DB + Alert schedule (kind="rest_probe")
    4. invalid frame → Alert schedule 0
    5. fallback None (REST 실패/invalid) → Alert schedule 0 (Redis/DB도 0)
    6. close 순서: fallback → DB → Alert → Redis (4개, Upbit upbit.py:920-940 mirror)
    7. flag=false invariant: client + UsdtAlertEvaluator 생성 0, network/Redis/DB/
       REST/Alert 0
    8. alert evaluator close 예외/timeout 격리 — WS loop 전파 X

U6 누적 — BithumbDbWriter + BithumbRestFallbackController + normalized REST helper.

U5 누적 — BithumbRedisWriter (tick-level Redis latest write) + tether topic trigger.
    - `BithumbRedisWriter` 클래스 (Upbit `UpbitRedisWriter` upbit.py:216-325 복제,
      source는 tick["source"]="bithumb" 그대로 사용 — 추상화 안 함, KRX 학습 적용)
    - `__init__`에서 writer 1회 생성 (reconnect 사이 재사용)
    - `_run_one_session` valid tick path → `self._redis_writer.schedule(tick)`
    - `_run_one_session` finally → `await self._redis_writer.close()`
      (Upbit upbit.py:938 mirror, session-level close + reconnect 시 같은 instance 재사용)
    - `stop()`은 변경 없음 — Upbit parity (stop은 _stop_event.set만, writer close X)

U5 핵심 acceptance (13개, Codex 합의):
    기능 7:
    1. flag=false → Redis write 0, trigger 0, client 생성 0
    2. UpbitRedisWriter 패턴 복제, source는 tick["source"]="bithumb"
    3. set_latest_usdt_rate_from_sync_job 성공 시에만 trigger
    4. Redis helper False/예외 시 trigger 미호출
    5. trigger 예외 → writer/WS loop 미전파
    6. DB writer / REST fallback U5에서는 0 (U6 영역)
    7. valid tick만 writer.schedule(), invalid frame은 schedule 0
    운영 5:
    8. MAX_PENDING_WRITES=20 saturation guard (Redis 장애 시 task 폭증 차단)
    9. `_write_lock` Redis SET 직렬화 (병렬 to_thread race 차단)
    10. `asyncio.to_thread`로 sync helper 격리 (event loop non-blocking)
    11. `close()` drain (1s) + timeout cancel
    12. TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS 재사용 (telemetry 분기 폭증 방지)
    lifecycle 1:
    13. session-level close + reconnect 재사용 — `__init__`에서 1회 생성,
        매 `_run_one_session` finally에서 close() drain + _tasks.clear(),
        다음 reconnect session에서 같은 writer instance가 빈 pending set으로 재시작

Phase B.3 6 stage 분할 완료 (U2-U7). 후속 phase 영역:
    - Phase B.4 (예정): Coinone WS 확장 — 별 protocol (Upbit 패턴 비호환)
    - Phase B.5 (예정): Korbit WS 확장
    - Phase B.6 (예정): Gopax WS 확장
    - 공통화 검토: Bithumb (Phase B.3) + Coinone (Phase B.4) land 후 중복 명확해진
      시점에 base class 도입 판단 (선제 abstraction 금지 — KRX close finalizer 학습)

운영 활성화 조건 (USDT_WS_DESIGN_PLAN §12.5.3):
    KRX close finalizer 5/18 CF + 5/19 CM 첫 실측 + 7일 telemetry 안정 후
    별도 deploy GO. 그 전까지 USDT_WS_BITHUMB_ENABLED=false default.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from app import latest_rates_cache, tether_topic_trigger
# UsdtLivenessMonitor 재사용 — source-neutral (Upbit 패턴 import)
from app.crawlers.usdt_ws.upbit import UsdtLivenessMonitor
# UsdtAlertEvaluator + AlertObservation 재사용 — source-neutral (Upbit/Bithumb 공통,
# Phase B.1 PR6 source-neutral 설계 적용).
from app.notifications.alert_evaluator import (
    AlertObservation,
    UsdtAlertEvaluator,
)
from app.tether_topic_trigger import (
    TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
)

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.bithumb")

_KST = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────────────────────────────────
# Constants — Upbit-compatible (USDT_EXCHANGE_WEBSOCKET_GUIDE §4)
# ─────────────────────────────────────────────────────────────────────────

BITHUMB_WS_URL = "wss://ws-api.bithumb.com/websocket/v1"
BITHUMB_SUBSCRIBE_TICKET = "fxi-usdt-bithumb"
BITHUMB_TARGET_CODE = "KRW-USDT"

# U3 recv loop timeout — Upbit 동일 (stop_event 즉시 반응 + 정상 idle 허용)
RECV_TIMEOUT_SEC = 1.0

# U4 §5 silence threshold — Bithumb heartbeat 공식 미명시 (USDT_EXCHANGE_WEBSOCKET_GUIDE
# §4 + §1.1 Phase B.0 재확인 2026-05-14) → provisional 30s 유지 (Upbit 동일).
STALE_AFTER_SEC = 30.0

# U4 Explicit heartbeat — websockets 자동 ping/pong은 recv()로 노출 X.
# §5 frame/heartbeat liveness 관찰 위해 직접 ping 관리 (Upbit/KRX 패턴 mirror).
PING_INTERVAL_SEC = 20.0  # 매 20s ping (STALE_AFTER_SEC 30s 이내 회복)
PING_TIMEOUT_SEC = 10.0   # pong 대기 시간

# U4 Reconnect backoff sequence — Upbit/KRX 동일.
RECONNECT_BACKOFF_SEQ = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RECONNECT_BACKOFF_TAIL = 30.0

# U5 Redis writer guards — Upbit 동일 값 (upbit.py:114-115).
MAX_PENDING_WRITES = 20         # task 폭증 안전망 (Redis 장애 시)
REDIS_CLOSE_TIMEOUT_SEC = 1.0   # close() drain timeout — 후 강제 cancel

# U6 DB writer — Upbit 동일 값 (upbit.py:118).
DB_WRITE_WINDOW_SEC = 1.0       # debounce window (KRX KrxDbWriter 동일)

# U6 REST fallback controller — Upbit 동일 값 (upbit.py:121-122).
FALLBACK_COOLDOWN_SEC = 30.0       # stale 지속 중 probe 빈도 제한 (= STALE_AFTER_SEC)
FALLBACK_PROBE_TIMEOUT_SEC = 10.0  # REST HTTP timeout

# Summary log follow-up (Phase B.5 §12.7.5 후속) — 60s cycle metric INFO emit.
# Korbit/Upbit summary log 패턴 mirror.
# PR 2c (Bithumb bundle): Bithumb은 Upbit 1-dim status + Korbit REST fallback union이라
# 9 fields emit (state 7 + redis_saturation_count + fallback_probe_scheduled_count).
# 향후 PR scope: probe lifecycle breakdown counter (success/timeout/none/cooldown_skip/
# in_flight_skip) — 별도.
SUMMARY_LOG_INTERVAL_SEC = 60.0


class BithumbRedisWriter:
    """Bithumb USDT/KRW Redis latest writer — tick-level, fire-and-forget.

    Upbit `UpbitRedisWriter` (upbit.py:216-325) 복제. source는 tick["source"]에서
    "bithumb"으로 자동 — class-level source 인자 없음 (KRX 학습: stage 추가 시 큰
    추상화 금지). generic UsdtRedisWriter 추상화는 거래소 3개 이상 누적 시점(U7+)
    별도 refactor PR로 검토.

    U5 핵심 (Upbit PR4 mirror):
        - 매 valid tick → `set_latest_usdt_rate_from_sync_job` 호출 (debounce 없음)
        - schedule()은 background task 생성 후 즉시 반환 (recv loop 격리)
        - asyncio.to_thread로 sync helper 호출 (event loop non-blocking)
        - MAX_PENDING_WRITES 한도로 Redis 장애 시 task 폭증 차단
        - close() drain-first, timeout 후 cancel (마지막 tick 보존)
        - Redis write 성공 시에만 tether topic trigger (lock 밖에서 호출)
        - reason은 기존 TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS 재사용

    Failure isolation:
        - helper False 반환 / exception → log only, WS session 영향 X
        - trigger 예외 → log only, writer/WS session 영향 X
        - last-write-wins (helper 기존 정책 유지)
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        # write order 직렬화: 병렬 to_thread + connection pool 조합으로
        # old tick이 new tick을 덮는 race 차단. lock은 to_thread 완료까지 보유
        # — schedule 순서 = Redis SET 순서. (Upbit Codex review 산물 mirror)
        self._write_lock: asyncio.Lock = asyncio.Lock()
        # PR 2c — Redis write queue saturation skip 누적 (Upbit PR 2b mirror).
        # 의미: MAX_PENDING_WRITES skip 발생 횟수. write 성공/실패 무관.
        self._saturation_count: int = 0

    @property
    def saturation_count(self) -> int:
        """PR 2c — Redis write queue saturation skip 누적 횟수 (read-only)."""
        return self._saturation_count

    def schedule(self, tick: dict) -> None:
        """tick → background Redis write task. fire-and-forget.

        Saturation 시 (`len(_tasks) >= MAX_PENDING_WRITES`) skip + warning + counter ++.
        Redis 장애로 task가 누적되는 시나리오 차단 (A8).
        """
        if len(self._tasks) >= MAX_PENDING_WRITES:
            self._saturation_count += 1
            logger.warning(
                "[usdt_ws.bithumb] Redis write queue saturated (%d in-flight) — skip tick",
                len(self._tasks),
            )
            return
        task = asyncio.create_task(self._write_async(tick))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write_async(self, tick: dict) -> None:
        """sync Redis helper를 to_thread로 호출. 실패는 격리.

        `_write_lock`으로 직렬화 (A9) — 동시 to_thread + connection pool 조합에서
        발생할 수 있는 SET 순서 역전 차단.

        Redis write 성공 시 lock 밖에서 tether topic trigger 호출 (3).
        trigger 예외는 writer/WS session에 전파하지 않음 (5).
        """
        outcome: latest_rates_cache.UsdtLatestWriteOutcome = (
            latest_rates_cache.UsdtLatestWriteOutcome.FAILED
        )
        source = tick["source"]
        asset = tick["asset"]
        async with self._write_lock:
            ts_iso = datetime.fromtimestamp(tick["timestamp_ms"] / 1000, tz=_KST).isoformat()
            try:
                outcome = await asyncio.to_thread(
                    latest_rates_cache.set_latest_usdt_rate_from_sync_job,
                    source=source,
                    asset=asset,
                    rate=tick["rate"],
                    timestamp=ts_iso,
                )
                if outcome is latest_rates_cache.UsdtLatestWriteOutcome.FAILED:
                    logger.warning(
                        "[usdt_ws.bithumb] Redis write returned FAILED "
                        "(helper 내부 log 참조, WS session 유지)"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[usdt_ws.bithumb] Redis write task crashed (격리, WS session 유지)"
                )
                outcome = latest_rates_cache.UsdtLatestWriteOutcome.FAILED

        # (5d-a) lock 밖에서 trigger — Redis SET 성공 시에만 (change notification).
        # SKIPPED (5s grain coalesce) / FAILED 시 trigger 차단.
        # trigger 호출 자체의 예외는 writer/WS에 전파 X (5, 책임 분리).
        if outcome is latest_rates_cache.UsdtLatestWriteOutcome.SET:
            try:
                tether_topic_trigger.request_tether_topic_trigger(
                    source=source,
                    asset=asset,
                    reason=TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
                )
            except Exception:
                logger.exception(
                    "[usdt_ws.bithumb] tether topic trigger 호출 실패 (격리, WS session 유지)"
                )

    async def close(self, timeout: float = REDIS_CLOSE_TIMEOUT_SEC) -> None:
        """pending writes drain (A11) — session/shutdown 시 마지막 tick latest 보존.

        timeout (default 1s) 안에 drain 안 되면 강제 cancel. Redis hung 시 무한
        대기 방지. finally에서 `_tasks.clear()` 보장 (A13 lifecycle invariant).
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
                "[usdt_ws.bithumb] Redis writer close timeout (%ds) — cancel %d pending tasks",
                timeout, len(self._tasks),
            )
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            self._tasks.clear()


class BithumbDbWriter:
    """Bithumb USDT/KRW DB writer — 1s window debounce + insert_source_rate_if_changed.

    Upbit `UpbitDbWriter` (upbit.py:328-434) 복제. tick rate ~100ms+ vs DB I/O
    ~10-50ms이라 매 tick INSERT는 DB 부담. window 내 마지막 tick만 helper 호출.

    asyncio loop 차단 방지:
        sync SQLAlchemy 호출은 asyncio.to_thread로 격리. DB session은
        to_thread 내부에서 get_db_context()로 생성/close.

    race 방지 (KRX PR6b-2b Codex 보정 + Upbit mirror):
        DB write (to_thread) 진행 중 새 tick 도착 → _pending_tick 갱신,
        _timer.done() X 라 schedule()이 새 timer 안 만듦. write 종료 후
        finally 블록에서 _pending_tick 재확인 후 새 timer 예약 — 누락 방지.

    failure isolation (A4): DB exception → log only, WS session 영향 X.

    shutdown (close, A3): pending tick 1초 기다리지 않고 즉시 flush.
        window 일관성보다 last tick 보장 우선.
    """

    def __init__(self, *, window_sec: float = DB_WRITE_WINDOW_SEC) -> None:
        self._window_sec = window_sec
        self._pending_tick: Optional[dict] = None
        self._timer: Optional[asyncio.Task] = None

    def schedule(self, tick: dict) -> None:
        """tick 입력 → pending 갱신 + window timer 시작/유지. sync, 즉시 반환."""
        self._pending_tick = tick
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._flush_after_window())

    async def _flush_after_window(self) -> None:
        """window 만료 → last pending tick을 helper에 전달.

        write 진행 중 새 tick 도착 시 finally에서 새 timer 예약 (race 방지).
        """
        try:
            await asyncio.sleep(self._window_sec)
        except asyncio.CancelledError:
            raise
        tick = self._pending_tick
        self._pending_tick = None
        if tick is None:
            return
        try:
            await asyncio.to_thread(self._sync_db_write, tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[usdt_ws.bithumb] DB write failed (격리): %s: %s",
                type(exc).__name__, exc,
            )
        finally:
            # write 중 들어온 tick 처리 — 새 window 예약. finally 블록은
            # 같은 코루틴 frame 안, await 없는 sync 영역이라 다른 코루틴
            # race X (schedule()이 _timer.done() 체크 전 새 task 할당).
            if self._pending_tick is not None:
                self._timer = asyncio.create_task(self._flush_after_window())

    @staticmethod
    def _sync_db_write(tick: dict) -> None:
        """sync DB write — to_thread 내부 실행.

        get_db_context()로 SessionLocal 생성/close. insert_source_rate_if_changed는
        내부에서 commit. Decimal 정규화 없음 (REST polling
        `usdt_sources.collect_usdt_rates`와 일관, 둘 다 float 그대로 전달).
        """
        # 함수 내부 import — 모듈 로드 시 DB 의존성 격리.
        from app import crud
        from app.database import get_db_context

        with get_db_context() as db:
            crud.insert_source_rate_if_changed(
                db=db,
                source=tick["source"],
                asset=tick["asset"],
                rate=tick["rate"],
                # §12.9.8 ③ super-lite — exchange event ts를 저장 시각으로 (now() 아님).
                #   out-of-order stale tick이 latest로 오판되지 않게 (timestamp DESC 쿼리).
                timestamp=crud.event_ms_to_utc_naive(tick["timestamp_ms"]),
            )

    async def close(self) -> None:
        """shutdown 시 timer cancel + pending tick 즉시 flush.

        1초 window 기다리지 않고 마지막 tick DB 저장. window 일관성보다
        last tick 보장 우선 (shutdown은 드문 이벤트).
        """
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
            try:
                await self._timer
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[usdt_ws.bithumb] db_writer timer cancel 실패")
        self._timer = None
        tick = self._pending_tick
        self._pending_tick = None
        if tick is None:
            return
        try:
            await asyncio.to_thread(self._sync_db_write, tick)
        except Exception as exc:
            logger.warning(
                "[usdt_ws.bithumb] DB write on close failed (격리): %s: %s",
                type(exc).__name__, exc,
            )


class BithumbRestFallbackController:
    """Bithumb REST fallback (silent probe 옵션 B).

    Upbit `UpbitRestFallbackController` (upbit.py:437-585) 복제. U7에서 alert evaluator
    wiring 추가 (Upbit upbit.py:621-625 inject 패턴 mirror).

    WS frame/heartbeat silence (last_activity_at 기준) 감지 시 REST 1회 probe →
    normalized tick → 기존 fanout (Redis + DB + Alert) 재사용. WS reconnect loop는
    그대로 유지, fallback은 freshness 보조.

    Guardrails (Upbit mirror):
        - schedule_probe()는 sync/non-blocking (_set_status 동기 흐름에서 호출).
        - In-flight skip: probe 진행 중 중복 trigger 차단.
        - Cooldown skip: 마지막 probe 종료 후 FALLBACK_COOLDOWN_SEC 미경과 시 skip
          (stale 지속 중 폭주 방지).
        - Probe 실패도 cooldown 적용 (REST rate limit 보호).
        - reset_cooldown(): normal 복귀 시 호출 → 다음 stale 즉시 1회 probe 보장.
        - REST 실패 격리: log only, WS session/reconnect 영향 X.
        - fallback tick의 source/asset = "bithumb"/"usdt-krw" (downstream 일관).
        - U7 alert wiring: probe success → Redis + DB + Alert schedule.
          AlertObservation kind="rest_probe" (log/metric 구분, Upbit PR6 mirror).
    """

    def __init__(
        self,
        redis_writer: "BithumbRedisWriter",
        db_writer: "BithumbDbWriter",
        alert_evaluator: "UsdtAlertEvaluator",
        *,
        cooldown_sec: float = FALLBACK_COOLDOWN_SEC,
        probe_timeout_sec: float = FALLBACK_PROBE_TIMEOUT_SEC,
    ) -> None:
        self._redis_writer = redis_writer
        self._db_writer = db_writer
        self._alert_evaluator = alert_evaluator
        self._cooldown_sec = cooldown_sec
        self._probe_timeout_sec = probe_timeout_sec
        self._in_flight: bool = False
        self._cooldown_until: float = 0.0
        self._pending_task: Optional[asyncio.Task] = None
        # PR 2c — scheduled probe 누적 (Korbit PR 2a mirror).
        # 의미: loop.create_task() 성공 시점 기준 +1. in-flight/cooldown/no-loop skip 미증가.
        # success/timeout/exception 무관 — schedule 자체만 카운팅.
        self._scheduled_probe_count: int = 0

    @property
    def scheduled_probe_count(self) -> int:
        """PR 2c — REST fallback probe schedule 누적 횟수 (read-only, instance lifetime)."""
        return self._scheduled_probe_count

    def schedule_probe(self, reason: str) -> None:
        """sync: in-flight/cooldown 체크 후 background probe task 생성. 즉시 반환.

        `_set_status("stale")` 안에서 호출 — 동기 흐름이라 즉시 반환 필수.
        Production은 항상 asyncio context 안 (recv loop / reconnect loop), 단
        sync test에서 _set_status 직접 호출 시 no-loop. defensive하게 skip.
        """
        now = time.time()
        if self._in_flight:
            logger.debug(
                "[usdt_ws.bithumb.fallback] probe in-flight, skip (reason=%s)", reason,
            )
            return
        if now < self._cooldown_until:
            remaining = self._cooldown_until - now
            logger.debug(
                "[usdt_ws.bithumb.fallback] cooldown active, skip (%.1fs remaining, reason=%s)",
                remaining, reason,
            )
            return
        # Loop 체크를 coroutine 생성 전에 — no-loop 시 coroutine 미생성으로
        # "coroutine was never awaited" warning 회피.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[usdt_ws.bithumb.fallback] no event loop, skip probe (reason=%s)", reason,
            )
            return
        self._pending_task = loop.create_task(self._run_probe(reason))
        self._in_flight = True
        # PR 2c — schedule 성공 시점 누적 (skip 분기들은 위에서 이미 return).
        self._scheduled_probe_count += 1

    def reset_cooldown(self) -> None:
        """normal 복귀 시 호출 — cooldown clear. 다음 stale 즉시 1회 probe 보장 (A8)."""
        self._cooldown_until = 0.0
        logger.debug("[usdt_ws.bithumb.fallback] cooldown reset on normal recovery")

    async def _run_probe(self, reason: str) -> None:
        """REST probe → normalized tick → fanout (Redis + DB + Alert).

        실패 시 log only + cooldown 적용 (재시도 X). WS session 영향 X.
        U7 alert wiring: AlertObservation kind="rest_probe" schedule (Upbit upbit.py:539-546 mirror).
        """
        try:
            logger.info(
                "[usdt_ws.bithumb.fallback] probe start (reason=%s)", reason,
            )
            tick = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_bithumb_tick),
                timeout=self._probe_timeout_sec,
            )
            if tick is None:
                logger.warning(
                    "[usdt_ws.bithumb.fallback] probe returned None — REST/parse 실패 or invalid rate",
                )
                return

            # 기존 fanout 재사용 (U5 Redis writer + U6 DB writer + U7 Alert evaluator)
            self._redis_writer.schedule(tick)
            self._db_writer.schedule(tick)
            observation = AlertObservation(
                source=tick["source"],
                asset=tick["asset"],
                rate=tick["rate"],
                timestamp_ms=tick["timestamp_ms"],
                kind="rest_probe",  # U7: REST probe kind 구분 (log/metric)
            )
            self._alert_evaluator.schedule(observation)
            logger.info(
                "[usdt_ws.bithumb.fallback] probe success (rate=%s, ts_ms=%d, reason=%s)",
                tick["rate"], tick["timestamp_ms"], reason,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "[usdt_ws.bithumb.fallback] probe timeout (%.1fs, reason=%s)",
                self._probe_timeout_sec, reason,
            )
        except Exception:
            logger.exception(
                "[usdt_ws.bithumb.fallback] probe error (격리, reason=%s)", reason,
            )
        finally:
            # 실패해도 cooldown 적용 (REST rate limit 보호).
            self._cooldown_until = time.time() + self._cooldown_sec
            self._in_flight = False

    @staticmethod
    def _fetch_bithumb_tick() -> Optional[dict]:
        """sync REST fetch — to_thread 내부 실행. 모듈 내부 import (순환 회피)."""
        from app.crawlers.usdt_sources import fetch_bithumb_usdt_tick
        return fetch_bithumb_usdt_tick()

    async def close(self) -> None:
        """pending probe task cancel + await. WS session 종료 시 호출."""
        task = self._pending_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[usdt_ws.bithumb.fallback] close error")
        self._pending_task = None
        self._in_flight = False


class BithumbWsClient:
    """Bithumb USDT/KRW WebSocket client — U2-U7 누적 (Phase B.3 완료).

    State:
        - `_stop_event`: stop signal (asyncio.Event)
        - `_running`: 중복 start 방지 flag
        - `_ws`: active WebSocketClientProtocol (session 안에서만)
        - `_first_tick_logged`: first tick INFO log 1회 emit 후 DEBUG로 격하
        - `_liveness`: UsdtLivenessMonitor (frame/heartbeat gap state, U4)
        - `_status`: "normal" | "reconnecting" | "stale"
        - `_status_transition_count`: status별 전이 count (telemetry)
        - `_reconnect_attempt_count`: ConnectionClosed/Exception 누적 attempt
        - `_redis_writer`: BithumbRedisWriter (tick-level fire-and-forget, U5)
        - `_db_writer`: BithumbDbWriter (1s window debounce, U6)
        - `_alert_evaluator`: UsdtAlertEvaluator (observation-based, source-neutral, U7)
        - `_fallback_controller`: BithumbRestFallbackController (silent probe, U6+U7)

    누적 capabilities:
        - U4: Reconnect loop + backoff sequence + ping/pong heartbeat + stale 전이
        - U5: 매 valid tick → Redis latest write (fire-and-forget) + topic trigger
        - U6: 매 valid tick → DB writer (1s window debounce + race-prevention)
        - U6: stale 전이 → REST fallback silent probe → fanout (Redis+DB+Alert)
        - U7: 매 valid tick → AlertObservation schedule (kind="tick")
        - U7: REST probe success → AlertObservation schedule (kind="rest_probe")

    close 순서 (Upbit upbit.py:920-940 mirror):
        fallback → DB → Alert → Redis (4개, 각 try/except 격리)
    """

    def __init__(self) -> None:
        # U2 lifecycle
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        # U3 session state
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False
        # U4 liveness + reconnect state
        self._liveness: UsdtLivenessMonitor = UsdtLivenessMonitor()
        self._status: str = "normal"
        self._status_transition_count: dict[str, int] = {
            "normal": 0, "reconnecting": 0, "stale": 0,
        }
        self._reconnect_attempt_count: int = 0
        # U5 Redis writer (tick-level, fire-and-forget) — A13 lifecycle invariant:
        # __init__에서 1회 생성, reconnect 사이 재사용 (재할당 없음).
        self._redis_writer: BithumbRedisWriter = BithumbRedisWriter()
        # U6 DB writer (1s window debounce). Upbit upbit.py:616 mirror.
        self._db_writer: BithumbDbWriter = BithumbDbWriter()
        # U7 Alert evaluator (observation-based, source-neutral helper 재사용).
        # Upbit upbit.py:618 mirror — Bithumb client 자체 instance 보유.
        self._alert_evaluator: UsdtAlertEvaluator = UsdtAlertEvaluator()
        # U6 REST fallback controller (silent probe).
        # U7: alert_evaluator inject 추가 (Upbit upbit.py:621-625 mirror).
        self._fallback_controller: BithumbRestFallbackController = (
            BithumbRestFallbackController(
                redis_writer=self._redis_writer,
                db_writer=self._db_writer,
                alert_evaluator=self._alert_evaluator,
            )
        )

    @staticmethod
    def _build_subscribe_payload() -> list[dict]:
        """Bithumb subscribe payload — Upbit-compatible 3-element list."""
        return [
            {"ticket": BITHUMB_SUBSCRIBE_TICKET},
            {"type": "ticker", "codes": [BITHUMB_TARGET_CODE]},
            {"format": "DEFAULT"},
        ]

    def _parse_ticker_message(self, raw) -> Optional[dict]:
        """Bithumb ticker frame → normalized tick dict, None on invalid/ignore.

        Normalized shape (Upbit fetch_upbit_usdt_tick contract 일치):
            {"source": "bithumb", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}

        Guard (Upbit 패턴 mirror):
            - 0/negative rate → None
            - timestamp missing → None
            - type != "ticker" or code != KRW-USDT → None
        """
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if isinstance(raw, str):
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw, dict):
            message = raw
        else:
            return None

        if not isinstance(message, dict):
            return None
        if message.get("type") != "ticker":
            return None
        if message.get("code") != BITHUMB_TARGET_CODE:
            return None

        try:
            rate = float(message["trade_price"])
        except (TypeError, ValueError, KeyError):
            return None
        if rate <= 0:
            return None

        ts_raw = message.get("trade_timestamp") or message.get("timestamp")
        if ts_raw is None:
            return None
        try:
            ts_ms = int(ts_raw)
        except (TypeError, ValueError):
            return None

        return {
            "source": "bithumb",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }

    def _handle_message(self, raw) -> Optional[dict]:
        """parse + log only. Returns normalized tick dict or None.

        U4 시점: downstream IO 없음 (Redis/DB/alert는 U5/U6).
        valid tick → liveness.observe_tick은 caller (_run_one_session)에서 호출.
        """
        tick = self._parse_ticker_message(raw)
        if tick is None:
            return None
        if not self._first_tick_logged:
            logger.info("[usdt_ws.bithumb] first tick", extra=tick)
            self._first_tick_logged = True
        else:
            logger.debug("[usdt_ws.bithumb] tick", extra=tick)
        return tick

    def _set_status(self, new_status: str) -> None:
        """status 전이 + counter + log + (U6) fallback hook.

        U4 시점: counter + log only.
        U5 시점: Redis writer 추가, but _set_status에서 직접 schedule 호출 X.
        U6 확장: normal → stale 전이 시 `fallback_controller.schedule_probe` 호출 (A7).
            normal 복귀 시 `fallback_controller.reset_cooldown` 호출 (A8).
            Redis/DB direct write는 여전히 X — fallback controller가 그 경로를
            schedule_probe → REST → writer.schedule로 우회하여 들어감.
        U7 결정: alert evaluator hook은 `_set_status`가 아니라 `_run_one_session` 및
            `_run_probe`에서 호출 (tick/probe path 양쪽). status 전이 자체는 alert
            발화 trigger가 아니므로 _set_status는 fallback hook만 유지.

        Upbit upbit.py:728-735 mirror.
        """
        if self._status == new_status:
            return
        prev = self._status
        self._status = new_status
        if new_status in self._status_transition_count:
            self._status_transition_count[new_status] += 1
        logger.info(
            "[usdt_ws.bithumb] status %s → %s (gap_buckets=%s max_gap=%.2fs)",
            prev, new_status,
            self._liveness.gap_buckets, self._liveness.max_frame_gap_sec,
        )
        # U6 fallback hook (의도적 확장, A7/A8)
        if prev == "normal" and new_status == "stale":
            self._fallback_controller.schedule_probe(reason="stale_transition")
        elif new_status == "normal":
            # 모든 normal 복귀에서 cooldown reset (Upbit Codex Finding 1 mirror):
            # stale → reconnecting → normal 경로에서도 reset 보장 →
            # 새 outage cycle의 stale probe가 옛 cooldown으로 skip되지 않음.
            self._fallback_controller.reset_cooldown()

    @staticmethod
    def _compute_backoff(attempt: int) -> float:
        """Reconnect backoff — Upbit/KRX 동일 sequence."""
        if attempt <= 0:
            return RECONNECT_BACKOFF_SEQ[0]
        if attempt <= len(RECONNECT_BACKOFF_SEQ):
            return RECONNECT_BACKOFF_SEQ[attempt - 1]
        return RECONNECT_BACKOFF_TAIL

    async def _ping_loop(self, ws) -> None:
        """주기적 ping → pong 성공 시 heartbeat observation (§5 준수).

        실패/timeout 시 ws.close() → recv loop가 ConnectionClosed 감지 →
        start() reconnect 트리거. ping_loop 자체는 silently return.

        Acceptance 5: heartbeat/pong → liveness만 (status 직접 변경 X).
        """
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(PING_INTERVAL_SEC)
            except asyncio.CancelledError:
                return
            if self._stop_event.is_set():
                return
            try:
                pong_waiter = await ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=PING_TIMEOUT_SEC)
                self._liveness.observe_heartbeat(time.time())
            except asyncio.CancelledError:
                return
            except (asyncio.TimeoutError, ConnectionClosed) as exc:
                logger.warning(
                    "[usdt_ws.bithumb] ping failed: %s — closing ws for reconnect",
                    type(exc).__name__,
                )
                try:
                    await ws.close()
                except Exception:
                    pass
                return

    async def _run_one_session(self) -> None:
        """단일 connect → subscribe → recv loop + ping_loop background.

        §5 준수: ping_interval=None (websockets auto-ping 비활성) + explicit
        _ping_loop으로 heartbeat 직접 관찰. last_activity_at 기준 stale 판정.

        Acceptance 3: ping task session 종료 시 cancel/await (finally 블록).
        Acceptance 4: stale 전이 — status/log/counter only.
        """
        async with websockets.connect(
            BITHUMB_WS_URL,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=10,
        ) as ws:
            self._ws = ws
            self._liveness.reset_active_session()
            logger.info("[usdt_ws.bithumb] connected url=%s", BITHUMB_WS_URL)
            payload = self._build_subscribe_payload()
            await ws.send(json.dumps(payload))
            logger.info(
                "[usdt_ws.bithumb] subscribed code=%s ticket=%s",
                BITHUMB_TARGET_CODE, BITHUMB_SUBSCRIBE_TICKET,
            )
            self._set_status("normal")

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                while not self._stop_event.is_set():
                    now = time.time()
                    # stale check — last_activity_at (max tick/heartbeat) 기준 (§5)
                    if self._status != "stale" and self._liveness.is_stale(now, STALE_AFTER_SEC):
                        self._set_status("stale")
                    elif self._status == "stale" and not self._liveness.is_stale(now, STALE_AFTER_SEC):
                        self._set_status("normal")

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                    except asyncio.TimeoutError:
                        continue
                    except ConnectionClosed:
                        if self._stop_event.is_set():
                            logger.info("[usdt_ws.bithumb] connection closed after stop")
                            return
                        raise
                    # valid ticker만 liveness activity + Redis/DB/Alert schedule.
                    # invalid/non-ticker frame은 모두 X (U7 acceptance 4).
                    tick = self._handle_message(raw)
                    if tick is not None:
                        self._liveness.observe_tick(time.time())
                        # U5: Redis writer schedule (fire-and-forget, recv loop 격리)
                        self._redis_writer.schedule(tick)
                        # U6: DB writer schedule (1s window debounce, race-prevention)
                        self._db_writer.schedule(tick)
                        # U7: AlertObservation schedule (source-neutral evaluator 재사용)
                        observation = AlertObservation(
                            source=tick["source"],
                            asset=tick["asset"],
                            rate=tick["rate"],
                            timestamp_ms=tick["timestamp_ms"],
                            kind="tick",  # WS tick (Upbit upbit.py:906 mirror)
                        )
                        self._alert_evaluator.schedule(observation)
            finally:
                # U4 Acceptance 3: ping task cancel/await 보장.
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("[usdt_ws.bithumb] ping_task cleanup 실패")
                # U7 close 순서: fallback → DB → Alert → Redis (4개, Upbit upbit.py:920-940
                # mirror). fallback이 모든 writer를 schedule할 수 있으므로 먼저 멈춰야
                # downstream writer 들이 깔끔히 drain. Redis는 DB/Alert 확정 후 마지막.
                # U6 fallback controller: pending probe task cancel.
                try:
                    await self._fallback_controller.close()
                except Exception:
                    logger.exception("[usdt_ws.bithumb] fallback_controller.close() 실패")
                # U6 DB writer (source of truth) timer cancel + pending tick 즉시 flush.
                try:
                    await self._db_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.bithumb] db_writer.close() 실패")
                # U7 Alert evaluator drain (5s timeout — FCM 보호, Upbit upbit.py:931-935 mirror).
                # 예외 격리: WS loop에 전파 X (A7 격리).
                try:
                    await self._alert_evaluator.close()
                except Exception:
                    logger.exception("[usdt_ws.bithumb] alert_evaluator.close() 실패")
                # U5 A13 lifecycle: session-level Redis writer close — pending tasks
                # drain (1s) + timeout cancel → _tasks.clear(). Upbit upbit.py:938
                # mirror. close 후에도 writer instance는 동일 (reconnect 재사용).
                try:
                    await self._redis_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.bithumb] redis_writer.close() 실패")

    async def start(self) -> None:
        """reconnect 루프. 단일 session crash 시 backoff 후 재시도.

        Acceptance 2: bounded (test에서 backoff sleep mock으로 즉시 진행).
        Acceptance 6: ConnectionClosed → _reconnect_attempt_count ++.
        Acceptance 7: stop_event 도달 시 빠른 종료 (wait_for + timeout 즉시 반응).

        Upbit PR3 start() 패턴 mirror.
        """
        if self._running:
            logger.debug("[usdt_ws.bithumb] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.bithumb] start (U4 reconnect loop)")
        # PR 2c — start-level summary log task (Korbit/Upbit 패턴 mirror).
        # _run_one_session 영향 0 — start lifetime 동안만 60s cycle emit.
        summary_task = asyncio.create_task(self._summary_log_loop())
        attempt = 0
        try:
            while not self._stop_event.is_set():
                try:
                    await self._run_one_session()
                    if self._stop_event.is_set():
                        break
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except ConnectionClosed as exc:
                    if self._stop_event.is_set():
                        break
                    self._set_status("reconnecting")
                    attempt += 1
                    self._reconnect_attempt_count += 1
                    backoff = self._compute_backoff(attempt)
                    logger.warning(
                        "[usdt_ws.bithumb] connection closed (attempt %d): %s — backoff %.1fs",
                        attempt, exc, backoff,
                    )
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    self._set_status("reconnecting")
                    attempt += 1
                    self._reconnect_attempt_count += 1
                    backoff = self._compute_backoff(attempt)
                    logger.warning(
                        "[usdt_ws.bithumb] session error (attempt %d): %s: %s — backoff %.1fs",
                        attempt, type(exc).__name__, exc, backoff,
                    )
                else:
                    continue  # 정상 종료 후 즉시 다음 iteration

                # backoff sleep — stop_event 즉시 반응 (acceptance 7).
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=backoff,
                    )
                    break  # stop_event 도착
                except asyncio.TimeoutError:
                    pass  # backoff 완료, 다음 iteration
        finally:
            # PR 2c — summary_task cancel/await (reconnect loop 예외와 독립 try/except).
            # Korbit start() finally cancel 패턴 mirror.
            summary_task.cancel()
            try:
                await summary_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[usdt_ws.bithumb] summary_task cleanup 실패")
            self._running = False
            self._ws = None
            logger.info(
                "[usdt_ws.bithumb] start exited (reconnect_attempts=%d)",
                self._reconnect_attempt_count,
            )

    async def _summary_log_loop(self) -> None:
        """PR 2c — start lifetime 동안 60s cycle metric INFO emit (Bithumb 9 fields).

        Korbit `_summary_log_loop` / Upbit `_summary_log_loop` 패턴 mirror.
        Bithumb은 Upbit 1-dim status + Korbit REST fallback union이라 9 fields
        (state 7 — Upbit 1-dim 동일 — + counter 2).

        Emit metric (9개, state 7 + counter 2):
            - frames_per_min: `_liveness.frame_count_total` 차이 / elapsed
            - last_tick_age: `now - _liveness.last_tick_at` (없으면 -1.0 sentinel)
            - last_heartbeat_age: `now - _liveness.last_heartbeat_at` (없으면 -1.0 sentinel)
            - max_frame_gap: `_liveness.max_frame_gap_sec`
            - status: `_status` (1-dim: normal/reconnecting/stale)
            - reconnect_attempts: `_reconnect_attempt_count` (instance lifetime)
            - status_transitions: `_status_transition_count` 3 keys (normal:N/reconnecting:N/stale:N)
            - redis_saturation_count: `_redis_writer.saturation_count` (PR 2c)
            - fallback_probe_scheduled_count: `_fallback_controller.scheduled_probe_count` (PR 2c)

        Bithumb-specific schema:
            - Upbit과 달리 fallback_probe_scheduled_count 포함
            - Korbit과 달리 connection_status / ticker_freshness_status 부재 (1-dim)
            - 향후 PR scope (별도): probe lifecycle breakdown counter

        Sentinel 정책 (Korbit/Upbit 동일):
            last_tick_at / last_heartbeat_at이 None일 경우 (첫 tick/PONG 전) -1.0
            numeric sentinel. log 파싱 / numeric aggregation 일관성.
        """
        prev_frame_total = self._liveness.frame_count_total
        prev_at = time.time()
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(SUMMARY_LOG_INTERVAL_SEC)
            except asyncio.CancelledError:
                return
            now = time.time()
            elapsed = max(now - prev_at, 1e-9)
            frames_in_window = self._liveness.frame_count_total - prev_frame_total
            frames_per_min = int(round(frames_in_window * 60 / elapsed))
            prev_frame_total = self._liveness.frame_count_total
            prev_at = now

            last_tick = self._liveness.last_tick_at
            last_tick_age = (now - last_tick) if last_tick is not None else -1.0
            last_heartbeat = self._liveness.last_heartbeat_at
            last_heartbeat_age = (now - last_heartbeat) if last_heartbeat is not None else -1.0

            transitions_str = "/".join(
                f"{k}:{v}" for k, v in self._status_transition_count.items()
            )

            logger.info(
                "[usdt_ws.bithumb] metrics frames_per_min=%d "
                "last_tick_age=%.1f last_heartbeat_age=%.1f "
                "max_frame_gap=%.1f "
                "status=%s "
                "reconnect_attempts=%d "
                "status_transitions=%s "
                "redis_saturation_count=%d "
                "fallback_probe_scheduled_count=%d",
                frames_per_min,
                last_tick_age, last_heartbeat_age,
                self._liveness.max_frame_gap_sec,
                self._status,
                self._reconnect_attempt_count,
                transitions_str,
                self._redis_writer.saturation_count,
                self._fallback_controller.scheduled_probe_count,
            )

    async def stop(self) -> None:
        """stop_event set → reconnect loop + ping loop 종료.

        Acceptance 7: 빠른 종료 (idempotent).

        Note (Codex L2): Upbit `stop()`은 `await ws.close()`도 호출하지만 Bithumb은
        recv loop가 `RECV_TIMEOUT_SEC=1.0s`마다 깨어나 stop_event 확인 → 최대 1s
        bound. backoff sleep도 `wait_for(stop_event.wait(), timeout=backoff)`라
        stop_event set 시 즉시 break. 따라서 ws.close 미호출이 의도적 — 후속
        maintainer가 Upbit parity 위해 반사적으로 ws.close 추가 금지.
        """
        self._stop_event.set()
