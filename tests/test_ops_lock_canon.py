"""lock 정본 + **실제 상호배제** 통합 테스트.

⚠️ 이 파일이 두 번 고쳐진 이력:
① lock 경로가 세 스크립트에 각각 리터럴로 있고 각각 env로 재정의됐다 → 정본 파일로 모았다.
② 그 정본의 **위치**를 다시 env(`FXI_LOCK_CONF`)로 재정의할 수 있었다 → 결함이 한 단계
   이동한 것일 뿐이었다. 배포와 cron은 **별도 프로세스**라 한쪽 환경만 바꿀 수 있고, 그러면
   배제가 사라진다("재정의하면 함께 움직인다"는 추론이 틀렸다 — cron daemon엔 전파되지 않는다).
   → env 손잡이를 **전부 제거**하고 고정 상대 경로(`$(dirname $0)/lock.conf`)만 읽는다.
   테스트는 스크립트와 정본을 임시 디렉터리에 **함께 복사**해 설치 단위로 격리한다.

`fcntl.flock` 기반 충실한 더블을 쓴다 — 실제로 잠그므로 서로 다른 프로세스가 같은 설치에서
진짜로 배제된다.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
OPS = REPO / "ops"
SCRIPTS = ("deploy-fastapi.sh", "cron-with-lock.sh", "cron-job.sh")

FAKE_FLOCK = '''#!/usr/bin/env python3
"""fcntl 기반 flock 대역 — 실제 advisory lock을 잡는다(경로별 독립)."""
import fcntl, sys, time
args = sys.argv[1:]
timeout = None
if args and args[0] == "-w":
    timeout = float(args[1]); args = args[2:]
fd = int(args[0])
deadline = None if timeout is None else time.monotonic() + timeout
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); sys.exit(0)
    except OSError:
        if deadline is not None and time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(0.02)
'''


def _install(root: pathlib.Path):
    """ops 스크립트와 lock 정본을 한 디렉터리에 **함께** 설치한다 = 프로덕션과 같은 형태."""
    ops = root / "ops"; ops.mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy2(OPS / name, ops / name)
    shutil.copy2(OPS / "cron-allowlist.txt", ops / "cron-allowlist.txt")
    lock = root / "shared.lock"
    (ops / "lock.conf").write_text(f"FXI_DEPLOY_LOCK={lock}\n", encoding="utf-8")
    bin_ = root / "bin"; bin_.mkdir()
    fl = bin_ / "flock"; fl.write_text(FAKE_FLOCK, encoding="utf-8"); fl.chmod(0o755)
    return ops, lock, fl


class TestLockCanon(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.addCleanup(self.td.cleanup)
        self.root = pathlib.Path(self.td.name)
        self.ops, self.lock, self.flock = _install(self.root)

    def _env(self, **extra):
        return dict(os.environ, FLOCK_BIN=str(self.flock), **extra)

    def test_no_script_honours_an_env_override_for_the_canon(self):
        """⛔ env 손잡이가 하나라도 되살아나면 결함이 한 단계 이동한 상태로 돌아간다."""
        import re
        pat = re.compile(r"\$\{(FXI_LOCK_CONF|LOCK_FILE|FXI_LOCK_FILE|WRAPPER"
                         r"|CRON_JOB_SCRIPT|CRON_ALLOWLIST):-")
        for name in SCRIPTS:
            with self.subTest(script=name):
                src = (OPS / name).read_text(encoding="utf-8")
                self.assertIsNone(pat.search(src), f"{name}에 env 손잡이가 있다")

    def test_each_script_opens_the_installed_canon_lock(self):
        """⛔ **각 스크립트를 실제로 실행**해 그 설치의 lock 파일을 여는지 관측한다.

        ⚠️ 구 버전은 루프 변수 `script`를 쓰지 않고 같은 conf를 세 번 source해 **공허**했다 —
        한 스크립트가 다른 기본 정본을 쓰도록 바뀌어도 통과했다(실측 지적).
        여기서는 `exec 200>"$FXI_DEPLOY_LOCK"`가 파일을 **생성**하는 사실을 관측한다.
        """
        for name in ("cron-with-lock.sh", "cron-job.sh"):
            with self.subTest(script=name):
                root = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(
                    shutil.rmtree, root, ignore_errors=True)
                ops, lock, fl = _install(root)
                args = (["--policy", "wait", "--", "true"] if name == "cron-with-lock.sh"
                        else ["--print-crontab"])
                if name == "cron-job.sh":
                    continue   # --print-crontab 은 lock을 열지 않는다(정본 출력 전용)
                subprocess.run(["bash", str(ops / name), *args],
                               env=dict(os.environ, FLOCK_BIN=str(fl)),
                               capture_output=True, text=True, timeout=30)
                self.assertTrue(lock.exists(), f"{name}이 설치 정본의 lock을 열지 않았다")

    def test_cron_job_uses_the_installed_wrapper_and_lock(self):
        """launcher → wrapper → lock 경로가 **설치 안에서** 이어지는지 실행으로 확인."""
        marker = self.root / "ran"
        stub = self.ops / "docker"
        stub.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n", encoding="utf-8")
        stub.chmod(0o755)
        r = subprocess.run(["bash", str(self.ops / "cron-job.sh"), "hourly-bithumb"],
                           env=self._env(DOCKER_BIN=str(stub)), capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(marker.exists(), "launcher가 docker를 실행하지 않았다")
        self.assertTrue(self.lock.exists(), "설치 정본의 lock을 열지 않았다")

    def _hold(self, ops, lock_marker):
        proc = subprocess.Popen(
            ["bash", str(ops / "cron-with-lock.sh"), "--policy", "block", "--",
             "sh", "-c", f"touch {lock_marker}; sleep 30"],
            env=dict(os.environ, FLOCK_BIN=str(ops.parent / "bin" / "flock")),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while not lock_marker.exists():
            self.assertLess(time.monotonic(), deadline, "holder가 lock을 잡지 못했다")
            self.assertIsNone(proc.poll(), "holder가 죽었다")
            time.sleep(0.02)
        return proc

    def test_same_install_actually_excludes_two_processes(self):
        """⛔ **실제 배제** — 같은 설치에서 block이 잡고 있으면 wait는 timeout(75)이다."""
        self._hold(self.ops, self.root / "held")
        r = subprocess.run(
            ["bash", str(self.ops / "cron-with-lock.sh"), "--policy", "wait", "--", "echo", "RAN"],
            env=self._env(CRON_LOCK_WAIT="1"), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        self.assertNotIn("RAN", r.stdout)

    def test_two_separate_installs_do_not_interfere(self):
        """더블이 **경로별 독립**을 실제로 모델링하는지 — 아니면 위 테스트가 공허하다.

        ⚠️ 이것은 "프로덕션에 두 경로가 있어도 된다"는 뜻이 **아니다**. 프로덕션 설치는 하나이고,
        env로 정본을 갈아끼울 수단도 없앴다. 여기서 두 설치를 만드는 것은 **더블 자기검사**다.
        """
        other = pathlib.Path(tempfile.mkdtemp()); self.addCleanup(
            shutil.rmtree, other, ignore_errors=True)
        ops2, _, fl2 = _install(other)
        self._hold(self.ops, self.root / "held")
        r = subprocess.run(
            ["bash", str(ops2 / "cron-with-lock.sh"), "--policy", "wait", "--", "echo", "RAN"],
            env=dict(os.environ, FLOCK_BIN=str(fl2), CRON_LOCK_WAIT="1"),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RAN", r.stdout)

    def test_missing_canon_is_refused(self):
        (self.ops / "lock.conf").unlink()
        r = subprocess.run(
            ["bash", str(self.ops / "cron-with-lock.sh"), "--policy", "wait", "--", "echo", "RAN"],
            env=self._env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertNotIn("RAN", r.stdout)


if __name__ == "__main__":
    unittest.main()
