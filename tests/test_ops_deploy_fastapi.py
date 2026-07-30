"""ops/deploy-fastapi.sh 실패 주입 테스트.

⚠️ 이 파일이 존재하는 이유: 같은 배포 절차를 메모리의 **복사 코드**로 두는 동안 셸 제어흐름
결함을 연속 3번 냈다. 복사 코드는 검증 대상이 아니라 같은 부류가 계속 재발했다.

실측으로 확인한 세 결함(전부 이 테스트가 잠근다):
- `set -u`만 쓰면 실패한 명령 뒤에도 본문이 계속되고 기존 컨테이너가 healthy면 **rc=0**이 된다.
- 성공 조건이 health뿐이면 recreate가 조용히 미적용된 경우를 **성공으로 오판**한다.
- bash signal trap은 handler가 반환하면 **본문을 계속 실행**한다(TERM → trap → continued → rc=0).

⛔ **이 테스트가 잠그지 못하는 것**(변이 실측으로 확인, 과대주장 방지):
- `set -Eeuo pipefail` → `set -u`로 되돌려도 **전부 green**이다. 수렴 검사(running == target)가
  tag 실패 경로를 이미 잡기 때문이다. `set -e`는 defense-in-depth이고 실제 게이트는 수렴 검사다.
- signal handler를 EXIT과 **통합**해도 전부 green이다. 그 타이밍에서는 recover가 running을
  fallback으로 되돌려 수렴 검사가 어차피 실패하기 때문이다.
  ⚠️ 통합 handler가 **오탐을 내는 창은 실재한다** — 수렴 확인 직후 `trap -` 직전에 signal이 오면
  recover가 롤백한 뒤 본문이 계속되어 `exit 0`이 나간다. 그 창을 재현하는 테스트를 시도했으나
  가짜 docker의 `$PPID`가 명령 치환 **서브셸**이라 본 스크립트에 signal이 닿지 않아 **재현 실패**했다.
  타이밍 의존 테스트를 CI에 넣는 대가가 크다는 이 리포의 교훈에 따라 넣지 않았다 —
  그 창은 **구조로 닫혀 있고(분리 handler) 테스트로는 잠겨 있지 않다.**
  bash 의미론 자체는 격리 실험으로 확인했다: 통합 handler → `TERM → trap → 본문 계속 → rc=0`,
  분리 handler(`recover; exit 1`) → rc=1.
"""
import os
import pathlib
import signal
import subprocess
import sys
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "ops" / "deploy-fastapi.sh"

TARGET = "img:new"
FALLBACK = "img:old"
# ⚠️ 실제 docker의 형식 차이를 **그대로 모델링**한다 — 같은 형식을 주면 short/full 혼용 결함이
#    가려진다(실측으로 그렇게 가려졌다). images는 축약, image inspect / inspect는 sha256: 전체.
NEW_SHORT, OLD_SHORT = "newid0000000", "oldid0000000"
NEW_ID = "sha256:" + NEW_SHORT + "a" * 52
OLD_ID = "sha256:" + OLD_SHORT + "b" * 52


class _Harness:
    """가짜 `docker`를 PATH에 심어 스크립트를 구동한다.

    가짜는 상태를 파일로 들고 있어(running image id) recreate가 실제로 적용됐는지를 표현할 수 있다 —
    "health는 OK인데 running이 안 바뀐" 상태를 만들 수 있어야 H2를 잠글 수 있다.
    """

    def __init__(self, tmp: pathlib.Path, *, fail=(), apply_recreate=True,
                 healthy=True, kill_on=None, missing=(),
                 fail_tag_nth=0, fail_up_nth=0, cron_wrapped=True,
                 cron_wait=600, cron_query_fails=False, cron_commented=False,
                 cron_lines=None):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        (tmp / "running").write_text(OLD_ID, encoding="utf-8")
        (tmp / "latest").write_text(FALLBACK, encoding="utf-8")
        (tmp / "calls").write_text("", encoding="utf-8")
        fake = self.bin / "docker"
        fake.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            TMP={tmp}
            echo "$*" >> "$TMP/calls"
            FAIL="{','.join(fail)}"
            MISSING="{','.join(missing)}"
            FAIL_TAG_NTH={fail_tag_nth}
            FAIL_UP_NTH={fail_up_nth}
            case "$1" in
              images)
                # 축약 ID — 스크립트가 이걸 쓰면 inspect(전체 ID)와 어긋나 수렴이 영구 실패한다.
                T="$2"; [ "$T" = "img:latest" ] && T=$(cat "$TMP/latest")
                case "$T" in
                  {TARGET}) echo {NEW_SHORT} ;;
                  {FALLBACK}) echo {OLD_SHORT} ;;
                esac ;;
              image)
                # docker image inspect <tag> --format '{{{{.Id}}}}' — 전체 ID. 없는 태그는 비영 종료.
                # ⚠️ 실 docker처럼 `img:latest`는 **그 태그가 가리키는 이미지**로 해석해야 한다 —
                #    안 하면 수렴 판정의 latest 대조가 구조적으로 실패해 happy path가 red가 된다.
                T="$3"; [ "$T" = "img:latest" ] && T=$(cat "$TMP/latest")
                for m in ${{MISSING//,/ }}; do [ "$T" = "$m" ] && exit 1; done
                case "$T" in
                  {TARGET}) echo {NEW_ID} ;;
                  {FALLBACK}) echo {OLD_ID} ;;
                  *) exit 1 ;;
                esac ;;
              tag)
                N=$(( $(cat "$TMP/tagn" 2>/dev/null || echo 0) + 1 )); echo $N > "$TMP/tagn"
                case ",$FAIL," in *,tag,*) exit 1 ;; esac
                [ "$FAIL_TAG_NTH" -eq "$N" ] && exit 1     # 선택적: N번째 tag만 실패
                echo "$2" > "$TMP/latest" ;;
              inspect) cat "$TMP/running" ;;
              compose)
                shift
                if [ "$1" = "up" ]; then
                  M=$(( $(cat "$TMP/upn" 2>/dev/null || echo 0) + 1 )); echo $M > "$TMP/upn"
                  case ",$FAIL," in *,up,*) exit 1 ;; esac
                  [ "$FAIL_UP_NTH" -eq "$M" ] && exit 1     # 선택적: M번째 up만 실패
                  {"" if apply_recreate else "exit 0  # recreate 미적용 시뮬"}
                  L=$(cat "$TMP/latest")
                  case "$L" in
                    {TARGET}) echo {NEW_ID} > "$TMP/running" ;;
                    {FALLBACK}) echo {OLD_ID} > "$TMP/running" ;;
                  esac
                elif [ "$1" = "exec" ]; then
                  {'''echo '{"status":"healthy","message":"ok"}' ''' if healthy
                   else '''echo '{"status":"unhealthy","message":"degraded"}' '''}
                fi ;;
            esac
            exit 0
            """), encoding="utf-8")
        fake.chmod(0o755)
        # 가짜 flock — LOCKED 파일이 있으면 획득 실패(cron이 쥐고 있는 상황).
        fl = self.bin / "flock"
        fl.write_text(f'#!/usr/bin/env bash\n[ -f {tmp}/LOCKED ] && exit 1\nexit 0\n',
                      encoding="utf-8")
        fl.chmod(0o755)
        # 가짜 crontab — 5줄 전부 flock으로 감싼 형태를 기본으로 낸다(감싸이지 않은 경우도 재현).
        ct = self.bin / "crontab"
        if cron_lines is not None:
            lines = list(cron_lines)
        else:
            canon = subprocess.run(["bash", str(REPO / "ops" / "cron-job.sh"), "--print-crontab"],
                                   capture_output=True, text=True, check=True).stdout.splitlines()
            if cron_commented:
                lines = [f"  # {l}" for l in canon]
            elif not cron_wrapped:
                lines = [l.replace("ops/cron-job.sh daily-all",
                                   "/usr/bin/docker compose run --rm fastapi python s.py")
                         for l in canon]
            else:
                lines = canon
        body = "\n".join(lines)
        if cron_query_fails:
            ct.write_text('#!/usr/bin/env bash\nexit 1\n', encoding="utf-8")
        else:
            ct.write_text(f'#!/usr/bin/env bash\ncat <<EOF\n{body}\nEOF\n', encoding="utf-8")
        ct.chmod(0o755)
        self.kill_on = kill_on

    def run(self, *, target=TARGET, fallback=FALLBACK, timeout=60, now_utc="00:20",
            extra_args=()):
        env = dict(
            os.environ,
            NOW_UTC=now_utc,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            DOCKER_BIN="docker",
            HEALTH_RETRIES="2",
            HEALTH_SLEEP="0",
            COMPOSE_DIR=str(self.tmp),
            LATEST_TAG="img:latest",
            LOCK_FILE=str(self.tmp / "deploy.lock"),
            FLOCK_BIN=str(self.bin / "flock"),
            CRONTAB_BIN=str(self.bin / "crontab"),
            CRON_JOB_SCRIPT=str(REPO / "ops" / "cron-job.sh"),
        )
        proc = subprocess.Popen(
            ["bash", str(SCRIPT), "--target", target, "--fallback", fallback, *extra_args],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        if self.kill_on:
            # 본문이 상태를 바꾼 뒤(태그 전환 후)에 signal을 보내야 M3를 재현할 수 있다.
            deadline = 40
            while deadline > 0:
                if (self.tmp / "calls").read_text(encoding="utf-8").count("tag ") >= 1:
                    break
                deadline -= 1
                os.times()
            proc.send_signal(self.kill_on)
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out

    @property
    def latest(self):
        return (self.tmp / "latest").read_text(encoding="utf-8").strip()

    @property
    def running(self):
        return (self.tmp / "running").read_text(encoding="utf-8").strip()


class TestDeployScript(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_substring_bypasses_are_all_refused(self):
        """⛔ **substring 검사의 원리적 한계** — 줄이 임의 shell이면 무엇을 확인해도 그 **옆에서**
        다른 일을 할 수 있다. 실측으로 전부 통과했던 세 형태:

            ① `--policy wait -- true && docker compose run …`  (wrapper가 lock을 놓은 뒤 docker)
            ② `LOCK_FILE=/tmp/other.lock … -- docker compose run …` (배포와 다른 lock)
            ③ `--policy waiter` (substring 매치 → 통과, 런타임에만 실패)

        그래서 crontab에서 임의성을 없애고 **정본과의 문자열 동일성**만 본다.
        """
        forms = [
            "5 * * * * cd ~/x && ops/cron-with-lock.sh --policy wait -- true && "
            "/usr/bin/docker compose run --rm fastapi python s.py",
            "5 * * * * cd ~/x && LOCK_FILE=/tmp/o.lock ops/cron-with-lock.sh --policy wait -- "
            "/usr/bin/docker compose run --rm fastapi python s.py",
            "5 * * * * cd ~/x && ops/cron-with-lock.sh --policy waiter -- "
            "/usr/bin/docker compose run --rm fastapi python s.py",
        ]
        for form in forms:
            with self.subTest(form=form[:60]):
                tmp = pathlib.Path(__import__("tempfile").mkdtemp())
                rc, out = _Harness(tmp, cron_lines=[form]).run()
                self.assertEqual(rc, 5, out)
                self.assertIn("정본과 다르다", out)

    def test_extra_raw_docker_line_is_refused(self):
        """⛔ 정본 5줄이 **다 있으면서** raw docker 줄이 하나 더 있으면 그 줄은 lock 밖에서 돈다.

        ⚠️ 이 케이스가 없으면 수집 grep에서 `docker compose run` 대안을 빼는 변이가 **생존한다**
        (실측). 정본만 수집하면 추가된 raw 줄이 애초에 비교 대상에 안 들어와 통과한다.
        """
        canon = subprocess.run(["bash", str(REPO / "ops" / "cron-job.sh"), "--print-crontab"],
                               capture_output=True, text=True, check=True).stdout.splitlines()
        extra = ("13 * * * * cd ~/x && /usr/bin/docker compose run --rm fastapi "
                 "python scripts/rogue.py")
        rc, out = _Harness(self.tmp, cron_lines=canon + [extra]).run()
        self.assertEqual(rc, 5, out)
        self.assertIn("정본과 다르다", out)

    def test_partial_canonical_crontab_is_refused(self):
        """정본 일부만 있으면 거부 — 줄이 사라진 것도 잡아야 한다."""
        canon = subprocess.run(["bash", str(REPO / "ops" / "cron-job.sh"), "--print-crontab"],
                               capture_output=True, text=True, check=True).stdout.splitlines()
        rc, out = _Harness(self.tmp, cron_lines=canon[:2]).run()
        self.assertEqual(rc, 5, out)

    def test_crontab_query_failure_is_refused(self):
        """⛔ **fail-open 반례 ②** — 구 버전은 `|| true`가 조회 실패를 **빈 crontab으로 세탁**해
        "0/0 감쌈"으로 통과시켰다(실측). 확인 불가는 거부여야 한다.
        """
        h = _Harness(self.tmp, cron_query_fails=True)
        rc, out = h.run()
        self.assertEqual(rc, 5, out)
        self.assertIn("조회 실패", out)
        self.assertEqual(h.latest, FALLBACK)

    def test_zero_target_lines_is_refused(self):
        """대상 줄이 0이면 crontab이 바뀌었거나 조회가 비었다는 뜻 — 그것도 거부다."""
        h = _Harness(self.tmp, cron_commented=True)
        rc, out = h.run()
        self.assertEqual(rc, 5, out)
        self.assertIn("정본과 다르다", out, "선행 공백 뒤 주석이 활성 cron으로 계산됐다")

    def test_unwrapped_cron_is_refused(self):
        """⛔ crontab이 lock을 잡지 않으면 **배포 쪽만 잡아도 무의미**하다 — 산문 경고가 아니라 게이트다.

        감싸이지 않은 cron은 배포가 lock을 쥐고 있어도 그대로 실행되어 일시적 split에 끼어든다.
        """
        h = _Harness(self.tmp, cron_wrapped=False)
        rc, out = h.run()
        self.assertEqual(rc, 5, out)
        self.assertIn("정본과 다르다", out)
        self.assertEqual(h.latest, FALLBACK, "거부인데 상태가 바뀌었다")

    def test_deploy_refuses_when_lock_is_held(self):
        """⛔ 비대칭 정책 — 배포는 lock 획득 실패 시 **즉시 거부**한다(대기하지 않는다).

        대기하면 긴 cron 뒤에 붙어 예산·타이밍 가정이 전부 깨진다. 반대로 cron은 대기해야 한다
        (`-w 0`이면 daily append가 조용히 건너뛰어 데이터 공백이 된다).
        """
        (self.tmp / "LOCKED").write_text("1", encoding="utf-8")
        h = _Harness(self.tmp)
        rc, out = h.run()
        self.assertEqual(rc, 4, out)
        self.assertIn("쥐고 있다", out)
        self.assertEqual(h.latest, FALLBACK)

    def test_cron_firing_minute_itself_is_refused(self):
        """⛔ **현재 분이 곧 발화 분**인 경우. 실측으로 통과했던 경계다.

        구 산술은 `m > mm`(현재 분 제외) + margin 비교 `<`를 써서 `00:11`을 "54분", `15:01`을
        "4분"으로 판정해 **통과**시켰다. `-ge` / `-le`로 고쳤다.
        """
        for now in ("00:05", "00:07", "00:09", "00:11", "15:01"):
            with self.subTest(now=now):
                h = _Harness(self.tmp)
                rc, out = h.run(now_utc=now)
                self.assertEqual(rc, 3, out)
                self.assertIn("0분 뒤", out)

    def test_cron_window_is_refused_before_any_mutation(self):
        """⛔ retag~recreate 사이의 **일시적 split** 구간에 cron이 겹치면 검증되지 않은 조합이 실제로 돈다.

        실측 crontab(UTC): 매시 :05 :07 :09 :11 + 매일 15:01. 배포 예산(recreate + health 최대 90s)이
        그 발화에 닿을 수 있으면 **상태를 바꾸기 전에** 거부해야 한다.
        ⚠️ 이 가드는 host lock(flock)의 **대체가 아니라 임시 방편**이다 — 예산 초과 배포는 여전히 겹친다.
        """
        # ⛔ `("00:01", 4)` / `("14:57", 4)`는 **LEFT == margin** 인 경계다 — 이 케이스가 없으면
        #    margin 비교를 `-le` → `-lt`로 바꾸는 변이가 **생존한다**(실측으로 생존했다).
        for now, why in [("00:01", ":05까지 정확히 margin(4)분"),
                         ("14:57", "15:01까지 정확히 margin(4)분"),
                         ("00:04", ":05 직전"), ("00:06", ":07 직전"),
                         ("14:58", "15:01 직전"), ("15:00", "15:01 직전")]:
            with self.subTest(now=now, why=why):
                h = _Harness(self.tmp)
                rc, out = h.run(now_utc=now)
                self.assertEqual(rc, 3, out)
                self.assertIn("거부", out)
                self.assertEqual(h.latest, FALLBACK, "거부인데 상태가 바뀌었다")

    def test_outside_cron_window_proceeds(self):
        for now in ("00:12", "00:20", "00:59", "13:30"):
            with self.subTest(now=now):
                tmp = pathlib.Path(__import__("tempfile").mkdtemp())
                rc, out = _Harness(tmp).run(now_utc=now)
                self.assertEqual(rc, 0, out)

    def test_cron_guard_can_be_overridden_loudly(self):
        h = _Harness(self.tmp)
        rc, out = h.run(now_utc="00:04", extra_args=("--allow-cron-window",))
        self.assertEqual(rc, 0, out)
        self.assertIn("allow-cron-window", out, "우회가 조용히 지나갔다")

    def test_happy_path_converges_and_exits_zero(self):
        h = _Harness(self.tmp)
        rc, out = h.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(h.latest, TARGET)
        self.assertEqual(h.running, NEW_ID)

    def test_double_models_the_real_id_format_difference(self):
        """⛔ 더블 자기검사 — `images`(축약)와 `image inspect`(전체)가 **다른 형식**을 주어야 한다.

        더블이 두 명령에 같은 형식을 반환하던 동안 short/full 혼용 결함이 **가려졌다**(실측:
        운영에서 `images`=`48434889c5d6` / `inspect`=`sha256:48434889c5d6…a25563`).
        이 검사가 없으면 누군가 더블을 "단순화"해 그 은폐가 되돌아온다.
        """
        h = _Harness(self.tmp)
        env = dict(os.environ, PATH=f"{h.bin}:{os.environ['PATH']}")
        short = subprocess.run(["docker", "images", TARGET, "--format", "{{.ID}}"],
                               env=env, capture_output=True, text=True).stdout.strip()
        full = subprocess.run(["docker", "image", "inspect", TARGET, "--format", "{{.Id}}"],
                              env=env, capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(short, full, "더블이 형식 차이를 모델링하지 않는다")
        self.assertTrue(full.startswith("sha256:"))
        self.assertFalse(short.startswith("sha256:"))

    def test_missing_target_tag_changes_nothing(self):
        """preflight — 없는 태그로 진행하면 복구 대상도 없어 손쓸 수 없다."""
        h = _Harness(self.tmp, missing=(TARGET,))
        rc, out = h.run()
        self.assertEqual(rc, 1)
        self.assertIn("PREFLIGHT", out)
        self.assertEqual(h.latest, FALLBACK, "상태가 바뀌었다")

    def test_missing_fallback_tag_changes_nothing(self):
        h = _Harness(self.tmp, missing=(FALLBACK,))
        rc, out = h.run()
        self.assertEqual(rc, 1)
        self.assertEqual(h.latest, FALLBACK)

    def test_tag_failure_recovers_and_fails(self):
        """⛔ set -u만 쓰던 구 버전은 여기서 계속 진행해 rc=0을 냈다."""
        h = _Harness(self.tmp, fail=("tag",))
        rc, out = h.run()
        self.assertEqual(rc, 1, out)
        self.assertIn("복구", out)

    def test_recreate_failure_restores_latest(self):
        h = _Harness(self.tmp, fail=("up",))
        rc, out = h.run()
        self.assertEqual(rc, 1, out)
        self.assertEqual(h.latest, FALLBACK, "latest가 신 이미지에 남아 split이 됐다")

    def test_health_ok_but_recreate_not_applied_is_a_failure(self):
        """⛔ **H2** — health만 보면 recreate 미적용을 성공으로 오판한다.

        가짜 docker가 `compose up`을 성공으로 답하되 running image를 바꾸지 않는다.
        health는 계속 OK다. 성공 조건에 image ID 대조가 없으면 이 테스트가 red가 된다.
        """
        h = _Harness(self.tmp, apply_recreate=False)
        rc, out = h.run()
        self.assertEqual(rc, 1, out)
        self.assertIn("수렴", out)

    def test_unhealthy_after_recreate_recovers(self):
        """⚠️ 가짜 응답이 `{"status":"unhealthy"}`다 — `grep -q healthy`는 여기에 **매치되어**
        구 스크립트가 성공으로 오판했다(이 테스트가 실제 결함을 잡았다). 상태 필드 대조가 필요하다.
        """
        h = _Harness(self.tmp, healthy=False)
        rc, out = h.run()
        self.assertEqual(rc, 1, out)
        self.assertEqual(h.latest, FALLBACK)

    def test_recovery_without_retag_is_not_reported_as_success(self):
        """⛔ **복구 성공 판정에 `latest == want`가 빠지면 split을 "복구 완료"로 보고한다.**

        재현(선택적 실패 주입): 배포 recreate가 성공을 답하되 **미적용**(running은 fallback) →
        목표 수렴 실패 → recover 진입 → **retag 실패**(latest는 target에 남는다) +
        **복구 recreate 실패**(running은 fallback). 이때 health OK이고 running == fallback이므로
        `latest`를 보지 않는 판정은 **"복구 완료 — fallback 이미지로 수렴"**을 출력한다.
        그런데 실제 상태는 `latest`=target / `running`=fallback = **cron 신 / 웹 구 split**이다.

        ⚠️ rc는 두 경우 모두 1이다(본문이 이미 exit 1). 그래서 **메시지**를 단언한다 —
        운영자가 "복구 완료"를 읽고 split을 놓치는 것이 이 결함의 실해다.
        """
        h = _Harness(self.tmp, apply_recreate=False, fail_tag_nth=2, fail_up_nth=2)
        rc, out = h.run()
        self.assertEqual(rc, 1, out)
        self.assertNotIn("복구 완료", out, f"split인데 복구 완료로 보고했다:\n{out}")
        self.assertIn("미수렴", out, out)

    def test_sigterm_after_tag_does_not_report_success(self):
        """⛔ **M3** — bash signal trap은 handler 반환 후 **본문을 계속** 실행한다.

        실측: EXIT과 같은 handler를 쓰면 TERM → trap → continued → **rc=0**이 나온다.
        signal handler를 `recover; exit 1`로 분리해야 여기서 red가 나지 않는다.
        """
        h = _Harness(self.tmp, kill_on=signal.SIGTERM)
        rc, out = h.run()
        self.assertNotEqual(rc, 0, f"signal 뒤 성공으로 종료했다: {out}")


if __name__ == "__main__":
    unittest.main()
