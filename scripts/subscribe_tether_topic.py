#!/usr/bin/env python3
"""Dev/test tether topic subscriber (PR Z-2b Level 3 시험 도구, 2026-05-11).

wss://fxi.kr/ws 연결 → `{"type":"subscribe","topics":["usdt:krw"]}` 전송 → 수신
payload를 LEGACY / TOPIC / RAW로 분류해 출력 → timeout 후 unsubscribe.

용도: TOPIC_DISPATCHER_ENABLED=true 짧은 시험 시 실제 publish path 검증.
운영 영향 0 (구독자 측, 서버 측은 publish_topic 호출 발화).

분류:
    - "ping" 응답 (type=pong): 무시
    - type=snapshot + version=1 + data.usdt_krw → TOPIC (테더 탭 snapshot)
    - 그 외 (type=rates 등 legacy broadcast) → LEGACY
    - JSON 파싱 실패 또는 알 수 없는 형식 → RAW

사용:
    python scripts/subscribe_tether_topic.py
    python scripts/subscribe_tether_topic.py --timeout 600
    python scripts/subscribe_tether_topic.py --url wss://fxi.kr/ws --topic usdt:krw
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Tuple

import websockets


def classify_message(raw: str) -> Tuple[str, dict]:
    """수신 메시지 분류.

    Returns:
        (kind, parsed_dict) — kind는 "TOPIC" / "LEGACY" / "RAW" / "PONG"
        parsed_dict는 JSON 파싱 성공 시 dict, 실패 시 빈 dict
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ("RAW", {})

    if not isinstance(data, dict):
        return ("RAW", {})

    t = data.get("type")
    if t == "pong":
        return ("PONG", data)

    # topic snapshot 판정: type=snapshot + version=1 + data.usdt_krw 존재
    if (
        t == "snapshot"
        and data.get("version") == 1
        and isinstance(data.get("data"), dict)
        and "usdt_krw" in data["data"]
    ):
        return ("TOPIC", data)

    return ("LEGACY", data)


async def run(url: str, timeout: int, topic: str, recv_timeout: float = 5.0) -> int:
    """subscriber 실행. 반환값: 정상=0, 오류=1.

    Args:
        recv_timeout: ws.recv 한 번에 대기할 최대 시간 (초). 테스트에서 짧게 (0.01)
            지정하면 wait_for 차단 시간을 줄여 단위 테스트 빨라짐.
    """
    try:
        async with websockets.connect(url, ping_interval=20) as ws:
            # [CONNECTED]는 실제 connect 성공 후 출력 (실패 시 헷갈림 방지, Codex 권고)
            print(f"[CONNECTED] url={url}")

            # subscribe 메시지 전송
            sub_msg = json.dumps({"type": "subscribe", "topics": [topic]})
            await ws.send(sub_msg)
            print(f"[SUBSCRIBED] topic={topic} timeout={timeout}s")

            topic_count = 0
            legacy_count = 0
            raw_count = 0
            deadline = time.monotonic() + timeout

            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    print("[CONNECTION_CLOSED]")
                    break

                kind, data = classify_message(msg)
                if kind == "PONG":
                    continue
                if kind == "TOPIC":
                    topic_count += 1
                    data_keys = list(data.get("data", {}).keys())
                    print(f"[TOPIC] keys={data_keys}")
                elif kind == "LEGACY":
                    legacy_count += 1
                    data_keys = list(data.get("data", {}).keys())
                    print(f"[LEGACY] type={data.get('type')} keys={data_keys[:5]}")
                else:  # RAW
                    raw_count += 1
                    preview = msg[:100] if isinstance(msg, str) else str(msg)[:100]
                    print(f"[RAW] {preview!r}")

            # cleanup: unsubscribe 시도 (best-effort)
            try:
                unsub_msg = json.dumps({"type": "unsubscribe", "topics": [topic]})
                await ws.send(unsub_msg)
                print(f"[UNSUBSCRIBED] topic={topic}")
            except Exception:
                # 이미 닫혔으면 무시
                pass

            print(f"[SUMMARY] topic={topic_count} legacy={legacy_count} raw={raw_count}")
            return 0
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Tether topic subscriber (dev/test 시험 도구)"
    )
    ap.add_argument("--url", default="wss://fxi.kr/ws", help="WebSocket URL")
    ap.add_argument("--timeout", type=int, default=600,
                    help="subscriber 실행 시간 (초). default 600초 (10분)")
    ap.add_argument("--topic", default="usdt:krw", help="구독 topic 이름")
    args = ap.parse_args()
    return asyncio.run(run(args.url, args.timeout, args.topic))


if __name__ == "__main__":
    import sys
    sys.exit(main())
