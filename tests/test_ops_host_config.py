"""호스트 로그 rotation 설정의 **load-bearing 지시자** 회귀 가드.

이 설정들은 호스트(`/etc/logrotate.d/`, `/etc/systemd/journald.conf.d/`)에 설치되지만
**정본은 `ops/` 아래 체크인된 파일**이다. 호스트에만 두면 교체·복구 시 사라지고,
`postrotate` 실패 처리 같은 세부를 리뷰할 수도 없다(2026-07-26까지 실제로 그 상태였다).

여기서 잠그는 건 "설치됐는가"가 아니라 **"정본이 무해하게 약해지지 않았는가"**다 —
특히 `postrotate`의 USR1은 지우면 **조용히** 망가진다: nginx가 rename된 inode에 계속 써서
새 파일이 0바이트로 남고, 디스크는 계속 찬다. 실제 설치·동작 검증은 호스트에서
`ops/install-host-config.sh`가 담당한다(테스트에서 호스트를 만질 수 없다):
`--check`(비파괴 동기화·문법·실효값) / `--install`(복구) / `--verify-reopen`(⚠️ 강제 rotation).

⚠️ 이 파일 안의 다른 `--verify` 언급은 **제거된 구 플래그에 대한 역사 서술**이다(왜 이름을
바꿨는지). 지시가 아니므로 그대로 둔다.
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGROTATE = REPO_ROOT / "ops" / "logrotate" / "fxi-nginx"
JOURNALD = REPO_ROOT / "ops" / "systemd" / "zz-fxi-limits.conf"
INSTALLER = REPO_ROOT / "ops" / "install-host-config.sh"
LEGACY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "legacy_journald_limits.conf"


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


class TestJournaldDropInOrdering(unittest.TestCase):
    """drop-in은 **파일명 lexical 순서**로 처리되고 뒤가 이긴다 — 이름이 계약이다."""

    def test_name_sorts_after_distro_dropins(self):
        """관례인 `NN-` 숫자 접두사는 접두사 없는 배포판 파일에 **진다**(숫자 < 소문자).

        실측: `90-fxi-limits.conf` < `limits.conf` < `syslog.conf` < `zz-fxi-limits.conf`.
        디스크 안전 상한이라 조용히 덮이면 안 되므로 마지막으로 정렬되는 이름을 쓴다.
        """
        name = JOURNALD.name
        for distro in ("syslog.conf", "99-default.conf", "limits.conf"):
            self.assertGreater(name, distro,
                               f"{name}이 {distro}보다 먼저 정렬되면 그쪽 정의가 이긴다")

    def test_installer_removes_legacy_only_when_fxi_owned(self):
        """`limits.conf`는 **흔한 이름**이라 관리자가 만든 다른 파일일 수 있다 —
        소유 마커가 있을 때만 지우고 없으면 중단해야 한다(codex).
        """
        text = INSTALLER.read_text()
        self.assertIn("grep -Fxq", text,
                      "부분 문자열이 아니라 마커 **줄 전체**를 정확 비교해야 한다")
        self.assertIn("^# fxi-managed: ", text, "기대 마커를 정본에서 뽑아 써야 한다")
        self.assertIn("중단:", text, "마커가 없으면 중단해야 한다")

    def test_legacy_migration_accepts_pre_marker_canonical(self):
        """마커는 나중에 도입했다 — **그 이전에 설치된 우리 파일**에는 마커가 없다.

        구 정본 해시를 알고 있으므로 정확히 일치하면 우리 것으로 인정해야 한다.
        아니면 구 버전에서 올라온 호스트가 자기 파일을 남의 것으로 보고 중단한다(codex).
        """
        import hashlib
        text = INSTALLER.read_text()
        self.assertIn("legacy_known_sha256=", text, "구 정본 해시 기반 마이그레이션 경로가 없다")
        embedded = re.search(r"legacy_known_sha256=([0-9a-f]{64})", text).group(1)
        # ⚠️ `git show <구커밋>`에 의존하면 안 된다 — Actions checkout은 fetch-depth 1(shallow)이라
        #    구 커밋이 없어 CI에서만 실패한다(실제로 1322c59가 그렇게 red였다).
        #    구 정본을 **fixture로 체크인**해 CI와 로컬이 같은 것을 보게 한다.
        actual = LEGACY_FIXTURE.read_bytes()
        self.assertTrue(actual, "구 정본 fixture가 비어 있다")
        self.assertEqual(embedded, hashlib.sha256(actual).hexdigest(),
                         "박아둔 해시가 구 정본 fixture와 다르다 — 마이그레이션이 동작하지 않는다")

    def test_legacy_fixture_matches_history_when_available(self):
        """전체 clone에서는 fixture가 실제 git 히스토리와 같은지도 본다(shallow면 skip).

        fixture가 히스토리에서 떨어져 나가면 마이그레이션 대상이 실물과 달라진다.
        """
        import subprocess
        out = subprocess.run(["git", "show", "b65bc24:ops/systemd/journald-limits.conf"],
                             cwd=REPO_ROOT, capture_output=True)
        if out.returncode != 0 or not out.stdout:
            self.skipTest("shallow clone — 구 커밋 없음")
        self.assertEqual(out.stdout, LEGACY_FIXTURE.read_bytes(),
                         "fixture가 실제 구 정본과 다르다")

    def test_legacy_migration_requires_exact_match(self):
        """부분 일치로 지우면 안 된다 — 남이 손댄 파일도 우리 것으로 오인한다."""
        text = INSTALLER.read_text()
        self.assertIn('[ "$legacy_sha256" = "$legacy_known_sha256" ]', text)

    def test_canonical_files_carry_ownership_marker(self):
        """소유 판별이 가능하려면 정본 자체에 마커가 있어야 한다."""
        for path in (LOGROTATE, JOURNALD):
            self.assertIn("fxi-managed:", path.read_text(), f"{path.name}에 소유 마커가 없다")

    def test_override_guidance_does_not_say_reinstall(self):
        """override는 **재설치로 안 고쳐진다** — 같은 이름을 다시 복사해도 순서가 그대로다(codex)."""
        text = INSTALLER.read_text()
        self.assertIn("재설치로는 안 고쳐진다", text)
        self.assertIn("cat-config", text, "범인 drop-in을 찾는 방법을 안내해야 한다")

    def test_drift_guidance_is_split_by_kind(self):
        """공통 종료부가 무조건 "재설치"를 안내하면 override에서 **자기모순**이 된다 —
        게다가 무기명 실행은 강제 rotation까지 한다. 종류별로 갈려야 한다(codex).
        """
        text = INSTALLER.read_text()
        self.assertIn("file_drift", text)
        self.assertIn("override_drift", text)
        self.assertIn("--install", text, "재설치 안내는 강제 rotation 없는 모드를 가리켜야 한다")
        # 공통 종료부에 무조건 재설치를 안내하는 옛 문구가 남아 있으면 안 된다
        self.assertNotIn("→ 재설치하려면: sudo $0\"", text)

    def test_install_mode_does_not_force_rotation(self):
        """복구 경로가 강제 rotation을 동반하면 "복구하려다 로그를 잃는다"."""
        text = INSTALLER.read_text()
        case_body = text[text.index("case \"${1:-}\""):]
        self.assertRegex(case_body, r"--install\)\s+install_config; check ;;",
                         "--install은 install+check만 해야 한다(verify_reopen 금지)")


class TestInstaller(unittest.TestCase):
    def test_installs_both_configs(self):
        text = INSTALLER.read_text()
        self.assertTrue(INSTALLER.stat().st_mode & 0o111, "실행 권한이 없다")
        self.assertIn("logrotate/fxi-nginx", text)
        self.assertIn("systemd/zz-fxi-limits.conf", text)

    def test_destructive_and_nondestructive_modes_are_separated(self):
        """`logrotate -f`는 **실제 로그를 회전**시킨다 — 반복 실행하면 `rotate 7`을 밀어내
        과거 로그가 조기 삭제된다. 상시 점검(`--check`)은 그걸 하면 안 된다.

        초안은 그 파괴적 동작을 `--verify`("검증만")라는 이름 뒤에 숨겼다.
        """
        text = INSTALLER.read_text()
        self.assertIn("--check", text, "비파괴 점검 모드가 있어야 한다")
        self.assertIn("--verify-reopen", text, "파괴적 모드는 이름으로 드러나야 한다")
        check_body = text[text.index("check() {"):text.index("verify_reopen() {")]
        self.assertNotIn("logrotate -f", check_body,
                         "비파괴 모드가 강제 rotation을 하면 이름이 거짓말이 된다")

    def test_reopen_check_detects_silent_usr1_failure(self):
        """실증이 '문법 OK'로 끝나면 안 된다 — 실제로 새 파일이 자라는지 봐야 한다."""
        text = INSTALLER.read_text()
        self.assertIn("logrotate -f", text, "강제 rotation 없이는 재오픈을 확인할 수 없다")
        self.assertRegex(text, r'\$after.*-gt.*\$before', "rotation 후 새 파일 증가를 비교해야 한다")
        self.assertIn("exit 1", text, "검증 실패 시 non-zero로 끝나야 한다")

    def test_check_compares_installed_against_canonical(self):
        """문법만 보면 호스트에서 USR1·rotate 상한을 손으로 지워도 통과한다 —
        그러면 "정본"이 이름뿐이다. 설치본과 **동일성**을 봐야 계약이 성립한다(codex).
        """
        text = INSTALLER.read_text()
        check_body = text[text.index("check() {"):text.index("verify_reopen() {")]
        self.assertIn("cmp -s", check_body, "정본↔설치본 비교가 없다")
        self.assertIn("/etc/logrotate.d/fxi-nginx", check_body)
        self.assertIn("/etc/systemd/journald.conf.d/zz-fxi-limits.conf", check_body)
        self.assertIn("exit 1", check_body, "drift를 발견하면 non-zero로 끝나야 한다")

    def test_check_uses_effective_journald_value(self):
        """journald는 drop-in 병합이라 **파일이 맞아도 후순위가 덮을 수 있다**.

        실측: 우리 drop-in 뒤에 `/usr/lib/systemd/journald.conf.d/syslog.conf`가 온다.
        systemd는 뒤에 오는 정의가 이기므로 `tail -1`(마지막)이 실효값이다.
        """
        text = INSTALLER.read_text()
        check_body = text[text.index("check() {"):text.index("verify_reopen() {")]
        self.assertIn("cat-config", check_body)
        self.assertIn("tail -1", check_body,
                      "마지막 값을 봐야 후순위 drop-in 재정의를 검출한다")

    def test_reopen_check_targets_the_local_nginx(self):
        """공인 DNS로 쏘면 호스트 교체 중(DNS 전환 전) **구 서버**를 때려 오판한다.

        `--resolve`로 로컬을 확정해야 이 호스트의 로그가 자라는지 보는 검사가 된다.
        """
        text = INSTALLER.read_text()
        self.assertIn("--resolve fxi.kr:443:127.0.0.1", text,
                      "검증 트래픽이 이 호스트의 nginx로 간다는 보장이 필요하다")


if __name__ == "__main__":
    unittest.main()
