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
SNAPSHOT = REPO / "app" / "topic_initial_snapshot.py"
DISPATCHER = REPO / "app" / "topic_dispatcher.py"

# 변이 대상 테스트 — 모듈 계약 + S2/S3 배선 계약 + 기존 인가·스냅샷 계약.
TEST_TARGETS = [
    "tests/test_subscribe_load_metrics.py",
    "tests/test_subscribe_load_wiring.py",
    "tests/test_subscribe_load_snapshot_wiring.py",
    "tests/test_topic_authorization.py",
    "tests/test_topic_initial_snapshot.py",
]

# (라벨, 대상 파일, old, new). old/new 가 tuple 이면 **다중 pair 를 순차 적용**하는 한 변이다
# — "이동"(제거+이식)은 반드시 다중 pair 로 표현한다(단일 pair 로 나누면 각각이 제거/중복
# 변이가 되어 라벨과 불일치 — 실측 2회 반복된 실수). 각 old 는 정확히 1회 등장해야 한다.
MUTANTS: list[tuple] = [  # (label, path, old, new) — old/new 는 str 또는 같은 길이의 tuple
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
    # ── S3: seam ③ + channel (topic_initial_snapshot.py / topic_dispatcher.py) ──
    ("S3-40 channel 기본값을 anonymous 로", SNAPSHOT,
     '    websocket: "WebSocket", topics: List[str], *, channel: str = "unattributed"',
     '    websocket: "WebSocket", topics: List[str], *, channel: str = "anonymous"'),
    ("S3-41 record_snapshot_call 누락", SNAPSHOT,
     "    subscribe_load.record_snapshot_call(channel)\n    sent = 0",
     "    sent = 0"),
    ("S3-42 topics_deduped 를 dedupe 앞으로", SNAPSHOT,
     '''        if topic in seen:
            continue
        seen.add(topic)
        # ⛔ dedupe **통과분만** 센다 — 요청 1건이 몇 배 topic 으로 퍼지는지의 분자.
        #    build 실패 topic 도 수요였으므로 여기(=build 앞)서 센다.
        subscribe_load.record_snapshot_topic()''',
     '''        subscribe_load.record_snapshot_topic()
        if topic in seen:
            continue
        seen.add(topic)'''),
    # ⚠️ "이동" 은 제거+이식 한 span — try **첫 문장**으로 옮기는 변이는 여전히 build 앞이라
    #    등가였다(실측 생존). build 실패 continue 를 **지나서** 세도록 except 뒤로 옮긴다.
    ("S3-43 topics_deduped 를 build 격리 뒤로(실패 topic 미집계)", SNAPSHOT,
     '''        seen.add(topic)
        # ⛔ dedupe **통과분만** 센다 — 요청 1건이 몇 배 topic 으로 퍼지는지의 분자.
        #    build 실패 topic 도 수요였으므로 여기(=build 앞)서 센다.
        subscribe_load.record_snapshot_topic()

        try:
            # ⛔ 관측 CM 은 기존 except **안**이다 — build 예외는 `__aexit__` 를 먼저 통과해
            #    `build_failed` 로 파생 기록된 뒤 기존 격리(continue)로 잡힌다.
            #    `_build_snapshot_sync` 본문은 무계측이다(REST twin 이 공유 — trip-wire 로 잠금).
            async with subscribe_load.observe(subscribe_load.SNAPSHOT_BUILD) as load:
                payload = await asyncio.to_thread(
                    subscribe_load.timed_call, subscribe_load.SNAPSHOT_BUILD, load,
                    _build_snapshot_sync, topic,
                )
                load.finish("none_payload" if payload is None else "built")
        except Exception:
            logger.warning(
                "initial snapshot build 실패 (격리)",
                extra={"topic": topic},
                exc_info=True,
            )
            continue''',
     '''        seen.add(topic)

        try:
            async with subscribe_load.observe(subscribe_load.SNAPSHOT_BUILD) as load:
                payload = await asyncio.to_thread(
                    subscribe_load.timed_call, subscribe_load.SNAPSHOT_BUILD, load,
                    _build_snapshot_sync, topic,
                )
                load.finish("none_payload" if payload is None else "built")
        except Exception:
            logger.warning(
                "initial snapshot build 실패 (격리)",
                extra={"topic": topic},
                exc_info=True,
            )
            continue
        subscribe_load.record_snapshot_topic()'''),
    ("S3-44 build worker 축 우회(timed_call 제거)", SNAPSHOT,
     '''                payload = await asyncio.to_thread(
                    subscribe_load.timed_call, subscribe_load.SNAPSHOT_BUILD, load,
                    _build_snapshot_sync, topic,
                )''',
     '''                payload = await asyncio.to_thread(_build_snapshot_sync, topic)'''),
    ("S3-45 built/none_payload 스왑", SNAPSHOT,
     '                load.finish("none_payload" if payload is None else "built")',
     '                load.finish("built" if payload is None else "none_payload")'),
    ("S3-46 build CM 을 try 밖으로(파생 기록 소실)", SNAPSHOT,
     '''        try:
            # ⛔ 관측 CM 은 기존 except **안**이다 — build 예외는 `__aexit__` 를 먼저 통과해
            #    `build_failed` 로 파생 기록된 뒤 기존 격리(continue)로 잡힌다.
            #    `_build_snapshot_sync` 본문은 무계측이다(REST twin 이 공유 — trip-wire 로 잠금).
            async with subscribe_load.observe(subscribe_load.SNAPSHOT_BUILD) as load:
                payload = await asyncio.to_thread(
                    subscribe_load.timed_call, subscribe_load.SNAPSHOT_BUILD, load,
                    _build_snapshot_sync, topic,
                )
                load.finish("none_payload" if payload is None else "built")
        except Exception:''',
     '''        async with subscribe_load.observe(subscribe_load.SNAPSHOT_BUILD) as load:
          try:
            payload = await asyncio.to_thread(
                subscribe_load.timed_call, subscribe_load.SNAPSHOT_BUILD, load,
                _build_snapshot_sync, topic,
            )
            load.finish("none_payload" if payload is None else "built")
          except Exception:
            load.finish("none_payload")'''),
    ("S3-47 lease_skipped 기록 누락", SNAPSHOT,
     '''            # ⛔ build 를 **다 하고 버린** 낭비 — 폭주가 스스로를 키우는 구간의 신호.
            subscribe_load.record_snapshot_send("lease_skipped")''',
     "            pass"),
    ("S3-48 sent 기록 누락", SNAPSHOT,
     '''            sent += 1
            subscribe_load.record_snapshot_send("sent")''',
     "            sent += 1"),
    ("S3-49 connection_closed 기록 누락", SNAPSHOT,
     '''            subscribe_load.record_snapshot_send("connection_closed")''',
     "            pass"),
    ("S3-50 send raised catch 를 BaseException 으로 확대", SNAPSHOT,
     '''        except Exception:
            # ⛔ **`Exception` 한정** — `BaseException` 으로 넓히면 취소(CancelledError)가
            #    `raised`(실제 전송 예외 신호)를 오염시킨다. 기록만 하고 **그대로 전파** —
            #    위 주석의 규율(프로그래밍 오류를 접지 않는다)은 불변이다.
            subscribe_load.record_snapshot_send("raised")
            raise''',
     '''        except BaseException:
            subscribe_load.record_snapshot_send("raised")
            raise'''),
    ("S3-51 raised 기록 자체 제거", SNAPSHOT,
     '''            subscribe_load.record_snapshot_send("raised")
            raise''',
     "            raise"),
    # ⚠️ 진짜 "이동" — 다중 pair 한 변이 (제거 + return 직전 이식). 조기 이탈 경로에서
    #    분모가 빠지는 회귀를 표현한다(WF-M1 실측 생존 → 잠금).
    ("S3-54 [WF-M1] record_snapshot_call 을 return 직전으로 이동(다중 pair)", SNAPSHOT,
     ('''    subscribe_load.record_snapshot_call(channel)
    sent = 0''',
      '''            subscribe_load.record_snapshot_send("raised")
            raise

    return sent'''),
     ('''    sent = 0''',
      '''            subscribe_load.record_snapshot_send("raised")
            raise

    subscribe_load.record_snapshot_call(channel)
    return sent''')),
    ("S3-54b 분모 중복 계수(요청당 2회 — 이동 아님, 정직 재명명)", SNAPSHOT,
     '''            subscribe_load.record_snapshot_send("raised")
            raise

    return sent''',
     '''            subscribe_load.record_snapshot_send("raised")
            raise

    subscribe_load.record_snapshot_call(channel)
    return sent'''),
    ("S3-55 [WF-M2] record_snapshot_call 을 loop 안으로 이동", SNAPSHOT,
     '''    subscribe_load.record_snapshot_call(channel)
    sent = 0
    seen: set = set()
    for topic in topics:
        if topic in seen:
            continue
        seen.add(topic)''',
     '''    sent = 0
    seen: set = set()
    for topic in topics:
        if topic in seen:
            continue
        seen.add(topic)
        subscribe_load.record_snapshot_call(channel)'''),
    ("S3-56 [WF-M4] 빈 topics 를 record 앞 early-return", SNAPSHOT,
     '''    subscribe_load.record_snapshot_call(channel)
    sent = 0''',
     '''    if not topics:
        return 0
    subscribe_load.record_snapshot_call(channel)
    sent = 0'''),
    ("S3-52 dispatcher anonymous channel 제거", DISPATCHER,
     'await send_initial_snapshots(websocket, free_topics, channel="anonymous")',
     "await send_initial_snapshots(websocket, free_topics)"),
    ("S3-53 dispatcher token_bearing channel 제거", DISPATCHER,
     'await send_initial_snapshots(websocket, accepted_names, channel="token_bearing")',
     "await send_initial_snapshots(websocket, accepted_names)"),
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
        if isinstance(old, tuple) or isinstance(new, tuple):
            # ⛔ shape 검사 — zip 은 길이가 다르면 조용히 잘라 edit 하나가 사라진다.
            if not (isinstance(old, tuple) and isinstance(new, tuple) and len(old) == len(new)):
                print(f"  ⚠️ 다중 pair shape 불일치 — {label}"); problems += 1; continue
            pairs = list(zip(old, new))
        else:
            pairs = [(old, new)]
        bad = [o for o, _ in pairs if text.count(o) != 1]
        if bad:
            print(f"  ⚠️ 앵커 {[text.count(o) for o, _ in pairs]}회 — {label}"); problems += 1; continue
        try:
            mutated = text
            for o, nw in pairs:
                mutated = mutated.replace(o, nw, 1)
            path.write_text(mutated)
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
