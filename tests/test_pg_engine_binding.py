"""S2-PG0 — **실제 PostgreSQL** 에 대고 `app/database.py` 의 engine 배선을 증명한다.

## 왜 이 파일이 필요한가

`tests/conftest.py` 는 모든 테스트의 `DATABASE_URL` 을 **file-backed SQLite 로 덮는다**(RDS 연결
회피). 그래서 PostgreSQL 전용 설정(`statement_timeout` · `connect_timeout` · 풀 고갈 거동)은
지금까지 **CI 에서 한 번도 실행되지 않았다**. 기존 `test_pool_timeout_is_503` 도
`sqlalchemy.exc.TimeoutError` 를 **직접 주입**해 HTTP 분류만 증명하고 풀 설정은 증명하지 않는다.

⛔ 그래서 in-process 로 별 engine 을 만들어 시험하면 **여전히 공허하다** — production 이
   `create_engine()` 에 실제로 그 kwargs 를 넘기는지는 증명되지 않는다. 이 파일은
   **새 프로세스에서 `DATABASE_URL=PG_TEST_URL` 로 `app.database` 를 최초 import** 해
   그 배선 자체를 본다.

## fail-closed

⛔ CI 에서 `PG_TEST_URL` 이 없으면 **skip 이 아니라 fail** 이다. skip 으로 두면 workflow 의
   service 를 지우거나 env 배선이 끊겨도 **전부 skip 되어 green** 이 된다 — 계약이 사라진 것을
   아무도 모른다. 로컬(비-CI)에서만 skip 한다.
⛔ `PG_TEST_URL` 이 있는데 접속이 안 되면 어디서든 fail 이다. "설정했는데 못 붙었다" 는
   skip 사유가 아니라 결함이다.
"""
import os
import pathlib
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
PG_URL = os.getenv("PG_TEST_URL", "").strip()
ON_CI = os.getenv("GITHUB_ACTIONS", "").lower() == "true"


def _require_pg() -> str:
    """PG URL 을 돌려주거나 **fail-closed** 한다. 로컬에서만 skip."""
    if PG_URL:
        return PG_URL
    if ON_CI:
        raise AssertionError(
            "CI 인데 PG_TEST_URL 이 없다 — workflow 의 postgres service 또는 env 배선이 끊겼다. "
            "skip 하면 계약이 사라진 것을 아무도 모른다."
        )
    raise unittest.SkipTest("로컬: PG_TEST_URL 미설정 (CI 에서는 fail)")


def _run_in_fresh_process(code: str, *, url: str,
                          profile: str | None = None) -> subprocess.CompletedProcess:
    """⛔ **새 프로세스**여야 한다 — conftest 가 이미 `DATABASE_URL` 을 SQLite 로 덮었고,
    `app.database` 는 import 시점에 engine 을 만든다. 같은 프로세스에서는 되돌릴 수 없다."""
    env = dict(os.environ, DATABASE_URL=url, PYTHONPATH=str(REPO))
    env.pop("PG_TEST_URL", None)
    env.pop("DB_WORKLOAD_PROFILE", None)      # 부모 환경이 새지 않게 명시적으로 지운다
    if profile is not None:
        env["DB_WORKLOAD_PROFILE"] = profile
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=REPO, env=env, timeout=120)


class TestFailClosedWiring(unittest.TestCase):
    """게이트 자체가 살아 있는지 — 이게 죽으면 아래 전부가 조용히 무의미해진다."""

    def test_ci_without_pg_url_is_a_failure_not_a_skip(self):
        """⛔ 판정 함수의 **fail-closed 분기**를 직접 시험한다.

        이 단언이 없으면 "CI 에서 fail 한다" 는 주석일 뿐 기전이 아니다.
        """
        import unittest.mock as mock

        with mock.patch.object(sys.modules[__name__], "PG_URL", ""), \
             mock.patch.object(sys.modules[__name__], "ON_CI", True):
            with self.assertRaises(AssertionError):
                _require_pg()

    def test_local_without_pg_url_skips(self):
        import unittest.mock as mock

        with mock.patch.object(sys.modules[__name__], "PG_URL", ""), \
             mock.patch.object(sys.modules[__name__], "ON_CI", False):
            with self.assertRaises(unittest.SkipTest):
                _require_pg()

    def test_conftest_still_forces_sqlite(self):
        """⛔ 이 파일이 존재하는 **이유**를 잠근다.

        누군가 conftest 의 SQLite 강제를 없애면 이 파일의 subprocess 우회는 불필요해지지만,
        동시에 다른 모든 테스트가 실제 DB 에 붙게 된다 — 그 변화를 조용히 넘기지 않는다.
        """
        self.assertTrue(os.environ.get("DATABASE_URL", "").startswith("sqlite:"),
                        "conftest 가 더 이상 SQLite 를 강제하지 않는다 — 이 파일의 전제가 바뀌었다")


class TestProductionEngineBindsToPostgres(unittest.TestCase):
    """`app/database.py` 가 **실제로** PostgreSQL 경로를 타는지."""

    def setUp(self):
        self.url = _require_pg()

    def test_fresh_import_reaches_the_server_and_reports_the_pool(self):
        """⛔ engine 객체만 보는 게 아니라 **연결까지** 한다 — kwargs 는 맞는데 못 붙는
        상태를 초록으로 넘기지 않는다."""
        code = (
            "from app.database import engine\n"
            "from sqlalchemy import text\n"
            "with engine.connect() as c:\n"
            "    v = c.execute(text('SHOW server_version')).scalar()\n"
            "print('DIALECT=' + engine.dialect.name)\n"
            "print('POOL_SIZE=' + str(engine.pool.size()))\n"
            "print('OVERFLOW=' + str(engine.pool._max_overflow))\n"
            "print('SERVER=' + str(v))\n"
        )
        r = _run_in_fresh_process(code, url=self.url)
        self.assertEqual(r.returncode, 0, f"production engine import 실패\n{r.stderr[-1500:]}")
        out = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        self.assertEqual(out.get("DIALECT"), "postgresql", out)
        # ⛔ 풀 크기는 이 슬라이스의 **고정 전제**다 — 상한과 용량을 같이 바꾸면 개선의
        #    귀속이 불가능해진다(capacity 재산정은 별 슬라이스).
        self.assertEqual(out.get("POOL_SIZE"), "3", out)
        self.assertEqual(out.get("OVERFLOW"), "2", out)
        self.assertTrue(out.get("SERVER", "").startswith("17."),
                        f"운영 RDS 는 17.x 다 — 다른 major 에서 증명하면 계약이 아니다: {out}")


class TestWorkloadProfileReachesTheServer(unittest.TestCase):
    """S2 배선 증명 — production `app/database.py` 가 timeout 3종을 **실제로** 넘기는가.

    ⚠️ 처음엔 `connect_timeout` 을 "같은 connect_args dict 에 실리니 statement_timeout 이
       도달하면 함께 배선된 것" 이라는 **합성 논증**으로 덮으려 했다. 그럴 필요가 없었다 —
       실측으로 libpq 가 `conn.info.get_parameters()` 로 값을 **되돌려준다**(미지정이면 None).
       합성 논증보다 직접 관측이 있으면 직접 관측을 쓴다.
    """

    #: profile → (SHOW statement_timeout 기대값, connect_timeout, pool_timeout)
    #: ⚠️ 표기는 **추측하지 않고 확인**했다 — PostgreSQL 은 60000ms 를 `'1min'`,
    #:    900000ms 를 `'15min'` 으로 돌려준다(실측).
    EXPECTED = {
        "online": ("1min", "5", 10),
        "maintenance": ("15min", "10", 30),
    }

    def setUp(self):
        self.url = _require_pg()

    def _probe(self, profile):
        code = (
            "from sqlalchemy import text\n"
            "from app.database import engine, DB_WORKLOAD_PROFILE\n"
            "with engine.connect() as c:\n"
            "    st = c.execute(text('SHOW statement_timeout')).scalar()\n"
            "    p = c.connection.dbapi_connection.info.get_parameters()\n"
            "print('PROFILE=' + DB_WORKLOAD_PROFILE)\n"
            "print('STATEMENT=' + str(st))\n"
            "print('CONNECT=' + str(p.get('connect_timeout')))\n"
            "print('POOL=' + str(engine.pool._timeout))\n"
        )
        r = _run_in_fresh_process(code, url=self.url, profile=profile)
        self.assertEqual(r.returncode, 0, f"profile={profile} 배선 실패\n{r.stderr[-1500:]}")
        return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)

    def test_each_profile_lands_all_three_timeouts_on_a_live_connection(self):
        for profile, (stmt, conn_to, pool_to) in self.EXPECTED.items():
            with self.subTest(profile=profile):
                out = self._probe(profile)
                self.assertEqual(out.get("PROFILE"), profile, out)
                self.assertEqual(out.get("STATEMENT"), stmt,
                                 f"서버가 보고한 statement_timeout 이 다르다: {out}")
                self.assertEqual(out.get("CONNECT"), conn_to,
                                 f"libpq 가 되돌려준 connect_timeout 이 다르다: {out}")
                self.assertEqual(out.get("POOL"), str(pool_to), out)

    def test_the_two_profiles_actually_differ_on_the_server(self):
        """⛔ 값이 같으면 profile 은 이름만 있는 장식이다."""
        online = self._probe("online")
        maint = self._probe("maintenance")
        self.assertNotEqual(online["STATEMENT"], maint["STATEMENT"])
        self.assertNotEqual(online["POOL"], maint["POOL"])

    def test_absent_profile_defaults_to_online_on_the_server(self):
        out = self._probe(None)
        self.assertEqual(out.get("PROFILE"), "online", out)
        self.assertEqual(out.get("STATEMENT"), self.EXPECTED["online"][0], out)

    def test_statement_timeout_actually_cancels_a_long_query(self):
        """⛔ `SHOW` 가 값을 보고하는 것과 서버가 **실제로 취소하는 것**은 다른 주장이다.

        online 상한(10s)보다 긴 `pg_sleep` 이 SQLSTATE **57014**(query_canceled)로 끊겨야 한다.
        상한을 짧게 덮어써 CI 시간을 태우지 않는다 — 덮어쓰기는 production 배선을 우회하므로
        **위 배선 시험과 짝을 이룰 때만** 의미가 있다.
        """
        code = (
            "from sqlalchemy import text\n"
            "from app.database import engine\n"
            "with engine.connect() as c:\n"
            "    c.execute(text(\"SET statement_timeout = '1200ms'\"))\n"
            "    try:\n"
            "        c.execute(text('SELECT pg_sleep(5)'))\n"
            "        print('SQLSTATE=none')\n"
            "    except Exception as exc:\n"
            "        orig = getattr(exc, 'orig', None)\n"
            "        print('SQLSTATE=' + str(getattr(orig, 'sqlstate', None)))\n"
        )
        r = _run_in_fresh_process(code, url=self.url, profile="online")
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertIn("SQLSTATE=57014", r.stdout,
                      f"긴 질의가 취소되지 않았다 — statement_timeout 이 서버에서 무의미하다: {r.stdout}")


class TestInvalidProfileIsAStartupFailure(unittest.TestCase):
    """⛔ 조용한 online 폴백은 maintenance 작업을 **중간까지만 쓰고** 자른다 — 미실행보다 나쁘다.

    PG 가 필요 없다(engine 생성 전에 죽어야 하므로). 그래서 `_require_pg()` 를 걸지 않는다.
    """

    BAD = ("", " ", "Online", "ONLINE", "maintenence", "maint", "online ")

    def test_bad_profiles_kill_the_import_before_the_engine_exists(self):
        for bad in self.BAD:
            with self.subTest(profile=bad):
                r = _run_in_fresh_process("import app.database\nprint('IMPORTED=1')\n",
                                          url="sqlite:///:memory:", profile=bad)
                self.assertNotEqual(r.returncode, 0, f"{bad!r} 로 import 가 성공했다")
                self.assertNotIn("IMPORTED=1", r.stdout)
                self.assertIn("InvalidDbWorkloadProfile", r.stderr,
                              f"다른 이유로 죽었다 — 원인이 profile 이 아니다: {r.stderr[-600:]}")

    def test_valid_profiles_import_cleanly_on_sqlite_too(self):
        """profile 은 SQLite 에서도 **검증**된다(로컬에서 오타를 미리 잡는다)."""
        for good in ("online", "maintenance", None):
            with self.subTest(profile=good):
                r = _run_in_fresh_process("import app.database\nprint('IMPORTED=1')\n",
                                          url="sqlite:///:memory:", profile=good)
                self.assertEqual(r.returncode, 0, r.stderr[-800:])
                self.assertIn("IMPORTED=1", r.stdout)


class TestSqlitePathNeedsNoPostgres(unittest.TestCase):
    """⛔ SQLite 계약은 **PG 없이도 돌아야 한다**.

    초판은 이 둘을 `TestProductionEngineBindsToPostgres` 안에 두어 `setUp` 의 `_require_pg()`
    가 클래스 전체에 걸렸다 — PG 가 없는 환경(로컬 기본)에서 **두 계약이 통째로 skip** 됐다
    (실측: SKIPPED 2). PG 요구는 **실제 PostgreSQL 을 쓰는 테스트 하나에만** 건다(codex).
    """

    def test_sqlite_path_does_not_receive_postgres_only_kwargs(self):
        """⛔ 반대 방향 회귀 — PostgreSQL 전용 옵션이 SQLite 경로로 새면 로컬이 죽는다.

        ⚠️ 초판은 **공허했다**(codex, 재현 확인). `create_connect_args(engine.url)` 은 **URL 에서만**
           인자를 도출해 `create_engine(connect_args=…)` 로 넘긴 것을 보지 못한다 — 실측으로
           내가 준 `check_same_thread=False` 조차 `True` 로 나왔다. 그래서 `statement_timeout` 을
           SQLite 로 흘려도 그 검사는 통과하고, 실제로는 **연결을 열 때** `TypeError` 가 난다.
           판정을 **실제 연결**로 바꾼다.
        """
        code = (
            "from sqlalchemy import text\n"
            "from app.database import engine\n"
            "with engine.connect() as c:\n"
            "    c.execute(text('select 1'))\n"
            "print('DIALECT=' + engine.dialect.name)\n"
            "print('CONNECT_OK=1')\n"
        )
        r = _run_in_fresh_process(code, url="sqlite:///:memory:")
        self.assertEqual(r.returncode, 0,
                         f"SQLite 경로가 실제 연결에서 죽는다 — PG 전용 인자 누수 의심\n{r.stderr[-1200:]}")
        self.assertIn("DIALECT=sqlite", r.stdout)
        self.assertIn("CONNECT_OK=1", r.stdout)

    def test_the_sqlite_leak_check_actually_bites(self):
        """⛔ 위 검사가 **무는지**를 합성 반례로 증명한다 — 공허한 판정식을 한 번 썼기 때문이다."""
        code = (
            "from sqlalchemy import create_engine, text\n"
            "eng = create_engine('sqlite:///:memory:',\n"
            "                    connect_args={'check_same_thread': False,\n"
            "                                  'options': '-c statement_timeout=1000'})\n"
            "with eng.connect() as c:\n"
            "    c.execute(text('select 1'))\n"
            "print('CONNECT_OK=1')\n"
        )
        r = _run_in_fresh_process(code, url="sqlite:///:memory:")
        self.assertNotEqual(r.returncode, 0, "PG 전용 인자를 흘렸는데 연결이 성공했다 — 검사가 공허하다")
        self.assertIn("TypeError", r.stderr)


if __name__ == "__main__":
    unittest.main()
