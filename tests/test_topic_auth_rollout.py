"""익명 subscribe rollout 정책·계측 (`app/topic_auth_rollout.py`) 단위 회귀.

⛔ 이 파일이 존재하는 이유: 설계 검토 중 확인한 것들이 전부 **일회성 shell probe** 였다.
   probe 는 그 순간만 증명하고 회귀를 막지 못한다 — 아래 반례들은 실제로 코드에 있었던
   fail-open 이거나(문자열 iterable · enum↔문자열 불일치), 있었다면 조용히 열렸을 것들이다.

⛔ 배선(dispatcher/main) 테스트는 여기 두지 않는다 — 이 모듈은 publisher/main runtime 에
   의존하지 않아야 하고, 실제 메시지 행동 계약은 dispatcher 쪽 테스트가 잠근다.
"""
import os
import pathlib
import subprocess
import sys
import unittest

from app import config
from app.topic_auth_rollout import TopicAuthRollout, TopicAuthStage

REPO = pathlib.Path(__file__).resolve().parent.parent


def _stage_in_fresh_process(value):
    """새 프로세스에서 `config.WS_TOPIC_AUTH_STAGE.value` 를 읽는다.

    ⛔ `importlib.reload()` 를 쓰지 않는다 — 이미 로드된 모듈과 `load_dotenv()` 의 부작용이
       남아 무엇을 관측하는지 흐려진다.

    ⛔ 로컬 `.env` 는 정상 운영 설정이므로 "이 키가 없어야 한다"를 테스트하지
       않는다. fresh process 안에서 `load_dotenv` 를 무효화해 기본값 검사를 격리한다.
    """
    env = dict(os.environ)
    env.pop("WS_TOPIC_AUTH_STAGE", None)
    if value is not None:
        env["WS_TOPIC_AUTH_STAGE"] = value
    return subprocess.run(
        [sys.executable, "-c",
         (
             "import dotenv; "
             "dotenv.load_dotenv = lambda *args, **kwargs: False; "
             "from app import config; "
             "print(config.WS_TOPIC_AUTH_STAGE.value)"
         )],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
    )

FX = ("fx:usd-krw", "fx:jpy-krw", "fx:eur-krw")
USDT = "usdt:krw"


KRX = "krx:usd-krw-futures"
# 정책표 전체 = FX 3 + USDT + KRX. 최종-stage RC 후보는 KRX 배포 flag off 를 반영해 4종.
POLICY = (*FX, USDT, KRX)
RC_CANDIDATES = (*FX, USDT)


def _rollout(stage=TopicAuthStage.COMPATIBILITY, **kw):
    kw.setdefault("policy_topics", POLICY)
    kw.setdefault("final_stage_rc_candidate_topics", RC_CANDIDATES)
    return TopicAuthRollout(stage=stage, fx_topics=FX, usdt_topic=USDT, **kw)


class _Sock:
    """WebSocket 대역 — 해시 가능한 아무 객체면 된다(모듈이 타입을 보지 않는다)."""


class TestConfigParserIsStrict(unittest.TestCase):
    """⛔ 보안 강제 단계라 **enum 에 정의된 문자열만** 받는다. 조용한 fallback 은 금지."""

    def test_module_level_value_is_parsed_not_a_string(self):
        """⛔ 실제 결함이었다 — config 가 문자열을 저장하고 소비자가 enum 과 비교해
        기본값에서도 filter 는 ValueError, snapshot 은 AttributeError 였다."""
        self.assertIsInstance(config.WS_TOPIC_AUTH_STAGE, TopicAuthStage)

    def test_default_is_compatibility_in_a_fresh_process(self):
        """⛔ parser 에 "compatibility" 를 직접 넣는 것은 **기본값 검사가 아니다** —
        `WS_TOPIC_AUTH_STAGE=reject_anonymous_fx` 환경에서도 통과한다(실측).
        기본값은 env 를 지운 **새 프로세스**에서만 관측된다.
        """
        result = _stage_in_fresh_process(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "compatibility")

    def test_explicit_values_are_honoured_in_a_fresh_process(self):
        for value in ("reject_anonymous_fx", "enforce_authenticated_premium"):
            with self.subTest(value=value):
                result = _stage_in_fresh_process(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), value)

    def test_bad_values_fail_at_import_time_in_a_fresh_process(self):
        for raw in ("Compatibility", " compatibility ", "enforce_fx"):
            with self.subTest(raw=raw):
                result = _stage_in_fresh_process(raw)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("WS_TOPIC_AUTH_STAGE must be one of", result.stderr)

    def test_legacy_allowed_tuple_is_absent(self):
        """기존의 중복 정본이던 ALLOWED tuple 을 다시 도입하지 않는다."""
        self.assertFalse(hasattr(config, "WS_TOPIC_AUTH_ALLOWED_STAGES"))

    def test_rejects_non_string_input(self):
        """⛔ "정확히 문자열만" 계약 — `TopicAuthStage(member)` 는 그 member 를 그대로
        돌려주므로, 타입 검사가 없으면 enum 인스턴스가 통과해 계약이 거짓이 된다(실측).
        """
        for raw in (TopicAuthStage.COMPATIBILITY, None, 1, b"compatibility",
                    ["compatibility"]):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                config.parse_topic_auth_stage(raw)

    def test_rejects_case_variants_whitespace_and_unknown(self):
        for raw in ("Compatibility", "REJECT_ANONYMOUS_FX", " compatibility ",
                    "compatibility\n", "enforce_fx", ""):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                config.parse_topic_auth_stage(raw)


class TestConstructorRejectsMiswiring(unittest.TestCase):
    """⛔ 배선 오류는 요청 경로가 아니라 **생성 시점**에 터져야 한다."""

    def test_string_is_not_a_topic_collection(self):
        """⛔ 실제 fail-open 이었다 — `list("fx:usd-krw")` 가 문자 집합이 되어 통과했고,
        그 상태의 `reject_anonymous_fx` 는 진짜 topic 을 제거하지 못한다."""
        with self.assertRaises(TypeError):
            TopicAuthRollout(stage=TopicAuthStage.COMPATIBILITY,
                             fx_topics="fx:usd-krw", usdt_topic=USDT, policy_topics=POLICY, final_stage_rc_candidate_topics=RC_CANDIDATES)

    def test_bytes_is_not_a_topic_collection(self):
        with self.assertRaises(TypeError):
            TopicAuthRollout(stage=TopicAuthStage.COMPATIBILITY,
                             fx_topics=b"fx:usd-krw", usdt_topic=USDT, policy_topics=POLICY, final_stage_rc_candidate_topics=RC_CANDIDATES)

    def test_mapping_is_rejected_so_values_cannot_be_forgotten(self):
        """⛔ `FX_TOPICS` 를 `.values()` 없이 넘기면 **키**(asset 이름)가 들어온다."""
        with self.assertRaises(TypeError):
            # ⛔ key 가 canonical 이어야 **guard 를 실제로 시험**한다. 비-canonical key 를 쓰면
            #    guard 를 지워도 뒤쪽 형식 검증이 ValueError 로 잡아 시험이 무의미해진다.
            TopicAuthRollout(stage=TopicAuthStage.COMPATIBILITY,
                             fx_topics={"fx:usd-krw": "ignored"}, usdt_topic=USDT, policy_topics=POLICY, final_stage_rc_candidate_topics=RC_CANDIDATES)

    def test_stage_must_be_the_enum_not_its_value(self):
        with self.assertRaises(TypeError):
            TopicAuthRollout(stage="compatibility", fx_topics=FX, usdt_topic=USDT, policy_topics=POLICY, final_stage_rc_candidate_topics=RC_CANDIDATES)

    def test_rejects_empty_duplicate_nonstring_and_malformed_fx_topics(self):
        for label, fx in [
            ("빈 collection", []),
            ("중복", ["fx:usd-krw", "fx:usd-krw"]),
            ("비-str", [1]),
            ("unhashable 비-str", [["fx:usd-krw"]]),
            ("빈 문자열", [""]),
            ("빈 suffix", ["fx:"]),
            ("앞 공백", [" fx:usd-krw"]),
            ("뒤 공백", ["fx:usd-krw "]),
            ("내부 공백", ["fx:usd krw"]),
            ("prefix 아님", ["usdt:krw"]),
        ]:
            with self.subTest(label=label), self.assertRaises(ValueError):
                TopicAuthRollout(stage=TopicAuthStage.COMPATIBILITY,
                                 fx_topics=fx, usdt_topic=USDT, policy_topics=POLICY, final_stage_rc_candidate_topics=RC_CANDIDATES)

    def test_rejects_malformed_usdt_topic(self):
        for usdt in ("", "usdt:", " usdt:krw", "usdt:krw ",
                     "usdt:k rw", "fx:usd-krw", "not-usdt"):
            with self.subTest(usdt=usdt), self.assertRaises(ValueError):
                TopicAuthRollout(stage=TopicAuthStage.COMPATIBILITY,
                                 fx_topics=FX, usdt_topic=usdt, policy_topics=POLICY,
                                 final_stage_rc_candidate_topics=RC_CANDIDATES)

    def test_started_at_seam_rejects_bool_nan_and_negative(self):
        """⛔ `bool` 은 float subclass 라 `True` 가 1.0 으로 통과한다."""
        for bad in (True, float("nan"), float("inf"), -1.0, "0"):
            with self.subTest(bad=bad), self.assertRaises((TypeError, ValueError)):
                _rollout(started_at_epoch_seconds=bad)

    def test_started_at_seam_accepts_a_finite_value(self):
        self.assertEqual(_rollout(started_at_epoch_seconds=0)
                         .snapshot()["started_at_epoch_seconds"], 0.0)


class TestPolicy(unittest.TestCase):
    def test_compatibility_passes_everything_through(self):
        r = _rollout()
        free = ["fx:usd-krw", USDT, "fx:usd-krw"]
        self.assertEqual(r.filter_anonymous_topics(free), free)

    def test_reject_removes_only_the_exact_fx_set(self):
        r = _rollout(TopicAuthStage.REJECT_ANONYMOUS_FX)
        # `fx:` 로 시작하지만 주입 집합에 없는 문자열은 정책 대상이 아니다(prefix 판정 금지).
        self.assertEqual(
            r.filter_anonymous_topics(["fx:usd-krw", USDT, "fx:not-a-real-topic"]),
            [USDT, "fx:not-a-real-topic"],
        )

    def test_enforce_rejects_every_anonymous_topic(self):
        r = _rollout(TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM)
        self.assertEqual(r.filter_anonymous_topics(["fx:usd-krw", USDT]), [])

    def test_order_and_duplicates_are_preserved(self):
        r = _rollout(TopicAuthStage.REJECT_ANONYMOUS_FX)
        self.assertEqual(
            r.filter_anonymous_topics([USDT, "fx:usd-krw", USDT]), [USDT, USDT])

    def test_unknown_stage_fails_closed_rather_than_returning_input(self):
        r = _rollout()
        r._stage = object()          # 도달 불가 상태를 강제로 만든다
        with self.assertRaises(ValueError):
            r.filter_anonymous_topics(["fx:usd-krw"])

    def test_authenticated_planner_uses_the_runtime_object_stage(self):
        """config patch가 아니라 production에 주입되는 rollout 객체가 두 정책축의 정본이다."""
        compatibility = _rollout(TopicAuthStage.COMPATIBILITY)
        enforced = _rollout(TopicAuthStage.ENFORCE_AUTHENTICATED_PREMIUM)

        self.assertEqual(
            compatibility.plan_authenticated_topics([USDT], uid="u1").identity_only,
            (USDT,),
        )
        self.assertEqual(
            enforced.plan_authenticated_topics([USDT], uid="u1").premium_only,
            (USDT,),
        )


class TestMetrics(unittest.TestCase):
    def test_duplicate_topics_in_one_request_count_once(self):
        r = _rollout()
        r.observe_anonymous_subscribe(_Sock(), ["fx:usd-krw", "fx:usd-krw"])
        s = r.snapshot()
        self.assertEqual(s["per_topic_attempts"]["fx:usd-krw"], 1)
        self.assertEqual(s["anonymous_fx_attempts_total"], 1)

    def test_fx_union_is_not_the_sum_of_per_topic(self):
        """⛔ per_topic 합산은 한 요청이 FX 3종을 함께 요청할 때 **중복 계산**된다."""
        r = _rollout()
        r.observe_anonymous_subscribe(_Sock(), list(FX))
        s = r.snapshot()
        self.assertEqual(sum(s["per_topic_attempts"][t] for t in FX), 3)
        self.assertEqual(s["anonymous_fx_attempts_total"], 1)

    def test_scoped_total_includes_usdt_but_fx_total_does_not(self):
        r = _rollout()
        r.observe_anonymous_subscribe(_Sock(), [USDT])
        s = r.snapshot()
        self.assertEqual(s["anonymous_auth_scoped_attempts_total"], 1)
        self.assertEqual(s["anonymous_fx_attempts_total"], 0,
                         "USDT-only 연결이 FX 전환 위험으로 집계되면 안 된다")

    def test_unscoped_request_counts_only_the_anonymous_total(self):
        r = _rollout()
        r.observe_anonymous_subscribe(_Sock(), ["krx:usd-krw-futures", "nope"])
        s = r.snapshot()
        self.assertEqual(s["anonymous_subscribe_attempts_total"], 1)
        self.assertEqual(s["anonymous_auth_scoped_attempts_total"], 0)

    def test_first_seen_counts_once_per_connection_and_topic(self):
        r, sock = _rollout(), _Sock()
        for _ in range(3):
            r.observe_anonymous_subscribe(sock, ["fx:usd-krw"])
        s = r.snapshot()
        self.assertEqual(s["per_topic_attempts"]["fx:usd-krw"], 3)
        self.assertEqual(s["per_topic_first_seen_connections"]["fx:usd-krw"], 1)
        self.assertEqual(s["anonymous_fx_first_seen_connections_total"], 1)

    def test_a_second_fx_topic_on_the_same_connection_is_not_a_new_fx_connection(self):
        r, sock = _rollout(), _Sock()
        r.observe_anonymous_subscribe(sock, ["fx:usd-krw"])
        r.observe_anonymous_subscribe(sock, ["fx:jpy-krw"])
        self.assertEqual(r.snapshot()["anonymous_fx_first_seen_connections_total"], 1)

    def test_disconnect_is_idempotent_and_frees_per_connection_state(self):
        r, sock = _rollout(), _Sock()
        r.observe_anonymous_subscribe(sock, ["fx:usd-krw"])
        self.assertEqual(r.snapshot()["active_auth_scoped_connections_tracked"], 1)
        r.disconnect(sock)
        r.disconnect(sock)                              # 멱등
        self.assertEqual(r.snapshot()["active_auth_scoped_connections_tracked"], 0)
        # ⚠️ 누적 counter 는 줄지 않는다.
        self.assertEqual(r.snapshot()["anonymous_fx_first_seen_connections_total"], 1)

    def test_reconnect_counts_a_new_first_seen(self):
        r = _rollout()
        a = _Sock()
        r.observe_anonymous_subscribe(a, ["fx:usd-krw"])
        r.disconnect(a)
        r.observe_anonymous_subscribe(_Sock(), ["fx:usd-krw"])
        self.assertEqual(r.snapshot()["anonymous_fx_first_seen_connections_total"], 2)


class TestTokenBearingObservation(unittest.TestCase):
    """미검증 token-bearing 후보 계측 — **인증 수요가 아니다.**

    ⛔ 계측 지점이 Firebase 검증 **전**이라 이 값에는 공격·오타 토큰이 섞인다. 이름과 caveat 이
       그 사실을 운반하고, 아래 회귀가 채널 분리·정리 소유권·가용성 구분을 잠근다.
    """

    def test_channels_are_separate_but_share_one_connection_entry(self):
        """⛔ 같은 소켓이 두 채널 모두에서 first-seen 으로 잡힌다 — `identified` 는 메시지마다
        재계산되므로 연결 집합은 서로소가 아니다. 그런데 저장소는 하나여야 한다."""
        for label, order in (("익명→token", ("anon", "tb")), ("token→익명", ("tb", "anon"))):
            with self.subTest(label=label):
                r, sock = _rollout(), _Sock()
                for kind in order:
                    if kind == "anon":
                        r.observe_anonymous_subscribe(sock, [FX[0]])
                    else:
                        r.observe_token_bearing_subscribe(sock, [FX[0]])
                snap = r.snapshot()
                self.assertEqual(snap["per_topic_first_seen_connections"][FX[0]], 1)
                self.assertEqual(
                    snap["unverified_token_bearing_per_topic_first_seen_connections"][FX[0]], 1)
                self.assertEqual(snap["active_auth_scoped_connections_tracked"], 1)
                self.assertEqual(
                    snap["unverified_token_bearing_active_connections_tracked"], 1)

    def test_retries_increment_attempts_but_not_first_seen_on_the_same_connection(self):
        """재시도는 attempts만 늘리고 topic·policy first-seen은 연결당 한 번만 센다."""
        r, sock = _rollout(), _Sock()
        r.observe_token_bearing_subscribe(sock, [FX[0]])
        r.observe_token_bearing_subscribe(sock, [FX[0]])
        r.observe_token_bearing_subscribe(sock, [FX[1]])

        snap = r.snapshot()
        self.assertEqual(snap["unverified_token_bearing_policy_attempts_total"], 3)
        self.assertEqual(
            snap["unverified_token_bearing_policy_first_seen_connections_total"], 1)
        self.assertEqual(snap["unverified_token_bearing_per_topic_attempts"][FX[0]], 2)
        self.assertEqual(snap["unverified_token_bearing_per_topic_attempts"][FX[1]], 1)
        self.assertEqual(
            snap["unverified_token_bearing_per_topic_first_seen_connections"][FX[0]], 1)
        self.assertEqual(
            snap["unverified_token_bearing_per_topic_first_seen_connections"][FX[1]], 1)

        # disconnect 뒤의 새 소켓은 새 연결이므로 누적 first-seen이 다시 증가한다.
        r.disconnect(sock)
        r.observe_token_bearing_subscribe(_Sock(), [FX[0]])
        snap = r.snapshot()
        self.assertEqual(snap["unverified_token_bearing_policy_attempts_total"], 4)
        self.assertEqual(
            snap["unverified_token_bearing_policy_first_seen_connections_total"], 2)
        self.assertEqual(snap["unverified_token_bearing_per_topic_attempts"][FX[0]], 3)
        self.assertEqual(
            snap["unverified_token_bearing_per_topic_first_seen_connections"][FX[0]], 2)

    def test_one_disconnect_clears_both_channels(self):
        r, sock = _rollout(), _Sock()
        r.observe_anonymous_subscribe(sock, [FX[0]])
        r.observe_token_bearing_subscribe(sock, [USDT])
        r.disconnect(sock)
        snap = r.snapshot()
        self.assertEqual(snap["active_auth_scoped_connections_tracked"], 0)
        self.assertEqual(snap["unverified_token_bearing_active_connections_tracked"], 0)

    def test_krx_only_counts_for_policy_but_not_final_stage_rc_candidate(self):
        """⛔ 가용성 구분 — KRX 배포 flag off 면 planner 에 도달하지 않으므로 RC 후보가 아니다."""
        r = _rollout()
        r.observe_token_bearing_subscribe(_Sock(), [KRX])
        snap = r.snapshot()
        self.assertEqual(snap["unverified_token_bearing_policy_attempts_total"], 1)
        self.assertEqual(
            snap["unverified_token_bearing_final_stage_rc_candidate_attempts_total"], 0)
        self.assertEqual(snap["unverified_token_bearing_per_topic_attempts"][KRX], 1)

    def test_mixed_available_fx_and_unavailable_krx_is_an_rc_candidate(self):
        """union은 요청 전체 거부가 아니다. KRX가 unavailable이어도 FX가 RC까지 간다."""
        r = _rollout()
        r.observe_token_bearing_subscribe(_Sock(), [FX[0], KRX])
        snap = r.snapshot()
        self.assertEqual(
            snap["unverified_token_bearing_final_stage_rc_candidate_attempts_total"], 1)

    def test_duplicate_topics_in_one_request_count_once(self):
        r = _rollout()
        r.observe_token_bearing_subscribe(_Sock(), [FX[0], FX[0], FX[0]])
        snap = r.snapshot()
        self.assertEqual(snap["unverified_token_bearing_per_topic_attempts"][FX[0]], 1)
        self.assertEqual(snap["unverified_token_bearing_policy_attempts_total"], 1)

    def test_off_policy_topics_do_not_create_keys_or_policy_attempts(self):
        r = _rollout()
        r.observe_token_bearing_subscribe(_Sock(), ["아무거나", "fx:made-up"])
        snap = r.snapshot()
        self.assertEqual(snap["unverified_token_bearing_subscribe_attempts_total"], 1)
        self.assertEqual(snap["unverified_token_bearing_policy_attempts_total"], 0)
        self.assertEqual(snap["unverified_token_bearing_active_connections_tracked"], 1)
        self.assertEqual(set(snap["unverified_token_bearing_per_topic_attempts"]), set(POLICY))

    def test_anonymous_fields_keep_their_meaning(self):
        """⛔ token-bearing 전용 연결이 익명 필드를 부풀리면 안 된다 — 구 관측치와 비교 불가해진다."""
        r = _rollout()
        r.observe_token_bearing_subscribe(_Sock(), [FX[0], USDT])
        snap = r.snapshot()
        for field in ("anonymous_subscribe_attempts_total", "anonymous_auth_scoped_attempts_total",
                      "anonymous_fx_attempts_total", "anonymous_fx_first_seen_connections_total",
                      "active_auth_scoped_connections_tracked"):
            self.assertEqual(snap[field], 0, field)
        self.assertEqual(set(snap["per_topic_attempts"].values()), {0})

    def test_anonymous_only_connection_does_not_inflate_token_bearing_active(self):
        """active gauge도 채널별이다 — 공유 map의 전체 길이를 쓰면 익명 연결까지 섞인다."""
        r = _rollout()
        r.observe_anonymous_subscribe(_Sock(), [FX[0]])
        snap = r.snapshot()
        self.assertEqual(snap["active_auth_scoped_connections_tracked"], 1)
        self.assertEqual(snap["unverified_token_bearing_active_connections_tracked"], 0)

    def test_injected_sets_are_validated(self):
        for label, kw in (
            ("policy 문자열", {"policy_topics": "fx:usd-krw"}),
            ("policy 빈 값", {"policy_topics": []}),
            ("policy 중복", {"policy_topics": (*POLICY, POLICY[0])}),
            ("RC 후보 문자열", {"final_stage_rc_candidate_topics": "fx:usd-krw"}),
            ("RC 후보 중복", {"final_stage_rc_candidate_topics": (FX[0], FX[0])}),
            ("RC 후보가 정책표 밖", {"final_stage_rc_candidate_topics": ("dxy:spot",)}),
        ):
            with self.subTest(label=label), self.assertRaises((TypeError, ValueError)):
                _rollout(**kw)

    def test_policy_must_include_the_rollout_canonical_topics(self):
        """후보 집합도 같이 축소해 subset 검사 뒤의 canonical 배선 가드를 직접 친다."""
        with self.assertRaises(ValueError):
            _rollout(
                policy_topics=(*FX, KRX),
                final_stage_rc_candidate_topics=FX,
            )


class TestSnapshotSchema(unittest.TestCase):
    EXPECTED = {
        "scope", "pid", "started_at_epoch_seconds", "stage",
        "anonymous_subscribe_attempts_total", "anonymous_auth_scoped_attempts_total",
        "anonymous_fx_attempts_total", "anonymous_fx_first_seen_connections_total",
        "per_topic_attempts", "per_topic_first_seen_connections",
        "active_auth_scoped_connections_tracked", "caveat",
        # token-bearing 축 — 익명 필드의 의미를 넓히지 않고 **별도**로 붙는다.
        "unverified_token_bearing_subscribe_attempts_total",
        "unverified_token_bearing_policy_attempts_total",
        "unverified_token_bearing_final_stage_rc_candidate_attempts_total",
        "unverified_token_bearing_final_stage_rc_candidate_topics",
        "unverified_token_bearing_policy_first_seen_connections_total",
        "unverified_token_bearing_per_topic_attempts",
        "unverified_token_bearing_per_topic_first_seen_connections",
        "unverified_token_bearing_active_connections_tracked",
    }

    def test_schema_is_fixed(self):
        rollout = _rollout()
        # frozenset 반복 순서가 우연히 정렬될 수 있으므로 역순 iterable을 주입해 정렬 자체를 친다.
        # raw snapshot을 해시하는 배포 절차상 같은 값은 항상 같은 JSON 배열 순서여야 한다.
        rollout._final_stage_rc_candidate_topics = tuple(
            reversed(sorted(RC_CANDIDATES))
        )
        snapshot = rollout.snapshot()
        self.assertEqual(set(snapshot), self.EXPECTED)
        self.assertEqual(
            snapshot["unverified_token_bearing_final_stage_rc_candidate_topics"],
            sorted(RC_CANDIDATES),
        )

    def test_runtime_context_is_merged_by_the_endpoint_not_read_here(self):
        """⛔ 이 객체는 런타임 flag 를 읽지 않는다 — 그건 admin endpoint 가 합친다."""
        self.assertNotIn("topic_dispatcher_enabled", _rollout().snapshot())

    def test_counter_keys_are_fixed_at_construction(self):
        """⛔ 클라가 보낸 문자열이 key 가 되면 cardinality 가 폭발한다."""
        r = _rollout()
        r.observe_anonymous_subscribe(_Sock(), ["아무거나", "fx:made-up"])
        for field in ("per_topic_attempts", "per_topic_first_seen_connections"):
            self.assertEqual(set(r.snapshot()[field]), set(FX) | {USDT})

    def test_snapshot_carries_no_identifying_material(self):
        """⛔ 검사 대상은 **값**이지 필드명이 아니다.

        구 판은 `repr(snapshot())` 에서 `"token"` 부분 문자열을 찾았는데, 그건 정당한 필드명
        (`unverified_token_bearing_*`)과 충돌한다. 단어가 아니라 **토큰 물질**과 **key 어휘**를
        본다 — 그래야 필드가 늘어도 판별력이 유지된다.
        """
        import re

        r, sock = _rollout(), _Sock()
        r.observe_anonymous_subscribe(sock, ["fx:usd-krw"])
        r.observe_token_bearing_subscribe(sock, ["fx:usd-krw", KRX])
        snap = r.snapshot()

        def strings(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from strings(v)
            elif isinstance(node, (list, tuple)):
                for v in node:
                    yield from strings(v)
            elif isinstance(node, str):
                yield node

        material = re.compile(r"eyJ[A-Za-z0-9_-]{10,}|\bBearer\b|0x[0-9a-f]{8,}", re.I)
        for value in strings(snap):
            self.assertIsNone(material.search(value), f"토큰 물질로 보이는 값: {value!r}")
        self.assertNotIn(repr(sock), repr(snap))
        # key 어휘는 고정 — 클라 문자열이 key 가 되지 않는다.
        for field in ("per_topic_attempts", "per_topic_first_seen_connections"):
            self.assertEqual(set(snap[field]), set(FX) | {USDT})
        for field in ("unverified_token_bearing_per_topic_attempts",
                      "unverified_token_bearing_per_topic_first_seen_connections"):
            self.assertEqual(set(snap[field]), set(POLICY))


if __name__ == "__main__":
    unittest.main()
