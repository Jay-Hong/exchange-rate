"""USDT WebSocket — Upbit canary client (PR3 LivenessMonitor + reconnect).

USDT_WS_DESIGN_PLAN §12.2 PR1~PR3 누적.
PR1: lifecycle skeleton.
PR2: connect/subscribe/parse + log only.
PR3: UsdtLivenessMonitor + reconnect loop + status state machine
     + explicit ping/pong heartbeat observation (§5 frame/heartbeat
     silence ≠ price tick silence 원칙 준수).

NO Redis (PR4), NO DB (PR5), NO alert (PR6), NO REST fallback (PR7).

PR3 핵심 §5 준수:
    - stale 판정 기준: `last_activity_at = max(last_tick_at, last_heartbeat_at)`
    - ticker가 5분 silent 이어도 heartbeat이 30s 이내면 normal 유지
    - heartbeat과 ticker 둘 다 silent여야 stale
    - 저유동성 false positive 차단 (§5 핵심 nuance)

PR3 guardrail (Codex 검토):
    - `_set_status("stale")`은 counter + log만. REST probe / Redis / DB /
      alert side effect 절대 호출 X (PR7에서 fallback trigger 추가 예정).

KRX 패턴 mirror:
    - `KrxLivenessMonitor` (krx_kis.py:513) — frame counters lifetime,
      last_*_at + gap_buckets + max_gap active-session reset
    - `_run_session` reconnect (krx_kis.py:1114) — RECONNECT_BACKOFF_SEQ + tail
    - `ping_interval=None` + 명시 ping (krx_kis.py:1168) — heartbeat observation
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.upbit")

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
            self._running = False
            self._ws = None
            logger.info(
                "[usdt_ws.upbit] start exited (reconnect_attempts=%d)",
                self._reconnect_attempt_count,
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
        """status 전이 + counter + log only (PR3 guardrail).

        REST probe / Redis / DB / alert side effect 절대 호출 X (PR7에서 추가).
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

    def _handle_message(self, raw) -> bool:
        """parse + log only. Returns True if valid ticker, False if ignored/invalid.

        반환값은 caller가 `_liveness.observe_tick()` 호출 여부 결정 — invalid /
        non-ticker frame (status / KRW-BTC / JSON parse 실패 등)을 liveness
        activity와 분리 (Codex review). PR4/PR5/PR6 downstream은 별도 hook.
        """
        tick = self._parse_ticker_message(raw)
        if tick is None:
            return False
        if not self._first_tick_logged:
            logger.info("[usdt_ws.upbit] first tick", extra=tick)
            self._first_tick_logged = True
        else:
            logger.debug("[usdt_ws.upbit] tick", extra=tick)
        return True

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
                    # valid ticker만 liveness activity로 집계 (Codex review).
                    # invalid/non-ticker frame은 metric 오염 방지.
                    if self._handle_message(raw):
                        self._liveness.observe_tick(time.time())
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("[usdt_ws.upbit] ping_task cleanup 실패")
