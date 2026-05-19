"""USDT WebSocket — Korbit canary client (Phase B.5 Stage K2-K4).

USDT_WS_DESIGN_PLAN §12.7 Phase B.5.
Coinone Stage C2-C4 + Bithumb Stage U4 패턴 혼합 mirror.

K2 lifecycle skeleton (완료):
    - `_stop_event` + `_running`
    - flag=false 시 scheduler가 KorbitWsClient 자체를 생성 안 함 (acceptance)

K3 connect/subscribe/parse + log only (완료):
    - Constants / `_build_subscribe_payload` (list-wrap) / `_parse_ticker_message`
      (close + lastTradedAt fallback) / `_handle_message` (unified status 분기) /
      `_run_one_session` (single session) / `start` (no reconnect)

K4 liveness + ws.ping() heartbeat + 2 status 분리 + reconnect loop (현재):
    - `UsdtLivenessMonitor` source-neutral 재사용 (Upbit/Bithumb/Coinone와 동일)
    - **`_ping_loop` — Bithumb explicit ping mirror**: `ws.ping()` + `wait_for(pong_waiter, timeout)` +
      성공 시 `_liveness.observe_heartbeat`. timeout/closed 시 ws.close() → recv loop가
      ConnectionClosed → start reconnect loop. (Coinone application-level PING과 다름 —
      K-2 smoke pong_waiter 5회 정상 검증 완료, median 14.64ms latency)
    - **2 status 차원 분리 (Coinone C4 mirror)**:
        - `_connection_status`: normal | reconnecting | stale (PING/PONG + last_activity_at 기준)
        - `_ticker_freshness_status`: normal | warning (ticker age 기반, log only)
    - **2-signal invariant**: ticker silence + heartbeat fresh → reconnect/stale 안 됨
      (last_activity_at = max(tick, heartbeat), Coinone C4 핵심 결정 mirror)
    - **Reconnect loop (Bithumb start mirror)**: backoff sequence `[1,2,4,8,16,30]` Upbit/KRX 통일
    - **Scope guard**: K4에서 `UsdtLivenessMonitor` 허용. Redis/DB/Alert/REST는 미진입 (K5~K7 영역).

K5 이후 누적 예정 (USDT_WS_DESIGN_PLAN §12.7.4):
    - K5: KorbitRedisWriter + topic trigger 자동 발화
    - K6: KorbitDbWriter + REST fallback + `fetch_korbit_usdt_tick()` normalized helper
      (contract `{source, asset, rate, timestamp_ms}` Coinone/Bithumb과 동일).
      degraded transition + fallback hook도 K6에 추가.
    - K7: UsdtAlertEvaluator wiring
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from app.crawlers.usdt_ws.upbit import UsdtLivenessMonitor

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.korbit")


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
        }

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
        """K4 ticker freshness 차원 status 전이 + counter + log (Coinone C4 mirror).

        Transition-based — 매 loop마다 warning 안 찍히도록 상태 변화 시점에만 1회 log.
        new_status: normal | warning.
        K4 scope: log only, action 미진입. degraded transition + fallback hook은 K6 영역.
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

                    # Ticker freshness telemetry (transition 기반, K4 scope = normal ↔ warning).
                    # last_tick_at 없으면 (첫 tick 전) freshness 평가 skip.
                    # K6에서 warning → degraded (REST probe trigger) 추가 예정.
                    last_tick = self._liveness.last_tick_at
                    if last_tick is not None:
                        ticker_update_age_sec = now - last_tick
                        if (
                            self._ticker_freshness_status == "normal"
                            and ticker_update_age_sec > TICKER_FRESHNESS_WARNING_SEC
                        ):
                            self._set_ticker_freshness_status("warning")
                        # warning → normal 복귀는 valid ticker 수신 시점에 처리 (아래).

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
                    # K5~K7에서 Redis/DB/Alert downstream IO 추가 예정.
                    tick = self._handle_message(raw)
                    if tick is not None:
                        self._liveness.observe_tick(time.time())
                        if self._ticker_freshness_status == "warning":
                            self._set_ticker_freshness_status("normal")
            finally:
                # Acceptance #9: ping_task cancel/await 보장.
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("[usdt_ws.korbit] ping_task cleanup 실패")

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
