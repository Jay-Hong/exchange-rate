"""KIS WebSocket old-contract observer — 만기일 silence/체결 패턴 관찰 (#4).

목적 (KRX_CANARY.md §5/8 보강 + 사용자 단일가 종가 통찰):
    5/18 만기일 A75605 (옛 월물) WS frame 패턴 관찰. 운영 client는 07:00 KST에
    A75606으로 swap하므로 A75605는 운영 DB에 안 잡힘. 별도 observer로 11:10~11:45
    동안 A75605 subscribe 유지 → 단일가 호가 접수 (11:20~11:30) + 단일가 체결
    (11:30:00~11:30:59) + 만기 종료 후 silence/disconnect 패턴을 측정.

격리 원칙 (운영 contamination 0):
    - 운영 모듈 import 금지 (app.scheduler / app.notifications / app.database /
      app.cache / app.crawlers.usdt_ws / latest_rates_cache 등)
    - approval_key cache READ-ONLY (`.cache/kis_ws_approval.json`). 새 token 발급 X
    - DB/Redis/alert/scheduler/main 모두 미터치
    - 별도 logger (운영 log file 미오염, stdout/stderr only)
    - JSONL append만 `/tmp/krx_expiry_obs/ws_old_{trade,quote,system}.jsonl`

호가 raw 폭증 차단:
    - H0CFCNT0 (체결, 빈도 낮음): raw payload JSONL 저장
    - H0CFASP0 (호가, 단일가 구간 폭증 가능): 1분 bucket count + first/last/sample only

사용법:
    docker compose exec -T fastapi python scripts/observe_kis_ws_old.py \\
        --short-code A75605 --until 11:45 --wait-approval-cache-until 11:15
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import websockets

# Endpoint (운영 코드 인용: app/crawlers/krx_kis.py:78 — 같은 값 사용)
KIS_WS_URL = "ws://ops.koreainvestment.com:21000/tryitout"

# Approval cache path (운영 코드 인용: app/crawlers/krx_kis.py:82)
# READ-ONLY로만 사용. 새 token 발급 금지.
APPROVAL_CACHE_PATH = Path(".cache/kis_ws_approval.json")

# Subscribe TR (운영 코드 인용: app/sources/kis_futures.py + krx_kis.py:104)
TR_TRADE = "H0CFCNT0"   # 체결 (raw 저장)
TR_QUOTE = "H0CFASP0"   # 호가 (bucket only)

# 출력 디렉토리 (host crontab cron 등록 위치와 동일)
OBS_DIR = Path("/tmp/krx_expiry_obs")

KST = timezone(timedelta(hours=9))

logger = logging.getLogger("observe_kis_ws_old")


# ---------------------------------------------------------------------------
# Approval cache read-only
# ---------------------------------------------------------------------------

def read_approval_cache() -> Optional[str]:
    """Read approval_key from cache. None if missing/expired/invalid.

    Never writes. Never issues new token.
    """
    if not APPROVAL_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(APPROVAL_CACHE_PATH.read_text())
    except Exception:
        return None
    expires_at = data.get("expires_at_epoch")
    if not isinstance(expires_at, (int, float)):
        return None
    if expires_at <= time.time() + 60:  # 1분 미만 남은 token도 skip
        return None
    key = data.get("approval_key")
    if not isinstance(key, str) or not key:
        return None
    return key


async def wait_for_approval_cache(timeout_at: datetime) -> Optional[str]:
    """Polling read until cache exists or timeout. 5s interval. No new issue."""
    while True:
        key = read_approval_cache()
        if key:
            return key
        now = datetime.now(KST)
        if now >= timeout_at:
            return None
        logger.info(
            "[ws_obs] approval cache 미존재 — 5초 후 재시도 (timeout=%s)",
            timeout_at.strftime("%H:%M"),
        )
        await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Frame handling
# ---------------------------------------------------------------------------

class QuoteBucketAggregator:
    """H0CFASP0 호가 frame을 1분 bucket으로 집계 (raw 폭증 차단).

    bucket key: 'YYYY-MM-DDTHH:MM'
    저장: count + first/last frame timestamp + first/mid/last raw (실제 raw).
    """

    def __init__(self, output_path: Path):
        self._output = output_path
        self._buckets: Dict[str, Dict[str, Any]] = {}

    def add(self, tr_id: str, raw_data: str, now_iso: str) -> None:
        bucket_key = now_iso[:16]
        b = self._buckets.setdefault(
            bucket_key,
            {
                "bucket_minute": bucket_key,
                "tr_id": tr_id,
                "count": 0,
                "first_at": now_iso,
                "last_at": now_iso,
                "first_raw": None,
                "mid_raw": None,
                "last_raw": None,  # Codex Medium 1: 매 frame 갱신
            },
        )
        b["count"] += 1
        b["last_at"] = now_iso
        b["last_raw"] = raw_data[:500]
        if b["first_raw"] is None:
            b["first_raw"] = raw_data[:500]
        if b["count"] == 100 and b["mid_raw"] is None:
            b["mid_raw"] = raw_data[:500]

    def flush_before(self, current_bucket_key: str) -> None:
        """Codex Medium 2: incremental flush — current bucket 이전 minute만 flush.

        매 minute boundary에서 호출하여 process kill 시에도 이전 minute 데이터 보존.
        """
        with self._output.open("a") as f:
            for key in sorted(self._buckets):
                if key >= current_bucket_key:
                    continue
                f.write(json.dumps(self._buckets[key], ensure_ascii=False) + "\n")
        # 이전 minute bucket 제거
        self._buckets = {
            k: v for k, v in self._buckets.items() if k >= current_bucket_key
        }

    def flush(self) -> None:
        """전체 flush — graceful close 또는 SIGTERM handler에서 호출."""
        with self._output.open("a") as f:
            for key in sorted(self._buckets):
                f.write(json.dumps(self._buckets[key], ensure_ascii=False) + "\n")
        self._buckets.clear()


def write_jsonl(path: Path, record: Dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# WebSocket loop
# ---------------------------------------------------------------------------

def build_sub_message(approval_key: str, tr_id: str, tr_key: str) -> str:
    """KIS WS subscribe body (운영 코드 인용: krx_kis.py:1323-1333)."""
    return json.dumps({
        "header": {
            "approval_key": approval_key,
            "custtype": "P",
            "tr_type": "1",
            "content-type": "utf-8",
        },
        "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
    })


async def run_observer(
    short_code: str,
    until: datetime,
    approval_key: str,
) -> None:
    """Single WS connect + subscribe + recv loop until `until`."""
    trade_path = OBS_DIR / "ws_old_trade.jsonl"
    quote_path = OBS_DIR / "ws_old_quote.jsonl"
    system_path = OBS_DIR / "ws_old_system.jsonl"

    quote_agg = QuoteBucketAggregator(quote_path)
    trade_count = 0
    quote_count = 0
    system_count = 0

    logger.info(
        "[ws_obs] connect %s — short_code=%s until=%s",
        KIS_WS_URL, short_code, until.strftime("%H:%M:%S"),
    )

    # Codex Medium 2: SIGTERM/SIGINT handler — flush 보장
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("[ws_obs] signal received — graceful close")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, RuntimeError):
            pass  # Windows or restricted env

    last_minute_key = ""

    try:
        async with websockets.connect(
            KIS_WS_URL,
            ping_interval=20,
            ping_timeout=10,
            open_timeout=10,
        ) as ws:
            await ws.send(build_sub_message(approval_key, TR_TRADE, short_code))
            await ws.send(build_sub_message(approval_key, TR_QUOTE, short_code))
            logger.info(
                "[ws_obs] subscribed %s/%s tr_key=%s",
                TR_TRADE, TR_QUOTE, short_code,
            )

            while True:
                if stop_event.is_set():
                    logger.info("[ws_obs] stop_event — break")
                    break
                now = datetime.now(KST)
                if now >= until:
                    logger.info(
                        "[ws_obs] until 도달 (%s) — close. trade=%d quote=%d system=%d",
                        until.strftime("%H:%M:%S"),
                        trade_count, quote_count, system_count,
                    )
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    # Codex Medium 2: minute boundary incremental flush
                    current_bucket = datetime.now(KST).isoformat()[:16]
                    if current_bucket != last_minute_key and last_minute_key:
                        quote_agg.flush_before(current_bucket)
                    last_minute_key = current_bucket
                    continue
                if not isinstance(raw, str):
                    continue

                # Codex High 2: PINGPONG pong 응답 (운영 krx_kis.py:1313-1318 mirror).
                # KIS는 pong 미응답 연결을 일정 시간 후 disconnect함.
                if "PINGPONG" in raw:
                    try:
                        await ws.pong(raw.encode())
                    except Exception as exc:
                        logger.warning(
                            "[ws_obs] pong send failed: %s: %s",
                            type(exc).__name__, exc,
                        )
                    continue

                now_iso = datetime.now(KST).isoformat()

                # Codex Medium 2: minute boundary incremental flush
                current_bucket = now_iso[:16]
                if current_bucket != last_minute_key and last_minute_key:
                    quote_agg.flush_before(current_bucket)
                last_minute_key = current_bucket

                if raw.startswith(("0|", "1|")):
                    # tick frame: "0|TR_ID|count|data"
                    parts = raw.split("|", 3)
                    if len(parts) < 4:
                        write_jsonl(system_path, {
                            "at": now_iso, "kind": "malformed_tick",
                            "raw": raw[:300],
                        })
                        continue
                    tr_id = parts[1]
                    data = parts[3]
                    if tr_id == TR_TRADE:
                        trade_count += 1
                        write_jsonl(trade_path, {
                            "at": now_iso, "tr_id": tr_id,
                            "short_code": short_code, "raw": data,
                        })
                    elif tr_id == TR_QUOTE:
                        quote_count += 1
                        quote_agg.add(tr_id, data, now_iso)
                    else:
                        write_jsonl(system_path, {
                            "at": now_iso, "kind": "unknown_tr",
                            "tr_id": tr_id, "raw": raw[:300],
                        })
                else:
                    # system message (subscribe success / PINGPONG / error)
                    system_count += 1
                    try:
                        msg = json.loads(raw)
                        header = msg.get("header") or {}
                        body = msg.get("body") or {}
                        write_jsonl(system_path, {
                            "at": now_iso, "kind": "system",
                            "tr_id": header.get("tr_id"),
                            "rt_cd": body.get("rt_cd"),
                            "msg_cd": body.get("msg_cd"),
                            "msg1": body.get("msg1"),
                            "raw": raw[:500],
                        })
                    except json.JSONDecodeError:
                        write_jsonl(system_path, {
                            "at": now_iso, "kind": "system_parse_fail",
                            "raw": raw[:500],
                        })
    except Exception as exc:
        logger.exception("[ws_obs] connect/run error: %s", type(exc).__name__)
        write_jsonl(system_path, {
            "at": datetime.now(KST).isoformat(),
            "kind": "exception",
            "type": type(exc).__name__,
            "msg": str(exc)[:300],
        })
    finally:
        quote_agg.flush()
        logger.info(
            "[ws_obs] flushed. trade=%d quote=%d system=%d",
            trade_count, quote_count, system_count,
        )


def parse_time_today(s: str) -> datetime:
    """'HH:MM' -> today's KST datetime."""
    hh, mm = s.split(":")
    now = datetime.now(KST)
    return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(description="KIS WS old-contract observer")
    parser.add_argument("--short-code", required=True, help="e.g. A75605")
    parser.add_argument("--until", default="11:45", help="HH:MM (KST today)")
    parser.add_argument(
        "--wait-approval-cache-until", default="11:15",
        help="HH:MM (KST today). approval cache 없으면 이 시각까지 polling 후 종료",
    )
    args = parser.parse_args()

    OBS_DIR.mkdir(parents=True, exist_ok=True)
    until = parse_time_today(args.until)
    wait_until = parse_time_today(args.wait_approval_cache_until)

    async def _main_async() -> None:
        key = read_approval_cache()
        if not key:
            logger.info(
                "[ws_obs] approval cache 미존재 — wait_until=%s까지 대기 (no new issue)",
                wait_until.strftime("%H:%M"),
            )
            key = await wait_for_approval_cache(wait_until)
            if not key:
                logger.warning(
                    "[ws_obs] approval cache 없음 (wait timeout). 종료 — 운영 client가 발급 후 재시도 필요."
                )
                return
        await run_observer(args.short_code, until, key)

    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        logger.info("[ws_obs] KeyboardInterrupt — close")
    return 0


if __name__ == "__main__":
    sys.exit(main())
