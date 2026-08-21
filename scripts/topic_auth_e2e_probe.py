"""Container-local functional probe for the authenticated topic cutover.

This process runs inside ``exchange-rate-app`` while the dispatcher is briefly
enabled.  It deliberately does not mutate flags.  The host-side
``topic_auth_e2e_canary.py`` owns the bounded ON/OFF transaction and its
independent watchdog.

Secrets are accepted only as two newline-delimited values on stdin.  They are
never placed in argv, output, or exception messages.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Sequence


HTTP_BASE_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000/ws"
USDT_TOPIC = "usdt:krw"
DXY_TOPIC = "dxy:spot"
PREMIUM_TOPICS = (USDT_TOPIC, DXY_TOPIC)


class ProbeFailure(RuntimeError):
    """A secret-free functional canary failure."""


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: Any
    headers: dict[str, str]


@dataclass(frozen=True)
class ProbeTimings:
    frame_timeout_seconds: float = 20.0
    # Bounds the anonymous-silence assertion and includes a text ping/pong
    # round trip. Legacy rates are change-driven, so a second rates frame is
    # valid evidence when present but cannot be a pass requirement.
    anonymous_silence_seconds: float = 15.0
    denied_silence_seconds: float = 1.0


def read_token_pair(stream: Any) -> tuple[str, str]:
    """Read exactly ``premium\nnonpremium`` without echoing either value."""
    lines = stream.read().splitlines()
    if len(lines) != 2:
        raise ProbeFailure("stdin에는 premium/nonpremium 토큰 두 줄이 정확히 필요하다")
    premium, nonpremium = (line.strip() for line in lines)
    if not premium or not nonpremium:
        raise ProbeFailure("premium/nonpremium 토큰은 비어 있을 수 없다")
    if premium == nonpremium:
        raise ProbeFailure("premium/nonpremium 토큰이 같아 권한 경계를 검증할 수 없다")
    return premium, nonpremium


def _decode_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def http_topic_snapshot(topic: str, token: Optional[str]) -> HttpResult:
    query = urllib.parse.urlencode({"topic": topic})
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{HTTP_BASE_URL}/api/v2/topics/snapshot?{query}", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return HttpResult(
                status=response.status,
                body=_decode_json(raw),
                headers={key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return HttpResult(
            status=exc.code,
            body=_decode_json(exc.read()),
            headers={key.lower(): value for key, value in exc.headers.items()},
        )
    except (OSError, urllib.error.URLError) as exc:
        raise ProbeFailure(
            f"REST snapshot transport 실패: {type(exc).__name__}"
        ) from None


def http_legacy_rates() -> HttpResult:
    request = urllib.request.Request(f"{HTTP_BASE_URL}/api/rates")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return HttpResult(
                status=response.status,
                body=_decode_json(response.read()),
                headers={key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return HttpResult(
            status=exc.code,
            body=_decode_json(exc.read()),
            headers={key.lower(): value for key, value in exc.headers.items()},
        )
    except (OSError, urllib.error.URLError) as exc:
        raise ProbeFailure(
            f"legacy REST transport 실패: {type(exc).__name__}"
        ) from None


def validate_legacy_rest(result: HttpResult) -> None:
    if result.status != 200:
        raise ProbeFailure(f"익명 legacy /api/rates가 200이 아니다(status={result.status})")
    if not isinstance(result.body, dict):
        raise ProbeFailure("legacy /api/rates 응답이 JSON 객체가 아니다")
    rates = result.body.get("rates")
    metadata = result.body.get("metadata")
    if not isinstance(rates, list) or not rates:
        raise ProbeFailure("legacy /api/rates의 rates가 비어 있다")
    if any(not isinstance(row, dict) for row in rates):
        raise ProbeFailure("legacy /api/rates에 객체가 아닌 행이 있다")
    if not isinstance(metadata, dict) or metadata.get("total_count") != len(rates):
        raise ProbeFailure("legacy /api/rates metadata가 rates와 일치하지 않는다")


def admin_tether_subscriber_count() -> int:
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        raise ProbeFailure("ADMIN_PASSWORD가 없어 registry를 검증할 수 없다")
    credential = base64.b64encode(f"admin:{password}".encode()).decode()
    request = urllib.request.Request(
        f"{HTTP_BASE_URL}/admin/api/topic-status",
        headers={"Authorization": f"Basic {credential}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = _decode_json(response.read())
    except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise ProbeFailure(
            f"registry telemetry 조회 실패: {type(exc).__name__}"
        ) from None
    if not isinstance(body, dict):
        raise ProbeFailure("registry telemetry가 JSON 객체가 아니다")
    count = body.get("subscribed_connection_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ProbeFailure("registry telemetry의 subscriber count가 유효하지 않다")
    return count


def _validate_snapshot_envelope(frame: Any, topic: str) -> dict[str, Any]:
    if not isinstance(frame, dict):
        raise ProbeFailure(f"{topic} snapshot이 JSON 객체가 아니다")
    if frame.get("type") != "snapshot" or frame.get("topic") != topic:
        raise ProbeFailure(f"{topic} snapshot envelope가 계약과 다르다")
    if frame.get("version") != 1 or not isinstance(frame.get("data"), dict):
        raise ProbeFailure(f"{topic} snapshot version/data가 계약과 다르다")
    return frame["data"]


KST = timedelta(hours=9)


def _require_kst_iso8601(raw, label: str) -> None:
    """계약은 `ISO8601 KST` 다 — timezone-aware 만으로는 계약을 잠그지 못한다.

    ⛔ `+00:00` 은 같은 순간을 나타내지만 wire 표현 계약 위반이다. publisher는 datetime과
       문자열 입력을 모두 KST로 정규화한다. 따라서 다른 offset이 관측되면 배포 이미지 drift,
       우회 producer, 또는 정규화 회귀를 잡아야 한다.
    """
    if not isinstance(raw, str):
        raise ProbeFailure(f"{label}가 문자열이 아니다")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ProbeFailure(f"{label}가 ISO-8601이 아니다") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProbeFailure(f"{label}에 timezone이 없다")
    if parsed.utcoffset() != KST:
        raise ProbeFailure(f"{label}가 KST(+09:00)가 아니다")


def validate_dxy_snapshot(frame: Any) -> None:
    data = _validate_snapshot_envelope(frame, DXY_TOPIC)
    entry = data.get("dxy")
    if not isinstance(entry, dict):
        raise ProbeFailure("dxy:spot snapshot에 dxy entry가 없다")
    rate = entry.get("rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise ProbeFailure("dxy:spot rate가 숫자가 아니다")
    if not math.isfinite(float(rate)) or float(rate) <= 0:
        raise ProbeFailure("dxy:spot rate가 유효한 양수가 아니다")
    _require_kst_iso8601(entry.get("timestamp"), "dxy:spot timestamp")
    if not isinstance(entry.get("source"), str) or not entry["source"]:
        raise ProbeFailure("dxy:spot source가 비어 있다")


def validate_usdt_snapshot(frame: Any) -> None:
    data = _validate_snapshot_envelope(frame, USDT_TOPIC)
    rows = data.get("usdt_krw")
    if not isinstance(rows, list) or not rows:
        raise ProbeFailure("usdt:krw snapshot에 거래소 행이 없다")
    for row in rows:
        if not isinstance(row, dict):
            raise ProbeFailure("usdt:krw 거래소 행이 객체가 아니다")
        if row.get("asset") != "usdt-krw":
            raise ProbeFailure("usdt:krw 거래소 행의 asset이 다르다")
        if not isinstance(row.get("source"), str) or not row["source"]:
            raise ProbeFailure("usdt:krw 거래소 행의 source가 비어 있다")
        rate = row.get("rate")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise ProbeFailure("usdt:krw 거래소 행의 rate가 숫자가 아니다")
        if not math.isfinite(float(rate)) or float(rate) <= 0:
            raise ProbeFailure("usdt:krw 거래소 행의 rate가 유효한 양수가 아니다")
        # ⛔ `timestamp` 는 공통 entry 필수다 — 클라는 이걸로 (source,asset) merge 를 판정한다.
        #    빠지거나 naive 면 merge 자체가 불가능한데, 검사하지 않으면 그 snapshot 이 통과한다.
        #    DXY 쪽과 대칭이어야 한다(비대칭이면 한쪽만 false-green).
        _require_kst_iso8601(row.get("timestamp"), "usdt:krw 거래소 행의 timestamp")
        # optional 이지만 있으면 형식을 잠근다 — same-bucket ordering 계약의 입력이다.
        if row.get("rate_changed_at") is not None:
            _require_kst_iso8601(row["rate_changed_at"], "usdt:krw rate_changed_at")


def _leased_topics(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        raise ProbeFailure("subscription_ack topic container가 배열이 아니다")
    topics = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("topic"), str):
            raise ProbeFailure("subscription_ack topic entry가 유효하지 않다")
        lease_id = entry.get("lease_id")
        duration = entry.get("lease_duration_seconds")
        if not isinstance(lease_id, str) or not lease_id:
            raise ProbeFailure("accepted topic에 lease_id가 없다")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise ProbeFailure("accepted topic의 lease duration이 유효하지 않다")
        topic = entry["topic"]
        if topic in topics:
            raise ProbeFailure("subscription_ack에 중복 topic entry가 있다")
        topics.append(topic)
    return topics


def validate_premium_ack(frame: Any, request_id: str) -> None:
    if not isinstance(frame, dict) or frame.get("type") != "subscription_ack":
        raise ProbeFailure("premium subscribe 종결 프레임이 ack가 아니다")
    if frame.get("request_id") != request_id or frame.get("operation") != "subscribe":
        raise ProbeFailure("premium subscription_ack 상관관계가 어긋났다")
    if frame.get("rejected_topics") != []:
        raise ProbeFailure("premium topic이 거부됐다")
    if frame.get("removed_topics") != []:
        raise ProbeFailure("premium subscribe ack의 removed_topics가 비어 있지 않다")
    if _leased_topics(frame.get("accepted_topics")) != list(PREMIUM_TOPICS):
        raise ProbeFailure("premium accepted_topics가 USDT+DXY lease를 담지 않는다")
    if _leased_topics(frame.get("active_subscriptions")) != sorted(PREMIUM_TOPICS):
        raise ProbeFailure("premium active_subscriptions가 USDT+DXY lease를 담지 않는다")


def validate_nonpremium_ack(frame: Any, request_id: str) -> None:
    if not isinstance(frame, dict) or frame.get("type") != "subscription_ack":
        raise ProbeFailure("nonpremium subscribe 종결 프레임이 ack가 아니다")
    if frame.get("request_id") != request_id or frame.get("operation") != "subscribe":
        raise ProbeFailure("nonpremium subscription_ack 상관관계가 어긋났다")
    if frame.get("accepted_topics") != [] or frame.get("active_subscriptions") != []:
        raise ProbeFailure("nonpremium topic이 active registry에 들어갔다")
    if frame.get("removed_topics") != []:
        raise ProbeFailure("nonpremium subscribe ack의 removed_topics가 비어 있지 않다")
    rejected = frame.get("rejected_topics")
    if rejected != [{"topic": DXY_TOPIC, "error": "premium_required"}]:
        raise ProbeFailure("nonpremium DXY 거부가 premium_required가 아니다")


def validate_legacy_rates(frame: Any) -> None:
    if not isinstance(frame, dict) or frame.get("type") != "rates":
        raise ProbeFailure("legacy v1.2.2 초기 rates frame이 없다")
    data = frame.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("rates"), list):
        raise ProbeFailure("legacy rates frame schema가 유효하지 않다")
    rows = data["rates"]
    if not rows:
        raise ProbeFailure("legacy rates frame이 비어 있다")
    if any(not isinstance(row, dict) for row in rows):
        raise ProbeFailure("legacy rates frame에 객체가 아닌 행이 있다")


async def _recv_json(ws: Any, timeout: float) -> dict[str, Any]:
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    except asyncio.TimeoutError:
        raise
    except Exception as exc:
        raise ProbeFailure(f"WebSocket receive 실패: {type(exc).__name__}") from None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ProbeFailure("WebSocket frame이 UTF-8이 아니다") from None
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError):
        raise ProbeFailure("WebSocket frame이 JSON이 아니다") from None
    if not isinstance(frame, dict):
        raise ProbeFailure("WebSocket JSON frame이 객체가 아니다")
    return frame


async def _wait_for_legacy_rates(ws: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeFailure("legacy initial rates frame timeout")
        try:
            frame = await _recv_json(ws, remaining)
        except asyncio.TimeoutError:
            raise ProbeFailure("legacy initial rates frame timeout") from None
        if frame.get("type") == "rates":
            validate_legacy_rates(frame)
            return
        raise ProbeFailure("legacy initial rates보다 다른 frame이 먼저 도착했다")


async def probe_anonymous_ws(
    connect: Callable[[], Any],
    admin_count: Callable[[], int],
    timings: ProbeTimings,
) -> dict[str, Any]:
    before = await asyncio.to_thread(admin_count)
    if before != 0:
        raise ProbeFailure(
            f"익명 canary 시작 전 registry가 비어 있지 않다(count={before})"
        )
    async with connect() as ws:
        await _wait_for_legacy_rates(ws, timings.frame_timeout_seconds)
        await ws.send(json.dumps({"type": "subscribe", "topics": [USDT_TOPIC]}))
        await ws.send("ping")
        deadline = time.monotonic() + timings.anonymous_silence_seconds
        saw_pong = False
        saw_live_rates = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = await _recv_json(ws, remaining)
            except asyncio.TimeoutError:
                break
            if frame.get("type") == "pong":
                saw_pong = True
            elif frame.get("type") == "rates":
                validate_legacy_rates(frame)
                saw_live_rates = True
            else:
                raise ProbeFailure("익명 subscribe가 예상 밖 frame을 받았다")
        if not saw_pong:
            raise ProbeFailure("익명 침묵 중 ping/pong 생존 증거가 없다")
        during = await asyncio.to_thread(admin_count)
        if during != 0:
            raise ProbeFailure(
                f"익명 subscribe가 registry에 등록됐다(count={during})"
            )
    return {
        "legacy_initial_rates": True,
        "legacy_updates_seen": int(saw_live_rates),
        "pong": True,
        "registry_count": 0,
    }


async def _wait_for_ack(ws: Any, request_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeFailure("subscription_ack timeout")
        try:
            frame = await _recv_json(ws, remaining)
        except asyncio.TimeoutError:
            raise ProbeFailure("subscription_ack timeout") from None
        if frame.get("type") == "rates":
            validate_legacy_rates(frame)
            continue
        if frame.get("type") == "snapshot":
            raise ProbeFailure("subscription_ack보다 snapshot이 먼저 도착했다")
        if frame.get("request_id") == request_id and frame.get("type") in {
            "subscription_ack", "subscription_error"
        }:
            return frame


async def probe_nonpremium_ws(
    connect: Callable[[], Any], token: str, timings: ProbeTimings
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    async with connect() as ws:
        await _wait_for_legacy_rates(ws, timings.frame_timeout_seconds)
        await ws.send(json.dumps({
            "type": "subscribe",
            "request_id": request_id,
            "topics": [DXY_TOPIC],
            "id_token": token,
        }))
        ack = await _wait_for_ack(ws, request_id, timings.frame_timeout_seconds)
        validate_nonpremium_ack(ack, request_id)
        deadline = time.monotonic() + timings.denied_silence_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                frame = await _recv_json(ws, remaining)
            except asyncio.TimeoutError:
                break
            if frame.get("type") == "rates":
                validate_legacy_rates(frame)
                continue
            raise ProbeFailure("nonpremium 거부 뒤 예상 밖 frame을 받았다")
    return {"rejected": {DXY_TOPIC: "premium_required"}, "active": []}


async def probe_premium_ws(
    connect: Callable[[], Any], token: str, timings: ProbeTimings
) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    async with connect() as ws:
        await _wait_for_legacy_rates(ws, timings.frame_timeout_seconds)
        await ws.send(json.dumps({
            "type": "subscribe",
            "request_id": request_id,
            "topics": list(PREMIUM_TOPICS),
            "id_token": token,
        }))
        ack = await _wait_for_ack(ws, request_id, timings.frame_timeout_seconds)
        validate_premium_ack(ack, request_id)
        snapshots: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + timings.frame_timeout_seconds
        while set(snapshots) != set(PREMIUM_TOPICS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeFailure("premium USDT+DXY snapshot timeout")
            try:
                frame = await _recv_json(ws, remaining)
            except asyncio.TimeoutError:
                raise ProbeFailure("premium USDT+DXY snapshot timeout") from None
            if frame.get("type") == "rates":
                validate_legacy_rates(frame)
                continue
            if frame.get("type") == "snapshot" and frame.get("topic") in PREMIUM_TOPICS:
                snapshots[frame["topic"]] = frame
                continue
            raise ProbeFailure("premium subscribe 뒤 예상 밖 frame을 받았다")
        validate_usdt_snapshot(snapshots[USDT_TOPIC])
        validate_dxy_snapshot(snapshots[DXY_TOPIC])
    return {"accepted": list(PREMIUM_TOPICS), "snapshots": list(PREMIUM_TOPICS)}


async def run_probe(
    premium_token: str,
    nonpremium_token: str,
    *,
    connect: Callable[[], Any],
    request_snapshot: Callable[[str, Optional[str]], HttpResult] = http_topic_snapshot,
    request_legacy: Callable[[], HttpResult] = http_legacy_rates,
    admin_count: Callable[[], int] = admin_tether_subscriber_count,
    timings: ProbeTimings = ProbeTimings(),
) -> dict[str, Any]:
    legacy_rest = await asyncio.to_thread(request_legacy)
    validate_legacy_rest(legacy_rest)

    anonymous_rest = await asyncio.to_thread(request_snapshot, DXY_TOPIC, None)
    if anonymous_rest.status != 401:
        raise ProbeFailure(f"익명 DXY REST가 401이 아니다(status={anonymous_rest.status})")

    anonymous_ws = await probe_anonymous_ws(connect, admin_count, timings)

    nonpremium_rest = await asyncio.to_thread(
        request_snapshot, DXY_TOPIC, nonpremium_token
    )
    if nonpremium_rest.status != 403:
        raise ProbeFailure(
            f"nonpremium DXY REST가 403이 아니다(status={nonpremium_rest.status})"
        )
    nonpremium_ws = await probe_nonpremium_ws(connect, nonpremium_token, timings)

    premium_rest = await asyncio.to_thread(request_snapshot, DXY_TOPIC, premium_token)
    if premium_rest.status != 200:
        raise ProbeFailure(
            f"premium DXY REST가 200이 아니다(status={premium_rest.status})"
        )
    validate_dxy_snapshot(premium_rest.body)
    premium_ws = await probe_premium_ws(connect, premium_token, timings)

    return {
        "ok": True,
        "rest": {
            "legacy_anonymous": 200,
            "anonymous": 401,
            "nonpremium": 403,
            "premium": 200,
        },
        "anonymous_ws": anonymous_ws,
        "nonpremium_ws": nonpremium_ws,
        "premium_ws": premium_ws,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="authenticated topic E2E container probe",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--tokens-stdin",
        action="store_true",
        required=True,
        help="stdin의 premium/nonpremium Firebase token 두 줄",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    build_arg_parser().parse_args(argv)
    try:
        premium_token, nonpremium_token = read_token_pair(sys.stdin)
        import websockets

        def connect():
            return websockets.connect(WS_URL, ping_interval=20, max_size=1 << 20)

        summary = asyncio.run(
            run_probe(
                premium_token,
                nonpremium_token,
                connect=connect,
            )
        )
    except ProbeFailure as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": f"unexpected {type(exc).__name__}",
        }, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
