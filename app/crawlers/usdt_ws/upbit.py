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
    - **MAX_PENDING_WRITES guard**: Redis 장애 시 task 폭증 방지 (20 in-flight 한도).
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

from app import latest_rates_cache

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
MAX_PENDING_WRITES = 20   # task 폭증 안전망 (Redis 장애 시)
REDIS_CLOSE_TIMEOUT_SEC = 1.0  # close() drain timeout — 후 강제 cancel

# DB writer
DB_WRITE_WINDOW_SEC = 1.0  # debounce window (KRX KrxDbWriter 동일)


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

    def schedule(self, tick: dict) -> None:
        """tick → background Redis write task. fire-and-forget.

        Saturation 시 (`len(_tasks) >= MAX_PENDING_WRITES`) skip + warning.
        Redis 장애로 task가 누적되는 시나리오 차단.
        """
        if len(self._tasks) >= MAX_PENDING_WRITES:
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
        """
        async with self._write_lock:
            ts_iso = datetime.fromtimestamp(tick["timestamp_ms"] / 1000, tz=_KST).isoformat()
            try:
                success = await asyncio.to_thread(
                    latest_rates_cache.set_latest_usdt_rate_from_sync_job,
                    source=tick["source"],
                    asset=tick["asset"],
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
                    # valid ticker만 liveness activity + Redis/DB write schedule.
                    # invalid/non-ticker frame은 모두 X (Codex review).
                    tick = self._handle_message(raw)
                    if tick is not None:
                        self._liveness.observe_tick(time.time())
                        self._redis_writer.schedule(tick)
                        self._db_writer.schedule(tick)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("[usdt_ws.upbit] ping_task cleanup 실패")
                # close 순서 = DB → Redis (Codex review):
                # 1. DB가 source of truth (cache는 derived) — DB Tn 확정 후
                #    Redis를 동기화하는 방향이 일반 원칙.
                # 2. `await to_thread` 동안 event loop 양보 → 대기 중인 Redis
                #    background tasks가 동시에 진행 → Redis backlog 소진 ↑ →
                #    Redis drain 후 Tn까지 reach 확률 ↑.
                # PR5: DB writer timer cancel + pending tick 즉시 flush.
                try:
                    await self._db_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.upbit] db_writer.close() 실패")
                # PR4: pending Redis writes drain (timeout 후 cancel).
                # DB 마지막 tick 확정 후 마지막 chance로 Redis latest 동기화.
                try:
                    await self._redis_writer.close()
                except Exception:
                    logger.exception("[usdt_ws.upbit] redis_writer.close() 실패")
