"""Gopax USDT/KRW WebSocket client — Phase B.6 Stage G7 + PR 2e.

USDT_WS_DESIGN_PLAN §12.9 (Phase B.6, 2026-05-21).

G7 + PR 2e scope (이 파일의 현재 범위):
    G1 lifecycle + G2 Subscribe/Parse + G3 Primus pong + G4 reconnect/liveness +
    2-signal status + G5 GopaxRedisWriter + G6a GopaxDbWriter + G6b
    GopaxRestFallbackController + G7 UsdtAlertEvaluator wiring + **PR 2e
    Telemetry (60s cycle summary log emit — 10 fields union: 2-signal status +
    saturation_count + fallback_probe_scheduled_count, Coinone PR 2d 1:1
    mirror)**까지. Phase B.6 Gopax WS는 fanout + 관찰 인프라 모두 land됨.

G4 주요 변경 (G4 1ea4c74 → dd3e077 누적):
    - `UsdtLivenessMonitor` 통합 (source-neutral, Coinone/Korbit 패턴 mirror).
      `last_activity_at = max(tick, heartbeat)` 기반 stale 판정.
    - 2-signal status:
        * `_connection_status` (normal/reconnecting/stale) — Primus heartbeat +
          ConnectionClosed 기반.
        * `_ticker_freshness_status` (normal/warning/degraded) — tick silence
          age 기반. degraded는 **status 갱신만** (Codex 정정: fallback hook은 G6b).
    - `_status_transition_count` 6 keys (connection 3 + ticker 3) — telemetry.
    - reconnect loop (`start()` Bithumb 패턴 mirror): ConnectionClosed/Exception
      → backoff + 재시도. stop_event 즉시 반응.
    - `_handle_primus_ping` dual-write (Codex 정정): `_last_heartbeat_at` G3
      backward compat 유지 + `_liveness.observe_heartbeat(now)` 신규 — cleanup은
      별도 PR (G5/G6a 지났으니 dedicated cleanup PR로 처리 권장).
    - `_handle_message` valid tick 시 `_liveness.observe_tick(now)` 호출.
    - Constants: STALE_AFTER_SEC=360 / TICKER_FRESHNESS_WARNING=60 / DEGRADED=300
      (Coinone 기준 provisional) / RECONNECT_BACKOFF_SEQ (1,2,4,8,16,30) tail 30.

G5 주요 변경 (현재 stage):
    - `GopaxRedisWriter` class 신규 (Coinone C5 / Bithumb U5 1:1 mirror).
      tick-level fire-and-forget, asyncio.to_thread로 sync helper 호출.
    - `_write_lock`으로 SET 순서 직렬화 (write order race 차단).
    - `MAX_PENDING_WRITES=20` saturation guard (Redis 장애 시 task 폭증 차단).
    - `_saturation_count` G5 포함 (Codex 권고 — 신규 writer라 처음부터 read-only
      counter. summary log emit은 PR 2e에서 추가). skip branch에서만 +1.
    - Redis write 성공 시 lock 밖에서 `tether_topic_trigger.request_tether_topic_trigger`
      호출 (reason: `TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS`).
    - `close(timeout)`: drain → timeout 후 cancel + `_tasks.clear`.
    - `_run_one_session` valid tick path → `_redis_writer.schedule(tick)`.
      Primus/invalid frame은 schedule 0.
    - `_run_one_session` finally → `await self._redis_writer.close()` 예외 격리
      (Bithumb/Coinone session finally close 패턴 mirror — A13 lifecycle
      invariant: __init__ 1회 생성, reconnect 사이 재사용).
    - KST ISO timestamp 변환 (`_KST = timezone(timedelta(hours=9))`, Bithumb 동일).

⚠️ Production activation 제약 (PR 2e land 후에도 유지 — Codex 보강 표현):
    PR 2e land로 telemetry 관찰 인프라 완비. 하지만 `USDT_WS_GOPAX_ENABLED=false`
    유지. **Activation은 별도 canary 조건 검토 및 짧은 안정 관찰 후 결정**
    (PR 2e 자체만으로 자동 activation 아님). 그 전까지 production env는
    default false 유지.

    flag=false invariant (transitive): scheduler `start_usdt_ws_gopax_client()`
    env false → GopaxWsClient 생성 0 → 그 안의 UsdtAlertEvaluator/Redis/DB/
    fallback/summary_task 모두 생성 0 → network/Redis/DB/Alert/summary log emit
    call 0. 별도 evaluator-only / summary-only test 불필요 (Codex 권고).

G5/G6a/G6b/G7/PR 2e acceptance:
    - flag=false 시 GopaxWsClient 생성 X + task 생성 X (G1~G4 동일)
    - flag=true 시 reconnect loop + 2-signal status (G4) + Redis fanout (G5) +
      DB fanout (G6a) + REST fallback on degraded (G6b) + Alert fanout (G7) +
      60s cycle metric INFO emit (PR 2e)
    - valid tick → Redis latest cache + topic trigger + DB write (1s window debounce) +
      AlertObservation(kind="tick") schedule
    - invalid/Primus/control frame은 Redis/DB/Alert schedule 0
    - saturation skip 시 _saturation_count +1 (helper False/exception은 미증가)
    - degraded 전이 → fallback_controller.schedule_probe (G6b)
    - normal 복귀 → fallback_controller.reset_cooldown (G6b)
    - REST probe success → Redis + DB + AlertObservation(kind="rest_probe") schedule
    - schedule 성공 시 _scheduled_probe_count +1 (skip 미증가)
    - session finally close 순서: fallback → DB → Alert → Redis (각 close 예외 격리)
    - start() lifetime 동안 summary log task 1개 (60s cycle, 10 fields emit)
    - start() finally에서 summary_task cancel/await (예외 격리)
    - production env false 유지 (canary 조건 검토 + 짧은 안정 관찰 후 activation 결정)

G6a 주요 변경 (G6a fa29bdc):
    - `GopaxDbWriter` class 신규 (Bithumb U6a / Coinone C6a 1:1 mirror).
      1s window debounce + insert_source_rate_if_changed + race-prevention +
      failure isolation + close immediate flush.

G6b 주요 변경 (G6b fbf590d):
    - `GopaxRestFallbackController` class 신규 (Coinone C6b 1:1 mirror).
      degraded threshold trigger → REST 1회 probe → 기존 Redis/DB fanout 재사용.
    - `usdt_sources.fetch_gopax_usdt_tick()` 신규 helper (additive refactor —
      기존 `_fetch_gopax()`는 본 helper 재사용으로 rate-only 호환 유지, Bithumb/
      Korbit 패턴 mirror).
    - ISO 8601 timestamp 파싱 (Codex production curl 실측 — `data["time"]`이
      "2026-05-21T10:53:14.604Z" 형태). datetime.fromisoformat + Z→+00:00 변환.
    - `_scheduled_probe_count` G6b 포함 (Codex 권고 — 신규 controller라 처음부터
      read-only counter, summary log emit은 PR 2e). skip 분기는 미증가.
    - `_set_ticker_freshness_status` hook 확장 (Coinone C6b mirror):
        * degraded 전이 → `schedule_probe("ticker_degraded")`
        * normal 복귀 (prev != normal) → `reset_cooldown()` (옛 cooldown skip 회피)
    - Constants: FALLBACK_COOLDOWN_SEC=300 (Coinone 동일) / FALLBACK_PROBE_TIMEOUT_SEC=10.

G7 주요 변경 (G7 7047feb):
    - `UsdtAlertEvaluator` import + `GopaxWsClient.__init__`에서 1회 생성
      (Coinone C7 / Bithumb U7 / Korbit K7 mirror, source-neutral helper 재사용).
    - `GopaxRestFallbackController.__init__`에 `alert_evaluator` 필수 인자 추가.
    - `_run_one_session` valid tick path → `AlertObservation(kind="tick")` schedule.
    - `_run_probe` success → `AlertObservation(kind="rest_probe")` schedule.
    - `_run_one_session` finally close 순서 변경: fallback → DB → Alert → Redis.

PR 2e 주요 변경 (현재 stage):
    - `SUMMARY_LOG_INTERVAL_SEC = 60.0` module-level constant 추가.
    - `_summary_log_loop()` method 추가 (Coinone PR 2d 1:1 mirror, 10 fields:
      frames_per_min / last_tick_age / last_heartbeat_age / max_frame_gap /
      connection_status / ticker_freshness_status / reconnect_attempts /
      status_transitions / redis_saturation_count / fallback_probe_scheduled_count).
    - `start()` lifetime 시작 직후 `summary_task = asyncio.create_task(...)`
      생성 + finally cancel/await (Coinone PR 2d / Korbit/Bithumb start() mirror).
    - sentinel 정책 (Coinone/Korbit/Upbit/Bithumb 동일): last_tick_at/heartbeat_at이
      None일 경우 -1.0 numeric sentinel.

PR 2e 누적 attribute (G1 + G2 + G3 + G4 + G5 + G6a + G6b + G7 + PR 2e):
    - G1: `_stop_event` / `_running`
    - G2: `_ws` / `_first_tick_logged`
    - G3: `_last_heartbeat_at` (dual-write 유지)
    - G4: `_liveness` / `_connection_status` / `_ticker_freshness_status` /
      `_reconnect_attempt_count` / `_status_transition_count` (6 keys)
    - G5: `_redis_writer` (GopaxRedisWriter, A13 lifecycle invariant)
    - G6a: `_db_writer` (GopaxDbWriter, 1s window debounce)
    - G6b: `_fallback_controller` (GopaxRestFallbackController, 300s cooldown,
      _scheduled_probe_count read-only counter)
    - G7: `_alert_evaluator` (UsdtAlertEvaluator, source-neutral helper 재사용 —
      Coinone C7 / Bithumb U7 / Korbit K7 mirror)
    - PR 2e: `summary_task` (start lifetime local, 60s cycle metric INFO emit —
      Coinone PR 2d 1:1 mirror, 10 fields union)
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
from app.crawlers.usdt_ws.upbit import UsdtLivenessMonitor
from app.notifications.alert_evaluator import (
    AlertObservation,
    UsdtAlertEvaluator,
)
from app.tether_topic_trigger import (
    TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
)

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.gopax")

# G5 — KST timezone for Redis latest cache timestamp ISO format (Bithumb 동일).
_KST = timezone(timedelta(hours=9))

# Gopax WebSocket endpoint (Primus protocol — G3에서 pong 응답 추가).
# 공식 문서: https://gopax.github.io/wsapi/
GOPAX_WS_URL = "wss://wsapi.gopax.co.kr"

# G2 recv loop wake-up cycle — stop_event 즉시 반응용.
RECV_TIMEOUT_SEC = 1.0

# Gopax 거래쌍 명칭 — 클라이언트측 필터링 키.
GOPAX_TARGET_PAIR = "USDT-KRW"

# G4 — Connection liveness threshold (Coinone 동일 — Primus 30s × 12 안전 마진).
# server-initiated heartbeat이라 last_activity_at (tick OR pong 수신) 기준.
STALE_AFTER_SEC = 360.0

# G4 — Ticker freshness threshold (post-activation 실측 기반 Gopax-specific tuning).
# Gopax는 5 source 중 거래량 최저 → 정상 sparse traffic에서도 tick_age ~180s,
# max_frame_gap ~230s 발생. 60s warning은 false-positive 빈번 (매 cycle 도달).
# WARNING=300s로 관측치 대비 자연 마진 + DEGRADED=600s (10분 무tick 수준)에서만
# fallback probe 트리거 — warning/degraded 의미 보존. Coinone(60/300),
# Korbit(30/120)과 별도 tuning.
TICKER_FRESHNESS_WARNING_SEC = 300.0
TICKER_FRESHNESS_DEGRADED_SEC = 600.0

# G4 — Reconnect backoff sequence (Bithumb/Coinone/Korbit 동일).
# Gopax rate limit 20 req/sec/IP에 비해 매우 여유 (1초 minimum 안전).
RECONNECT_BACKOFF_SEQ = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RECONNECT_BACKOFF_TAIL = 30.0

# G5 — Redis writer guards (Coinone C5 / Bithumb U5 동일 값).
MAX_PENDING_WRITES = 20         # task 폭증 안전망 (Redis 장애 시)
REDIS_CLOSE_TIMEOUT_SEC = 1.0   # close() drain timeout — 후 강제 cancel

# G6a — DB writer debounce window (Bithumb U6a / Coinone C6a / Korbit K6a / KRX 동일).
# tick rate (~100ms+) vs DB I/O (~10-50ms)이라 매 tick INSERT는 DB 부담. window
# 내 마지막 tick만 helper 호출.
DB_WRITE_WINDOW_SEC = 1.0

# G6b — REST fallback controller (Coinone 동일 — 300s cooldown, 2-signal status mirror).
# COOLDOWN_SEC = DEGRADED_SEC: probe 빈도 최대 5분 1회 (degraded transition 후 다음
# degraded까지 cooldown). STALE_AFTER_SEC=360 (connection liveness 보조)과 별개.
FALLBACK_COOLDOWN_SEC = 300.0       # provisional, ticker freshness degraded 후 동일
FALLBACK_PROBE_TIMEOUT_SEC = 10.0   # REST HTTP timeout (Bithumb 동일)

# PR 2e — summary log emit cycle (Coinone PR 2d / Korbit PR 2a / Bithumb PR 2c / Upbit PR 2b 동일).
# start() lifetime 동안 60s cycle로 metric INFO 1줄 emit. _run_one_session 영향 0.
SUMMARY_LOG_INTERVAL_SEC = 60.0


class GopaxRedisWriter:
    """Gopax USDT/KRW Redis latest writer — tick-level, fire-and-forget (G5).

    Coinone `CoinoneRedisWriter` / Bithumb `BithumbRedisWriter` 1:1 mirror.
    source는 tick["source"]에서 "gopax"로 자동 — class-level source 인자 없음.

    G5 핵심 (Bithumb U5 mirror):
        - 매 valid tick → `set_latest_usdt_rate_from_sync_job` 호출 (debounce 없음)
        - schedule()은 background task 생성 후 즉시 반환 (recv loop 격리)
        - asyncio.to_thread로 sync helper 호출 (event loop non-blocking)
        - MAX_PENDING_WRITES 한도로 Redis 장애 시 task 폭증 차단
        - close() drain-first, timeout 후 cancel (마지막 tick 보존)
        - Redis write 성공 시에만 tether topic trigger (lock 밖에서 호출)
        - reason은 기존 TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS 재사용

    G5 saturation counter (Codex 권고 — 신규 writer라 처음부터 read-only counter 포함):
        - _saturation_count: MAX_PENDING_WRITES skip 발생 횟수
        - saturation skip branch에서만 +1 (helper false/exception은 미증가)
        - summary log emit은 PR 2e에서 추가 예정 (Coinone/Bithumb 패턴 분리)

    Failure isolation:
        - sync helper 예외/False → log only, task가 _tasks에서 제거됨 (callback)
        - tether topic trigger 예외 → log only, writer 동작/WS session 영향 X
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        # write order 직렬화 — Bithumb U5 patterns: 병렬 to_thread + connection pool
        # 조합에서 old tick이 new tick을 덮는 race 차단.
        self._write_lock: asyncio.Lock = asyncio.Lock()
        # G5 saturation counter (Codex 권고).
        self._saturation_count: int = 0

    @property
    def saturation_count(self) -> int:
        """G5 — Redis write queue saturation skip 누적 횟수 (read-only).

        의미: MAX_PENDING_WRITES skip 발생 횟수. write 성공/실패 무관 —
        helper false/exception은 saturation으로 세지 않음 (Codex 대칭 검증).
        """
        return self._saturation_count

    def schedule(self, tick: dict) -> None:
        """tick → background Redis write task. fire-and-forget.

        Saturation 시 (`len(_tasks) >= MAX_PENDING_WRITES`) skip + warning + counter ++.
        Redis 장애로 task가 누적되는 시나리오 차단.
        """
        if len(self._tasks) >= MAX_PENDING_WRITES:
            self._saturation_count += 1
            logger.warning(
                "[usdt_ws.gopax] Redis write queue saturated (%d in-flight) — skip tick",
                len(self._tasks),
            )
            return
        task = asyncio.create_task(self._write_async(tick))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write_async(self, tick: dict) -> None:
        """sync Redis helper를 to_thread로 호출. 실패는 격리.

        `_write_lock`으로 직렬화 — 동시 to_thread + connection pool 조합에서
        발생할 수 있는 SET 순서 역전 차단. Redis write 성공 시 lock 밖에서 tether
        topic trigger 호출. trigger 예외는 writer/WS session에 전파하지 않음.
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
                        "[usdt_ws.gopax] Redis write returned False "
                        "(helper 내부 log 참조, WS session 유지)"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[usdt_ws.gopax] Redis write task crashed (격리, WS session 유지)"
                )
                success = False

        # lock 밖에서 trigger — Redis write 성공 시에만.
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
                    "[usdt_ws.gopax] tether topic trigger 호출 실패 (격리, WS session 유지)"
                )

    async def close(self, timeout: float = REDIS_CLOSE_TIMEOUT_SEC) -> None:
        """pending writes drain — session/shutdown 시 마지막 tick latest 보존.

        timeout (default 1s) 안에 drain 안 되면 강제 cancel. Redis hung 시 무한
        대기 방지. finally에서 `_tasks.clear()` 보장 (A13 lifecycle invariant —
        __init__ 1회 생성, reconnect 사이 재사용).
        """
        if not self._tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            for task in self._tasks:
                if not task.done():
                    task.cancel()
        finally:
            self._tasks.clear()


class GopaxDbWriter:
    """Gopax USDT/KRW DB writer — 1s window debounce + insert_source_rate_if_changed.

    Bithumb `BithumbDbWriter` / Coinone `CoinoneDbWriter` 1:1 mirror.
    tick rate (~100ms+) vs DB I/O (~10-50ms)이라 매 tick INSERT는 DB 부담. window
    내 마지막 tick만 helper 호출.

    asyncio loop 차단 방지:
        sync SQLAlchemy 호출은 asyncio.to_thread로 격리. DB session은 to_thread
        내부에서 get_db_context()로 생성/close.

    race 방지 (Bithumb U6a mirror):
        DB write (to_thread) 진행 중 새 tick 도착 → _pending_tick 갱신,
        _timer.done() X 라 schedule()이 새 timer 안 만듦. write 종료 후
        finally 블록에서 _pending_tick 재확인 후 새 timer 예약 — 누락 방지.

    failure isolation: DB exception → log only, WS session 영향 X.

    shutdown (close): pending tick 1s window 기다리지 않고 즉시 flush.
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
                "[usdt_ws.gopax] DB write failed (격리): %s: %s",
                type(exc).__name__, exc,
            )
        finally:
            # write 중 들어온 tick 처리 — 새 window 예약. finally는 같은 코루틴
            # frame 안 sync 영역이라 다른 코루틴 race X.
            if self._pending_tick is not None:
                self._timer = asyncio.create_task(self._flush_after_window())

    @staticmethod
    def _sync_db_write(tick: dict) -> None:
        """sync DB write — to_thread 내부 실행.

        get_db_context()로 SessionLocal 생성/close. insert_source_rate_if_changed는
        내부에서 commit. Decimal 정규화 없음 (REST polling 및 Bithumb/Coinone과
        일관, float 그대로 전달).
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

        1s window 기다리지 않고 마지막 tick DB 저장. window 일관성보다 last tick
        보장 우선 (shutdown은 드문 이벤트).
        """
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
            try:
                await self._timer
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[usdt_ws.gopax] db_writer timer cancel 실패")
        self._timer = None
        tick = self._pending_tick
        self._pending_tick = None
        if tick is None:
            return
        try:
            await asyncio.to_thread(self._sync_db_write, tick)
        except Exception as exc:
            logger.warning(
                "[usdt_ws.gopax] DB write on close failed (격리): %s: %s",
                type(exc).__name__, exc,
            )


class GopaxRestFallbackController:
    """Gopax REST fallback (silent probe) — G6b + G7 alert wiring.

    Coinone CoinoneRestFallbackController 1:1 mirror. G7에서 alert evaluator inject
    완료 → probe success 시 AlertObservation(kind="rest_probe") schedule 추가.

    DATA frame age > TICKER_FRESHNESS_DEGRADED_SEC (300s) 감지 시 REST 1회
    probe → normalized tick → 기존 fanout (Redis + DB + Alert) 재사용. WS reconnect loop는
    그대로 유지, fallback은 freshness 보조.

    Guardrails (Coinone C6b/C7 mirror):
        - schedule_probe()는 sync/non-blocking (_set_ticker_freshness_status 동기 흐름에서 호출).
        - In-flight skip: probe 진행 중 중복 trigger 차단.
        - Cooldown skip: 마지막 probe 종료 후 FALLBACK_COOLDOWN_SEC 미경과 시 skip
          (degraded 지속 중 폭주 방지).
        - Probe 실패도 cooldown 적용 (REST rate limit 보호).
        - reset_cooldown(): normal 복귀 시 호출 → 다음 degraded 즉시 1회 probe 보장.
        - REST 실패 격리: log only, WS session/reconnect 영향 X.
        - fallback tick의 source/asset = "gopax"/"usdt-krw" (downstream 일관).
        - G7: probe success 시 AlertObservation(kind="rest_probe") schedule
          (Bithumb U7 mirror — log/metric 영역에서 tick vs probe 분리).

    G6b scheduled probe counter (Codex 권고 — 신규 controller라 처음부터 read-only counter):
        - _scheduled_probe_count: schedule_probe()가 in-flight/cooldown/no-loop skip
          통과 후 loop.create_task() 성공 시점에만 +1. skip은 미증가.
        - summary log emit은 PR 2e에서 추가 예정.
        - probe 결과 (success/timeout/none/exception) 무관 — schedule 자체만 카운팅.
    """

    def __init__(
        self,
        redis_writer: "GopaxRedisWriter",
        db_writer: "GopaxDbWriter",
        alert_evaluator: "UsdtAlertEvaluator",
        *,
        cooldown_sec: float = FALLBACK_COOLDOWN_SEC,
        probe_timeout_sec: float = FALLBACK_PROBE_TIMEOUT_SEC,
    ) -> None:
        self._redis_writer = redis_writer
        self._db_writer = db_writer
        self._alert_evaluator = alert_evaluator  # G7: AlertObservation schedule on probe success
        self._cooldown_sec = cooldown_sec
        self._probe_timeout_sec = probe_timeout_sec
        self._in_flight: bool = False
        self._cooldown_until: float = 0.0
        self._pending_task: Optional[asyncio.Task] = None
        # G6b — scheduled probe counter (Codex 권고).
        # in-flight/cooldown/no-loop skip 미증가, loop.create_task() 성공 시점에만 +1.
        self._scheduled_probe_count: int = 0

    @property
    def scheduled_probe_count(self) -> int:
        """G6b — REST fallback probe schedule 누적 횟수 (read-only, instance lifetime).

        increment 조건: `schedule_probe()`가 in-flight/cooldown/no-loop skip 통과 후
        `loop.create_task(_run_probe(...))` 직후. probe 결과(success/timeout/none/
        exception) 무관 — schedule 자체만 카운팅.
        """
        return self._scheduled_probe_count

    def schedule_probe(self, reason: str) -> None:
        """sync: in-flight/cooldown 체크 후 background probe task 생성. 즉시 반환.

        `_set_ticker_freshness_status("degraded")` 안에서 호출 — 동기 흐름이라 즉시 반환 필수.
        Production은 항상 asyncio context 안 (recv loop / reconnect loop), 단
        sync test에서 _set_ticker_freshness_status 직접 호출 시 no-loop. defensive하게 skip.
        """
        now = time.time()
        if self._in_flight:
            logger.debug(
                "[usdt_ws.gopax.fallback] probe in-flight, skip (reason=%s)", reason,
            )
            return
        if now < self._cooldown_until:
            remaining = self._cooldown_until - now
            logger.debug(
                "[usdt_ws.gopax.fallback] cooldown active, skip (%.1fs remaining, reason=%s)",
                remaining, reason,
            )
            return
        # Loop 체크를 coroutine 생성 전에 — no-loop 시 coroutine 미생성으로
        # "coroutine was never awaited" warning 회피.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[usdt_ws.gopax.fallback] no event loop, skip probe (reason=%s)", reason,
            )
            return
        self._pending_task = loop.create_task(self._run_probe(reason))
        self._in_flight = True
        # G6b counter (Codex 권고): schedule 성공 시점에만 +1 (skip 분기들은 위에서 return).
        self._scheduled_probe_count += 1

    def reset_cooldown(self) -> None:
        """normal 복귀 시 호출 — cooldown clear. 다음 degraded 즉시 1회 probe 보장."""
        self._cooldown_until = 0.0
        logger.debug("[usdt_ws.gopax.fallback] cooldown reset on normal recovery")

    async def _run_probe(self, reason: str) -> None:
        """REST probe → normalized tick → fanout (Redis + DB + Alert).

        실패 시 log only + cooldown 적용 (재시도 X). WS session 영향 X.
        G7: probe success 시 AlertObservation(kind="rest_probe") schedule (Coinone C7 mirror).
        """
        try:
            logger.info(
                "[usdt_ws.gopax.fallback] probe start (reason=%s)", reason,
            )
            tick = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_gopax_tick),
                timeout=self._probe_timeout_sec,
            )
            if tick is None:
                logger.warning(
                    "[usdt_ws.gopax.fallback] probe returned None — REST/parse 실패 or invalid rate",
                )
                return

            # 기존 fanout 재사용 (G5 Redis writer + G6a DB writer + G7 Alert evaluator).
            self._redis_writer.schedule(tick)
            self._db_writer.schedule(tick)
            # G7: REST probe kind 구분 (Coinone C7 mirror — log/metric 영역에서 tick vs probe 분리).
            observation = AlertObservation(
                source=tick["source"],
                asset=tick["asset"],
                rate=tick["rate"],
                timestamp_ms=tick["timestamp_ms"],
                kind="rest_probe",
            )
            self._alert_evaluator.schedule(observation)
            logger.info(
                "[usdt_ws.gopax.fallback] probe success (rate=%s, ts_ms=%d, reason=%s)",
                tick["rate"], tick["timestamp_ms"], reason,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "[usdt_ws.gopax.fallback] probe timeout (%.1fs, reason=%s)",
                self._probe_timeout_sec, reason,
            )
        except Exception:
            logger.exception(
                "[usdt_ws.gopax.fallback] probe error (격리, reason=%s)", reason,
            )
        finally:
            # 실패해도 cooldown 적용 (REST rate limit 보호).
            self._cooldown_until = time.time() + self._cooldown_sec
            self._in_flight = False

    @staticmethod
    def _fetch_gopax_tick() -> Optional[dict]:
        """sync REST fetch — to_thread 내부 실행. 모듈 내부 import (순환 회피)."""
        from app.crawlers.usdt_sources import fetch_gopax_usdt_tick
        return fetch_gopax_usdt_tick()

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
                logger.exception("[usdt_ws.gopax.fallback] pending task cleanup 실패")
        self._pending_task = None


class GopaxWsClient:
    """Gopax USDT/KRW WebSocket client — Phase B.6 Stage G7 + PR 2e.

    G1~G7 누적 + PR 2e Telemetry (60s cycle summary log emit, 10 fields union —
    Coinone PR 2d 1:1 mirror). fanout(Redis + DB + Fallback + Alert) + 관찰
    인프라 모두 land됨.

    ⚠️ Production activation 제약 (PR 2e land 후에도 유지 — Codex 보강 표현):
        PR 2e land로 telemetry 관찰 인프라 완비. 하지만 USDT_WS_GOPAX_ENABLED=false
        유지. activation은 별도 canary 조건 검토 및 짧은 안정 관찰 후 결정
        (PR 2e 자체만으로 자동 activation 아님). 자세한 내용은 module docstring 참조.
    """

    def __init__(self) -> None:
        # G1 lifecycle state
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        # G2 session state — connect 결과 + first tick log throttle
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False
        # G3 heartbeat — Primus pong send 성공 시점 timestamp. G4 진입 후에는
        # Codex 권고 dual-write로 유지 (UsdtLivenessMonitor.last_heartbeat_at과
        # 병행). cleanup은 별도 PR (G5/G6a 지났으니 dedicated cleanup PR로 처리 권장).
        self._last_heartbeat_at: Optional[float] = None
        # G4 — 2-signal status + liveness (Coinone/Korbit 패턴 mirror).
        # last_activity_at = max(tick, heartbeat)로 stale 판정 (UsdtLivenessMonitor).
        self._liveness: UsdtLivenessMonitor = UsdtLivenessMonitor()
        self._connection_status: str = "normal"          # normal/reconnecting/stale
        self._ticker_freshness_status: str = "normal"    # normal/warning/degraded
        self._reconnect_attempt_count: int = 0
        self._status_transition_count: dict[str, int] = {
            "connection_normal": 0,
            "connection_reconnecting": 0,
            "connection_stale": 0,
            "ticker_normal": 0,
            "ticker_warning": 0,
            "ticker_degraded": 0,
        }
        # G5 — Redis writer (tick-level, fire-and-forget). A13 lifecycle invariant:
        # __init__에서 1회 생성, reconnect 사이 재사용 (재할당 없음). Bithumb U5 mirror.
        self._redis_writer: GopaxRedisWriter = GopaxRedisWriter()
        # G6a — DB writer (1s window debounce). Bithumb U6a / Coinone C6a mirror.
        self._db_writer: GopaxDbWriter = GopaxDbWriter()
        # G7 — Alert evaluator (observation-based, source-neutral helper 재사용).
        # Coinone C7 / Bithumb U7 / Korbit K7 mirror — Gopax client 자체 instance 보유.
        self._alert_evaluator: UsdtAlertEvaluator = UsdtAlertEvaluator()
        # G6b — REST fallback controller (silent probe on degraded). Coinone C6b mirror.
        # Redis + DB writer + Alert evaluator inject (G7 alert_evaluator 필수).
        self._fallback_controller: GopaxRestFallbackController = GopaxRestFallbackController(
            redis_writer=self._redis_writer,
            db_writer=self._db_writer,
            alert_evaluator=self._alert_evaluator,
        )

    @staticmethod
    def _build_subscribe_payload() -> dict:
        """Gopax SubscribeToTickers payload — pair 지정 불가, 전체 ticker 구독.

        guide §7 spec: {"n": "SubscribeToTickers", "o": {}}
        클라이언트측에서 USDT-KRW만 필터링 (`_parse_ticker_message` 안).
        """
        return {"n": "SubscribeToTickers", "o": {}}

    @staticmethod
    def _is_primus_ping(raw) -> bool:
        """Primus ping 매칭 — **ping 전용** (Codex 정정).

        G3는 pong 응답 단계라 ping만 잡는다. pong/open/close 등 다른 Primus
        control frame은 False return (`_parse_ticker_message`의 G2 skip 분기에서
        별도 처리). 3 form 모두 처리:
            (i) JSON string form: '"primus::ping::..."' (with outer quotes)
            (ii) Plain text form: 'primus::ping::...' (no quotes)
            (iii) bytes form: G2 fast-path와 동일하게 decode 후 매칭

        Returns:
            True: Primus ping frame (3 form 중 하나)
            False: 그 외 (일반 ticker / non-ping Primus control frame / invalid)
        """
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return False
        if not isinstance(raw, str):
            return False
        text = raw.strip()
        return text.startswith('"primus::ping::') or text.startswith("primus::ping::")

    async def _handle_primus_ping(self, ws, raw) -> bool:
        """Primus ping → pong replacement + ws.send + _last_heartbeat_at 갱신.

        guide §7 line 408-410 명세:
            - JSON string form: '"primus::ping::..."' → '"primus::pong::..."'
            - Plain text form: 'primus::ping::...' → 'primus::pong::...'
            - replace("::ping::", "::pong::")로 두 form 모두 처리 가능

        Heartbeat timestamp 정책 (Codex 검토 포인트 #3):
            send 성공 시점에만 `_last_heartbeat_at = time.time()` 갱신. send 실패
            시 갱신 안 함 (이후 G4 liveness가 stale 판정 가능).

        Returns:
            True: pong 송신 성공 (heartbeat 갱신 완료)
            False: send 실패 — Codex 정정으로 _run_one_session()에서 session 종료
                   (G4 reconnect 부재라 깨진 session 명시적 종료).
        """
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        pong = raw.replace("::ping::", "::pong::")
        try:
            await ws.send(pong)
            now = time.time()
            # G4 dual-write (Codex 정정): _last_heartbeat_at G3 backward compat 유지
            # + _liveness.observe_heartbeat(now) 신규 (is_stale 판정 입력).
            # cleanup은 별도 PR (G5/G6a 지났으니 dedicated cleanup PR로 처리 권장).
            self._last_heartbeat_at = now
            self._liveness.observe_heartbeat(now)
            logger.debug("[usdt_ws.gopax] primus pong sent")
            return True
        except Exception:
            logger.exception(
                "[usdt_ws.gopax] primus pong send 실패 — session 종료 "
                "(G4 reconnect로 backoff 재시도 예정)",
            )
            return False

    @staticmethod
    def _normalize_tick(item: dict) -> Optional[dict]:
        """Gopax ticker item → normalized tick {source, asset, rate, timestamp_ms}.

        Guard (다른 4 source 패턴 mirror):
            - last 누락 / 0 / negative → None
            - lastTraded 누락 / 0 → None (timestamp 0은 invalid)
            - 타입 변환 실패 → None (격리)
        """
        try:
            rate = float(item["last"])
            if rate <= 0:
                return None
            timestamp_ms = int(item.get("lastTraded", 0) or 0)
            if timestamp_ms <= 0:
                return None
            return {
                "source": "gopax",
                "asset": "usdt-krw",
                "rate": rate,
                "timestamp_ms": timestamp_ms,
            }
        except (KeyError, ValueError, TypeError):
            return None

    def _parse_ticker_message(self, raw) -> Optional[dict]:
        """Gopax 2종 응답 + Primus ping skip (G2 placeholder) + USDT-KRW 필터링.

        Frame 종류:
            - Initial response: `n="SubscribeToTickers"`, `o.data`는 전체 ticker array
              → array iterate + `tradingPairName=="USDT-KRW"` 매칭
            - Delta: `n="TickerEvent"`, `o["USDT-KRW"]`는 dict
              → key 존재 시 직접 추출
            - Primus ping (server-initiated heartbeat): G2에서 skip만, G3에서 pong 응답

        Primus skip 정책 (G2 scope, Codex 정정 반영):
            1. JSON string form: `'"primus::ping::..."'` → strip 후 startswith 매칭
            2. Plain text form: `'primus::ping::...'` → startswith 매칭
            3. JSON-decoded str: `json.loads()` 결과가 str이고 `"primus::"` 시작
            세 가지 형태 모두 None return. G3에서 pong replacement 응답 추가.

        Returns normalized tick {source, asset, rate, timestamp_ms} or None.
        """
        # 1. bytes → str
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None

        # 2. Primus skip — raw text level (fast path).
        # JSON string form ('"primus::...'") + plain text form ('primus::...') 모두 처리.
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith('"primus::') or text.startswith("primus::"):
                return None

        # 3. JSON parse
        if isinstance(raw, str):
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                return None
        elif isinstance(raw, dict):
            message = raw
        else:
            return None

        # 4. JSON-decoded str case — json.loads('"primus::..."') 결과가 str "primus::..."
        if isinstance(message, str):
            if message.startswith("primus::"):
                return None
            return None  # unknown str frame → skip

        # 5. dict frame type 분기
        if not isinstance(message, dict):
            return None

        n = message.get("n")
        if n == "SubscribeToTickers":
            # Initial response — 전체 ticker array에서 USDT-KRW 매칭
            o = message.get("o", {})
            if not isinstance(o, dict):
                return None
            data = o.get("data", [])
            if not isinstance(data, list):
                return None
            for item in data:
                if isinstance(item, dict) and item.get("tradingPairName") == GOPAX_TARGET_PAIR:
                    return self._normalize_tick(item)
            return None
        elif n == "TickerEvent":
            # Delta — USDT-KRW key 존재 시 직접 추출
            o = message.get("o", {})
            if not isinstance(o, dict):
                return None
            item = o.get(GOPAX_TARGET_PAIR)
            if isinstance(item, dict):
                return self._normalize_tick(item)
            return None

        return None

    def _handle_message(self, raw) -> Optional[dict]:
        """raw frame → normalized tick (or None). First tick INFO 1회 + 이후 DEBUG.

        G4 갱신: valid tick 시 `_liveness.observe_tick(now)` 호출 (Bithumb 패턴 mirror)
        — last_activity_at 갱신 → stale check 입력. fanout (Redis/DB/Alert)은 G5-G7 영역.
        """
        tick = self._parse_ticker_message(raw)
        if tick is None:
            return None
        # G4 — liveness observe (last_activity_at = max(tick, heartbeat))
        self._liveness.observe_tick(time.time())
        if not self._first_tick_logged:
            logger.info("[usdt_ws.gopax] first tick", extra=tick)
            self._first_tick_logged = True
        else:
            logger.debug("[usdt_ws.gopax] tick", extra=tick)
        return tick

    def _set_connection_status(self, new_status: str) -> None:
        """G4 — connection status 전이 + counter + log (Coinone/Korbit 패턴 mirror).

        동일 status 재호출 시 미증가 (log flood 방지). transition counter는
        `connection_<new_status>` key로 증가.
        """
        if self._connection_status == new_status:
            return
        prev = self._connection_status
        self._connection_status = new_status
        key = f"connection_{new_status}"
        if key in self._status_transition_count:
            self._status_transition_count[key] += 1
        logger.info(
            "[usdt_ws.gopax] connection_status %s → %s (reconnect_attempt=%d max_gap=%.2fs)",
            prev, new_status, self._reconnect_attempt_count,
            self._liveness.max_frame_gap_sec,
        )

    def _set_ticker_freshness_status(self, new_status: str) -> None:
        """G4/G6b — ticker freshness status 전이 + counter + log + (G6b) fallback hook.

        G6b 확장 (Coinone C6b 패턴 mirror):
            - degraded 진입 → fallback_controller.schedule_probe("ticker_degraded")
            - normal 복귀 (prev != normal) → fallback_controller.reset_cooldown()
              새 outage cycle의 첫 degraded probe가 옛 cooldown으로 skip되지 않도록.
        """
        if self._ticker_freshness_status == new_status:
            return
        prev = self._ticker_freshness_status
        self._ticker_freshness_status = new_status
        key = f"ticker_{new_status}"
        if key in self._status_transition_count:
            self._status_transition_count[key] += 1
        logger.info(
            "[usdt_ws.gopax] ticker_freshness_status %s → %s "
            "(last_tick_at=%s max_gap=%.2fs)",
            prev, new_status, self._liveness.last_tick_at,
            self._liveness.max_frame_gap_sec,
        )
        # G6b — fallback hook (Coinone C6b mirror).
        if new_status == "degraded":
            self._fallback_controller.schedule_probe(reason="ticker_degraded")
        elif prev != "normal" and new_status == "normal":
            self._fallback_controller.reset_cooldown()

    @staticmethod
    def _compute_backoff(attempt: int) -> float:
        """G4 — reconnect backoff seq lookup (Bithumb mirror).

        attempt: 1-based. seq exhausted 시 tail 30s 유지.
        """
        if attempt <= 0:
            return RECONNECT_BACKOFF_SEQ[0]
        if attempt <= len(RECONNECT_BACKOFF_SEQ):
            return RECONNECT_BACKOFF_SEQ[attempt - 1]
        return RECONNECT_BACKOFF_TAIL

    async def _run_one_session(self) -> None:
        """G7 single session — connect + subscribe + recv + Primus pong + liveness +
        status + Redis fanout (G5) + DB fanout (G6a) + REST fallback (G6b) +
        Alert fanout (G7).

        Lifecycle (G7 누적 scope):
            1. websockets.connect(GOPAX_WS_URL) + reset_active_session()
            2. SubscribeToTickers payload send + _set_connection_status("normal")
            3. recv loop: wake every RECV_TIMEOUT_SEC (stop_event 반응)
            4. 매 iter: stale check (`_liveness.is_stale(now, STALE_AFTER_SEC)`) →
               connection_status 전이 (stale ↔ normal)
            5. 매 iter: ticker freshness 전이 (now - last_tick_at 기준 warning/degraded/normal)
               — degraded → fallback_controller.schedule_probe (G6b),
                 normal 복귀 → fallback_controller.reset_cooldown (G6b).
               (실제 hook은 _set_ticker_freshness_status 내부에서 처리.)
            6. Primus ping → pong send + dual-write heartbeat → continue
               (send 실패 시 RuntimeError raise → start() except 경로로 attempt++ + backoff)
            7. 그 외 frame → parse + handle + `_liveness.observe_tick(now)` + G5
               Redis fanout + G6a DB fanout + G7 AlertObservation(kind="tick")
               schedule (valid tick일 때만)
            8. ConnectionClosed → raise (return X) → start() except 경로로 attempt++ + backoff
               (return 시 start() else: continue로 tight loop hang)
            9. finally close 순서 (G7 갱신, Coinone C7 mirror 최종):
               G6b fallback → G6a DB → **G7 Alert** → G5 Redis. fallback이
               downstream writer를 schedule할 수 있어 먼저 멈추고, Redis는 항상
               마지막. 각 close 예외 격리 — 한 close 실패해도 뒤 close 실행 보장.
            10. PR 2e summary log emit은 별도 stage.

        ⚠️ PR 2e land 후에도 flag=true 자동 activation 아님 (Codex 보강 표현):
            PR 2e telemetry 관찰 인프라 완비. activation은 별도 canary 조건
            검토 및 짧은 안정 관찰 후 결정. 자세한 내용은 module docstring 참조.
        """
        async with websockets.connect(
            GOPAX_WS_URL,
            ping_interval=None,    # WS auto-ping 비활성 — Primus heartbeat는 G3에서 처리
            ping_timeout=None,
            open_timeout=10,
        ) as ws:
            self._ws = ws
            # G4 — active session 시작: liveness reset + connection_status normal.
            self._liveness.reset_active_session()
            logger.info("[usdt_ws.gopax] connected url=%s", GOPAX_WS_URL)
            payload = self._build_subscribe_payload()
            await ws.send(json.dumps(payload))
            logger.info(
                "[usdt_ws.gopax] subscribed all tickers (client-side filter pair=%s)",
                GOPAX_TARGET_PAIR,
            )
            self._set_connection_status("normal")

            try:
                while not self._stop_event.is_set():
                    now = time.time()
                    # G4 — stale check: last_activity_at (max tick/heartbeat) 기준.
                    if self._connection_status != "stale" and self._liveness.is_stale(now, STALE_AFTER_SEC):
                        self._set_connection_status("stale")
                    elif self._connection_status == "stale" and not self._liveness.is_stale(now, STALE_AFTER_SEC):
                        self._set_connection_status("normal")

                    # G4 — ticker freshness 전이 (tick silence age 기반).
                    # G6b: degraded → fallback_controller.schedule_probe (위 _set_ticker_freshness_status에서 처리).
                    if self._liveness.last_tick_at is not None:
                        ticker_age = now - self._liveness.last_tick_at
                        if (
                            self._ticker_freshness_status == "normal"
                            and ticker_age > TICKER_FRESHNESS_WARNING_SEC
                        ):
                            self._set_ticker_freshness_status("warning")
                        elif (
                            self._ticker_freshness_status == "warning"
                            and ticker_age > TICKER_FRESHNESS_DEGRADED_SEC
                        ):
                            self._set_ticker_freshness_status("degraded")
                        elif (
                            self._ticker_freshness_status != "normal"
                            and ticker_age <= TICKER_FRESHNESS_WARNING_SEC
                        ):
                            self._set_ticker_freshness_status("normal")

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                    except asyncio.TimeoutError:
                        continue
                    except ConnectionClosed:
                        # G4 (Codex 정정 v2): return 대신 raise — start()의 except
                        # ConnectionClosed 경로로 attempt++ + backoff + reconnecting
                        # status 전이. return 시 start()는 정상 종료로 인식 → else: continue
                        # → tight loop hang.
                        logger.warning(
                            "[usdt_ws.gopax] connection closed during recv "
                            "(G4 reconnect loop으로 raise — backoff 재시도)",
                        )
                        raise

                    # G3: Primus ping 먼저 처리 (pong 송신 + heartbeat 갱신).
                    if self._is_primus_ping(raw):
                        if not await self._handle_primus_ping(ws, raw):
                            # G4 (Codex 정정 v2): return 대신 raise — start()의
                            # except Exception 경로로 attempt++ + backoff.
                            # return 시 정상 종료 인식 → tight loop.
                            raise RuntimeError(
                                "primus pong send failed (G4 reconnect loop으로 backoff)"
                            )
                        continue

                    # 그 외 frame → parse + handle + G5 Redis fanout + G6a DB fanout + G7 Alert fanout.
                    tick = self._handle_message(raw)
                    if tick is not None:
                        # G5: valid tick → Redis fanout (Bithumb U5 mirror).
                        # invalid/Primus/control frame은 tick=None이라 schedule 호출 0.
                        self._redis_writer.schedule(tick)
                        # G6a: DB writer (1s window debounce, race-prevention).
                        # Bithumb U6a / Coinone C6a mirror.
                        self._db_writer.schedule(tick)
                        # G7: AlertObservation schedule (source-neutral evaluator 재사용).
                        # Coinone C7 / Bithumb U7 / Korbit K7 mirror — kind="tick" 구분.
                        observation = AlertObservation(
                            source=tick["source"],
                            asset=tick["asset"],
                            rate=tick["rate"],
                            timestamp_ms=tick["timestamp_ms"],
                            kind="tick",
                        )
                        self._alert_evaluator.schedule(observation)
            finally:
                self._ws = None
                # G6b finally 1: fallback controller close 먼저 (Bithumb U7 close 순서 mirror —
                # fallback → DB → Alert → Redis. fallback이 downstream writer를
                # schedule할 수 있으므로 먼저 멈춰야 깔끔하게 drain).
                # 각 close는 개별 try/except로 격리 — 한 close 실패해도 뒤 close 실행 보장.
                try:
                    await self._fallback_controller.close()
                except Exception:
                    logger.exception("[usdt_ws.gopax] fallback_controller.close() 실패")
                # G6a finally 2: DB writer (pending tick 즉시 flush + timer cancel).
                try:
                    await self._db_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.gopax] db_writer.close() 실패")
                # G7 finally 3: Alert evaluator drain (FCM 보호, Coinone C7 mirror).
                # 예외 격리: WS loop에 전파 X.
                try:
                    await self._alert_evaluator.close()
                except Exception:
                    logger.exception("[usdt_ws.gopax] alert_evaluator.close() 실패")
                # G5 finally 4: Redis writer drain + close 마지막 (Bithumb 순서 mirror).
                # session finally에서 close: 그 session task drain + _tasks.clear,
                # 다음 session에서 빈 _tasks로 새 schedule.
                try:
                    await self._redis_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.gopax] redis_writer.close() 실패")

    async def start(self) -> None:
        """G4 reconnect loop — session crash 시 backoff 후 재시도.

        Bithumb start() 패턴 mirror. ConnectionClosed / Exception / pong send 실패
        (`RuntimeError` raise) → except 경로에서 attempt++ + backoff + reconnecting
        status 전이 → 재시도. stop_event set 시 즉시 종료.

        Codex v2 정정: `_run_one_session()`이 실패 신호를 return이 아니라 exception
        으로 올려야 reconnect except 경로가 실제로 동작 (return 시 else: continue로
        정상 종료 인식 → tight loop hang).

        Acceptance:
            - 중복 start 방지 (`_running` flag)
            - ConnectionClosed → `_set_connection_status("reconnecting")` +
              `_reconnect_attempt_count++` + backoff sleep
            - Exception 동일 처리
            - stop_event set 시 즉시 종료 (backoff sleep도 즉시 break)
            - flag=false 시 본 함수 호출 자체가 발생하지 않음 (scheduler가 차단)

        Production activation 제약 (Codex 보강 표현 — PR 2e land 후에도 유지):
            PR 2e land로 telemetry 관찰 인프라 완비. 하지만 USDT_WS_GOPAX_ENABLED=
            false 유지. activation은 **별도 canary 조건 검토 및 짧은 안정 관찰 후
            결정** (PR 2e 자체만으로 자동 activation 아님).

        PR 2e 추가 lifecycle (현재 stage):
            - 시작 직후: `summary_task = asyncio.create_task(self._summary_log_loop())`
              (Coinone PR 2d mirror, 60s cycle, 10 fields emit, _run_one_session 영향 0).
            - finally: summary_task cancel + await + CancelledError pass + Exception logger.
        """
        if self._running:
            logger.debug("[usdt_ws.gopax] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info(
            "[usdt_ws.gopax] start (G7 + PR 2e — reconnect loop + liveness + Redis/DB/Alert fanout + REST fallback + summary log)",
        )
        # PR 2e — start-level summary log task (Coinone PR 2d / Korbit/Upbit/Bithumb 패턴 mirror).
        # _run_one_session 영향 0 — start lifetime 동안만 60s cycle metric INFO emit.
        summary_task = asyncio.create_task(self._summary_log_loop())
        attempt = 0
        try:
            while not self._stop_event.is_set():
                try:
                    await self._run_one_session()
                    if self._stop_event.is_set():
                        break
                    attempt = 0  # 정상 종료 (rare — recv loop가 stop_event로 빠진 case)
                except asyncio.CancelledError:
                    raise
                except ConnectionClosed as exc:
                    if self._stop_event.is_set():
                        break
                    self._set_connection_status("reconnecting")
                    attempt += 1
                    self._reconnect_attempt_count += 1
                    backoff = self._compute_backoff(attempt)
                    logger.warning(
                        "[usdt_ws.gopax] connection closed (attempt %d): %s — backoff %.1fs",
                        attempt, exc, backoff,
                    )
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    self._set_connection_status("reconnecting")
                    attempt += 1
                    self._reconnect_attempt_count += 1
                    backoff = self._compute_backoff(attempt)
                    logger.warning(
                        "[usdt_ws.gopax] session error (attempt %d): %s: %s — backoff %.1fs",
                        attempt, type(exc).__name__, exc, backoff,
                    )
                else:
                    continue  # 정상 종료 후 즉시 다음 iteration (no backoff)

                # backoff sleep — stop_event 즉시 반응.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=backoff,
                    )
                    break  # stop_event 도착
                except asyncio.TimeoutError:
                    pass  # backoff 완료, 다음 iteration
        finally:
            # PR 2e — summary_task cancel/await (reconnect loop 예외와 독립 try/except).
            # Coinone PR 2d / Korbit/Bithumb start() finally cancel 패턴 mirror.
            summary_task.cancel()
            try:
                await summary_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[usdt_ws.gopax] summary_task cleanup 실패")
            self._running = False
            logger.info(
                "[usdt_ws.gopax] start exited (reconnect_attempts=%d)",
                self._reconnect_attempt_count,
            )

    async def _summary_log_loop(self) -> None:
        """PR 2e — start lifetime 동안 60s cycle metric INFO emit (Gopax 10 fields).

        Coinone `_summary_log_loop` 1:1 mirror (Gopax는 Coinone형 2-signal status +
        2 counter 조합이라 10 fields union).

        Emit metric (10개, state 8 + counter 2):
            - frames_per_min: `_liveness.frame_count_total` 차이 / elapsed
            - last_tick_age: `now - _liveness.last_tick_at` (없으면 -1.0 sentinel)
            - last_heartbeat_age: `now - _liveness.last_heartbeat_at` (없으면 -1.0 sentinel)
            - max_frame_gap: `_liveness.max_frame_gap_sec`
            - connection_status: `_connection_status` (normal/reconnecting/stale, Coinone 동일)
            - ticker_freshness_status: `_ticker_freshness_status` (normal/warning/degraded, Coinone 동일)
            - reconnect_attempts: `_reconnect_attempt_count` (instance lifetime)
            - status_transitions: `_status_transition_count` 6 keys
              (connection 3 + ticker 3, Coinone 동일)
            - redis_saturation_count: `_redis_writer.saturation_count` (G5 land)
            - fallback_probe_scheduled_count: `_fallback_controller.scheduled_probe_count` (G6b land)

        Sentinel 정책 (Coinone/Korbit/Upbit/Bithumb 동일):
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
                "[usdt_ws.gopax] metrics frames_per_min=%d "
                "last_tick_age=%.1f last_heartbeat_age=%.1f "
                "max_frame_gap=%.1f "
                "connection_status=%s ticker_freshness_status=%s "
                "reconnect_attempts=%d "
                "status_transitions=%s "
                "redis_saturation_count=%d "
                "fallback_probe_scheduled_count=%d",
                frames_per_min,
                last_tick_age, last_heartbeat_age,
                self._liveness.max_frame_gap_sec,
                self._connection_status, self._ticker_freshness_status,
                self._reconnect_attempt_count,
                transitions_str,
                self._redis_writer.saturation_count,
                self._fallback_controller.scheduled_probe_count,
            )

    async def stop(self) -> None:
        """stop signal — recv loop / start wait 모두 풀어줌. idempotent."""
        self._stop_event.set()
