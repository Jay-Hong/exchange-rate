"""USDT WebSocket — Bithumb canary client (Phase B.3 Stage U2-U5).

USDT_WS_DESIGN_PLAN §12.5 Phase B.3 Stage U2-U5.
Upbit Phase B.1 PR1-PR4 패턴 작은 복제 (KRX close finalizer 분할 학습 — 큰 추상화 금지).

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
    - `_set_status` — counter + log only (Redis/DB/fallback hook 없음, U5/U6 영역)
    - `_compute_backoff` — Upbit 동일 sequence (1, 2, 4, 8, 16, 30s tail)
    - `_ping_loop` — 20s ping + pong → `_liveness.observe_heartbeat` (§5 provisional 30s)
    - `_run_one_session` 확장 — liveness reset + stale check + ping_task lifecycle
    - `start` reconnect loop — Upbit PR3 backoff sequence mirror

U5 (현재): BithumbRedisWriter (tick-level Redis latest write) + tether topic trigger.
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

후속 stage 예정 (별도 GO):
    U6: BithumbDbWriter (1초 window) + BithumbRestFallbackController +
        fetch_bithumb_usdt_tick() normalized REST helper
        ({source, asset, rate, timestamp_ms} shape — Upbit helper contract 일치)

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

    def schedule(self, tick: dict) -> None:
        """tick → background Redis write task. fire-and-forget.

        Saturation 시 (`len(_tasks) >= MAX_PENDING_WRITES`) skip + warning.
        Redis 장애로 task가 누적되는 시나리오 차단 (A8).
        """
        if len(self._tasks) >= MAX_PENDING_WRITES:
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
                        "[usdt_ws.bithumb] Redis write returned False "
                        "(helper 내부 log 참조, WS session 유지)"
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[usdt_ws.bithumb] Redis write task crashed (격리, WS session 유지)"
                )
                success = False

        # lock 밖에서 trigger — Redis write 성공 시에만 (3, 4).
        # trigger 호출 자체의 예외는 writer/WS에 전파 X (5, 책임 분리).
        if success:
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


class BithumbWsClient:
    """Bithumb USDT/KRW WebSocket client — U2-U4 누적.

    State (U4):
        - `_stop_event`: stop signal (asyncio.Event)
        - `_running`: 중복 start 방지 flag
        - `_ws`: active WebSocketClientProtocol (session 안에서만)
        - `_first_tick_logged`: first tick INFO log 1회 emit 후 DEBUG로 격하
        - `_liveness`: UsdtLivenessMonitor (frame/heartbeat gap state)
        - `_status`: "normal" | "reconnecting" | "stale"
        - `_status_transition_count`: status별 전이 count (telemetry)
        - `_reconnect_attempt_count`: ConnectionClosed/Exception 누적 attempt

    U4 핵심:
        - Reconnect loop (start) + backoff sequence
        - ping/pong heartbeat (observe via UsdtLivenessMonitor)
        - stale 전이 — status/log/counter only (Redis/DB/fallback 영역 X)

    NO Redis/DB/topic/fallback (U5/U6에서 추가 예정).
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
        """status 전이 + counter + log.

        U4 시점: counter + log only.
        Redis/DB/fallback hook은 U5/U6 영역 — 본 stage에서 호출 0 (acceptance 4).
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
                    # valid ticker만 liveness activity + Redis writer schedule.
                    # invalid/non-ticker frame은 모두 X (acceptance 7).
                    tick = self._handle_message(raw)
                    if tick is not None:
                        self._liveness.observe_tick(time.time())
                        # U5: Redis writer schedule (fire-and-forget, recv loop 격리)
                        self._redis_writer.schedule(tick)
                    # U6 영역: DB writer / alert / fallback (현재 미추가).
            finally:
                # Acceptance 3: ping task cancel/await 보장.
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("[usdt_ws.bithumb] ping_task cleanup 실패")
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
            self._running = False
            self._ws = None
            logger.info(
                "[usdt_ws.bithumb] start exited (reconnect_attempts=%d)",
                self._reconnect_attempt_count,
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
