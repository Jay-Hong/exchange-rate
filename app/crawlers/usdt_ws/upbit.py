"""USDT WebSocket — Upbit canary client (PR5 + DB writer).

USDT_WS_DESIGN_PLAN §12.2 PR1~PR5 누적.
PR1: lifecycle skeleton.
PR2: connect/subscribe/parse + log only.
PR3: UsdtLivenessMonitor + reconnect + explicit ping/pong heartbeat (§5).
PR4: UpbitRedisWriter — tick-level Redis latest write (fire-and-forget).
PR5: UpbitDbWriter — 1초 window debounce + insert_source_rate_if_changed.

NO alert (PR6), NO REST fallback (PR7).

PR5 핵심 설계 (Codex 검토):
    - **1초 window debounce**: tick rate ~100ms+ vs DB I/O ~10-50ms — 매 tick
      write는 DB 부담 ↑. KRX `KrxDbWriter._flush_after_window` 패턴 mirror.
      Redis writer (PR4)는 "latest 최신성" 목적이라 tick-level 유지,
      DB writer는 "저장량 제어" 목적 분리.
    - **`insert_source_rate_if_changed` signature 그대로 사용**: timestamp는
      DB 자동 생성 (helper에 timestamp param 없음). exchange timestamp 저장
      semantics는 schema 변경 영역 → 별 PR.
    - **`asyncio.to_thread` 격리**: sync SQLAlchemy 호출이 event loop 막지
      않음. `get_db_context()`로 session 생성/close가 thread 내부에서.
    - **race handling**: flush 진행 중 새 tick 도착 시 `_pending_tick` 갱신.
      finally 블록에서 `_pending_tick is not None`이면 새 timer 예약 →
      마지막 tick 누락 방지 (KRX PR6b-2b Codex 보정 패턴).
    - **close() 즉시 flush**: shutdown 시 1초 기다리지 않고 마지막 pending
      tick 즉시 DB write. window 일관성보다 last tick 보장 우선.
    - **Decimal 정규화 없음**: 기존 REST polling
      (`usdt_sources.py::collect_usdt_rates`)이 정규화 안 하므로 일관성 유지.
    - **failure isolation**: DB exception → log only, WS session 유지.
    - **dual-writer (REST polling + WS) baseline**: PR5에서 race 해결 X.
      `insert_source_rate_if_changed` last-row 비교가 대부분 dedup하나
      concurrent INSERT race 가능. Stage 1 운영에서 row 증가율 측정 → 필요
      시 별 PR.

PR4 핵심 설계 (Codex 검토):
    - **tick-level Redis write**: KRX는 Stage C(tick-level)를 보류한 영역이지만
      USDT는 새 build이라 behavior change 우려 없음. `latest`의 최신성 보존을
      위해 매 valid tick마다 write (debounce 없음).
    - **fire-and-forget background task**: recv loop가 Redis I/O에 묶이지 않음.
      `schedule()`은 즉시 반환, 백그라운드에서 `to_thread`로 sync helper 호출.
    - **write order 직렬화 (asyncio.Lock)**: schedule 순서 = Redis SET 순서
      보장. 병렬 to_thread + redis-py connection pool 조합에서 발생하는
      "old tick이 new tick을 덮음" race 차단. lock은 to_thread 완료까지 보유 →
      latency가 누적되면 MAX_PENDING_WRITES guard로 새 schedule skip.
    - **last-write-wins (helper 정책)**: `set_latest_usdt_rate_from_sync_job`는
      timestamp compare 없이 SET. 본 writer 측 직렬화로 sender side order는
      보장. REST polling과 dual-writer 시 한쪽 helper invocation order는 X —
      timestamp compare는 별 PR.
    - **MAX_PENDING_WRITES guard**: Redis 장애 시 task 폭증 방지 (50 in-flight 한도).
    - **close() drain-first**: 정상 shutdown 시 pending write 보존 (1s timeout 후 cancel).
    - **Redis 실패 격리**: helper False / exception 모두 log만, WS session 유지.

PR4 non-scope 유지:
    - DB insert (PR5)
    - alert evaluation (PR6)
    - REST fallback (PR7)
    - broadcast/read path / legacy_policy 변경 (기존 Z-2 infra 그대로)

KRX 패턴 mirror:
    - `KrxLivenessMonitor` (krx_kis.py:513) — frame counters + gap state
    - `_run_session` reconnect (krx_kis.py:1114) — backoff sequence
    - `ping_interval=None` + 명시 ping (krx_kis.py:1168) — heartbeat 관찰
    - `_handle_message` evolve: bool → Optional[dict] (PR3 Codex 예측)
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
from app.notifications.alert_evaluator import (
    AlertObservation,
    UsdtAlertEvaluator,
)
from app.tether_topic_trigger import (
    TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
)

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.upbit")

_KST = timezone(timedelta(hours=9))

UPBIT_WS_URL = "wss://api.upbit.com/websocket/v1"
UPBIT_SUBSCRIBE_TICKET = "fxi-usdt-upbit"
UPBIT_TARGET_CODE = "KRW-USDT"

# recv timeout — stop event 폴링 응답성.
RECV_TIMEOUT_SEC = 1.0

# §5 silence threshold — Phase B.0 (2026-05-14)에서 Upbit "WS ping 30s" 확정.
# 활동 기준 (`last_activity_at`) silence가 본 값을 초과하면 stale.
STALE_AFTER_SEC = 30.0

# Explicit heartbeat — websockets 자동 ping/pong은 recv()로 노출되지 않아
# §5 frame/heartbeat liveness 관찰 불가. KRX 패턴 mirror로 직접 ping 관리.
PING_INTERVAL_SEC = 20.0  # 매 20s ping (STALE_AFTER_SEC 30s 이내 회복)
PING_TIMEOUT_SEC = 10.0   # pong 대기 시간

# Reconnect backoff sequence — KRX (krx_kis.py:112) 동일.
RECONNECT_BACKOFF_SEQ = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RECONNECT_BACKOFF_TAIL = 30.0

# Gap bucket boundaries — KRX (krx_kis.py:556) 동일.
_GAP_BUCKET_KEYS = ("<=1s", "<=2s", "<=5s", "<=10s", "<=30s", "<=60s", ">60s")

# Redis writer guards
MAX_PENDING_WRITES = 50   # task 폭증 안전망 (Redis 장애 시)
REDIS_CLOSE_TIMEOUT_SEC = 1.0  # close() drain timeout — 후 강제 cancel

# DB writer
DB_WRITE_WINDOW_SEC = 1.0  # debounce window (KRX KrxDbWriter 동일)

# PR7 REST fallback controller (옵션 B: silent probe)
FALLBACK_COOLDOWN_SEC = 30.0      # stale 지속 중 probe 빈도 제한 (= STALE_AFTER_SEC)
FALLBACK_PROBE_TIMEOUT_SEC = 10.0  # REST HTTP timeout

# Summary log follow-up (PR 2b) — 60s cycle metric INFO emit.
# Korbit summary log 1차 PR (USDT_WS_DESIGN_PLAN §12.7.5 후속) 패턴 mirror,
# Upbit source-specific shape (status 1-차원, status_transitions 3 keys,
# redis_saturation_count 신규 counter).
SUMMARY_LOG_INTERVAL_SEC = 60.0


class UsdtLivenessMonitor:
    """USDT WebSocket frame liveness state — frame/heartbeat 활동 관찰.

    KRX `KrxLivenessMonitor` 패턴 mirror. USDT는 ticker 단일이라 trade/quote
    분리 없음. heartbeat은 explicit ping/pong observation (§5 준수).

    state 분류 (KRX와 동일):
        - lifetime (reset X): frame counters (total/tick/heartbeat)
        - active session reset 대상: last_*_at, gap_buckets, max_*_gap
    """

    def __init__(self) -> None:
        # frame counters (lifetime)
        self.frame_count_total: int = 0
        self.tick_count: int = 0
        self.heartbeat_count: int = 0
        # last activity timestamps (active session — reset 대상)
        self.last_tick_at: Optional[float] = None
        self.last_heartbeat_at: Optional[float] = None
        # gap buckets — tick frame 기반 (data freshness metric).
        # heartbeat gap은 PING_INTERVAL_SEC로 거의 일정해 관찰 가치 낮음.
        self.gap_buckets: dict[str, int] = self._init_gap_buckets()
        self.max_frame_gap_sec: float = 0.0

    @staticmethod
    def _init_gap_buckets() -> dict[str, int]:
        return {key: 0 for key in _GAP_BUCKET_KEYS}

    @staticmethod
    def _bucket_for(gap_sec: float) -> str:
        if gap_sec <= 1:
            return "<=1s"
        if gap_sec <= 2:
            return "<=2s"
        if gap_sec <= 5:
            return "<=5s"
        if gap_sec <= 10:
            return "<=10s"
        if gap_sec <= 30:
            return "<=30s"
        if gap_sec <= 60:
            return "<=60s"
        return ">60s"

    def reset_active_session(self) -> None:
        """active session 시작 시 active-session metric reset.

        lifetime counter는 유지. KRX `reset_active_session()` 패턴 동일.
        """
        self.last_tick_at = None
        self.last_heartbeat_at = None
        self.gap_buckets = self._init_gap_buckets()
        self.max_frame_gap_sec = 0.0

    def observe_tick(self, now: float) -> None:
        """ticker frame 관측 — counter/gap/last_tick_at 갱신."""
        prev = self.last_tick_at
        self.last_tick_at = now
        self.frame_count_total += 1
        self.tick_count += 1
        if prev is not None:
            gap = now - prev
            if gap > self.max_frame_gap_sec:
                self.max_frame_gap_sec = gap
            self.gap_buckets[self._bucket_for(gap)] += 1

    def observe_heartbeat(self, now: float) -> None:
        """pong 수신 — counter/last_heartbeat_at 갱신. gap bucket은 tick 전용."""
        self.last_heartbeat_at = now
        self.frame_count_total += 1
        self.heartbeat_count += 1

    @property
    def last_activity_at(self) -> Optional[float]:
        """stale 판정 기준 — §5 핵심 (heartbeat 포함)."""
        if self.last_tick_at is None and self.last_heartbeat_at is None:
            return None
        return max(self.last_tick_at or 0.0, self.last_heartbeat_at or 0.0)

    def is_stale(self, now: float, threshold: float) -> bool:
        """activity (tick OR heartbeat) silence가 threshold 초과인지.

        §5 핵심: ticker만 조용하고 heartbeat fresh이면 stale X (정상 저유동성).
        둘 다 silent여야 stale.
        """
        last = self.last_activity_at
        if last is None:
            return False
        return now - last > threshold


class UpbitRedisWriter:
    """Upbit USDT/KRW Redis latest writer — tick-level, fire-and-forget.

    PR4 핵심:
        - 매 valid tick → `set_latest_usdt_rate_from_sync_job` 호출 (debounce 없음)
        - schedule()은 background task 생성 후 즉시 반환 (recv loop 격리)
        - asyncio.to_thread로 sync helper 호출 (event loop 비non-blocking)
        - MAX_PENDING_WRITES 한도로 Redis 장애 시 task 폭증 차단
        - close() drain-first, timeout 후 cancel (마지막 tick 보존)

    Failure isolation:
        - helper False 반환 / exception → log only, WS session 영향 X
        - last-write-wins (helper 기존 정책 유지, timestamp compare는 별 PR)
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        # write order 직렬화 (Codex review): 병렬 to_thread + connection pool
        # 조합으로 old tick이 new tick을 덮는 race 차단. lock은 to_thread
        # 완료까지 보유 — schedule 순서 = Redis SET 순서.
        self._write_lock: asyncio.Lock = asyncio.Lock()
        # PR 2b: saturation skip 누적 counter — schedule()의 MAX_PENDING_WRITES skip
        # branch에서만 증가 (helper False / Redis exception / trigger exception은
        # saturation 아님). UpbitWsClient._summary_log_loop이 property로 읽음.
        self._saturation_count: int = 0

    @property
    def saturation_count(self) -> int:
        """PR 2b — Redis write queue saturation skip 누적 횟수 (read-only)."""
        return self._saturation_count

    def schedule(self, tick: dict) -> None:
        """tick → background Redis write task. fire-and-forget.

        Saturation 시 (`len(_tasks) >= MAX_PENDING_WRITES`) skip + warning + counter increment.
        Redis 장애로 task가 누적되는 시나리오 차단.

        PR 2b: saturation_count 증가는 MAX_PENDING_WRITES skip branch에서만.
        helper False / Redis exception / trigger exception은 saturation 아님 (log only,
        counter increment X).
        """
        if len(self._tasks) >= MAX_PENDING_WRITES:
            self._saturation_count += 1  # PR 2b: saturation skip counter
            logger.warning(
                "[usdt_ws.upbit] Redis write queue saturated (%d in-flight) — skip tick",
                len(self._tasks),
            )
            return
        task = asyncio.create_task(self._write_async(tick))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write_async(self, tick: dict) -> None:
        """sync Redis helper를 to_thread로 호출. 실패는 격리.

        `_write_lock`으로 직렬화 — 동시 to_thread + connection pool 조합에서
        발생할 수 있는 SET 순서 역전 차단 (Codex review).

        PR2 (Phase B.2): Redis write 성공 시 lock 밖에서 tether topic trigger
        호출. trigger 예외는 writer/WS session에 전파하지 않음.
        """
        success = False
        source = tick["source"]
        asset = tick["asset"]
        async with self._write_lock:
            ts_iso = datetime.fromtimestamp(tick["timestamp_ms"] / 1000, tz=_KST).isoformat()
            try:
                success = await asyncio.to_thread(
                    latest_rates_cache.set_latest_usdt_rate_from_sync_job,
                    source=source,
                    asset=asset,
                    rate=tick["rate"],
                    timestamp=ts_iso,
                )
                if not success:
                    logger.warning(
                        "[usdt_ws.upbit] Redis write returned False "
                        "(helper 내부 log 참조, WS session 유지)"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[usdt_ws.upbit] Redis write task crashed (격리, WS session 유지)"
                )
                success = False

        # PR2 hook: lock 밖에서 trigger — Redis write 성공 시에만.
        # trigger 호출 자체의 예외는 writer/WS에 전파 X (책임 분리).
        if success:
            try:
                tether_topic_trigger.request_tether_topic_trigger(
                    source=source,
                    asset=asset,
                    reason=TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
                )
            except Exception:
                logger.exception(
                    "[usdt_ws.upbit] tether topic trigger 호출 실패 (격리, WS session 유지)"
                )

    async def close(self, timeout: float = REDIS_CLOSE_TIMEOUT_SEC) -> None:
        """pending writes drain — 정상 shutdown 시 마지막 tick latest 보존.

        timeout (default 1s) 안에 drain 안 되면 강제 cancel (Codex review).
        Redis hung인 경우 무한 대기 방지.
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
                "[usdt_ws.upbit] Redis writer close timeout (%ds) — cancel %d pending tasks",
                timeout, len(self._tasks),
            )
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            self._tasks.clear()


class UpbitDbWriter:
    """Upbit USDT/KRW DB writer — 1초 window debounce + insert_source_rate_if_changed.

    KRX `KrxDbWriter` 패턴 mirror. tick rate ~100ms+ vs DB I/O ~10-50ms이라
    매 tick INSERT는 DB 부담. window 내 마지막 tick만 helper 호출.

    asyncio loop 차단 방지:
        sync SQLAlchemy 호출은 asyncio.to_thread로 격리. DB session은
        to_thread 내부에서 get_db_context()로 생성/close.

    race 방지 (KRX PR6b-2b Codex 보정 mirror):
        DB write (to_thread) 진행 중 새 tick 도착 → _pending_tick 갱신,
        _timer.done() X 라 schedule()이 새 timer 안 만듦. write 종료 후
        finally 블록에서 _pending_tick 재확인 후 새 timer 예약 — 누락 방지.

    failure isolation: DB exception → log only, WS session 영향 X.

    shutdown (close): pending tick 1초 기다리지 않고 즉시 flush. window
        일관성보다 last tick 보장 우선.
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

        write 진행 중 새 tick 도착 시 finally에서 새 timer 예약.
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
                "[usdt_ws.upbit] DB write failed (격리): %s: %s",
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
                logger.exception("[usdt_ws.upbit] db_writer timer cancel 실패")
        self._timer = None
        tick = self._pending_tick
        self._pending_tick = None
        if tick is None:
            return
        try:
            await asyncio.to_thread(self._sync_db_write, tick)
        except Exception as exc:
            logger.warning(
                "[usdt_ws.upbit] DB write on close failed (격리): %s: %s",
                type(exc).__name__, exc,
            )


class UpbitRestFallbackController:
    """Upbit REST fallback (silent probe 옵션 B, PR7).

    WS frame/heartbeat silence (last_activity_at 기준) 감지 시 REST 1회 probe →
    normalized tick → 기존 fanout (Redis/DB/Alert writer) 재사용. WS reconnect
    loop는 그대로 유지, fallback은 freshness 보조.

    Guardrails:
        - schedule_probe()는 sync/non-blocking (`_set_status` 동기 흐름에서 호출).
        - In-flight skip: probe 진행 중 중복 trigger 차단.
        - Cooldown skip: 마지막 probe 종료 후 FALLBACK_COOLDOWN_SEC 미경과 시 skip
          (stale 지속 중 폭주 방지).
        - Probe 실패도 cooldown 적용 (REST rate limit 보호).
        - reset_cooldown(): normal 복귀 시 호출 → 다음 stale 즉시 1회 probe 보장.
        - REST 실패 격리: log only, WS session/reconnect 영향 X.
        - fallback tick의 source/asset = "upbit"/"usdt-krw" (downstream 일관).
        - AlertObservation.kind="rest_probe" (PR6 kind 필드 활용, log filter).

    PR3 _set_status 확장 (PR7부터):
        PR3에서 _set_status는 "counter + log only"였으나 PR7부터 normal→stale
        전이 시 fallback schedule_probe 호출이 명시적으로 추가됨.
        (test_set_status_has_no_side_effects의 forbidden_attrs는 PR7
        `_fallback_controller`를 포함하지 않아 기존 검증 그대로 유효.)
    """

    def __init__(
        self,
        redis_writer: "UpbitRedisWriter",
        db_writer: "UpbitDbWriter",
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

    def schedule_probe(self, reason: str) -> None:
        """sync: in-flight/cooldown 체크 후 background probe task 생성. 즉시 반환.

        `_set_status("stale")` 안에서 호출 — 동기 흐름이라 즉시 반환 필수.
        Production은 항상 asyncio context 안 (recv loop / reconnect loop), 단
        sync test에서 `_set_status` 직접 호출 시 no-loop. defensive하게 skip.
        """
        now = time.time()
        if self._in_flight:
            logger.debug(
                "[usdt_ws.upbit.fallback] probe in-flight, skip (reason=%s)", reason,
            )
            return
        if now < self._cooldown_until:
            remaining = self._cooldown_until - now
            logger.debug(
                "[usdt_ws.upbit.fallback] cooldown active, skip (%.1fs remaining, reason=%s)",
                remaining, reason,
            )
            return
        # Loop 체크를 coroutine 생성 전에 — no-loop 시 coroutine 미생성으로
        # "coroutine was never awaited" warning 회피.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[usdt_ws.upbit.fallback] no event loop, skip probe (reason=%s)", reason,
            )
            return
        self._pending_task = loop.create_task(self._run_probe(reason))
        self._in_flight = True

    def reset_cooldown(self) -> None:
        """normal 복귀 시 호출 — cooldown clear. 다음 stale 즉시 1회 probe 보장."""
        self._cooldown_until = 0.0
        logger.debug("[usdt_ws.upbit.fallback] cooldown reset on normal recovery")

    async def _run_probe(self, reason: str) -> None:
        """REST probe → normalized tick → fanout (Redis/DB/Alert).

        실패 시 log only + cooldown 적용 (재시도 X). WS session 영향 X.
        """
        try:
            logger.info(
                "[usdt_ws.upbit.fallback] probe start (reason=%s)", reason,
            )
            tick = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_upbit_tick),
                timeout=self._probe_timeout_sec,
            )
            if tick is None:
                logger.warning(
                    "[usdt_ws.upbit.fallback] probe returned None — REST/parse 실패",
                )
                return

            # 기존 fanout 재사용 (PR4/PR5/PR6 writer/evaluator)
            self._redis_writer.schedule(tick)
            self._db_writer.schedule(tick)
            observation = AlertObservation(
                source=tick["source"],
                asset=tick["asset"],
                rate=tick["rate"],
                timestamp_ms=tick["timestamp_ms"],
                kind="rest_probe",  # PR6 kind 필드 활용 (log/metric 구분)
            )
            self._alert_evaluator.schedule(observation)
            logger.info(
                "[usdt_ws.upbit.fallback] probe success (rate=%s, ts_ms=%d, reason=%s)",
                tick["rate"], tick["timestamp_ms"], reason,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "[usdt_ws.upbit.fallback] probe timeout (%.1fs, reason=%s)",
                self._probe_timeout_sec, reason,
            )
        except Exception:
            logger.exception(
                "[usdt_ws.upbit.fallback] probe error (격리, reason=%s)", reason,
            )
        finally:
            # 실패해도 cooldown 적용 (REST rate limit 보호).
            self._cooldown_until = time.time() + self._cooldown_sec
            self._in_flight = False

    @staticmethod
    def _fetch_upbit_tick() -> Optional[dict]:
        """sync REST fetch — to_thread 내부 실행. 모듈 내부 import (순환 회피)."""
        from app.crawlers.usdt_sources import fetch_upbit_usdt_tick
        return fetch_upbit_usdt_tick()

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
                logger.exception("[usdt_ws.upbit.fallback] close error")
        self._pending_task = None
        self._in_flight = False


class UpbitWsClient:
    """Upbit USDT/KRW WebSocket client (PR3 reconnect + liveness).

    State machine: normal | reconnecting | stale.
        - normal: 연결 + activity fresh
        - reconnecting: disconnect 후 backoff 재시도 중
        - stale: 연결 살아있으나 frame+heartbeat 둘 다 silence (§5)

    PR3 `_set_status`는 counter + log만. REST fallback trigger / Redis / DB /
    alert side effect는 PR4-PR7에서 추가. 본 단계는 observation 책임만.
    """

    def __init__(self) -> None:
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        # websockets 16에서 WebSocketClientProtocol deprecated. Any 유지.
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False
        # PR3 liveness + reconnect state
        self._liveness: UsdtLivenessMonitor = UsdtLivenessMonitor()
        self._status: str = "normal"
        self._status_transition_count: dict[str, int] = {
            "normal": 0, "reconnecting": 0, "stale": 0,
        }
        self._reconnect_attempt_count: int = 0
        # PR4 Redis latest writer (tick-level, fire-and-forget)
        self._redis_writer: UpbitRedisWriter = UpbitRedisWriter()
        # PR5 DB writer (1초 window debounce + insert_source_rate_if_changed)
        self._db_writer: UpbitDbWriter = UpbitDbWriter()
        # PR6 Alert evaluator (observation-based, source-neutral helper 재사용)
        self._alert_evaluator: UsdtAlertEvaluator = UsdtAlertEvaluator()
        # PR7 REST fallback controller (silent probe, 옵션 B). WS silence 시
        # REST 1회 probe로 freshness 보전. 기존 fanout (Redis/DB/Alert) 재사용.
        self._fallback_controller: UpbitRestFallbackController = UpbitRestFallbackController(
            redis_writer=self._redis_writer,
            db_writer=self._db_writer,
            alert_evaluator=self._alert_evaluator,
        )

    async def start(self) -> None:
        """reconnect 루프. 단일 session crash 시 backoff 후 재시도.

        예외는 ConnectionClosed/일반 Exception 모두 잡고 reconnect. stop_event
        설정 시 즉시 종료. CancelledError는 propagate.
        """
        if self._running:
            logger.debug("[usdt_ws.upbit] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.upbit] start (PR3 reconnect loop)")
        # PR 2b: Summary log task — start lifetime (reconnect 사이 유지, Korbit 1차 PR 패턴 mirror).
        # 60s cycle metric INFO emit. Upbit source-specific 8 fields (status 1-차원,
        # status_transitions 3 keys, redis_saturation_count 신규).
        summary_task = asyncio.create_task(self._summary_log_loop())
        attempt = 0
        try:
            while not self._stop_event.is_set():
                try:
                    # status는 _run_one_session() 내부 subscribe 후 "normal"로
                    # 갱신. 진입 시점 redundant 호출 제거.
                    await self._run_one_session()
                    # session 정상 종료 (stop_event) — 재시도 X
                    if self._stop_event.is_set():
                        break
                    # 비정상 정상 종료 (없을 경우 방어) — reset & 재시도
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except ConnectionClosed as exc:
                    if self._stop_event.is_set():
                        break
                    # Codex review: backoff 진입 전 status 전이 — gauge stable.
                    self._set_status("reconnecting")
                    attempt += 1
                    self._reconnect_attempt_count += 1
                    backoff = self._compute_backoff(attempt)
                    logger.warning(
                        "[usdt_ws.upbit] connection closed (attempt %d): %s — backoff %.1fs",
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
                        "[usdt_ws.upbit] session error (attempt %d): %s: %s — backoff %.1fs",
                        attempt, type(exc).__name__, exc, backoff,
                    )
                else:
                    continue  # 정상 종료 후 즉시 다음 iteration

                # backoff sleep (stop_event 즉시 반응)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=backoff,
                    )
                    # stop_event 도착 → 루프 종료
                    break
                except asyncio.TimeoutError:
                    pass  # backoff 완료, 다음 시도
        finally:
            # PR 2b Codex Point 1: summary cleanup이 reconnect loop 예외와 독립 (개별 try/except).
            # CancelledError silently pass, 다른 예외만 logger.exception.
            summary_task.cancel()
            try:
                await summary_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[usdt_ws.upbit] summary_task cleanup 실패")
            self._running = False
            self._ws = None
            logger.info(
                "[usdt_ws.upbit] start exited (reconnect_attempts=%d)",
                self._reconnect_attempt_count,
            )

    async def _summary_log_loop(self) -> None:
        """PR 2b — start lifetime 동안 60s cycle metric INFO emit.

        Korbit `_summary_log_loop` (USDT_WS_DESIGN_PLAN §12.7.5 1차 PR) 패턴 mirror,
        Upbit source-specific shape (Codex Point 2).

        Korbit summary log와 8 field로 숫자는 같지만 cross-source 동일 schema가 아님.
        운영 파싱 시 source별 shape 분리 필수:

        Upbit-specific (이 source):
            - status (1-차원, normal/reconnecting/stale)
            - status_transitions (3 keys: normal/reconnecting/stale)
            - redis_saturation_count (PR 2b 신규 counter, _redis_writer.saturation_count property)

        Korbit-specific (참고):
            - connection_status + ticker_freshness_status (2-차원)
            - status_transitions (6 keys: connection_* 3 + ticker_* 3)

        공통 (5 fields): frames_per_min / last_tick_age / last_heartbeat_age /
                         max_frame_gap / reconnect_attempts

        제외 (Codex 강조):
            - ticker_freshness_status: Upbit에 없음. 억지 추가 X.
            - fallback_probe_count: PR 2a Korbit 우선, 본 PR 제외.

        Caveat — frames_per_min 측정 기준 (KRX / Korbit 동일 caveat 적용):
            reconnect 직후 첫 summary log의 frames_per_min은 직전 60초 전체 기준이라
            현재 session active duration 기준이 아닐 수 있다. baseline 분석 시
            reconnect 직후 첫 summary log는 caveat 또는 무시 권장.

        Sentinel 정책 (Korbit 1차 PR과 동일):
            last_tick_at / last_heartbeat_at이 None일 경우 -1.0 numeric sentinel.
            log 파싱 / numeric aggregation 일관성 위해 None 회피.
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

            # Sentinel -1.0 for None (log aggregation numeric 일관성)
            last_tick = self._liveness.last_tick_at
            last_tick_age = (now - last_tick) if last_tick is not None else -1.0
            last_heartbeat = self._liveness.last_heartbeat_at
            last_heartbeat_age = (now - last_heartbeat) if last_heartbeat is not None else -1.0

            # Upbit 3-keys status_transitions (Korbit 6-keys와 다름 — source-specific)
            transitions_str = "/".join(
                f"{k}:{v}" for k, v in self._status_transition_count.items()
            )

            logger.info(
                "[usdt_ws.upbit] metrics frames_per_min=%d "
                "last_tick_age=%.1f last_heartbeat_age=%.1f "
                "max_frame_gap=%.1f "
                "status=%s "
                "reconnect_attempts=%d "
                "status_transitions=%s "
                "redis_saturation_count=%d",
                frames_per_min,
                last_tick_age, last_heartbeat_age,
                self._liveness.max_frame_gap_sec,
                self._status,
                self._reconnect_attempt_count,
                transitions_str,
                self._redis_writer.saturation_count,  # PR 2b: property 사용 (Codex Point 1)
            )

    async def stop(self) -> None:
        """stop event set + ws close → recv wait_for / async loop 즉시 해제."""
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.exception("[usdt_ws.upbit] ws.close() 실패")

    def _set_status(self, new_status: str) -> None:
        """status 전이 + counter + log + (PR7부터) fallback hook.

        PR3 시점: counter + log only (REST probe / Redis / DB / alert side
        effect 절대 호출 X).
        PR7 확장: normal → stale 전이 시 fallback_controller.schedule_probe,
        stale → normal 전이 시 fallback_controller.reset_cooldown 호출.
        (변경 의도 명시: §5 옵션 B silent probe trigger 위치 = stale transition.)

        Redis/DB/Alert direct side effect는 여전히 X — fallback controller가
        그 경로를 schedule_probe → REST → writer.schedule로 우회하여 들어감.
        """
        if self._status == new_status:
            return
        prev = self._status
        self._status = new_status
        if new_status in self._status_transition_count:
            self._status_transition_count[new_status] += 1
        logger.info(
            "[usdt_ws.upbit] status %s → %s (gap_buckets=%s max_gap=%.2fs)",
            prev, new_status,
            self._liveness.gap_buckets, self._liveness.max_frame_gap_sec,
        )
        # PR7 fallback hook (의도적 확장)
        if prev == "normal" and new_status == "stale":
            self._fallback_controller.schedule_probe(reason="stale_transition")
        elif new_status == "normal":
            # 모든 normal 복귀에서 cooldown reset (Codex Finding 1):
            # stale → reconnecting → normal 경로에서도 reset 보장 →
            # 새 outage cycle의 stale probe가 옛 cooldown으로 skip되지 않음.
            self._fallback_controller.reset_cooldown()

    @staticmethod
    def _compute_backoff(attempt: int) -> float:
        if attempt <= 0:
            return RECONNECT_BACKOFF_SEQ[0]
        if attempt <= len(RECONNECT_BACKOFF_SEQ):
            return RECONNECT_BACKOFF_SEQ[attempt - 1]
        return RECONNECT_BACKOFF_TAIL

    @staticmethod
    def _build_subscribe_payload() -> list[dict]:
        return [
            {"ticket": UPBIT_SUBSCRIBE_TICKET},
            {"type": "ticker", "codes": [UPBIT_TARGET_CODE]},
            {"format": "DEFAULT"},
        ]

    def _parse_ticker_message(self, raw) -> Optional[dict]:
        """Upbit ticker frame → normalized tick dict, None on invalid/ignore.

        Accepts bytes (utf-8 JSON), str (JSON), or dict (pre-parsed for tests).
        Normalized shape (PR4-PR6 재사용 계약):
            {"source": "upbit", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}
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
        if message.get("code") != UPBIT_TARGET_CODE:
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
            "source": "upbit",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }

    def _handle_message(self, raw) -> Optional[dict]:
        """parse + log only. Returns normalized tick dict or None if invalid/ignored.

        PR4 signature evolve (PR3 Codex 예측): bool → Optional[dict]. parsed tick을
        caller에 반환 → recv loop가 `_redis_writer.schedule(tick)` 호출 가능.
        non-ticker (status / KRW-BTC / JSON parse 실패 등)는 None — Liveness
        activity / Redis write 둘 다 분리.
        """
        tick = self._parse_ticker_message(raw)
        if tick is None:
            return None
        if not self._first_tick_logged:
            logger.info("[usdt_ws.upbit] first tick", extra=tick)
            self._first_tick_logged = True
        else:
            logger.debug("[usdt_ws.upbit] tick", extra=tick)
        return tick

    async def _ping_loop(self, ws) -> None:
        """주기적 ping → pong 성공 시 heartbeat observation (§5 준수).

        실패/timeout 시 ws.close() 호출 → recv loop가 ConnectionClosed 감지 →
        start() reconnect 트리거. ping_loop 자체는 silently return.
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
                    "[usdt_ws.upbit] ping failed: %s — closing ws for reconnect",
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
        ping_loop으로 heartbeat 직접 관찰. last_activity_at 기준 stale 판정.
        """
        async with websockets.connect(
            UPBIT_WS_URL,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=10,
        ) as ws:
            self._ws = ws
            self._liveness.reset_active_session()
            logger.info("[usdt_ws.upbit] connected url=%s", UPBIT_WS_URL)
            payload = self._build_subscribe_payload()
            await ws.send(json.dumps(payload))
            logger.info(
                "[usdt_ws.upbit] subscribed code=%s ticket=%s",
                UPBIT_TARGET_CODE, UPBIT_SUBSCRIBE_TICKET,
            )
            self._set_status("normal")

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                while not self._stop_event.is_set():
                    now = time.time()
                    # stale 체크 — last_activity_at (max tick/heartbeat) 기준 (§5)
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
                            logger.info("[usdt_ws.upbit] connection closed after stop")
                            return
                        raise
                    # valid ticker만 liveness activity + Redis/DB/Alert schedule.
                    # invalid/non-ticker frame은 모두 X (Codex review).
                    tick = self._handle_message(raw)
                    if tick is not None:
                        self._liveness.observe_tick(time.time())
                        self._redis_writer.schedule(tick)
                        self._db_writer.schedule(tick)
                        observation = AlertObservation(
                            source=tick["source"],
                            asset=tick["asset"],
                            rate=tick["rate"],
                            timestamp_ms=tick["timestamp_ms"],
                            kind="tick",
                        )
                        self._alert_evaluator.schedule(observation)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("[usdt_ws.upbit] ping_task cleanup 실패")
                # close 순서 = Fallback → DB → Alert → Redis (PR7 Codex review):
                # 원칙: schedule 호출자 → 호출되는 writer 순서. fallback이 모든
                # writer를 schedule할 수 있으므로 먼저 멈춰야 downstream writer
                # 들이 깔끔히 drain됨.
                # PR7: fallback controller pending probe task cancel.
                try:
                    await self._fallback_controller.close()
                except Exception:
                    logger.exception("[usdt_ws.upbit] fallback_controller.close() 실패")
                # PR5: DB writer (source of truth) timer cancel + pending tick 즉시 flush.
                try:
                    await self._db_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.upbit] db_writer.close() 실패")
                # PR6: alert evaluator (DB triggered state 변경) drain (5s timeout).
                try:
                    await self._alert_evaluator.close()
                except Exception:
                    logger.exception("[usdt_ws.upbit] alert_evaluator.close() 실패")
                # PR4: Redis writer (cache) drain — DB/Alert 확정 후 latest 동기화.
                try:
                    await self._redis_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.upbit] redis_writer.close() 실패")
