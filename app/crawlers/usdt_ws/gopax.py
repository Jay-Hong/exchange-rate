"""Gopax USDT/KRW WebSocket client — Phase B.6 Stage G4.

USDT_WS_DESIGN_PLAN §12.9 (Phase B.6, 2026-05-21).

G4 scope (이 파일의 현재 범위):
    G1 lifecycle + G2 Subscribe/Parse + G3 Primus pong + **G4 UsdtLivenessMonitor +
    reconnect loop + 2-signal status (connection + ticker_freshness)**까지.
    Writers + fallback + alert (G5-G7) / Telemetry (PR 2e)는 별도 stage.

G4 주요 변경:
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
      G5 또는 별도 PR로 미룸.
    - `_handle_message` valid tick 시 `_liveness.observe_tick(now)` 호출.
    - Constants: STALE_AFTER_SEC=360 / TICKER_FRESHNESS_WARNING=60 / DEGRADED=300
      (Coinone 기준 provisional) / RECONNECT_BACKOFF_SEQ (1,2,4,8,16,30) tail 30.

⚠️ Production activation 제약 (G4 단계도 유지 — Codex 강조 강화):
    G4 land 후에도 `USDT_WS_GOPAX_ENABLED=true` 토글 금지. G4 단독은 reconnect/
    liveness만 land되고 Redis/DB/Telemetry 부재라 silent ingestion 상태 (관찰
    불가능). G4는 **local/staging smoke 가능 단계**.

    **Production activation은 최소 G5 Redis writer + PR 2e telemetry 이후 검토**.
    그 전까지 production env는 default false 유지.

G4 acceptance:
    - flag=false 시 GopaxWsClient 생성 X + task 생성 X (G1~G3 동일)
    - flag=true 시 reconnect loop으로 session crash 시 backoff 후 재시도
    - liveness 기반 connection_status 전이 (stale ↔ normal)
    - tick silence 기반 ticker_freshness_status 전이 (normal/warning/degraded)
    - degraded 전이는 status 갱신만 — fallback action은 G6b 영역 (Codex 정정)
    - production env false 유지 (G5 + PR 2e 이전 activation 보류)

G4 누적 attribute (G1 + G2 + G3 + G4):
    - G1: `_stop_event` / `_running`
    - G2: `_ws` / `_first_tick_logged`
    - G3: `_last_heartbeat_at` (dual-write 유지 — Codex 정정으로 G4에서 미제거)
    - G4: `_liveness` / `_connection_status` / `_ticker_freshness_status` /
      `_reconnect_attempt_count` / `_status_transition_count` (6 keys)
    G5-G7 attribute (`_redis_writer` / `_db_writer` / `_fallback_controller` /
    `_alert_evaluator`)는 여전히 본 stage 제외.
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

logger = logging.getLogger("exchange_rate.crawler.usdt_ws.gopax")

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

# G4 — Ticker freshness threshold (Coinone 기준, provisional).
# Gopax delta event 활성도 비례 변동 → Coinone과 유사. Korbit (30/120)보다 여유.
# G4 land 후 staging smoke 측정으로 확정 예정.
TICKER_FRESHNESS_WARNING_SEC = 60.0
TICKER_FRESHNESS_DEGRADED_SEC = 300.0

# G4 — Reconnect backoff sequence (Bithumb/Coinone/Korbit 동일).
# Gopax rate limit 20 req/sec/IP에 비해 매우 여유 (1초 minimum 안전).
RECONNECT_BACKOFF_SEQ = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
RECONNECT_BACKOFF_TAIL = 30.0


class GopaxWsClient:
    """Gopax USDT/KRW WebSocket client — Phase B.6 Stage G4.

    G1~G3 누적 + G4 UsdtLivenessMonitor + reconnect loop + 2-signal status.
    Coinone/Korbit 패턴 mirror (2-dim status, last_activity_at 기반 stale 판정).
    G5-G7 attribute (writers / fallback / alert)는 여전히 본 stage 제외.

    ⚠️ Production activation 제약 (G4 단계도 유지 — Codex 강조):
        G4 land 후에도 USDT_WS_GOPAX_ENABLED=true 토글 금지. G4 단독은 reconnect/
        liveness만 land되고 Redis/DB/Telemetry 부재라 silent ingestion 상태
        (관찰 불가). production activation은 **최소 G5 Redis writer + PR 2e
        telemetry 이후 검토**. G4는 **local/staging smoke 가능 단계**.
        자세한 내용은 module docstring 참조.
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
        # 병행). cleanup 시점은 G5 또는 별도 PR에서 판단.
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
            # cleanup 시점은 G5 또는 별도 PR에서 판단.
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
        """G4 — ticker freshness status 전이 + counter + log.

        Codex 정정 (G4 scope 명시): degraded 전이는 status 갱신만, fallback hook은
        G6b 영역. 본 G4에서는 `_fallback_controller` attribute 자체가 없다.
        TestTickerFreshnessDegradedNoFallback로 잠금.
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
        # G6b 영역 (fallback hook) 명시적 미진입 — degraded action 없음.

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
        """G4 single session — connect + subscribe + recv + Primus pong + liveness + status.

        Lifecycle (G4 scope):
            1. websockets.connect(GOPAX_WS_URL) + reset_active_session()
            2. SubscribeToTickers payload send + _set_connection_status("normal")
            3. recv loop: wake every RECV_TIMEOUT_SEC (stop_event 반응)
            4. 매 iter: stale check (`_liveness.is_stale(now, STALE_AFTER_SEC)`) →
               connection_status 전이 (stale ↔ normal)
            5. 매 iter: ticker freshness 전이 (now - last_tick_at 기준 warning/degraded/normal)
               — degraded는 status 갱신만, fallback hook은 G6b 영역 (Codex 정정)
            6. Primus ping → pong send + dual-write heartbeat → continue
               (send 실패 시 RuntimeError raise → start() except 경로로 attempt++ + backoff)
            7. 그 외 frame → parse + handle + `_liveness.observe_tick(now)`
            8. ConnectionClosed → raise (return X) → start() except 경로로 attempt++ + backoff
               (return 시 start() else: continue로 tight loop hang)
            9. fanout (Redis/DB/Alert) 없음 — G5-G7 영역

        ⚠️ G4 단독 flag=true는 production 활성화 금지 (Codex 강조):
            reconnect/liveness까지 land됐지만 Redis/DB/Telemetry 부재라 silent
            ingestion (관찰 불가). production activation은 G5 + PR 2e 이후 검토.
            자세한 내용은 module docstring 참조.
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
                    # Codex 정정: degraded는 status 갱신만, fallback hook은 G6b 영역.
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

                    # 그 외 frame → parse + handle. fanout은 G5+ 영역.
                    self._handle_message(raw)
            finally:
                self._ws = None

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

        Production activation 제약 (Codex 강조 — G4 단독 land 후에도 유지):
            G4 land 후에도 USDT_WS_GOPAX_ENABLED=false 유지. production activation은
            **최소 G5 Redis writer + PR 2e telemetry 이후 검토**. G4 단독은
            reconnect/liveness만 land되고 Redis/DB/Telemetry 부재라 silent ingestion
            상태 (관찰 불가). G4는 **local/staging smoke 가능 단계**.
        """
        if self._running:
            logger.debug("[usdt_ws.gopax] 이미 실행 중, 중복 start 무시")
            return
        self._running = True
        logger.info(
            "[usdt_ws.gopax] start (G4 — reconnect loop + 2-signal status + liveness)",
        )
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
            self._running = False
            logger.info(
                "[usdt_ws.gopax] start exited (reconnect_attempts=%d)",
                self._reconnect_attempt_count,
            )

    async def stop(self) -> None:
        """stop signal — recv loop / start wait 모두 풀어줌. idempotent."""
        self._stop_event.set()
