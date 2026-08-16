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

    def finalize(self, summary: dict) -> str:
        """terminal record 기록 → sha256 계산 → **이후 쓰기 거부**."""
        if self._finalized:
            raise RuntimeError("이미 finalize됐다 — 재-finalize는 checksum을 무의미하게 한다")
        if self.truncated:
            summary = {**summary, "truncated": True,
                       "verdict": worst(summary.get("verdict", UNVERIFIED), UNVERIFIED)}
        self.append("terminal", summary, terminal=True)
        self._finalized = True
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        (self.path.parent / (self.path.name + ".sha256")).write_text(
            f"{digest}  {self.path.name}\n", encoding="utf-8"
        )
        return digest


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

    @property
    def expected_contract(self) -> ContractTuple:
        return ContractTuple(self.expected_code, self.expected_month,
                             self.expected_expires_on)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True, indent=2)

    @classmethod
    def load(cls, path: pathlib.Path) -> "Baseline":
        try:
            raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CollectorError(f"baseline 읽기 실패: {exc}") from exc
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


def continuity_holds(samples: Sequence[ContainerIdentity]) -> bool:
    """전 poll에서 컨테이너 identity가 동일한가.

    `rollover_count`는 프로세스 재시작 시 0으로 초기화되므로, 검증 창 중간에
    재시작이 끼면 "count==0"이 "rollover 없었음"의 위증이 된다. ID만으로는
    부족하다 — 같은 ID로도 `docker restart`가 가능하므로 StartedAt·RestartCount
    까지 3-tuple로 본다.
    """
    return len(set(samples)) <= 1 if samples else False


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
    """
    startup_seconds: float = 600.0
    verify_seconds: float = 720.0
    poll_seconds: float = 45.0


async def run_verify(baseline: Baseline, adapters: Adapters,
                     artifact: EvidenceArtifact,
                     deadlines: Deadlines = Deadlines()) -> dict:
    """배포 후 게이트 전체. 판정만 하고 **아무것도 변경하지 않는다**(artifact 제외).

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
        identities.append(identity)

        sample_verdict, reasons = evaluate_post_sample(
            status, baseline, deadline_passed=deadline_passed)
        samples.append(sample_verdict)
        artifact.append("sample", {"verdict": sample_verdict, "reasons": reasons,
                                   "status": status, "identity": asdict(identity)})

        if sample_verdict == FAILED:
            break
        if sample_verdict == PASS:
            break
        if deadline_passed:
            samples[-1] = FAILED
            break
        await adapters.sleep(deadlines.poll_seconds)

    reasons: list[str] = []
    sample_result = aggregate(samples)

    continuity = PASS if continuity_holds(identities) else FAILED
    if continuity == FAILED:
        reasons.append("검증 창 중간 컨테이너 identity 변경 (rollover_count 신뢰 불가)")

    if new_identity is not None and new_identity.container_id == baseline.old_container_id:
        continuity = FAILED
        reasons.append("새 container ID가 구 ID와 동일 — 재생성되지 않았다")

    now_iso = adapters.now().isoformat()
    die = await _analyze_shutdown(adapters, baseline, now_iso, artifact)
    if die.status != PASS:
        reasons.append(f"구 컨테이너 종료 판정: {die.detail}")

    logs = await _analyze_logs(
        adapters, baseline, now_iso,
        new_identity.started_at if new_identity else None, artifact)
    if logs != PASS:
        reasons.append("로그 축 이상 (marker 검출 또는 취득 실패)")

    final = worst(sample_result, continuity, die.status, logs)
    return _finish(artifact, final, reasons, samples=samples)


async def _read_identity(adapters: Adapters) -> ContainerIdentity:
    raw = await adapters.inspect_format(
        "{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}")
    parts = str(raw).strip().split("|")
    if len(parts) != 3:
        raise CollectorError(f"inspect 형식 예상 밖: {str(raw)[:120]}")
    return ContainerIdentity(parts[0], parts[1], int(parts[2]))


async def _analyze_shutdown(adapters: Adapters, baseline: Baseline, now_iso: str,
                            artifact: EvidenceArtifact) -> DieAnalysis:
    """구 컨테이너 종료·제거. die는 종료만 증명하므로 제거는 별도로 본다."""
    try:
        events = await adapters.container_events(
            baseline.old_container_id, baseline.t0_iso, now_iso)
    except CollectorError as exc:
        artifact.append("shutdown", {"verdict": UNVERIFIED, "error": str(exc)[:200]})
        return DieAnalysis(UNVERIFIED, 0, None, False, f"event 조회 실패: {exc}")
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
        shutdown = filter_log_window(all_lines, baseline.t0_iso, new_started_at)
        runtime = filter_log_window(all_lines, new_started_at, now_iso)
    except CollectorError as exc:
        # 로그를 못 읽은 것은 "오류 없음"이 아니다.
        artifact.append("logs", {"verdict": UNVERIFIED, "error": str(exc)[:200]})
        return UNVERIFIED

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
        "unparsed_total": count_unplaceable(all_lines),
        "shutdown_failures": shutdown_hits,
        "runtime_failures": runtime_hits,
        "pre_rollover_evidence": pre_rollover,   # 구 프로세스 rollover = 관찰 자료
        "post_rollover": post_rollover,          # 신 프로세스 rollover = 비기대
    })
    if shutdown_hits or runtime_hits or post_rollover:
        return FAILED
    return PASS


def _finish(artifact: EvidenceArtifact, verdict_value: str, reasons: list[str],
            *, samples: Sequence[str]) -> dict:
    summary = {"verdict": verdict_value, "reasons": reasons,
               "sample_count": len(samples), "samples": list(samples)}
    digest = artifact.finalize(summary)
    return {**summary, "evidence": str(artifact.path), "sha256": digest}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _prepare(args) -> int:
    target = PRODUCTION_TARGET
    fmt = "{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.Image}}"
    raw = await run_command(target.inspect(fmt))
    parts = raw.strip().split("|")
    if len(parts) != 4:
        raise CollectorError(f"inspect 형식 예상 밖: {raw.strip()[:120]}")
    baseline = Baseline(
        t0_iso=_utc_now_iso(),
        old_container_id=parts[0],
        old_started_at=parts[1],
        old_restart_count=int(parts[2]),
        old_image=parts[3],
        expected_code=args.expect_code,
        expected_month=args.expect_month,
        expected_expires_on=args.expect_expires_on,
    )
    path = pathlib.Path(args.baseline)
    if path.exists():
        raise CollectorError(f"baseline이 이미 있다(이전 실행 산출물): {path}")
    path.write_text(baseline.to_json() + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (path.parent / (path.name + ".sha256")).write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )
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

    ver = sub.add_parser("verify", help="배포 후 게이트 (baseline 필수)")
    ver.add_argument("--baseline", required=True)
    ver.add_argument("--evidence", required=True, help="증거 artifact 경로 (기존 파일이면 거부)")
    ver.add_argument("--poll-seconds", type=float, default=45.0)
    ver.add_argument("--startup-deadline-seconds", type=float, default=600.0)
    ver.add_argument("--verify-deadline-seconds", type=float, default=720.0)
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

    async def sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    return Adapters(admin_fetch=admin_fetch, inspect_format=inspect_format,
                    container_events=container_events, read_log_lines=read_log_lines,
                    now=lambda: datetime.now(timezone.utc), sleep=sleep)


async def _verify(args) -> int:
    baseline = Baseline.load(pathlib.Path(args.baseline))
    evidence = pathlib.Path(args.evidence)
    if evidence.exists():
        raise CollectorError(f"evidence가 이미 있다(이전 실행 산출물): {evidence}")
    artifact = EvidenceArtifact(evidence)
    result = await run_verify(
        baseline, production_adapters(), artifact,
        Deadlines(startup_seconds=args.startup_deadline_seconds,
                  verify_seconds=args.verify_deadline_seconds,
                  poll_seconds=args.poll_seconds),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # PASS만 0. UNVERIFIED/FAILED는 후속(일봉 정정)을 차단한다.
    return 0 if result["verdict"] == PASS else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.phase == "prepare":
            return asyncio.run(_prepare(args))
        return asyncio.run(_verify(args))
    except CollectorError as exc:
        print(json.dumps({"verdict": UNVERIFIED, "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
