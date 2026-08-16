"""scripts/krx_deploy_verifier.py 단위 테스트 — Docker·실시간·네트워크 의존 0.

전부 순수 함수 또는 주입된 어댑터로 검증한다. `logs/app.log` 쓰기나 임시
디렉터리 의존도 없다(artifact는 `tmp_path` 아래에만 만든다).

실행:
    python -m pytest tests/test_krx_deploy_verifier.py -p no:asyncio -q
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import krx_deploy_verifier as V  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULER = REPO_ROOT / "app" / "scheduler.py"

_T0 = "2026-08-17T06:00:00+00:00"


def _baseline(**over) -> V.Baseline:
    base = dict(
        t0_iso=_T0,
        old_container_id="old123",
        old_started_at="2026-08-15T00:00:00Z",
        old_restart_count=0,
        old_image="sha256:aaa",
        expected_code="A75609",
        expected_month="202609",
        expected_expires_on="2026-09-21",
    )
    base.update(over)
    return V.Baseline(**base)


def _contract(code="A75609", month="202609", expires="2026-09-21") -> dict:
    return {"code": code, "month": month, "expires_on": expires}


def _status(**over) -> dict:
    """PASS 6조건을 모두 만족하는 기준 샘플."""
    base = {
        "enabled": True,
        "started": True,
        "job_registered": True,
        "last_run_at_kst": "2026-08-17T15:10:00+09:00",  # T0(06:00Z=15:00 KST) 이후
        "last_result": "no_op",
        "last_current_contract": _contract(),
        "last_resolved_contract": _contract(),
        "client_contract": _contract(),
        "rollover_count": 0,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 정책 맵 완전성 (AST)
# ---------------------------------------------------------------------------

class TestPolicyMapCompleteness(unittest.TestCase):
    """`RECONCILE_RESULTS`가 scheduler의 literal 전수를 빠짐없이 덮는가."""

    def test_extracted_results_match_declared_vocabulary(self):
        found = V.extract_reconcile_results(SCHEDULER)
        self.assertTrue(found, "scheduler에서 result literal을 하나도 못 찾았다")
        self.assertEqual(found, set(V.RECONCILE_RESULTS))

    def test_every_result_is_classified_in_both_phases(self):
        """미분류 result가 남아 있지 않다 — verdict가 ValueError를 내지 않는다."""
        for result in V.RECONCILE_RESULTS:
            for phase in ("pre", "post"):
                for deadline in (False, True):
                    with self.subTest(result=result, phase=phase, deadline=deadline):
                        got = V.verdict(result, phase=phase, deadline_passed=deadline)
                        self.assertIn(got, {V.OBSERVED, V.PASS, V.PENDING, V.FAILED})

    def test_unknown_result_raises(self):
        with self.assertRaises(ValueError):
            V.verdict("brand_new_state", phase="post", deadline_passed=False)

    def test_nonliteral_result_call_is_rejected(self):
        """변수·**kwargs·누락 전달은 정책 우회 → 추출 단계에서 실패."""
        for src in (
            "_record_krx_reconcile(result=some_var)\n",
            "_record_krx_reconcile(**kw)\n",
            "_record_krx_reconcile(current=c)\n",
        ):
            with self.subTest(src=src):
                with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
                    fh.write(src)
                    tmp = pathlib.Path(fh.name)
                try:
                    with self.assertRaises(ValueError):
                        V.extract_reconcile_results(tmp)
                finally:
                    tmp.unlink()


# ---------------------------------------------------------------------------
# verdict — phase 의존
# ---------------------------------------------------------------------------

class TestVerdictPhaseDependence(unittest.TestCase):

    def test_rollover_is_evidence_pre_but_failure_post(self):
        """정적 맵으로는 표현 불가한 축 — 같은 result가 phase에 따라 갈린다."""
        self.assertEqual(V.verdict("rollover", phase="pre", deadline_passed=False),
                         V.OBSERVED)
        self.assertEqual(V.verdict("rollover", phase="post", deadline_passed=False),
                         V.FAILED)

    def test_transient_states_pend_before_deadline_fail_after(self):
        for result in ("bootstrap_skipped", "bootstrap_started"):
            with self.subTest(result=result):
                self.assertEqual(
                    V.verdict(result, phase="post", deadline_passed=False), V.PENDING)
                self.assertEqual(
                    V.verdict(result, phase="post", deadline_passed=True), V.FAILED)

    def test_task_dead_restarted_is_failed_even_though_recovered(self):
        """복구됐어도 clean deploy의 'task crash 0'에는 위배."""
        self.assertEqual(
            V.verdict("task_dead_restarted", phase="post", deadline_passed=False),
            V.FAILED)

    def test_error_states_always_failed_post(self):
        for result in ("resolve_error", "resolved_none", "bootstrap_error",
                       "shutdown_error", "jump_suppressed", "task_dead_restart_error"):
            with self.subTest(result=result):
                self.assertEqual(
                    V.verdict(result, phase="post", deadline_passed=False), V.FAILED)

    def test_unknown_phase_raises(self):
        with self.assertRaises(ValueError):
            V.verdict("no_op", phase="during", deadline_passed=False)


# ---------------------------------------------------------------------------
# sticky 집계
# ---------------------------------------------------------------------------

class TestAggregate(unittest.TestCase):

    def test_failed_is_sticky_even_if_last_sample_passes(self):
        """최종 no_op 하나로 PASS를 계산하면 중간 FAILED가 덮인다."""
        self.assertEqual(
            V.aggregate([V.PENDING, V.FAILED, V.PASS, V.PASS]), V.FAILED)

    def test_pending_is_not_sticky(self):
        """정상 배포도 반드시 과도기를 지난다 — PENDING 누적 고정이면 아무도 통과 못 한다."""
        self.assertEqual(V.aggregate([V.PENDING, V.PENDING, V.PASS]), V.PASS)

    def test_unverified_is_not_sticky_either(self):
        """"FAILED만 sticky" 계약 — 과도기의 일시 조회 실패는 회복 가능하다.

        UNVERIFIED가 **최종** 판정이 되는 경우는 "deadline까지 유효 샘플 0건"이고,
        그건 빈 시퀀스로 표현된다(orchestrator가 결정).
        """
        self.assertEqual(V.aggregate([V.UNVERIFIED, V.PASS]), V.PASS)
        self.assertEqual(V.aggregate([V.UNVERIFIED, V.FAILED]), V.FAILED)

    def test_empty_samples_are_unverified(self):
        """유효 샘플 0건 = 관측 불능."""
        self.assertEqual(V.aggregate([]), V.UNVERIFIED)

    def test_worst_combines_axes(self):
        self.assertEqual(V.worst(V.PASS, V.UNVERIFIED), V.UNVERIFIED)
        self.assertEqual(V.worst(V.PASS, V.PENDING, V.FAILED), V.FAILED)


# ---------------------------------------------------------------------------
# PASS 6조건
# ---------------------------------------------------------------------------

class TestEvaluatePostSample(unittest.TestCase):

    def test_all_conditions_met_is_pass(self):
        got, reasons = V.evaluate_post_sample(_status(), _baseline(),
                                              deadline_passed=False)
        self.assertEqual((got, reasons), (V.PASS, []))

    def test_stale_tick_pends_then_fails_after_deadline(self):
        stale = _status(last_run_at_kst="2026-08-17T14:00:00+09:00")  # T0 이전
        self.assertEqual(
            V.evaluate_post_sample(stale, _baseline(), deadline_passed=False)[0],
            V.PENDING)
        self.assertEqual(
            V.evaluate_post_sample(stale, _baseline(), deadline_passed=True)[0],
            V.FAILED)

    def test_contract_tuple_requires_all_three_fields(self):
        """code만 맞고 month/expires_on이 다르면 FAILED."""
        for bad in (_contract(month="202610"), _contract(expires="2026-10-19")):
            with self.subTest(bad=bad):
                got, reasons = V.evaluate_post_sample(
                    _status(client_contract=bad), _baseline(), deadline_passed=False)
                self.assertEqual(got, V.FAILED)
                self.assertTrue(any("client_contract" in r for r in reasons))

    def test_nonzero_rollover_count_fails(self):
        """샘플 gap 사이 rollover가 no_op에 덮여도 counter가 잡는다."""
        got, reasons = V.evaluate_post_sample(
            _status(rollover_count=1), _baseline(), deadline_passed=False)
        self.assertEqual(got, V.FAILED)
        self.assertTrue(any("rollover_count" in r for r in reasons))

    def test_missing_job_registered_fails(self):
        got, _ = V.evaluate_post_sample(
            _status(job_registered=False), _baseline(), deadline_passed=False)
        self.assertEqual(got, V.FAILED)

    def test_naive_timestamp_is_not_treated_as_fresh(self):
        """aware datetime 비교 — naive는 문자열로 커 보여도 fresh로 인정하지 않는다."""
        got, _ = V.evaluate_post_sample(
            _status(last_run_at_kst="2026-12-31T23:59:59"),
            _baseline(), deadline_passed=True)
        self.assertEqual(got, V.FAILED)

    def test_string_compare_would_be_wrong_but_datetime_is_right(self):
        """`+09:00` 표기라 문자열 비교였다면 T0(`+00:00`)보다 작게 나오는 케이스."""
        # 15:10+09:00 == 06:10Z > T0(06:00Z) 이지만 문자열로는 "2026-08-17T15..." vs
        # "2026-08-17T06..." 라 우연히 커진다. 반대 방향(진짜 과거)을 확인한다.
        past = _status(last_run_at_kst="2026-08-17T14:59:00+09:00")  # 05:59Z
        self.assertEqual(
            V.evaluate_post_sample(past, _baseline(), deadline_passed=True)[0],
            V.FAILED)


# ---------------------------------------------------------------------------
# Docker event 축
# ---------------------------------------------------------------------------

class TestContainerEvents(unittest.TestCase):

    def _die(self, code="0", cid="old123"):
        return {"Action": "die", "Actor": {"ID": cid, "Attributes": {"exitCode": code}}}

    def test_single_clean_die_passes(self):
        got = V.analyze_container_events([self._die()], "old123")
        self.assertEqual((got.status, got.die_count, got.exit_code), (V.PASS, 1, "0"))

    def test_nonzero_exit_fails(self):
        got = V.analyze_container_events([self._die(code="137")], "old123")
        self.assertEqual(got.status, V.FAILED)

    def test_multiple_dies_are_failed_not_unverified(self):
        """조회·파싱 성공 + 창 안 다중 종료 = 관측된 lifecycle 이상."""
        got = V.analyze_container_events([self._die(), self._die()], "old123")
        self.assertEqual((got.status, got.die_count), (V.FAILED, 2))

    def test_no_die_event_is_unverified(self):
        got = V.analyze_container_events([], "old123")
        self.assertEqual((got.status, got.die_count), (V.UNVERIFIED, 0))

    def test_kill_event_is_not_a_failure(self):
        """compose의 정상 stop도 SIGTERM을 보낸다 — kill 자체는 실패 신호가 아니다."""
        events = [{"Action": "kill", "Actor": {"ID": "old123", "Attributes": {}}},
                  self._die()]
        self.assertEqual(V.analyze_container_events(events, "old123").status, V.PASS)

    def test_other_container_events_ignored(self):
        got = V.analyze_container_events([self._die(cid="other")], "old123")
        self.assertEqual(got.status, V.UNVERIFIED)

    def test_destroy_is_tracked_separately_from_die(self):
        """die는 종료만 증명한다 — 제거까지는 destroy(또는 inspect not-found)가 필요."""
        only_die = V.analyze_container_events([self._die()], "old123")
        self.assertFalse(only_die.destroyed)
        with_destroy = V.analyze_container_events(
            [self._die(), {"Action": "destroy", "Actor": {"ID": "old123"}}], "old123")
        self.assertTrue(with_destroy.destroyed)

    def test_malformed_event_is_unverified(self):
        self.assertEqual(
            V.analyze_container_events(["not a dict"], "old123").status, V.UNVERIFIED)


class TestInspectFailureClassification(unittest.TestCase):
    """`inspect` 실패의 성격 — 제거 증거 vs 관측 불능."""

    def test_no_such_object_is_removal_evidence(self):
        self.assertEqual(
            V.classify_inspect_failure(1, "Error: No such object: old123"), V.PASS)

    def test_daemon_error_is_unverified(self):
        for stderr in ("Cannot connect to the Docker daemon",
                       "permission denied while trying to connect"):
            with self.subTest(stderr=stderr):
                self.assertEqual(V.classify_inspect_failure(1, stderr), V.UNVERIFIED)

    def test_still_present_is_failed(self):
        """제거됐어야 하는데 조회가 성공 = force-recreate가 제거하지 않았다."""
        self.assertEqual(V.classify_inspect_failure(0, ""), V.FAILED)


# ---------------------------------------------------------------------------
# 연속성 3-tuple
# ---------------------------------------------------------------------------

class TestContinuity(unittest.TestCase):

    def _id(self, cid="new1", started="2026-08-17T06:05:00Z", restarts=0):
        return V.ContainerIdentity(cid, started, restarts)

    def test_identical_samples_hold(self):
        self.assertTrue(V.continuity_holds([self._id(), self._id(), self._id()]))

    def test_restart_count_change_breaks_continuity(self):
        """같은 ID로도 `docker restart`가 가능하므로 ID만으로는 부족하다."""
        self.assertFalse(V.continuity_holds([self._id(), self._id(restarts=1)]))

    def test_started_at_change_breaks_continuity(self):
        self.assertFalse(
            V.continuity_holds([self._id(), self._id(started="2026-08-17T06:09:00Z")]))

    def test_empty_samples_do_not_hold(self):
        self.assertFalse(V.continuity_holds([]))


# ---------------------------------------------------------------------------
# 로그 projection
# ---------------------------------------------------------------------------

class TestLogProjection(unittest.TestCase):

    def test_only_allowlist_fields_survive(self):
        line = json.dumps({
            "timestamp": "t", "level": "ERROR", "logger": "app", "message": "boom",
            "exc_info": "Traceback... token=SECRET", "extra": {"uid": "u1"},
        })
        self.assertEqual(V.project_log_line(line),
                         {"timestamp": "t", "level": "ERROR", "logger": "app",
                          "message": "boom"})

    def test_non_json_line_returns_none(self):
        self.assertIsNone(V.project_log_line("plain text log"))

    def test_marker_scan_finds_shutdown_failures(self):
        lines = [
            json.dumps({"level": "WARNING", "message":
                        "[krx_close_snapshot] close timeout (5s), cancel 2 pending tasks"}),
            json.dumps({"level": "INFO", "message": "무관한 로그"}),
        ]
        hits = V.scan_log_markers(lines, V.SHUTDOWN_FAILURE_MARKERS)
        self.assertEqual(len(hits), 1)

    def test_marker_scan_covers_warning_and_info_levels(self):
        """ERROR-only grep이면 WARNING·INFO lifecycle 사건을 전부 놓친다."""
        lines = [
            json.dumps({"level": "WARNING", "message": "[krx] reconcile 점프 의심 ..."}),
            json.dumps({"level": "INFO", "message": "[krx] rollover A/1 → B/2 ..."}),
        ]
        self.assertEqual(len(V.scan_log_markers(lines, V.RUNTIME_FAILURE_MARKERS)), 1)
        self.assertEqual(len(V.scan_log_markers(lines, [V.ROLLOVER_MARKER])), 1)

    def test_korean_marker_matches_escaped_production_line(self):
        """⭐ 운영 로거는 비ASCII를 `\\uXXXX`로 escape한다 (실측 확인).

        raw 문자열 substring으로 훑으면 한글 marker는 **절대 매칭되지 않고**
        조용히 0건이 되어 "오류 없음"으로 오독된다. 디코드 후 매칭을 잠근다.
        """
        escaped = json.dumps(
            {"level": "WARNING", "logger": "app.scheduler",
             "message": "[krx] reconcile 점프 의심 A75605/202605 → A75607/202607 — 보류"},
            ensure_ascii=True,  # 운영 로거와 동일
        )
        self.assertNotIn("점프 의심", escaped)  # raw에는 없다
        hits = V.scan_log_markers([escaped], V.RUNTIME_FAILURE_MARKERS)
        self.assertEqual(len(hits), 1)
        self.assertIn("점프 의심", hits[0]["message"])

    def test_ascii_only_marker_still_matches(self):
        """영문 marker는 escape 영향이 없다 — 회귀 방지용 대조군."""
        line = json.dumps({"level": "ERROR",
                           "message": "[krx] KisFuturesClient task crashed"})
        self.assertEqual(len(V.scan_log_markers([line], V.RUNTIME_FAILURE_MARKERS)), 1)


# ---------------------------------------------------------------------------
# Baseline / artifact
# ---------------------------------------------------------------------------

class TestBaseline(unittest.TestCase):

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "baseline.json"
            path.write_text(_baseline().to_json(), encoding="utf-8")
            self.assertEqual(V.Baseline.load(path), _baseline())

    def test_missing_file_is_collector_error_not_silent_default(self):
        """baseline이 없으면 현재 시각으로 대체하지 않는다."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CollectorErrorAlias):
                V.Baseline.load(pathlib.Path(tmp) / "absent.json")

    def test_missing_field_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "b.json"
            path.write_text(json.dumps({"t0_iso": _T0}), encoding="utf-8")
            with self.assertRaises(CollectorErrorAlias):
                V.Baseline.load(path)


CollectorErrorAlias = V.CollectorError


class TestEvidenceArtifact(unittest.TestCase):

    def test_records_are_written_and_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "ev.jsonl"
            art = V.EvidenceArtifact(path)
            art.append("sample", {"verdict": V.PASS})
            digest = art.finalize({"verdict": V.PASS})
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)  # sample + terminal
            self.assertEqual(json.loads(lines[-1])["kind"], "terminal")
            self.assertTrue((path.parent / (path.name + ".sha256")).exists())
            self.assertEqual(len(digest), 64)

    def test_existing_path_is_refused(self):
        """같은 이름이 이미 있으면 이전 실행 산출물이다 — 덮지 않는다."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "ev.jsonl"
            V.EvidenceArtifact(path)
            with self.assertRaises(FileExistsError):
                V.EvidenceArtifact(path)

    def test_cap_reserves_terminal_slot_and_marks_unverified(self):
        """cap 도달 후에도 terminal record가 기록되고 판정이 UNVERIFIED로 낮아진다.

        재사용 대상 primitive(`MetricsArtifact`)는 cap 도달 시 marker조차 조용히
        버린다 — 그러면 절단 사실이 산출물에 안 남는다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "ev.jsonl"
            art = V.EvidenceArtifact(path)
            art.records = V.MAX_ARTIFACT_RECORDS - 1  # 정상 record 한도 도달
            self.assertFalse(art.append("sample", {"v": 1}))
            self.assertTrue(art.truncated)
            art.finalize({"verdict": V.PASS})
            terminal = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertTrue(terminal["truncated"])
            self.assertEqual(terminal["verdict"], V.UNVERIFIED)


# ---------------------------------------------------------------------------
# status projection — jq 경로 회귀
# ---------------------------------------------------------------------------

class TestStatusProjection(unittest.TestCase):

    def test_reads_reconcile_subtree_not_top_level(self):
        """7필드는 최상위가 아니라 `.reconcile` 아래다.

        최상위에서 뽑으면 all-null 객체를 만들고도 성공한다 — 인증 오류 JSON이
        '빈 스냅샷'으로 둔갑하는 경로와 결합하면 이중으로 조용히 실패한다.
        """
        payload = {
            "enabled": True, "started": True,
            "reconcile": {"job_registered": True, "last_result": "no_op",
                          "rollover_count": 0},
            "client": {"contract": _contract(),
                       "lifecycle": {"started_at_kst": "2026-08-17T15:05:00+09:00"}},
        }
        got = V.project_status(payload)
        self.assertEqual(got["last_result"], "no_op")
        self.assertEqual(got["client_contract"], _contract())
        self.assertEqual(got["client_started_at_kst"], "2026-08-17T15:05:00+09:00")

    def test_client_absent_shape_does_not_crash(self):
        """`started=false`면 `.client`가 없다 — shape 분기."""
        got = V.project_status({"enabled": True, "started": False,
                                "reconcile": {"job_registered": True}})
        self.assertIsNone(got["client_contract"])
        self.assertFalse(got["started"])


class TestLogWindowFiltering(unittest.TestCase):
    """창 분할이 **timestamp로 실제 동작**하는가.

    어댑터가 창을 흉내 내면(구 sentinel 방식) 운영 배선이 since/until을 무시해도
    테스트가 통과한다 — codex 리뷰가 잡은 false PASS 경로다.
    """

    def _line(self, stamp: str, message: str = "x") -> str:
        return json.dumps({"timestamp": stamp, "level": "INFO", "message": message})

    def test_splits_by_timestamp_not_by_caller_hint(self):
        pre = self._line("2026-08-17T06:02:00+00:00", "pre")
        post = self._line("2026-08-17T06:07:00+00:00", "post")
        window = V.filter_log_window([pre, post], _T0, "2026-08-17T06:05:00Z")
        self.assertEqual(len(window.lines), 1)
        self.assertIn("pre", window.lines[0])

    def test_boundary_is_half_open(self):
        """`[since, until)` — until 정각은 제외(다음 창 소속)."""
        exact = self._line("2026-08-17T06:05:00+00:00")
        self.assertEqual(
            len(V.filter_log_window([exact], _T0, "2026-08-17T06:05:00Z").lines), 0)
        self.assertEqual(
            len(V.filter_log_window([exact], "2026-08-17T06:05:00Z",
                                    "2026-08-17T06:06:00Z").lines), 1)

    def test_kst_offset_lines_are_placed_correctly(self):
        """운영 로그는 `+09:00` KST다 — 문자열이 아니라 순간으로 비교해야 한다."""
        kst = self._line("2026-08-17T15:02:00+09:00")  # == 06:02Z
        self.assertEqual(
            len(V.filter_log_window([kst], _T0, "2026-08-17T06:05:00Z").lines), 1)

    def test_unparseable_lines_counted_not_placed(self):
        """어느 창에도 넣지 않는다 — 양쪽에 넣으면 pre 창 rollover가 실패로도 세어진다."""
        window = V.filter_log_window(
            ["plain text", json.dumps({"level": "INFO", "message": "no timestamp"})],
            _T0, "2026-08-17T06:05:00Z")
        self.assertEqual((len(window.lines), window.unparsed), (0, 2))

    def test_unplaceable_counted_once_not_per_window(self):
        """전체 로그를 두 창으로 나누므로, 창별 카운트를 쓰면 **중복 계상**된다.

        배치 불가는 창의 속성이 아니라 입력 전체의 속성이다 (codex 리뷰 지적).
        """
        lines = ["plain text", self._line("2026-08-17T06:02:00+00:00")]
        pre = V.filter_log_window(lines, _T0, "2026-08-17T06:05:00Z")
        post = V.filter_log_window(lines, "2026-08-17T06:05:00Z",
                                   "2026-08-17T06:09:00Z")
        # 창별로 더하면 1건이 2로 부풀어 오른다
        self.assertEqual(pre.unparsed + post.unparsed, 2)
        # 전체 기준 카운터는 1
        self.assertEqual(V.count_unplaceable(lines), 1)

    def test_naive_timestamp_is_unplaceable(self):
        self.assertEqual(
            V.count_unplaceable([self._line("2026-08-17T06:02:00")]), 1)

    def test_naive_boundary_rejected(self):
        with self.assertRaises(V.CollectorError):
            V.filter_log_window([], "2026-08-17T06:00:00", "2026-08-17T06:05:00Z")


class TestArtifactFinalizationEnforced(unittest.TestCase):
    """checksum 이후 내용이 바뀌면 그 checksum이 무의미하다."""

    def test_append_after_finalize_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = V.EvidenceArtifact(pathlib.Path(tmp) / "ev.jsonl")
            art.finalize({"verdict": V.PASS})
            with self.assertRaises(RuntimeError):
                art.append("late", {"x": 1})

    def test_double_finalize_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = V.EvidenceArtifact(pathlib.Path(tmp) / "ev.jsonl")
            art.finalize({"verdict": V.PASS})
            with self.assertRaises(RuntimeError):
                art.finalize({"verdict": V.FAILED})


# ---------------------------------------------------------------------------
# Orchestration — 전 IO 주입, 실시간·Docker 의존 0
# ---------------------------------------------------------------------------

class _Clock:
    """monotonic하게 전진하는 가짜 시계. sleep이 시간을 밀어 준다."""

    def __init__(self, start: datetime):
        self.now_value = start

    def now(self) -> datetime:
        return self.now_value

    async def sleep(self, seconds: float) -> None:
        self.now_value = self.now_value + timedelta(seconds=seconds)


def _adapters(clock, *, statuses, identity_raw="new1|2026-08-17T06:05:00Z|0",
              events=None, shutdown_lines=None, runtime_lines=None,
              removal_error="Error: No such object: old123"):
    """scripted 어댑터. `statuses`의 각 원소는 dict(payload) 또는 예외."""
    queue = list(statuses)

    async def admin_fetch(_path):
        item = queue.pop(0) if queue else queue_last[0]
        queue_last[0] = item
        if isinstance(item, Exception):
            raise item
        return item

    queue_last = [statuses[-1] if statuses else {}]

    async def inspect_format(fmt):
        if fmt.startswith("__probe__"):
            raise V.CollectorError(removal_error)
        return identity_raw

    async def container_events(_cid, _since, _until):
        return events if events is not None else [
            {"Action": "die",
             "Actor": {"ID": "old123", "Attributes": {"exitCode": "0"}}},
            {"Action": "destroy", "Actor": {"ID": "old123"}},
        ]

    async def read_log_lines(_since, _until):
        # 운영 어댑터와 동일하게 **전체 로그**를 돌려준다. 창 분할은
        # `filter_log_window`가 timestamp로 한다 — 어댑터가 창을 흉내 내면
        # 운영 배선 결함(since/until 무시)을 테스트가 못 잡는다.
        return list(shutdown_lines or []) + list(runtime_lines or [])

    return V.Adapters(admin_fetch=admin_fetch, inspect_format=inspect_format,
                      container_events=container_events,
                      read_log_lines=read_log_lines,
                      now=clock.now, sleep=clock.sleep)


def _payload(**over) -> dict:
    st = _status(**over)
    return {
        "enabled": st["enabled"], "started": st["started"],
        "reconcile": {
            "job_registered": st["job_registered"],
            "last_run_at_kst": st["last_run_at_kst"],
            "last_result": st["last_result"],
            "last_current_contract": st["last_current_contract"],
            "last_resolved_contract": st["last_resolved_contract"],
            "rollover_count": st["rollover_count"],
        },
        "client": {"contract": st["client_contract"],
                   "lifecycle": {"started_at_kst": "2026-08-17T15:05:00+09:00"}},
    }


class TestRunVerify(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.clock = _Clock(datetime(2026, 8, 17, 6, 6, tzinfo=timezone.utc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _artifact(self):
        return V.EvidenceArtifact(pathlib.Path(self.tmp.name) / "ev.jsonl")

    async def test_happy_path_passes_and_finalizes(self):
        ad = _adapters(self.clock, statuses=[_payload()])
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS)
        self.assertEqual(len(got["sha256"]), 64)

    async def test_transient_fetch_failure_then_pass(self):
        """과도기 조회 실패는 PENDING으로 기록되고 이후 정상 샘플로 해소된다."""
        ad = _adapters(self.clock, statuses=[
            V.CollectorError("connection refused"), _payload()])
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS)
        self.assertIn(V.PENDING, got["samples"])

    async def test_no_valid_sample_by_startup_deadline_is_unverified(self):
        """첫 응답이 영원히 안 와도 종료한다 — startup deadline이 필요한 이유."""
        ad = _adapters(self.clock, statuses=[V.CollectorError("refused")] * 200)
        got = await V.run_verify(
            _baseline(), ad, self._artifact(),
            deadlines=V.Deadlines(startup_seconds=120, verify_seconds=120,
                                  poll_seconds=45))
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_transient_state_fails_after_verify_deadline(self):
        ad = _adapters(self.clock,
                       statuses=[_payload(last_result="bootstrap_skipped")] * 200)
        got = await V.run_verify(
            _baseline(), ad, self._artifact(),
            deadlines=V.Deadlines(startup_seconds=600, verify_seconds=90,
                                  poll_seconds=45))
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_mid_window_restart_breaks_continuity(self):
        """검증 창 중간 재시작이면 rollover_count==0을 신뢰할 수 없다."""
        clock = self.clock
        seq = [_payload(last_result="bootstrap_skipped"), _payload()]
        ids = ["new1|2026-08-17T06:05:00Z|0", "new1|2026-08-17T06:05:00Z|1"]
        idx = {"i": 0}

        base_ad = _adapters(clock, statuses=seq)

        async def inspect_format(fmt):
            if fmt.startswith("__probe__"):
                raise V.CollectorError("Error: No such object: old123")
            value = ids[min(idx["i"], len(ids) - 1)]
            idx["i"] += 1
            return value

        ad = V.Adapters(admin_fetch=base_ad.admin_fetch, inspect_format=inspect_format,
                        container_events=base_ad.container_events,
                        read_log_lines=base_ad.read_log_lines,
                        now=clock.now, sleep=clock.sleep)
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("identity 변경" in r for r in got["reasons"]))

    async def test_nonzero_die_exit_fails(self):
        ad = _adapters(self.clock, statuses=[_payload()], events=[
            {"Action": "die",
             "Actor": {"ID": "old123", "Attributes": {"exitCode": "137"}}}])
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_missing_die_event_is_unverified_not_pass(self):
        """marker/이벤트 부재를 '정상'으로 기록하지 않는다."""
        ad = _adapters(self.clock, statuses=[_payload()], events=[])
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_shutdown_marker_in_pre_window_fails(self):
        line = json.dumps({"timestamp": "2026-08-17T06:02:00+00:00", "level": "WARNING",
                           "message":
                           "[krx_close_snapshot] close timeout (5s), cancel 1 pending tasks"})
        ad = _adapters(self.clock, statuses=[_payload()], shutdown_lines=[line])
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_pre_window_rollover_is_evidence_not_failure(self):
        """T0~StartedAt 구간의 구 프로세스 rollover는 정당한 관찰 자료다.

        한 창으로 합치면 배포가 07:00 직후일 때 이 rollover까지 FAILED로 오판한다.
        """
        line = json.dumps({"timestamp": "2026-08-17T06:02:00+00:00", "level": "INFO",
                           "message":
                           "[krx] rollover A75608/202608 → A75609/202609 reason=scheduled"})
        ad = _adapters(self.clock, statuses=[_payload()], shutdown_lines=[line])
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS)

    async def test_post_window_rollover_fails(self):
        line = json.dumps({"timestamp": "2026-08-17T06:05:30+00:00", "level": "INFO",
                           "message":
                           "[krx] rollover A75609/202609 → A75610/202610 reason=scheduled"})
        ad = _adapters(self.clock, statuses=[_payload()], runtime_lines=[line])
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_log_fetch_failure_is_unverified(self):
        base_ad = _adapters(self.clock, statuses=[_payload()])

        async def failing_logs(_s, _u):
            raise V.CollectorError("log read failed")

        ad = V.Adapters(admin_fetch=base_ad.admin_fetch,
                        inspect_format=base_ad.inspect_format,
                        container_events=base_ad.container_events,
                        read_log_lines=failing_logs,
                        now=self.clock.now, sleep=self.clock.sleep)
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_new_id_equal_to_old_fails(self):
        """재생성되지 않았으면 배포 자체가 반영되지 않았다."""
        ad = _adapters(self.clock, statuses=[_payload()],
                       identity_raw="old123|2026-08-15T00:00:00Z|0")
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("구 ID와 동일" in r for r in got["reasons"]))

    async def test_log_record_has_single_unparsed_total(self):
        """artifact 레코드를 직접 읽어 per-window `unparsed` 부재 + 전체 1건을 확인."""
        art = self._artifact()
        lines = [
            "plain text",  # timestamp 없음 → 어느 창에도 배치 불가
            json.dumps({"timestamp": "2026-08-17T06:02:00+00:00",
                        "level": "INFO", "message": "pre-window"}),
        ]
        ad = _adapters(self.clock, statuses=[_payload()], shutdown_lines=lines)
        await V.run_verify(_baseline(), ad, art)
        records = [json.loads(x) for x in
                   art.path.read_text(encoding="utf-8").strip().splitlines()]
        log_rec = next(r for r in records if r["kind"] == "logs")
        self.assertEqual(log_rec["unparsed_total"], 1)
        self.assertNotIn("unparsed", log_rec["window_pre"])
        self.assertNotIn("unparsed", log_rec["window_post"])
        self.assertIn("until", log_rec["window_post"])

    async def test_removal_probe_is_recorded_in_artifact(self):
        """probe 결과가 증거로 남는가 — 최종 PASS인데 'destroy 없음'만 남으면
        그걸 보강한 근거가 빠져 감사 계약이 불완전하다 (codex 리뷰 지적)."""
        art = self._artifact()
        ad = _adapters(self.clock, statuses=[_payload()], events=[
            {"Action": "die",
             "Actor": {"ID": "old123", "Attributes": {"exitCode": "0"}}}])  # destroy 없음
        got = await V.run_verify(_baseline(), ad, art)
        self.assertEqual(got["verdict"], V.PASS)
        records = [json.loads(x) for x in
                   art.path.read_text(encoding="utf-8").strip().splitlines()]
        shutdown = next(r for r in records if r["kind"] == "shutdown")
        self.assertEqual(shutdown["removal_probe"], V.PASS)
        self.assertTrue(shutdown["destroyed"])  # probe로 보강된 상태가 반영됨
        self.assertIn("not-found", shutdown["detail"])

    async def test_die_ok_but_not_removed_and_probe_says_present(self):
        """die만으로는 제거를 증명하지 못한다."""
        base_ad = _adapters(self.clock, statuses=[_payload()],
                            events=[{"Action": "die", "Actor": {
                                "ID": "old123", "Attributes": {"exitCode": "0"}}}])

        async def inspect_present(fmt):
            if fmt.startswith("__probe__"):
                return "still-there"  # 조회 성공 = 아직 존재
            return "new1|2026-08-17T06:05:00Z|0"

        ad = V.Adapters(admin_fetch=base_ad.admin_fetch,
                        inspect_format=inspect_present,
                        container_events=base_ad.container_events,
                        read_log_lines=base_ad.read_log_lines,
                        now=self.clock.now, sleep=self.clock.sleep)
        got = await V.run_verify(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)


if __name__ == "__main__":
    unittest.main()
