"""USDT WebSocket — Coinone canary client (Phase B.4 Stage C2-C4).

USDT_WS_DESIGN_PLAN §12.6 Phase B.4.
Bithumb Stage U2-U4 패턴 mirror (작은 단위 stage 분할 학습 — KRX close finalizer
1100 lines → 3 High findings → revert + Phase B.3 U2~U7 분할 재시작 학습 적용).

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

C4 connection liveness + ticker freshness telemetry (현재):
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
      alone → reconnect never (사용자 일요일 Bithumb 무체결 관찰 + USDT/KRW 시장에서 수분
      단위 무체결 정상 → false reconnect 위험 방지).

C5 이후 누적 예정 (USDT_WS_DESIGN_PLAN §12.6.4):
    - C5: CoinoneRedisWriter + topic trigger 자동 발화
    - C6: CoinoneDbWriter + REST fallback (180~300s ticker freshness degraded threshold 결정) +
      `fetch_coinone_usdt_tick()` normalized helper
    - C7: UsdtAlertEvaluator wiring
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

# UsdtLivenessMonitor 재사용 — source-neutral (last_activity_at = max(tick, heartbeat) 이미
# 2-signal 의식이라 Coinone에 그대로 적용 가능. C4 신규 monitor 신설 X).
from app.crawlers.usdt_ws.upbit import UsdtLivenessMonitor

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.coinone")


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
# 사용자 일요일 Bithumb 무체결 관찰: USDT/KRW 시장에서 수분 단위 무체결 정상 → DATA silence를
# connection failure로 해석 시 false reconnect 위험. ticker silence alone → reconnect never.
TICKER_FRESHNESS_WARNING_SEC = 60.0

# C4 Reconnect backoff — Bithumb/Upbit 동일 sequence (USDT_WS_DESIGN_PLAN §10).
RECONNECT_BACKOFF_SEQ = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RECONNECT_BACKOFF_TAIL = 30.0


class CoinoneWsClient:
    """Coinone USDT/KRW WebSocket client — C2 skeleton + C3 connect/parse + C4 liveness/freshness.

    State (C2 lifecycle + C3 session + C4 liveness/status):
        - `_stop_event`: stop signal (asyncio.Event)
        - `_running`: 중복 start 방지 flag
        - `_ws`: active WebSocketClientProtocol (session 안에서만)
        - `_first_tick_logged`: first DATA tick INFO log 1회 emit 후 DEBUG 격하
        - `_session_id`: CONNECTED frame `data.session_id` 캡처 (debugging/logging)
        - `_liveness`: UsdtLivenessMonitor (source-neutral 재사용, last_activity_at = max(tick, heartbeat))
        - `_connection_status`: normal | reconnecting | stale (PING/PONG 기반)
        - `_ticker_freshness_status`: normal | warning (DATA frame age 기반, action X)
        - `_status_transition_count`: 5개 키 counter (connection + ticker 차원)
        - `_reconnect_attempt_count`: reconnect attempt 누적
        - `_pong_event`: application-level PONG synchronization (asyncio.Event)

    C5 이후 누적 예정 (Redis/DB/Alert/REST state — Bithumb `BithumbWsClient` 패턴).
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
        self._ticker_freshness_status: str = "normal"  # normal | warning
        self._status_transition_count: dict[str, int] = {
            "connection_normal": 0,
            "connection_reconnecting": 0,
            "connection_stale": 0,
            "ticker_normal": 0,
            "ticker_warning": 0,
        }
        self._reconnect_attempt_count: int = 0
        # C4 application-level PONG synchronization (Coinone 별 protocol).
        # `_ping_loop`이 clear → send → wait_for 순서로 사용. PONG response는
        # `_handle_message`의 response_type=="PONG" 분기에서 set() 호출.
        self._pong_event: asyncio.Event = asyncio.Event()

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
        """C4 ticker freshness 차원 status 전이 + counter + log (Codex Point 2 안).

        Transition-based — 매 loop마다 warning 안 찍히도록 상태 변화 시점에만 1회 log
        (Codex Point 1: log flood 방지). DATA 수신 시 normal 복귀에서도 1회 log.

        new_status: normal | warning
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

                    # Ticker freshness telemetry (transition 기반, Codex Point 1 + 2).
                    # last_tick_at 없으면 (첫 tick 전) freshness 평가 skip.
                    last_tick = self._liveness.last_tick_at
                    if last_tick is not None:
                        ticker_update_age_sec = now - last_tick
                        if (
                            self._ticker_freshness_status == "normal"
                            and ticker_update_age_sec > TICKER_FRESHNESS_WARNING_SEC
                        ):
                            self._set_ticker_freshness_status("warning")
                        # warning → normal 복귀는 valid DATA 수신 시점에 처리 (아래).

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
                    # C5~C7에서 Redis/DB/Alert downstream IO 추가 예정.
                    tick = self._handle_message(raw)
                    if tick is not None:
                        self._liveness.observe_tick(time.time())
                        # warning → normal 복귀 (Codex Point 1 — 상태 전이 1회 log).
                        if self._ticker_freshness_status == "warning":
                            self._set_ticker_freshness_status("normal")
            finally:
                # ping_task cancel/await — Bithumb _run_one_session finally mirror.
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("[usdt_ws.coinone] ping_task cleanup 실패")

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
