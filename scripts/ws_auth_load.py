"""W canary 전용 **인증 부하** 생성기 — 유계(bounded) · in-flight 1 · 토큰 비노출.

⛔ **기존 subscribe smoke 도구를 부하원으로 쓰면 안 된다.** `subscribe_fx_topic.py` /
`subscribe_tether_topic.py` 는 `id_token` 없이 구독한다(실측 0건). 그런데 dispatcher 는
`id_token is None` 이면 free topic 만 등록하고 **그 자리에서 return** 한다
(`app/topic_dispatcher.py` — "토큰이 실린 요청만 인증 경로를 탄다"). 즉 그 도구로 부하를 주면
인증 executor 를 **한 번도 타지 않고** auth metrics 가 빈 채로 남는다 = **false green**.
→ 여기서는 매 요청에 **유효한 `id_token`** 을 실어 `verify_id_token` 을 실제로 돌린다.

⚠️ `fx:usd-krw` 로도 인증은 돈다 — `authorize_subscribe` 는 gated 여부를 **보기 전에** 실행된다.
   그래서 KRX(유일한 gated topic) 를 건드리지 않고도 인증 부하를 만들 수 있다.

## 안전 장치 (이 도구가 지켜야 하는 것)

⛔ **토큰은 CLI 인자로 받지 않는다.** argv 는 `ps` 로 다른 사용자에게 보이고 셸 히스토리에 남는다.
   stdin(`--token-stdin`) 또는 **권한 제한 파일**(`--token-file`, group/other 읽기 불가)만 받는다.
⛔ **토큰은 어디에도 출력하지 않는다** — 로그·요약·예외 메시지 전부. 길이조차 찍지 않는다.
⛔ **connection 당 in-flight 는 정확히 1개.** 클라가 큐를 무제한으로 쌓으면 서버 queue_wait 이
   클라 큐의 그림자가 되어 **측정 대상이 뒤바뀐다**. 구조로 강제한다 — 종결 프레임을 받기 전에는
   다음 요청을 보내지 않는다(§8-B-term: 식별된 요청은 정확히 하나의 종결 프레임을 받는다).
⚠️ **총 7분**으로 상한이 걸려 있다. lease 상한(15분) 안이라 창 중간 재인증이 필요 없다 —
   그래서 재인증 부하 클라이언트를 따로 만들지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

DEFAULT_URL = "wss://fxi.kr/ws"
LOAD_TOPIC = "fx:usd-krw"          # ⛔ gated 아님 — KRX 를 건드리지 않고 인증만 돌린다.
SUBSCRIBE_TIMEOUT_SECONDS = 15.0   # 서버 wire deadline(10s) 보다 넉넉히

TERMINATING_TYPES = ("subscription_ack", "subscription_error")


# ── 토큰 로딩 (비노출) ──────────────────────────────────────────────────────


class InsecureTokenFile(Exception):
    """권한이 느슨한 토큰 파일 — 읽지 않고 거부한다."""


def load_id_token(*, token_file: Optional[str] = None,
                  stdin_stream: Any = None) -> str:
    """stdin 또는 **권한 제한 파일**에서만 토큰을 읽는다.

    ⛔ CLI 인자 경로는 **존재하지 않는다**(argv 노출). 이 함수에 그런 매개변수를 추가하지 말 것.
    """
    if token_file:
        mode = os.stat(token_file).st_mode
        if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
            raise InsecureTokenFile(
                f"토큰 파일 권한이 느슨하다(group/other 접근 가능): {token_file} — chmod 600 후 재시도"
            )
        with open(token_file, encoding="utf-8") as handle:
            token = handle.read()
    else:
        stream = sys.stdin if stdin_stream is None else stdin_stream
        token = stream.read()
    token = token.strip()
    if not token:
        raise ValueError("토큰이 비어 있다")   # ⛔ 값을 메시지에 넣지 않는다
    return token


def build_arg_parser() -> argparse.ArgumentParser:
    # ⛔ `allow_abbrev=False` — argparse 는 기본적으로 **접두어 축약**을 허용한다. 지금은
    #    `--token-stdin`/`--token-file` 둘 다 있어 `--token` 이 "모호"로 반려되지만, 그건
    #    **우연한 보호**다: 한쪽을 없애는 순간 `--token <값>` 이 조용히 `--token-file` 로 붙는다.
    #    그러면 토큰이 argv 에 남는다(`ps` 노출) — 이 도구가 막으려던 바로 그것이다.
    parser = argparse.ArgumentParser(description="W canary 인증 부하 (bounded)",
                                     allow_abbrev=False)
    parser.add_argument("--url", default=DEFAULT_URL)
    source = parser.add_mutually_exclusive_group(required=True)
    # ⛔ `--token` 은 **의도적으로 없다** — argv 는 `ps` 로 보이고 히스토리에 남는다.
    source.add_argument("--token-stdin", action="store_true", help="stdin 으로 토큰 입력")
    source.add_argument("--token-file", help="chmod 600 파일 경로")
    parser.add_argument("--dry-run", action="store_true",
                        help="연결 없이 실행 계획만 출력")
    return parser


# ── 실행 계획 (유계) ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Phase:
    name: str
    concurrency: int
    seconds: int


#: ⚠️ 총 420초(7분) — lease 상한 900초 **안**이라 창 중간 재인증이 필요 없다.
PHASES: tuple[Phase, ...] = (
    Phase("baseline", 0, 60),
    Phase("c1", 1, 60),
    Phase("c4", 4, 120),
    Phase("c8", 8, 120),
    Phase("observe", 0, 60),
)

LEASE_MAX_SECONDS = 900.0


def total_plan_seconds(phases: Sequence[Phase] = PHASES) -> int:
    return sum(p.seconds for p in phases)


def plan_fits_in_lease(phases: Sequence[Phase] = PHASES) -> bool:
    """⛔ 창이 lease 를 넘으면 중간에 재인증이 필요해지고, 그러면 이 도구의 전제가 깨진다."""
    return total_plan_seconds(phases) < LEASE_MAX_SECONDS


# ── 종결 프레임 판정 (§8-B-term) ────────────────────────────────────────────


def is_terminating_frame(frame: Any, request_id: str) -> bool:
    """내 요청에 대한 **종결** 프레임인가.

    ⚠️ subscribe 직후에는 snapshot·data 프레임이 섞여 온다. 그걸 종결로 오인하면 in-flight 1
    규율이 깨져 다음 요청이 **겹쳐 나간다**(그러면 서버 queue_wait 이 오염된다).
    """
    if not isinstance(frame, dict):
        return False
    if frame.get("type") not in TERMINATING_TYPES:
        return False
    return frame.get("request_id") == request_id


# ── 집계 · 중단 판정 ────────────────────────────────────────────────────────


@dataclass
class LoadStats:
    sent: int = 0
    acked: int = 0
    timeouts: int = 0
    errors_by_code: dict = field(default_factory=dict)
    latency_ms_max: float = 0.0
    latency_ms_sum: float = 0.0

    def record_ack(self, latency_ms: float) -> None:
        self.acked += 1
        self.latency_ms_sum += latency_ms
        self.latency_ms_max = max(self.latency_ms_max, latency_ms)

    def record_error(self, code: str) -> None:
        self.errors_by_code[code] = self.errors_by_code.get(code, 0) + 1

    def summary(self) -> dict:
        return {
            "sent": self.sent, "acked": self.acked, "timeouts": self.timeouts,
            "errors_by_code": dict(self.errors_by_code),
            "latency_ms_max": round(self.latency_ms_max, 1),
            "latency_ms_avg": round(self.latency_ms_sum / self.acked, 1) if self.acked else None,
        }


def evaluate_client_abort(stats: LoadStats) -> list[str]:
    """⛔ 클라 쪽에서 **즉시 중단**해야 하는 조건. 1건이라도 나오면 멈춘다."""
    reasons = []
    if stats.timeouts:
        reasons.append(f"subscribe timeout {stats.timeouts}건")
    if stats.errors_by_code:
        reasons.append(f"예상하지 않은 subscription_error {stats.errors_by_code}")
    return reasons


def evaluate_server_abort(auth_metrics: dict, probe: dict,
                          previous_probe_outstanding: int = 0) -> list[str]:
    """운영자가 admin endpoint 스냅샷을 넣으면 **중단 여부를 판정**한다(순수 함수).

    ⛔ 이 도구는 admin 자격증명을 **다루지 않는다** — 부하 생성기가 운영 비밀을 들고 있을
       이유가 없다. 판정만 여기 두고 값은 운영자가 넣는다.
    """
    reasons = []
    if auth_metrics.get("queue_wait_ms_max", 0) >= 5000:
        reasons.append("auth queue_wait_ms_max >= 5000")
    if auth_metrics.get("never_started", 0) > 0:
        reasons.append("never_started 증가 (계획된 종료 전)")
    if auth_metrics.get("caller_cancelled_while_running", 0) > 0:
        reasons.append("caller_cancelled_while_running 증가 (계획된 종료 전)")
    outstanding = probe.get("outstanding", 0)
    if outstanding >= 1 and previous_probe_outstanding >= 1:
        reasons.append("default probe outstanding=1 2회 연속 (pool 포화)")
    if probe.get("queue_delay_ms_max", 0) >= 1000:
        reasons.append("default executor queue delay >= 1000ms")
    return reasons


# ── worker (in-flight 정확히 1) ─────────────────────────────────────────────


async def run_one_request(ws: Any, token: str, stats: LoadStats,
                          *, topic: str = LOAD_TOPIC,
                          timeout: float = SUBSCRIBE_TIMEOUT_SECONDS,
                          request_id: Optional[str] = None) -> Optional[dict]:
    """요청 1건 — 보내고 **종결 프레임을 받을 때까지** 다음을 보내지 않는다."""
    rid = request_id or str(uuid.uuid4())
    started = time.monotonic()
    stats.sent += 1
    await ws.send(json.dumps({
        "type": "subscribe", "request_id": rid, "topics": [topic], "id_token": token,
    }))

    deadline = started + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stats.timeouts += 1
            return None
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            stats.timeouts += 1
            return None
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            continue                      # 우리 계약 밖 프레임은 무시
        if not is_terminating_frame(frame, rid):
            continue                      # snapshot·data 프레임 — 종결이 아니다
        if frame.get("type") == "subscription_error":
            stats.record_error(str(frame.get("error")))
        else:
            stats.record_ack((time.monotonic() - started) * 1000.0)
        return frame


async def run_worker(connect: Callable[[], Any], token: str, stats: LoadStats,
                     stop_at: float, *, topic: str = LOAD_TOPIC) -> None:
    """연결 하나를 잡고 창이 끝날 때까지 **순차** 요청."""
    async with connect() as ws:
        while time.monotonic() < stop_at:
            await run_one_request(ws, token, stats, topic=topic)
            if evaluate_client_abort(stats):
                return


def render_summary(stats: LoadStats, phase: Phase) -> str:
    """⛔ 토큰은 여기에 **절대 들어오지 않는다** — 인자로도 받지 않는다."""
    return f"[{phase.name}] concurrency={phase.concurrency} {json.dumps(stats.summary())}"


async def run_plan(connect: Callable[[], Any], token: str,
                   *, phases: Sequence[Phase] = PHASES,
                   emit: Callable[[str], None] = print) -> LoadStats:
    stats = LoadStats()
    for phase in phases:
        stop_at = time.monotonic() + phase.seconds
        if phase.concurrency:
            await asyncio.gather(*[
                run_worker(connect, token, stats, stop_at) for _ in range(phase.concurrency)
            ])
        else:
            await asyncio.sleep(phase.seconds)
        emit(render_summary(stats, phase))
        aborts = evaluate_client_abort(stats)
        if aborts:
            emit(f"[ABORT] {aborts}")
            return stats
    return stats


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not plan_fits_in_lease():
        print("[BLOCKED] 실행 계획이 lease 상한을 넘는다", file=sys.stderr)
        return 2
    if args.dry_run:
        for phase in PHASES:
            print(f"  {phase.name:9} concurrency={phase.concurrency} {phase.seconds}s")
        print(f"  총 {total_plan_seconds()}s (lease 상한 {LEASE_MAX_SECONDS:.0f}s 안)")
        return 0

    token = load_id_token(token_file=args.token_file)          # stdin 은 기본 경로
    import websockets                                          # 부하 실행 시에만 필요

    def connect():
        return websockets.connect(args.url, ping_interval=20)

    stats = asyncio.run(run_plan(connect, token))
    return 1 if evaluate_client_abort(stats) else 0


if __name__ == "__main__":
    raise SystemExit(main())
