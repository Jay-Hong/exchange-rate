"""USDT WebSocket — Korbit canary client (Phase B.5 Stage K2-K6b).

USDT_WS_DESIGN_PLAN §12.7 Phase B.5.
Coinone Stage C2-C6b + Bithumb Stage U4-U6 패턴 mirror.
K6는 K6a (DbWriter) + K6b (REST helper + RestFallbackController +
ticker_freshness degraded threshold + fallback hook)로 분할.

K2 lifecycle skeleton (완료):
    - `_stop_event` + `_running`
    - flag=false 시 scheduler가 KorbitWsClient 자체를 생성 안 함 (acceptance)

K3 connect/subscribe/parse + log only (완료):
    - Constants / `_build_subscribe_payload` (list-wrap) / `_parse_ticker_message`
      (close + lastTradedAt fallback) / `_handle_message` (unified status 분기) /
      `_run_one_session` (single session) / `start` (no reconnect)

K4 liveness + ws.ping() heartbeat + 2 status 분리 + reconnect loop (완료):
    - `UsdtLivenessMonitor` source-neutral 재사용 (Upbit/Bithumb/Coinone와 동일)
    - **`_ping_loop` — Bithumb explicit ping mirror**: `ws.ping()` + `wait_for(pong_waiter, timeout)` +
      성공 시 `_liveness.observe_heartbeat`. timeout/closed 시 ws.close() → recv loop가
      ConnectionClosed → start reconnect loop.
    - **2 status 차원 분리 (Coinone C4 mirror)**: connection / ticker_freshness 분리
    - **2-signal invariant**: ticker silence + heartbeat fresh → reconnect/stale 안 됨
    - **Reconnect loop (Bithumb start mirror)**: backoff sequence `[1,2,4,8,16,30]` Upbit/KRX 통일

K5 Redis writer + topic trigger (완료):
    - Constants: MAX_PENDING_WRITES=20 / REDIS_CLOSE_TIMEOUT_SEC=1.0 (Coinone/Bithumb 동일)
    - `KorbitRedisWriter`: tick-level fire-and-forget (Coinone C5 1:1 mirror, logger prefix만 변경).
      schedule → asyncio.create_task → _write_async (write_lock 직렬화 + to_thread + helper False
      격리 + success 시 lock 밖에서 tether_topic_trigger).
    - `_handle_message`의 valid ticker tick → `_run_one_session`에서 observe_tick → ticker freshness
      normal 복귀 → `_redis_writer.schedule(tick)` (recv loop 격리).
    - timestamp_ms (raw int) → KST ISO 변환 (`datetime.fromtimestamp(ms/1000, tz=_KST).isoformat()`)
    - close: pending drain + timeout 후 cancel (마지막 tick 보존).
    - finally 순서: ping_task cancel/await → `_redis_writer.close()` (Bithumb/Coinone 패턴 mirror).

K6a DB writer + 1s window debounce (완료):
    - Constants: DB_WRITE_WINDOW_SEC=1.0 (Coinone/Bithumb 동일)
    - `KorbitDbWriter`: Coinone C6a 1:1 mirror (logger prefix만 변경).
      schedule → window timer → _flush_after_window → asyncio.to_thread(_sync_db_write) →
      crud.insert_source_rate_if_changed. Race 방지: write 중 새 tick 도착 시 finally에서
      새 timer 예약.
    - `_handle_message`의 valid ticker tick → `_run_one_session`에서 observe_tick →
      ticker freshness normal 복귀 → `_redis_writer.schedule` → `_db_writer.schedule`.
    - close: timer cancel + pending tick 즉시 flush (window 일관성보다 last tick 우선).
    - finally 순서: ping_task → `_db_writer.close()` → `_redis_writer.close()`.

K6b REST fallback + degraded transition + fallback hook (현재):
    - Constants 3개: TICKER_FRESHNESS_DEGRADED_SEC=120.0 / FALLBACK_COOLDOWN_SEC=120.0 /
      FALLBACK_PROBE_TIMEOUT_SEC=10.0. 모두 provisional — K-2 30분 활발 시간대 sample base
      (sparse-time guarantee X). canary 자연 누적 후 조정.
    - `fetch_korbit_usdt_tick()` normalized helper (usdt_sources.py) — contract
      `{source, asset, rate, timestamp_ms}` Coinone/Bithumb과 동일, 내부 timestamp source는
      `data[0].lastTradedAt` (Coinone top-level `timestamp` 대신, K-1 audit + K-2 검증).
    - `KorbitRestFallbackController`: Coinone C6b 1:1 mirror (logger prefix만 변경).
      schedule_probe (sync, in-flight/cooldown/no-loop skip) → _run_probe → REST fetch
      (to_thread + timeout) → fanout (Redis + DB; Alert는 K7).
    - 3-level ticker freshness status + hook: normal | warning (30s+, log only) |
      degraded (120s+, REST probe trigger). `_set_ticker_freshness_status` hook —
      degraded → schedule_probe(reason="ticker_degraded"), normal 복귀 → reset_cooldown.
    - `_run_one_session` 2-iteration degraded transition: 1st iter normal→warning,
      2nd iter warning→degraded → schedule_probe.
    - finally close order: ping_task → fallback → DB → Redis (Coinone C6b mirror).

K7 이후 누적 예정 (USDT_WS_DESIGN_PLAN §12.7.4):
    - K7: UsdtAlertEvaluator wiring (`AlertObservation` schedule on valid tick + REST probe success).
      finally close order 확장: fallback → DB → Alert → Redis (4-step).
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

from app.crawlers.usdt_ws.upbit import UsdtLivenessMonitor
# K5 Redis writer + topic trigger imports (Coinone C5 패턴 mirror).
from app import latest_rates_cache, tether_topic_trigger
from app.tether_topic_trigger import (
    TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
)

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.korbit")

# K5 timestamp_ms → KST ISO 변환용 (Coinone _KST mirror).
_KST = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────────────────────────────────
# Constants — USDT_EXCHANGE_WEBSOCKET_GUIDE §6 + USDT_WS_DESIGN_PLAN §12.7.3
# ─────────────────────────────────────────────────────────────────────────

KORBIT_WS_URL = "wss://ws-api.korbit.co.kr/v2/public"
# Korbit symbol notation — 소문자 underscore (WS/REST 일관). guide §6 참조.
KORBIT_SYMBOL = "usdt_krw"
# Subscribe payload requestId — ACK frame echo로 추적 가능. K-2 smoke 검증: 1 OK.
SUBSCRIBE_REQUEST_ID = 1

# K3 recv loop timeout — stop_event 즉시 반응 + idle 허용 (Coinone/Bithumb 동일).
RECV_TIMEOUT_SEC = 1.0

# K4 Connection liveness — Bithumb explicit ping pattern + Coinone 통일 default.
# DATA frame이 primary activity (K-2 smoke ~10s 간격 publish 관찰), ws.ping()은
# silence/backstop heartbeat. last_activity_at = max(tick, heartbeat)로 ticker가
# 계속 오면 heartbeat 드물어도 stale 아님.
PING_INTERVAL_SEC = 300.0
# K-2 smoke PONG latency 13-15ms 대비 ~300배 margin (보수적).
PING_TIMEOUT_SEC = 5.0
# is_stale threshold — PING_INTERVAL + 60s buffer.
STALE_AFTER_SEC = 360.0

# K4 Ticker freshness telemetry — K-2 base 10s × 3 = 30s warning (log only).
# Coinone (60s/300s)와 다른 absolute value — K-2 publish interval base가 다름
# (Coinone median 1.4s vs Korbit ~10s constant 관찰). degraded transition은 K6 영역.
TICKER_FRESHNESS_WARNING_SEC = 30.0

# K4 Reconnect backoff sequence — Upbit/KRX/Bithumb 통일 (USDT_WS_DESIGN_PLAN §5).
RECONNECT_BACKOFF_SEQ = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RECONNECT_BACKOFF_TAIL = 30.0

# K5 Redis writer guards — Coinone/Bithumb 동일 값.
MAX_PENDING_WRITES = 20         # task 폭증 안전망 (Redis 장애 시)
REDIS_CLOSE_TIMEOUT_SEC = 1.0   # close() drain timeout — 후 강제 cancel

# K6a DB writer — Coinone/Bithumb 동일 값 (contract 통일).
# Korbit K-2 관찰은 ~10s publish이므로 매 tick INSERT 부담은 낮으나,
# Bithumb/Coinone과 동일 1s window debounce 유지 (writer contract 통일).
DB_WRITE_WINDOW_SEC = 1.0

# K6b ticker freshness degraded threshold (provisional) — K-2 base 10s × 12.
# K-2는 30분 활발 시간대 sample이라 sparse-time guarantee X (Codex 강조).
# canary 자연 누적 후 조정 — 단일 상수 격리.
TICKER_FRESHNESS_DEGRADED_SEC = 120.0

# K6b REST fallback cooldown — degraded threshold와 동일 (Coinone 패턴).
FALLBACK_COOLDOWN_SEC = 120.0

# K6b REST probe timeout — Coinone 동일.
FALLBACK_PROBE_TIMEOUT_SEC = 10.0


class KorbitRedisWriter:
    """Korbit USDT/KRW Redis latest writer — tick-level, fire-and-forget (K5).

    Coinone `CoinoneRedisWriter` 1:1 mirror. logger prefix만 korbit.
    source는 tick["source"]에서 "korbit" 자동 (KRX 학습: stage 추가 시 큰
    추상화 금지. generic UsdtRedisWriter 추상화는 거래소 5개 land 후 별도 시점).

    K5 핵심 (Coinone C5 mirror):
        - 매 valid tick → `set_latest_usdt_rate_from_sync_job` 호출 (debounce 없음 —
          DB writer가 K6에서 1s window debounce 담당, Redis는 tick-level latest 보존)
        - schedule()은 background task 생성 후 즉시 반환 (recv loop 격리)
        - asyncio.to_thread로 sync helper 호출 (event loop non-blocking)
        - MAX_PENDING_WRITES 한도로 Redis 장애 시 task 폭증 차단
        - close() drain-first, timeout 후 cancel (마지막 tick 보존)
        - Redis write 성공 시에만 tether topic trigger (lock 밖에서 호출)
        - reason은 source-neutral `TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS` 재사용

    Failure isolation:
        - helper False 반환 / exception → log only, WS session 영향 X
        - trigger 예외 → log only, writer/WS session 영향 X
        - last-write-wins (helper 기존 정책 유지)

    timestamp_ms → KST ISO 변환:
        - tick["timestamp_ms"] (raw int ms) → `datetime.fromtimestamp(ms/1000, tz=_KST).isoformat()`
        - K3에서 raw 유지 결정 (KST/UTC 변환은 sink 별 정책 — Redis는 KST ISO, DB는 K6 결정)
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        # write order 직렬화: 병렬 to_thread + connection pool 조합으로
        # old tick이 new tick을 덮는 race 차단. lock은 to_thread 완료까지 보유
        # — schedule 순서 = Redis SET 순서. (Coinone C5 mirror)
        self._write_lock: asyncio.Lock = asyncio.Lock()

    def schedule(self, tick: dict) -> None:
        """tick → background Redis write task. fire-and-forget.

        Saturation 시 (`len(_tasks) >= MAX_PENDING_WRITES`) skip + warning.
        Redis 장애로 task가 누적되는 시나리오 차단.
        """
        if len(self._tasks) >= MAX_PENDING_WRITES:
            logger.warning(
                "[usdt_ws.korbit] Redis write queue saturated (%d in-flight) — skip tick",
                len(self._tasks),
            )
            return
        task = asyncio.create_task(self._write_async(tick))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write_async(self, tick: dict) -> None:
        """sync Redis helper를 to_thread로 호출. 실패는 격리.

        `_write_lock`으로 직렬화 — 동시 to_thread + connection pool 조합에서
        발생할 수 있는 SET 순서 역전 차단.

        Redis write 성공 시 lock 밖에서 tether topic trigger 호출.
        trigger 예외는 writer/WS session에 전파하지 않음.
        """
        success = False
        source = tick["source"]
        asset = tick["asset"]
        async with self._write_lock:
            # K5 timestamp_ms → KST ISO (Coinone C5 mirror).
            ts_iso = datetime.fromtimestamp(
                tick["timestamp_ms"] / 1000, tz=_KST,
            ).isoformat()
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
                        "[usdt_ws.korbit] Redis write returned False "
                        "(helper 내부 log 참조, WS session 유지)"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[usdt_ws.korbit] Redis write task crashed (격리, WS session 유지)"
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
                    "[usdt_ws.korbit] tether topic trigger 호출 실패 (격리, WS session 유지)"
                )

    async def close(self, timeout: float = REDIS_CLOSE_TIMEOUT_SEC) -> None:
        """pending writes drain — session/shutdown 시 마지막 tick latest 보존.

        timeout (default 1s) 안에 drain 안 되면 강제 cancel. Redis hung 시 무한
        대기 방지. finally에서 `_tasks.clear()` 보장.
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
                "[usdt_ws.korbit] Redis writer close timeout (%ds) — cancel %d pending tasks",
                timeout, len(self._tasks),
            )
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            self._tasks.clear()


class KorbitDbWriter:
    """Korbit USDT/KRW DB writer — 1s window debounce + insert_source_rate_if_changed (K6a).

    Coinone `CoinoneDbWriter` 1:1 mirror. tick rate ~10s vs DB I/O ~10-50ms이라
    매 tick INSERT 부담은 낮으나 contract 통일 위해 1s window debounce 적용.

    asyncio loop 차단 방지:
        sync SQLAlchemy 호출은 asyncio.to_thread로 격리. DB session은
        to_thread 내부에서 get_db_context()로 생성/close.

    race 방지 (KRX PR6b-2b Codex 보정 + Coinone C6a mirror):
        DB write (to_thread) 진행 중 새 tick 도착 → _pending_tick 갱신,
        _timer.done() X 라 schedule()이 새 timer 안 만듦. write 종료 후
        finally 블록에서 _pending_tick 재확인 후 새 timer 예약 — 누락 방지.

    failure isolation: DB exception → log only, WS session 영향 X.

    shutdown (close): pending tick 1초 기다리지 않고 즉시 flush.
        window 일관성보다 last tick 보장 우선 (shutdown은 드문 이벤트).
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
                "[usdt_ws.korbit] DB write failed (격리): %s: %s",
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
        내부에서 commit. Decimal 정규화 없음 (REST polling과 일관, float 그대로 전달).
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
                logger.exception("[usdt_ws.korbit] db_writer timer cancel 실패")
        self._timer = None
        tick = self._pending_tick
        self._pending_tick = None
        if tick is None:
            return
        try:
            await asyncio.to_thread(self._sync_db_write, tick)
        except Exception as exc:
            logger.warning(
                "[usdt_ws.korbit] DB write on close failed (격리): %s: %s",
                type(exc).__name__, exc,
            )


class KorbitRestFallbackController:
    """Korbit REST fallback (silent probe) — K6b.

    Coinone `CoinoneRestFallbackController` (coinone.py:412) 1:1 mirror.
    K7에서 alert evaluator wiring 추가 예정 — 현재는 Redis + DB fanout만.

    DATA frame age > TICKER_FRESHNESS_DEGRADED_SEC (120s provisional) 감지 시 REST 1회
    probe → normalized tick → 기존 fanout (Redis + DB) 재사용. WS reconnect loop는
    그대로 유지, fallback은 freshness 보조.

    Guardrails (Coinone C6b mirror):
        - schedule_probe()는 sync/non-blocking (_set_ticker_freshness_status 동기 흐름에서 호출).
        - In-flight skip: probe 진행 중 중복 trigger 차단.
        - Cooldown skip: 마지막 probe 종료 후 FALLBACK_COOLDOWN_SEC 미경과 시 skip
          (degraded 지속 중 폭주 방지).
        - Probe 실패도 cooldown 적용 (REST rate limit 보호).
        - reset_cooldown(): normal 복귀 시 호출 → 다음 degraded 즉시 1회 probe 보장.
        - REST 실패 격리: log only, WS session/reconnect 영향 X.
        - fallback tick의 source/asset = "korbit"/"usdt-krw" (downstream 일관).
        - K7에서 alert wiring 추가 예정 (현재는 Redis + DB만 schedule).
    """

    def __init__(
        self,
        redis_writer: "KorbitRedisWriter",
        db_writer: "KorbitDbWriter",
        *,
        cooldown_sec: float = FALLBACK_COOLDOWN_SEC,
        probe_timeout_sec: float = FALLBACK_PROBE_TIMEOUT_SEC,
    ) -> None:
        self._redis_writer = redis_writer
        self._db_writer = db_writer
        # alert_evaluator는 K7에서 inject 예정.
        self._cooldown_sec = cooldown_sec
        self._probe_timeout_sec = probe_timeout_sec
        self._in_flight: bool = False
        self._cooldown_until: float = 0.0
        self._pending_task: Optional[asyncio.Task] = None

    def schedule_probe(self, reason: str) -> None:
        """sync: in-flight/cooldown 체크 후 background probe task 생성. 즉시 반환.

        `_set_ticker_freshness_status("degraded")` 안에서 호출 — 동기 흐름이라 즉시 반환 필수.
        Production은 항상 asyncio context 안 (recv loop / reconnect loop), 단
        sync test에서 _set_ticker_freshness_status 직접 호출 시 no-loop. defensive하게 skip.
        """
        now = time.time()
        if self._in_flight:
            logger.debug(
                "[usdt_ws.korbit.fallback] probe in-flight, skip (reason=%s)", reason,
            )
            return
        if now < self._cooldown_until:
            remaining = self._cooldown_until - now
            logger.debug(
                "[usdt_ws.korbit.fallback] cooldown active, skip (%.1fs remaining, reason=%s)",
                remaining, reason,
            )
            return
        # Loop 체크를 coroutine 생성 전에 — no-loop 시 coroutine 미생성으로
        # "coroutine was never awaited" warning 회피.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[usdt_ws.korbit.fallback] no event loop, skip probe (reason=%s)", reason,
            )
            return
        self._pending_task = loop.create_task(self._run_probe(reason))
        self._in_flight = True

    def reset_cooldown(self) -> None:
        """normal 복귀 시 호출 — cooldown clear. 다음 degraded 즉시 1회 probe 보장."""
        self._cooldown_until = 0.0
        logger.debug("[usdt_ws.korbit.fallback] cooldown reset on normal recovery")

    async def _run_probe(self, reason: str) -> None:
        """REST probe → normalized tick → fanout (Redis + DB; Alert는 K7).

        실패 시 log only + cooldown 적용 (재시도 X). WS session 영향 X.
        K7: probe success 시 AlertObservation(kind="rest_probe") schedule 추가 예정.
        """
        try:
            logger.info(
                "[usdt_ws.korbit.fallback] probe start (reason=%s)", reason,
            )
            tick = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_korbit_tick),
                timeout=self._probe_timeout_sec,
            )
            if tick is None:
                logger.warning(
                    "[usdt_ws.korbit.fallback] probe returned None — REST/parse 실패 or invalid rate",
                )
                return

            # 기존 fanout 재사용 (K5 Redis writer + K6a DB writer). K7에서 Alert 추가 예정.
            self._redis_writer.schedule(tick)
            self._db_writer.schedule(tick)
            logger.info(
                "[usdt_ws.korbit.fallback] probe success (rate=%s, ts_ms=%d, reason=%s)",
                tick["rate"], tick["timestamp_ms"], reason,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "[usdt_ws.korbit.fallback] probe timeout (%.1fs, reason=%s)",
                self._probe_timeout_sec, reason,
            )
        except Exception:
            logger.exception(
                "[usdt_ws.korbit.fallback] probe error (격리, reason=%s)", reason,
            )
        finally:
            # 실패해도 cooldown 적용 (REST rate limit 보호).
            self._cooldown_until = time.time() + self._cooldown_sec
            self._in_flight = False

    @staticmethod
    def _fetch_korbit_tick() -> Optional[dict]:
        """sync REST fetch — to_thread 내부 실행. 모듈 내부 import (순환 회피)."""
        from app.crawlers.usdt_sources import fetch_korbit_usdt_tick
        return fetch_korbit_usdt_tick()

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
                logger.exception("[usdt_ws.korbit.fallback] pending task cleanup 실패")
        self._pending_task = None


class KorbitWsClient:
    """Korbit USDT/KRW WebSocket client — K2 skeleton + K3 parse + K4 liveness/reconnect.

    State:
        - `_stop_event`: stop signal (asyncio.Event)
        - `_running`: 중복 start 방지 flag
        - `_ws`: active WebSocketClientProtocol (session 안에서만)
        - `_first_tick_logged`: first ticker INFO log 1회 emit 후 DEBUG 격하
        - `_liveness`: UsdtLivenessMonitor (source-neutral 재사용)
        - `_connection_status`: normal | reconnecting | stale (PING/PONG + activity 기준)
        - `_ticker_freshness_status`: normal | warning (ticker age 기반, log only)
        - `_reconnect_attempt_count`: cumulative reconnect attempts (log/metric)
        - `_status_transition_count`: status 전이 횟수 카운터 (log/metric)

    K5 이후 누적 예정 (Redis/DB/Alert/REST state — Coinone `CoinoneWsClient` 패턴).
    """

    def __init__(self) -> None:
        # K2 lifecycle
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        # K3 session state
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False
        # K4 liveness + 2 status
        self._liveness: UsdtLivenessMonitor = UsdtLivenessMonitor()
        self._connection_status: str = "normal"
        self._ticker_freshness_status: str = "normal"
        self._reconnect_attempt_count: int = 0
        self._status_transition_count: dict[str, int] = {
            "connection_normal": 0,
            "connection_reconnecting": 0,
            "connection_stale": 0,
            "ticker_normal": 0,
            "ticker_warning": 0,
            "ticker_degraded": 0,  # K6b 3-level extension
        }
        # K5 Redis writer (tick-level, fire-and-forget) — A13 lifecycle invariant:
        # __init__에서 1회 생성, reconnect 사이 재사용 (재할당 없음). Coinone C5 mirror.
        self._redis_writer: KorbitRedisWriter = KorbitRedisWriter()
        # K6a DB writer (1s window debounce). Coinone C6a mirror.
        self._db_writer: KorbitDbWriter = KorbitDbWriter()
        # K6b REST fallback controller (degraded threshold trigger). Coinone C6b mirror.
        # alert_evaluator inject은 K7. 현재는 Redis + DB fanout만.
        self._fallback_controller: KorbitRestFallbackController = KorbitRestFallbackController(
            redis_writer=self._redis_writer,
            db_writer=self._db_writer,
        )

    @staticmethod
    def _build_subscribe_payload() -> list:
        """Korbit subscribe payload — list-wrap form (Coinone single-dict와 다름).

        USDT_EXCHANGE_WEBSOCKET_GUIDE §6: list 안에 단일 subscribe dict.
        K-2 smoke 검증: 30분 valid run에서 ACK + 213 ticker frame 정상 수신.
        """
        return [
            {
                "requestId": SUBSCRIBE_REQUEST_ID,
                "method": "subscribe",
                "type": "ticker",
                "symbols": [KORBIT_SYMBOL],
            }
        ]

    def _parse_ticker_message(self, message: dict) -> Optional[dict]:
        """Korbit ticker frame → normalized tick dict, None on invalid/non-ticker.

        Normalized shape (Upbit/Bithumb/Coinone `fetch_*_usdt_tick` contract 일치):
            {"source": "korbit", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}

        timestamp_ms는 raw int 유지 (KST/UTC 변환은 K6 DB writer 영역).
        timestamp source: `data.lastTradedAt` 우선, 부재 시 top-level `timestamp` fallback
        (USDT_WS_DESIGN_PLAN §12.7.3 결정 5 — sparse-time 보수적 보존).

        Guard:
            - type != "ticker" → None
            - symbol != "usdt_krw" → None
            - data dict 부재 → None
            - close 부재 / parse 실패 / <= 0 → None
            - lastTradedAt + top-level timestamp 둘 다 부재 / parse 실패 → None

        Snapshot 처리 (K-2 smoke 검증):
            - first frame에 `snapshot: true` 1회, 이후 ticker는 key 자체 누락
            - 가격값은 동일 추출 가능 → snapshot 필드 무시
        """
        if message.get("type") != "ticker":
            return None
        if message.get("symbol") != KORBIT_SYMBOL:
            return None
        data = message.get("data")
        if not isinstance(data, dict):
            return None

        close_raw = data.get("close")
        if close_raw is None:
            return None
        try:
            rate = float(close_raw)
        except (TypeError, ValueError):
            return None
        if rate <= 0:
            return None

        # timestamp: data.lastTradedAt 우선, 부재 시 top-level timestamp fallback
        ts_raw = data.get("lastTradedAt")
        if ts_raw is None:
            ts_raw = message.get("timestamp")
        if ts_raw is None:
            return None
        try:
            ts_ms = int(ts_raw)
        except (TypeError, ValueError):
            return None

        return {
            "source": "korbit",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }

    def _handle_message(self, raw) -> Optional[dict]:
        """Korbit frame → 분기 3+1 + log only. ticker tick dict 반환, 그 외 None.

        Frame types (USDT_EXCHANGE_WEBSOCKET_GUIDE §6 + K-2 smoke 실측):
            - ACK/ERROR (unified `status` 키): `{"status":"success"|"fail", "requestId":...}`
              — subscribe ACK 1회 (status=success) + error 시 status=fail
            - ticker (`type=="ticker"`): `_parse_ticker_message`로 normalize.
              first tick INFO log + 이후 DEBUG (Coinone 패턴 mirror).
            - unknown / missing: WARNING (safe log).

        Acceptance (K3):
            - return: ticker만 tick dict / 그 외 None (K5~K7 callsite 재사용 위해 유지)
            - downstream IO 0 (Redis/DB/alert/REST 미호출 — K5~K7 영역)
        """
        # raw decode (bytes/str/dict)
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("[usdt_ws.korbit] non-utf8 frame, skip")
                return None
        if isinstance(raw, str):
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("[usdt_ws.korbit] invalid JSON frame, skip")
                return None
        elif isinstance(raw, dict):
            message = raw
        else:
            logger.warning("[usdt_ws.korbit] unexpected raw type=%s, skip", type(raw).__name__)
            return None

        if not isinstance(message, dict):
            logger.warning("[usdt_ws.korbit] non-dict message, skip")
            return None

        # Unified ACK/ERROR via `status` 분기 (K-2 smoke 검증, USDT_WS_DESIGN_PLAN §12.7.3 결정 2)
        if "status" in message:
            status = message.get("status")
            if status == "success":
                logger.info(
                    "[usdt_ws.korbit] subscribe ACK requestId=%s",
                    message.get("requestId"),
                )
            elif status == "fail":
                logger.warning(
                    "[usdt_ws.korbit] ERROR code=%s message=%s requestId=%s",
                    message.get("code"),
                    message.get("message"),
                    message.get("requestId"),
                )
            else:
                logger.warning(
                    "[usdt_ws.korbit] unknown status=%r, skip",
                    status,
                )
            return None

        if message.get("type") == "ticker":
            tick = self._parse_ticker_message(message)
            if tick is None:
                logger.warning("[usdt_ws.korbit] ticker parse failed, skip")
                return None
            if not self._first_tick_logged:
                logger.info("[usdt_ws.korbit] first tick", extra=tick)
                self._first_tick_logged = True
            else:
                logger.debug("[usdt_ws.korbit] tick", extra=tick)
            return tick

        # unknown / missing type — safe log (Coinone 패턴 mirror)
        logger.warning(
            "[usdt_ws.korbit] unknown frame type=%r keys=%s, skip",
            message.get("type"),
            list(message.keys()),
        )
        return None

    def _set_connection_status(self, new_status: str) -> None:
        """K4 connection 차원 status 전이 + counter + log (Coinone C4 mirror, 2-signal 분리).

        new_status: normal | reconnecting | stale.
        K4 scope: status/log/counter only. fallback hook 없음 (K6 영역).
        """
        if self._connection_status == new_status:
            return
        prev = self._connection_status
        self._connection_status = new_status
        key = f"connection_{new_status}"
        if key in self._status_transition_count:
            self._status_transition_count[key] += 1
        logger.info(
            "[usdt_ws.korbit] connection_status %s → %s (reconnect_attempt=%d max_gap=%.2fs)",
            prev, new_status, self._reconnect_attempt_count,
            self._liveness.max_frame_gap_sec,
        )

    def _set_ticker_freshness_status(self, new_status: str) -> None:
        """K4/K6b ticker freshness 차원 status 전이 + counter + log + K6b fallback hook.

        Transition-based — 매 loop마다 warning 안 찍히도록 상태 변화 시점에만 1회 log.
        new_status: normal | warning | degraded (3-level, K6b 확장).

        K6b fallback hook (Coinone C6b mirror):
            - degraded 진입 시 fallback_controller.schedule_probe(reason="ticker_degraded")
            - normal 복귀 시 fallback_controller.reset_cooldown()
        """
        if self._ticker_freshness_status == new_status:
            return
        prev = self._ticker_freshness_status
        self._ticker_freshness_status = new_status
        key = f"ticker_{new_status}"
        if key in self._status_transition_count:
            self._status_transition_count[key] += 1
        logger.info(
            "[usdt_ws.korbit] ticker_freshness_status %s → %s "
            "(last_tick_at=%s max_gap=%.2fs)",
            prev, new_status, self._liveness.last_tick_at,
            self._liveness.max_frame_gap_sec,
        )
        # K6b fallback hook (Coinone C6b mirror)
        if new_status == "degraded":
            self._fallback_controller.schedule_probe(reason="ticker_degraded")
        elif new_status == "normal":
            # warning → normal 또는 degraded → normal 모두 reset_cooldown 호출.
            # 새 outage cycle의 첫 degraded probe가 옛 cooldown으로 skip되지 않도록.
            self._fallback_controller.reset_cooldown()

    @staticmethod
    def _compute_backoff(attempt: int) -> float:
        """Reconnect backoff — Upbit/KRX/Bithumb 통일 sequence."""
        if attempt <= 0:
            return RECONNECT_BACKOFF_SEQ[0]
        if attempt <= len(RECONNECT_BACKOFF_SEQ):
            return RECONNECT_BACKOFF_SEQ[attempt - 1]
        return RECONNECT_BACKOFF_TAIL

    async def _ping_loop(self, ws) -> None:
        """K4 주기적 ws.ping() → pong 성공 시 heartbeat observation (Bithumb 패턴 mirror).

        실패/timeout 시 ws.close() → recv loop가 ConnectionClosed → start reconnect 트리거.
        ping_loop 자체는 silently return.

        Acceptance:
            - 성공 시 `_liveness.observe_heartbeat(now)` 호출 (status 직접 변경 X)
            - timeout/closed 시 ws.close() + return (recv loop에 위임)
            - CancelledError는 quietly return (finally의 ping_task cancel/await에서 처리)
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
                    "[usdt_ws.korbit] ping failed: %s — closing ws for reconnect",
                    type(exc).__name__,
                )
                try:
                    await ws.close()
                except Exception:
                    pass
                return

    async def _run_one_session(self) -> None:
        """K4 단일 connect → subscribe → recv loop + ping_loop background.

        §6 미명시 heartbeat — K-2 smoke 검증으로 explicit `_ping_loop` 채택 (Bithumb mirror).
        Coinone application-level PING 회피.

        Acceptance:
            - websockets.connect(URL, ping_interval=None) — auto-ping 비활성, explicit _ping_loop 사용
            - liveness reset_active_session() 호출 (active session metric 초기화)
            - ping_task background — PING_INTERVAL_SEC=300 cycle
            - recv loop: RECV_TIMEOUT_SEC=1.0 (stop_event 즉시 반응)
            - is_stale 양방향 전이 (heartbeat fresh이면 ticker silence 무관 — Coinone C4 invariant)
            - ticker_update_age_sec > 30s → ticker_freshness_status normal → warning (1회 log, action X)
            - valid ticker → observe_tick(now) + warning → normal 복귀
            - finally: ping_task cancel/await (Acceptance #9)
            - ticker silence alone → reconnect never (핵심 invariant)
            - Redis/DB/Alert/REST writer 일체 미진입 (K5~K7 영역)
        """
        async with websockets.connect(
            KORBIT_WS_URL,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=10,
        ) as ws:
            self._ws = ws
            self._liveness.reset_active_session()
            logger.info("[usdt_ws.korbit] connected url=%s", KORBIT_WS_URL)
            payload = self._build_subscribe_payload()
            await ws.send(json.dumps(payload))
            logger.info(
                "[usdt_ws.korbit] subscribed symbol=%s requestId=%s",
                KORBIT_SYMBOL, SUBSCRIBE_REQUEST_ID,
            )
            self._set_connection_status("normal")
            self._set_ticker_freshness_status("normal")

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                while not self._stop_event.is_set():
                    now = time.time()
                    # Connection liveness — UsdtLivenessMonitor.is_stale은 heartbeat fresh이면
                    # ticker silence 무관 False (last_activity_at = max(tick, heartbeat)).
                    # ticker silence alone → reconnect never (Codex 강조 + Coinone C4 invariant).
                    if self._connection_status != "stale" and self._liveness.is_stale(now, STALE_AFTER_SEC):
                        self._set_connection_status("stale")
                    elif self._connection_status == "stale" and not self._liveness.is_stale(now, STALE_AFTER_SEC):
                        self._set_connection_status("normal")

                    # Ticker freshness telemetry (transition 기반, 3-level: normal/warning/degraded).
                    # K4: normal → warning (30s+, log only).
                    # K6b: warning → degraded (120s+, REST probe trigger via _set_*_status hook).
                    # last_tick_at 없으면 (첫 tick 전) freshness 평가 skip.
                    # 2-iteration 특성 (Codex Point 1): if/elif 구조라 normal → degraded 직행 불가.
                    last_tick = self._liveness.last_tick_at
                    if last_tick is not None:
                        ticker_update_age_sec = now - last_tick
                        if (
                            self._ticker_freshness_status == "normal"
                            and ticker_update_age_sec > TICKER_FRESHNESS_WARNING_SEC
                        ):
                            self._set_ticker_freshness_status("warning")
                        elif (
                            self._ticker_freshness_status == "warning"
                            and ticker_update_age_sec > TICKER_FRESHNESS_DEGRADED_SEC
                        ):
                            # K6b: degraded 진입 시 _set_*_status hook이 REST probe schedule
                            self._set_ticker_freshness_status("degraded")
                        # warning/degraded → normal 복귀는 valid ticker 수신 시점에 처리 (아래).

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                    except asyncio.TimeoutError:
                        continue
                    except ConnectionClosed:
                        if self._stop_event.is_set():
                            logger.info("[usdt_ws.korbit] connection closed after stop")
                            return
                        raise

                    # K4: valid ticker → observe_tick + freshness warning → normal 복귀.
                    # K5: Redis writer schedule (tick-level, fire-and-forget).
                    # K6a: DB writer schedule (1s window debounce).
                    # K6b~K7에서 REST fallback / Alert downstream IO 추가 예정.
                    tick = self._handle_message(raw)
                    if tick is not None:
                        self._liveness.observe_tick(time.time())
                        # warning/degraded → normal 복귀 (K4 + K6b — 상태 전이 1회 log + cooldown reset hook).
                        if self._ticker_freshness_status in ("warning", "degraded"):
                            self._set_ticker_freshness_status("normal")
                        # K5: Redis writer schedule (recv loop 격리 — fire-and-forget).
                        self._redis_writer.schedule(tick)
                        # K6a: DB writer schedule (1s window debounce + race-prevention).
                        self._db_writer.schedule(tick)
            finally:
                # Acceptance #9: ping_task cancel/await 보장.
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("[usdt_ws.korbit] ping_task cleanup 실패")
                # K6b close order (Coinone C6b mirror): ping_task → fallback → DB → Redis.
                # K7 누적 시: fallback → DB → Alert → Redis (Bithumb 최종 순서). Redis는 항상 마지막.
                try:
                    await self._fallback_controller.close()
                except Exception:
                    logger.exception("[usdt_ws.korbit] fallback_controller.close() 실패")
                try:
                    await self._db_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.korbit] db_writer.close() 실패")
                try:
                    await self._redis_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.korbit] redis_writer.close() 실패")

    async def start(self) -> None:
        """K4 — reconnect loop with backoff (Bithumb start mirror).

        Acceptance:
            - 중복 start 방지 (_running flag)
            - reconnect trigger 3가지: ping timeout / ConnectionClosed / session exception
            - backoff sequence `[1,2,4,8,16,30]` (Upbit/KRX/Bithumb 통일)
            - backoff sleep은 stop_event.wait() with timeout (stop_event 즉시 반응)
            - CancelledError는 raise (task cancel propagate)
            - is_stale → status/log/counter only (reconnect 유도 X — Codex 강조)
        """
        if self._running:
            logger.debug("[usdt_ws.korbit] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.korbit] start (K4 — reconnect loop with backoff)")
        attempt = 0
        try:
            while not self._stop_event.is_set():
                try:
                    await self._run_one_session()
                    # _run_one_session 정상 종료 (stop_event 또는 ConnectionClosed after stop)
                    if self._stop_event.is_set():
                        break
                    # stop_event 안 set인데 정상 종료 → idle reset 후 backoff sleep
                    attempt += 1
                    self._reconnect_attempt_count += 1
                    self._set_connection_status("reconnecting")
                    backoff = self._compute_backoff(attempt)
                    logger.warning(
                        "[usdt_ws.korbit] session ended unexpectedly (attempt %d) — backoff %.1fs",
                        attempt, backoff,
                    )
                except asyncio.CancelledError:
                    raise
                except ConnectionClosed as exc:
                    attempt += 1
                    self._reconnect_attempt_count += 1
                    self._set_connection_status("reconnecting")
                    backoff = self._compute_backoff(attempt)
                    logger.warning(
                        "[usdt_ws.korbit] connection closed (attempt %d): %s — backoff %.1fs",
                        attempt, exc, backoff,
                    )
                except Exception as exc:
                    attempt += 1
                    self._reconnect_attempt_count += 1
                    self._set_connection_status("reconnecting")
                    backoff = self._compute_backoff(attempt)
                    logger.warning(
                        "[usdt_ws.korbit] session error (attempt %d): %s: %s — backoff %.1fs",
                        attempt, type(exc).__name__, exc, backoff,
                    )

                # backoff sleep — stop_event 즉시 반응.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=backoff,
                    )
                    # stop_event set during backoff → break
                    break
                except asyncio.TimeoutError:
                    # backoff 소진 → 다음 attempt 진입
                    continue
        finally:
            self._running = False
            self._ws = None

    async def stop(self) -> None:
        """stop signal — `_run_one_session()`의 recv loop 및 start reconnect loop 모두 빠짐."""
        self._stop_event.set()
