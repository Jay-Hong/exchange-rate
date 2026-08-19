"""scripts/krx_deploy_verifier.py 단위 테스트 — Docker·실시간·네트워크 의존 0.

전부 순수 함수 또는 주입된 어댑터로 검증한다. `logs/app.log` 쓰기나 임시
디렉터리 의존도 없다(artifact는 `tmp_path` 아래에만 만든다).

실행:
    python -m pytest tests/test_krx_deploy_verifier.py -p no:asyncio -q
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import krx_deploy_verifier as V  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEDULER = REPO_ROOT / "app" / "scheduler.py"
RUNBOOK = REPO_ROOT / "KRX_CANARY.md"
REST_DB_VERIFY = REPO_ROOT / "ops" / "verify-rest-db-deploy.sh"

_T0 = "2026-08-17T06:00:00+00:00"


_FP = "f" * 64


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
        expected_revision="a" * 40,
        expected_source_sha256=_FP,
        service_baseline={"kb.usd-krw": _T0, "nh.usd-krw": _T0},
        usdt_baseline={"upbit": "2026-08-17T14:59:00+09:00",
                       "bithumb": "2026-08-17T14:59:30+09:00"},
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


class TestPrepareFailsClosed(unittest.IsolatedAsyncioTestCase):
    """재생성 뒤가 아니라 baseline 을 다시 얻을 수 있는 prepare 에서 차단한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.baseline = pathlib.Path(self.tmp.name) / "baseline.json"
        self.args = SimpleNamespace(
            repo_root=str(REPO_ROOT),
            expect_revision="a" * 40,
            expect_code="A75609",
            expect_month="202609",
            expect_expires_on="2026-09-21",
            baseline=str(self.baseline),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _common_patches(self):
        return (
            mock.patch.object(V, "_resolve_revision", return_value="a" * 40),
            mock.patch.object(V, "compute_local_source_fingerprint", return_value=_FP),
            mock.patch.object(
                V, "run_command",
                new=mock.AsyncMock(return_value="old123|2026-08-15T00:00:00Z|0|sha256:aaa"),
            ),
            mock.patch.object(V, "production_adapters", return_value=object()),
            mock.patch.object(V, "_utc_now_iso", return_value=_T0),
        )

    async def test_service_baseline_failure_leaves_no_artifact(self):
        patches = self._common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                mock.patch.object(
                    V, "snapshot_source_freshness",
                    new=mock.AsyncMock(side_effect=V.CollectorError("503")),
                ), mock.patch.object(
                    V, "snapshot_usdt_liveness", new=mock.AsyncMock(),
                ) as usdt:
            with self.assertRaisesRegex(V.CollectorError, "서비스 baseline.*recreate 금지"):
                await V._prepare(self.args)

        usdt.assert_not_awaited()
        self.assertFalse(self.baseline.exists())
        self.assertFalse(pathlib.Path(str(self.baseline) + ".sha256").exists())

    async def test_usdt_baseline_failure_leaves_no_artifact(self):
        patches = self._common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                mock.patch.object(
                    V, "snapshot_source_freshness",
                    new=mock.AsyncMock(return_value={"kb.usd-krw": _T0}),
                ), mock.patch.object(
                    V, "snapshot_usdt_liveness",
                    new=mock.AsyncMock(side_effect=V.CollectorError("503")),
                ):
            with self.assertRaisesRegex(V.CollectorError, "USDT baseline.*recreate 금지"):
                await V._prepare(self.args)

        self.assertFalse(self.baseline.exists())
        self.assertFalse(pathlib.Path(str(self.baseline) + ".sha256").exists())

    async def test_empty_service_baseline_is_not_treated_as_success(self):
        patches = self._common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                mock.patch.object(
                    V, "snapshot_source_freshness",
                    new=mock.AsyncMock(return_value={}),
                ), mock.patch.object(
                    V, "snapshot_usdt_liveness", new=mock.AsyncMock(),
                ) as usdt:
            with self.assertRaisesRegex(V.CollectorError, "서비스 baseline 이 비었다"):
                await V._prepare(self.args)

        usdt.assert_not_awaited()
        self.assertFalse(self.baseline.exists())
        self.assertFalse(pathlib.Path(str(self.baseline) + ".sha256").exists())

    async def test_empty_usdt_baseline_is_not_treated_as_success(self):
        patches = self._common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                mock.patch.object(
                    V, "snapshot_source_freshness",
                    new=mock.AsyncMock(return_value={"kb.usd-krw": _T0}),
                ), mock.patch.object(
                    V, "snapshot_usdt_liveness",
                    new=mock.AsyncMock(return_value={}),
                ):
            with self.assertRaisesRegex(V.CollectorError, "USDT baseline 이 비었다"):
                await V._prepare(self.args)

        self.assertFalse(self.baseline.exists())
        self.assertFalse(pathlib.Path(str(self.baseline) + ".sha256").exists())

    async def test_complete_baselines_are_persisted_with_checksum(self):
        patches = self._common_patches()
        service = {"kb.usd-krw": _T0}
        usdt = {"upbit": "2026-08-17T14:59:00+09:00"}
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                mock.patch.object(
                    V, "snapshot_source_freshness",
                    new=mock.AsyncMock(return_value=service),
                ), mock.patch.object(
                    V, "snapshot_usdt_liveness",
                    new=mock.AsyncMock(return_value=usdt),
                ):
            self.assertEqual(await V._prepare(self.args), 0)

        saved = V.Baseline.load(self.baseline)
        self.assertEqual(saved.service_baseline, service)
        self.assertEqual(saved.usdt_baseline, usdt)


class TestProductionRunbookFailClosed(unittest.TestCase):
    """운영 명령의 안전장치가 설명 문구만 남고 실행 블록에서 빠지지 않는다."""

    @classmethod
    def setUpClass(cls):
        cls.text = RUNBOOK.read_text(encoding="utf-8")
        section = cls.text.index("### 실행 순서")
        fence = cls.text.index("```bash", section) + len("```bash")
        end = cls.text.index("```", fence)
        cls.script = cls.text[fence:end]
        cls.normalized = " ".join(cls.script.split())

    def test_candidate_is_full_sha_and_moving_pull_is_absent(self):
        self.assertIn('${DEPLOY_SHA:?승인받은 40자리 DEPLOY_SHA', self.script)
        self.assertIn('[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]]', self.script)
        self.assertNotIn("DEPLOY_SHA=5a3a6c1", self.script)
        commands = [line.strip() for line in self.script.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        self.assertFalse(any(line.startswith("git pull") or " -- git pull" in line
                             for line in commands), commands)
        self.assertIn('git fetch origin master:refs/remotes/origin/master', self.normalized)
        self.assertIn('git merge --ff-only "$DEPLOY_SHA"', self.normalized)
        self.assertIn('[ "$REMOTE_SHA" != "$DEPLOY_SHA" ]', self.script)
        self.assertIn('[ "$BRANCH" != master ]', self.script)
        self.assertIn('git status --porcelain', self.script)

    def test_all_post_follower_abort_paths_use_the_cleanup_helper(self):
        self.assertIn("stop_follower()", self.script)
        self.assertIn('echo "rc=$rc" > "$D/02-old-container.rc"', self.script)
        self.assertIn("trap on_exit EXIT", self.script)
        self.assertIn("trap - EXIT INT TERM", self.script)
        self.assertIn("FOLLOWER_MUST_LIVE=1", self.script)
        self.assertIn('case " $(jobs -pr) "', self.script)
        self.assertIn("require_follower || exit 1", self.script)
        follower = self.script.index("docker logs -f")
        tail = self.script[follower:]
        self.assertNotIn('kill "$FOLLOWER"', tail)
        self.assertGreaterEqual(tail.count("stop_follower"), 6)

    def test_follower_is_closed_and_bound_to_collect(self):
        collect = self.script.index("scripts/krx_deploy_verifier.py collect")
        stop = self.script.rindex("stop_follower", 0, collect)
        self.assertLess(stop, collect)
        self.assertIn('--old-container-log "$D/02-old-container.log"', self.script)
        self.assertNotIn('follower rc=', self.script)

    def test_dead_follower_is_rejected_by_the_runbook_helper(self):
        start = self.script.index("stop_follower()")
        end = self.script.index("\nverify_final_capture()", start)
        helpers = self.script[start:end]
        with tempfile.TemporaryDirectory() as td:
            result = subprocess.run(
                [
                    "bash", "-c",
                    (
                        'D="$1"; FOLLOWER_MUST_LIVE=1; '
                        + helpers
                        + "\nbash -c 'exit 7' & FOLLOWER=$!; sleep 0.1; "
                        + "require_follower"
                    ),
                    "--", td,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            abort = (pathlib.Path(td) / "ABORT.txt").read_text(encoding="utf-8")
            self.assertIn("follower 조기 종료(rc=7)", abort)

    def test_topic_auth_handoff_is_an_executable_gate(self):
        capture = self.script.index("systemctl start fxi-topic-auth-capture.service")
        artifact = self.script.index('verify_final_capture "$CAPTURE_DIR" "$HANDOFF_T0"')
        disable = self.script.index(
            "systemctl disable --now fxi-topic-auth-capture.timer")
        state = self.script.index("verify_timer_down")
        checkout = self.script.index('git merge --ff-only "$DEPLOY_SHA"')
        self.assertLess(capture, artifact)
        self.assertLess(artifact, disable)
        self.assertLess(disable, checkout)
        self.assertLess(state, checkout)
        self.assertIn('get("status") != "matched"', self.script)
        self.assertIn('get("valid") is not True', self.script)
        self.assertIn('[ "$enabled" = disabled ]', self.script)
        self.assertIn('[ "$active" = inactive ]', self.script)
        self.assertIn('[ "$service" = inactive ]', self.script)

    def test_final_capture_helper_rejects_status_schema_and_checksum_mutations(self):
        start = self.script.index("verify_final_capture()")
        end = self.script.index("\n}\n\nverify_timer_down()", start) + 2
        helper = self.script[start:end]

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            raw_path = root / "20260818T070000Z.raw.json"
            meta_path = root / "20260818T070000Z.meta.json"

            def publish(path: pathlib.Path, body: bytes) -> str:
                path.write_bytes(body)
                digest = hashlib.sha256(body).hexdigest()
                pathlib.Path(str(path) + ".sha256").write_text(
                    f"{digest}  {path.name}\n", encoding="utf-8")
                return digest

            raw_sha = publish(raw_path, b'{"ok": true}\n')
            meta = {
                "captured_at_utc": "2026-08-18T07:00:00Z",
                "window": {"status": "matched"},
                "metric_schema": {"valid": True},
                "artifacts": {"raw_file": raw_path.name, "raw_sha256": raw_sha},
            }

            def run(candidate: dict) -> subprocess.CompletedProcess[str]:
                publish(
                    meta_path,
                    (json.dumps(candidate, sort_keys=True) + "\n").encode("utf-8"),
                )
                return subprocess.run(
                    [
                        "bash", "-c",
                        helper + '\nverify_final_capture "$1" "$2"',
                        "--", str(root), "2026-08-18T06:59:59Z",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )

            self.assertEqual(run(meta).returncode, 0)
            for field, value in (("status", "changed"), ("valid", False)):
                broken = json.loads(json.dumps(meta))
                target = broken["window"] if field == "status" else broken["metric_schema"]
                target[field] = value
                with self.subTest(field=field):
                    self.assertNotEqual(run(broken).returncode, 0)

            self.assertEqual(run(meta).returncode, 0)
            raw_path.write_bytes(b'{"tampered": true}\n')
            result = subprocess.run(
                [
                    "bash", "-c", helper + '\nverify_final_capture "$1" "$2"',
                    "--", str(root), "2026-08-18T06:59:59Z",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_evidence_directory_and_log_boundaries_are_private_and_bounded(self):
        self.assertIn("umask 077", self.script)
        self.assertIn('install -d -m 0700 "$D"', self.script)
        self.assertIn('docker logs -f --since "$T0" --timestamps', self.normalized)
        self.assertIn('docker logs --timestamps --since "$T0"', self.normalized)
        self.assertNotIn('--since "$TS"', self.script)

    def test_recreate_cannot_build_or_touch_dependencies(self):
        self.assertIn(
            "docker compose up -d --pull never --no-build --no-deps "
            "--force-recreate fastapi",
            self.normalized,
        )
        build = self.script.index(
            'docker compose -f docker-compose.yml -f "$D/compose-candidate.yml" build fastapi')
        cutover = self.script.index("cutover_latest || exit 1")
        recreate = self.script.index("if ! run 09-recreate.txt")
        self.assertLess(build, cutover)
        self.assertLess(cutover, recreate)
        self.assertIn('image: "$CANDIDATE_TAG"', self.script)
        self.assertIn('[ "$LATEST_AFTER_BUILD" != "$OLD_IMAGE" ]', self.script)
        self.assertIn("CUTOVER_PENDING=1", self.script)
        self.assertIn('restore_old_image > "$D/auto-rollback-on-exit.txt"', self.script)

    def test_rest_db_smoke_precedes_rollback_disarm_and_collect(self):
        health = self.script.index('run 10-health.txt')
        smoke = self.script.index('bash ops/verify-rest-db-deploy.sh "$D"')
        disarm = self.script.index("CUTOVER_PENDING=0", smoke)
        collect = self.script.index("scripts/krx_deploy_verifier.py collect")
        self.assertLess(health, smoke)
        self.assertLess(smoke, disarm)
        self.assertLess(disarm, collect)
        self.assertIn("run 11-rest-db.txt", self.script)

    def test_collect_rc_is_preserved_even_under_errexit(self):
        self.assertIn("if python3 scripts/krx_deploy_verifier.py collect", self.script)
        self.assertIn("then collect_rc=0", self.script)
        self.assertIn("else collect_rc=$?", self.script)
        self.assertIn('echo "rc=$collect_rc"', self.script)

        start = self.script.index("if python3 scripts/krx_deploy_verifier.py collect")
        end = self.script.index("\n\n# 12) follower", start)
        collect = self.script[start:end]
        with tempfile.TemporaryDirectory() as td:
            result = subprocess.run(
                [
                    "bash", "-c",
                    'set -e; D="$1"; cd "$2"; ' + collect + '; echo SURVIVED',
                    "--", td, str(REPO_ROOT),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("SURVIVED", result.stdout)
            outcome = (pathlib.Path(td) / "12-collect.txt").read_text(encoding="utf-8")
            self.assertRegex(outcome, r"rc=[1-9][0-9]*")

    def test_evidence_finalizer_records_success_but_never_auto_approves(self):
        start = self.script.index("finalize_evidence()")
        end = self.script.index("\n# 실패하면 그 자리에서", start)
        helper = self.script[start:end]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "evidence.jsonl").write_text('{"ok": true}\n', encoding="utf-8")
            result = subprocess.run(
                ["bash", "-c", helper + '\nfinalize_evidence "$1" 0 0', "--", td],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            status = (root / "FINAL_STATUS").read_text(encoding="utf-8")
            self.assertIn("status=EVIDENCE_COMPLETE_REVIEW_REQUIRED", status)
            self.assertNotIn("status=PASS", status)
            self.assertIn("verified=true", (root / "manifest.rc").read_text(encoding="utf-8"))
            self._assert_manifest(root, "MANIFEST.sha256")
            self._assert_manifest(root, "CLOSURE.sha256")

    def test_evidence_finalizer_closes_artifacts_but_exits_nonzero_on_findings(self):
        start = self.script.index("finalize_evidence()")
        end = self.script.index("\n# 실패하면 그 자리에서", start)
        helper = self.script[start:end]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "evidence.jsonl").write_text('{"blocked": true}\n', encoding="utf-8")
            result = subprocess.run(
                ["bash", "-c", helper + '\nfinalize_evidence "$1" 7 0', "--", td],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            status = (root / "FINAL_STATUS").read_text(encoding="utf-8")
            self.assertIn("status=DEPLOY_BLOCKED", status)
            self.assertIn("collect_rc=7", status)
            self.assertIn("manifest_rc=0", status)
            self._assert_manifest(root, "MANIFEST.sha256")
            self._assert_manifest(root, "CLOSURE.sha256")

    def test_evidence_finalizer_records_manifest_failure_and_blocks(self):
        start = self.script.index("finalize_evidence()")
        end = self.script.index("\n# 실패하면 그 자리에서", start)
        helper = self.script[start:end]
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            payload = root / "evidence.jsonl"
            payload.write_text('{"ok": true}\n', encoding="utf-8")
            (root / "unsafe-link").symlink_to(payload)
            result = subprocess.run(
                ["bash", "-c", helper + '\nfinalize_evidence "$1" 0 0', "--", td],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            manifest_state = (root / "manifest.rc").read_text(encoding="utf-8")
            self.assertIn("generate_and_verify_rc=1", manifest_state)
            self.assertIn("verified=false", manifest_state)
            status = (root / "FINAL_STATUS").read_text(encoding="utf-8")
            self.assertIn("status=DEPLOY_BLOCKED", status)
            self.assertIn("manifest_rc=1", status)
            self._assert_manifest(root, "CLOSURE.sha256")

    def test_runbook_returns_the_finalizer_status(self):
        self.assertIn('if finalize_evidence "$D" "$collect_rc" "$logs_rc"', self.script)
        self.assertIn('exit "$final_rc"', self.script)
        self.assertIn('status = "DEPLOY_BLOCKED" if blocked else', self.script)
        self.assertIn("EVIDENCE_COMPLETE_REVIEW_REQUIRED", self.script)
        self.assertIn("directory_fd = os.open(path.parent, os.O_RDONLY)", self.script)
        self.assertIn("os.fsync(directory_fd)", self.script)
        self.assertIn("os.close(directory_fd)", self.script)

    def _assert_manifest(self, root: pathlib.Path, name: str) -> None:
        lines = (root / name).read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines, name)
        for line in lines:
            expected, separator, filename = line.partition("  ")
            self.assertEqual(separator, "  ", line)
            target = root / filename
            self.assertTrue(target.is_file(), filename)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), expected)

    def test_maintenance_and_complete_baseline_precede_recreate(self):
        maintenance = self.script.index("ops/install-db-maintenance-cron.sh --check")
        prepare = self.script.index("scripts/krx_deploy_verifier.py prepare")
        recreate = self.script.index("if ! run 09-recreate.txt")
        self.assertLess(maintenance, prepare)
        self.assertLess(prepare, recreate)

    def test_full_image_rollback_is_wired_and_verified(self):
        rollback = self.text[self.text.index("### 전체 이미지 rollback"):]
        normalized = " ".join(rollback.split())
        self.assertIn('source "$D/01-deploy-state.env"', rollback)
        self.assertIn('docker tag "$ROLLBACK_TAG" exchange-rate-fastapi:latest', normalized)
        self.assertIn(
            "docker compose up -d --pull never --no-build --no-deps "
            "--force-recreate fastapi",
            normalized,
        )
        self.assertIn('[ "$ROLLED_IMAGE" = "$OLD_IMAGE" ]', rollback)
        self.assertIn('[ "${HEALTH:-}" = healthy ]', rollback)
        self.assertIn("응급 복원 명령", rollback)
        self.assertIn("rollback drill 성공 증거가 아니다", rollback)
        self.assertIn("REST·DB·KRX 기능 복원을 증명하지 않는다", rollback)
        self.assertIn("NEW_IMAGE", rollback)

    def test_document_matches_usdt_verifier_and_incident_boundary(self):
        self.assertIn("/admin/api/usdt-redis-stats", self.text)
        self.assertNotIn("USDT는 이 도구가 안 덮는다", self.text)
        self.assertIn("2026-08-14 시간봉 오염 재집계·복구 계약", self.text)
        # incident guard 가 편입됐다 — "미포함" 서술이 되살아나면 문서가 기전을 부정한다.
        self.assertNotIn("미커밋 `app/krx_hourly_incidents.py`", self.text)
        self.assertNotIn("append guard가 포함되지 않는다", self.text)
        # 배포 전/후 경계가 **둘 다** 남아야 한다. 배포 전 금지를 지우면 배포까지의
        # 구간이 무방비가 되고, 배포 후 계약을 지우면 기전이 문서에서 사라진다.
        self.assertIn("2026-08-14 08:00 이상 12:00 미만 KST", self.text)
        self.assertIn("`--as-of`로 과거에 이동", self.text)
        self.assertIn("배포 전 — 운영 이미지에 incident guard 없음", self.text)
        self.assertIn("배포 후 — incident guard 포함 이미지", self.text)
        self.assertIn("incident guard 배포만으로는 위 금지를 해제하지 않는다", self.text)
        # 해제는 날짜 추정이 아니라 cleanup 실측으로 판정한다.
        self.assertIn("2026-09-14 03:31:01 KST", self.text)
        self.assertIn("cleanup 성공과 해당 오염 raw tick **0건**을 확인한 뒤 해제한다", self.text)
        # 검증 명령은 컨테이너 안에서 돌아야 한다 — 호스트에는 python/의존성이 없다.
        # 느슨한 존재 확인은 문서 내 다른 docker exec 호출로도 만족한다 — 블록 전체를 잠근다.
        self.assertIn(
            "docker exec exchange-rate-app \\\n"
            "    python /app/scripts/hourly_append_krx_source_hourly_rates.py \\\n"
            "    --window-days 14 --as-of",
            self.text,
        )
        # ls/sha256sum 은 값만 출력한다 — authoritative 는 verifier 지문 게이트다.
        self.assertIn("보조 확인", self.text)
        self.assertNotIn("  python scripts/hourly_append_krx_source_hourly_rates.py", self.text)
        self.assertNotIn("~2026-09-13", self.text)
        # 로그 부재를 배포 증거로 쓰는 거짓 게이트가 다시 들어오지 않도록.
        self.assertIn("로그에 GUARD 라인이 없는 것은 **증거가 아니다**", self.text)
        self.assertNotIn("`--window-days 4`\n이상", self.text)


class TestRestDbDeploySmoke(unittest.TestCase):
    def setUp(self):
        self.script = REST_DB_VERIFY.read_text(encoding="utf-8")

    def _run(self, *, auth="401", online="online|1min|5|10",
             maintenance="maintenance|15min|10|30",
             canceled="57014|1|1", pool="timeout_10s|recovered"):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "docker"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                "previous=''\n"
                "for arg in \"$@\"; do\n"
                "  if [ \"$previous\" = -c ]; then\n"
                "    \"$PYTHON_CHECK\" -c 'import ast,sys; ast.parse(sys.argv[1])' \"$arg\" || exit 8\n"
                "  fi\n"
                "  previous=\"$arg\"\n"
                "done\n"
                "args=\"$*\"\n"
                "case \"$args\" in\n"
                "  *'curl -sS'* ) printf '%s' \"${AUTH}\" ;;\n"
                "  *'DB_WORKLOAD_PROFILE'* )\n"
                "    case \"$args\" in *'compose run'* ) printf '%s\\n' \"${MAINT}\" ;;"
                " * ) printf '%s' \"${ONLINE}\" ;; esac ;;\n"
                "  *'pg_backend_pid'* ) printf '%s' \"${CANCELED}\" ;;\n"
                "  *'held = []'* ) printf '%s' \"${POOL}\" ;;\n"
                "  *'logs fastapi'* ) exit 0 ;;\n"
                "  * ) echo \"unexpected docker call: $args\" >&2; exit 9 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            evidence = root / "evidence"
            env = {
                **dict(os.environ),
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "FXI_REPO_DIR": str(repo),
                "AUTH": auth,
                "ONLINE": online,
                "MAINT": maintenance,
                "CANCELED": canceled,
                "POOL": pool,
                "PYTHON_CHECK": sys.executable,
            }
            result = subprocess.run(
                ["bash", str(REST_DB_VERIFY), str(evidence)],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            output = (evidence / "rest-db.txt").read_text(encoding="utf-8")
            return result, output

    def test_success_requires_all_positive_runtime_signals(self):
        result, output = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FINAL=REST_DB_EVIDENCE_COMPLETE", output)
        self.assertIn("PASS invalid JWT (401)", output)
        self.assertIn("PASS SQLSTATE/same PID/reuse (57014|1|1)", output)

    def test_named_app_or_executor_unavailable_blocks(self):
        result, output = self._run(auth="503")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL invalid JWT expected=401 actual=503", output)
        self.assertIn("FINAL=DEPLOY_BLOCKED", output)

    def test_different_connection_does_not_count_as_recovery(self):
        result, output = self._run(canceled="57014|0|1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL SQLSTATE/same PID/reuse", output)

    def test_runtime_probe_uses_the_container_port_not_the_host_port(self):
        self.assertIn("docker exec exchange-rate-app curl", self.script)
        self.assertIn("http://127.0.0.1:8000/api/notification-settings", self.script)
        self.assertNotIn("curl http://localhost:8000", self.script)


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

class TestSwallowedShutdownFailureVocabulary(unittest.TestCase):
    """graceful marker 순서가 **완전해도** 삼킨 실패는 FAILED 여야 한다.

    `main.py` 의 auth lane 2 지점과 Uvicorn 의 lifespan 실패는 예외를 재전파하지
    않으므로 4개 graceful marker 가 그대로 다 찍힌다. 실패 어휘가 없으면 그 경로가
    조용히 PASS 로 통과한다.
    """

    GOOD = [
        "2026-08-18T11:13:58.4Z INFO:     Shutting down",
        "2026-08-18T11:13:58.5Z INFO:     Waiting for application shutdown.",
        "2026-08-18T11:14:00.7Z INFO:     Application shutdown complete.",
        "2026-08-18T11:14:00.7Z INFO:     Finished server process [7]",
    ]

    def test_clean_sequence_passes(self):
        self.assertEqual(V.analyze_graceful_shutdown_log(self.GOOD).status, V.PASS)

    def test_swallowed_failures_are_caught_despite_complete_markers(self):
        import json as _json
        for marker, line in (
            ("uvicorn lifespan 실패",
             "2026-08-18T11:14:00.7Z ERROR:    Application shutdown failed. Exiting."),
            ("graceful timeout",
             "2026-08-18T11:14:00.7Z ERROR:    Cancel 3 running task(s), "
             "timeout graceful shutdown exceeded"),
            ("auth lane 종료 시작",
             "2026-08-18T11:13:59.0Z " + _json.dumps(
                 {"timestamp": "2026-08-18T20:13:59+09:00", "level": "ERROR",
                  "logger": "exchange_rate.main",
                  "message": "auth lane 종료 시작 실패"}, ensure_ascii=False)),
            ("auth lane 합류",
             "2026-08-18T11:14:00.7Z " + _json.dumps(
                 {"timestamp": "2026-08-18T20:14:00+09:00", "level": "ERROR",
                  "logger": "exchange_rate.main",
                  "message": "auth lane 합류 실패"}, ensure_ascii=False)),
        ):
            with self.subTest(marker=marker):
                got = V.analyze_graceful_shutdown_log(self.GOOD + [line])
                self.assertEqual(got.status, V.FAILED, msg=got.detail)


class TestContainerEvents(unittest.TestCase):

    def _die(self, code="0", cid="old123"):
        return {"Action": "die", "Actor": {"ID": cid, "Attributes": {"exitCode": code}}}

    def test_single_clean_die_passes(self):
        got = V.analyze_container_events([self._die()], "old123")
        self.assertEqual((got.status, got.die_count, got.exit_code), (V.PASS, 1, "0"))

    def test_nonzero_exit_fails(self):
        got = V.analyze_container_events([self._die(code="137")], "old123")
        self.assertEqual(got.status, V.FAILED)

    def test_missing_exit_code_is_unverified_not_failed(self):
        """`exitCode` 속성 부재는 **관측 불능**이다 — 예전엔 `"" != "0"` 으로 FAILED 였다.

        관측 불능을 장애로 승격하면, 종료 방식을 읽지 못한 정상 배포가 차단된다.
        UNVERIFIED 도 후속을 막지만 사람이 원인을 구분할 수 있다.
        """
        got = V.analyze_container_events(
            [{"Action": "die", "Actor": {"ID": "old123", "Attributes": {}}}], "old123")
        self.assertEqual(got.status, V.UNVERIFIED)
        self.assertIsNone(got.exit_code)
        self.assertEqual(got.die_count, 1)

    def test_signal_exit_codes_other_than_sigterm_still_fail(self):
        """143 승격은 `_analyze_shutdown` 이 증거와 결속할 때만이고, 128+N 일반 허용이 아니다."""
        for code in ("137", "139", "129", "1", "3"):
            with self.subTest(code=code):
                got = V.analyze_container_events([self._die(code=code)], "old123")
                self.assertEqual(got.status, V.FAILED)
        # 143 자체도 이 저수준 함수에서는 FAILED — 승격은 상위에서만 일어난다.
        self.assertEqual(
            V.analyze_container_events([self._die(code="143")], "old123").status, V.FAILED)

    def test_143_is_not_accepted_without_correlated_log_evidence(self):
        """이벤트 순수 함수는 143만 보고 정상 종료로 승격하지 않는다."""
        got = V.analyze_container_events([self._die(code="143")], "old123")
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
        self.assertEqual(V.evaluate_continuity([self._id(), self._id(), self._id()])[0], V.PASS)

    def test_restart_count_change_breaks_continuity(self):
        """같은 ID로도 `docker restart`가 가능하므로 ID만으로는 부족하다."""
        self.assertEqual(V.evaluate_continuity([self._id(), self._id(restarts=1)])[0], V.FAILED)

    def test_started_at_change_breaks_continuity(self):
        self.assertEqual(V.evaluate_continuity(
            [self._id(), self._id(started="2026-08-17T06:09:00Z")])[0], V.FAILED)

    def test_empty_samples_do_not_hold(self):
        self.assertEqual(V.evaluate_continuity([])[0], V.UNVERIFIED)


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

    def test_docker_timestamp_prefixed_json_is_projected(self):
        line = (
            '2026-08-18T11:14:03.123456789Z '
            '{"level":"INFO","message":"Application startup complete.",'
            '"extra":{"uid":"secret"}}')
        self.assertEqual(
            V.project_log_line(line),
            {"timestamp": "2026-08-18T11:14:03.123456789Z",
             "level": "INFO", "message": "Application startup complete."})

    def test_docker_timestamp_prefixed_plaintext_is_projected(self):
        line = "2026-08-18T11:14:03.123456789Z INFO: Started server process [7]"
        self.assertEqual(
            V.project_log_line(line),
            {"timestamp": "2026-08-18T11:14:03.123456789Z",
             "message": "INFO: Started server process [7]"})

    def test_lifecycle_markers_must_be_complete_and_ordered(self):
        reversed_lines = list(reversed(_OLD_GRACEFUL_LINES))
        got = V.analyze_graceful_shutdown_log(reversed_lines)
        self.assertEqual(got.status, V.UNVERIFIED)
        self.assertIn("누락/순서", got.detail)

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


class TestProductionLogAdapters(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_log_reader_uses_container_identity_and_exact_window(self):
        target = V.Target(container="candidate-app")
        output = (
            "2026-08-18T11:14:03.123456789Z "
            "INFO: Application startup complete.\n")
        with mock.patch.object(
                V, "run_command", new=mock.AsyncMock(return_value=output)) as run:
            adapters = V.production_adapters(target=target)
            lines = await adapters.read_log_lines(
                "2026-08-18T11:14:02Z", "2026-08-18T11:26:02Z")

        run.assert_awaited_once_with([
            "docker", "logs", "--timestamps",
            "--since", "2026-08-18T11:14:02Z",
            "--until", "2026-08-18T11:26:02Z",
            "candidate-app",
        ])
        self.assertEqual(lines, [output.strip()])

    async def test_old_log_reader_requires_explicit_collect_binding(self):
        adapters = V.production_adapters()
        with self.assertRaisesRegex(V.CollectorError, "경로가 배선되지 않았다"):
            await adapters.read_old_container_log()

    async def test_missing_old_log_does_not_leave_stale_evidence_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            baseline_path = root / "baseline.json"
            body = _baseline().to_json().encode("utf-8")
            baseline_path.write_bytes(body)
            (root / "baseline.json.sha256").write_text(
                f"{hashlib.sha256(body).hexdigest()}  baseline.json\n",
                encoding="utf-8",
            )
            evidence = root / "evidence.jsonl"
            args = SimpleNamespace(
                baseline=str(baseline_path), evidence=str(evidence),
                old_container_log=str(root / "missing.log"),
                startup_deadline_seconds=1, verify_deadline_seconds=1,
                observe_seconds=1, poll_seconds=1,
            )
            with self.assertRaisesRegex(V.CollectorError, "follower 파일 없음"):
                await V._collect(args)
            self.assertFalse(evidence.exists())


# ---------------------------------------------------------------------------
# Baseline / artifact
# ---------------------------------------------------------------------------

class TestExclusiveCreate(unittest.TestCase):
    """baseline·sidecar는 **배타 생성**이어야 한다."""

    def test_creates_with_0600_and_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "b.json"
            V._write_exclusive(path, b"hello")
            self.assertEqual(path.read_bytes(), b"hello")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_refuses_existing_file(self):
        """`exists()` 후 write는 TOCTOU다 — O_EXCL이 그 창을 없앤다."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "b.json"
            V._write_exclusive(path, b"first")
            with self.assertRaises(CollectorErrorAlias):
                V._write_exclusive(path, b"second")
            self.assertEqual(path.read_bytes(), b"first")


class TestResolveRevision(unittest.TestCase):
    """지문은 worktree에서 뜨므로 **HEAD와 clean 여부**가 지문의 전제다."""

    def _repo(self, tmp: str) -> pathlib.Path:
        import subprocess

        root = pathlib.Path(tmp) / "repo"
        (root / "app").mkdir(parents=True)
        (root / "app" / "m.py").write_text("x = 1\n")

        def git(*a):
            subprocess.run(["git", "-C", str(root), *a], check=True,
                           capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        return root

    def test_accepts_matching_head(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            self.assertEqual(V._resolve_revision(root, head), head)
            self.assertEqual(V._resolve_revision(root, head[:12]), head)

    def test_rejects_other_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            with self.assertRaises(CollectorErrorAlias):
                V._resolve_revision(root, "0" * 40)

    def test_rejects_dirty_worktree(self):
        """dirty하면 지문이 배포될 코드와 달라진다 — 기대값으로 못 쓴다."""
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            (root / "app" / "m.py").write_text("x = 2\n")
            with self.assertRaises(CollectorErrorAlias) as ctx:
                V._resolve_revision(root, head)
            self.assertIn("dirty", str(ctx.exception))


class TestSourceFingerprint(unittest.TestCase):

    def test_detects_content_and_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "r"
            (root / "app").mkdir(parents=True)
            (root / "app" / "m.py").write_text("x = 1\n")
            base = V.compute_local_source_fingerprint(root)
            self.assertEqual(V.compute_local_source_fingerprint(root), base)

            (root / "app" / "m.py").write_text("x = 2\n")
            self.assertNotEqual(V.compute_local_source_fingerprint(root), base)

            (root / "app" / "m.py").write_text("x = 1\n")
            self.assertEqual(V.compute_local_source_fingerprint(root), base)
            (root / "app" / "n.py").write_text("y = 1\n")
            self.assertNotEqual(V.compute_local_source_fingerprint(root), base)

    def test_ignores_pycache_and_dockerignored(self):
        """image에는 `.pyc`가 있고 리포에는 없을 수 있다 — 지문이 갈리면 안 된다.

        제외 규칙이 `.dockerignore`보다 좁으면 정상 배포가 불일치로 막힌다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "r"
            (root / "app" / "__pycache__").mkdir(parents=True)
            (root / "app" / "m.py").write_text("x = 1\n")
            base = V.compute_local_source_fingerprint(root)
            (root / "app" / "__pycache__" / "m.cpython-313.pyc").write_bytes(b"\x00")
            # 제외 규칙을 **전수** 확인한다 — 손으로 고른 몇 개만 보면 나중에
            # 규칙이 늘었을 때 그 항목은 검사되지 않는다.
            for i, suffix in enumerate(V._FINGERPRINT_SKIP_SUFFIXES):
                (root / "app" / f"skipped{i}{suffix}").write_text("무시 대상")
            for name in V._FINGERPRINT_SKIP_NAMES:
                (root / "app" / name).write_bytes(b"\x00")
            self.assertEqual(V.compute_local_source_fingerprint(root), base)
        # 비-.py 리소스는 **포함**된다 (templates/static이 그 경로다)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "r"
            (root / "templates").mkdir(parents=True)
            (root / "templates" / "a.html").write_text("<p>1</p>")
            base = V.compute_local_source_fingerprint(root)
            (root / "templates" / "a.html").write_text("<p>2</p>")
            self.assertNotEqual(V.compute_local_source_fingerprint(root), base)

    def test_covers_every_dockerfile_copy_root(self):
        """⭐ 범위가 이미지 COPY 집합 전부인가.

        `app/`만 해싱하면 실제 반례가 생긴다 — 이 리포의 이번 작업이 그랬다:
        변경이 `scripts/`에만 있어 `app/` 지문이 이전 revision과 **같았다**
        (codex 감사 지적). COPY root별로 변화 민감도를 각각 확인한다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "r"
            for name in V.SOURCE_FINGERPRINT_ROOTS:
                (root / name).mkdir(parents=True)
                (root / name / "f.py").write_text("x = 1\n")
            base = V.compute_local_source_fingerprint(root)
            for name in V.SOURCE_FINGERPRINT_ROOTS:
                with self.subTest(root=name):
                    target = root / name / "f.py"
                    target.write_text("x = 2\n")
                    self.assertNotEqual(
                        V.compute_local_source_fingerprint(root), base,
                        f"{name}/ 변경을 지문이 못 잡는다")
                    target.write_text("x = 1\n")
            self.assertEqual(V.compute_local_source_fingerprint(root), base)

    def test_required_skips_cover_bytecode_cache(self):
        """**필수** 제외(bytecode cache)가 실제 제외 목록 안에 있는가.

        컨테이너는 `PYTHONDONTWRITEBYTECODE=1`이라 런타임에 만들지 **않는다**.
        필요한 이유는 반대쪽이다: dockerignore anchoring 때문에 host의
        `app/__pycache__`가 **빌드 시 이미지로 복사**되는데, host 쪽 내용은
        python을 돌릴 때마다 바뀐다. 비교는 "prepare 시점 host ↔ build된
        이미지"라, 그 사이에 pycache가 바뀌면 정상 배포가 불일치로 막힌다.
        """
        for name in V._FINGERPRINT_REQUIRED_SKIP_DIRS:
            self.assertIn(name, V._FINGERPRINT_SKIP_DIRS)
        for suffix in V._FINGERPRINT_REQUIRED_SKIP_SUFFIXES:
            self.assertIn(suffix, V._FINGERPRINT_SKIP_SUFFIXES)

    def test_no_tracked_file_is_excluded(self):
        """⭐ 제외 규칙이 **tracked payload**를 숨기지 않는가.

        dockerignore anchoring 때문에 `scripts/example.md`는 이미지에 들어가는데
        지문에서는 양쪽 다 빠진다 — 그러면 그 파일만 바꾼 revision이 "지문 동일"이
        되어 범위 주장이 우회된다 (codex 감사 지적). 제외는 untracked 부산물에만
        걸려야 한다.

        규칙을 흉내 내지 않고 **지문이 실제로 방문한 경로**와 비교한다 — 흉내가
        규칙과 갈리면 이 테스트가 조용히 통과한다.
        """
        import subprocess

        repo = pathlib.Path(V.__file__).resolve().parent.parent
        proc = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z", *V.SOURCE_FINGERPRINT_ROOTS],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        tracked = {p for p in proc.stdout.split("\0") if p}
        self.assertTrue(tracked, "tracked 파일을 하나도 못 읽었다")

        visited = V.list_fingerprinted_paths(repo)
        hidden = sorted(tracked - visited)
        self.assertEqual(hidden, [], (
            "tracked 파일이 지문에서 빠진다 — 이 파일만 바꾼 revision을 "
            f"구분하지 못한다: {hidden}"))

    def test_dockerignore_has_no_in_root_rules(self):
        """`.dockerignore`가 **COPY root 안을 겨냥하는** 규칙을 얻으면 알린다.

        실측(이 호스트 docker): dockerignore 패턴은 빌드 컨텍스트 **루트에
        고정**되어 하위 디렉터리에 적용되지 않는다 — `*.md`를 넣어도
        `app/README.md`는 이미지에 들어왔다. 그래서 현재 루트 규칙들은
        host/container 갈림을 만들지 않는다.

        하지만 `**/...`이나 `app/...` 형태의 규칙이 추가되면 그때는 진짜
        갈림이 생긴다 — host엔 있고 이미지엔 없어 정상 배포가 FAILED로
        막힌다. 그 시점에 이 테스트가 깨져 제외 목록 갱신을 강제한다.
        """
        repo = pathlib.Path(V.__file__).resolve().parent.parent
        rules = [
            line.strip()
            for line in (repo / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertTrue(rules, ".dockerignore를 읽지 못했다")
        in_root = [
            r for r in rules
            if r.lstrip("!").startswith("**/")
            or any(r.lstrip("!").startswith(f"{root}/")
                   for root in V.SOURCE_FINGERPRINT_ROOTS)
        ]
        self.assertEqual(in_root, [], (
            "`.dockerignore`가 COPY root 안을 겨냥한다 — 이 경로들은 host에만 "
            "있고 이미지엔 없어 지문이 갈린다. 제외 목록에 반영하라: "
            f"{in_root}"))

    def test_roots_match_dockerfile_copy(self):
        """Dockerfile의 COPY 목록과 지문 범위를 **대조**한다.

        Dockerfile에 COPY가 추가됐는데 지문 범위가 그대로면, 그 경로의 변경은
        검증 없이 배포된다. 이 테스트가 그때 깨진다.
        """
        import re

        dockerfile = (pathlib.Path(V.__file__).resolve().parent.parent
                      / "Dockerfile").read_text()
        copied = set(re.findall(r"^COPY\s+(?!--from)(\S+)/\s+\./",
                                dockerfile, re.MULTILINE))
        self.assertEqual(copied, set(V.SOURCE_FINGERPRINT_ROOTS),
                         f"Dockerfile COPY={sorted(copied)} vs "
                         f"지문 범위={sorted(V.SOURCE_FINGERPRINT_ROOTS)}")


class TestBaseline(unittest.TestCase):

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "baseline.json"
            _write_baseline(path, _baseline())
            self.assertEqual(V.Baseline.load(path), _baseline())

    def test_missing_file_is_collector_error_not_silent_default(self):
        """baseline이 없으면 현재 시각으로 대체하지 않는다."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(CollectorErrorAlias):
                V.Baseline.load(pathlib.Path(tmp) / "absent.json")

    def test_tampered_baseline_is_rejected(self):
        """⭐ sidecar checksum을 **검증**한다.

        prepare가 sha256을 남기는데 verify가 확인하지 않으면 그 sha256은 장식이다.
        baseline은 T0·구 identity·기대 계약의 유일한 출처라, 변조되면 게이트
        전체가 잘못된 기준 위에서 돈다 (codex 감사에서 실제 재현됨).
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "b.json"
            _write_baseline(path, _baseline())
            # 내용만 바꾸고 sidecar는 그대로 → 불일치
            path.write_text(_baseline(old_container_id="attacker").to_json(),
                            encoding="utf-8")
            with self.assertRaises(CollectorErrorAlias) as ctx:
                V.Baseline.load(path)
            self.assertIn("checksum", str(ctx.exception))

    def test_missing_sidecar_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "b.json"
            path.write_text(_baseline().to_json(), encoding="utf-8")  # sidecar 없음
            with self.assertRaises(CollectorErrorAlias):
                V.Baseline.load(path)

    def test_missing_field_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "b.json"
            _write_baseline(path, None, raw=json.dumps({"t0_iso": _T0}))
            with self.assertRaises(CollectorErrorAlias):
                V.Baseline.load(path)


CollectorErrorAlias = V.CollectorError


def _write_baseline(path: pathlib.Path, baseline, *, raw: str = None) -> None:
    """baseline + 정상 checksum sidecar를 함께 쓴다 (load가 sidecar를 검증한다)."""
    import hashlib
    text = raw if raw is not None else baseline.to_json()
    path.write_text(text, encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (path.parent / (path.name + ".sha256")).write_text(
        f"{digest}  {path.name}\n", encoding="utf-8")


class TestEvidenceArtifact(unittest.TestCase):

    def test_records_are_written_and_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "ev.jsonl"
            art = V.EvidenceArtifact(path)
            art.append("sample", {"verdict": V.PASS})
            _, digest = art.finalize({"verdict": V.PASS})
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


def _line(stamp: str, level: str = "info", message: str = "x") -> str:
    return json.dumps({"timestamp": stamp, "level": level.upper(),
                       "logger": "app.main", "message": message})


_OLD_GRACEFUL_LINES = [
    _line("2026-08-17T06:03:00+00:00", message="Shutting down"),
    _line("2026-08-17T06:03:01+00:00",
          message="Waiting for application shutdown."),
    _line("2026-08-17T06:03:02+00:00",
          message="Application shutdown complete."),
    _line("2026-08-17T06:03:03+00:00",
          message="Finished server process [7]"),
]

_RUNTIME_START_LINES = [
    _line("2026-08-17T06:05:01+00:00", message="Started server process [7]"),
    _line("2026-08-17T06:05:03+00:00",
          message="Application startup complete."),
]

_RUNTIME_OK_LINE = _line(
    "2026-08-17T06:05:30+00:00", message="startup complete")


def _adapters(clock, *, statuses, identity_raw="new1|2026-08-17T06:05:00Z|0",
              events=None, old_container_lines=None, shutdown_lines=None,
              runtime_lines=None,
              removal_error="Error: No such object: old123",
              new_image="sha256:bbb", health_status="healthy",
              probe=None, source_fp=_FP, final_identity_raw=None,
              rates_count=30, rates_updated_at="2026-08-17T06:06:00+00:00",
              rates_error=None, rates_by_source=None,
              usdt_after=None, usdt_error=None):
    """scripted 어댑터. `statuses`의 각 원소는 dict(payload) 또는 예외.

    실제 게이트 축을 전부 덮는다 — 하나라도 빠지면 orchestration이 그 축에서
    UNVERIFIED를 내므로, 어댑터 누락이 곧 테스트 실패로 드러난다.
    """
    queue = list(statuses)
    queue_last = [statuses[-1] if statuses else {}]
    probe_map = probe if probe is not None else dict(V.EXPIRY_BEHAVIOR_PROBE)

    async def admin_fetch(path):
        if path == "/health":
            return {"status": health_status}
        if path == "/api/rates":
            if rates_error is not None:
                raise rates_error
            if rates_by_source is not None:
                rows = [{"currency": "usd-krw", "bank": b, "rate": 1400.0,
                         "timestamp": t} for b, t in rates_by_source.items()]
            else:
                rows = [{"currency": "usd-krw", "bank": b, "rate": 1400.0,
                         "timestamp": rates_updated_at}
                        for b in ("kb", "nh")] * max(1, rates_count // 2)
                rows = rows[:rates_count] if rates_count else []
            return {
                "rates": rows,
                "metadata": {"updated_at": rates_updated_at,
                             "total_count": len(rows)},
            }
        if path == "/admin/api/dashboard":
            return {"broadcast": {"success_rate": 100.0}, "errors_1h": 0}
        if path == "/admin/api/usdt-redis-stats":
            if usdt_error is not None:
                raise usdt_error
            # 기본값은 **baseline 의 두 소스가 모두 재개**한 정상 상태여야 한다
            # (baseline 과 어긋나면 모든 happy-path 가 USDT 축에서 걸린다)
            after = (usdt_after if usdt_after is not None
                     else {"upbit": "2026-08-17T15:10:00+09:00",
                           "bithumb": "2026-08-17T15:10:30+09:00"})
            return {"per_source": {
                src: {"last_direct_write_success_at": ts}
                for src, ts in after.items()}}
        item = queue.pop(0) if queue else queue_last[0]
        queue_last[0] = item
        if isinstance(item, Exception):
            raise item
        return item

    polling_done = [False]

    async def inspect_format(fmt):
        if fmt.startswith("__probe__"):
            raise V.CollectorError(removal_error)
        if "Image" in fmt:
            return new_image
        if final_identity_raw is not None and polling_done[0]:
            # 폴링이 끝난 뒤(종료 직전) 재확인만 다른 값을 준다. 표본 개수로
            # 판별하면 관측 창 길이가 바뀔 때 조용히 어긋난다.
            return final_identity_raw
        return identity_raw

    async def container_events(_cid, _since, _until):
        return events if events is not None else [
            {"Action": "die",
             "Actor": {"ID": "old123", "Attributes": {"exitCode": "0"}}},
            {"Action": "destroy", "Actor": {"ID": "old123"}},
        ]

    async def read_old_container_log():
        base = (list(old_container_lines) if old_container_lines is not None
                else list(_OLD_GRACEFUL_LINES))
        return base + list(shutdown_lines or [])

    async def read_log_lines(_since, _until):
        # 운영 어댑터와 동일하게 신 컨테이너 전용 로그만 돌려준다.
        if runtime_lines is not None:
            return list(runtime_lines)
        return list(_RUNTIME_START_LINES) + [_RUNTIME_OK_LINE]

    async def expiry_probe(month):
        value = probe_map.get(month)
        if isinstance(value, Exception):
            raise value
        return value

    async def source_fingerprint():
        # 이 축은 폴링이 모두 끝난 뒤에만 호출된다.
        polling_done[0] = True
        if isinstance(source_fp, Exception):
            raise source_fp
        return source_fp

    return V.Adapters(admin_fetch=admin_fetch, inspect_format=inspect_format,
                      container_events=container_events,
                      read_old_container_log=read_old_container_log,
                      read_log_lines=read_log_lines, expiry_probe=expiry_probe,
                      source_fingerprint=source_fingerprint,
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


class TestCollectEvidence(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.clock = _Clock(datetime(2026, 8, 17, 6, 6, tzinfo=timezone.utc))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _artifact(self):
        self._art = V.EvidenceArtifact(pathlib.Path(self.tmp.name) / "ev.jsonl")
        return self._art

    def _records(self, kind: str) -> list[dict]:
        """증거 파일에서 해당 축의 record 를 읽는다 (payload 는 top-level 병합)."""
        out = []
        for line in self._art.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("kind") == kind:
                out.append(rec)
        return out

    async def test_happy_path_passes_and_finalizes(self):
        ad = _adapters(self.clock, statuses=[_payload(), _payload()])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS)
        self.assertEqual(len(got["sha256"]), 64)

    async def test_transient_fetch_failure_then_pass(self):
        """과도기 조회 실패는 PENDING으로 기록되고 이후 정상 샘플로 해소된다."""
        ad = _adapters(self.clock, statuses=[
            V.CollectorError("connection refused"), _payload(), _payload()])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS)
        self.assertIn(V.PENDING, got["samples"])

    async def test_observation_window_is_actually_held_open(self):
        """⭐ runbook의 "12분간 전량 저장"이 코드에서 실제로 일어나는가.

        첫 PASS 2건에서 끝내면 실 관측 창이 ~45초로 줄어 그 뒤의 재시작을
        못 본다 (codex 감사 지적). 표본 수와 **경과 시간**을 함께 본다.
        """
        ad = _adapters(self.clock, statuses=[_payload()])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS)
        # 720s / 45s = 16 interval → 최소 17 표본
        self.assertGreaterEqual(len(got["samples"]), 17,
                                f"관측 창이 닫혔다: {len(got['samples'])} 표본")

    async def test_restart_late_in_window_is_caught(self):
        """창 **후반**의 재시작을 잡는가 — 창이 장식이 아니라는 증거."""
        ids = ["new1|2026-08-17T06:05:00Z|0"] * 12 + [
            "new1|2026-08-17T06:05:00Z|1"]
        idx = {"i": 0}
        base_ad = _adapters(self.clock, statuses=[_payload()])

        async def inspect_format(fmt):
            if fmt.startswith("__probe__"):
                raise V.CollectorError("Error: No such object: old123")
            if "Image" in fmt:
                return "sha256:bbb"
            value = ids[min(idx["i"], len(ids) - 1)]
            idx["i"] += 1
            return value

        ad = dataclasses.replace(base_ad, inspect_format=inspect_format)
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_final_identity_change_after_axes_fails(self):
        """폴링이 끝난 뒤 축 검사 중 재시작 → 앞선 관측이 무효."""
        ad = _adapters(self.clock, statuses=[_payload()],
                       final_identity_raw="new2|2026-08-17T06:30:00Z|0")
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("종료 직전" in r for r in got["reasons"]),
                        got["reasons"])

    async def test_final_restart_count_nonzero_fails(self):
        ad = _adapters(self.clock, statuses=[_payload()],
                       final_identity_raw="new1|2026-08-17T06:05:00Z|2")
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_source_fingerprint_mismatch_fails(self):
        """⭐ calendar fix를 포함하지만 **다른 revision**인 image를 잡는다.

        동작 probe만 있으면 통과한다 — 그것이 이 축이 따로 있는 이유다.
        """
        ad = _adapters(self.clock, statuses=[_payload()], source_fp="9" * 64)
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("소스 지문" in r for r in got["reasons"]), got["reasons"])

    async def test_events_captured_before_observation_window(self):
        """⭐ `docker events`를 **관측 창이 끝난 뒤**에만 조회하면 die 를 놓친다.

        데몬의 이벤트 재생 버퍼는 시간창이 아니라 **전역 FIFO 256건**이다
        (실측: `--since 720h` 를 줘도 정확히 256). 운영 호스트는 헬스체크만으로
        분당 ~30건을 만들어 8~13분이면 버퍼가 한 바퀴 돈다. 그런데 조회는
        `observe_seconds`(720s) 를 채운 **뒤**라 최소 12분 후다 → 배포 시점의
        die/destroy 가 이미 밀려나 정상 배포도 UNVERIFIED 로 차단된다.

        그래서 **첫 정상 응답 직후에 한 번** 잡아 두어야 한다. 여기서는 늦은
        조회가 버퍼 축출로 비어 있어도 이른 조회분으로 판정이 서는지 본다.
        """
        calls = []

        async def evicting_events(_cid, _since, _until):
            calls.append(len(calls))
            if len(calls) == 1:          # 이른 조회 — 아직 버퍼에 있다
                return [
                    {"Action": "die",
                     "Actor": {"ID": "old123", "Attributes": {"exitCode": "0"}}},
                    {"Action": "destroy", "Actor": {"ID": "old123"}},
                ]
            return []                    # 늦은 조회 — 축출됐다

        base = _adapters(self.clock, statuses=[_payload()])
        ad = dataclasses.replace(base, container_events=evicting_events)
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertGreaterEqual(len(calls), 2,
                                "events 를 이른 시점과 늦은 시점에 각각 잡지 않는다")
        self.assertEqual(got["verdict"], V.PASS, got["reasons"])

    async def test_late_destroy_still_counted(self):
        """반대 방향: destroy 가 늦게 오면 **늦은 조회**가 그걸 잡아야 한다."""
        calls = []

        async def late_destroy(_cid, _since, _until):
            calls.append(len(calls))
            if len(calls) == 1:
                return [{"Action": "die",
                         "Actor": {"ID": "old123", "Attributes": {"exitCode": "0"}}}]
            return [{"Action": "destroy", "Actor": {"ID": "old123"}}]

        base = _adapters(self.clock, statuses=[_payload()])
        ad = dataclasses.replace(base, container_events=late_destroy)
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS, got["reasons"])

    async def test_truncated_old_follower_is_unverified(self):
        """구 follower tail이 잘리면 exit 0도 정상 종료로 승인하지 않는다."""
        ad = _adapters(
            self.clock, statuses=[_payload()],
            old_container_lines=_OLD_GRACEFUL_LINES[:-1])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)
        self.assertTrue(any("graceful" in r or "로그" in r for r in got["reasons"]),
                        got["reasons"])

    async def test_missing_runtime_startup_is_unverified(self):
        """신 container 로그에 startup 시작점이 없으면 중간 누락을 배제 못 한다."""
        ad = _adapters(
            self.clock, statuses=[_payload()], runtime_lines=[_RUNTIME_OK_LINE])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_complete_identity_specific_logs_pass(self):
        ad = _adapters(self.clock, statuses=[_payload()])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS, got["reasons"])

    async def test_health_axis_is_reachability_only_not_service_gate(self):
        """⭐ `/health`는 정적 dict라 도달성만 증명한다 — 축 이름·문서가 그래야 한다.

        `app/main.py`의 `/health`는 `{"status": "healthy", ...}`를 **무조건**
        반환한다. 이 축의 PASS를 "FX·USDT·broadcast가 살아있다"로 읽으면 안 된다
        (외부 검토 지적). 코드가 하지 않는 것을 docstring이 약속하면 그게 곧
        false confidence다.
        """
        src = pathlib.Path(V.__file__).read_text()
        i = src.index("async def _verify_reachability")
        doc = src[i:i + 1400]
        self.assertIn("도달성", doc)
        self.assertNotIn("FX 크롤러", doc,
                         "이 축이 하지 않는 일을 docstring이 약속한다")
        # 서비스 실증은 별 축이 담당해야 한다
        self.assertIn("async def _collect_service_evidence", src)

    async def test_observation_time_going_backwards_fails(self):
        """⭐ 관측 시각이 뒤로 가는 것은 **모호하지 않은** 회귀다."""
        base = _baseline(service_baseline={"kb.usd-krw": _T0, "nh.usd-krw": _T0})
        ad = _adapters(self.clock, statuses=[_payload()],
                       rates_by_source={"kb": "2026-08-17T00:00:00+00:00",  # 역행
                                        "nh": "2026-08-17T06:06:00+00:00"})
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("kb.usd-krw" in r and "뒤로" in r for r in got["reasons"]),
                        got["reasons"])

    async def test_no_advance_is_recorded_not_judged(self):
        """⭐ "갱신 없음"은 **판정하지 않는다** — 이 창에서 죽음과 느린 주기가
        구분되지 않기 때문이다(배포+관측 ~15분이면 죽은 소스도 15분치만 낡는다).
        대신 소스별로 기록해 사람이 대조하게 한다.
        """
        stale = "2026-08-14T06:00:00+00:00"    # 휴장 소스: 그대로 멈춰 있다
        base = _baseline(service_baseline={"sc.usd-krw": stale, "nh.usd-krw": _T0})
        ad = _adapters(self.clock, statuses=[_payload()],
                       rates_by_source={"sc": stale,
                                        "nh": "2026-08-17T06:06:00+00:00"})
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS, got["reasons"])
        per = self._records("service")[-1]["per_source"]
        self.assertFalse(per["sc.usd-krw"]["advanced"])
        self.assertEqual(per["sc.usd-krw"]["verdict"], V.OBSERVED)
        self.assertTrue(per["nh.usd-krw"]["advanced"])

    async def test_usdt_source_dead_after_restart_fails(self):
        """⭐ `--force-recreate` 는 24/7 USDT WS 도 함께 재시작한다.

        `/api/rates` 는 legacy 정책상 USDT 를 **제외**하므로 FX 축이 못 본다.
        USDT 는 쉬는 시간이 없으므로 "재시작 후 관측 창 내내 write 0" 은
        모호하지 않은 실패다 (외부 검토 지적).
        """
        ad = _adapters(self.clock, statuses=[_payload()],
                       usdt_after={"upbit": "2026-08-17T15:06:00+09:00",
                                   "bithumb": None})       # 재시작 후 write 0
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("bithumb" in r for r in got["reasons"]), got["reasons"])

    async def test_usdt_source_present_but_stale_timestamp_fails(self):
        """키는 있는데 성공 시각이 **재시작 이전**이면 재개하지 못한 것이다.

        (키 자체가 없는 경우와 **다른 분기**다 — 둘 다 잡아야 한다.)
        """
        base = _baseline(usdt_baseline={"upbit": "2026-08-17T15:05:00+09:00"})
        ad = _adapters(self.clock, statuses=[_payload()],
                       usdt_after={"upbit": "2026-08-17T15:04:00+09:00"})
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("upbit" in r and "재시작 후" in r
                            for r in got["reasons"]), got["reasons"])

    async def test_usdt_active_source_disappearing_fails(self):
        """⭐ 재시작 후 **키 자체가 사라진** 소스를 잡는다.

        `per_source` 는 lazy populate 다(`app/usdt_redis_stats.py`) — write 가
        한 번도 없으면 그 source 는 응답에 **아예 나타나지 않는다**. 그러니
        `after` 만 순회하면 가장 현실적인 실패 경로를 통째로 건너뛴다
        (외부 검토 지적 + 직접 재현).
        """
        base = _baseline(usdt_baseline={"upbit": "2026-08-17T15:05:00+09:00",
                                        "bithumb": "2026-08-17T15:05:30+09:00"})
        ad = _adapters(self.clock, statuses=[_payload()],
                       usdt_after={"upbit": "2026-08-17T15:10:00+09:00"})
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("bithumb" in r for r in got["reasons"]), got["reasons"])

    async def test_usdt_baseline_missing_is_unverified(self):
        """⭐ 배포 전 스냅샷이 없으면 **증거 부재**다 — PASS 로 넘기지 않는다.

        전 판은 baseline=None 을 "모든 after 소스가 배포 전 활성"으로 읽어,
        한 소스만 재개해도 PASS 였다.
        """
        base = _baseline(usdt_baseline=None)
        ad = _adapters(self.clock, statuses=[_payload()],
                       usdt_after={"upbit": "2026-08-17T15:10:00+09:00"})
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)
        self.assertTrue(any("USDT" in r or "usdt" in r for r in got["reasons"]),
                        got["reasons"])

    async def test_usdt_unparseable_baseline_is_unverified_not_inactive(self):
        """읽을 수 없는 baseline 시각을 '비활성'으로 접으면 판정이 조용히 사라진다."""
        base = _baseline(usdt_baseline={"upbit": "not-a-timestamp"})
        ad = _adapters(self.clock, statuses=[_payload()], usdt_after={})
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_usdt_inactive_before_deploy_is_not_judged(self):
        """배포 **전에도** 죽어 있던 소스는 이 배포의 책임이 아니다."""
        base = _baseline(usdt_baseline={"upbit": "2026-08-17T14:59:00+09:00",
                                        "gopax": "2026-08-10T00:00:00+09:00"})
        ad = _adapters(self.clock, statuses=[_payload()],
                       usdt_after={"upbit": "2026-08-17T15:06:00+09:00",
                                   "gopax": None})
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS, got["reasons"])

    async def test_usdt_stats_failure_is_unverified(self):
        ad = _adapters(self.clock, statuses=[_payload()],
                       usdt_error=V.CollectorError("503"))
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_key_is_bank_and_currency_not_bank_alone(self):
        """⭐ 통화 하나가 사라져도 잡아야 한다.

        키를 bank 로만 잡으면 운영 30행이 10키로 뭉개져 `kb.eur` 소실을
        `kb.usd` 가 가린다 — max(timestamp) 를 소스별로 옮겼을 뿐이 된다
        (외부 검토 지적).
        """
        base = _baseline(service_baseline={"kb.usd-krw": _T0, "kb.eur-krw": _T0})

        async def rates_two_currencies(path):
            if path == "/health":
                return {"status": "healthy"}
            if path == "/api/rates":
                return {"rates": [{"bank": "kb", "currency": "usd-krw",
                                   "timestamp": "2026-08-17T06:06:00+00:00"}]}
            if path == "/admin/api/dashboard":
                return {"broadcast": {}, "errors_1h": 0}
            return {}

        ad = dataclasses.replace(_adapters(self.clock, statuses=[_payload()]),
                                 admin_fetch=rates_two_currencies)
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("kb.eur-krw" in r for r in got["reasons"]),
                        got["reasons"])

    async def test_every_baseline_source_is_recorded(self):
        """`max(timestamp)` 로 접지 않는다 — **소스마다** 기록이 남아야 한다."""
        base = _baseline(service_baseline={"kb.usd-krw": _T0, "nh.usd-krw": _T0, "sc.usd-krw": _T0})
        ad = _adapters(self.clock, statuses=[_payload()],
                       rates_by_source={"kb": "2026-08-17T06:06:00+00:00",
                                        "nh": "2026-08-17T06:06:00+00:00",
                                        "sc": _T0})
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS, got["reasons"])
        self.assertEqual(set(self._records("service")[-1]["per_source"]),
                         {"kb.usd-krw", "nh.usd-krw", "sc.usd-krw"})

    async def test_source_disappearing_from_response_fails(self):
        """baseline 에 있던 소스가 응답에서 **사라지면** 회귀다."""
        base = _baseline(service_baseline={"kb.usd-krw": _T0, "nh.usd-krw": _T0})
        ad = _adapters(self.clock, statuses=[_payload()],
                       rates_by_source={"nh": "2026-08-17T06:06:00+00:00"})
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_service_baseline_missing_is_unverified(self):
        """baseline 스냅샷이 없으면 비교할 기준이 없다 — PASS 로 넘기지 않는다."""
        base = _baseline(service_baseline=None)
        ad = _adapters(self.clock, statuses=[_payload()])
        got = await V.collect_evidence(base, ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_service_evidence_empty_rates_is_regression_not_unverified(self):
        """빈 응답은 관측 불능이 아니라 **baseline 소스 전부 소실** = 회귀다."""
        ad = _adapters(self.clock, statuses=[_payload()], rates_by_source={})
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("사라졌다" in r for r in got["reasons"]), got["reasons"])

    async def test_service_evidence_fetch_failure_is_unverified(self):
        ad = _adapters(self.clock, statuses=[_payload()],
                       rates_error=V.CollectorError("503"))
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_source_fingerprint_failure_is_unverified(self):
        ad = _adapters(self.clock, statuses=[_payload()],
                       source_fp=V.CollectorError("exec 실패"))
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_no_valid_sample_by_startup_deadline_is_unverified(self):
        """첫 응답이 영원히 안 와도 종료한다 — startup deadline이 필요한 이유."""
        ad = _adapters(self.clock, statuses=[V.CollectorError("refused")] * 200)
        got = await V.collect_evidence(
            _baseline(), ad, self._artifact(),
            deadlines=V.Deadlines(startup_seconds=120, verify_seconds=120,
                                  poll_seconds=45))
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_transient_state_fails_after_verify_deadline(self):
        ad = _adapters(self.clock,
                       statuses=[_payload(last_result="bootstrap_skipped")] * 200)
        got = await V.collect_evidence(
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

        ad = dataclasses.replace(base_ad, inspect_format=inspect_format)
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("identity 변경" in r for r in got["reasons"]))

    async def test_nonzero_die_exit_fails(self):
        ad = _adapters(self.clock, statuses=[_payload()], events=[
            {"Action": "die",
             "Actor": {"ID": "old123", "Attributes": {"exitCode": "137"}}}])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_sigterm_143_with_destroy_and_graceful_log_passes(self):
        """Compose SIGTERM 143은 세 증거가 결속될 때만 정상으로 승격한다."""
        ad = _adapters(self.clock, statuses=[_payload()], events=[
            {"Action": "die", "Actor": {
                "ID": "old123", "Attributes": {"exitCode": "143"}}},
            {"Action": "destroy", "Actor": {"ID": "old123"}},
        ])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS, got["reasons"])

    async def test_sigterm_143_without_complete_graceful_log_is_unverified(self):
        ad = _adapters(
            self.clock, statuses=[_payload()],
            events=[
                {"Action": "die", "Actor": {
                    "ID": "old123", "Attributes": {"exitCode": "143"}}},
                {"Action": "destroy", "Actor": {"ID": "old123"}},
            ],
            old_container_lines=_OLD_GRACEFUL_LINES[:-1],
        )
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_sigterm_143_with_shutdown_failure_marker_fails(self):
        failure = _line(
            "2026-08-17T06:02:00+00:00", "warning",
            "[krx_close_snapshot] close timeout (5s), cancel 1 pending tasks")
        ad = _adapters(
            self.clock, statuses=[_payload()],
            events=[
                {"Action": "die", "Actor": {
                    "ID": "old123", "Attributes": {"exitCode": "143"}}},
                {"Action": "destroy", "Actor": {"ID": "old123"}},
            ],
            shutdown_lines=[failure],
        )
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_sigterm_143_without_removal_evidence_fails(self):
        base = _adapters(self.clock, statuses=[_payload()], events=[
            {"Action": "die", "Actor": {
                "ID": "old123", "Attributes": {"exitCode": "143"}}},
        ])

        async def inspect_present(fmt):
            if fmt.startswith("__probe__"):
                return "still-there"
            if "Image" in fmt:
                return "sha256:bbb"
            return "new1|2026-08-17T06:05:00Z|0"

        got = await V.collect_evidence(
            _baseline(), dataclasses.replace(base, inspect_format=inspect_present),
            self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_missing_die_event_is_unverified_not_pass(self):
        """marker/이벤트 부재를 '정상'으로 기록하지 않는다."""
        ad = _adapters(self.clock, statuses=[_payload()], events=[])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_shutdown_marker_in_pre_window_fails(self):
        line = json.dumps({"timestamp": "2026-08-17T06:02:00+00:00", "level": "WARNING",
                           "message":
                           "[krx_close_snapshot] close timeout (5s), cancel 1 pending tasks"})
        ad = _adapters(self.clock, statuses=[_payload()], shutdown_lines=[line])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_pre_window_rollover_is_evidence_not_failure(self):
        """T0~StartedAt 구간의 구 프로세스 rollover는 정당한 관찰 자료다.

        한 창으로 합치면 배포가 07:00 직후일 때 이 rollover까지 FAILED로 오판한다.
        """
        line = json.dumps({"timestamp": "2026-08-17T06:02:00+00:00", "level": "INFO",
                           "message":
                           "[krx] rollover A75608/202608 → A75609/202609 reason=scheduled"})
        ad = _adapters(self.clock, statuses=[_payload()], shutdown_lines=[line])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.PASS)

    async def test_post_window_rollover_fails(self):
        line = json.dumps({"timestamp": "2026-08-17T06:05:30+00:00", "level": "INFO",
                           "message":
                           "[krx] rollover A75609/202609 → A75610/202610 reason=scheduled"})
        ad = _adapters(self.clock, statuses=[_payload()], runtime_lines=[line])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_log_fetch_failure_is_unverified(self):
        base_ad = _adapters(self.clock, statuses=[_payload(), _payload()])

        async def failing_logs(_s, _u):
            raise V.CollectorError("log read failed")

        ad = dataclasses.replace(base_ad, read_log_lines=failing_logs)
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_new_id_equal_to_old_fails(self):
        """재생성되지 않았으면 배포 자체가 반영되지 않았다."""
        ad = _adapters(self.clock, statuses=[_payload()],
                       identity_raw="old123|2026-08-15T00:00:00Z|0")
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
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
        await V.collect_evidence(_baseline(), ad, art)
        records = [json.loads(x) for x in
                   art.path.read_text(encoding="utf-8").strip().splitlines()]
        log_rec = next(r for r in records if r["kind"] == "logs")
        self.assertEqual(log_rec["unparsed_total"], 1)
        self.assertNotIn("unparsed", log_rec["window_pre"])
        self.assertNotIn("unparsed", log_rec["window_post"])
        self.assertIn("until", log_rec["window_post"])

    async def test_old_image_deployed_is_caught_by_behavior_probe(self):
        """⭐ **구 이미지를 force-recreate해도 KRX 축은 전부 PASS다.**

        8/17 이후에는 구 코드도 A75609를 고르고 no_op을 낸다. 그래서 동작
        probe(`_compute_expiry_date("202608")`)가 유일한 판별자다 — 구 코드는
        무보정 셋째 월요일 `2026-08-17`을 반환한다.
        """
        ad = _adapters(self.clock, statuses=[_payload(), _payload()],
                       probe={"202608": "2026-08-17", "202602": "2026-02-16"})
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("구 코드가 돌고 있다" in r for r in got["reasons"]))

    async def test_same_image_id_fails(self):
        """이미지가 그대로면 재빌드/재배포가 반영되지 않았다 (Coinone 사고 형태)."""
        ad = _adapters(self.clock, statuses=[_payload(), _payload()],
                       new_image="sha256:aaa")  # baseline.old_image와 동일
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("Image가 구 Image와 동일" in r for r in got["reasons"]))

    async def test_probe_failure_is_unverified(self):
        ad = _adapters(self.clock, statuses=[_payload(), _payload()],
                       probe={"202608": V.CollectorError("exec failed")})
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_unhealthy_service_fails(self):
        """force-recreate는 FX·USDT·broadcast·API도 함께 재시작한다 —
        KRX 축만 보고 PASS를 선언할 수 없다."""
        ad = _adapters(self.clock, statuses=[_payload(), _payload()],
                       health_status="degraded")
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)

    async def test_single_sample_cannot_pass_continuity(self):
        """첫 PASS에서 즉시 끝내면 identity 표본이 1건 — 변화를 볼 기회가 없었다.

        polling이 최소 표본을 확보하도록 바뀌었으므로 정상 경로에서는 2건 이상이
        모인다. 표본 1건짜리 판정은 UNVERIFIED임을 순수 함수로 잠근다.
        """
        one = V.ContainerIdentity("new1", "2026-08-17T06:05:00Z", 0)
        self.assertEqual(V.evaluate_continuity([one])[0], V.UNVERIFIED)

    async def test_restart_count_nonzero_fails(self):
        ad = _adapters(self.clock, statuses=[_payload(), _payload()],
                       identity_raw="new1|2026-08-17T06:05:00Z|1")
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)
        self.assertTrue(any("RestartCount" in r for r in got["reasons"]))

    async def test_empty_runtime_log_window_is_unverified(self):
        """회전 등으로 post 창이 통째로 비면 '오류 없음'이 아니라 관측 불능이다.

        새 프로세스는 startup 로그를 반드시 남기므로, 빈 창은 로그 유실 신호다.
        """
        ad = _adapters(self.clock, statuses=[_payload(), _payload()], runtime_lines=[])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_unplaceable_log_line_degrades_to_unverified(self):
        """배치 불가 줄이 있으면 그 줄에 marker가 있었는지 알 수 없다."""
        ad = _adapters(self.clock, statuses=[_payload(), _payload()],
                       shutdown_lines=["timestamp 없는 외래 줄"])
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.UNVERIFIED)

    async def test_removal_probe_is_recorded_in_artifact(self):
        """probe 결과가 증거로 남는가 — 최종 PASS인데 'destroy 없음'만 남으면
        그걸 보강한 근거가 빠져 감사 계약이 불완전하다 (codex 리뷰 지적)."""
        art = self._artifact()
        ad = _adapters(self.clock, statuses=[_payload()], events=[
            {"Action": "die",
             "Actor": {"ID": "old123", "Attributes": {"exitCode": "0"}}}])  # destroy 없음
        got = await V.collect_evidence(_baseline(), ad, art)
        self.assertEqual(got["verdict"], V.PASS)
        records = [json.loads(x) for x in
                   art.path.read_text(encoding="utf-8").strip().splitlines()]
        shutdown = next(r for r in records if r["kind"] == "shutdown")
        self.assertEqual(shutdown["removal_probe"], V.PASS)
        self.assertTrue(shutdown["destroyed"])  # probe로 보강된 상태가 반영됨
        self.assertEqual(shutdown["graceful_shutdown"]["verdict"], V.PASS)
        self.assertEqual(
            shutdown["graceful_shutdown"]["markers"],
            list(V.GRACEFUL_SHUTDOWN_MARKERS))
        self.assertIn("graceful shutdown", shutdown["detail"])

    async def test_die_ok_but_not_removed_and_probe_says_present(self):
        """die만으로는 제거를 증명하지 못한다."""
        base_ad = _adapters(self.clock, statuses=[_payload()],
                            events=[{"Action": "die", "Actor": {
                                "ID": "old123", "Attributes": {"exitCode": "0"}}}])

        async def inspect_present(fmt):
            if fmt.startswith("__probe__"):
                return "still-there"  # 조회 성공 = 아직 존재
            if "Image" in fmt:
                return "sha256:bbb"
            return "new1|2026-08-17T06:05:00Z|0"

        ad = dataclasses.replace(base_ad, inspect_format=inspect_present)
        got = await V.collect_evidence(_baseline(), ad, self._artifact())
        self.assertEqual(got["verdict"], V.FAILED)


if __name__ == "__main__":
    unittest.main()
