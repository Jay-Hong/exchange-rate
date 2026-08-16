#!/usr/bin/env python3
"""S5 — auth_executor 제출/시작 장부 변이 배터리.

⛔ **종료 코드만 보고 KILLED 라 하지 말 것.** 인터프리터에 pytest 가 없거나 수집이 깨져도
   비-0 이 나와 *모든* 변이가 KILLED 로 보인다 — 판정기가 무엇을 넣어도 통과하는 형태가 된다
   (codex 지적). 그래서 이 하네스는:
     1. **무변이 기준선**을 먼저 돌려 exit 0 이 아니면 즉시 중단하고,
     2. pytest 종료 코드를 **테스트 실패(1)** 와 **인프라 오류(2·3·4·5)** 로 가르며,
     3. 패턴 불일치(INVALID)를 통과가 아니라 **실패**로 센다.
   복원은 `try/finally` + sha256 대조로 보증한다.

사용: python scripts/mutation_auth_executor_ledger.py
"""

# 표준 라이브러리
import ast
import hashlib
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "app" / "auth_executor.py"
# ⛔ **probe 는 변이마다 다르다.** 단일 테스트 집합으로 돌리면 "무엇이 죽였는가" 가 합쳐져
#    귀속이 불가능하다 — 무수식 cross-lane 변이는 trip-wire 와 행동 테스트가 **둘 다** 잡는데,
#    한 번에 돌리면 어느 쪽이 공허해도 초록으로 보인다.
PROBES = {
    "s5":        ["tests/test_auth_executor_ledger.py", "tests/test_auth_executor.py"],
    # ⛔ WS facade 경로와 generic lane 경로를 **나눈다** — 합치면 어느 쪽이 잠갔는지 모른다.
    "wslog":     ["tests/test_auth_executor.py"],
    "lanelog":   ["tests/test_auth_executor_lane.py::TestTwoLanes::"
                  "test_log_flag_is_read_at_call_time_per_lane"],
    "tripwire":  ["tests/test_auth_executor_lane.py::"
                  "test_lane_never_references_its_own_names_unqualified"],
    "twolane":   ["tests/test_auth_executor_lane.py::TestTwoLanes"],
    "startupscope": ["tests/test_auth_lane_startup_scope.py"],
}
DEFAULT_PROBES = ("s5",)

# ⛔ 줄 **중간 표현식** 앵커만 줄경계 검사에서 면제한다. 목록을 명시하지 않으면 규칙이
#    "대충 넘어간다" 가 되어 위 결함이 그대로 돌아온다.
def _lane_owned_names() -> tuple[str, ...]:
    """⛔ **소스에서 도출**한다 — 수기 목록이면 새 메서드·상태가 늘 때 조용히 낡는다."""
    tree = ast.parse(SRC.read_text())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "AuthExecutorLane")
    owned = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for n in ast.walk(cls):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
            owned.add(n.attr)
    return tuple(sorted(x for x in owned if x.startswith("_")))


LANE_OWNED = _lane_owned_names()

MID_LINE_ANCHORS: frozenset[str] = frozenset({
    "(time.monotonic() - exec_started) * 1000.0",
})

# 변이별 필수 probe (미지정은 DEFAULT_PROBES). 신규 2종은 아래에서 채운다.
MUTANT_PROBES: dict[str, tuple[str, ...]] = {
    "S1a′-6 rollback 실패를 로그로도 안 남김": ("startupscope",),
    "S1a′-7 진단 로깅 보호 제거(원인 마스킹)": ("startupscope",),

    "S1a′-1 try 밖": ("startupscope",),
    "S1a′-2 created 무시": ("startupscope",),
    "S1a′-3 BaseException→Exception": ("startupscope",),
    "S1a′-4 합류 생략": ("startupscope",),
    "S1a′-5 rollback 실패가 startup 원인을 마스킹": ("startupscope",),

    # ⛔ 각 probe 가 **독립적으로** 죽여야 KILLED — 합쳐 돌리면 한쪽이 공허해도 안 보인다.
    "S1a-① log flag 를 생성 시점 bool 로 캡처": ("wslog", "lanelog"),
    "S1a-② lane-owned 호출을 무수식으로(cross-lane 오작동)": ("tripwire", "twolane"),
}

# pytest 종료 코드: 0 통과 · 1 테스트 실패 · 2 중단 · 3 내부오류 · 4 사용오류 · 5 수집 0건
_TEST_FAILED = 1

MUTANTS: list[tuple[str, list[tuple[str, str]]]] = [
    ('① 예약을 submit 뒤로',
     [('        self._note_submitted()               # ⛔ submit **앞**(변이 ①) — 그리고 **시계보다도 앞**\n', ''),
      ('        self._safe_ledger(self._note_submit_succeeded)', '        self._note_submitted()\n        self._safe_ledger(self._note_submit_succeeded)')]),
    ('② submit 실패 상계 제거',
     [('            self._note_submit_failed()       # 장부 상계 후 그대로 재전파 (변이 ②)\n            raise', '            raise')]),
    ('③ in_flight 감소를 _record 밖으로',
     [('            if worker_finished:\n                if self._metrics["in_flight"] <= 0:   # ⛔ 음수면 reset 가드(`> 0`)가 영구히 무력해진다\n                    raise RuntimeError("in_flight underflow — worker_finished 가 잘못 전달됐다")\n                self._metrics["in_flight"] -= 1\n', '')]),
    ('④ reset 이 max 를 0 으로',
     [('        new["in_flight_max"] = new["in_flight"]\n', '        new["in_flight_max"] = 0\n')]),
    ('④b reset 이 in_flight 까지 0 으로',
     [('        new["in_flight_max"] = new["in_flight"]\n', '        new["in_flight"] = 0\n        new["in_flight_max"] = new["in_flight"]\n')]),
    ('⑥ reset 에서 신규 카운터 누락',
     [('                   submitted_total=0, submit_failed_total=0, started_total=0,\n                   ledger_errors_total=0, by_outcome={})\n', '                   by_outcome={})\n')]),
    ('⑦ peak 을 제출 시점에 갱신',
     [('    def _note_submitted(self) -> None:\n        with self._metrics_lock:\n            self._metrics["submitted_total"] += 1', '    def _note_submitted(self) -> None:\n        with self._metrics_lock:\n            self._metrics["submitted_total"] += 1\n            q = self._queued_locked()\n            if q > self._metrics["queued_observed_max"]: self._metrics["queued_observed_max"] = q')]),
    ('⑧ 활성 중 reset 거부 제거',
     [('            # ⚠️ `!= 0` 이다 — 음수도 거부한다. 지켜지는 것은 **불균형** 신호이지 `ledger_errors_total`\n            #    이 아니다(균형이 맞으면 그 카운터는 reset 과 함께 0 으로 내려간다).\n            if self._metrics["in_flight"] > 0 or self._queued_locked() != 0:\n                raise AuthExecutorMetricsBusy(\n                    f"활성 작업 중 reset 거부 (in_flight={self._metrics[\'in_flight\']}, "\n                    f"queued={self._queued_locked()}, ledger_errors={self._metrics[\'ledger_errors_total\']})")\n', '')]),
    ('High2 장부 기록을 started_evt 앞으로',
     [('                started_evt.set()\n                start_receipt: list[bool] = []\n                self._safe_ledger(lambda: self._note_worker_started(start_receipt))   # 실패해도 업무는 계속\n', '                start_receipt: list[bool] = []\n                self._safe_ledger(lambda: self._note_worker_started(start_receipt))   # 실패해도 업무는 계속\n                started_evt.set()\n')]),
    ('High3a 시계를 장부 앞으로',
     [('            # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가\n            #    영구 잔존한다(codex 재현). 경계는 그대로: Event·클로저 생성 **앞**.\n            submitted = time.monotonic()\n', ''),
      ('        self._note_submitted()               # ⛔ submit **앞**(변이 ①) — 그리고 **시계보다도 앞**\n', '        submitted = time.monotonic()\n        self._note_submitted()               # ⛔ submit **앞**(변이 ①) — 그리고 **시계보다도 앞**\n')]),
    ('High3b execution 을 dequeue 시각부터',
     [('(time.monotonic() - exec_started) * 1000.0', '(time.monotonic() - dequeued) * 1000.0')]),
    ('High3c 시계를 Event 생성 뒤로',
     [('            # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가\n            #    영구 잔존한다(codex 재현). 경계는 그대로: Event·클로저 생성 **앞**.\n            submitted = time.monotonic()\n', ''),
      ('            finished_evt = threading.Event()\n', '            finished_evt = threading.Event()\n            submitted = time.monotonic()\n')]),
    ('Major-A Event 생성을 보상 범위 밖으로',
     [('            started_evt = threading.Event()\n            finished_evt = threading.Event()\n', ''),
      ('        try:\n            # ⚠️ 시계도', '        started_evt = threading.Event()\n        finished_evt = threading.Event()\n        try:\n            # ⚠️ 시계도')]),
    ('Major-B 제출 후 부기를 치명으로',
     [('        self._safe_ledger(self._note_submit_succeeded)', '        self._note_submit_succeeded()')]),
    ('Major-C 시계를 보상 범위 밖으로',
     [('        try:\n            # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가', '        submitted = time.monotonic()\n        try:\n            # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가'),
      ('            # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가\n            #    영구 잔존한다(codex 재현). 경계는 그대로: Event·클로저 생성 **앞**.\n            submitted = time.monotonic()\n', '')]),
    ('Major-D 진단 로깅 보호 제거',
     [('            try:                                    # ⛔ 진단 로깅 실패가 업무 결과를 대체하면 안 된다\n                logger.debug("auth_executor 장부 기록 실패", exc_info=True)\n            except Exception:                       # pragma: no cover - 최후 방어\n                pass\n', '            logger.debug("auth_executor 장부 기록 실패", exc_info=True)\n')]),
    ('Major-E receipt 대신 반환값으로 커밋 판정',
     [('            receipt.append(True)', '            pass'),
      ('                started_ok = bool(start_receipt)', '                started_ok = self._safe_ledger(lambda: self._note_worker_started(start_receipt))')]),
    ('Major-F helper 를 순차 in-place 갱신으로',
     [('            new = dict(self._metrics)\n            new["started_total"] += 1\n            new["in_flight"] += 1\n            new["in_flight_max"] = max(new["in_flight_max"], new["in_flight"])\n            # ⚠️ receipt 는 커밋 **직전**. 이 뒤 update 가 실패하면 카운터는 안 올랐는데 receipt 만\n            #    남지만, 그 경우는 `_record` 의 underflow 가드가 무해하게 흡수한다(음수 방지).\n            receipt.append(True)\n            self._metrics = new              # ⛔ **단일 리바인딩**. `update()` 는 기존 dict 를 제자리\n                                        #    갱신해 부분 실패가 가능하다 — copy-and-swap 이 아니다.\n', '            self._metrics["started_total"] += 1\n            self._metrics["in_flight"] += 1\n            if self._metrics["in_flight"] > self._metrics["in_flight_max"]:\n                self._metrics["in_flight_max"] = self._metrics["in_flight"]\n            receipt.append(True)\n')]),
    ('Major-G 리바인딩을 제자리 update 로',
     [('            self._metrics = new              # ⛔ **단일 리바인딩**. `update()` 는 기존 dict 를 제자리\n                                        #    갱신해 부분 실패가 가능하다 — copy-and-swap 이 아니다.\n', '            self._metrics.update(new)\n')]),
    ('Major-H 장부 오염 시 queue 무시(완화 재도입)',
     [('            if self._metrics["in_flight"] > 0 or self._queued_locked() != 0:\n', '            trust_queue = self._metrics["ledger_errors_total"] == 0\n            if self._metrics["in_flight"] > 0 or (trust_queue and self._queued_locked() > 0):\n')]),
    ('Major-I drain 미증명 force 경로 재도입',
     [('            # ⚠️ `!= 0` 이다 — 음수도 거부한다. 지켜지는 것은 **불균형** 신호이지 `ledger_errors_total`\n', '            if self._executor is None:          # 변이: drain 미증명 bypass 재도입\n                self._reset_locked()\n                return\n            # ⚠️ `!= 0` 이다 — 음수도 거부한다. 지켜지는 것은 **불균형** 신호이지 `ledger_errors_total`\n')]),
    ('(c) in_flight underflow 가드 제거',
     [('                if self._metrics["in_flight"] <= 0:   # ⛔ 음수면 reset 가드(`> 0`)가 영구히 무력해진다\n                    raise RuntimeError("in_flight underflow — worker_finished 가 잘못 전달됐다")\n', '')]),
    ('S1a-① log flag 를 생성 시점 bool 로 캡처',
     [('        self._log_timings = log_timings', '        _captured = log_timings()\n        self._log_timings = lambda: _captured')]),
    ('S1a-② lane-owned 호출을 무수식으로(cross-lane 오작동)',
     [('        executor = self.begin_auth_executor_shutdown()', '        executor = begin_auth_executor_shutdown()')]),
    ('S1a′-1 try 밖',
     [('    try:\n        start_auth_executor(max_workers)\n        yield', '    start_auth_executor(max_workers)\n    try:\n        yield')]),
    ('S1a′-2 created 무시',
     [('        if created:\n            # ⛔ **rollback 실패가 startup 원인을 덮으면 안 된다.**', '        if True:\n            # ⛔ **rollback 실패가 startup 원인을 덮으면 안 된다.**')]),
    ('S1a′-3 BaseException→Exception',
     [('    except BaseException:\n        if created:', '    except Exception:\n        if created:')]),
    ('S1a′-4 합류 생략',
     [('                await await_auth_executor_shutdown(executor)\n', '')]),
    ('S1a′-5 rollback 실패가 startup 원인을 마스킹',
     [('            except BaseException:      # noqa: BLE001 — 원인 보존이 우선이다\n',
       '            except BaseException:\n                raise\n')]),
    ('S1a′-6 rollback 실패를 로그로도 안 남김',
     [('                try:\n                    logger.exception("auth lane rollback 실패 — startup 원인을 그대로 올린다")\n                except BaseException:  # pragma: no cover - 최후 방어\n                    pass', '                pass')]),
    ('S1a′-7 진단 로깅 보호 제거(원인 마스킹)',
     [('                try:\n                    logger.exception("auth lane rollback 실패 — startup 원인을 그대로 올린다")\n                except BaseException:  # pragma: no cover - 최후 방어\n                    pass', '                logger.exception("auth lane rollback 실패 — startup 원인을 그대로 올린다")')]),
]


def _run(probe: str) -> subprocess.CompletedProcess:
    """probe 하나만 돌린다 — **귀속의 단위**다."""
    return subprocess.run([sys.executable, "-m", "pytest", *PROBES[probe],
                           "-q", "-p", "no:asyncio", "-x"],
                          capture_output=True, text=True, cwd=REPO)


def validate_mapping() -> list[str]:
    """실행 **전에** mapping 을 거부한다 — 오타는 조용히 DEFAULT_PROBES 로 떨어진다.

    ⛔ 그러면 귀속 게이트가 있는 것처럼 보이면서 실제로는 기본 probe 만 돌아간다.
    ⚠️ probes 를 mutant 레코드 안에 직접 두는 편이 더 안전하다(이름 매칭 자체가 사라진다) —
       현행 22개 레코드를 건드리지 않으려고 mapping 을 쓰되, **엄격 검증**으로 구멍을 막는다.
    """
    errs: list[str] = []
    names = [n for n, _ in MUTANTS]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        errs.append(f"변이 이름 중복: {sorted(dup)}")
    for key, probes in MUTANT_PROBES.items():
        if key not in names:
            errs.append(f"MUTANT_PROBES 의 키가 변이 이름에 없다(오타?): {key!r}")
        if not probes:
            errs.append(f"{key!r} 의 probe 가 비었다")
        for pr in probes:
            if pr not in PROBES:
                errs.append(f"{key!r} 가 모르는 probe 를 가리킨다: {pr!r}")
    for pr, sel in PROBES.items():
        if not sel:
            errs.append(f"probe {pr!r} 의 선택자가 비었다")
        for s in sel:
            if not str(s).strip():          # ⛔ [""] 도 "비었음" 이다
                errs.append(f"probe {pr!r} 에 빈 선택자가 있다")
    # ⛔ 기본 probe 도 검증한다 — 여기가 비거나 모르는 이름이면 **매핑 없는 변이 전부**가
    #    조용히 아무것도 안 돌리거나 죽는다(재현: () 와 ("does-not-exist",) 둘 다 통과했다).
    if not DEFAULT_PROBES:
        errs.append("DEFAULT_PROBES 가 비었다")
    for pr in DEFAULT_PROBES:
        if pr not in PROBES:
            errs.append(f"DEFAULT_PROBES 가 모르는 probe 를 가리킨다: {pr!r}")
    return errs


def apply_pairs(source: str, pairs: list[tuple[str, str]]) -> tuple[str | None, str]:
    """변이 적용 — **순수 함수**라 영구 테스트로 잠근다. 반환 (결과, 사유). 실패면 (None, 사유).

    ⛔ 검사는 **line-start alignment** 다 — 앵커 **시작**이 줄머리인지만 본다. 끝 경계는 보지
       않는다(주석 줄 중간에서 끝나는 앵커가 실제로 있다 — Major-A). 시작만 정렬돼도
       들여쓰기는 보존된다.
    ⛔ `count == 1` 만으로는 부족하다: 클래스 안 8칸 줄에 4칸 부분문자열이 1회 들어가도
       통과하고, 그 변이는 들여쓰기가 깨진 코드를 만든다(실측 7건).
    """
    cur = source
    for old, new in pairs:
        if cur.count(old) != 1:
            return None, f"앵커 발생 {cur.count(old)}회"
        i = cur.find(old)
        if old not in MID_LINE_ANCHORS and not (i == 0 or cur[i - 1] == "\n"):
            return None, "앵커가 줄머리에서 시작하지 않는다(line-start 미정렬)"
        nxt = cur.replace(old, new, 1)
        if nxt == cur:
            return None, "pair no-op (old == new)"
        cur = nxt
    if cur == source:
        return None, "전체 no-op"
    try:
        ast.parse(cur)
    except SyntaxError as exc:
        return None, f"구문 파괴: {exc.msg}"
    return cur, "ok"


def false_kill_name(verdict: str, stdout: str) -> str | None:
    """거짓 KILLED 판정 — **순수 함수**. lane-owned 이름의 NameError 만, **KILLED 일 때만**.

    ⛔ verdict 앞에 두면 정당한 mutation-dependent NameError 도 INVALID 가 되고
       `{1,2}` 같은 INFRA 우선 결과까지 덮는다(codex 지적).
    ⚠️ stdout 만 본다. lane-owned 집합이 전부 소문자·밑줄이라 대문자·숫자 이름은 범위 밖이다.
    """
    if verdict != "KILLED":
        return None
    m = re.search(r"NameError: name '(" + "|".join(LANE_OWNED) + r")' is not defined", stdout)
    return m.group(1) if m else None


def classify(rcs: dict[str, int]) -> str:
    """probe별 exit code → 판정. **순수 함수**라 단위 테스트로 잠근다.

    ⛔ 순서가 계약이다: 인프라 오류가 하나라도 있으면 다른 probe 결과와 **무관하게** INFRA 다.
       한때 `any(rc == 1)` 을 먼저 봐서 {1, 2} 를 SURVIVED 로 접었다 — 인프라 실패를 판별력
       결론으로 바꾸는 오분류다(codex 지적).
    """
    if not rcs:
        return "INFRA"
    # ⛔ 계약은 **"0 과 1 외에는 전부 INFRA"** 다. 한때 알려진 코드 집합을 따로 두고
    #    `rc in <집합> or rc not in (0, 1)` 로 적었는데 앞 조건이 뒤에 완전히 포함돼
    #    집합을 바꿔도 판정이 안 변했다 — 단일 출처처럼 보이지만 장식이었다(codex 지적).
    if any(rc not in (0, _TEST_FAILED) for rc in rcs.values()):
        return "INFRA"
    if all(rc == _TEST_FAILED for rc in rcs.values()):
        return "KILLED"
    return "SURVIVED"


def _probes_for(name: str) -> tuple[str, ...]:
    """변이 이름 → 필수 probe. **모든** 필수 probe 가 각각 exit 1 이어야 KILLED 다."""
    return MUTANT_PROBES.get(name, DEFAULT_PROBES)


def main() -> int:
    original = SRC.read_text()
    digest = hashlib.sha256(original.encode()).hexdigest()

    try:
        import pytest  # noqa: F401
    except ImportError:
        print(f"❌ INFRA: {sys.executable} 에 pytest 가 없다 — 이대로 돌리면 모든 변이가 KILLED 로 보인다")
        return 2

    # ⛔ **probe 마다** 무변이 기준선을 따로 확인한다 — 하나가 이미 red 면 그 probe 의 KILLED 는
    #    변이가 아니라 기존 실패를 본 것이다.
    map_errs = validate_mapping()
    if map_errs:
        print("❌ INFRA: probe mapping 오류 — 판정 전에 거부한다")
        for e in map_errs:
            print(f"   · {e}")
        return 2
    used = sorted({p for n, _ in MUTANTS for p in _probes_for(n)})
    for probe in used:
        b = _run(probe)
        if b.returncode != 0:
            print(f"❌ INFRA: probe '{probe}' 무변이 기준선이 exit {b.returncode} — 판정 불가")
            print(b.stdout[-1200:])
            return 2
    print(f"✅ 기준선 green — probe {used}\n")

    killed = survived = invalid = infra = 0
    try:
        for name, pairs in MUTANTS:
            mutated, why = apply_pairs(original, pairs)
            if mutated is None:
                print(f"  INVALID  {name}  ← {why}")
                invalid += 1
                continue
            SRC.write_text(mutated)
            probes = _probes_for(name)
            results = {pr: _run(pr) for pr in probes}
            rcs = {pr: r.returncode for pr, r in results.items()}
            # ⛔ **모든** 필수 probe 가 각각 exit 1 이어야 KILLED — 하나라도 초록이면 그 probe 는
            #    이 변이에 대해 공허하다는 뜻이고, 합쳐 돌렸다면 그 사실이 감춰졌을 것이다.
            r = results[probes[0]]
            verdict = classify(rcs)
            tag = "" if probes == DEFAULT_PROBES else f" [probe {','.join(probes)}]"
            bad = false_kill_name(verdict, "".join(r.stdout for r in results.values()))
            if bad:
                print(f"  INVALID  {name}  ← 변이가 무수식 {bad} 를 남겼다(변환 누락)")
                invalid += 1
                continue
            if verdict == "KILLED":
                hint = next((ln for ln in r.stdout.splitlines()
                             if ln.startswith("FAILED") or "Error:" in ln), "")
                print(f"  KILLED   {name}{tag}  ← {hint[:66]}")
                killed += 1
            elif verdict == "SURVIVED":
                alive = [pr for pr, rc in rcs.items() if rc == 0]
                print(f"  SURVIVED {name}{tag}  ← probe {alive} 가 이 변이를 못 잡는다(공허)")
                survived += 1
            else:                                   # classify() == "INFRA"
                # ⛔ **verdict 만 본다.** 한때 여기서 첫 probe 의 returncode 를 다시 검사해
                #    {0, 2}(첫 probe 초록 + 다른 probe 인프라 오류)를 SURVIVED 로 접었다
                #    — 판정기를 분리하고도 구 분기가 남아 결론이 갈렸다(codex 재현: survived=1).
                bad = {pr: rc for pr, rc in rcs.items() if rc not in (0, _TEST_FAILED)}
                print(f"  INFRA    {name}{tag}  ← pytest exit {bad} (테스트 실패 아님)")
                infra += 1
    finally:
        SRC.write_text(original)
        assert hashlib.sha256(SRC.read_text().encode()).hexdigest() == digest, "원본 복원 실패"

    print(f"\n  killed={killed} survived={survived} invalid={invalid} infra={infra} / {len(MUTANTS)}")
    return 0 if (survived == 0 and invalid == 0 and infra == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
