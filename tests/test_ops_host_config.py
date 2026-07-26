"""호스트 로그 rotation 설정의 **load-bearing 지시자** 회귀 가드.

이 설정들은 호스트(`/etc/logrotate.d/`, `/etc/systemd/journald.conf.d/`)에 설치되지만
**정본은 `ops/` 아래 체크인된 파일**이다. 호스트에만 두면 교체·복구 시 사라지고,
`postrotate` 실패 처리 같은 세부를 리뷰할 수도 없다(2026-07-26까지 실제로 그 상태였다).

여기서 잠그는 건 "설치됐는가"가 아니라 **"정본이 무해하게 약해지지 않았는가"**다 —
특히 `postrotate`의 USR1은 지우면 **조용히** 망가진다: nginx가 rename된 inode에 계속 써서
새 파일이 0바이트로 남고, 디스크는 계속 찬다. 실제 설치·동작 검증은
`ops/install-host-config.sh --verify`가 담당한다(테스트에서 호스트를 만질 수 없다).
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGROTATE = REPO_ROOT / "ops" / "logrotate" / "fxi-nginx"
JOURNALD = REPO_ROOT / "ops" / "systemd" / "journald-limits.conf"
INSTALLER = REPO_ROOT / "ops" / "install-host-config.sh"


class TestNginxLogrotateConfig(unittest.TestCase):
    def setUp(self):
        self.text = LOGROTATE.read_text()

    def test_rotates_the_bind_mounted_nginx_logs(self):
        """compose가 mount하는 그 경로여야 한다 — 다른 경로면 아무것도 회전시키지 않는다."""
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        self.assertIn("./volumes/logs/nginx:/var/log/nginx", compose,
                      "compose의 mount 경로가 바뀌면 이 설정도 함께 바뀌어야 한다")
        self.assertIn("/volumes/logs/nginx/*.log", self.text)

    def test_postrotate_signals_nginx_to_reopen(self):
        """USR1이 없으면 **silent failure** — 새 파일이 0바이트로 남고 디스크는 계속 찬다."""
        self.assertIn("postrotate", self.text)
        self.assertRegex(self.text, r"kill\s+--signal=USR1\s+exchange-rate-nginx",
                         "컨테이너 nginx에 USR1을 보내야 로그 파일을 재오픈한다")

    def test_postrotate_failure_does_not_break_rotation(self):
        """컨테이너가 없을 때 rotation 자체가 실패하면 안 된다(부팅 직후 등)."""
        self.assertIn("|| true", self.text)

    def test_has_safety_directives(self):
        for directive in ("missingok", "notifempty", "sharedscripts", "su root root"):
            self.assertIn(directive, self.text, f"{directive} 누락")

    def test_size_is_not_mixed_with_time_directives(self):
        """logrotate man: `size`는 daily/weekly 등과 **mutually exclusive**다.

        섞어 두면 시간 쪽이 조용히 무시돼 "매일 도는 줄" 오해한다(초안이 그랬다).
        """
        body = self.text[self.text.index("{"):]
        self.assertRegex(body, re.compile(r"^\s*size\s+\d+[kMG]?\s*$", re.M),
                         "size 지시자가 없다")
        for time_directive in ("daily", "weekly", "monthly", "yearly"):
            self.assertNotRegex(body, re.compile(rf"^\s*{time_directive}\s*$", re.M),
                                f"size와 {time_directive}를 함께 쓰면 안 된다")

    def test_bounds_disk_with_rotate_and_compress(self):
        self.assertRegex(self.text, re.compile(r"^\s*rotate\s+\d+", re.M),
                         "rotate 없이는 무한 누적된다")
        self.assertIn("compress", self.text)


class TestJournaldConfig(unittest.TestCase):
    def test_has_persistent_limits(self):
        text = JOURNALD.read_text()
        self.assertIn("[Journal]", text)
        self.assertRegex(text, re.compile(r"^SystemMaxUse=\d+[KMG]?$", re.M))
        self.assertRegex(text, re.compile(r"^SystemKeepFree=\d+[KMG]?$", re.M))


class TestInstaller(unittest.TestCase):
    def test_installs_both_configs_and_verifies(self):
        text = INSTALLER.read_text()
        self.assertTrue(INSTALLER.stat().st_mode & 0o111, "실행 권한이 없다")
        self.assertIn("logrotate/fxi-nginx", text)
        self.assertIn("systemd/journald-limits.conf", text)
        self.assertIn("--verify", text)

    def test_verify_detects_silent_usr1_failure(self):
        """검증이 '문법 OK'로 끝나면 안 된다 — 실제로 새 파일이 자라는지 봐야 한다."""
        text = INSTALLER.read_text()
        self.assertIn("logrotate -f", text, "강제 rotation 없이는 재오픈을 확인할 수 없다")
        self.assertRegex(text, r"after.*-gt.*before|\-gt \"\$before\"",
                         "rotation 후 새 파일 증가를 비교해야 한다")
        self.assertIn("exit 1", text, "검증 실패 시 non-zero로 끝나야 한다")


if __name__ == "__main__":
    unittest.main()
