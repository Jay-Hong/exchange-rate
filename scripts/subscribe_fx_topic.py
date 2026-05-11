"""Dev/test FX topic subscriber (PR Z-2c Step 5 smoke 도구, 2026-05-12).

3개 FX topic(fx:usd-krw, fx:jpy-krw, fx:eur-krw)을 동시 구독하고 수신 payload의
schema invariant를 검증한다. subscribe_tether_topic.py와 별개 — tether는
single-topic(usdt:krw) + `data.usdt_krw` 키로 판정해서 FX shape(`data.banks`)와
호환 안 됨.

사용 예:
    python scripts/subscribe_fx_topic.py
    python scripts/subscribe_fx_topic.py --timeout 120
    python scripts/subscribe_fx_topic.py --url wss://fxi.kr/ws --timeout 60

검증 invariant (USDT_PHASE1_CLIENT_GUIDE.md "FX topic schema" 잠금):
    - payload top-level keys ⊇ {"type", "version", "topic", "data"}
    - payload["type"] == "snapshot"
    - payload["version"] == 1
    - payload["topic"] ∈ {"fx:usd-krw", "fx:jpy-krw", "fx:eur-krw"}
    - "banks" in payload["data"] (Required, list)
    - "reference" in payload["data"]는 Optional
    - 각 entry keys == {"source", "asset", "rate", "timestamp"}
    - entry에 "display_name" 없음
    - entry["asset"] == topic의 asset 부분

운영 영향 0 (구독자 측). 서버 측 영향은 fx publish 호출 발화뿐 (FF=true일 때).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any, Dict, List, Tuple

import websockets


FX_TOPICS: Tuple[str, ...] = ("fx:usd-krw", "fx:jpy-krw", "fx:eur-krw")
EXPECTED_TOP_KEYS = {"type", "version", "topic", "data"}
EXPECTED_ENTRY_KEYS = {"source", "asset", "rate", "timestamp"}


def _validate_payload(payload: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """payload schema 검증. (ok, problems_list) 반환."""
    problems: List[str] = []

    keys = set(payload.keys())
    missing = EXPECTED_TOP_KEYS - keys
    if missing:
        problems.append(f"missing top-level keys: {sorted(missing)}")

    if payload.get("type") != "snapshot":
        problems.append(f"type != snapshot (got {payload.get('type')!r})")
    if payload.get("version") != 1:
        problems.append(f"version != 1 (got {payload.get('version')!r})")

    topic = payload.get("topic")
    if topic not in FX_TOPICS:
        problems.append(f"topic not in FX_TOPICS (got {topic!r})")

    data = payload.get("data")
    if not isinstance(data, dict):
        problems.append(f"data is not dict (got {type(data).__name__})")
        return (False, problems)

    if "banks" not in data:
        problems.append("data.banks key absent (Required)")
    elif not isinstance(data["banks"], list):
        problems.append(f"data.banks not list (got {type(data['banks']).__name__})")

    # asset prefix 추출 (topic == "fx:<asset>")
    asset = topic.split(":", 1)[1] if topic and ":" in topic else None

    # bank entries 검증
    for i, entry in enumerate(data.get("banks", [])):
        if not isinstance(entry, dict):
            problems.append(f"banks[{i}] not dict")
            continue
        entry_keys = set(entry.keys())
        if entry_keys != EXPECTED_ENTRY_KEYS:
            problems.append(f"banks[{i}] keys != {EXPECTED_ENTRY_KEYS} (got {sorted(entry_keys)})")
        if "display_name" in entry:
            problems.append(f"banks[{i}] has forbidden display_name")
        if asset and entry.get("asset") != asset:
            problems.append(f"banks[{i}].asset != {asset!r} (got {entry.get('asset')!r})")

    # reference 검증 (Optional)
    if "reference" in data:
        ref = data["reference"]
        if not isinstance(ref, dict):
            problems.append(f"data.reference not dict (got {type(ref).__name__})")
        else:
            ref_keys = set(ref.keys())
            if ref_keys != EXPECTED_ENTRY_KEYS:
                problems.append(f"reference keys != {EXPECTED_ENTRY_KEYS} (got {sorted(ref_keys)})")
            if "display_name" in ref:
                problems.append("reference has forbidden display_name")
            if ref.get("source") != "investing":
                problems.append(f"reference.source != investing (got {ref.get('source')!r})")
            if asset and ref.get("asset") != asset:
                problems.append(f"reference.asset != {asset!r} (got {ref.get('asset')!r})")

    return (not problems, problems)


def _classify(raw: str) -> Tuple[str, Dict[str, Any]]:
    """수신 메시지 분류. (kind, parsed) — kind: FX_TOPIC / LEGACY / PONG / RAW."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ("RAW", {})

    if not isinstance(data, dict):
        return ("RAW", {})

    if data.get("type") == "pong":
        return ("PONG", data)

    # FX topic 판정: top-level topic 필드가 fx: prefix
    topic = data.get("topic")
    if isinstance(topic, str) and topic in FX_TOPICS:
        return ("FX_TOPIC", data)

    return ("LEGACY", data)


async def run(url: str, timeout: int, recv_timeout: float = 5.0) -> int:
    counts: Dict[str, int] = {t: 0 for t in FX_TOPICS}
    legacy_count = 0
    raw_count = 0
    invalid_count = 0
    all_problems: List[Tuple[str, List[str]]] = []  # (topic, problems)

    try:
        async with websockets.connect(url, ping_interval=20) as ws:
            print(f"[CONNECTED] url={url}")

            sub_msg = json.dumps({"type": "subscribe", "topics": list(FX_TOPICS)})
            await ws.send(sub_msg)
            print(f"[SUBSCRIBED] topics={list(FX_TOPICS)} timeout={timeout}s")

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed:
                    print("[CONNECTION_CLOSED]")
                    break

                kind, payload = _classify(msg)
                if kind == "PONG":
                    continue
                if kind == "FX_TOPIC":
                    topic = payload["topic"]
                    counts[topic] = counts.get(topic, 0) + 1
                    ok, problems = _validate_payload(payload)
                    if not ok:
                        invalid_count += 1
                        all_problems.append((topic, problems))
                        print(f"[FX_TOPIC INVALID] {topic} problems={problems}")
                    else:
                        bank_count = len(payload["data"].get("banks", []))
                        has_ref = "reference" in payload["data"]
                        print(f"[FX_TOPIC OK] {topic} banks={bank_count} reference={has_ref}")
                elif kind == "LEGACY":
                    legacy_count += 1
                    # legacy 표시는 minimal — 첫 5개 키만
                    data_keys = list(payload.get("data", {}).keys())[:5]
                    print(f"[LEGACY] type={payload.get('type')} keys={data_keys}")
                else:  # RAW
                    raw_count += 1
                    preview = msg[:100] if isinstance(msg, str) else str(msg)[:100]
                    print(f"[RAW] {preview!r}")

            try:
                unsub_msg = json.dumps({"type": "unsubscribe", "topics": list(FX_TOPICS)})
                await ws.send(unsub_msg)
                print(f"[UNSUBSCRIBED] topics={list(FX_TOPICS)}")
            except Exception:
                pass

            print("[SUMMARY]")
            total_topic = sum(counts.values())
            for t in FX_TOPICS:
                print(f"  {t}: {counts[t]}")
            print(f"  legacy={legacy_count} raw={raw_count}")
            print(f"  total_fx_topic={total_topic} invalid={invalid_count}")
            if all_problems:
                print("[INVALIDS]")
                for topic, probs in all_problems[:10]:
                    print(f"  {topic}: {probs}")

            # Exit code 분류 (fail-fast — smoke 도구는 "수신 0" silent 통과 차단):
            #   0 = 모든 FX_TOPICS 1+ 수신 + invalid 0
            #   1 = connection ERROR (except 분기)
            #   2 = 수신 있으나 invalid payload 발생
            #   3 = total FX topic 수신 0 (FF 활성화 실패 / subscribe 미도달 의심)
            #   4 = 일부 topic 수신 0 (broadcast 변동 부족 / publisher 일부 실패)
            if total_topic == 0:
                print("[FAIL] no FX topic payload received "
                      "(check FX_TOPIC_ENABLED, subscribe path, broadcast change)")
                return 3
            missing_topics = [t for t in FX_TOPICS if counts[t] == 0]
            if missing_topics:
                print(f"[FAIL] missing topics: {missing_topics} "
                      "(broadcast 변동 부족 또는 publisher 일부 실패 — --timeout 늘려 재시도)")
                return 4
            if invalid_count > 0:
                print(f"[FAIL] {invalid_count} invalid payload(s) detected")
                return 2
            print("[OK] all 3 FX topics received and validated")
            return 0
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="FX topic (fx:usd-krw/jpy-krw/eur-krw) subscriber (dev/test smoke 도구)"
    )
    ap.add_argument("--url", default="wss://fxi.kr/ws", help="WebSocket URL")
    ap.add_argument("--timeout", type=int, default=60,
                    help="subscriber 실행 시간 (초). default 60s")
    ap.add_argument("--recv-timeout", type=float, default=5.0,
                    help="ws.recv 1회 최대 대기 (초). default 5.0s")
    args = ap.parse_args()
    return asyncio.run(run(args.url, args.timeout, recv_timeout=args.recv_timeout))


if __name__ == "__main__":
    import sys
    sys.exit(main())
