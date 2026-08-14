#!/usr/bin/env python3
"""subscribe-load 계측의 **영속 변이 하네스** — 수동 실행 도구 (CI 미연결).

배경: S1~S1d 의 변이 판정은 세션-로컬 ad-hoc 스크립트로 돌아 "독립 재현 가능한 영속
증거가 아니다"(codex). 이 파일이 **현행 계약의 잔존 대표 배터리**를 리포에 고정한다 —
원 S1 39종 전체가 아니라 현행 tree 에 앵커가 남은 것(S1b 잔존 3 + S1c 19 + S1d 1 + S2 계열)
이다. 슬라이스가 늘 때마다 `MUTANTS` 에 추가한다.

사용:
    python3 scripts/mutation_subscribe_load_metrics.py            # 전체
    python3 scripts/mutation_subscribe_load_metrics.py --only 라벨부분문자열

판정 규약 (⛔ 판정기가 아는 형태만 보면 안 된다 — S1c 실측 교훈):
  - killed_tests: pytest 가 빨갛다 — FAILED(단언)·SUBFAIL(subTest)·ERROR(setUp/tearDown)
  - killed_guard: **collection 단계**가 `SubscribeLoadContractError` 로 죽는다 (import-time
    스키마 가드의 의도된 kill — "무효"로 오분류했던 harness 버그를 여기 고정)
  - invalid: 구문 오류·수집 오류 등 판정 불능 (배터리 결함 — 앵커를 고칠 것)
  - survived: 전부 green (테스트 gap — 잠글 것)
종료 코드: survived/invalid/앵커 불일치가 하나라도 있으면 1.

안전: 대상 파일은 byte 백업 후 finally 에서 복원하고, 복원 결과를 sha256 으로 재확인한다
(복원 실패를 조용히 넘기면 미커밋 작업이 소실된다 — 실측 사고 이력).
"""

# 표준 라이브러리
import argparse
import hashlib
import pathlib
import py_compile
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MODULE = REPO / "app" / "subscribe_load_metrics.py"
WIRING = REPO / "app" / "topic_authorization.py"

# 변이 대상 테스트 — 모듈 계약 + S2 배선 계약 + 기존 인가 계약(관측시각 500.0 등).
TEST_TARGETS = [
    "tests/test_subscribe_load_metrics.py",
    "tests/test_subscribe_load_wiring.py",
    "tests/test_topic_authorization.py",
]

# (라벨, 대상 파일, old[유일 앵커], new). old 는 대상 파일에서 정확히 1회 등장해야 한다.
MUTANTS: list[tuple[str, pathlib.Path, str, str]] = [
    # ── S1c: 저장 스키마 ≠ 제출 allowlist ──────────────────────────────
    ("S1c-01 _resolve_outcome 검증 → 저장 스키마 복귀", MODULE,
     "if not isinstance(candidate, str) or candidate not in SUBMITTABLE_OUTCOMES[axis]:",
     "if not isinstance(candidate, str) or candidate not in AXIS_OUTCOMES[axis]:"),
    ("S1c-02 premium 제출 집합에 cancelled 추가(확대)", MODULE,
     '    PREMIUM_RC: frozenset({"granted", "denied", "unavailable_transient", "unavailable_persistent"}),\n    KRX_ENTITLEMENT:',
     '    PREMIUM_RC: frozenset({"granted", "denied", "unavailable_transient", "unavailable_persistent", "cancelled"}),\n    KRX_ENTITLEMENT:'),
    ("S1c-03 snapshot 제출 집합에 build_failed 추가(확대)", MODULE,
     '    SNAPSHOT_BUILD: frozenset({"built", "none_payload"}),',
     '    SNAPSHOT_BUILD: frozenset({"built", "none_payload", "build_failed"}),'),
    ("S1c-04 channel membership → 저장 key 복귀", MODULE,
     "        if key not in CHANNELS:", "        if key not in CHANNEL_KEYS:"),
    ("S1c-05 send membership → 저장 key 복귀", MODULE,
     "        if key not in SEND_OUTCOMES:", "        if key not in SEND_OUTCOME_KEYS:"),
    ("S1c-06 저장에서 unclassified key 제거", MODULE,
     '        "calls_by_channel": {name: 0 for name in CHANNEL_KEYS},',
     '        "calls_by_channel": {name: 0 for name in CHANNELS},'),
    ("S1c-07 CONTRACT_VERSION /2 복귀", MODULE,
     'CONTRACT_VERSION = "subscribe-load/3"', 'CONTRACT_VERSION = "subscribe-load/2"'),
    ("S1c-08 premium 제출 집합 축소(unavailable_persistent)", MODULE,
     '    PREMIUM_RC: frozenset({"granted", "denied", "unavailable_transient", "unavailable_persistent"}),\n    KRX_ENTITLEMENT:',
     '    PREMIUM_RC: frozenset({"granted", "denied", "unavailable_transient"}),\n    KRX_ENTITLEMENT:'),
    ("S1c-09 SEND_OUTCOMES 축소(lease_skipped)", MODULE,
     'SEND_OUTCOMES: tuple[str, ...] = ("sent", "lease_skipped", "connection_closed", "raised")',
     'SEND_OUTCOMES: tuple[str, ...] = ("sent", "connection_closed", "raised")'),
    ("S1c-10 CHANNELS 축소(token_bearing)", MODULE,
     'CHANNELS: tuple[str, ...] = ("anonymous", "token_bearing", "unattributed")',
     'CHANNELS: tuple[str, ...] = ("anonymous", "unattributed")'),
    ("S1c-11 snapshot 제출 집합 축소(built)", MODULE,
     '    SNAPSHOT_BUILD: frozenset({"built", "none_payload"}),',
     '    SNAPSHOT_BUILD: frozenset({"none_payload"}),'),
    ("S1c-12 스키마 가드 top-level 호출 제거", MODULE,
     "\n\n_validate_schema()", "\n"),
    ("S1c-13 가드 disjoint 검사 제거", MODULE,
     '''        if submittable & derived:
            raise SubscribeLoadContractError(f"{axis}: 제출과 파생이 겹친다 — 분리 계약 위반")
''', ""),
    ("S1c-14 가드 partition 검사 제거", MODULE,
     '''        if set(outcomes) != submittable | derived:
            raise SubscribeLoadContractError(f"{axis}: 저장 스키마가 제출 ⊎ 파생과 다르다")
''', ""),
    ("S1c-15 record pairing 임계구역 분리 복귀", MODULE,
     '''            if folded:
                _metrics["metrics_internal_errors_total"] += 1
            _metrics["snapshot_send"]["calls_by_channel"][key] += 1''',
     '''            _metrics["snapshot_send"]["calls_by_channel"][key] += 1
        if folded:
            _note_internal_error()'''),
    ("S1c-16 가드 send 저장 key 검사 제거", MODULE,
     '''    if _UNCLASSIFIED in SEND_OUTCOMES or set(SEND_OUTCOME_KEYS) != set(SEND_OUTCOMES) | {_UNCLASSIFIED}:
        raise SubscribeLoadContractError("snapshot_send: send 저장 key 가 제출 ⊎ unclassified 와 다르다")''',
     "    pass"),
    ("S1c-17 가드 channel 저장 key 검사 제거", MODULE,
     '''    if _UNCLASSIFIED in CHANNELS or set(CHANNEL_KEYS) != set(CHANNELS) | {_UNCLASSIFIED}:
        raise SubscribeLoadContractError("snapshot_send: channel 저장 key 가 제출 ⊎ unclassified 와 다르다")''',
     "    pass"),
    ("S1c-18 channel fold 관측-먼저 복귀", MODULE,
     '''            if folded:
                _metrics["metrics_internal_errors_total"] += 1
            _metrics["snapshot_send"]["calls_by_channel"][key] += 1''',
     '''            _metrics["snapshot_send"]["calls_by_channel"][key] += 1
            if folded:
                _metrics["metrics_internal_errors_total"] += 1'''),
    ("S1c-19 send fold 관측-먼저 복귀", MODULE,
     '''            if folded:
                _metrics["metrics_internal_errors_total"] += 1
            _metrics["snapshot_send"]["sends_by_outcome"][key] += 1''',
     '''            _metrics["snapshot_send"]["sends_by_outcome"][key] += 1
            if folded:
                _metrics["metrics_internal_errors_total"] += 1'''),
    # ── S1d: 축 terminal fold 진단-먼저 ────────────────────────────────
    ("S1d-20 exit fold 관측-먼저 복귀", MODULE,
     '''                  if internal_error:
                      _metrics["metrics_internal_errors_total"] += 1
                  block["by_outcome"][outcome] += 1''',
     '''                  block["by_outcome"][outcome] += 1
                  if internal_error:
                      _metrics["metrics_internal_errors_total"] += 1'''),
    # ── S1b 잔존 앵커 (현행 tree 와 일치하는 것만 — 앵커 불일치는 실행이 잡는다) ──
    ("S1b-21 Event 생성 보호 제거", MODULE,
     '''        self._worker_started_fallback = False
        try:
            self._worker_started = threading.Event()
        except Exception:  # noqa: BLE001''',
     '''        self._worker_started_fallback = False
        self._worker_started = threading.Event()
        if False:  # noqa: BLE001'''),
    ("S1b-22 worker-start 관측 보호 제거", MODULE,
     '''    try:
        handle.mark_worker_started()
    except Exception:  # noqa: BLE001
        _note_internal_error()
        _warn("subscribe-load: worker-start 관측 실패", axis=axis, exc_info=sys.exc_info())''',
     "    handle.mark_worker_started()"),
    ("S1b-23 terminal 전이 실패 counter 미기록", MODULE,
     '''              transition_failure = sys.exc_info()
              # ⚠️ counter 도 **가용하면** 올린다 — 이 경로만 WARNING 만 내고 있었다(실측).
              _note_internal_error()''',
     "              transition_failure = sys.exc_info()"),
    # ── S2: seam ①② 배선 (topic_authorization.py) ─────────────────────
    ("S2-24 premium 관측시각을 RC 호출 뒤로 이동", WIRING,
     '''        premium_observed_at_mono = mono()          # ⛔ RC 호출 **직전**
        result = await fetch_revenuecat_result(user_id, clock=system_clock())''',
     '''        result = await fetch_revenuecat_result(user_id, clock=system_clock())
        premium_observed_at_mono = mono()'''),
    ("S2-25 premium finish 누락(관측 미확정)", WIRING,
     "        load.finish(_axis_outcome(verdict))\n    if isinstance(verdict, Unavailable)",
     "        pass\n    if isinstance(verdict, Unavailable)"),
    ("S2-26 krx CM 을 pre-check 앞으로 확장(계약 오류가 축 오염)", WIRING,
     '''    assert_single_gated_topic()
    if premium.uid != user_id:
        raise ValueError("premium 관측 UID 와 entitlement 조회 UID 가 다르다")

    # ⛔ 위 두 pre-check 는 관측 CM **밖**이다''',
     '''    async with subscribe_load.observe(subscribe_load.KRX_ENTITLEMENT) as _pre:
        assert_single_gated_topic()
        if premium.uid != user_id:
            raise ValueError("premium 관측 UID 와 entitlement 조회 UID 가 다르다")

    # ⛔ 위 두 pre-check 는 관측 CM **밖**이다'''),
    ("S2-27 worker 축 우회(timed_call 제거)", WIRING,
     '''            observation = await asyncio.to_thread(
                subscribe_load.timed_call, subscribe_load.KRX_ENTITLEMENT, load,
                _observe_krx_entitlement_sync, user_id, mono=mono,
            )''',
     '''            observation = await asyncio.to_thread(
                _observe_krx_entitlement_sync, user_id, mono=mono,
            )'''),
    ("S2-28 공통 finish 누락(관측 미확정)", WIRING,
     "        load.finish(_axis_outcome(verdict))\n    # ⛔ 로깅은 관측 CM **밖**",
     "        pass\n    # ⛔ 로깅은 관측 CM **밖**"),
    ("S2-28b TRANSIENT 분기 verdict 를 PERSISTENT 로", WIRING,
     '''            verdict = Unavailable(kind, "entitlement_db_error")
            unavailable_log, caught = "entitlement 조회 DB 오류", exc''',
     '''            verdict = Unavailable(UnavailableKind.PERSISTENT, "entitlement_db_error")
            unavailable_log, caught = "entitlement 조회 DB 오류", exc'''),
    ("S2-29 _axis_outcome transient/persistent 반전", WIRING,
     '''        if verdict.kind is UnavailableKind.TRANSIENT:
            return "unavailable_transient"
        if verdict.kind is UnavailableKind.PERSISTENT:
            return "unavailable_persistent"''',
     '''        if verdict.kind is UnavailableKind.TRANSIENT:
            return "unavailable_persistent"
        if verdict.kind is UnavailableKind.PERSISTENT:
            return "unavailable_transient"'''),
    ("S2-30 denied 를 granted 버킷으로(매핑 우회)", WIRING,
     '''            if not observation.allowed:
                verdict = Denied("krx_entitlement_required")''',
     '''            if not observation.allowed:
                verdict = Denied("krx_entitlement_required")
                load.finish("granted")'''),
    ("S2-31 [WF-G4] TRANSIENT_DB_ERRORS except 협소화", WIRING,
     "        except TRANSIENT_DB_ERRORS as exc:",
     "        except sqlalchemy_exc.OperationalError as exc:"),
    # ⚠️ "이동" 은 제거+이식을 **한 span** 으로 — 둘로 나누면 이식-단독이 등가 변이가 된다
    #    (첫 호출 통과 시 둘째도 통과하는 중복 no-op).
    ("S2-32 [WF-G1] tripwire 를 CM 안으로 이동", WIRING,
     '''    assert_single_gated_topic()
    if premium.uid != user_id:
        raise ValueError("premium 관측 UID 와 entitlement 조회 UID 가 다르다")

    # ⛔ 위 두 pre-check 는 관측 CM **밖**이다 — 배선·계약 오류(트립와이어, UID 불일치)를
    #    krx 축의 `raised` 로 기록하면 외부축(DB) 결함 신호가 오염된다. 축 관측은 여기부터다.
    unavailable_log: Optional[str] = None
    caught: Optional[BaseException] = None
    async with subscribe_load.observe(subscribe_load.KRX_ENTITLEMENT) as load:
        try:''',
     '''    if premium.uid != user_id:
        raise ValueError("premium 관측 UID 와 entitlement 조회 UID 가 다르다")

    unavailable_log: Optional[str] = None
    caught: Optional[BaseException] = None
    async with subscribe_load.observe(subscribe_load.KRX_ENTITLEMENT) as load:
        assert_single_gated_topic()
        try:'''),
    ("S2-33 [WF-G2] premium mono 를 CM 진입 앞으로 hoist", WIRING,
     '''    async with subscribe_load.observe(subscribe_load.PREMIUM_RC) as load:
        premium_observed_at_mono = mono()          # ⛔ RC 호출 **직전**''',
     '''    premium_observed_at_mono = mono()
    async with subscribe_load.observe(subscribe_load.PREMIUM_RC) as load:'''),
    ("S2-34 [WF-G3] _axis_outcome fail-fast 격하", WIRING,
     '''    raise TypeError(f"관측 outcome 으로 매핑할 수 없는 verdict: {type(verdict).__name__}")''',
     '''    return "granted"'''),
    ("S2-35 로그 traceback 전달 누락", WIRING,
     "        _log_unavailable(verdict.kind, unavailable_log, exc=caught)",
     "        _log_unavailable(verdict.kind, unavailable_log)"),
    # ⚠️ 이 슬라이스의 핵심 설계 이탈(로깅 CM-밖)을 되돌리는 변이 — "이동" 이라 단일 span.
    ("S2-36 로깅을 CM 안으로 복귀", WIRING,
     '''        load.finish(_axis_outcome(verdict))
    # ⛔ 로깅은 관측 CM **밖** — 로깅 인프라 결함(Filter/Logger 오류)이 실제 DB transient
    #    사건을 krx `raised` 로 둔갑시켜 `unavailable_*` 신호를 지우면 안 된다(pre-check 와
    #    같은 원칙, Workflow attribution probe 실측). duration 에도 로그 시간이 안 섞인다.
    #    traceback 은 `caught` 를 명시 전달해 보존한다(밖에서는 sys.exc_info() 가 빈다).
    if unavailable_log is not None:
        _log_unavailable(verdict.kind, unavailable_log, exc=caught)
    return verdict''',
     '''        load.finish(_axis_outcome(verdict))
        if unavailable_log is not None:
            _log_unavailable(verdict.kind, unavailable_log, exc=caught)
    return verdict'''),
    ("S2-37 Unavailable.kind runtime guard 제거", WIRING,
     '''    def __post_init__(self) -> None:
        if not isinstance(self.kind, UnavailableKind):
            raise TypeError(
                "Unavailable.kind 는 UnavailableKind 여야 한다: "
                f"{type(self.kind).__name__}"
            )''',
     '''    def __post_init__(self) -> None:
        pass'''),
    ("S2-38 premium 로깅을 finish 앞 CM 안에 주입", WIRING,
     '''        load.finish(_axis_outcome(verdict))
    if isinstance(verdict, Unavailable) and verdict.kind is UnavailableKind.PERSISTENT:''',
     '''        if isinstance(verdict, Unavailable) and verdict.kind is UnavailableKind.PERSISTENT:
            logger.error("injected logging inside observation")
        load.finish(_axis_outcome(verdict))
    if isinstance(verdict, Unavailable) and verdict.kind is UnavailableKind.PERSISTENT:'''),
]


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tests() -> str:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *TEST_TARGETS, "-q", "-p", "no:asyncio",
         "--no-header", "--tb=short"],
        capture_output=True, text=True, timeout=300, cwd=REPO)
    out = r.stdout + r.stderr
    # ⛔ "ERROR" in out 같은 느슨한 조건은 tearDown ERROR kill 을 가드-kill 로 오분류했다
    #    (실측: S1b-23). 가드-kill 은 **collection 단계** 실패 + 가드 예외명일 때만이다.
    if "error during collection" in out or "errors during collection" in out:
        return "killed_guard" if "SubscribeLoadContractError" in out else "invalid"
    if "INTERNALERROR" in out:
        return "invalid"
    # FAILED(단언 실패)·SUBFAIL(subTest)·"ERROR tests/"(setUp/tearDown 오류) 전부 kill 이다.
    red = any(line.startswith(("FAILED", "SUBFAIL", "ERROR ")) for line in out.splitlines())
    if (r.returncode != 0) != red:
        return "invalid"
    return "killed_tests" if red else "survived"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="라벨 부분 문자열 필터")
    args = parser.parse_args()

    selected = [m for m in MUTANTS if args.only in m[0]]
    if not selected:
        print(f"⛔ '--only {args.only}' 에 맞는 변이가 없다"); return 1

    if run_tests() != "survived":  # 변이 0 상태 = 전부 green 이어야 baseline
        print("⛔ baseline 이 green 이 아니다 — 변이 판정 불가"); return 1

    problems = 0
    tally = {"killed_tests": 0, "killed_guard": 0}
    for label, path, old, new in selected:
        original = path.read_bytes()
        original_sha = hashlib.sha256(original).hexdigest()
        text = original.decode()
        if text.count(old) != 1:
            print(f"  ⚠️ 앵커 {text.count(old)}회 — {label}"); problems += 1; continue
        try:
            path.write_text(text.replace(old, new, 1))
            try:
                py_compile.compile(str(path), doraise=True)
            except Exception:
                print(f"  ⚠️ 구문 오류(무효 변이) — {label}"); problems += 1; continue
            state = run_tests()
            if state in tally:
                tally[state] += 1
                print(f"  ✅ {state} — {label}")
            else:
                print(f"  ❌ {state} — {label}"); problems += 1
        finally:
            path.write_bytes(original)
            if sha(path) != original_sha:  # ⛔ 복원 실패는 조용히 넘기지 않는다
                print(f"⛔ 복원 실패: {path}"); return 2
    print(f"합계: 테스트-kill {tally['killed_tests']} + 가드-kill {tally['killed_guard']}"
          f" / {len(selected)} (문제 {problems})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
