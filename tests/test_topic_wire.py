"""WS 오류 프레임 **단일 생성 지점**의 계약 (`app/topic_wire.py`).

## 왜 별 파일인가

`build_subscription_error` 는 firebase-free 순수 함수이고, 그 존재 이유가 **§8-C 의 결합 규칙을
생성 시점에 강제**하는 것이다. 그 강제는 `/ws` 경계에서 관측되지 않는다 — 호출부가 전부 올바른
조합만 넘기면 강제를 통째로 지워도 스위트가 green 이기 때문이다(변이 실측: `emitter: 필수 조합
검사 제거` / `금지 조합 검사 제거` 둘 다 **생존**했다). 그래서 여기서 직접 잠근다.
"""
import pathlib
import unittest

from app.topic_wire import (
    PER_TOPIC_ERRORS,
    WHOLE_REQUEST_ERRORS,
    FirebaseNotInitialized,
    SubscribeAuthFailed,
    build_subscription_ack,
    build_subscription_error,
    ConnectionIdentity,
    SubscribeIdentityConflict,
)


class TestRetryAfterCoupling(unittest.TestCase):
    """§8-C: `temporarily_unavailable` 은 `retry_after_seconds` 를 **동반**한다."""

    def test_temporarily_unavailable_requires_retry_after(self):
        """⛔ 없으면 클라가 재시도 시점을 유도할 수 없다 — 조용한 실패가 된다."""
        with self.assertRaises(ValueError):
            build_subscription_error(request_id="r", error="temporarily_unavailable")

    def test_retry_after_must_be_a_positive_int(self):
        """⚠️ `bool` 도 거부한다 — `isinstance(True, int)` 가 참이라 흘러들 수 있다."""
        for bad in (0, -1, 1.5, "5", True, None):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                build_subscription_error(
                    request_id="r",
                    error="temporarily_unavailable",
                    retry_after_seconds=bad,
                )

    def test_other_errors_must_not_carry_retry_after(self):
        """⛔ `invalid_token` 에 붙으면 **자격이 죽은 채 영구 재시도 루프**가 된다."""
        for code in ("invalid_token", "invalid_request"):
            with self.subTest(error=code), self.assertRaises(ValueError):
                build_subscription_error(
                    request_id="r", error=code, retry_after_seconds=5
                )

    def test_valid_frames_have_exactly_the_expected_keys(self):
        unavailable = build_subscription_error(
            request_id="r1", error="temporarily_unavailable", retry_after_seconds=5
        )
        self.assertEqual(
            unavailable,
            {
                "type": "subscription_error",
                "request_id": "r1",
                "error": "temporarily_unavailable",
                "retry_after_seconds": 5,
            },
        )
        terminal = build_subscription_error(request_id="r2", error="invalid_token")
        self.assertEqual(
            terminal,
            {"type": "subscription_error", "request_id": "r2", "error": "invalid_token"},
        )
        self.assertNotIn("retry_after_seconds", terminal)

    def test_no_upper_bound_is_enforced(self):
        """⚠️ 상한을 **의도적으로** 강제하지 않는다 — 정본 §8-C 는 "동반"만 요구한다.

        여기서 임의 상한을 박으면 값·jitter 정책을 **코드가 먼저 결정**해 버린다. 그 정책이
        정해지면 이 테스트를 그 결정으로 교체할 것(지금 지우면 근거 없이 되돌린 것이 된다).
        """
        frame = build_subscription_error(
            request_id="r", error="temporarily_unavailable", retry_after_seconds=3600
        )
        self.assertEqual(frame["retry_after_seconds"], 3600)


class TestErrorVocabulary(unittest.TestCase):
    """§8-C 의 **전체-요청 코드**만 프레임이 된다.

    ⛔ 어휘를 강제하지 않으면 오타나 즉흥 코드가 정상 프레임으로 나가 코드가 **제3의 계약**을
    만든다(실측: `error="typo_not_in_section_8"` 이 그대로 통과했다 — codex Medium).
    """

    def test_unknown_codes_are_rejected(self):
        for bad in ("typo_not_in_section_8", "", "INVALID_TOKEN", None):
            with self.subTest(error=bad), self.assertRaises(ValueError):
                build_subscription_error(request_id="r", error=bad)

    def test_per_topic_codes_are_rejected(self):
        """per-topic 코드는 ack 의 `rejected_topics` 에 실린다 — 여기 오면 계약 혼선이다."""
        for per_topic in (
            "unknown_topic", "topic_unavailable", "premium_required",
            "krx_entitlement_required", "topics_disabled",
        ):
            with self.subTest(error=per_topic), self.assertRaises(ValueError):
                build_subscription_error(request_id="r", error=per_topic)

    def test_every_allowed_code_can_actually_build_a_frame(self):
        """⛔ 자기검사 — 허용 목록이 비거나 좁아지면 이 테스트가 먼저 깨진다."""
        self.assertEqual(
            WHOLE_REQUEST_ERRORS,
            {"invalid_token", "temporarily_unavailable", "invalid_request", "request_too_large"},
        )
        for code in sorted(WHOLE_REQUEST_ERRORS):
            with self.subTest(error=code):
                retry = 5 if code == "temporarily_unavailable" else None
                frame = build_subscription_error(
                    request_id="r", error=code, retry_after_seconds=retry
                )
                self.assertEqual(frame["error"], code)


class TestSubscribeAuthFailed(unittest.TestCase):
    def test_carries_the_wire_verdict(self):
        failure = SubscribeAuthFailed("temporarily_unavailable", 5)
        self.assertEqual(failure.error, "temporarily_unavailable")
        self.assertEqual(failure.retry_after_seconds, 5)

    def test_retry_after_defaults_to_absent(self):
        self.assertIsNone(SubscribeAuthFailed("invalid_token").retry_after_seconds)

    def test_firebase_not_initialized_is_a_dedicated_type(self):
        """⛔ 전용 타입이라야 `RuntimeError` 를 통째로 잡는 실수를 피할 수 있다."""
        self.assertTrue(issubclass(FirebaseNotInitialized, RuntimeError))
        self.assertIsNot(FirebaseNotInitialized, RuntimeError)


class TestAuthTimeConstants(unittest.TestCase):
    """두 시간 축은 **독립**이다 — 관계를 불변식으로 강제하지 않는다.

    ⛔ 한때 `D > T` 를 import 시점에 강제했고 근거를 "D ≤ T 면 transport 상한이 죽은 코드"라고
    적었는데 **그 근거가 거짓이었다**(codex High, 재현 확인): 호출자가 포기해도 `to_thread`
    worker 는 취소되지 않고 **SDK 자체 상한 T 에 종료된다**(D=0.05/T=0.20 → worker 0.205s).
    즉 유효한 설정을 막는 게이트였다. 그래서 이 클래스는 **관계가 아니라 현재 선택**을 기록한다.
    """

    def test_records_the_current_tuning_choice(self):
        """현재 D > T 는 **튜닝 선택**을 기록한 것이다 — 불변식도, 보장도 아니다.

        ⛔ 한때 이 테스트가 "분류 보존"을 주장했는데 **과장이었다**(codex Medium): T 는
        per-attempt 상한이고 한 번의 검증은 인증서 조회 + 계정 조회 × (1 + 재시도) + backoff 를
        합치므로, 그 총합은 D 를 넘을 수 있다. 따라서 D > T 여도 느린 실패는 deadline 으로 뭉쳐진다.

        ⚠️ 이 단언이 red 가 되면 **버그가 아니라 튜닝 변경**이다 — 값만 맞추고 지나가지 말고
        위 사실을 근거로 D·T 를 함께 다시 정할 것.
        """
        from app import config

        self.assertGreater(
            config.WS_AUTH_WIRE_DEADLINE_SECONDS,
            config.WS_AUTH_HTTP_TIMEOUT_SECONDS,
        )

    def test_persistent_faults_wait_longer_than_transient_ones(self):
        from app import config

        self.assertGreater(
            config.WS_AUTH_PERSISTENT_FAULT_RETRY_AFTER_SECONDS,
            config.WS_AUTH_RETRY_AFTER_SECONDS,
            "재시도로 낫지 않는 결함에 같은 간격을 주면 retry storm 이 된다",
        )


class TestSubscriptionAck(unittest.TestCase):
    """§8-B ack 의 **형태**를 호출자 규율이 아니라 타입으로 강제한다.

    ⛔ 형태가 어긋난 ack 은 **조용한 사고**다: 클라(iOS)의 `activeSubscriptions` 는
    non-optional 객체 배열이라 디코드가 통째로 실패하고, 그러면 "프레임 0개"와 구분되지 않아
    in-flight 슬롯이 영영 안 풀린다. 그래서 wrap 을 builder 안에서 한다.
    """

    def _ack(self, **kw):
        base = dict(request_id="r", operation="subscribe",
                    accepted=[], rejected=[], active=[])
        base.update(kw)
        return build_subscription_ack(**base)

    def test_containers_are_object_arrays(self):
        """⛔ 정본 §8-B-stage: 컨테이너는 Stage 1 부터 최종형(객체 배열)이다."""
        frame = self._ack(accepted=["fx:usd-krw"],
                          rejected=[("krx:usd-krw-futures", "topic_unavailable")],
                          active=["fx:usd-krw"])
        self.assertEqual(
            frame,
            {
                "type": "subscription_ack",
                "request_id": "r",
                "operation": "subscribe",
                "accepted_topics": [{"topic": "fx:usd-krw"}],
                "rejected_topics": [
                    {"topic": "krx:usd-krw-futures", "error": "topic_unavailable"}
                ],
                "removed_topics": [],
                "active_subscriptions": [{"topic": "fx:usd-krw"}],
            },
        )

    def test_active_is_sorted_but_accepted_keeps_request_order(self):
        """⚠️ 두 축의 정렬 정책이 **다르다**. 하나로 통일하면 둘 중 하나가 깨진다.

        `active` 의 입력은 `set` 이라 정렬하지 않으면 `PYTHONHASHSEED` 에 따라 프로세스마다
        wire 순서가 달라진다. `accepted` 는 클라가 보낸 순서를 되비춘다.
        """
        frame = self._ack(accepted=["usdt:krw", "fx:eur-krw"],
                          active={"usdt:krw", "fx:eur-krw", "fx:usd-krw"})
        self.assertEqual([x["topic"] for x in frame["active_subscriptions"]],
                         ["fx:eur-krw", "fx:usd-krw", "usdt:krw"])
        self.assertEqual([x["topic"] for x in frame["accepted_topics"]],
                         ["usdt:krw", "fx:eur-krw"])

    def test_removed_topics_is_not_an_argument(self):
        """⛔ 인자가 아니라 **항상 `[]`**. 인자로 두면 unsubscribe 결과를 담고 싶어지는데,
        `removed_topics` 는 §C2 eviction 축이라 다른 것이다."""
        import inspect
        self.assertNotIn("removed_topics",
                         inspect.signature(build_subscription_ack).parameters)
        self.assertEqual(self._ack(accepted=["fx:usd-krw"])["removed_topics"], [])

    def test_request_id_must_be_a_non_empty_string(self):
        """⛔ **ack 은 nullable 이 아니다**(§8-B-stage) — 클라가 필수 필드로 모델링한다."""
        for bad in (None, "", 123, True):
            with self.subTest(request_id=bad), self.assertRaises(ValueError):
                self._ack(request_id=bad)

    def test_operation_vocabulary_is_enforced(self):
        for bad in ("resubscribe", "", None, "SUBSCRIBE"):
            with self.subTest(operation=bad), self.assertRaises(ValueError):
                self._ack(operation=bad)
        for good in ("subscribe", "unsubscribe"):
            with self.subTest(operation=good):
                self.assertEqual(self._ack(operation=good)["operation"], good)

    def test_rejection_reasons_must_be_per_topic_codes(self):
        """⛔ per-topic 어휘 밖(오타·전체-요청 코드)이면 프레임을 만들 수 없다."""
        for bad in ("typo_not_in_section_8", "invalid_token", "temporarily_unavailable"):
            with self.subTest(error=bad), self.assertRaises(ValueError):
                self._ack(rejected=[("fx:usd-krw", bad)])

    def test_every_per_topic_code_can_build_a_rejection(self):
        """⛔ 자기검사 — 허용 목록이 비거나 좁아지면 이 테스트가 먼저 깨진다."""
        self.assertEqual(
            PER_TOPIC_ERRORS,
            {"topics_disabled", "unknown_topic", "premium_required",
             "krx_entitlement_required", "topic_unavailable"},
        )
        self.assertFalse(PER_TOPIC_ERRORS & WHOLE_REQUEST_ERRORS,
                         "per-topic 과 전체-요청 어휘가 겹치면 범위 구분이 무너진다")
        for code in sorted(PER_TOPIC_ERRORS):
            with self.subTest(error=code):
                frame = self._ack(rejected=[("fx:usd-krw", code)])
                self.assertEqual(frame["rejected_topics"][0]["error"], code)


class TestGuideExamplesAreConstructible(unittest.TestCase):
    """⛔ **핸드오프 가이드에 적힌 프레임은 서버가 실제로 만들 수 있어야 한다.**

    `REALTIME_V2_CLIENT_GUIDE.md` 는 CLAUDE.md 가 "신규 앱 핸드오프 **단일 계약**"으로 지정한
    문서다 — Android 는 그걸 보고 decoder 를 쓴다. 그런데 실측으로 두 번,
    **서버가 생성할 수 없는 프레임**이 예시로 실려 있었다:
      - `error="invalid_request"` + `retry_after_seconds=5` (builder 가 `ValueError` 로 거부하는 조합)
      - 문자열 배열 컨테이너(구 초안)

    사람이 대조하는 것으로는 반복해서 놓친다. 그래서 **예시를 실제 builder 에 통과시킨다.**
    """

    GUIDE = pathlib.Path(__file__).resolve().parent.parent / "REALTIME_V2_CLIENT_GUIDE.md"
    START_MARKER = "<!-- topic-wire-examples:start -->"
    END_MARKER = "<!-- topic-wire-examples:end -->"

    def _documented_frames(self):
        """전용 wire 블록의 모든 최상위 JSON 객체를 파싱한다.

        파싱 실패를 건너뛰면 예시가 늘어난 뒤 깨진 하나가 개수 하한 뒤로 숨을 수 있다. 따라서
        전용 마커 안에서는 객체 하나라도 파싱할 수 없거나 중괄호가 닫히지 않으면 즉시 실패한다.
        """
        import json
        import re

        text = self.GUIDE.read_text(encoding="utf-8")
        self.assertEqual(text.count(self.START_MARKER), 1, "wire 예시 시작 마커는 정확히 하나여야 한다")
        self.assertEqual(text.count(self.END_MARKER), 1, "wire 예시 종료 마커는 정확히 하나여야 한다")
        section = text.split(self.START_MARKER, 1)[1].split(self.END_MARKER, 1)[0]
        blocks = re.findall(r"```jsonc\n(.*?)```", section, re.S)
        self.assertEqual(len(blocks), 1, "wire 예시는 전용 jsonc 블록 하나에 있어야 한다")

        # JSON 문자열 안의 `//`는 보존하고 문자열 밖 line comment만 제거한다.
        uncommented_lines = []
        for line in blocks[0].splitlines():
            kept = []
            in_string = False
            escaped = False
            i = 0
            while i < len(line):
                ch = line[i]
                if in_string:
                    kept.append(ch)
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                elif ch == '"':
                    in_string = True
                    kept.append(ch)
                elif ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                    break
                else:
                    kept.append(ch)
                i += 1
            uncommented_lines.append("".join(kept))
        stripped = "\n".join(uncommented_lines)

        frames = []
        depth, start = 0, None
        in_string = False
        escaped = False
        for i, ch in enumerate(stripped):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                self.assertGreater(depth, 0, "wire 예시에 여는 중괄호 없는 닫는 중괄호가 있다")
                depth -= 1
                if depth == 0 and start is not None:
                    raw = stripped[start:i + 1]
                    try:
                        obj = json.loads(raw)
                    except ValueError as exc:
                        self.fail(f"wire 예시 JSON을 파싱할 수 없다: {exc}: {raw}")
                    self.assertIsInstance(obj, dict, "wire 예시 최상위 값은 객체여야 한다")
                    frames.append(obj)
                    start = None
        self.assertFalse(in_string, "wire 예시에 닫히지 않은 문자열이 있다")
        self.assertEqual(depth, 0, "wire 예시에 닫히지 않은 중괄호가 있다")
        return frames

    def test_every_documented_frame_is_what_the_builder_emits(self):
        frames = self._documented_frames()
        identities = [
            (frame.get("type"), frame.get("operation") or frame.get("error"))
            for frame in frames
        ]
        self.assertEqual(
            identities,
            [
                ("subscription_ack", "subscribe"),
                ("subscription_error", "invalid_request"),
                ("subscription_error", "temporarily_unavailable"),
            ],
            "wire 예시의 추가·제거·종류 변경은 검사 기대값도 명시적으로 갱신해야 한다",
        )

        for frame in frames:
            with self.subTest(frame=frame.get("error") or frame.get("operation")):
                if frame["type"] == "subscription_error":
                    rebuilt = build_subscription_error(
                        request_id=frame["request_id"],
                        error=frame["error"],
                        retry_after_seconds=frame.get("retry_after_seconds"),
                    )
                else:
                    documented_leases = {
                        x["topic"]: (x["lease_id"], x["lease_duration_seconds"])
                        for x in frame["accepted_topics"] + frame["active_subscriptions"]
                        if "lease_id" in x
                    }
                    rebuilt = build_subscription_ack(
                        request_id=frame["request_id"],
                        operation=frame["operation"],
                        accepted=[x["topic"] for x in frame["accepted_topics"]],
                        rejected=[(x["topic"], x["error"]) for x in frame["rejected_topics"]],
                        active=[x["topic"] for x in frame["active_subscriptions"]],
                        leases=documented_leases or None,
                    )
                self.assertEqual(
                    rebuilt, frame,
                    "가이드의 예시가 서버가 만드는 프레임과 다르다 — 신규 소비자가 "
                    "**존재하지 않는 shape** 로 decoder 를 쓰게 된다",
                )

    def test_malformed_example_is_not_hidden_by_the_other_examples(self):
        """파싱 불가 예시는 다른 정상 예시가 충분히 많아도 건너뛰지 않는다."""
        import tempfile

        text = self.GUIDE.read_text(encoding="utf-8")
        malformed = text.replace(
            '"error": "invalid_request"',
            '"error": invalid_request',
            1,
        )
        self.assertNotEqual(malformed, text, "mutation anchor가 사라졌다")
        with tempfile.TemporaryDirectory() as tmp:
            original = self.GUIDE
            try:
                self.GUIDE = pathlib.Path(tmp) / "guide.md"
                self.GUIDE.write_text(malformed, encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "파싱할 수 없다"):
                    self._documented_frames()
            finally:
                self.GUIDE = original


class TestEveryPublisherGoesThroughTheLeaseGate(unittest.TestCase):
    """⛔ **발행 경로는 하나도 빠짐없이 lease 게이트를 지나야 한다.**

    실측: `publish_topic` 에만 게이트를 넣었더니 sibling 인 `publish_topic_detailed` 가
    `registry.get_subscribers()` 를 직접 불러 **만료된 lease 로 전송**했다 — 그리고 그 함수는
    `atomic_fx_live` 라는 **live caller** 를 가진다.

    ⚠️ 구조 검사인 이유: 새 발행 함수는 **아직 없으므로** 행동 테스트를 쓸 대상이 없다.
    이 검사는 "다음 사람이 같은 우회를 만들지 못하게" 하는 것이 목적이다.
    """

    def test_no_publisher_reads_the_raw_subscriber_set(self):
        import ast
        import pathlib as _pathlib

        source = (_pathlib.Path(__file__).resolve().parent.parent
                  / "app" / "topic_dispatcher.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        allowed = {"leased_subscribers"}          # 게이트 자신
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in allowed or not node.name.startswith(("publish", "_publish")):
                continue
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "get_subscribers"):
                    offenders.append(node.name)
        self.assertEqual(
            offenders, [],
            "발행 함수가 raw 구독자 집합을 직접 읽는다 — 그 경로만 lease 게이트를 우회한다: "
            f"{offenders}",
        )

    def test_the_detector_would_catch_a_bypass(self):
        """⛔ 자기검사 — 검출기가 아무것도 못 찾는 형태면 위 테스트는 공허하다."""
        import ast

        tree = ast.parse(
            "async def publish_bad(topic):\n"
            "    return registry.get_subscribers(topic)\n"
        )
        found = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "get_subscribers" for c in ast.walk(n))
        ]
        self.assertEqual(found, ["publish_bad"])


class TestPolicyCloseCodeIsSentFromOneIdentityConflictSite(unittest.TestCase):
    """⛔ **문법적 직접 호출 지점 검사**다 — "정책 사유가 하나" 를 보장하지 **않는다**.

    `1008` 은 표준의 **일반 정책 위반 코드**이고 서버는 `reason` 도 싣지 않는다. 그래서
    "이 close 가 무슨 사유인지" 는 **호출 지점이 어디냐로만** 구분된다 — 지금은 cross-UID
    (`except SubscribeIdentityConflict`) 한 곳뿐이라 `REALTIME_V2_CLIENT_GUIDE §6` 이
    *"cross-UID 면 재연결이 정상 복구"* 라고 적을 수 있다(그 절은 **1008 일반**에 대한 규칙이
    아니다 — 좁혀 두었다).

    ## 무엇을 덮는가 (자기검사가 증명한다)

    `app/**/*.py` 안의 **직접 attribute 호출** `<expr>.close(...)`. code 가 리터럴이 아니어도
    (`code=CONST` / `close(f())` / `close(**opts)`) **fail-closed 로 기록**해 단언을 실패시킨다.
    곁들여 `close` 를 이름에 담는 것과 `getattr(ws, "close")`, `WebSocketException` 도 **금지**한다
    — 추적할 수 없으니 쓰지 못하게 막는 쪽을 택했다(현재 셋 다 0건이라 비용이 없다).

    ## ⛔ 무엇을 덮지 못하는가 (적대적 검증으로 실측한 경계 — 조용한 상한이 아니다)

      - **helper 다중화**: 한 호출 지점이 여러 사유를 인자로 받아 같은 close 를 실행. AST 로는
        원리적으로 구분 불가다.
      - **프레임워크·ASGI 레벨**: raw ASGI `{"type": "websocket.close", "code": ...}` 를 send 로
        직접 보내기 / send 채널을 감싸는 미들웨어 / 서버 설정(`--ws-max-size` 의 1009 등).
      - **트리 밖**: `app/` 바깥 모듈, `app/` 안의 **비-.py** 소스를 동적 로드, `app/` 안의
        **심볼릭 링크 디렉터리**(`rglob` 가 따라가지 않음 — 실측).
      - ⚠️ **FastAPI 자체가 1008 을 보낼 수 있다**: WebSocket 엔드포인트에 검증 대상 파라미터를
        추가하면 검증 실패 시 프레임워크가 1008 로 닫는다. 현재 `/ws` 는 `websocket` 하나만
        받아 해당 없음 — **파라미터를 추가하는 순간** 이 절과 가이드 §6 을 함께 봐야 한다.

    이 경계 밖은 검사로 막을 수 없다. 유일한 방어는 **사유를 늘릴 때 가이드부터 갱신하는 규율**이다.
    """

    @staticmethod
    def _close_sites(tree):
        """`<expr>.close(...)` 중 **close code 를 지정한** 호출의 `(lineno, code)`.

        ⛔ **fail-closed 가 핵심이다**: `code=` 가 있거나 positional 인자가 있는데 값이 정수
        **리터럴이 아니면 `None` 으로 기록**한다 — 그래야 최종 단언이 실패한다. 리터럴만 세면
        `close(code=POLICY_VIOLATION)` / `close(get_code())` 같은 두 번째 발신자가 **조용히
        통과**한다(codex Blocker — 이 리포에서 "판정기가 아는 형태만 본다"로 반복된 실패다).
        `**kwargs` 언패킹도 같은 이유로 `None` 이다.

        ⚠️ 인자 없는 `db.close()` / `ws.close()` 는 대상이 아니다 — 전자는 WebSocket 이 아니고
        후자는 code 를 지정하지 않는 **정상 종료(1000)** 라 정책 신호가 아니다.
        """
        import ast
        sites = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "close"):
                continue
            code_kw = [kw for kw in node.keywords if kw.arg == "code"]
            if code_kw:
                value = code_kw[0].value
                sites.append((node.lineno,
                              value.value if isinstance(value, ast.Constant) else None))
            elif node.args:
                first = node.args[0]
                literal = isinstance(first, ast.Constant) and isinstance(first.value, int)
                sites.append((node.lineno, first.value if literal else None))
            elif any(kw.arg is None for kw in node.keywords):     # close(**opts)
                sites.append((node.lineno, None))
        return sites

    @staticmethod
    def _close_aliases(tree):
        """`x = <expr>.close` — **close 를 이름에 담는** 지점.

        ⛔ 검출기는 데이터 흐름을 추적하지 않는다. 한때 `Name(id="close")` 도 매칭해 alias 를
        지원하는 척했는데, 그건 `close(...)` 라는 **아무 함수**나 오인하면서
        `finish = ws.close` 는 놓치는 **오탐·누락 동시 발생**이었다(codex Medium).
        그래서 추적하는 대신 **금지**한다 — 현재 `app/` 에 이 패턴은 0건이라 비용이 없다.
        """
        import ast
        out = []
        for node in ast.walk(tree):
            value = getattr(node, "value", None)
            if (isinstance(node, (ast.Assign, ast.AnnAssign))
                    and isinstance(value, ast.Attribute) and value.attr == "close"):
                out.append(node.lineno)
            # `getattr(ws, "close")(...)` — 이름을 문자열로 감춰 attribute 호출을 피하는 형태.
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr" and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "close"):
                out.append(node.lineno)
        return out

    @staticmethod
    def _policy_raise_sites(tree):
        """`WebSocketException(...)` 생성 지점 — **close 를 부르지 않고** 정책 종료를 만드는 경로.

        Starlette 의 정식 관용구라 누군가 자연스럽게 쓸 수 있는데, `close()` 호출이 없어 위
        검출기가 통째로 놓친다(적대적 검증에서 실제로 나온 형태). 현재 `app/` 사용 0건이라
        **금지가 무료**다 — 쓰려면 가이드 §6 부터 갱신하라는 뜻이다.
        """
        import ast
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "WebSocketException":
                    out.append(node.lineno)
        return out

    @staticmethod
    def _identity_conflict_handler_lines(tree):
        """`except SubscribeIdentityConflict:` 블록이 덮는 줄 번호 집합.

        ⚠️ 이것이 **사유를 코드에 묶는 유일한 장치**다 — 1008 자체는 사유를 말하지 않으므로
        "그 핸들러 안에 있다" 가 지금 아는 전부다.
        """
        import ast
        covered = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            name = getattr(node.type, "id", None) or getattr(node.type, "attr", None)
            if name != "SubscribeIdentityConflict":
                continue
            for inner in ast.walk(node):
                if hasattr(inner, "lineno"):
                    covered.add(inner.lineno)
        return covered

    def test_the_only_policy_close_is_the_identity_conflict_one(self):
        import ast
        import pathlib as _pathlib

        app_dir = _pathlib.Path(__file__).resolve().parent.parent / "app"
        found = []
        conflict_lines = {}
        raises = []
        # ⚠️ **상대 경로**로 키를 잡는다 — basename 으로 잡으면 `app/sources/topic_dispatcher.py`
        #    같은 동명 파일이 handler-scope 검사를 덮어써 통과시킨다(적대적 검증에서 나왔다).
        for path in sorted(app_dir.rglob("*.py")):
            rel = str(path.relative_to(app_dir))
            tree = ast.parse(path.read_text(encoding="utf-8"))
            conflict_lines[rel] = self._identity_conflict_handler_lines(tree)
            for lineno in self._policy_raise_sites(tree):
                raises.append((rel, lineno))
            for lineno, code in self._close_sites(tree):
                found.append((rel, lineno, code))

        self.assertEqual(
            raises, [],
            "`WebSocketException` 으로 정책 종료를 만들면 close 검출기가 통째로 놓친다 — "
            f"쓰려면 REALTIME_V2_CLIENT_GUIDE §6 부터 갱신할 것: {raises}",
        )

        self.assertEqual(
            [(rel, code) for rel, _, code in found],
            [("topic_dispatcher.py", 1008)],
            "WebSocket close code 발신 지점이 바뀌었다 — 1008 이 더 이상 cross-UID 와 1:1 이\n"
            "아니게 되면 REALTIME_V2_CLIENT_GUIDE §6 의 복구 규칙이 틀린 지시가 된다.\n"
            f"먼저 구분 가능한 신호(private 4xxx / stable reason)를 정하고 §6 을 갱신할 것: {found}",
        )
        aliases = []
        for path in sorted(app_dir.rglob("*.py")):
            for lineno in self._close_aliases(ast.parse(path.read_text(encoding="utf-8"))):
                aliases.append((str(path.relative_to(app_dir)), lineno))
        self.assertEqual(
            aliases, [],
            "`close` 를 이름에 담았다 — 검출기가 추적할 수 없어 정책 close 가 숨는다. "
            f"직접 호출(`ws.close(code=...)`)로 둘 것: {aliases}",
        )

        name, lineno, _ = found[0]
        self.assertIn(
            lineno, conflict_lines[name],
            "1008 close 가 `except SubscribeIdentityConflict` 밖으로 나갔다 — 그 순간 코드가\n"
            "가리키는 사유가 불명확해진다(가이드 §6 은 cross-UID 로 좁혀 적혀 있다).",
        )

    def test_the_detector_is_fail_closed_on_dynamic_code_values(self):
        """⛔ 자기검사 — **비리터럴 code 를 놓치면 검출기 전체가 fail-open** 이다.

        리터럴만 세던 구 버전은 `close(code=POLICY_VIOLATION)` 을 아예 기록하지 않아, 두 번째
        발신자를 추가해도 최종 단언이 **green** 이었다(codex Blocker).
        """
        import ast

        tree = ast.parse(
            "async def evict(ws, db, opts):\n"
            "    db.close()\n"                       # 인자 없음 → 정책 신호 아님
            "    await ws.close()\n"                 # code 미지정(1000) → 정책 신호 아님
            "    await ws.close(code=1008)\n"        # 리터럴 keyword
            "    await ws.close(4001)\n"             # 리터럴 positional
            "    await ws.close(code=POLICY_VIOLATION)\n"   # 동적 keyword → None
            "    await ws.close(get_close_code())\n"        # 동적 positional → None
            "    await ws.close(**opts)\n"                  # 언패킹 → None
        )
        self.assertEqual(
            [code for _, code in self._close_sites(tree)],
            [1008, 4001, None, None, None],
            "동적 code 를 놓치면 두 번째 발신자가 조용히 통과한다",
        )

    def test_the_alias_ban_detector_finds_a_stashed_close(self):
        """⛔ 자기검사 — alias 금지가 실제로 발화하는지. (추적이 아니라 **금지**가 방어다.)"""
        import ast

        tree = ast.parse("def f(ws):\n    finish = ws.close\n    return finish\n")
        self.assertEqual(self._close_aliases(tree), [2])
        self.assertEqual(self._close_aliases(ast.parse("def g(ws):\n    x = ws.send\n")), [])

    def test_the_handler_scope_detector_would_notice_a_move(self):
        """⛔ 자기검사 — 핸들러 범위 검출기가 실제로 안팎을 가르는지."""
        import ast

        tree = ast.parse(
            "async def f(ws):\n"
            "    try:\n"
            "        bind()\n"
            "    except SubscribeIdentityConflict:\n"
            "        await ws.close(code=1008)\n"    # line 5 — 안
            "    await ws.close(code=1008)\n"        # line 6 — 밖
        )
        covered = self._identity_conflict_handler_lines(tree)
        self.assertIn(5, covered)
        self.assertNotIn(6, covered)


class TestConnectionIdentityBinding(unittest.TestCase):
    """⛔ 이 가드는 **테스트가 하나도 없었다**(변이가 찾아냈다). 뚫리면 falsy 결속 이후
    **아무 UID 나** 충돌 없이 통과한다 — cross-UID 방어 전체가 무의미해진다."""

    def test_falsy_uid_is_refused_rather_than_silently_unbound(self):
        for bad in (None, "", 0, b"uid"):
            with self.subTest(uid=bad):
                identity = ConnectionIdentity()
                with self.assertRaises(ValueError):
                    identity.bind(bad)
                self.assertIsNone(
                    identity.uid, f"거부해 놓고 {bad!r} 를 결속했다")
                # ⛔ **핵심**: 거부 뒤에도 소유권은 비어 있어야 하고, 그 다음 정상 UID 가
                #    첫 소유자가 되어야 한다(빈 결속이 소유권을 삼키면 안 된다).
                identity.bind("uid-real")
                self.assertEqual(identity.uid, "uid-real")

    def test_same_uid_rebinds_but_a_different_one_conflicts(self):
        identity = ConnectionIdentity()
        identity.bind("uid-a")
        identity.bind("uid-a")                     # 토큰 갱신 — 허용
        self.assertEqual(identity.uid, "uid-a")
        with self.assertRaises(SubscribeIdentityConflict) as ctx:
            identity.bind("uid-b")
        self.assertEqual((ctx.exception.bound_uid, ctx.exception.presented_uid),
                         ("uid-a", "uid-b"))
        self.assertEqual(identity.uid, "uid-a",
                         "충돌 뒤 소유권이 새 UID 로 넘어갔다 — 거부의 의미가 없다")


if __name__ == "__main__":
    unittest.main()
