"""변이 배터리 안전장치의 **영구** 계약.

⛔ 손으로 한 번 돌려본 것은 계약이 아니다. 이 모듈은 production 파일을 일시 변형하는 도구의
   안전장치라, 조용히 무력해지면 그 사실이 **다음 사고로만** 드러난다.

배경(실제 사고): 감사 workflow 의 병렬 에이전트들이 같은 worktree 에서 동시에 production
파일을 변이해 무관한 25건이 red 가 됐고, 동시 쓰기로 복원이 덮여 `init_rest_auth_app()` 이
`pass` 로 남았다. 근본 기전은 workflow 격리이고 여기 잠그는 것은 **방어층**이다.
"""
import ast
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from mutation_battery_guard import (  # noqa: E402
    ISOLATION_MARKER, BatteryLockBusy, MutatedFile, MutationConflict, NotIsolated,
    battery_lock, isolated_worktree, lock_path,
)

REPO = pathlib.Path(__file__).resolve().parent.parent


class TestBatteryLock(unittest.TestCase):
    def test_lock_lives_in_the_worktree_git_dir_not_the_working_tree(self):
        """⛔ working tree 에 두면 `.gitignore` 가 필요하고, 커밋되면 **stale 이 정상처럼** 보인다.

        락 파일의 존재는 활성 소유권의 증거가 아니다 — 소유권은 커널이 판정한다.
        """
        path = lock_path(REPO)
        self.assertNotIn(str(path), str(REPO / "app"))
        self.assertIn(".git", str(path), f"working tree 안에 있다: {path}")
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", str(path)],
                                 cwd=REPO, capture_output=True, text=True)
        self.assertNotEqual(tracked.returncode, 0, "락 파일이 추적되고 있다 — 커밋되면 안 된다")

    def test_second_acquisition_is_refused_and_released_lock_is_reusable(self):
        """⛔ 비차단이다 — 기다리면 두 번째 실행이 조용히 줄을 서서 원인 추적을 흐린다."""
        with battery_lock(REPO):
            with self.assertRaises(BatteryLockBusy):
                with battery_lock(REPO):
                    self.fail("두 번째 획득이 성공했다 — 락이 무력하다")
        with battery_lock(REPO):
            pass  # 해제 후에는 다시 잡혀야 한다

    def test_lock_is_released_even_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with battery_lock(REPO):
                raise RuntimeError("배터리 실패")
        with battery_lock(REPO):
            pass


class TestMutatedFile(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory(prefix="fxi-mutated-file-test-")
        self.addCleanup(self._tmpdir.cleanup)
        d = pathlib.Path(self._tmpdir.name)
        # ⛔ 표식을 **명시적으로** 붙인다. 이 클래스가 시험하는 것은 `MutatedFile` 의
        #    충돌 감지 의미이지 격리 자체가 아니다(격리는 전용 클래스가 시험한다).
        #    가드를 끄는 대신 opt-in 한다.
        (d / ISOLATION_MARKER).write_text("test\n")
        self.tmp = d / "target.py"
        self.tmp.write_text("x = 1\n")

    def test_normal_write_then_restore_returns_the_original(self):
        handle = MutatedFile(self.tmp)
        handle.write_mutant("x = 2\n")
        self.assertEqual(self.tmp.read_text(), "x = 2\n")
        handle.restore()
        self.assertEqual(self.tmp.read_text(), "x = 1\n")

    def test_restore_preserves_the_exact_original_bytes(self):
        original = b"x = 1\r\n"
        self.tmp.write_bytes(original)
        handle = MutatedFile(self.tmp)
        handle.write_mutant("x = 2\n")
        handle.restore()
        self.assertEqual(self.tmp.read_bytes(), original)

    def test_external_change_before_write_is_detected_and_preserved(self):
        """⛔ 변이를 **쓰기 전에도** 확인한다 — 그 사이 남이 고쳤으면 그걸 덮게 된다."""
        handle = MutatedFile(self.tmp)
        self.tmp.write_text("x = 99\n")          # 남의 정상 변경
        with self.assertRaises(MutationConflict):
            handle.write_mutant("x = 2\n")
        self.assertEqual(self.tmp.read_text(), "x = 99\n", "남의 변경이 덮였다")

    def test_external_change_before_restore_is_detected_and_preserved(self):
        """⛔ **이 사고의 핵심 기전이다.**

        "복원 후 sha == 내 스냅샷" 은 충돌 부재를 증명하지 못한다 — 남이 그 사이 정상 변경을
        했어도 내가 스냅샷으로 덮으면 sha 는 일치한다. 실제로 그렇게 편집이 사라졌다.
        """
        handle = MutatedFile(self.tmp)
        handle.write_mutant("x = 2\n")
        self.tmp.write_text("x = 42\n")          # 남의 정상 변경
        with self.assertRaises(MutationConflict):
            handle.restore()
        self.assertEqual(self.tmp.read_text(), "x = 42\n", "남의 변경이 덮였다")

    def test_conflict_message_does_not_leak_file_contents(self):
        """진단은 sha 로 한다 — production 본문을 로그·transcript 에 흘리지 않는다."""
        handle = MutatedFile(self.tmp)
        handle.write_mutant("SECRET_MARKER = 'x'\n")
        self.tmp.write_text("OTHER_MARKER = 'y'\n")
        with self.assertRaises(MutationConflict) as ctx:
            handle.restore()
        self.assertNotIn("SECRET_MARKER", str(ctx.exception))
        self.assertNotIn("OTHER_MARKER", str(ctx.exception))


class TestHarnessNoOpGuards(unittest.TestCase):
    """`apply_pairs` 가 **아무것도 바꾸지 않는 변이**를 통과시키면 SURVIVED 로 오독된다."""

    def _apply(self, source, pairs):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "battery_s1b", REPO / "scripts" / "mutation_rest_auth_lane.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["battery_s1b"] = mod
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
        return mod.apply_pairs(source, pairs)

    def test_pairwise_noop_is_rejected(self):
        out, why = self._apply("a = 1\n", [("a = 1\n", "a = 1\n")])
        self.assertIsNone(out)
        self.assertIn("no-op", why)

    def test_net_noop_across_two_pairs_is_rejected(self):
        """⛔ 합성 반례(codex): 각 pair 는 바꾸지만 **최종본은 원본과 같다**.

        pair 별 검사만으로는 통과하고, 그 변이는 아무 계약도 시험하지 않은 채 SURVIVED 로
        보고된다 — 존재하지 않는 커버리지를 믿게 만든다.
        """
        out, why = self._apply("a = 1\n", [("a = 1\n", "a = 2\n"), ("a = 2\n", "a = 1\n")])
        self.assertIsNone(out, "전체 no-op 변이가 통과했다")
        self.assertIn("전체 no-op", why)

    def test_missing_anchor_is_rejected(self):
        out, why = self._apply("a = 1\n", [("nonexistent", "x")])
        self.assertIsNone(out)
        self.assertIn("앵커", why)

    def test_syntax_breaking_mutant_is_rejected(self):
        out, why = self._apply("a = 1\n", [("a = 1\n", "a = (\n")])
        self.assertIsNone(out)
        self.assertIn("문법", why)


class TestRunnersUseASingleSnapshot(unittest.TestCase):
    """⛔ 배터리가 원본을 **두 번 읽으면** 충돌 감지가 그 창에서 무력화된다(codex).

    두 읽기 사이에 외부 변경이 끼면 `original` 은 구 내용, handle 의 기준은 신 내용이 되어
    `write_mutant` 는 "디스크가 내 것" 이라고 판단하고 **구 내용 기반 mutant 로 남의 변경을
    덮는다**. 스냅샷의 진실원은 `MutatedFile.original` 하나여야 한다.
    """

    RUNNERS = ("mutation_auth_executor_ledger.py", "mutation_rest_auth_lane.py")

    def test_adopting_runners_do_not_read_the_target_twice(self):
        import ast

        for name in self.RUNNERS:
            with self.subTest(runner=name):
                tree = ast.parse((REPO / "scripts" / name).read_text())
                # ⚠️ 범위를 **변이 실행 함수 안**으로 좁힌다. 모듈 수준의 `SRC.read_text()` 는
                #    lane-owned 심볼을 소스에서 도출하는 등 **다른 목적**이라 정당하다 — 처음엔
                #    전역으로 물어 올바른 코드에 red 를 냈다(실측).
                runners = [n for n in ast.walk(tree)
                           if isinstance(n, ast.FunctionDef)
                           and n.name in {"main", "_main_locked"}]
                self.assertTrue(runners, f"{name}: 변이 실행 함수를 못 찾았다")
                snapshot_reads = [
                    ast.unparse(c) for fn in runners for c in ast.walk(fn)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "read_text" and isinstance(c.func.value, ast.Name)
                    and c.func.value.id in {"SRC", "TARGET", "MAIN", "LANE", "FCM"}
                ]
                self.assertEqual(snapshot_reads, [],
                                 f"{name}: 변이 루프가 대상 파일을 직접 읽는다 — "
                                 f"MutatedFile.original 이 유일한 스냅샷이어야 한다: {snapshot_reads}")

    def test_conflict_detection_survives_a_synthetic_double_read(self):
        """합성 반례: 두 번 읽는 구현이 실제로 남의 변경을 덮는다는 것을 보인다."""
        import tempfile

        d = pathlib.Path(tempfile.mkdtemp())
        (d / ISOLATION_MARKER).write_text("test\n")
        tmp = d / "t.py"
        tmp.write_text("orig\n")
        stale = tmp.read_text()              # ① 첫 읽기
        tmp.write_text("external\n")         # 남의 변경
        handle = MutatedFile(tmp)            # ② 두 번째 읽기 — 기준이 신 내용이 된다
        handle.write_mutant(stale.replace("orig", "mutant"))   # 구 내용 기반 mutant
        self.assertEqual(tmp.read_text(), "mutant\n",
                         "이 시나리오가 바로 덮어쓰기다 — 그래서 단일 스냅샷이어야 한다")
        handle.restore()
        self.assertEqual(tmp.read_text(), "external\n",
                         "복원이 handle 기준(신 내용)으로 돌아가야 한다")
        tmp.unlink()


class TestIsolationIsEnforcedNotJustPracticed(unittest.TestCase):
    """⛔ **락은 배터리끼리만 막는다.** 일반 `pytest`·`git`·`docker build` 같은 독자는 락을
    모르고 변이본이 디스크에 있는 순간을 읽는다 — 오늘 실제로 발화했다(적대적 리뷰 에이전트가
    내 배터리의 S2-6/S2-4 변이본을 읽고 "설명되지 않는 red 3건" 을 보고).

    ⛔ "배터리 도는지 확인 후 거절" 은 답이 아니다 — 확인과 실행 사이 TOCTOU 창이 남는다(codex).
       격리는 그 창 자체를 없앤다. 여기 잠그는 것은 **절차가 아니라 기전**이다.
    """

    def test_shared_worktree_paths_are_recognised_as_not_isolated(self):
        """공유 경계에 실제로 결박한다. 판정 함수만 시험하면 호출이 사라져도 초록이다."""
        import tempfile

        target = pathlib.Path(tempfile.mkdtemp()) / "target.py"
        target.write_text("x = 1\n")
        handle = MutatedFile(target)
        with self.assertRaises(NotIsolated):
            handle.write_mutant("x = 2\n")
        self.assertEqual(target.read_text(), "x = 1\n", "격리 거부 뒤 파일이 바뀌었다")
        with self.assertRaises(NotIsolated):
            handle.restore()
        self.assertEqual(target.read_text(), "x = 1\n", "격리 거부 뒤 restore가 파일을 썼다")

    def test_isolated_worktree_allows_mutation_and_leaves_the_shared_tree_untouched(self):
        target = REPO / "app" / "database_settings.py"
        before = target.read_text()
        with isolated_worktree(REPO) as wt:
            twin = wt / "app" / "database_settings.py"
            self.assertTrue(twin.is_file(), "격리 트리에 대상 파일이 없다")
            from mutation_battery_guard import assert_isolated

            assert_isolated(twin)          # 격리 안이면 통과한다
            handle = MutatedFile(twin)
            handle.write_mutant(twin.read_text() + "\n# mutant\n")
            self.assertIn("# mutant", twin.read_text())
            self.assertEqual(target.read_text(), before, "공유 트리가 오염됐다")
            handle.restore()
        self.assertEqual(target.read_text(), before)

    def test_uncommitted_work_is_reproduced_inside_the_isolation(self):
        """⛔ 배터리는 보통 **미커밋** 상태를 시험한다. HEAD 만 뜨면 다른 코드를 시험하면서
        초록을 보고한다 — 그 오판을 막는다.

        이 계약을 시험하려고 실제 리포의 production 파일을 수정하면 테스트 자신이 공유 트리를
        오염시킨다. 독립 임시 리포에서만 미커밋 변경을 만든다.
        """
        import tempfile
        import subprocess as sp

        marker = "# fxi-isolation-probe\n"
        with tempfile.TemporaryDirectory(prefix="fxi-isolation-source-") as td:
            repo = pathlib.Path(td)
            sp.run(["git", "init", "-q"], cwd=repo, check=True)
            target = repo / "target.py"
            target.write_text("x = 1\n")
            sp.run(["git", "add", "target.py"], cwd=repo, check=True)
            sp.run(["git", "-c", "user.name=FXi Test", "-c", "user.email=fxi@test.invalid",
                    "commit", "-qm", "base"], cwd=repo, check=True)

            before = target.read_text()
            target.write_text(before + marker)
            with isolated_worktree(repo) as wt:
                self.assertIn(marker, (wt / "target.py").read_text(),
                              "미커밋 변경이 격리 트리에 재현되지 않았다")
            self.assertEqual(target.read_text(), before + marker,
                             "격리 복제가 원본 임시 리포를 바꿨다")

    def test_the_worktree_is_removed_afterwards(self):
        with isolated_worktree(REPO) as wt:
            path = wt
            self.assertTrue(path.is_dir())
        self.assertFalse(path.exists(), "격리 worktree 가 남았다")

    def test_every_repository_mutation_runner_adopts_all_three_guards(self):
        """새 runner가 직접 쓰기로 되돌아가도 8개 수기 목록을 갱신하지 않고 잡는다."""
        scripts = REPO / "scripts"
        runners = {
            path for path in scripts.glob("mutation_*.py")
            if path.name != "mutation_battery_guard.py"
        }
        self.assertGreaterEqual(len(runners), 8, sorted(path.name for path in runners))
        for path in sorted(runners):
            tree = ast.parse(path.read_text())
            calls = {
                node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            for required in ("battery_lock", "isolated_worktree", "MutatedFile"):
                self.assertIn(required, calls, f"{path.name}: {required} 호출이 없다")


class TestRunnerIsolationIntegration(unittest.TestCase):
    """helper 단독 시험이 아니라 runner가 격리 경계를 실제로 사용하는지 확인한다."""

    @staticmethod
    def _load(name: str, filename: str):
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_auth_runner_probe_reads_the_isolated_mutant(self):
        """구 runner는 mutant는 격리 트리에 쓰고 pytest는 공유 트리에서 돌려 SURVIVED였다."""
        import contextlib
        import io

        mod = self._load("battery_auth_isolation_test", "mutation_auth_executor_ledger.py")
        target = REPO / "app" / "auth_executor.py"
        before = target.read_text()
        worktrees_before = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        mod.MUTANTS = [mod.MUTANTS[0]]
        mod.MUTANT_PROBES = {}
        mod.DEFAULT_PROBES = ("s5",)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = mod.main()
        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("killed=1 survived=0", output.getvalue())
        self.assertEqual(target.read_text(), before, "공유 production 파일이 바뀌었다")
        worktrees_after = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(worktrees_after, worktrees_before, "runner가 임시 worktree를 남겼다")

    def test_rest_runner_explicitly_closes_its_isolation_context(self):
        """CPython 객체 소멸에 기대지 않고 runner가 context의 __exit__를 호출해야 한다."""
        mod = self._load("battery_rest_cleanup_test", "mutation_rest_auth_lane.py")

        class SpyContext:
            def __init__(self, value=None):
                self.value = value
                self.exited = False

            def __enter__(self):
                return self.value

            def __exit__(self, *_):
                self.exited = True

        lock = SpyContext()
        worktree = SpyContext(REPO)
        mod.MUTANTS = []
        mod.battery_lock = lambda _: lock
        mod.isolated_worktree = lambda _: worktree
        self.assertEqual(mod.main(), 0)
        self.assertTrue(worktree.exited, "격리 worktree context를 닫지 않았다")
        self.assertTrue(lock.exited, "battery lock context를 닫지 않았다")
        self.assertIsNone(mod._WORK, "정리 뒤 worktree 전역이 남았다")

    def test_s7_runner_probe_reads_the_isolated_mutant(self):
        """변이와 probe cwd가 다른 트리를 보면 배터리는 전부 SURVIVED가 된다."""
        import contextlib
        import io

        mod = self._load("battery_s7_isolation_test", "mutation_s7_exposure.py")
        target = REPO / "app" / "main.py"
        before = target.read_text()
        worktrees_before = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        mod.MUTANTS = [mod.MUTANTS[0]]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = mod.main()
        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("killed=1 survived=0", output.getvalue())
        self.assertEqual(target.read_text(), before, "공유 production 파일이 바뀌었다")
        worktrees_after = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(worktrees_after, worktrees_before, "runner가 임시 worktree를 남겼다")

    def test_s6_runner_probe_reads_the_isolated_mutant(self):
        """다중 대상 runner도 변이 파일과 probe가 같은 격리 트리를 봐야 한다."""
        import contextlib
        import io

        mod = self._load("battery_s6_isolation_test", "mutation_s6_arrival.py")
        target = REPO / "app" / "ws_connection_metrics.py"
        before = target.read_text()
        worktrees_before = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        mod.MUTANTS = [mod.MUTANTS[0]]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = mod.main()
        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("killed=1 survived=0", output.getvalue())
        self.assertEqual(target.read_text(), before, "공유 production 파일이 바뀌었다")
        worktrees_after = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(worktrees_after, worktrees_before, "runner가 임시 worktree를 남겼다")

    def test_s0_runner_probe_reads_the_isolated_mutant(self):
        """S0 runner의 pytest도 격리된 queue-wait 변이를 읽어야 한다."""
        import contextlib
        import io

        mod = self._load("battery_s0_isolation_test", "mutation_s0_queue_wait.py")
        target = REPO / "app" / "subscribe_load_metrics.py"
        before = target.read_bytes()
        worktrees_before = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        mod.MUTANTS = [mod.MUTANTS[0]]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = mod.main()
        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("killed=1 survived=0", output.getvalue())
        self.assertEqual(target.read_bytes(), before, "공유 production 파일 bytes가 바뀌었다")
        worktrees_after = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(worktrees_after, worktrees_before, "runner가 임시 worktree를 남겼다")

    def test_c3_runner_probe_reads_the_isolated_mutant(self):
        """C3 runner는 격리된 test/workflow/lock을 한 묶음으로 읽어야 한다."""
        import contextlib
        import io

        mod = self._load("battery_c3_isolation_test", "mutation_c3_ios_pin_gate.py")
        target = REPO / "tests" / "test_topic_only_ledger.py"
        before = target.read_bytes()
        worktrees_before = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        mod.MUTANTS = [mod.MUTANTS[0]]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = mod.main()
        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("killed=1 survived=0", output.getvalue())
        self.assertEqual(target.read_bytes(), before, "공유 ledger 테스트가 바뀌었다")
        worktrees_after = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(worktrees_after, worktrees_before, "runner가 임시 worktree를 남겼다")

    def test_subscribe_load_runner_probe_reads_the_isolated_mutant(self):
        """CLI runner의 대표 변이도 격리 probe에서 실제로 죽어야 한다."""
        import contextlib
        import io
        from unittest import mock

        mod = self._load(
            "battery_subscribe_load_isolation_test",
            "mutation_subscribe_load_metrics.py",
        )
        target = REPO / "app" / "subscribe_load_metrics.py"
        before = target.read_bytes()
        worktrees_before = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        mod.MUTANTS = [mod.MUTANTS[0]]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["mutation_subscribe_load_metrics.py"]):
            with contextlib.redirect_stdout(output):
                rc = mod.main()
        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("/ 1 (문제 0)", output.getvalue())
        self.assertEqual(target.read_bytes(), before, "공유 subscribe-load 파일이 바뀌었다")
        worktrees_after = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(worktrees_after, worktrees_before, "runner가 임시 worktree를 남겼다")


if __name__ == "__main__":
    unittest.main()
