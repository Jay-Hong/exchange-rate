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


def _install_ops(root):
    """⚠️ ops 스크립트와 lock 정본을 **함께** 복사한다.

    정본 위치·lock 경로의 env 손잡이를 전부 없앴으므로(별도 프로세스에 전파되지 않아 배제가
    깨지던 결함) 테스트도 **설치 단위**로 격리한다 = 프로덕션과 같은 형태.
    """
    import shutil
    ops = root / "ops"
    ops.mkdir(parents=True, exist_ok=True)
    for name in ("deploy-fastapi.sh", "cron-with-lock.sh", "cron-job.sh", "cron-allowlist.txt"):
        shutil.copy2(REPO / "ops" / name, ops / name)
    (ops / "lock.conf").write_text(f"FXI_DEPLOY_LOCK={root}/deploy.lock\n", encoding="utf-8")
    return ops


def _canonical():
    return subprocess.run(["bash", str(REPO / "ops" / "cron-job.sh"), "--print-crontab"],
                          capture_output=True, text=True, check=True).stdout.splitlines()


def _allowlisted():
    raw = (REPO / "ops" / "cron-allowlist.txt").read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in raw if l.strip() and not l.strip().startswith("#")]
SCRIPT = REPO / "ops" / "deploy-fastapi.sh"

TARGET = "img:new"
FALLBACK = "img:old"
# ⚠️ 실제 docker의 형식 차이를 **그대로 모델링**한다 — 같은 형식을 주면 short/full 혼용 결함이
#    가려진다(실측으로 그렇게 가려졌다). images는 축약, image inspect / inspect는 sha256: 전체.
NEW_SHORT, OLD_SHORT = "newid0000000", "oldid0000000"
NEW_ID = "sha256:" + NEW_SHORT + "a" * 52
BUILT_ID = "sha256:" + "bui1d0000000" + "c" * 52
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
                 cron_lines=None, preseed_tags=None):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        (tmp / "calls").write_text("", encoding="utf-8")
        # ⚠️ lock 경로는 **정본 파일을 갈아끼워** 옮긴다 — 개별 env 손잡이는 없앴다
        #    (양쪽이 따로 움직이면 상호배제가 깨지므로).
        self.ops = _install_ops(tmp)
        if preseed_tags:
            # ⚠️ 태그가 **이미 다른 이미지**를 가리키는 상황을 만든다(immutable 위반 재현).
            import json
            st = {"tags": {TARGET: NEW_ID, FALLBACK: OLD_ID, "img:latest": OLD_ID},
                  "running": OLD_ID, "tagn": 0, "upn": 0}
            st["tags"].update(preseed_tags)
            (tmp / "state.json").write_text(json.dumps(st), encoding="utf-8")
        fake = self.bin / "docker"
        # ⚠️ **태그 테이블을 가진 더블**. 누적된 bash 패치워크(latest 파일 + id override)로는
        #    임의 태그(`…:sha-<gitsha>`)를 해소할 수 없었다 — 준비 단계가 만드는 immutable 태그가
        #    바로 그것이다. 실 docker처럼 name→id 매핑을 들고, `images`는 축약 /
        #    `image inspect`는 전체 ID를 준다(그 형식 차이가 과거 결함의 원인이었다).
        fake.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, os, pathlib, sys
            TMP = pathlib.Path({str(tmp)!r})
            ST = TMP / "state.json"
            FAIL = {list(fail)!r}
            MISSING = {list(missing)!r}
            FAIL_TAG_NTH, FAIL_UP_NTH = {fail_tag_nth}, {fail_up_nth}
            APPLY = {bool(apply_recreate)!r}
            HEALTHY = {bool(healthy)!r}
            NEW, OLD, BUILT = {NEW_ID!r}, {OLD_ID!r}, {BUILT_ID!r}
            if ST.exists():
                s = json.loads(ST.read_text())
            else:
                s = {{"tags": {{{TARGET!r}: NEW, {FALLBACK!r}: OLD, "img:latest": OLD}},
                      "running": OLD, "tagn": 0, "upn": 0}}
            (TMP / "calls").open("a").write(" ".join(sys.argv[1:]) + "\\n")
            def save():
                ST.write_text(json.dumps(s))
            a = sys.argv[1:]
            def out(x):
                print(x)
            if a[:1] == ["images"]:
                i = s["tags"].get(a[1])
                if i: out(i.replace("sha256:", "")[:12])          # 축약 ID
            elif a[:2] == ["image", "inspect"]:
                tag = a[2]
                if tag in MISSING or tag not in s["tags"]:
                    save(); sys.exit(1)
                out(s["tags"][tag])                                # 전체 ID
            elif a[:1] == ["inspect"]:
                out(s["running"])
            elif a[:1] == ["tag"]:
                s["tagn"] += 1
                if "tag" in FAIL or s["tagn"] == FAIL_TAG_NTH:
                    save(); sys.exit(1)
                src = a[1] if a[1].startswith("sha256:") else s["tags"].get(a[1])
                if not src:
                    save(); sys.exit(1)
                s["tags"][a[2]] = src
            elif "build" in a and a[:1] == ["compose"]:
                if "build" in FAIL:
                    save(); sys.exit(1)
                # 별 프로젝트로 빌드하면 **live latest 를 건드리지 않는다**(프로젝트명이 이미지명).
                proj = a[a.index("-p") + 1] if "-p" in a else "exchange-rate"
                s["tags"][f"{{proj}}-fastapi:latest"] = BUILT
                if proj == "exchange-rate":
                    s["tags"]["img:latest"] = BUILT                # 구 경로(같은 프로젝트) 회귀용
            elif a[:2] == ["compose", "up"]:
                s["upn"] += 1
                if "up" in FAIL or s["upn"] == FAIL_UP_NTH:
                    save(); sys.exit(1)
                if APPLY:
                    s["running"] = s["tags"].get("img:latest", s["running"])
            elif a[:2] == ["compose", "exec"]:
                out('{{"status":"healthy","message":"ok"}}' if HEALTHY
                    else '{{"status":"unhealthy","message":"degraded"}}')
            save()
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
            canon = _canonical() + _allowlisted()
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
            extra_args=(), env_extra=None):
        env = dict(
            os.environ,
            NOW_UTC=now_utc,
            PATH=f"{self.bin}:{os.environ['PATH']}",
            DOCKER_BIN="docker",
            HEALTH_RETRIES="2",
            HEALTH_SLEEP="0",
            COMPOSE_DIR=str(self.tmp),
            LATEST_TAG="img:latest",
            FLOCK_BIN=str(self.bin / "flock"),
            CRONTAB_BIN=str(self.bin / "crontab"),
            **(env_extra or {}),
        )
        proc = subprocess.Popen(
            ["bash", str(self.ops / "deploy-fastapi.sh"),
             *(("--target", target, "--emergency") if target else ()),
             "--fallback", fallback, *extra_args],
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
    def calls(self):
        f = self.tmp / "calls"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def _state(self):
        """상태 파일이 없으면 **초기 상태**를 돌려준다 — docker 호출이 0이면 상태 무변화다.

        ⚠️ 빈 dict를 돌려주면 "거부인데 상태가 바뀌었나" 단언이 `None != FALLBACK`으로 오탐한다.
        """
        import json
        f = self.tmp / "state.json"
        if not f.exists():
            return {"tags": {TARGET: NEW_ID, FALLBACK: OLD_ID, "img:latest": OLD_ID},
                    "running": OLD_ID}
        return json.loads(f.read_text())

    @property
    def latest(self):
        """latest 가 가리키는 **태그 이름**(구 하네스와 같은 의미를 유지한다)."""
        st = self._state()
        lid = st.get("tags", {}).get("img:latest")
        for name, i in st.get("tags", {}).items():
            if i == lid and name in (TARGET, FALLBACK):
                return name
        return lid

    @property
    def running(self):
        return self._state().get("running")


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
                self.assertIn("선언된 집합과 다르다", out)

    def test_extra_raw_docker_line_is_refused(self):
        """⛔ 정본 5줄이 **다 있으면서** raw docker 줄이 하나 더 있으면 그 줄은 lock 밖에서 돈다.

        ⚠️ 이 케이스가 없으면 수집 grep에서 `docker compose run` 대안을 빼는 변이가 **생존한다**
        (실측). 정본만 수집하면 추가된 raw 줄이 애초에 비교 대상에 안 들어와 통과한다.
        """
        for form, why in [
            ("/usr/bin/docker compose run --rm fastapi python rogue.py", "직접"),
            ("/usr/bin/docker  compose run --rm  fastapi python rogue.py", "공백 2개"),
            ("/usr/bin/docker-compose run --rm fastapi python rogue.py", "docker-compose"),
            ("bash -c 'exec /usr/bin/docker compose run --rm fastapi python rogue.py'", "bash -c"),
        ]:
            with self.subTest(why=why):
                extra = f"13 * * * * cd ~/x && {form}"
                tmp = pathlib.Path(__import__("tempfile").mkdtemp())
                rc, out = _Harness(tmp, cron_lines=_canonical() + _allowlisted() + [extra]).run()
                self.assertEqual(rc, 5, out)
                self.assertIn("선언되지 않은", out)

    def test_missing_allowlist_file_is_refused(self):
        """⛔ allowlist 파일이 없으면 **확인 불가**다 → 거부(실측: 존재 검사 제거 변이가 생존했다)."""
        h = _Harness(self.tmp)
        (h.ops / "cron-allowlist.txt").unlink()
        rc, out = h.run()
        self.assertEqual(rc, 5, out)
        self.assertIn("allowlist 파일이 없다", out)

    def test_partial_canonical_crontab_is_refused(self):
        """정본 일부만 있으면 거부 — 줄이 사라진 것도 잡아야 한다."""
        rc, out = _Harness(self.tmp, cron_lines=_canonical()[:2] + _allowlisted()).run()
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
        self.assertIn("선언된 집합과 다르다", out, "선행 공백 뒤 주석이 활성 cron으로 계산됐다")

    def test_unwrapped_cron_is_refused(self):
        """⛔ crontab이 lock을 잡지 않으면 **배포 쪽만 잡아도 무의미**하다 — 산문 경고가 아니라 게이트다.

        감싸이지 않은 cron은 배포가 lock을 쥐고 있어도 그대로 실행되어 일시적 split에 끼어든다.
        """
        h = _Harness(self.tmp, cron_wrapped=False)
        rc, out = h.run()
        self.assertEqual(rc, 5, out)
        self.assertIn("선언된 집합과 다르다", out)
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

    def test_tag_failure_fails_without_rolling_back(self):
        """`docker tag` 가 실패하면 `latest` 는 **바뀌지 않았다** → 되돌릴 것이 없다.

        ⛔ 구 테스트는 여기서 `"복구"` 를 단언해 **과잉 롤백을 계약으로 고정**하고 있었다.
        상태 변경 전 실패에 운영을 되돌리고 재시작하는 것이 바로 이번에 고친 결함이다.
        (원래 이 테스트의 목적은 `set -u` 만 쓰던 구 버전이 계속 진행해 rc=0 을 내던 것을 잠그는
         것이었고, 그 부분은 rc=1 단언이 그대로 유지한다.)
        """
        h = _Harness(self.tmp, fail=("tag",))
        rc, out = h.run()
        self.assertEqual(rc, 1, out)
        self.assertIn("되돌리지 않는다", out)
        self.assertEqual(h.latest, FALLBACK, "latest 가 바뀌었다")
        self.assertNotIn("compose up", h.calls, "상태 변경 전인데 재시작했다")

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


class TestPrepareUnderLock(unittest.TestCase):
    """⛔ **준비(pull·build)도 lock 안이어야 한다.**

    compose의 fastapi 서비스는 `image:` 키가 없어 `docker compose build`가 **`latest`를 움직인다**
    (실측: 2026-07-30 배포에서 build 직후 latest=신 / running=구 = split). 그 순간 cron은 신
    이미지, web은 구 이미지다. `git pull`도 host의 launcher를 즉시 바꾼다.
    그래서 lock을 쥔 뒤 pull·build를 하고, build가 움직인 `latest`를 **즉시 복원**한다.
    """

    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory(); self.addCleanup(self._td.cleanup)
        self.tmp = pathlib.Path(self._td.name)

    FULL_SHA = "d" * 40

    def _git(self, *, fetch_ok=True, runtime_changed=False, worktree_ok=True, dirty=False):
        """git 더블 — fetch/rev-parse FETCH_HEAD/worktree/diff/pull 을 모델링한다.

        ⚠️ live worktree를 **pull하지 않는** 것이 이 경로의 계약이므로, `pull` 호출 여부와 시점을
        기록해 테스트가 관측한다(준비 단계에서 불리면 계약 위반).
        """
        # ⚠️ 이름을 `git`으로 두면 PATH 로 해소돼 `GIT_BIN` 주입이 **검증되지 않는다**
        #    (실측: env_extra 가 env 에 안 들어갔는데도 테스트가 통과했다).
        g = self.tmp / "bin" / "git-fake"
        g.parent.mkdir(parents=True, exist_ok=True)
        g.write_text(f"""#!/usr/bin/env bash
echo "$*" >> {self.tmp}/git_calls
case "$1" in
  status)    {'echo " M ops/cron-job.sh"' if dirty else ':'} ;;
  fetch)     {"exit 0" if fetch_ok else "exit 1"} ;;
  rev-parse)
    # ⚠️ `--short` 를 **존중**한다 — 무시하면 "축약 SHA로 되돌리는" 변이가 보이지 않는다(실측 생존).
    if [ "$2" = "--short" ]; then echo {self.FULL_SHA[:7]}; else echo {self.FULL_SHA}; fi ;;
  diff)
    # ⚠️ pathspec 을 **존중**한다 — 무시하면 `_RUNTIME_PATHS` 를 좁히는 변이가 보이지 않는다
    #    (실측 생존). 변경은 `ops/cron-job.sh` 에 있다고 모델링한다.
    if [ "{runtime_changed}" = "True" ]; then
      for p in "$@"; do
        case "$p" in ops|ops/cron-job.sh) exit 1 ;; esac
      done
    fi
    exit 0 ;;
  worktree)  {"exit 0" if worktree_ok else "exit 1"} ;;
esac
exit 0
""", encoding="utf-8")
        g.chmod(0o755); return g

    @property
    def git_calls(self):
        f = self.tmp / "git_calls"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def test_prepare_uses_full_sha_and_never_touches_live_latest(self):
        """⛔ 준비는 **별 worktree + 별 compose 프로젝트**로 빌드해 live `latest`를 안 건드린다.

        구 구현은 live worktree에서 `docker compose build`를 돌려 `latest`를 움직인 뒤 복원했다 —
        복원 사이 창이 있었고, 무엇보다 **pull이 launcher를 먼저 바꿔** 실패 시 세대가 갈렸다.
        태그는 **전체 SHA**다(축약은 충돌 가능하고 재빌드가 기존 태그를 덮는다).
        """
        h = _Harness(self.tmp)
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git())})
        self.assertEqual(rc, 0, out)
        self.assertIn(f"sha-{self.FULL_SHA}", out, "전체 SHA 태그를 쓰지 않았다")
        self.assertNotIn("복원", out, "live latest 를 움직였다(별 프로젝트 빌드가 아니다)")
        self.assertIn("-p fxi-build", h.calls, "별 compose 프로젝트로 빌드하지 않았다")
        self.assertIn("worktree add", self.git_calls, "별 worktree 를 쓰지 않았다")

    def test_live_worktree_is_never_pulled(self):
        """⛔ pull을 **트랜잭션에서 뺀다** — 넣으면 pull 성공 직후 signal에서 세대가 갈린다.

        구 구현은 수렴 뒤 pull했고, 그 창에서 `recover()`는 이미지만 되돌려 **신 launcher + 구
        이미지**가 남았다(실측 지적). 소스 롤백은 실행 중 스크립트를 덮어쓰므로 트랜잭션에 넣을 수
        없다 → preflight가 runtime 파일 변경을 거부하는 것으로 대체했다.
        """
        h = _Harness(self.tmp)
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git())})
        self.assertEqual(rc, 0, out)
        calls = [l.split()[0] for l in self.git_calls.strip().splitlines()]
        self.assertNotIn("pull", calls, f"live worktree 를 pull했다: {calls}")
        self.assertIn("그대로 둔다", out)

    def test_build_failure_leaves_live_worktree_untouched(self):
        """실패 시 live는 손대지 않은 상태(구 launcher + 구 이미지 = **일관**)로 남아야 한다."""
        h = _Harness(self.tmp, fail=("up",))
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git())})
        self.assertEqual(rc, 1, out)
        self.assertNotIn("pull", self.git_calls, "실패했는데 live worktree 를 pull했다")

    def test_ref_that_changes_runtime_files_is_refused(self):
        """⛔ **runtime이 live worktree에서 읽는 파일**(`ops/`·`docker-compose.yml`)이 바뀌면 거부.

        이미지만 원자적으로 바꿀 수 있으므로, 그 파일들이 바뀌면 "이미지는 신 세대 / 그 파일들은
        구 세대"가 되고 한 트랜잭션에 넣을 방법이 없다 → host-config migration이 필요하다.
        ⚠️ 이 거부가 두 High(정본 변경 미검증 / pull-signal 창)를 **함께** 닫는다.
        """
        h = _Harness(self.tmp)
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git(runtime_changed=True))})
        self.assertEqual(rc, 1, out)
        self.assertIn("runtime이 읽는 파일", out)
        self.assertIn("host-config migration", out)
        self.assertNotIn("worktree add", self.git_calls, "거부인데 빌드를 시작했다")

    def test_fetch_failure_is_refused_before_any_build(self):
        h = _Harness(self.tmp)
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git(fetch_ok=False))})
        self.assertEqual(rc, 1, out)
        self.assertIn("fetch", out)

    def test_worktree_failure_is_refused(self):
        h = _Harness(self.tmp)
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git(worktree_ok=False))})
        self.assertEqual(rc, 1, out)
        self.assertIn("worktree", out)

    def test_temp_worktree_is_cleaned_when_build_itself_fails(self):
        """⛔ build가 실패하면 inline 정리에 **도달하지 못한다** — trap만이 정리할 수 있다.

        ⚠️ `fail=("up",)`로는 이 성질이 안 잡힌다(build 성공 후 inline `cleanup_worktree`가 이미
        불려 관측이 가려진다 — 실측: recover 의 정리를 제거하는 변이가 생존했다).
        """
        h = _Harness(self.tmp, fail=("build",))
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git())})
        self.assertEqual(rc, 1, out)
        self.assertIn("worktree remove", self.git_calls,
                      "build 실패 경로에서 worktree 를 정리하지 않았다(trap 미연결)")

    def test_temp_worktree_is_cleaned_on_failure(self):
        """⛔ build 중 실패·종료 시 임시 worktree 정리가 **종료 처리에 연결**돼야 한다.

        연결되지 않으면 /tmp 디렉터리와 git worktree metadata가 남는다(실측 지적).
        """
        h = _Harness(self.tmp, fail=("up",))
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git())})
        self.assertEqual(rc, 1, out)
        self.assertIn("worktree remove", self.git_calls,
                      "실패 경로에서 worktree 를 정리하지 않았다")

    def test_existing_tag_pointing_elsewhere_is_refused(self):
        """⛔ 같은 SHA 태그가 **이미 다른 이미지**를 가리키면 덮어쓰지 않고 거부한다.

        덮어쓰면 immutable의 의미가 사라진다 — 같은 SHA가 다른 결과를 낸다는 신호(dirty tree·
        base image 변동)를 조용히 지워 버린다.
        ⚠️ 이 케이스가 없으면 그 거부를 제거하는 변이가 **생존한다**(실측).
        """
        tag = f"exchange-rate-fastapi:sha-{self.FULL_SHA}"
        h = _Harness(self.tmp, preseed_tags={tag: OLD_ID})
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git())})
        self.assertEqual(rc, 1, out)
        self.assertIn("immutable 위반", out)

    def test_pre_mutation_refusals_do_not_roll_back_production(self):
        """⛔ **상태를 바꾸기 전 거부가 운영을 되돌려선 안 된다.**

        실측 결함: trap이 준비 단계 **전에** 무장돼 fetch 실패·runtime 변경 거부·worktree 생성 실패가
        모두 `recover()`로 들어가 `tag fallback latest` + `compose up --force-recreate`를 호출했다 —
        **잘못된 ref나 네트워크 오류만으로 운영을 되돌리고 재시작**했다. 안전장치가 위험원이 됐다.
        cleanup은 항상 하되 이미지 롤백은 `latest` 전환 성공 뒤에만 활성화한다.
        """
        for label, kw in [("fetch 실패", dict(fetch_ok=False)),
                          ("runtime 변경", dict(runtime_changed=True)),
                          ("worktree 실패", dict(worktree_ok=False)),
                          ("dirty runtime", dict(dirty=True))]:
            with self.subTest(label=label):
                tmp = pathlib.Path(__import__("tempfile").mkdtemp())
                h = _Harness(tmp)
                rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                                env_extra={"GIT_BIN": str(self._git(**kw))})
                self.assertEqual(rc, 1, out)
                self.assertNotIn("tag img:old img:latest", h.calls,
                                 f"{label}: 상태 변경 전인데 롤백했다")
                self.assertNotIn("compose up", h.calls,
                                 f"{label}: 상태 변경 전인데 컨테이너를 재시작했다")
                self.assertIn("되돌리지 않는다", out)

    def test_dirty_runtime_files_are_refused(self):
        """⛔ 커밋 비교만으로는 **live worktree의 로컬 수정본**을 놓친다 — 무엇이 도는지 확정 불가."""
        h = _Harness(self.tmp)
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git(dirty=True))})
        self.assertEqual(rc, 1, out)
        self.assertIn("수정돼 있다", out)
        self.assertNotIn("worktree add", self.git_calls, "거부인데 빌드를 시작했다")

    def test_target_without_emergency_is_refused(self):
        """⛔ `--target` 은 runtime 동일성 검사를 **우회**한다 → 긴급 롤백 전용으로 격리한다."""
        h = _Harness(self.tmp)
        proc = __import__("subprocess").run(
            ["bash", str(h.ops / "deploy-fastapi.sh"), "--target", TARGET, "--fallback", FALLBACK],
            env=dict(__import__("os").environ, PATH=f"{h.bin}:{__import__('os').environ['PATH']}",
                     DOCKER_BIN="docker", NOW_UTC="00:20", COMPOSE_DIR=str(self.tmp),
                     LATEST_TAG="img:latest", FLOCK_BIN=str(h.bin / "flock"),
                     CRONTAB_BIN=str(h.bin / "crontab"), HEALTH_RETRIES="2", HEALTH_SLEEP="0"),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("긴급 롤백 전용", proc.stdout + proc.stderr)

    def test_prepare_and_target_together_are_refused(self):
        h = _Harness(self.tmp)
        rc, out = h.run(extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git())})
        self.assertEqual(rc, 2, out)

    def test_prepare_happens_after_the_lock_is_held(self):
        """⛔ lock을 못 잡으면 fetch도 build도 하지 않는다."""
        (self.tmp / "LOCKED").write_text("1", encoding="utf-8")
        h = _Harness(self.tmp)
        rc, out = h.run(target=None, extra_args=("--prepare-from", "master"),
                        env_extra={"GIT_BIN": str(self._git())})
        self.assertEqual(rc, 4, out)
        self.assertNotIn("[prepare]", out, "lock 없이 준비를 시작했다")
        self.assertEqual(self.git_calls.strip(), "", "lock 없이 git을 만졌다")


if __name__ == "__main__":
    unittest.main()
