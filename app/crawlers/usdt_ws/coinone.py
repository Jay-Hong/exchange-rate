"""USDT WebSocket — Coinone canary client (Phase B.4 Stage C2-C7 complete).

USDT_WS_DESIGN_PLAN §12.6 Phase B.4.
Bithumb Stage U2-U7 패턴 mirror (작은 단위 stage 분할 학습 — KRX close finalizer
1100 lines → 3 High findings → revert + Phase B.3 U2~U7 분할 재시작 학습 적용).
C6는 C6a (DbWriter, sparse-time smoke 무관) + C6b (REST helper + RestFallbackController +
ticker_freshness degraded threshold provisional 300s, sparse-time smoke 후 조정)로 분할 (Codex 권장).
C7으로 기능 구현은 complete — 남은 작업은 sparse-time smoke 기반 threshold 조정 + canary enable.

C2 lifecycle skeleton (완료):
    - `_stop_event` + `_running` (network/Redis/DB/Alert import 없음)
    - flag=false 시 scheduler가 CoinoneWsClient 자체를 생성 안 함 (acceptance)

C3 connect/subscribe/parse + log only (완료):
    - Constants: WS URL / quote/target currency / recv timeout
    - `_build_subscribe_payload`: Coinone single-dict form (별 protocol)
    - `_parse_data_message`: DATA response → normalized tick
    - `_handle_message`: response_type 분기 5+1 + DATA tick return
    - `_run_one_session`: connect + subscribe + recv loop
    - `start`: `_run_one_session` 단일 실행

C4 connection liveness + ticker freshness telemetry (완료):
    - Constants: PING_INTERVAL_SEC=300 / PING_TIMEOUT_SEC=5 / STALE_AFTER_SEC=360 (5분 cycle + 마진) /
      TICKER_FRESHNESS_WARNING_SEC=60 (telemetry only, action X — USDT_WS_DESIGN_PLAN §12.6.3 + Codex 정정)
    - Application-level PING/PONG event-based (Coinone 별 protocol — `{"request_type":"PING"}`).
      Bithumb의 WS protocol `ws.ping()`과 달리 PONG response가 `_handle_message`로 도착하므로
      `_pong_event` (asyncio.Event)으로 1:1 synchronization (clear → send → wait_for 순서).
    - `_connection_status`: normal | reconnecting | stale (connection 차원, Codex 안)
    - `_ticker_freshness_status`: normal | warning (ticker 차원, Codex 안 — `_status` 단일 통합 회피)
    - ticker freshness warning: 상태 전이 기반 1회 log + DATA 수신 시 normal 복귀 1회 log
      (RECV_TIMEOUT_SEC=1.0 × 60s+ silence = 분당 ~60 warning log 폭주 방지, Codex Point 1)
    - `UsdtLivenessMonitor` 재사용 (확장 없음) — last_activity_at = max(tick, heartbeat) 이미
      2-signal 의식. heartbeat fresh이면 ticker silence 무관 is_stale=False.
    - Reconnect loop + backoff sequence (Bithumb start() mirror)
    - **scope guard**: Redis/DB/Alert/REST writer 일체 미진입 (C5~C7 영역). ticker silence
      alone → reconnect never (사용자 일요일 Upbit 무체결 관찰 + USDT/KRW 시장에서 수분
      단위 무체결 정상 → false reconnect 위험 방지).

C5 Redis writer + topic trigger (완료):
    - Constants: MAX_PENDING_WRITES=20 / REDIS_CLOSE_TIMEOUT_SEC=1.0 (Bithumb 동일)
    - `CoinoneRedisWriter`: tick-level fire-and-forget (Bithumb U5 1:1 mirror, logger prefix만 변경).
      schedule → asyncio.create_task → _write_async (write_lock 직렬화 + to_thread + helper False
      격리 + success 시 lock 밖에서 tether_topic_trigger).
    - `_handle_message`의 valid DATA tick → `_run_one_session`에서 observe_tick → ticker freshness
      normal 복귀 → `_redis_writer.schedule(tick)` (recv loop 격리).
    - timestamp_ms (raw int) → KST ISO 변환 (`datetime.fromtimestamp(ms/1000, tz=_KST).isoformat()`)
    - close: pending drain + timeout 후 cancel (마지막 tick 보존).
    - finally 순서: ping_task cancel/await → `_redis_writer.close()` (Codex Point 1).

C6a DB writer + 1s window debounce (완료):
    - Constants: DB_WRITE_WINDOW_SEC=1.0 (Bithumb 동일)
    - `CoinoneDbWriter`: Bithumb U6 1:1 mirror (logger prefix만 변경).
      schedule → window timer → _flush_after_window → asyncio.to_thread(_sync_db_write) →
      crud.insert_source_rate_if_changed. Race 방지: write 중 새 tick 도착 시 finally에서
      새 timer 예약 (KRX PR6b-2b 패턴).
    - `_handle_message`의 valid DATA tick → `_run_one_session`에서 observe_tick →
      ticker freshness normal 복귀 → `_redis_writer.schedule` → `_db_writer.schedule`.
    - close: timer cancel + pending tick 즉시 flush (window 일관성보다 last tick 우선).
    - finally 순서: ping_task → `_db_writer.close()` → `_redis_writer.close()` (Bithumb mirror).

C6b REST fallback + degraded threshold + DEGRADED hook (완료):
    - Constants 3개: TICKER_FRESHNESS_DEGRADED_SEC=300.0 / FALLBACK_COOLDOWN_SEC=300.0 /
      FALLBACK_PROBE_TIMEOUT_SEC=10.0. 모두 provisional — sparse-time smoke 후 조정.
    - `fetch_coinone_usdt_tick()` normalized helper (usdt_sources.py) — Bithumb 패턴 mirror.
      기존 `_fetch_coinone()` rate-only는 새 helper 기반 wrapping.
    - `CoinoneRestFallbackController` — Bithumb U6 1:1 mirror. C7에서 alert evaluator wiring 추가 예정.
      schedule_probe (sync, in-flight/cooldown skip + loop.create_task) + _run_probe (REST fetch 10s
      timeout → fanout Redis + DB; Alert는 C7) + reset_cooldown + close (pending task cancel).
    - 3-level ticker freshness status: normal | warning (60s+, log only) | degraded (300s+, REST probe trigger).
    - `_set_ticker_freshness_status` hook: degraded 진입 → `_fallback_controller.schedule_probe`,
      normal 복귀 (warning/degraded 어느 곳에서든) → `_fallback_controller.reset_cooldown`.
    - finally 순서 (Bithumb U7 mirror): fallback → DB → (Alert C7) → Redis.
    - ticker silence alone → reconnect never (Codex 강조 유지). degraded는 REST probe만 trigger.

C7 UsdtAlertEvaluator wiring (현재 — Phase B.4 complete):
    - `UsdtAlertEvaluator` (source-neutral, Bithumb U7 mirror) — Coinone client 자체 instance 보유.
    - `CoinoneRestFallbackController.__init__`에 `alert_evaluator` 필수 inject 추가 (Codex Point 1).
    - `_run_one_session` valid DATA tick path → `_alert_evaluator.schedule(AlertObservation(kind="tick"))`.
    - `_run_probe` success → `_alert_evaluator.schedule(AlertObservation(kind="rest_probe"))`.
    - finally 순서 (Bithumb U7 mirror, 최종): fallback → DB → Alert → Redis (4개).
    - 각 close 개별 try/except 격리 (Codex Point 2) — 한 close 실패해도 뒤 close 실행 보장.
    - flag=false invariant: CoinoneWsClient 생성 0 → UsdtAlertEvaluator 생성 0.

Phase B.4 complete. 남은 작업:
    - sparse-time smoke (새벽/주말 30분) — TICKER_FRESHNESS_DEGRADED_SEC + FALLBACK_COOLDOWN_SEC
      provisional 300s 정확값 조정 (별 작은 commit).
    - Canary enable 판단: USDT_WS_COINONE_ENABLED=true 활성화 GO.
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

# UsdtLivenessMonitor 재사용 — source-neutral (last_activity_at = max(tick, heartbeat) 이미
# 2-signal 의식이라 Coinone에 그대로 적용 가능. C4 신규 monitor 신설 X).
from app.crawlers.usdt_ws.upbit import UsdtLivenessMonitor
# C5 Redis writer + topic trigger imports (Bithumb U5 패턴 mirror).
from app import latest_rates_cache, tether_topic_trigger
from app.tether_topic_trigger import (
    TETHER_TRIGGER_REASON_USDT_WS_REDIS_WRITE_SUCCESS,
)
# C7 Alert evaluator + observation — source-neutral (Bithumb U7 패턴 mirror, Phase B.1 PR6
# source-neutral 설계 그대로 적용).
from app.notifications.alert_evaluator import (
    AlertObservation,
    UsdtAlertEvaluator,
)

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.coinone")

# C5 timestamp_ms → KST ISO 변환용 (Bithumb _KST mirror).
_KST = timezone(timedelta(hours=9))


# ─────────────────────────────────────────────────────────────────────────
# Constants — USDT_EXCHANGE_WEBSOCKET_GUIDE §5 + USDT_WS_DESIGN_PLAN §12.6.3
# ─────────────────────────────────────────────────────────────────────────

COINONE_WS_URL = "wss://stream.coinone.co.kr"
COINONE_QUOTE_CURRENCY = "KRW"
COINONE_TARGET_CURRENCY = "USDT"

# C3 recv loop timeout — stop_event 즉시 반응 + idle 허용 (Bithumb 동일).
RECV_TIMEOUT_SEC = 1.0

# C4 application-level PING/PONG — Coinone 공식 idle 30분 방지 (USDT_EXCHANGE_WEBSOCKET_GUIDE §5).
# 5분 cycle은 안전 마진 6× (운영 정책, Codex 정정 — 공식 의무 아님).
PING_INTERVAL_SEC = 300.0
PING_TIMEOUT_SEC = 5.0  # PONG 대기 시간. C0 smoke 실측 13ms latency × ~400 안전 마진.

# C4 UsdtLivenessMonitor.is_stale threshold — PING_INTERVAL_SEC + 마진.
# 정상 5분 PING cycle 사이 false stale 전이 방지 (Codex 강조). 실제 reconnect는
# PONG timeout/ConnectionClosed가 주도하고, STALE_AFTER_SEC는 보조 안전망.
STALE_AFTER_SEC = 360.0

# C4 Ticker freshness warning threshold — telemetry only, action 미진입.
# USDT_WS_DESIGN_PLAN §12.6.3 결정 3 + Codex 정정: 60s = warning/log only (transition 기반).
# 실제 REST probe trigger threshold (180~300s degraded)는 C6 결정.
# 사용자 일요일 Upbit 무체결 관찰: USDT/KRW 시장에서 수분 단위 무체결 정상 → DATA silence를
# connection failure로 해석 시 false reconnect 위험. ticker silence alone → reconnect never.
TICKER_FRESHNESS_WARNING_SEC = 60.0

# C4 Reconnect backoff — Bithumb/Upbit 동일 sequence (USDT_WS_DESIGN_PLAN §10).
RECONNECT_BACKOFF_SEQ = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RECONNECT_BACKOFF_TAIL = 30.0

# C5 Redis writer guards — Bithumb U5 동일 값 (bithumb.py:145-147 mirror).
MAX_PENDING_WRITES = 20         # task 폭증 안전망 (Redis 장애 시)
REDIS_CLOSE_TIMEOUT_SEC = 1.0   # close() drain timeout — 후 강제 cancel

# C6a DB writer — Bithumb U6 동일 값 (bithumb.py:150 mirror).
# tick rate ~100ms+ vs DB I/O ~10-50ms이라 매 tick INSERT는 DB 부담.
# window 내 마지막 tick만 helper 호출.
DB_WRITE_WINDOW_SEC = 1.0

# C6b Ticker freshness degraded threshold — REST fallback probe trigger (action threshold).
# **provisional** — 사용자 일요일 Upbit 3분(180s) 무체결 실관찰 + 5분 안전 마진. sparse-time
# smoke (새벽/주말 30분) 후 정확값 확정 예정 (USDT_WS_DESIGN_PLAN §12.6.3 + Codex 정정).
# 300s = warning(60s)보다 5× 길게 잡아 false probe 폭주 회피.
TICKER_FRESHNESS_DEGRADED_SEC = 300.0

# C6b REST fallback controller — Bithumb U6 패턴 mirror, threshold만 provisional 다름.
# COOLDOWN_SEC = DEGRADED_SEC: probe 빈도 최대 5분 1회 (degraded transition 후 다음
# degraded까지 cooldown). STALE_AFTER_SEC=360 (connection liveness 보조)과 별개 의미 분리.
FALLBACK_COOLDOWN_SEC = 300.0           # provisional, sparse-time smoke 후 조정 가능
FALLBACK_PROBE_TIMEOUT_SEC = 10.0       # REST HTTP timeout (Bithumb 동일)


class CoinoneRedisWriter:
    """Coinone USDT/KRW Redis latest writer — tick-level, fire-and-forget (C5).

    Bithumb `BithumbRedisWriter` (bithumb.py:157-274) 1:1 mirror. logger prefix만
    coinone. source는 tick["source"]에서 "coinone" 자동 (KRX 학습: stage 추가 시 큰
    추상화 금지. generic UsdtRedisWriter 추상화는 거래소 3개 이상 누적 시점 별도 PR).

    C5 핵심 (Bithumb U5 mirror):
        - 매 valid tick → `set_latest_usdt_rate_from_sync_job` 호출 (debounce 없음 —
          DB writer가 C6에서 1s window debounce 담당, Redis는 tick-level latest 보존)
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
        - C3에서 raw 유지 결정 (KST/UTC 변환은 sink 별 정책 — Redis는 KST ISO, DB는 C6 결정)
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        # write order 직렬화: 병렬 to_thread + connection pool 조합으로
        # old tick이 new tick을 덮는 race 차단. lock은 to_thread 완료까지 보유
        # — schedule 순서 = Redis SET 순서. (Bithumb U5 mirror)
        self._write_lock: asyncio.Lock = asyncio.Lock()

    def schedule(self, tick: dict) -> None:
        """tick → background Redis write task. fire-and-forget.

        Saturation 시 (`len(_tasks) >= MAX_PENDING_WRITES`) skip + warning.
        Redis 장애로 task가 누적되는 시나리오 차단.
        """
        if len(self._tasks) >= MAX_PENDING_WRITES:
            logger.warning(
                "[usdt_ws.coinone] Redis write queue saturated (%d in-flight) — skip tick",
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
            # C5 timestamp_ms → KST ISO (Bithumb U5 mirror).
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
                        "[usdt_ws.coinone] Redis write returned False "
                        "(helper 내부 log 참조, WS session 유지)"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[usdt_ws.coinone] Redis write task crashed (격리, WS session 유지)"
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
                    "[usdt_ws.coinone] tether topic trigger 호출 실패 (격리, WS session 유지)"
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
                "[usdt_ws.coinone] Redis writer close timeout (%ds) — cancel %d pending tasks",
                timeout, len(self._tasks),
            )
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            self._tasks.clear()


class CoinoneDbWriter:
    """Coinone USDT/KRW DB writer — 1s window debounce + insert_source_rate_if_changed (C6a).

    Bithumb `BithumbDbWriter` (bithumb.py:277-383) 1:1 mirror. tick rate ~100ms+ vs DB I/O
    ~10-50ms이라 매 tick INSERT는 DB 부담. window 내 마지막 tick만 helper 호출.

    asyncio loop 차단 방지:
        sync SQLAlchemy 호출은 asyncio.to_thread로 격리. DB session은
        to_thread 내부에서 get_db_context()로 생성/close.

    race 방지 (KRX PR6b-2b Codex 보정 + Bithumb U6 mirror):
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
                "[usdt_ws.coinone] DB write failed (격리): %s: %s",
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
                logger.exception("[usdt_ws.coinone] db_writer timer cancel 실패")
        self._timer = None
        tick = self._pending_tick
        self._pending_tick = None
        if tick is None:
            return
        try:
            await asyncio.to_thread(self._sync_db_write, tick)
        except Exception as exc:
            logger.warning(
                "[usdt_ws.coinone] DB write on close failed (격리): %s: %s",
                type(exc).__name__, exc,
            )


class CoinoneRestFallbackController:
    """Coinone REST fallback (silent probe) — C6b.

    Bithumb `BithumbRestFallbackController` (bithumb.py:386-533) 1:1 mirror.
    C7에서 alert evaluator wiring 추가 예정 — 현재는 Redis + DB fanout만.

    DATA frame age > TICKER_FRESHNESS_DEGRADED_SEC (300s provisional) 감지 시 REST 1회
    probe → normalized tick → 기존 fanout (Redis + DB) 재사용. WS reconnect loop는
    그대로 유지, fallback은 freshness 보조.

    Guardrails (Bithumb mirror):
        - schedule_probe()는 sync/non-blocking (_set_ticker_freshness_status 동기 흐름에서 호출).
        - In-flight skip: probe 진행 중 중복 trigger 차단.
        - Cooldown skip: 마지막 probe 종료 후 FALLBACK_COOLDOWN_SEC 미경과 시 skip
          (degraded 지속 중 폭주 방지).
        - Probe 실패도 cooldown 적용 (REST rate limit 보호).
        - reset_cooldown(): normal 복귀 시 호출 → 다음 degraded 즉시 1회 probe 보장.
        - REST 실패 격리: log only, WS session/reconnect 영향 X.
        - fallback tick의 source/asset = "coinone"/"usdt-krw" (downstream 일관).
        - C7에서 alert wiring 추가 예정 (현재는 Redis + DB만 schedule).
    """

    def __init__(
        self,
        redis_writer: "CoinoneRedisWriter",
        db_writer: "CoinoneDbWriter",
        alert_evaluator: "UsdtAlertEvaluator",
        *,
        cooldown_sec: float = FALLBACK_COOLDOWN_SEC,
        probe_timeout_sec: float = FALLBACK_PROBE_TIMEOUT_SEC,
    ) -> None:
        self._redis_writer = redis_writer
        self._db_writer = db_writer
        self._alert_evaluator = alert_evaluator  # C7: AlertObservation schedule on probe success
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
                "[usdt_ws.coinone.fallback] probe in-flight, skip (reason=%s)", reason,
            )
            return
        if now < self._cooldown_until:
            remaining = self._cooldown_until - now
            logger.debug(
                "[usdt_ws.coinone.fallback] cooldown active, skip (%.1fs remaining, reason=%s)",
                remaining, reason,
            )
            return
        # Loop 체크를 coroutine 생성 전에 — no-loop 시 coroutine 미생성으로
        # "coroutine was never awaited" warning 회피.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "[usdt_ws.coinone.fallback] no event loop, skip probe (reason=%s)", reason,
            )
            return
        self._pending_task = loop.create_task(self._run_probe(reason))
        self._in_flight = True

    def reset_cooldown(self) -> None:
        """normal 복귀 시 호출 — cooldown clear. 다음 degraded 즉시 1회 probe 보장."""
        self._cooldown_until = 0.0
        logger.debug("[usdt_ws.coinone.fallback] cooldown reset on normal recovery")

    async def _run_probe(self, reason: str) -> None:
        """REST probe → normalized tick → fanout (Redis + DB + Alert).

        실패 시 log only + cooldown 적용 (재시도 X). WS session 영향 X.
        C7: probe success 시 AlertObservation(kind="rest_probe") schedule 추가.
        """
        try:
            logger.info(
                "[usdt_ws.coinone.fallback] probe start (reason=%s)", reason,
            )
            tick = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_coinone_tick),
                timeout=self._probe_timeout_sec,
            )
            if tick is None:
                logger.warning(
                    "[usdt_ws.coinone.fallback] probe returned None — REST/parse 실패 or invalid rate",
                )
                return

            # 기존 fanout 재사용 (C5 Redis writer + C6a DB writer + C7 Alert evaluator).
            self._redis_writer.schedule(tick)
            self._db_writer.schedule(tick)
            # C7: REST probe kind 구분 (Bithumb U7 mirror — log/metric 영역에서 tick vs probe 분리).
            observation = AlertObservation(
                source=tick["source"],
                asset=tick["asset"],
                rate=tick["rate"],
                timestamp_ms=tick["timestamp_ms"],
                kind="rest_probe",
            )
            self._alert_evaluator.schedule(observation)
            logger.info(
                "[usdt_ws.coinone.fallback] probe success (rate=%s, ts_ms=%d, reason=%s)",
                tick["rate"], tick["timestamp_ms"], reason,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "[usdt_ws.coinone.fallback] probe timeout (%.1fs, reason=%s)",
                self._probe_timeout_sec, reason,
            )
        except Exception:
            logger.exception(
                "[usdt_ws.coinone.fallback] probe error (격리, reason=%s)", reason,
            )
        finally:
            # 실패해도 cooldown 적용 (REST rate limit 보호).
            self._cooldown_until = time.time() + self._cooldown_sec
            self._in_flight = False

    @staticmethod
    def _fetch_coinone_tick() -> Optional[dict]:
        """sync REST fetch — to_thread 내부 실행. 모듈 내부 import (순환 회피)."""
        from app.crawlers.usdt_sources import fetch_coinone_usdt_tick
        return fetch_coinone_usdt_tick()

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
                logger.exception("[usdt_ws.coinone.fallback] close error")
        self._pending_task = None
        self._in_flight = False


class CoinoneWsClient:
    """Coinone USDT/KRW WebSocket client — Phase B.4 complete (C2-C7 누적).

    State:
        - `_stop_event`: stop signal (asyncio.Event)
        - `_running`: 중복 start 방지 flag
        - `_ws`: active WebSocketClientProtocol (session 안에서만)
        - `_first_tick_logged`: first DATA tick INFO log 1회 emit 후 DEBUG 격하
        - `_session_id`: CONNECTED frame `data.session_id` 캡처 (debugging/logging)
        - `_liveness`: UsdtLivenessMonitor (source-neutral 재사용, last_activity_at = max(tick, heartbeat))
        - `_connection_status`: normal | reconnecting | stale (PING/PONG 기반)
        - `_ticker_freshness_status`: normal | warning (60s+) | degraded (300s+, REST probe trigger)
        - `_status_transition_count`: 6개 키 counter (connection 3 + ticker 3 차원)
        - `_reconnect_attempt_count`: reconnect attempt 누적
        - `_pong_event`: application-level PONG synchronization (asyncio.Event)
        - `_redis_writer`: CoinoneRedisWriter (tick-level fire-and-forget, C5)
        - `_db_writer`: CoinoneDbWriter (1s window debounce, C6a)
        - `_fallback_controller`: CoinoneRestFallbackController (silent probe on degraded, C6b)
        - `_alert_evaluator`: UsdtAlertEvaluator (observation-based, source-neutral, C7)

    Phase B.4 기능 구현 complete. 남은 작업: sparse-time smoke 기반 threshold 조정 + canary enable.
    """

    def __init__(self) -> None:
        # C2 lifecycle
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False
        # C3 session state
        self._ws: Optional[Any] = None
        self._first_tick_logged: bool = False
        self._session_id: Optional[str] = None
        # C4 liveness — UsdtLivenessMonitor 재사용 (source-neutral).
        self._liveness: UsdtLivenessMonitor = UsdtLivenessMonitor()
        # C4 2-signal status 분리 (Codex 안 — Upbit/Bithumb `_status` 단일 통합 회피).
        # _connection_status: PING/PONG 기반 (PONG timeout / ConnectionClosed → reconnect).
        # _ticker_freshness_status: DATA frame age 기반 (transition 1회 log, action X).
        self._connection_status: str = "normal"  # normal | reconnecting | stale
        # C4: normal | warning (60s+). C6b: degraded (300s+, REST probe trigger) 추가.
        self._ticker_freshness_status: str = "normal"  # normal | warning | degraded
        self._status_transition_count: dict[str, int] = {
            "connection_normal": 0,
            "connection_reconnecting": 0,
            "connection_stale": 0,
            "ticker_normal": 0,
            "ticker_warning": 0,
            "ticker_degraded": 0,
        }
        self._reconnect_attempt_count: int = 0
        # C4 application-level PONG synchronization (Coinone 별 protocol).
        # `_ping_loop`이 clear → send → wait_for 순서로 사용. PONG response는
        # `_handle_message`의 response_type=="PONG" 분기에서 set() 호출.
        self._pong_event: asyncio.Event = asyncio.Event()
        # C5 Redis writer (tick-level, fire-and-forget) — A13 lifecycle invariant:
        # __init__에서 1회 생성, reconnect 사이 재사용 (재할당 없음). Bithumb U5 mirror.
        self._redis_writer: CoinoneRedisWriter = CoinoneRedisWriter()
        # C6a DB writer (1s window debounce). Bithumb U6 mirror.
        self._db_writer: CoinoneDbWriter = CoinoneDbWriter()
        # C7 Alert evaluator (observation-based, source-neutral helper 재사용).
        # Bithumb U7 mirror — Coinone client 자체 instance 보유.
        self._alert_evaluator: UsdtAlertEvaluator = UsdtAlertEvaluator()
        # C6b REST fallback controller (silent probe). Bithumb U6/U7 mirror.
        # Redis + DB writer + Alert evaluator inject (C7 alert_evaluator 필수).
        self._fallback_controller: CoinoneRestFallbackController = (
            CoinoneRestFallbackController(
                redis_writer=self._redis_writer,
                db_writer=self._db_writer,
                alert_evaluator=self._alert_evaluator,
            )
        )

    @staticmethod
    def _build_subscribe_payload() -> dict:
        """Coinone subscribe payload — single-dict form (별 protocol, Upbit/Bithumb과 다름).

        USDT_EXCHANGE_WEBSOCKET_GUIDE §5: 대문자 enum 필수 (request_type/channel).
        DEFAULT format 유지 (USDT_WS_DESIGN_PLAN §12.6.3 결정 4 — SHORT는 범위 밖).
        """
        return {
            "request_type": "SUBSCRIBE",
            "channel": "TICKER",
            "topic": {
                "quote_currency": COINONE_QUOTE_CURRENCY,
                "target_currency": COINONE_TARGET_CURRENCY,
            },
        }

    def _parse_data_message(self, message: dict) -> Optional[dict]:
        """Coinone DATA frame → normalized tick dict, None on invalid/non-DATA.

        Normalized shape (Upbit/Bithumb fetch_*_usdt_tick contract 일치):
            {"source": "coinone", "asset": "usdt-krw", "rate": float, "timestamp_ms": int}

        timestamp_ms는 raw int 유지 (KST/UTC 변환은 C6 DB writer 영역).

        Guard:
            - response_type != "DATA" → None
            - data dict 부재 → None
            - quote_currency != KRW or target_currency != USDT → None
            - last 부재 / parse 실패 / <= 0 → None
            - timestamp 부재 / parse 실패 → None
        """
        if message.get("response_type") != "DATA":
            return None
        data = message.get("data")
        if not isinstance(data, dict):
            return None
        if data.get("quote_currency") != COINONE_QUOTE_CURRENCY:
            return None
        if data.get("target_currency") != COINONE_TARGET_CURRENCY:
            return None

        last_raw = data.get("last")
        if last_raw is None:
            return None
        try:
            rate = float(last_raw)
        except (TypeError, ValueError):
            return None
        if rate <= 0:
            return None

        ts_raw = data.get("timestamp")
        if ts_raw is None:
            return None
        try:
            ts_ms = int(ts_raw)
        except (TypeError, ValueError):
            return None

        return {
            "source": "coinone",
            "asset": "usdt-krw",
            "rate": rate,
            "timestamp_ms": ts_ms,
        }

    def _handle_message(self, raw) -> Optional[dict]:
        """Coinone frame → response_type 분기 + log only. DATA tick dict 반환, 그 외 None.

        Response types (USDT_EXCHANGE_WEBSOCKET_GUIDE §5 + C0 smoke 실측):
            - CONNECTED: handshake 직후 1회. `data.session_id` 캡처 (debugging).
            - SUBSCRIBED: subscribe ACK 1회. log only.
            - DATA: ticker frame. `_parse_data_message`로 normalize.
              first tick INFO log + 이후 DEBUG (Bithumb 패턴 mirror).
            - PONG: PING 응답. C4에서 latency 측정 예정. C3는 DEBUG only.
            - ERROR: 서버 error 응답. WARNING (error_message log).
            - unknown / missing: WARNING (safe log — Codex 강조 2: unknown safe).

        Acceptance (C3):
            - return: DATA만 tick dict / 그 외 None (C5~C7 callsite 재사용 위해 유지)
            - downstream IO 0 (Redis/DB/alert/REST 미호출 — C5~C7 영역)
        """
        # raw decode (bytes/str/dict)
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("[usdt_ws.coinone] non-utf8 frame, skip")
                return None
        if isinstance(raw, str):
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("[usdt_ws.coinone] invalid JSON frame, skip")
                return None
        elif isinstance(raw, dict):
            message = raw
        else:
            logger.warning("[usdt_ws.coinone] unexpected raw type=%s, skip", type(raw).__name__)
            return None

        if not isinstance(message, dict):
            logger.warning("[usdt_ws.coinone] non-dict message, skip")
            return None

        rtype = message.get("response_type")

        if rtype == "CONNECTED":
            # data field가 dict가 아닐 경우 (malformed frame) safe fallback — C3 "safe
            # log-only" invariant 유지 (AttributeError로 session crash 방지).
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            session_id = data.get("session_id")
            if isinstance(session_id, str) and session_id:
                self._session_id = session_id
            logger.info(
                "[usdt_ws.coinone] CONNECTED session_id=%s",
                self._session_id,
            )
            return None

        if rtype == "SUBSCRIBED":
            # data field defensive — CONNECTED와 동일 패턴
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            logger.info(
                "[usdt_ws.coinone] SUBSCRIBED channel=%s topic=%s",
                message.get("channel"), data,
            )
            return None

        if rtype == "DATA":
            tick = self._parse_data_message(message)
            if tick is None:
                logger.warning("[usdt_ws.coinone] DATA parse failed, skip")
                return None
            if not self._first_tick_logged:
                logger.info("[usdt_ws.coinone] first tick", extra=tick)
                self._first_tick_logged = True
            else:
                logger.debug("[usdt_ws.coinone] tick", extra=tick)
            return tick

        if rtype == "PONG":
            # C4: _ping_loop의 wait_for(_pong_event.wait())을 풀어줌
            self._pong_event.set()
            logger.debug("[usdt_ws.coinone] PONG")
            return None

        if rtype == "ERROR":
            logger.warning(
                "[usdt_ws.coinone] ERROR error_code=%s error_message=%s",
                message.get("error_code"), message.get("error_message"),
            )
            return None

        # unknown / missing response_type — safe log (Codex 강조 2)
        logger.warning(
            "[usdt_ws.coinone] unknown response_type=%r, skip",
            rtype,
        )
        return None

    def _set_connection_status(self, new_status: str) -> None:
        """C4 connection 차원 status 전이 + counter + log (Codex 안 — 2-signal 분리).

        new_status: normal | reconnecting | stale
        """
        if self._connection_status == new_status:
            return
        prev = self._connection_status
        self._connection_status = new_status
        key = f"connection_{new_status}"
        if key in self._status_transition_count:
            self._status_transition_count[key] += 1
        logger.info(
            "[usdt_ws.coinone] connection_status %s → %s (reconnect_attempt=%d max_gap=%.2fs)",
            prev, new_status, self._reconnect_attempt_count,
            self._liveness.max_frame_gap_sec,
        )

    def _set_ticker_freshness_status(self, new_status: str) -> None:
        """C4 ticker freshness 차원 status 전이 + counter + log + C6b fallback hook.

        Transition-based — 매 loop마다 warning 안 찍히도록 상태 변화 시점에만 1회 log
        (log flood 방지). DATA 수신 시 normal 복귀에서도 1회 log.

        C6b fallback hook (Bithumb U6 _set_status mirror):
            - degraded 진입 시 fallback_controller.schedule_probe(reason="ticker_degraded")
            - normal 복귀 시 fallback_controller.reset_cooldown()

        new_status: normal | warning | degraded
        """
        if self._ticker_freshness_status == new_status:
            return
        prev = self._ticker_freshness_status
        self._ticker_freshness_status = new_status
        key = f"ticker_{new_status}"
        if key in self._status_transition_count:
            self._status_transition_count[key] += 1
        logger.info(
            "[usdt_ws.coinone] ticker_freshness_status %s → %s "
            "(last_tick_at=%s max_gap=%.2fs)",
            prev, new_status, self._liveness.last_tick_at,
            self._liveness.max_frame_gap_sec,
        )
        # C6b fallback hook
        if new_status == "degraded":
            self._fallback_controller.schedule_probe(reason="ticker_degraded")
        elif new_status == "normal":
            # warning → normal 또는 degraded → normal 모두 reset_cooldown 호출.
            # 새 outage cycle의 첫 degraded probe가 옛 cooldown으로 skip되지 않도록.
            self._fallback_controller.reset_cooldown()

    @staticmethod
    def _compute_backoff(attempt: int) -> float:
        """Reconnect backoff — Bithumb/Upbit 동일 sequence."""
        if attempt <= 0:
            return RECONNECT_BACKOFF_SEQ[0]
        if attempt <= len(RECONNECT_BACKOFF_SEQ):
            return RECONNECT_BACKOFF_SEQ[attempt - 1]
        return RECONNECT_BACKOFF_TAIL

    async def _ping_loop(self, ws) -> None:
        """C4 application-level PING/PONG event-based (Codex Point 3 안).

        Coinone 별 protocol — Bithumb의 `ws.ping()` (WS protocol) 대신
        `ws.send({"request_type":"PING"})` (application-level). PONG response는
        `_handle_message`의 response_type=="PONG" 분기에서 `_pong_event.set()`.

        Sequence:
            1. _pong_event.clear() — 이전 PONG event 초기화
            2. ws.send({"request_type":"PING"})
            3. wait_for(_pong_event.wait(), PING_TIMEOUT_SEC=5)
            4. 성공 → observe_heartbeat / timeout → ws.close() → recv ConnectionClosed → reconnect

        Acceptance:
            - timeout/closed → ws.close() (recv loop가 ConnectionClosed 감지 → reconnect)
            - heartbeat/pong → liveness만 갱신 (status 직접 변경 X)
        """
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(PING_INTERVAL_SEC)
            except asyncio.CancelledError:
                return
            if self._stop_event.is_set():
                return
            try:
                # Codex Point 3 sequence: clear → send → wait_for
                self._pong_event.clear()
                await ws.send(json.dumps({"request_type": "PING"}))
                await asyncio.wait_for(
                    self._pong_event.wait(), timeout=PING_TIMEOUT_SEC,
                )
                self._liveness.observe_heartbeat(time.time())
            except asyncio.CancelledError:
                return
            except (asyncio.TimeoutError, ConnectionClosed) as exc:
                logger.warning(
                    "[usdt_ws.coinone] ping failed: %s — closing ws for reconnect",
                    type(exc).__name__,
                )
                try:
                    await ws.close()
                except Exception:
                    pass
                return

    async def _run_one_session(self) -> None:
        """C4 — connect + subscribe + recv loop + ping_task background.

        Acceptance:
            - websockets.connect(URL, ping_interval=None) — auto-ping 비활성, application-level PING 사용
            - ping_task background — 5분 cycle PING + PONG event wait
            - recv loop: RECV_TIMEOUT_SEC=1.0 (stop_event 즉시 반응)
            - is_stale 체크 (connection liveness — heartbeat fresh면 ticker silence 무관)
            - ticker_update_age_sec > 60s 시 ticker_freshness_status normal → warning (1회 log)
            - valid DATA → observe_tick(time.time()) + ticker_freshness_status warning → normal 복귀
            - finally: ping_task cancel/await
            - ticker silence alone → reconnect never (Codex 강조)
            - Redis/DB/Alert/REST writer 일체 미진입 (C5~C7 영역)
        """
        async with websockets.connect(
            COINONE_WS_URL,
            ping_interval=None,
            ping_timeout=None,
            open_timeout=10,
        ) as ws:
            self._ws = ws
            self._liveness.reset_active_session()
            logger.info("[usdt_ws.coinone] connected url=%s", COINONE_WS_URL)
            payload = self._build_subscribe_payload()
            await ws.send(json.dumps(payload))
            logger.info(
                "[usdt_ws.coinone] subscribed channel=TICKER topic=%s/%s",
                COINONE_QUOTE_CURRENCY, COINONE_TARGET_CURRENCY,
            )
            self._set_connection_status("normal")
            self._set_ticker_freshness_status("normal")

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                while not self._stop_event.is_set():
                    now = time.time()
                    # Connection liveness — UsdtLivenessMonitor.is_stale은 heartbeat fresh면
                    # ticker silence 무관 False (last_activity_at = max(tick, heartbeat)).
                    # ticker silence alone → reconnect never (Codex 강조).
                    if self._connection_status != "stale" and self._liveness.is_stale(now, STALE_AFTER_SEC):
                        self._set_connection_status("stale")
                    elif self._connection_status == "stale" and not self._liveness.is_stale(now, STALE_AFTER_SEC):
                        self._set_connection_status("normal")

                    # Ticker freshness telemetry (transition 기반).
                    # C4: normal → warning (60s+, log only).
                    # C6b: warning → degraded (300s+, REST probe trigger via _set_*_status hook).
                    # last_tick_at 없으면 (첫 tick 전) freshness 평가 skip.
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
                            # C6b: degraded 진입 시 _set_*_status hook이 REST probe schedule
                            self._set_ticker_freshness_status("degraded")
                        # warning/degraded → normal 복귀는 valid DATA 수신 시점에 처리 (아래).

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_SEC)
                    except asyncio.TimeoutError:
                        continue
                    except ConnectionClosed:
                        if self._stop_event.is_set():
                            logger.info("[usdt_ws.coinone] connection closed after stop")
                            return
                        raise

                    # C3 parse + log only. C4: valid DATA → observe_tick + ticker freshness normal 복귀.
                    # C5: Redis writer schedule (tick-level, fire-and-forget).
                    # C6a: DB writer schedule (1s window debounce).
                    # C6b~C7에서 REST fallback / Alert downstream IO 추가 예정.
                    tick = self._handle_message(raw)
                    if tick is not None:
                        self._liveness.observe_tick(time.time())
                        # warning/degraded → normal 복귀 (C4 + C6b — 상태 전이 1회 log + cooldown reset hook).
                        if self._ticker_freshness_status in ("warning", "degraded"):
                            self._set_ticker_freshness_status("normal")
                        # C5: Redis writer schedule (recv loop 격리 — fire-and-forget).
                        self._redis_writer.schedule(tick)
                        # C6a: DB writer schedule (1s window debounce + race-prevention).
                        self._db_writer.schedule(tick)
                        # C7: AlertObservation schedule (source-neutral evaluator 재사용).
                        # Bithumb U7 mirror — kind="tick" 구분 (REST probe와 log/metric 분리).
                        observation = AlertObservation(
                            source=tick["source"],
                            asset=tick["asset"],
                            rate=tick["rate"],
                            timestamp_ms=tick["timestamp_ms"],
                            kind="tick",
                        )
                        self._alert_evaluator.schedule(observation)
            finally:
                # C4 finally: ping_task cancel/await (Bithumb _run_one_session mirror).
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("[usdt_ws.coinone] ping_task cleanup 실패")
                # C6b finally 1: fallback_controller — pending probe task cancel.
                # 순서 (Bithumb U7 mirror, 최종): fallback → DB → Alert → Redis.
                # fallback이 모든 writer를 schedule할 수 있으므로 먼저 멈춰야 downstream
                # writer들이 깔끔히 drain. Redis는 항상 마지막.
                # 각 close는 개별 try/except로 격리 — 한 close 실패해도 뒤 close 실행 보장 (Codex Point 2).
                try:
                    await self._fallback_controller.close()
                except Exception:
                    logger.exception("[usdt_ws.coinone] fallback_controller.close() 실패")
                # C6a finally 2: DB writer (source of truth) timer cancel + pending tick 즉시 flush.
                try:
                    await self._db_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.coinone] db_writer.close() 실패")
                # C7 finally 3: Alert evaluator drain (FCM 보호, Bithumb U7 mirror).
                # 예외 격리: WS loop에 전파 X (acceptance 8).
                try:
                    await self._alert_evaluator.close()
                except Exception:
                    logger.exception("[usdt_ws.coinone] alert_evaluator.close() 실패")
                # C5 finally 4: Redis writer pending drain (마지막).
                try:
                    await self._redis_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.coinone] redis_writer.close() 실패")

    async def start(self) -> None:
        """C4 — `_run_one_session` reconnect loop + backoff (Bithumb start() mirror).

        Acceptance:
            - 중복 start 방지 (_running flag)
            - session crash (ConnectionClosed / Exception) → backoff + reconnect
            - stop_event set 시 즉시 종료
            - ticker silence alone에서는 reconnect 발생 안 함 (PONG fail / ConnectionClosed에서만)
        """
        if self._running:
            logger.debug("[usdt_ws.coinone] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info("[usdt_ws.coinone] start (C4 — connect/subscribe/parse + PING/PONG + reconnect)")
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
                        "[usdt_ws.coinone] connection closed (attempt %d): %s — backoff %.1fs",
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
                        "[usdt_ws.coinone] session error (attempt %d): %s: %s — backoff %.1fs",
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
            self._running = False
            self._ws = None

    async def stop(self) -> None:
        """stop signal.

        - `_stop_event.set()` — recv loop / ping_loop / start backoff sleep 모두 풀어줌
        - `_pong_event.set()` — 진행 중인 _ping_loop의 wait_for(_pong_event.wait())을 즉시 풀어줌
          (정상 PONG처럼 wakeup하지만 stop_event도 set이라 다음 iteration에서 종료)
        """
        self._stop_event.set()
        self._pong_event.set()
