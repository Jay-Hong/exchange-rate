"""KRX 만기 수정 배포 verifier — prepare/verify 2단계, read-only + 증거 artifact.

배포 당일 셸로 조립하지 않는다. 이 절차는 polling·sticky 집계·시간창 로그 대조·
증거 파일을 갖춘 **검증 프로그램**이고, 셸로 짜면 이 리포가 이미 값을 치른 함정들
(`set -e` + `rc=$?`, jq 경로 오류, credential argv 노출, 경계 오판)을 재생산한다.

## 왜 2단계인가

`verify` 단독으로는 **신뢰할 수 있는 T0를 복원할 수 없다**. 배포 후에는 구
컨테이너가 이미 제거돼 `docker inspect <old-id>`가 실패하고, 그 실패가 "제거됨"인지
"조회 불능"인지도 구분해야 한다. 그래서 `prepare`가 배포 **직전**에 구 컨테이너
identity와 T0를 고정하고, `verify`가 그 baseline을 입력으로 받는다.
baseline이 없으면 현재 시각으로 대체하지 않고 **UNVERIFIED**다.

## 판정 원칙 (이 파일 전체를 관통)

  - **관측 성공 + 나쁜 상태 = FAILED** / **관측 자체가 불능 = UNVERIFIED**
    `CollectorError`의 "지표를 읽지 못했다 ≠ 지표가 깨끗하다"와 같은 축이고,
    그 대칭인 "읽지 못했다 ≠ 장애다"도 함께 지킨다.
  - **FAILED는 sticky** — 한 번 관측되면 이후 샘플이 정상이어도 고정된다.
    PENDING은 sticky가 아니다(정상 배포도 반드시 과도기를 지난다).
  - 증거 부재를 "정상"으로 기록하지 않는다. marker 부재는 "timeout을 관측하지
    못했다"이지 "timeout이 없었다"가 아니므로, clean shutdown 판정에는 로그
    완전성과 die exitCode가 **함께** 필요하다.

## 범위

배포 전 rollover 증거 수집은 **관찰 전용(non-blocking)**이고, 배포 후 게이트만
필수다. 게이트 실패는 rollback을 자동으로 의미하지 않는다 — UNVERIFIED/FAILED
모두 **후속(일봉 정정)을 차단**하고 조사로 넘긴다.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

_SCRIPTS = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from canary_monitor import (  # noqa: E402
    CollectorError,
    PRODUCTION_TARGET,
    Target,
    admin_fetch_command,
    parse_curl_response,
    run_command,
)

REPO_ROOT = _SCRIPTS.parent

# ---------------------------------------------------------------------------
# 판정 어휘
# ---------------------------------------------------------------------------

PASS = "PASS"
PENDING = "PENDING"
FAILED = "FAILED"
UNVERIFIED = "UNVERIFIED"
OBSERVED = "OBSERVED"  # pre-deploy 관찰 전용 — 게이트가 아니다

_SEVERITY = {OBSERVED: 0, PASS: 1, PENDING: 2, UNVERIFIED: 3, FAILED: 4}

# reconcile이 기록하는 result literal 전수. `app/scheduler.py`의
# `_record_krx_reconcile(result=...)` 호출과 **정확히 일치**해야 하며, 신규 literal이
# 추가되면 아래 정책 맵이 미분류가 되어 테스트가 먼저 깨진다.
RECONCILE_RESULTS = frozenset({
    "no_op",
    "rollover",
    "resolve_error",
    "resolved_none",
    "bootstrap_skipped",
    "bootstrap_started",
    "bootstrap_error",
    "shutdown_error",
    "jump_suppressed",
    "task_dead_restarted",
    "task_dead_restart_error",
})

# 배포 후 창에서 "다음 tick을 기다려 볼 수 있는" 과도기 상태.
# `bootstrap_started`는 복구 자체는 성공이지만 **startup bootstrap이 실패했거나
# 미완이었다**는 함의라 로그 확인이 따라야 한다.
_TRANSIENT_POST = frozenset({"bootstrap_skipped", "bootstrap_started"})


def verdict(result: str, *, phase: str, deadline_passed: bool) -> str:
    """단일 reconcile 관측의 판정. **정적 맵이 아니라 phase·상황을 받는 함수**다.

    같은 `rollover`가 배포 전에는 정당한 자동 rollover 증거이고 배포 후에는
    비기대 상태(새 코드 bootstrap은 목표 월물 직결)이므로, 정적 dict로는 표현할
    수 없다.

    Args:
        result: `RECONCILE_RESULTS` 중 하나.
        phase: "pre"(배포 전, 구 프로세스) | "post"(배포 후, 새 프로세스).
        deadline_passed: verification deadline 경과 여부. 과도기 상태를
            PENDING으로 둘지 FAILED로 확정할지를 가른다.

    Raises:
        ValueError: 미등록 result — 정책 맵이 뚫린 것이므로 조용히 통과시키지 않는다.
    """
    if result not in RECONCILE_RESULTS:
        raise ValueError(f"미등록 reconcile result: {result!r} — 정책 맵 갱신 필요")
    if phase == "pre":
        # 배포 전은 전부 관찰 자료다. 구 프로세스가 무엇을 했든 배포를 막지 않는다.
        return OBSERVED
    if phase != "post":
        raise ValueError(f"unknown phase: {phase!r}")
    if result == "no_op":
        # PASS 후보일 뿐 — freshness·계약 tuple·job 상태·연속성은 호출자가 결합한다.
        return PASS
    if result in _TRANSIENT_POST:
        return FAILED if deadline_passed else PENDING
    return FAILED


def aggregate(verdicts: Sequence[str]) -> str:
    """샘플 시계열 → 최종 판정. **FAILED만 sticky**.

    최종 no_op 하나로 PASS를 계산하면 중간의 `task_dead_restarted`·`rollover`가
    덮인다. 반대로 PENDING까지 누적 고정하면 정상 배포도 통과할 수 없다
    (초기 connection refused와 `bootstrap_skipped`를 반드시 지나므로).

    **UNVERIFIED도 sticky가 아니다** (codex 리뷰로 정정). 과도기의 일시적 조회
    실패는 이후 정상 샘플로 해소될 수 있다. UNVERIFIED가 **최종** 판정이 되는
    경우는 "deadline까지 유효 샘플을 한 번도 얻지 못함"이고, 그건 빈 시퀀스로
    표현된다 — 즉 여기서가 아니라 orchestrator가 결정한다.
    """
    if not verdicts:
        return UNVERIFIED
    if FAILED in verdicts:
        return FAILED
    return verdicts[-1]


def worst(*verdicts: str) -> str:
    """축별 판정 결합 — 가장 나쁜 것이 이긴다."""
    return max(verdicts, key=lambda v: _SEVERITY[v])


# ---------------------------------------------------------------------------
# 계약 tuple
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContractTuple:
    code: str
    month: str
    expires_on: str

    @classmethod
    def from_payload(cls, raw: Any) -> Optional["ContractTuple"]:
        if not isinstance(raw, dict):
            return None
        try:
            return cls(str(raw["code"]), str(raw["month"]), str(raw["expires_on"]))
        except (KeyError, TypeError):
            return None


def contract_matches(observed: Any, expected: ContractTuple) -> bool:
    """`code`뿐 아니라 `month`·`expires_on`까지 일치해야 한다.

    3필드를 요구하면 덤으로 **새 캘린더의 무보정 월 회귀**가 라이브로 검증된다 —
    목표 월물의 `expires_on`이 보정 없는 셋째 월요일로 나와야 하므로, walk-back이
    정상 월을 잘못 건드리면 여기서 걸린다.
    """
    got = ContractTuple.from_payload(observed)
    return got is not None and got == expected


# ---------------------------------------------------------------------------
# Docker event 축 (구 컨테이너 종료·제거 증거)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DieAnalysis:
    status: str          # PASS / FAILED / UNVERIFIED
    die_count: int
    exit_code: Optional[str]
    destroyed: bool
    detail: str


def analyze_container_events(events: Sequence[dict], old_id: str) -> DieAnalysis:
    """`docker events --format '{{json .}}'` 파싱 결과 → 종료/제거 판정.

    문자열 grep이 아니라 구조로 본다. `kill`은 실패 신호가 아니다 — compose의
    정상 stop도 SIGTERM을 보낸다. 판정 축은 **최종 die의 exitCode**다.
    """
    dies, destroyed = [], False
    for ev in events:
        if not isinstance(ev, dict):
            return DieAnalysis(UNVERIFIED, 0, None, False, "event가 JSON 객체가 아님")
        actor = ev.get("Actor") or {}
        if str(actor.get("ID", "")) != old_id:
            continue
        action = ev.get("Action")
        if action == "die":
            dies.append(str((actor.get("Attributes") or {}).get("exitCode", "")))
        elif action == "destroy":
            destroyed = True
    if not dies:
        return DieAnalysis(UNVERIFIED, 0, None, destroyed, "die 이벤트 0건 — 관측 불능")
    if len(dies) > 1:
        # 조회·파싱은 성공했는데 같은 컨테이너가 창 안에서 여러 번 종료됐다.
        # 관측 불능이 아니라 실제 lifecycle 이상이다.
        return DieAnalysis(FAILED, len(dies), dies[-1], destroyed,
                           f"die {len(dies)}건 — crash-loop 의심")
    code = dies[0]
    if code != "0":
        return DieAnalysis(FAILED, 1, code, destroyed, f"die exitCode={code}")
    return DieAnalysis(PASS, 1, code, destroyed, "정상 종료")


def classify_inspect_failure(returncode: int, stderr: str) -> str:
    """`docker inspect <old-id>` 실패의 성격 구분.

    daemon이 정상 응답하며 "그런 객체 없음"이라고 한 것은 **제거 증거**이고,
    daemon 접근 실패·권한 오류는 **관측 불능**이다. exit code만 보면 둘 다
    nonzero라 뭉개지므로 stderr까지 본다.
    """
    lowered = (stderr or "").lower()
    if returncode != 0 and ("no such object" in lowered or "no such container" in lowered):
        return PASS  # 제거 확인
    if returncode == 0:
        return FAILED  # 아직 존재 — force-recreate가 제거하지 않았다
    return UNVERIFIED


# ---------------------------------------------------------------------------
# 증거 artifact — finalized/checksummed
# ---------------------------------------------------------------------------

MAX_ARTIFACT_RECORDS = 2000
_TERMINAL_SLOT = 1  # 마지막 1칸은 terminal record 전용으로 예약


class EvidenceArtifact:
    """JSONL 증거 파일. `O_EXCL` 생성 + 0600 + record 당 fsync.

    ⚠️ **immutable이 아니다.** `O_EXCL`은 같은 이름의 최초 생성만 막고 0600은
    소유자 쓰기를 허용한다. "finalized"는 `finalize()`가 마지막 record를 쓰고
    sha256을 계산한 뒤로는 **더 쓰지 않는다**는 규율이지 파일시스템 보증이 아니다.

    cap에 도달하면 정상 record는 거부하되 **terminal record 1칸을 남겨** 절단
    사실이 기록되게 한다. 재사용 primitive(`MetricsArtifact`)는 cap 도달 후
    marker조차 조용히 버리므로 그대로 쓸 수 없다.
    """

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.records = 0
        self.truncated = False
        self._finalized = False
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)

    def append(self, kind: str, payload: dict, *, terminal: bool = False) -> bool:
        if self._finalized:
            # checksum 이후 내용이 바뀌면 그 checksum이 무의미해진다.
            raise RuntimeError("finalize() 이후에는 append할 수 없다")
        limit = MAX_ARTIFACT_RECORDS if terminal else MAX_ARTIFACT_RECORDS - _TERMINAL_SLOT
        if self.records >= limit:
            self.truncated = True
            return False
        record = {"kind": kind, "captured_at": _utc_now_iso(), **payload}
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.records += 1
        return True

    def finalize(self, summary: dict) -> tuple[str, str]:
        """terminal record 기록 → sha256 계산 → **이후 쓰기 거부**.

        Returns:
            `(effective_verdict, sha256)`. 절단이 있었으면 **반환 verdict도** 낮춘다 —
            terminal record만 UNVERIFIED로 바꾸고 호출자에게는 PASS를 돌려주면,
            CLI가 exit 0을 내고 산출물만 "증거 불완전"이라고 말하는 모순이 생긴다
            (codex 감사에서 `returned=PASS / terminal=UNVERIFIED`로 재현됨).
        """
        if self._finalized:
            raise RuntimeError("이미 finalize됐다 — 재-finalize는 checksum을 무의미하게 한다")
        effective = summary.get("verdict", UNVERIFIED)
        if self.truncated:
            effective = worst(effective, UNVERIFIED)
            summary = {**summary, "truncated": True, "verdict": effective}
        self.append("terminal", summary, terminal=True)
        self._finalized = True
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        (self.path.parent / (self.path.name + ".sha256")).write_text(
            f"{digest}  {self.path.name}\n", encoding="utf-8"
        )
        return effective, digest


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Baseline (prepare 산출물)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Baseline:
    t0_iso: str
    old_container_id: str
    old_started_at: str
    old_restart_count: int
    old_image: str
    expected_code: str
    expected_month: str
    expected_expires_on: str
    expected_revision: str
    expected_source_sha256: str
    service_baseline: Optional[dict] = None

    @property
    def expected_contract(self) -> ContractTuple:
        return ContractTuple(self.expected_code, self.expected_month,
                             self.expected_expires_on)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, indent=2)

    @classmethod
    def load(cls, path: pathlib.Path) -> "Baseline":
        """baseline 로드 — **sidecar checksum을 반드시 검증**한다.

        prepare가 sha256을 남기는데 verify가 확인하지 않으면 그 sha256은 장식이다.
        baseline은 T0·구 컨테이너 identity·기대 계약의 유일한 출처이므로, 변조되면
        게이트 전체가 잘못된 기준 위에서 돈다 (codex 감사에서 실제 재현됨).
        """
        path = pathlib.Path(path)
        sidecar = path.parent / (path.name + ".sha256")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CollectorError(f"baseline 읽기 실패: {exc}") from exc
        if not sidecar.exists():
            raise CollectorError(f"baseline checksum sidecar 없음: {sidecar.name}")
        try:
            recorded = sidecar.read_text(encoding="utf-8").split()[0].strip()
        except (OSError, IndexError) as exc:
            raise CollectorError(f"checksum sidecar 파싱 실패: {exc}") from exc
        actual = hashlib.sha256(data).hexdigest()
        if recorded != actual:
            raise CollectorError(
                f"baseline checksum 불일치 (기록 {recorded[:12]}… != 실제 {actual[:12]}…) "
                "— 변조되었거나 sidecar가 다른 파일의 것이다"
            )
        try:
            raw = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CollectorError(f"baseline 파싱 실패: {exc}") from exc
        if not isinstance(raw, dict):
            raise CollectorError("baseline이 JSON 객체가 아니다")
        missing = {f for f in cls.__dataclass_fields__} - set(raw)
        if missing:
            raise CollectorError(f"baseline 필드 누락: {sorted(missing)}")
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# 연속성 3-tuple
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContainerIdentity:
    container_id: str
    started_at: str
    restart_count: int


MIN_CONTINUITY_SAMPLES = 2


def evaluate_continuity(samples: Sequence[ContainerIdentity]) -> tuple[str, str]:
    """컨테이너 연속성 판정 → `(verdict, detail)`.

    `rollover_count`는 프로세스 재시작 시 0으로 초기화되므로, 검증 창 중간에
    재시작이 끼면 "count==0"이 "rollover 없었음"의 위증이 된다. ID만으로는
    부족하다 — 같은 ID로도 `docker restart`가 가능하므로 StartedAt·RestartCount
    까지 3-tuple로 본다.

    ⚠️ **표본 1건은 PASS가 아니다** (codex 감사 지적). 첫 PASS에서 즉시 polling을
    끝내면 identity 표본이 1건뿐이고, 그러면 "변하지 않았다"를 관측한 게 아니라
    **변화를 볼 기회가 없었다**. 관측 불능이므로 UNVERIFIED다.

    그리고 새 컨테이너의 `RestartCount`는 **0이어야 한다** — force-recreate 직후
    재시작이 있었다면 그 자체가 clean deploy 위반이다.
    """
    if not samples:
        return UNVERIFIED, "identity 표본 0건"
    if len(set(samples)) > 1:
        return FAILED, "검증 창 중간 컨테이너 identity 변경 (rollover_count 신뢰 불가)"
    if samples[0].restart_count != 0:
        return FAILED, f"새 컨테이너 RestartCount={samples[0].restart_count} != 0"
    if len(samples) < MIN_CONTINUITY_SAMPLES:
        return UNVERIFIED, (
            f"identity 표본 {len(samples)}건 (<{MIN_CONTINUITY_SAMPLES}) — "
            "변화를 볼 기회가 없었다"
        )
    return PASS, "연속성 유지"


# ---------------------------------------------------------------------------
# 로그 축 — allowlist projection
# ---------------------------------------------------------------------------

# 비정상 lifecycle marker. `logger.*` 전수를 rg로 쓸어 담지 않는다 — 로그 문자열은
# 닫힌 어휘가 아니라 완전성 검사 대상이 될 수 없고, 무관한 debug 한 줄 추가가
# CI를 깨뜨린다. **판정에 실제로 쓰는 소수만** curated로 유지한다.
SHUTDOWN_FAILURE_MARKERS = (
    "[krx_close_snapshot] close timeout",
    "[krx] bootstrap cancel 실패",
    "[krx] client.stop() 실패",
    "[krx] task await 실패",
)
RUNTIME_FAILURE_MARKERS = (
    "[krx] KisFuturesClient task crashed",
    "[krx] bootstrap 실패",
    "[krx] active contract resolve 실패",
    "[krx] reconcile resolve 실패",
    "[krx] reconcile shutdown 실패",
    "[krx] reconcile rollover bootstrap 실패",
    "[krx] reconcile 점프 의심",
    "[krx] reconcile: active USD futures contract 없음",
    "[krx] reconcile: client task dead",
)
ROLLOVER_MARKER = "[krx] rollover"

_ALLOWED_LOG_FIELDS = ("timestamp", "level", "logger", "message")


def project_log_line(line: str) -> Optional[dict]:
    """JSON 로그 → allowlist 필드만. 원문·exc_info·extra는 저장하지 않는다.

    산출물은 사후 공유·보관되고 로그에는 UID·토큰·경로가 섞인다. 전체 `docker
    inspect` 덤프를 남기지 않는 것과 같은 이유다(`{{json .Config.Env}}`는
    KIS_APP_SECRET을 전량 노출한다).
    """
    try:
        parsed = json.loads(line)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return {k: parsed[k] for k in _ALLOWED_LOG_FIELDS if k in parsed}


@dataclass(frozen=True)
class LogWindow:
    lines: list[str]
    unparsed: int


def count_unplaceable(lines: Sequence[str]) -> int:
    """timestamp를 못 읽어 **어느 창에도 배치할 수 없는** 줄 수.

    `filter_log_window`는 같은 전체 로그로 두 번 호출되므로 그 반환값의
    `unparsed`를 창별로 기록하면 **한 줄이 양쪽에 중복 계상**된다
    (codex 리뷰 지적). 배치 불가는 창의 속성이 아니라 입력 전체의 속성이므로
    여기서 한 번만 센다.
    """
    total = 0
    for line in lines:
        stamp = (project_log_line(line) or {}).get("timestamp")
        try:
            when = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            total += 1
            continue
        if when.tzinfo is None:
            total += 1
    return total


def covers_window_start(lines: Sequence[str],
                        since_iso: str) -> tuple[bool, Optional[str]]:
    """남아 있는 로그가 창 **시작점 이전**까지 덮는가 → `(덮는가, 가장 오래된 줄)`.

    `app.log*`를 사후 조회하는 구조라, 회전이 보존분(backupCount)을 넘겨 일어나면
    `[T0, …)`의 앞부분이 통째로 사라진다. 그 상태에서 "실패 marker 0건"은
    **관측 결과가 아니라 관측 부재**다.

    판정은 "가장 오래된 파싱 가능한 줄 <= T0". 같거나 이르면 T0 시점이 보존분
    안에 들어 있다는 뜻이다. 줄이 하나도 파싱되지 않으면 덮는다고 말할 수 없다.
    """
    try:
        since = datetime.fromisoformat(since_iso)
    except (TypeError, ValueError) as exc:
        raise CollectorError(f"로그 창 시작 파싱 실패: {exc}") from exc
    if since.tzinfo is None:
        raise CollectorError("로그 창 경계는 timezone-aware여야 한다")

    # `filter_log_window`와 **같은 방식**으로 뽑는다 — 다르게 뽑으면 "덮는다고
    # 판정한 창"과 "실제로 필터된 창"이 갈릴 수 있다.
    stamps = []
    for line in lines:
        projected = project_log_line(line)
        stamp = (projected or {}).get("timestamp")
        try:
            when = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            continue
        stamps.append(when)
    if not stamps:
        return False, None
    oldest = min(stamps)
    return oldest <= since, oldest.isoformat()


def filter_log_window(lines: Sequence[str], since_iso: str,
                      until_iso: str) -> LogWindow:
    """`timestamp` 기준 `[since, until)` 필터 — **순수 함수**.

    운영 어댑터가 since/until을 무시하고 전체 로그를 넘기면 3분할이 허구가 되고,
    배포 훨씬 전의 marker가 창 안 사건으로 오탐된다(codex 리뷰 지적). 창 분할을
    어댑터가 아니라 여기서 하면 테스트가 실제 경계를 검증할 수 있다.

    timestamp를 못 읽는 줄은 **어느 창에도 넣지 않고 카운트만** 한다. 앱 로거는
    항상 JSON을 내므로(실측) 그런 줄은 외래 입력이고, 양쪽에 넣으면 pre 창의
    rollover(증거)가 runtime 창(실패)으로도 세어져 false FAILED가 된다.
    """
    try:
        since = datetime.fromisoformat(since_iso)
        until = datetime.fromisoformat(until_iso)
    except (TypeError, ValueError) as exc:
        raise CollectorError(f"로그 창 경계 파싱 실패: {exc}") from exc
    if since.tzinfo is None or until.tzinfo is None:
        raise CollectorError("로그 창 경계는 timezone-aware여야 한다")

    picked, unparsed = [], 0
    for line in lines:
        projected = project_log_line(line)
        stamp = (projected or {}).get("timestamp")
        try:
            when = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            unparsed += 1
            continue
        if when.tzinfo is None:
            unparsed += 1
            continue
        if since <= when < until:
            picked.append(line)
    return LogWindow(picked, unparsed)


def scan_log_markers(lines: Sequence[str], markers: Sequence[str]) -> list[dict]:
    """marker 매칭 — **JSON을 먼저 디코드한 뒤** message에 대해 본다.

    ⛔ raw 문자열 substring으로 매칭하면 안 된다. 운영 JSON 로거는 비ASCII를
    `\\uXXXX`로 escape하므로(실측: `"[kis_ws] status stale \\u2192 reconnecting"`),
    한글이 든 marker(`[krx] reconcile 점프 의심` 등)는 **raw line에 절대 나타나지
    않는다**. 디코드 없이 훑으면 조용히 0건이 되고 "오류 없음"으로 오독된다.
    """
    hits = []
    for line in lines:
        projected = project_log_line(line)
        haystack = projected.get("message", "") if projected else line
        if not any(m in haystack for m in markers):
            continue
        hits.append(projected or {"message": line.strip()[:200]})
    return hits


# ---------------------------------------------------------------------------
# AST — 정책 맵 완전성
# ---------------------------------------------------------------------------

def extract_reconcile_results(scheduler_path: pathlib.Path) -> set[str]:
    """`_record_krx_reconcile(result=...)` 의 literal 전수를 AST로 추출.

    문자열 grep이 아니라 AST인 이유: 주석·docstring이 같은 문자열을 언급할 수
    있다. 그리고 **literal이 아닌 전달(변수·**kwargs·누락)은 정책 우회**이므로
    `ValueError`로 실패시킨다 — 통과시키면 추출기가 값을 못 보고 완전성 검사가
    조용히 통과한다.
    """
    tree = ast.parse(pathlib.Path(scheduler_path).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name != "_record_krx_reconcile":
            continue
        if any(kw.arg is None for kw in node.keywords):
            raise ValueError(f"line {node.lineno}: **kwargs 전달 — 정책 우회")
        kw = next((k for k in node.keywords if k.arg == "result"), None)
        if kw is None:
            raise ValueError(f"line {node.lineno}: result= 누락")
        if not isinstance(kw.value, ast.Constant) or not isinstance(kw.value.value, str):
            raise ValueError(f"line {node.lineno}: result=가 문자열 literal이 아님")
        found.add(kw.value.value)
    return found


# ---------------------------------------------------------------------------
# IO 어댑터 (테스트에서 전량 주입)
# ---------------------------------------------------------------------------

@dataclass
class Adapters:
    admin_fetch: Callable[[str], Any]
    inspect_format: Callable[[str], Any]
    container_events: Callable[[str, str, str], Any]
    read_log_lines: Callable[[str, str], Any]
    expiry_probe: Callable[[str], Any]
    source_fingerprint: Callable[[], Any]
    now: Callable[[], datetime]
    sleep: Callable[[float], Any]


async def fetch_krx_status(target: Target = PRODUCTION_TARGET) -> dict:
    """admin endpoint 조회 — 비밀번호가 argv에 노출되지 않는 기존 경로 재사용.

    `parse_curl_response`가 non-2xx·형식 오류·비-dict를 전부 `CollectorError`로
    fail-closed 처리한다. 인증 오류 JSON이 "빈 스냅샷"으로 둔갑하지 않는다.
    """
    raw = await run_command(admin_fetch_command("/admin/api/krx-status", target))
    return parse_curl_response(raw)


def project_status(payload: dict) -> dict:
    """allowlist projection — `.reconcile` 7필드 + `.client.contract` + lifecycle.

    ⚠️ 7필드는 최상위가 아니라 **`.reconcile` 아래**이고, 실행 중 계약은
    **`.client.contract`** 다. 최상위에서 뽑으면 all-null 객체를 만들고도
    성공한다.
    """
    reconcile = payload.get("reconcile") or {}
    client = payload.get("client") or {}
    return {
        "enabled": payload.get("enabled"),
        "started": payload.get("started"),
        "job_registered": reconcile.get("job_registered"),
        "next_run_at_kst": reconcile.get("next_run_at_kst"),
        "last_run_at_kst": reconcile.get("last_run_at_kst"),
        "last_result": reconcile.get("last_result"),
        "last_current_contract": reconcile.get("last_current_contract"),
        "last_resolved_contract": reconcile.get("last_resolved_contract"),
        "rollover_count": reconcile.get("rollover_count"),
        "client_contract": client.get("contract"),
        "client_started_at_kst": (client.get("lifecycle") or {}).get("started_at_kst"),
    }


# ---------------------------------------------------------------------------
# PASS 조건 결합
# ---------------------------------------------------------------------------

def evaluate_post_sample(status: dict, baseline: Baseline, *,
                         deadline_passed: bool) -> tuple[str, list[str]]:
    """단일 post-deploy 샘플 판정 → (verdict, 사유 목록).

    PASS 6조건: enabled ∧ started ∧ job_registered ∧ fresh no_op ∧
    계약 tuple 3곳 일치 ∧ rollover_count == 0.

    `rollover_count`는 상태 필드 중 **유일하게 덮어쓰이지 않는** 신호다. 샘플
    간격 사이에 rollover가 발생하고 다음 tick의 no_op에 덮여도 counter가 남으므로,
    "polling은 강한 관측 수단이지 누락 불가 보장이 아니다"의 백스톱이 된다.
    """
    reasons: list[str] = []
    if not (status.get("enabled") and status.get("started")
            and status.get("job_registered")):
        return FAILED, ["enabled/started/job_registered 미충족"]

    last_run = status.get("last_run_at_kst")
    if not last_run:
        return (FAILED if deadline_passed else PENDING), ["last_run_at_kst 없음"]
    if not _is_after(last_run, baseline.t0_iso):
        return (FAILED if deadline_passed else PENDING), ["fresh tick 아님"]

    result = status.get("last_result")
    if result is None:
        return (FAILED if deadline_passed else PENDING), ["last_result 없음"]
    try:
        base = verdict(result, phase="post", deadline_passed=deadline_passed)
    except ValueError as exc:
        return FAILED, [str(exc)]
    if base != PASS:
        return base, [f"last_result={result}"]

    expected = baseline.expected_contract
    for key in ("client_contract", "last_current_contract", "last_resolved_contract"):
        if not contract_matches(status.get(key), expected):
            reasons.append(f"{key} 계약 tuple 불일치")
    if status.get("rollover_count") != 0:
        reasons.append(f"rollover_count={status.get('rollover_count')} != 0")
    return (FAILED if reasons else PASS), reasons


def _is_after(candidate_iso: str, reference_iso: str) -> bool:
    """aware datetime 비교. **문자열 비교 금지** — microsecond 유무·offset 표기가 가변."""
    try:
        a = datetime.fromisoformat(str(candidate_iso))
        b = datetime.fromisoformat(str(reference_iso))
    except (TypeError, ValueError):
        return False
    if a.tzinfo is None or b.tzinfo is None:
        return False
    return a > b


# ---------------------------------------------------------------------------
# Orchestration — polling · 이중 deadline · 축 결합 · artifact
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Deadlines:
    """두 deadline이 **모두** 필요하다.

    `startup`: T0 → 첫 정상 admin 응답. 없으면 첫 응답이 영원히 안 올 때
        verifier가 종료하지 않는다.
    `verify`: 첫 정상 응답 → 게이트 확정. 중간 상태(`bootstrap_skipped` 등)가
        다음 tick에 해소되므로 2 interval을 포괄해야 한다(기본 12분).
    `observe`: 첫 정상 응답 후 **관측을 계속할 최소 시간**. 이게 없으면 첫 PASS
        2건에서 곧장 끝나 실제 관측 창이 약 45초로 줄고, runbook의 "12분간
        전량 저장"이 문서에만 있는 말이 된다 (codex 감사 지적).
    """
    startup_seconds: float = 600.0
    verify_seconds: float = 720.0
    observe_seconds: float = 720.0
    poll_seconds: float = 45.0


async def collect_evidence(baseline: Baseline, adapters: Adapters,
                           artifact: EvidenceArtifact,
                           deadlines: Deadlines = Deadlines()) -> dict:
    """배포 후 **증거 수집기**. 판정은 보조 자료이고 게이트는 사람이 잡는다.

    ⚠️ 이 함수의 반환 verdict는 **자동 승인 신호가 아니다**. 관측 배선에서만
    P1이 6건 나왔던 이력(codex 감사)이 있어, 무인 PASS 경로를 두지 않는다.
    산출물은 축별 증거 파일이고, 최종 판정은 `KRX_CANARY.md`의 배포 runbook
    체크리스트를 사람이 대조해 내린다. 여기서 UNVERIFIED/FAILED가 나오면
    그것만으로 후속(일봉 정정)을 차단하기에 충분하지만, PASS는 **필요조건일 뿐
    충분조건이 아니다**.

    축을 결합해 최종 판정을 낸다:
      1. admin 샘플 시계열 (sticky 집계)
      2. 컨테이너 연속성 3-tuple (검증 창 중간 재시작 검출)
      3. 구 컨테이너 die exitCode + 제거 증거
      4. 로그 3분할 (T0~StartedAt 구 프로세스 shutdown / StartedAt 이후 신 runtime)

    관측 성공 + 나쁜 상태 = FAILED / 관측 불능 = UNVERIFIED. 둘 다 후속을
    차단하지만 rollback을 자동으로 의미하지는 않는다.
    """
    start = adapters.now()
    samples: list[str] = []
    identities: list[ContainerIdentity] = []
    first_ok_at: Optional[datetime] = None
    new_identity: Optional[ContainerIdentity] = None
    early_events: list = []

    while True:
        elapsed = (adapters.now() - start).total_seconds()
        if first_ok_at is None and elapsed > deadlines.startup_seconds:
            # 유효 샘플 0건으로 startup deadline 초과 = 관측 불능
            return _finish(artifact, UNVERIFIED, ["startup deadline까지 유효 응답 0건"],
                           samples=samples)
        if first_ok_at is not None:
            since_first = (adapters.now() - first_ok_at).total_seconds()
            deadline_passed = since_first > deadlines.verify_seconds
        else:
            deadline_passed = False

        try:
            payload = await adapters.admin_fetch("/admin/api/krx-status")
            status = project_status(payload)
            identity = await _read_identity(adapters)
        except CollectorError as exc:
            # 과도기의 일시 실패 — 즉시 UNVERIFIED가 아니다.
            artifact.append("sample", {"verdict": PENDING, "error": str(exc)[:200]})
            samples.append(PENDING)
            if deadline_passed:
                return _finish(artifact, FAILED,
                               ["verify deadline까지 조회 실패 지속"], samples=samples)
            await adapters.sleep(deadlines.poll_seconds)
            continue

        if first_ok_at is None:
            first_ok_at = adapters.now()
            new_identity = identity
            # ⛔ **여기서 events 를 한 번 잡는다.** 데몬의 이벤트 재생 버퍼는
            #    시간창이 아니라 전역 FIFO **256건**이다(실측: `--since 720h`를
            #    줘도 정확히 256). 운영 호스트는 헬스체크만으로 분당 ~30건을
            #    만들어 8~13분이면 한 바퀴 돈다. 관측 창(720s)을 채운 뒤에만
            #    조회하면 배포 시점의 die/destroy 가 이미 밀려나 있어, 정상
            #    배포도 "die 0건 → UNVERIFIED"로 차단된다.
            early_events = await _capture_events(
                adapters, baseline, first_ok_at.isoformat(), artifact, "early")
        identities.append(identity)

        sample_verdict, reasons = evaluate_post_sample(
            status, baseline, deadline_passed=deadline_passed)
        samples.append(sample_verdict)
        artifact.append("sample", {"verdict": sample_verdict, "reasons": reasons,
                                   "status": status, "identity": asdict(identity)})

        if sample_verdict == FAILED:
            break
        observed = (adapters.now() - first_ok_at).total_seconds()
        if (sample_verdict == PASS
                and len(identities) >= MIN_CONTINUITY_SAMPLES
                and observed >= deadlines.observe_seconds):
            # 첫 PASS에서 곧장 끝내면 identity 표본이 1건뿐이라 연속성을
            # "관측"한 게 아니라 "볼 기회가 없었던" 것이 된다. 표본 수만이
            # 아니라 **관측 시간**도 채워야 한다 — 배포 직후 45초만 보고
            # "연속성 유지"라고 적으면 그 뒤의 재시작을 못 본다.
            break
        if deadline_passed:
            samples[-1] = FAILED
            break
        await adapters.sleep(deadlines.poll_seconds)

    reasons: list[str] = []
    sample_result = aggregate(samples)

    continuity, continuity_detail = evaluate_continuity(identities)
    if continuity != PASS:
        reasons.append(continuity_detail)

    if new_identity is not None and new_identity.container_id == baseline.old_container_id:
        continuity = FAILED
        reasons.append("새 container ID가 구 ID와 동일 — 재생성되지 않았다")

    now_iso = adapters.now().isoformat()
    die = await _analyze_shutdown(adapters, baseline, now_iso, artifact,
                                  early_events=early_events)
    if die.status != PASS:
        reasons.append(f"구 컨테이너 종료 판정: {die.detail}")

    logs = await _analyze_logs(
        adapters, baseline, now_iso,
        new_identity.started_at if new_identity else None, artifact)
    if logs != PASS:
        reasons.append("로그 축 이상 (marker 검출·취득 실패·창 불완전)")

    deployed, deployed_detail = await _verify_deployed_code(adapters, baseline, artifact)
    if deployed != PASS:
        reasons.append(deployed_detail)

    health, health_detail = await _verify_reachability(adapters, artifact)
    if health != PASS:
        reasons.append(health_detail)

    service, service_detail = await _collect_service_evidence(
        adapters, baseline, artifact, adapters.now())
    if service != PASS:
        reasons.append(service_detail)

    # 모든 축을 마친 **뒤** identity를 다시 읽는다. 축 검사에 걸린 시간 동안
    # 재시작이 있었다면 앞선 관측이 통째로 다른 프로세스의 것이 된다.
    final_identity, final_detail = await _verify_final_identity(
        adapters, new_identity, artifact)
    if final_identity != PASS:
        reasons.append(final_detail)

    final = worst(sample_result, continuity, die.status, logs, deployed, health,
                  service, final_identity)
    return _finish(artifact, final, reasons, samples=samples)


async def _verify_final_identity(adapters: Adapters,
                                 observed: Optional[ContainerIdentity],
                                 artifact: EvidenceArtifact) -> tuple[str, str]:
    """**종료 직전** 3-tuple 재확인 + `RestartCount == 0`.

    폴링 창이 끝난 뒤에도 로그·probe·health 축이 남아 있어 실제로는 수 분이
    더 흐른다. 그 구간의 재시작을 아무도 보지 않으면 "12분간 무재시작"이
    검사하지 않은 구간을 포함한 주장이 된다 (codex 감사 지적).
    """
    if observed is None:
        artifact.append("final_identity", {"verdict": UNVERIFIED,
                                           "reason": "관측된 identity 없음"})
        return UNVERIFIED, "종료 직전 재확인 불가 — 유효 identity 표본 0건"
    try:
        current = await _read_identity(adapters)
    except CollectorError as exc:
        artifact.append("final_identity", {"verdict": UNVERIFIED, "error": str(exc)[:200]})
        return UNVERIFIED, f"종료 직전 identity 조회 실패: {exc}"

    artifact.append("final_identity", {"observed": asdict(observed),
                                       "final": asdict(current)})
    if current != observed:
        return FAILED, (
            f"종료 직전 identity가 달라졌다: {asdict(observed)} → {asdict(current)}")
    if current.restart_count != 0:
        return FAILED, f"종료 직전 RestartCount={current.restart_count} (≠0)"
    return PASS, "종료 직전 identity 동일 + RestartCount=0"


# 배포된 코드가 **수정본인지**를 가르는 동작 probe.
# 구 코드는 202608 → 2026-08-17(무보정 셋째 월요일)을 반환하고, 수정본은 8/14다.
# 이미지 태그 비교와 **독립**된 축이라 "태그만 같고 내용이 구 코드"도 잡는다.
EXPIRY_BEHAVIOR_PROBE = {"202608": "2026-08-14", "202602": "2026-02-13"}


# 배포된 코드가 **의도한 revision인가**를 가르는 지문.
#
# 동작 probe(위)는 "보정이 들어갔는가"만 본다 — calendar fix를 포함하지만
# 배포 대상이 아닌 **다른** commit/image도 통과한다 (codex 감사 지적).
# image label 대신 소스 자체를 해싱하는 이유: 라벨은 build 인자가 틀리면
# 거짓말을 하지만, 지문은 실제 파일에서 나온다.
#
# **범위 = Dockerfile이 최종 이미지로 COPY하는 리포 경로 전부**
# (`app/` `static/` `templates/` `scripts/`). `app/`만 해싱하면 실제 반례가
# 생긴다 — 이 작업 자체가 그랬다: 변경이 `scripts/`에만 있어 `app/` 지문이
# 이전 revision과 **같았다**. 그러면 구 revision 이미지가 통과한다.
#
# **범위 밖(이 지문이 증명하지 않는 것)**: 의존성. `requirements.lock.txt`는
# builder 스테이지에만 들어가 최종 이미지에 파일로 남지 않으므로 컨테이너에서
# 해싱할 수 없다. 즉 이 축은 "리포 payload가 그 revision인가"를 증명하고
# "빌드 입력이 같은가"는 증명하지 않는다.
#
# **제외 규칙의 기준**: "host worktree와 container에서 갈릴 수 있는 것"이다.
# `.dockerignore`를 그대로 옮기는 것이 아니다 — 옮길 필요도 없다.
#
#   실측(2026-08-17, 이 호스트의 docker로 직접 build): context 루트의
#   `.dockerignore`에 `*.md` / `__pycache__/` / `*.py[cod]`를 넣고 `COPY app/`를
#   했더니 `app/README.md` · `app/__pycache__/z.pyc` · `app/x.pyc` · `app/sub/y.pyc`가
#   **전부 이미지에 들어왔다**. dockerignore 패턴은 빌드 컨텍스트 **루트에 고정**되어
#   하위 디렉터리에 적용되지 않는다. 따라서 루트 규칙은 COPY root 안의 파일을
#   지우지 않고, "host엔 있고 이미지엔 없다"는 갈림도 만들지 않는다.
#
# 그래서 **반드시 필요한** 제외는 (1) 뿐이고, (2)(3)은 방어적 여유다:
#   (1) **bytecode cache**. 컨테이너는 `PYTHONDONTWRITEBYTECODE=1`(Dockerfile)이라
#       런타임에 만들지 **않는다** — 그런데도 필요한 이유는 반대쪽이다:
#       위 anchoring 때문에 **host의 `app/__pycache__`가 이미지로 복사된다**.
#       비교되는 두 값은 "prepare 시점 host"와 "build된 이미지"인데, host 쪽
#       내용은 python을 돌릴 때마다 바뀐다 — prepare와 build 사이에 한 번만
#       바뀌어도 정상 배포가 불일치로 막힌다.
#       (실측: 이 worktree의 COPY root 안에 `.pyc` 161개)
#   (2) host 쪽 편집기·OS 부산물 (`.DS_Store` `.swp` `.swo`).
#   (3) `.dockerignore`와 이름을 맞춰 둔 역사적 항목 (`.md` `.log` `$py.class`
#       `node_modules` `.pytest_cache`) — inert.
#
# ⚠️ (2)(3)은 **검출력을 깎는다**: anchoring 때문에 `scripts/example.md`는
# 이미지에 들어가는데 지문에서는 양쪽 다 빠져, 그 파일만 바꾼 revision이
# "지문 동일"이 된다 (codex 감사 지적). 그래서 **tracked 파일과 제외 규칙의
# 교집합이 항상 비어 있어야** 한다 — `test_no_tracked_file_is_excluded`가
# 잠근다. 제외는 untracked 부산물에만 걸린다.
#
# 장래에 `.dockerignore`가 **COPY root 안을 겨냥하는** 규칙(`**/...` 또는
# `app/...` 형태)을 얻으면 그때는 진짜 갈림이 생긴다 — 테스트가 그걸 잡는다
# (`test_dockerignore_has_no_in_root_rules`).
#
# **host와 container에서 이 동일한 프로그램 문자열을 그대로 실행**한다 —
# 두 벌로 나눠 쓰면 로직이 갈릴 수 있다.
SOURCE_FINGERPRINT_ROOTS = ("app", "scripts", "static", "templates")
# (1) 필수 — container 런타임 생성물
_FINGERPRINT_REQUIRED_SKIP_DIRS = ("__pycache__",)
_FINGERPRINT_REQUIRED_SKIP_SUFFIXES = (".pyc", ".pyo", ".pyd")
_FINGERPRINT_SKIP_DIRS = _FINGERPRINT_REQUIRED_SKIP_DIRS + (
    ".pytest_cache", "node_modules", ".git")
_FINGERPRINT_SKIP_SUFFIXES = _FINGERPRINT_REQUIRED_SKIP_SUFFIXES + (
    "$py.class", ".md", ".log", ".swp", ".swo")
_FINGERPRINT_SKIP_NAMES = (".DS_Store",)

# walk·제외 규칙은 **여기 한 곳**에만 있다. 지문 프로그램과 경로 나열
# 프로그램이 이 본문을 공유하므로, 테스트가 보는 규칙과 지문이 쓰는 규칙이
# 갈릴 수 없다.
_FINGERPRINT_COLLECT = (
    "import hashlib,os\n"
    f"ROOTS={SOURCE_FINGERPRINT_ROOTS!r}\n"
    f"SKIP_DIRS={_FINGERPRINT_SKIP_DIRS!r}\n"
    f"SKIP_SUF={_FINGERPRINT_SKIP_SUFFIXES!r}\n"
    f"SKIP_NAMES={_FINGERPRINT_SKIP_NAMES!r}\n"
    "paths=[]\n"
    "for root in ROOTS:\n"
    "    for d,dirs,fs in os.walk(root):\n"
    "        dirs[:]=[x for x in dirs if x not in SKIP_DIRS]\n"
    "        for f in fs:\n"
    "            if f in SKIP_NAMES or f.endswith(SKIP_SUF):\n"
    "                continue\n"
    "            paths.append(os.path.join(d,f))\n"
)

SOURCE_FINGERPRINT_PROGRAM = _FINGERPRINT_COLLECT + (
    "h=hashlib.sha256()\n"
    "for p in sorted(paths):\n"
    "    h.update(p.encode());h.update(b'\\0')\n"
    "    h.update(hashlib.sha256(open(p,'rb').read()).hexdigest().encode())\n"
    "    h.update(b'\\n')\n"
    "print(h.hexdigest())\n"
)

_FINGERPRINT_LIST_PROGRAM = _FINGERPRINT_COLLECT + (
    "print('\\n'.join(sorted(paths)))\n"
)


def _run_fingerprint_program(program: str, repo_root: pathlib.Path,
                             what: str) -> str:
    proc = subprocess.run([sys.executable, "-c", program],
                          cwd=str(repo_root), capture_output=True, text=True)
    if proc.returncode != 0:
        raise CollectorError(f"{what} 실패: {proc.stderr.strip()[:200]}")
    return proc.stdout


def compute_local_source_fingerprint(repo_root: pathlib.Path) -> str:
    """host 쪽 지문 — container와 **같은 프로그램**을 같은 상대 경로로 실행."""
    return _run_fingerprint_program(
        SOURCE_FINGERPRINT_PROGRAM, repo_root, "소스 지문 계산").strip()


def list_fingerprinted_paths(repo_root: pathlib.Path) -> set[str]:
    """지문이 **실제로 방문하는** 경로 집합.

    제외 규칙을 두 번째로 구현하지 않기 위해 존재한다 — 테스트가 규칙을
    따로 흉내 내면, 흉내가 규칙과 갈렸을 때 조용히 통과한다.
    """
    out = _run_fingerprint_program(
        _FINGERPRINT_LIST_PROGRAM, repo_root, "지문 경로 나열")
    return {line for line in out.splitlines() if line}


async def _verify_deployed_code(adapters: Adapters, baseline: Baseline,
                                artifact: EvidenceArtifact) -> tuple[str, str]:
    """**수정된 코드가 실제로 돌고 있는가.**

    ⚠️ 이게 없으면 8/17 이후에는 **구 이미지를 force-recreate해도 게이트를
    통과한다** — 구 코드도 그 시점엔 A75609를 선택하고 no_op을 내기 때문이다
    (codex 감사 지적). 이 리포는 이미 같은 사고를 겪었다: 2026-05-19 Coinone
    canary가 `build` 없이 `--force-recreate`만 해서 구 이미지로 기동했다.

    세 축을 함께 본다:
      1. 새 컨테이너 Image가 baseline의 구 Image와 **다른가**
      2. 컨테이너 안에서 `_compute_expiry_date`가 **보정된 만기**를 내는가
      3. 컨테이너 안 **리포 payload 지문**(`app` `scripts` `static` `templates`)이
         배포하려던 revision의 것과 같은가
    (2)는 "보정이 들어갔는가", (3)은 "그 revision의 **리포 payload**인가"를
    가른다 — (2)만 있으면 calendar fix를 포함한 다른 commit도 통과한다.
    (3)이 증명하지 않는 것은 빌드 입력(의존성)이다 — 위 상수 주석 참조.
    """
    detail_parts: list[str] = []
    try:
        new_image = (await adapters.inspect_format("{{.Image}}")).strip()
    except CollectorError as exc:
        artifact.append("deployed_code", {"verdict": UNVERIFIED, "error": str(exc)[:200]})
        return UNVERIFIED, f"이미지 조회 실패: {exc}"
    if new_image == baseline.old_image:
        artifact.append("deployed_code", {"verdict": FAILED, "reason": "image 동일",
                                          "image": new_image})
        return FAILED, "새 컨테이너 Image가 구 Image와 동일 — 재빌드/재배포 미반영"
    detail_parts.append("image 변경 확인")

    probe_results: dict[str, str] = {}
    for month, expected in EXPIRY_BEHAVIOR_PROBE.items():
        try:
            got = (await adapters.expiry_probe(month)).strip()
        except CollectorError as exc:
            artifact.append("deployed_code", {"verdict": UNVERIFIED,
                                              "error": str(exc)[:200],
                                              "probe_month": month})
            return UNVERIFIED, f"동작 probe 실패({month}): {exc}"
        probe_results[month] = got
        if got != expected:
            artifact.append("deployed_code", {"verdict": FAILED, "image": new_image,
                                              "probe": probe_results})
            return FAILED, (
                f"동작 probe 불일치: _compute_expiry_date({month}) = {got} "
                f"!= {expected} — **구 코드가 돌고 있다**"
            )
    detail_parts.append("동작 probe 일치")

    try:
        got_fp = (await adapters.source_fingerprint()).strip()
    except CollectorError as exc:
        artifact.append("deployed_code", {"verdict": UNVERIFIED, "image": new_image,
                                          "probe": probe_results,
                                          "error": str(exc)[:200]})
        return UNVERIFIED, f"소스 지문 조회 실패: {exc}"
    if got_fp != baseline.expected_source_sha256:
        artifact.append("deployed_code", {"verdict": FAILED, "image": new_image,
                                          "probe": probe_results,
                                          "source_sha256": got_fp,
                                          "expected_source_sha256":
                                              baseline.expected_source_sha256})
        return FAILED, (
            f"소스 지문 불일치: 컨테이너 {got_fp[:12]}… != 기대 "
            f"{baseline.expected_source_sha256[:12]}… (revision {baseline.expected_revision[:12]}…) "
            "— 배포하려던 revision이 아니다")

    artifact.append("deployed_code", {"verdict": PASS, "image": new_image,
                                      "probe": probe_results,
                                      "source_sha256": got_fp,
                                      "revision": baseline.expected_revision})
    return PASS, "; ".join(detail_parts + ["소스 지문 일치"])


# `/api/rates`의 최신 관측이 이보다 오래되면 서비스가 데이터를 못 만들고 있다고 본다.
# 배포 창(평일 15:47~17:50 / 06:02~08:30)은 크롤러 빈도가 낮은 구간을 포함하므로
# 넉넉히 잡는다 — 이 축의 목적은 "정지"를 잡는 것이지 지연을 재는 게 아니다.
RATES_MAX_AGE_SECONDS = 1800.0


async def _verify_reachability(adapters: Adapters,
                               artifact: EvidenceArtifact) -> tuple[str, str]:
    """**도달성만** 본다. 서비스가 건강하다는 뜻이 아니다.

    ⚠️ `/health`는 [app/main.py] `health_check()`가 **정적 dict**를 반환한다 —
    `{"status": "healthy", ...}`가 조건 없이 나온다. 그래서 이 축의 PASS가
    증명하는 것은 "프로세스가 HTTP에 응답한다"뿐이다.

    전에는 이 축을 "일반 서비스 게이트"라 부르고 docstring이 FX·USDT·broadcast를
    관측한다고 적었다 — 코드가 하지 않는 일을 문서가 약속한 것이고, 그게 곧
    false confidence다 (외부 검토 지적). 실 서비스 증거는
    `_collect_service_evidence`가 담당한다.
    """
    try:
        payload = await adapters.admin_fetch("/health")
    except CollectorError as exc:
        artifact.append("reachability", {"verdict": UNVERIFIED, "error": str(exc)[:200]})
        return UNVERIFIED, f"/health 조회 실패: {exc}"
    status = str(payload.get("status", "")).lower()
    ok = status in {"healthy", "ok"}
    artifact.append("reachability",
                    {"verdict": PASS if ok else FAILED, "status": status,
                     "means": "HTTP 응답 가능 — 서비스 건강도 아님"})
    return (PASS, "도달성 확인") if ok else (FAILED, f"/health status={status!r}")


async def snapshot_source_freshness(adapters: Adapters) -> dict:
    """`/api/rates` 를 **소스별 최신 관측 시각**으로 접는다.

    ⚠️ 범위: `/api/rates` 는 legacy 정책상 **USDT·KRX 를 제외**한다
    (`app/crud.py` `_filter_source_entries_by_legacy_policy`, Z-2d). 즉 이 축은
    FX(은행+investing)만 덮는다. KRX 는 별도 축(krx-status)이 보고, USDT 는
    이 도구가 덮지 않는다 — runbook 이 그 한계를 적는다.
    """
    payload = await adapters.admin_fetch("/api/rates")
    rates = payload.get("rates")
    if not isinstance(rates, list):
        raise CollectorError("/api/rates 응답에 rates 배열이 없다")
    # ⛔ 빈 배열을 조회 실패(UNVERIFIED)로 접지 않는다 — baseline 의 모든 소스가
    #    사라진 것이고, 그건 관측 불능이 아니라 **회귀**다. 아래 비교가 그렇게 센다.
    newest: dict[str, str] = {}
    for row in rates:
        if not isinstance(row, dict):
            continue
        source = row.get("bank") or row.get("source")
        raw = row.get("timestamp")
        if not source or not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        prev = newest.get(str(source))
        if prev is None or parsed > datetime.fromisoformat(prev):
            newest[str(source)] = parsed.isoformat()
    return newest


def evaluate_service_regression(before: dict, after: dict, now: datetime
                                ) -> tuple[str, list[str], dict]:
    """baseline 대비 소스별 상태. **명확한 것만 판정**하고 나머지는 증거로 남긴다.

    왜 stale 나이로 판정하지 않는가 — 두 방향 모두 실측으로 막혔다:

    1. **절대 임계값은 배포 창에서 오탐한다.** 배포 창은 휴장·장외를 포함하고
       그때 은행 고시는 정상적으로 낡아 있다. 2026-08-17(대체공휴일) 운영 응답:
       sc 248282s / ibk 23529s / bs 16349s — 전부 정상이다.
    2. **`max(timestamp)` 하나는 한 소스만 살아 있어도 통과시킨다.** 같은 응답에서
       30행 중 15행이 30분 초과인데 nh 6초가 전체를 통과시켰다.
    3. **나이 델타도 못 쓴다.** 배포+관측이 ~15분이라, 배포 순간 죽은 소스도
       collect 시점엔 15분치만 낡는다. "죽음"과 "느린 주기"가 그 창 안에서
       구분되지 않는다 — 델타 임계값은 둘 중 하나를 반드시 틀린다.

    그래서 **모호하지 않은 것만** FAILED로 올린다:
      - baseline 에 있던 소스가 응답에서 **사라짐**
      - 관측 시각이 **뒤로 감** (데이터 손상)
    "갱신 없음(advanced=False)"은 판정하지 않고 소스별로 **기록**한다 —
    runbook 8항목 #6이 사람에게 그 목록을 대조하게 한다.
    """
    reasons: list[str] = []
    detail: dict[str, dict] = {}
    for source, before_iso in sorted(before.items()):
        try:
            before_at = datetime.fromisoformat(str(before_iso))
        except (TypeError, ValueError):
            continue
        after_iso = after.get(source)
        if after_iso is None:
            reasons.append(f"{source}: baseline 에 있었는데 응답에서 사라졌다")
            detail[source] = {"before": before_iso, "after": None,
                              "verdict": FAILED}
            continue
        after_at = datetime.fromisoformat(str(after_iso))
        if after_at < before_at:
            reasons.append(
                f"{source}: 관측 시각이 뒤로 갔다 ({before_iso} → {after_iso})")
            detail[source] = {"before": before_iso, "after": after_iso,
                              "verdict": FAILED}
            continue
        detail[source] = {
            "before": before_iso, "after": after_iso,
            "advanced": after_at > before_at,
            "age_seconds": round((now - after_at).total_seconds(), 1),
            # ⛔ 판정이 아니다. 갱신 없음이 죽음인지 느린 주기인지는 이 창에서
            #    구분되지 않으므로 사람이 본다.
            "verdict": PASS if after_at > before_at else OBSERVED,
        }
    verdict = FAILED if reasons else PASS
    return verdict, reasons, detail


async def _collect_service_evidence(adapters: Adapters, baseline: Baseline,
                                    artifact: EvidenceArtifact,
                                    now: datetime) -> tuple[str, str]:
    """**실제 데이터가 흐르는가** — baseline 대비 회귀로 판정한다.

    `/health` 는 정적 dict 라 도달성만 증명한다(`_verify_reachability`). 이 축이
    서비스 실증을 맡는다. dashboard 는 **판정하지 않고 증거로만** 남긴다 —
    임계값을 여기서 정하면 시간대별 정상 범위를 이 도구가 참칭하게 된다.
    """
    if not baseline.service_baseline:
        artifact.append("service", {"verdict": UNVERIFIED,
                                    "error": "baseline 에 서비스 스냅샷이 없다"})
        return UNVERIFIED, "서비스 baseline 부재 — 회귀를 판정할 기준이 없다"
    try:
        after = await snapshot_source_freshness(adapters)
    except CollectorError as exc:
        artifact.append("service", {"verdict": UNVERIFIED, "error": str(exc)[:200]})
        return UNVERIFIED, f"/api/rates 조회 실패: {exc}"

    verdict, reasons, detail = evaluate_service_regression(
        baseline.service_baseline, after, now)

    dashboard: Any = None
    try:
        dashboard = await adapters.admin_fetch("/admin/api/dashboard")
    except CollectorError as exc:
        dashboard = {"error": str(exc)[:200]}

    artifact.append("service", {"verdict": verdict, "per_source": detail,
                                "dashboard": dashboard,
                                "scope": "FX only — /api/rates 는 USDT·KRX 제외"})
    if verdict != PASS:
        return verdict, "; ".join(reasons)
    return PASS, f"서비스 회귀 없음 ({len(detail)} source)"


async def _read_identity(adapters: Adapters) -> ContainerIdentity:
    raw = await adapters.inspect_format(
        "{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}")
    parts = str(raw).strip().split("|")
    if len(parts) != 3:
        raise CollectorError(f"inspect 형식 예상 밖: {str(raw)[:120]}")
    return ContainerIdentity(parts[0], parts[1], int(parts[2]))


async def _capture_events(adapters: Adapters, baseline: Baseline, until_iso: str,
                          artifact: EvidenceArtifact, phase: str) -> list:
    """`[T0, until)` 이벤트를 잡아 artifact 에 남긴다. 실패해도 죽지 않는다.

    이른 시점(첫 정상 응답 직후)과 늦은 시점(축 검사 종료)에 각각 부른다 —
    이유는 호출부 주석 참조(재생 버퍼 256건 상한).
    """
    try:
        events = list(await adapters.container_events(
            baseline.old_container_id, baseline.t0_iso, until_iso))
    except CollectorError as exc:
        artifact.append("events_capture",
                        {"phase": phase, "error": str(exc)[:200]})
        return []
    artifact.append("events_capture", {"phase": phase, "count": len(events),
                                       "until": until_iso})
    return events


def merge_events(*batches: Sequence[dict]) -> list[dict]:
    """여러 번 잡은 이벤트를 **중복 없이** 합친다.

    같은 이벤트가 두 조회에 모두 잡히면 die 가 2건으로 세어져 판정이 흔들린다.
    """
    merged: list[dict] = []
    seen: set[str] = set()
    for batch in batches:
        for event in batch or []:
            key = json.dumps(event, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            merged.append(event)
    return merged


async def _analyze_shutdown(adapters: Adapters, baseline: Baseline, now_iso: str,
                            artifact: EvidenceArtifact,
                            early_events: Optional[Sequence[dict]] = None
                            ) -> DieAnalysis:
    """구 컨테이너 종료·제거. die는 종료만 증명하므로 제거는 별도로 본다.

    이른 조회분(`early_events`)과 늦은 조회분을 **합쳐서** 본다: 이른 쪽은
    버퍼 축출 전의 die 를, 늦은 쪽은 뒤늦게 온 destroy 를 각각 담당한다.
    """
    late = await _capture_events(adapters, baseline, now_iso, artifact, "late")
    events = merge_events(early_events or [], late)
    if not events:
        artifact.append("shutdown", {"verdict": UNVERIFIED,
                                     "error": "이른·늦은 조회 모두 이벤트 0건"})
        return DieAnalysis(UNVERIFIED, 0, None, False, "event 0건 — 관측 불능")
    analysis = analyze_container_events(list(events), baseline.old_container_id)
    removal_probe: Optional[str] = None
    if analysis.status == PASS and not analysis.destroyed:
        # die만으로는 제거를 증명하지 못한다 — inspect not-found로 보강.
        removal_probe = await _classify_removal(adapters, baseline.old_container_id)
        if removal_probe == PASS:
            analysis = replace(
                analysis, destroyed=True,
                detail="정상 종료 + inspect not-found로 제거 확인")
        else:
            analysis = replace(analysis, status=removal_probe,
                               detail="die는 정상이나 제거 증거 없음")
    # ⚠️ record는 probe **이후** 한 번만 쓴다. probe 전에 쓰면 최종 PASS artifact에
    #    "destroy 없음"만 남고 그걸 보강한 증거가 빠져 감사 계약이 불완전해진다.
    artifact.append("shutdown", {"verdict": analysis.status,
                                 "die_count": analysis.die_count,
                                 "exit_code": analysis.exit_code,
                                 "destroyed": analysis.destroyed,
                                 "removal_probe": removal_probe,
                                 "detail": analysis.detail})
    return analysis


async def _classify_removal(adapters: Adapters, old_id: str) -> str:
    try:
        await adapters.inspect_format(f"__probe__{old_id}")
    except CollectorError as exc:
        return classify_inspect_failure(1, str(exc))
    return FAILED  # 조회 성공 = 아직 존재


async def _analyze_logs(adapters: Adapters, baseline: Baseline, now_iso: str,
                        new_started_at: Optional[str],
                        artifact: EvidenceArtifact) -> str:
    """시간창 3분할. 구 프로세스 shutdown 창과 신 프로세스 runtime 창을 나눈다.

    한 창으로 합치면, 배포가 07:00 직후일 때 `[T0, StartedAt)` 사이에 찍힌 **구
    프로세스의 정당한 rollover**까지 FAILED로 오판한다.

    경계는 **새 컨테이너의 실제 `StartedAt`**이다 — 앱 startup(lifespan/bootstrap)
    오류는 전부 StartedAt 이후에 나므로, 그걸 구 프로세스 창에 넣으면 안 된다.
    StartedAt을 못 얻었으면 창을 나눌 수 없으므로 UNVERIFIED다(추정 금지).
    """
    if not new_started_at:
        artifact.append("logs", {"verdict": UNVERIFIED,
                                 "error": "새 컨테이너 StartedAt 미취득 — 창 분할 불가"})
        return UNVERIFIED
    try:
        all_lines = list(await adapters.read_log_lines(baseline.t0_iso, now_iso))
        covered, oldest = covers_window_start(all_lines, baseline.t0_iso)
        if not covered:
            # ⛔ 남은 줄만 보고 "실패 marker 0건"을 내면 "실패가 없었다"가 아니라
            #    "볼 수도 없었다"가 된다. 회전(backupCount 초과)으로 창 앞부분이
            #    사라진 경우가 그렇다 (외부 검토 지적).
            artifact.append("logs", {
                "verdict": UNVERIFIED, "t0": baseline.t0_iso,
                "oldest_retained": oldest,
                "error": "로그 회전으로 [T0, StartedAt) 창 앞부분이 남아 있지 않다"})
            return UNVERIFIED
        shutdown = filter_log_window(all_lines, baseline.t0_iso, new_started_at)
        runtime = filter_log_window(all_lines, new_started_at, now_iso)
    except CollectorError as exc:
        # 로그를 못 읽은 것은 "오류 없음"이 아니다.
        artifact.append("logs", {"verdict": UNVERIFIED, "error": str(exc)[:200]})
        return UNVERIFIED

    unplaceable_total = count_unplaceable(all_lines)
    shutdown_hits = scan_log_markers(shutdown.lines, SHUTDOWN_FAILURE_MARKERS)
    runtime_hits = scan_log_markers(runtime.lines, RUNTIME_FAILURE_MARKERS)
    pre_rollover = scan_log_markers(shutdown.lines, [ROLLOVER_MARKER])
    post_rollover = scan_log_markers(runtime.lines, [ROLLOVER_MARKER])

    artifact.append("logs", {
        "window_pre": {"since": baseline.t0_iso, "until": new_started_at,
                       "lines": len(shutdown.lines)},
        "window_post": {"since": new_started_at, "until": now_iso,
                        "lines": len(runtime.lines)},
        # 배치 불가는 창 속성이 아니라 입력 전체의 속성 — 한 번만 센다.
        "unparsed_total": unplaceable_total,
        "shutdown_failures": shutdown_hits,
        "runtime_failures": runtime_hits,
        "pre_rollover_evidence": pre_rollover,   # 구 프로세스 rollover = 관찰 자료
        "post_rollover": post_rollover,          # 신 프로세스 rollover = 비기대
    })
    if shutdown_hits or runtime_hits or post_rollover:
        return FAILED

    # ⚠️ marker 부재만으로 PASS를 주면 **로그가 유실됐을 때도 PASS**다
    #    (codex 감사 지적). 창이 실제로 관측 가능했는지를 함께 요구한다:
    #      - 배치 불가 줄이 있으면 그 줄에 marker가 있었는지 알 수 없다
    #      - post 창이 통째로 비었으면 회전으로 사라졌거나 로그가 안 쌓인 것이다
    #        (새 프로세스는 startup 로그를 반드시 남긴다)
    if unplaceable_total:
        return UNVERIFIED
    if not runtime.lines:
        return UNVERIFIED
    return PASS


def _finish(artifact: EvidenceArtifact, verdict_value: str, reasons: list[str],
            *, samples: Sequence[str]) -> dict:
    summary = {"verdict": verdict_value, "reasons": reasons,
               "sample_count": len(samples), "samples": list(samples)}
    effective, digest = artifact.finalize(summary)
    if effective != verdict_value:
        reasons = reasons + ["증거 artifact 절단 — 판정을 낮춘다"]
    return {**summary, "verdict": effective, "reasons": reasons,
            "evidence": str(artifact.path), "sha256": digest}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write_exclusive(path: pathlib.Path, data: bytes) -> None:
    """**배타 생성** + fsync. `exists()` 후 `write_text()`는 배타가 아니다.

    두 verifier가 동시에 돌면 TOCTOU로 한쪽이 다른 쪽 baseline을 덮어쓴다.
    "이전 실행 산출물을 덮지 않는다"는 보장은 O_EXCL이 하는 것이지 사전
    `exists()` 검사가 하는 게 아니다 (codex 감사 지적).
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise CollectorError(f"이미 있다(이전 실행 산출물): {path}") from exc
    except OSError as exc:
        raise CollectorError(f"생성 실패 {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise CollectorError(f"기록 실패 {path}: {exc}") from exc


def _resolve_revision(repo_root: pathlib.Path, expected: str) -> str:
    """worktree가 **기대 revision에 clean하게** 있는지 확인 후 full SHA 반환.

    지문을 worktree에서 뜨므로, dirty하거나 다른 commit이면 그 지문은 배포될
    코드의 것이 아니다. → prepare는 `git pull` **뒤**, build **전**에 돌린다.
    """
    def git(*argv: str) -> str:
        proc = subprocess.run(["git", "-C", str(repo_root), *argv],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise CollectorError(f"git {' '.join(argv)} 실패: {proc.stderr.strip()[:200]}")
        return proc.stdout.strip()

    head = git("rev-parse", "HEAD")
    if not head.startswith(expected) and not expected.startswith(head):
        raise CollectorError(
            f"worktree HEAD={head[:12]}… != 기대 revision {expected[:12]}… "
            "— 배포하려는 commit을 checkout한 뒤 prepare할 것")
    dirty = git("status", "--porcelain")
    if dirty:
        raise CollectorError(
            "worktree가 dirty하다 — 지문이 배포될 코드와 달라진다:\n"
            + "\n".join(dirty.splitlines()[:10]))
    return head


async def _prepare(args) -> int:
    target = PRODUCTION_TARGET
    repo_root = pathlib.Path(args.repo_root).resolve()
    revision = _resolve_revision(repo_root, args.expect_revision)
    source_sha = compute_local_source_fingerprint(repo_root)
    fmt = "{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.Image}}"
    raw = await run_command(target.inspect(fmt))
    parts = raw.strip().split("|")
    if len(parts) != 4:
        raise CollectorError(f"inspect 형식 예상 밖: {raw.strip()[:120]}")
    # 배포 **전** 소스별 신선도. 이게 없으면 collect 가 회귀를 판정할 기준이 없다.
    # 실패해도 prepare 를 죽이지 않는다 — collect 가 UNVERIFIED 로 보고한다.
    try:
        service_baseline = await snapshot_source_freshness(
            production_adapters(target))
    except CollectorError as exc:
        print(f"⚠️ 서비스 baseline 스냅샷 실패: {exc}", file=sys.stderr)
        service_baseline = None

    baseline = Baseline(
        t0_iso=_utc_now_iso(),
        old_container_id=parts[0],
        old_started_at=parts[1],
        old_restart_count=int(parts[2]),
        old_image=parts[3],
        expected_code=args.expect_code,
        expected_month=args.expect_month,
        expected_expires_on=args.expect_expires_on,
        expected_revision=revision,
        expected_source_sha256=source_sha,
        service_baseline=service_baseline,
    )
    path = pathlib.Path(args.baseline)
    payload = (baseline.to_json() + "\n").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    # sidecar를 **먼저** 배타 생성한다. baseline만 만들어진 뒤 sidecar 생성이
    # 실패하면 checksum 없는 baseline이 남아 load가 UNVERIFIED로 막히는데,
    # 그 상태에서 재실행하면 baseline이 이미 있어 또 막힌다.
    sidecar = path.parent / (path.name + ".sha256")
    _write_exclusive(sidecar, f"{digest}  {path.name}\n".encode("utf-8"))
    try:
        _write_exclusive(path, payload)
    except CollectorError:
        sidecar.unlink(missing_ok=True)
        raise
    print(json.dumps({"phase": "prepare", "baseline": str(path), "sha256": digest,
                      **asdict(baseline)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="KRX 배포 verifier (prepare/verify)")
    sub = p.add_subparsers(dest="phase", required=True)

    pre = sub.add_parser("prepare", help="배포 직전 baseline 고정 (read + artifact write)")
    pre.add_argument("--baseline", required=True, help="baseline 산출 경로 (기존 파일이면 거부)")
    pre.add_argument("--expect-code", required=True, help="배포 후 기대 계약 코드 (예: A75609)")
    pre.add_argument("--expect-month", required=True, help="기대 월물 (예: 202609)")
    pre.add_argument("--expect-expires-on", required=True, help="기대 만기 (예: 2026-09-21)")
    pre.add_argument("--expect-revision", required=True,
                     help="배포할 commit SHA — worktree HEAD가 이것이고 clean해야 한다")
    pre.add_argument("--repo-root", default=str(pathlib.Path(__file__).resolve().parent.parent),
                     help="지문을 뜰 worktree 경로 (기본: 이 스크립트의 리포)")

    ver = sub.add_parser(
        "collect",
        help="배포 후 증거 수집 (baseline 필수). **자동 승인 아님** — runbook 대조 필요")
    ver.add_argument("--baseline", required=True)
    ver.add_argument("--evidence", required=True, help="증거 artifact 경로 (기존 파일이면 거부)")
    ver.add_argument("--poll-seconds", type=float, default=45.0)
    ver.add_argument("--startup-deadline-seconds", type=float, default=600.0)
    ver.add_argument("--verify-deadline-seconds", type=float, default=720.0)
    ver.add_argument("--observe-seconds", type=float, default=720.0,
                     help="첫 정상 응답 후 관측을 계속할 최소 시간 (runbook: 12분)")
    return p


def production_adapters(target: Target = PRODUCTION_TARGET) -> Adapters:
    """운영 어댑터 배선. 명령 **리터럴만** 여기 모여 있고 판정 로직은 전부 위에 있다.

    ⚠️ `docker events --filter`·`--format` 플래그는 **운영 호스트에서 사전 확인**
    해야 한다. 이 리포는 `docker builder prune --max-used-space`가 다른 버전에서
    이름이 다른 사례를 이미 겪었다 — 문서에 적힌 이름이 그 서버에서 유효하다는
    보장은 없다.
    """
    async def admin_fetch(path: str) -> dict:
        return parse_curl_response(await run_command(admin_fetch_command(path, target)))

    async def inspect_format(fmt: str):
        if fmt.startswith("__probe__"):
            cid = fmt[len("__probe__"):]
            return await run_command(["docker", "inspect", cid, "--format", "{{.Id}}"])
        return await run_command(target.inspect(fmt))

    async def container_events(cid: str, since: str, until: str):
        raw = await run_command([
            "docker", "events", "--since", since, "--until", until,
            "--filter", f"container={cid}", "--format", "{{json .}}",
        ])
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                raise CollectorError(f"event JSON 파싱 실패: {exc}") from exc
        return out

    async def read_log_lines(since: str, until: str):
        """host bind mount 로그 전체를 읽는다 (창 분할은 `filter_log_window`가 한다).

        `docker logs`는 컨테이너 재생성 시 구 로그가 사라지므로 bind mount를 본다.
        rotation 때문에 `app.log*` 전체가 대상이다.
        ⛔ `2>/dev/null || true` 로 감싸지 않는다 — 로그 부재·권한 오류가 빈 로그로
           바뀌면 그대로 false PASS 가 된다. 실패는 CollectorError 로 올려 UNVERIFIED.
        """
        log_dir = REPO_ROOT / "volumes" / "logs" / "app"
        paths = sorted(log_dir.glob("app.log*"))
        if not paths:
            raise CollectorError(f"로그 파일 없음: {log_dir}/app.log*")
        lines: list[str] = []
        for path in paths:
            try:
                lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError as exc:
                raise CollectorError(f"로그 읽기 실패 {path.name}: {exc}") from exc
        return lines

    async def expiry_probe(contract_month: str) -> str:
        """컨테이너 **안에서** 만기 계산을 실행 — 배포된 코드의 실제 동작을 본다.

        이미지 태그 비교와 독립된 축이다. 태그만 갈아끼운 경우도 여기서 걸린다.
        """
        code = (
            "from app.sources.kis_master import _compute_expiry_date;"
            f"print(_compute_expiry_date({contract_month!r}))"
        )
        return await run_command(
            target.compose("exec", "-T", "fastapi", "python", "-c", code))

    async def source_fingerprint() -> str:
        """컨테이너 안 리포 payload 지문 — 배포된 **revision**을 가른다.

        범위는 `SOURCE_FINGERPRINT_ROOTS`(Dockerfile COPY 집합)다. `app/`만
        보면 뚫린다 — 그 반례가 실제로 있었다.
        """
        return await run_command(
            target.compose("exec", "-T", "fastapi", "python", "-c",
                           SOURCE_FINGERPRINT_PROGRAM))

    async def sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    return Adapters(admin_fetch=admin_fetch, inspect_format=inspect_format,
                    container_events=container_events, read_log_lines=read_log_lines,
                    expiry_probe=expiry_probe, source_fingerprint=source_fingerprint,
                    now=lambda: datetime.now(timezone.utc), sleep=sleep)


async def _collect(args) -> int:
    baseline = Baseline.load(pathlib.Path(args.baseline))
    evidence = pathlib.Path(args.evidence)
    if evidence.exists():
        raise CollectorError(f"evidence가 이미 있다(이전 실행 산출물): {evidence}")
    artifact = EvidenceArtifact(evidence)
    result = await collect_evidence(
        baseline, production_adapters(), artifact,
        Deadlines(startup_seconds=args.startup_deadline_seconds,
                  verify_seconds=args.verify_deadline_seconds,
                  observe_seconds=args.observe_seconds,
                  poll_seconds=args.poll_seconds),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["verdict"] != PASS:
        print("\n⛔ 차단 소견이 있다 — 후속(일봉 정정) 진행 금지.", file=sys.stderr)
        return 1
    print("\n⚠️ 차단 소견 없음. 이것은 **자동 승인이 아니다** — "
          "KRX_CANARY.md 배포 runbook 체크리스트를 사람이 대조해 최종 판정할 것.",
          file=sys.stderr)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.phase == "prepare":
            return asyncio.run(_prepare(args))
        return asyncio.run(_collect(args))
    except CollectorError as exc:
        print(json.dumps({"verdict": UNVERIFIED, "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
