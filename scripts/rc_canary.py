"""RC canary — RevenueCat 지연·응답 분류**만** 측정한다 (R-GATE-1 활성화 전 사이징).

관찰 창(flag-off)은 정상상태 RC 지배항인 lease 갱신 재구독을 구조적으로 못 본다 — lease
발급이 flag gate 뒤에 있기 때문이다. 그래서 사이징은 이 격리 canary 가 맡는다.

## 계약 (외부 검토로 좁혀진 범위 — 넓히지 말 것)

- **premium-only plan 의 정확한 모양을 강제**한다: `uid` 일치 · `premium_only == (CANARY_TOPIC,)`
  · 나머지 partition 전부 비어 있음. planner drift 가 다른 UID/topic 을 돌려줘도 **호출 0회로
  중단**한다 — 실제 RC 조회는 `plan.uid` 로 나가므로 여기가 어긋나면 다른 사용자를 조회하고도
  잘못된 provenance 를 남긴다.
- **KRX 배제** — entitlement DB read 가 섞이면 순수 RC 지연이 아니다.
- **격리 프로세스** — dispatcher·rollout counter·registry·lease 를 import 하지 않는다
  (직접 import 표면은 테스트가 AST 로 잠근다. ⚠️ 전이 import 까지 보장하는 것은 아니다 —
  `app.topic_authorization` 의 의존은 config·clock·subscription 뿐임을 검토로 확인했다).
- **측정 범위는 RC 왕복뿐** — Firebase·WS deadline·lease 발급·재연결 폭주는 여기 없다.
- **프로세스 내부 deadline 은 협력적**이다 — iterations ≤ 60(hard cap) · cadence ≥ 1s ·
  per-call timeout ≤ 60s · 전체 runtime ≤ 1800s. 세 값 모두 **유한성**을 검증하고(NaN 은
  비교식을 조용히 통과한다 — 실측), 남은 deadline 이 per-call timeout 보다 작으면 그만큼만
  기다린다. 강제 상한과 host 전체 동시성 1은 runner가 맡는다.
- **비밀·예외 문자열 무출력** — provider는 원래 UID·전송 예외를 logger에 남기므로 격리
  프로세스의 logging을 호출 전에 전역 차단한다. 산출물에는 예외 **타입 이름**만. UID도 raw
  대신 **fingerprint**(sha256 앞 16자리)로 남긴다.
- **산출물 무결성** — exclusive create(`open 'x'`) 로 artifact·sidecar 모두 덮어쓰기가
  구조적으로 불가능하다. 이름은 random nonce를 써 컨테이너 간 PID 중복과 무관하다.

## 실행 계약 (운영)

⛔ 운영 이미지에는 이 스크립트가 없다 — `scripts/` 는 빌드 시 복사되고 bind mount 가 아니다.
   재빌드·`--force-recreate` 는 **관찰 창을 리셋**하므로 금지. 실행은 검증된 host-side runner
   `ops/run_rc_canary.sh` 가 맡는다: 스크립트 tracked/clean/upstream 검증 → 출력 디렉터리
   **선생성**(없으면 docker 가 root:root 로 만들어 appuser 가 못 쓴다 — 실측) → `.env` 전체가
   아니라 **RC 키 한 줄만** 필터해 전달 → 실행 중 컨테이너의 pinned 이미지에 read-only bind →
   **process-level outer timeout**(아래 deadline 한계 보완) → image ID 를 provenance 로 주입.

⚠️ deadline 은 **협력적(cooperative)** 이다 — `asyncio.wait_for` 의 취소를 삼키는 callee 는
   상한을 넘길 수 있다(실측: cap 1s 에 1.37s). 초과는 산출물에 `cooperative_deadline_overrun`
   으로 자기보고되고, 강제 종료는 runner의 `timeout` 뒤 **named-container cleanup**이 맡는다.

⚠️ 실행 전 전용 UID(운영 사용자 금지)가 정해져야 한다. UID는 runner의 0600 env 파일로만
   전달하며 Docker argv에는 싣지 않는다.
"""
import argparse
import asyncio
import datetime
import hashlib
import json
import logging
import math
import os
import pathlib
import re
import secrets
import statistics
import sys
import time

# ⛔ hard cap — argparse 로 올릴 수 없다. 외부 quota 보호가 목적이므로 상수로 박는다.
MAX_ITERATIONS = 60
MIN_CADENCE_SECONDS = 1.0
MAX_CALL_TIMEOUT_SECONDS = 60.0
MAX_RUNTIME_SECONDS_CAP = 1800.0
CANARY_TOPIC = "fx:usd-krw"  # 정책표의 PREMIUM_ONLY canonical topic. KRX 는 절대 넣지 않는다.


class CanaryContractError(RuntimeError):
    """전제(plan 모양, RC 설정, 시간 박스)가 무너지면 호출 0회로 중단한다."""


_HEX_40 = re.compile(r"[0-9a-f]{40}")
_HEX_64 = re.compile(r"[0-9a-f]{64}")


def _require_finite(name: str, value: float, low: float, high: float) -> None:
    """⛔ 단방향 `<` 비교는 NaN 을 조용히 통과시킨다(실측). **양방향 range** 비교는
    NaN/inf 모두에서 False 가 되므로 이 한 검사가 유한성까지 함께 잠근다 — 별도
    isfinite 분기를 두면 무하중 중복이 된다(변이 생존 실측)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanaryContractError(f"{name} 는 수여야 한다: {value!r}")
    if not low <= value <= high:
        raise CanaryContractError(f"{name} 는 {low}..{high} 이어야 한다: {value!r}")


def require_rc_config(getenv=os.getenv) -> None:
    """RC 자격증명이 없으면 **한 번도 호출하지 않고** 중단한다. 값은 출력하지 않는다."""
    if not getenv("REVENUECAT_API_KEY", ""):
        raise CanaryContractError("REVENUECAT_API_KEY 가 비어 있다 — canary 를 시작하지 않는다")


def suppress_runtime_logging() -> None:
    """provider가 raw UID·전송 URL을 stderr에 남기지 못하게 격리 프로세스 로그를 끈다.

    canary의 결과 채널은 최종 JSON artifact와 비식별 stdout 요약뿐이다. 운영 앱 프로세스가
    아니라 one-shot 격리 프로세스이므로 전역 disable이 의도된 최소 표면이다.
    """
    logging.disable(logging.CRITICAL)


def require_runner_context(max_runtime_seconds: float, getenv=os.getenv) -> None:
    """정상 runner가 주입하는 provenance 필드의 형식을 강제한다.

    이 값들은 인증 토큰이 아니므로 환경을 직접 위조하는 악성 호출자를 막는 장치는 아니다.
    정상 운영 경로의 누락·형식 drift를 호출 전에 거부하는 계약이다.
    """
    image_id = getenv("CANARY_IMAGE_ID", "")
    source_head = getenv("CANARY_RUNNER_SOURCE_HEAD", "")
    runner_sha = getenv("CANARY_RUNNER_SHA256", "")
    raw_outer_timeout = getenv("CANARY_OUTER_TIMEOUT_SECONDS", "")
    if not image_id.startswith("sha256:") or not _HEX_64.fullmatch(image_id[7:]):
        raise CanaryContractError("CANARY_IMAGE_ID가 pinned image 형식이 아니다")
    if not _HEX_40.fullmatch(source_head):
        raise CanaryContractError("CANARY_RUNNER_SOURCE_HEAD가 full commit SHA가 아니다")
    if not _HEX_64.fullmatch(runner_sha):
        raise CanaryContractError("CANARY_RUNNER_SHA256이 sha256 형식이 아니다")
    try:
        outer_timeout = float(raw_outer_timeout)
    except ValueError:
        raise CanaryContractError("CANARY_OUTER_TIMEOUT_SECONDS가 수가 아니다") from None
    if not math.isfinite(outer_timeout) or outer_timeout <= max_runtime_seconds:
        raise CanaryContractError("outer timeout이 cooperative runtime보다 크지 않다")


def validate_run_parameters(
    uid: str,
    iterations: int,
    cadence_seconds: float,
    call_timeout_seconds: float,
    max_runtime_seconds: float,
) -> None:
    """CLI와 직접 함수 호출이 공유하는 무-I/O 입력 검증."""
    if not isinstance(uid, str) or not uid:
        raise CanaryContractError("전용 UID 가 필요하다")
    if not isinstance(iterations, int) or not 1 <= iterations <= MAX_ITERATIONS:
        raise CanaryContractError(f"iterations 는 1..{MAX_ITERATIONS} 이어야 한다: {iterations!r}")
    _require_finite("cadence_seconds", cadence_seconds, MIN_CADENCE_SECONDS, 3600.0)
    _require_finite("call_timeout_seconds", call_timeout_seconds, 0.1, MAX_CALL_TIMEOUT_SECONDS)
    _require_finite("max_runtime_seconds", max_runtime_seconds, 1.0, MAX_RUNTIME_SECONDS_CAP)


def build_premium_only_plan(uid: str):
    """ENFORCE stage planner 로 plan 을 만들고 **정확한 모양**을 강제한다.

    ⛔ `requires_premium()` 같은 성질 검사만으로는 부족하다 — planner 가 다른 UID 나 다른
       premium topic 을 돌려줘도 통과한다(실측). RC 조회는 `plan.uid` 로 나가므로 모양
       전체를 대조한다.
    """
    from app.config import TopicAuthStage
    from app.topic_policy import plan_authenticated

    plan = plan_authenticated(
        [CANARY_TOPIC], stage=TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM, uid=uid
    )
    if plan.uid != uid:
        raise CanaryContractError(
            "plan.uid 가 요청 UID 와 다르다 — 다른 사용자를 조회하게 된다"
        )
    if plan.premium_only != (CANARY_TOPIC,):
        raise CanaryContractError(
            f"premium_only partition 이 정확히 ({CANARY_TOPIC!r},) 가 아니다"
        )
    if plan.identity_only or plan.premium_and_entitlement:
        raise CanaryContractError(
            "identity/KRX partition 이 섞였다 — 순수 RC 측정이 아니다"
        )
    # ⛔ `requires_premium()/requires_entitlement()` 성질 backstop 은 두지 않는다 — 위
    #    exact-shape 검사 3개가 그 성질을 **함의**하므로 무하중 중복이고, 오히려 KRX 분기의
    #    변이를 가려 하중 측정을 왜곡했다(생존 실측).
    return plan


def classify(result) -> str:
    """운영 verdict 타입으로만 분류한다. 예외 문자열은 어디에도 싣지 않는다."""
    from app.topic_authorization import AuthorizationOutcome, Denied, PremiumGranted, Unavailable

    if isinstance(result, Unavailable):
        return f"unavailable_{result.kind.value}"
    if isinstance(result, AuthorizationOutcome):
        if isinstance(result.premium, PremiumGranted):
            return "premium_granted"
        if isinstance(result.premium, Denied):
            return f"denied_{result.premium.error}"
        return "outcome_without_premium_observation"
    return f"unexpected_{type(result).__name__}"


def _nearest_rank(latencies, p):
    """nearest-rank percentile (정렬된 입력). p50 은 median 을 따로 쓴다."""
    if not latencies:
        return None
    index = max(0, math.ceil(p / 100.0 * len(latencies)) - 1)
    return latencies[index]


async def run_canary(
    *,
    uid: str,
    iterations: int,
    cadence_seconds: float,
    call_timeout_seconds: float,
    max_runtime_seconds: float,
    authorize=None,
    sleeper=asyncio.sleep,
    mono=time.monotonic,
) -> dict:
    """호출 완료 뒤 cadence를 두고 순차 실행한다. 내부 deadline은 cooperative다."""
    validate_run_parameters(
        uid, iterations, cadence_seconds, call_timeout_seconds, max_runtime_seconds
    )

    plan = build_premium_only_plan(uid)
    if authorize is None:
        from app.topic_authorization import authorize_subscription_plan
        authorize = authorize_subscription_plan

    started_utc = datetime.datetime.now(datetime.timezone.utc)
    run_start = mono()
    calls = []
    aborted_reason = None
    for seq in range(iterations):
        if seq:
            remaining = max_runtime_seconds - (mono() - run_start)
            if remaining <= cadence_seconds:
                aborted_reason = "max_runtime_exceeded"
                break
            await sleeper(cadence_seconds)
        remaining = max_runtime_seconds - (mono() - run_start)
        if remaining <= 0:
            aborted_reason = "max_runtime_exceeded"
            break
        # deadline이 per-call timeout보다 가깝다면 그만큼만 기다린다. 단, 취소를 삼키는
        # callee까지 강제 종료하는 상한은 host runner의 container cleanup이 맡는다.
        effective_timeout = min(call_timeout_seconds, remaining)
        t0 = mono()
        try:
            result = await asyncio.wait_for(
                authorize(plan, mono=mono), timeout=effective_timeout
            )
            label = classify(result)
        except asyncio.TimeoutError:
            label = "timeout"
        except Exception as exc:  # ⛔ 타입 이름만 — str(exc) 금지 (자격증명·URL 유출 경로)
            label = f"error_{type(exc).__name__}"
        calls.append(
            {
                "seq": seq,
                "offset_s": round(t0 - run_start, 3),
                "latency_ms": round((mono() - t0) * 1000.0, 2),
                "label": label,
            }
        )

    ended_utc = datetime.datetime.now(datetime.timezone.utc)
    elapsed = mono() - run_start
    # ⛔ wait_for 는 취소를 삼키는 callee 를 강제 종료하지 못한다(실측 1.37s > cap 1s).
    #    초과를 산출물에 자기보고한다 — 강제 kill 은 runner 의 process-level timeout 몫이다.
    if aborted_reason is None and elapsed > max_runtime_seconds:
        aborted_reason = "cooperative_deadline_overrun"
    latencies = sorted(c["latency_ms"] for c in calls)
    counts = {}
    for c in calls:
        counts[c["label"]] = counts.get(c["label"], 0) + 1
    program = pathlib.Path(__file__)
    return {
        "schema": "rc-canary/3",
        "started_utc": started_utc.strftime("%Y%m%dT%H%M%SZ"),
        "ended_utc": ended_utc.strftime("%Y%m%dT%H%M%SZ"),
        "elapsed_s": round(elapsed, 3),
        "image_id": os.getenv("CANARY_IMAGE_ID", "unknown"),
        "runner_source_head": os.getenv("CANARY_RUNNER_SOURCE_HEAD", "unknown"),
        "runner_sha256": os.getenv("CANARY_RUNNER_SHA256", "unknown"),
        "outer_timeout_seconds": os.getenv("CANARY_OUTER_TIMEOUT_SECONDS", "unknown"),
        "uid_fingerprint": hashlib.sha256(uid.encode()).hexdigest()[:16],
        "program_sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
        "params": {
            "iterations_requested": iterations,
            "cadence_seconds": cadence_seconds,
            "call_timeout_seconds": call_timeout_seconds,
            "max_runtime_seconds": max_runtime_seconds,
            "topic": CANARY_TOPIC,
            "concurrency": 1,
        },
        "calls": calls,
        "counts_by_label": counts,
        "latency_ms": {
            "count": len(latencies),
            "p50_median": statistics.median(latencies) if latencies else None,
            "p95_nearest_rank": _nearest_rank(latencies, 95),
            "max": latencies[-1] if latencies else None,
        },
        "aborted_reason": aborted_reason,
        "measures_only": "revenuecat_round_trip",
        "not_measured": ["firebase", "ws_deadline", "lease_issuance", "reconnect_storm"],
    }


def reserve_artifact(output_dir: str, started_utc: str) -> pathlib.Path:
    """RC 를 **한 번도 부르기 전에** 출력 경로를 exclusive create 로 예약한다.

    ⛔ 예약 없이는 quota 를 다 쓴 뒤에야 쓰기 실패·이름 충돌을 발견한다(검토 실측).
    ⛔ 이름의 nonce 는 pid 가 아니다 — one-shot 컨테이너는 PID 가 사실상 상수라(8개 병렬
       중 7개가 PID 7 — 실측) 초 단위 시각+pid 는 컨테이너 간 고유하지 않다.
    """
    directory = pathlib.Path(output_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    except OSError as exc:
        # ⛔ root 소유 디렉터리(docker 자동 생성 실측)에서는 chmod/mkdir 자체가 죽는다 —
        #    unhandled traceback 이 아니라 계약 거부로, RC 호출 전에 끝낸다.
        raise CanaryContractError(
            f"출력 디렉터리 준비 실패 ({type(exc).__name__}) — RC 호출 전에 중단"
        ) from None
    name = f"{started_utc}-{secrets.token_hex(4)}.rc-canary.json"
    path = directory / name
    try:
        with open(path, "x", encoding="utf-8") as handle:  # ⛔ 'x' — 존재하면 즉시 실패
            handle.write(json.dumps({"schema": "rc-canary/3", "status": "in_progress"}) + "\n")
    except FileExistsError:
        raise CanaryContractError(f"산출물 이름이 이미 있다: {name}") from None
    except OSError as exc:
        raise CanaryContractError(
            f"출력 디렉터리에 쓸 수 없다 ({type(exc).__name__}) — RC 호출 전에 중단"
        ) from None
    path.chmod(0o600)
    return path


def finalize_artifact(reserved: pathlib.Path, summary: dict) -> pathlib.Path:
    """예약된 경로에 최종 요약을 원자적으로 쓰고 sidecar 를 exclusive 로 남긴다."""
    body = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    temp = reserved.parent / f".{reserved.name}.tmp"
    temp.write_text(body)
    temp.chmod(0o600)
    os.replace(temp, reserved)  # 우리가 예약한 파일 위로만 — 원자적
    digest = hashlib.sha256(body.encode()).hexdigest()
    sidecar = reserved.parent / f"{reserved.name}.sha256"
    try:
        with open(sidecar, "x", encoding="utf-8") as handle:
            handle.write(f"{digest}  {reserved.name}\n")
    except FileExistsError:
        # ⛔ artifact(증거)는 남긴다 — raw-first. sidecar 충돌만 실패로 알린다.
        raise CanaryContractError(
            f"sidecar 가 이미 있다 — artifact 는 보존됨: {reserved.name}"
        ) from None
    sidecar.chmod(0o600)
    return reserved


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--cadence-seconds", type=float, default=5.0)
    parser.add_argument("--call-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--max-runtime-seconds", type=float, default=600.0)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        suppress_runtime_logging()
        require_rc_config()
        uid = os.getenv("CANARY_UID", "")
        validate_run_parameters(
            uid,
            args.iterations,
            args.cadence_seconds,
            args.call_timeout_seconds,
            args.max_runtime_seconds,
        )
        require_runner_context(args.max_runtime_seconds)
        started_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        reserved = reserve_artifact(args.output_dir, started_utc)  # ⛔ RC 호출 **전**
        summary = asyncio.run(
            run_canary(
                uid=uid,
                iterations=args.iterations,
                cadence_seconds=args.cadence_seconds,
                call_timeout_seconds=args.call_timeout_seconds,
                max_runtime_seconds=args.max_runtime_seconds,
            )
        )
        path = finalize_artifact(reserved, summary)
    except CanaryContractError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "artifact": path.name,
                "counts_by_label": summary["counts_by_label"],
                "latency_ms": summary["latency_ms"],
                "aborted_reason": summary["aborted_reason"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
