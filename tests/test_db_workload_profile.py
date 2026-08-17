"""S2 — `DB_WORKLOAD_PROFILE` 판정과 engine kwargs 도출의 **순수 계약**.

⚠️ 이 파일만으로는 production 이 그 kwargs 를 실제로 쓴다는 것을 증명하지 못한다. 배선 증명은
   `tests/test_pg_engine_binding.py` 의 **새 프로세스 + 실연결** 쪽이다. 순수 함수 시험만 두고
   "계약이 잠겼다" 고 말하면 그게 이 트랙에서 7번 반복한 공허한 판정이다.
"""
import ast
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app import database_settings as ds  # noqa: E402


class TestProfileResolution(unittest.TestCase):
    def test_absent_means_online(self):
        self.assertEqual(ds.resolve_profile(None), ds.ONLINE)
        self.assertEqual(ds.resolve_profile_from_env({}), ds.ONLINE)

    def test_an_injected_mapping_wins_over_os_environ(self):
        """⛔ 초판은 깨끗한 환경에서만 돌아 **항상 참**이었다.

        `env = environ or os.environ` (전형적 falsy 버그 — 빈 매핑이 실제 환경으로 폴백)은
        부모에 값이 **있을 때만** 드러난다. 그런데 online 은 이 변수의 *부재*로 결정되므로
        (운영 `.env` 실측 0건) 실제 환경에서는 영원히 안 드러난다. 그래서 반대로 세팅한다.
        """
        with mock.patch.dict(os.environ, {ds.ENV_VAR: ds.MAINTENANCE}):
            self.assertEqual(ds.resolve_profile_from_env({}), ds.ONLINE,
                             "주입한 빈 매핑이 os.environ 으로 폴백했다")
            self.assertEqual(ds.resolve_profile_from_env(), ds.MAINTENANCE,
                             "미주입일 때는 os.environ 을 봐야 한다")

    def test_exact_values_are_accepted(self):
        self.assertEqual(ds.resolve_profile("online"), ds.ONLINE)
        self.assertEqual(ds.resolve_profile("maintenance"), ds.MAINTENANCE)
        self.assertEqual(
            ds.resolve_profile_from_env({ds.ENV_VAR: "maintenance"}), ds.MAINTENANCE)

    def test_everything_else_is_a_startup_failure(self):
        """⛔ 관용(`.lower()`/`.strip()`)을 넣으면 **틀린 값이 정상처럼 통과**한다.

        빈 문자열을 미지정으로 보지 않는 이유: M0a manifest 는 빈 값을 만들지 않는다 —
        빈 값은 누군가 절반만 고친 흔적이다.
        """
        for bad in ("", " ", "Online", "ONLINE", "online ", " online", "Maintenance",
                    "maintenence", "maint", "prod", "0", "true", "online,maintenance"):
            with self.subTest(raw=bad):
                with self.assertRaises(ds.InvalidDbWorkloadProfile):
                    ds.resolve_profile(bad)

    def test_the_error_names_the_variable_and_the_allowed_values(self):
        """진단 가능성 — 기동이 죽었을 때 무엇을 고쳐야 하는지 메시지 하나로 알아야 한다."""
        with self.assertRaises(ds.InvalidDbWorkloadProfile) as ctx:
            ds.resolve_profile("maintenence")
        msg = str(ctx.exception)
        self.assertIn(ds.ENV_VAR, msg)
        self.assertIn("maintenence", msg, "틀린 값 자체가 메시지에 없다")
        for allowed in ds.PROFILES:
            self.assertIn(allowed, msg)


class TestBackendDiscrimination(unittest.TestCase):
    """⛔ `"sqlite" in url` 회귀를 막는다."""

    def test_a_postgres_url_whose_name_contains_sqlite_is_not_sqlite(self):
        """구 구현이 오판하던 **정확한 반례**다.

        오판하면 PG 전용 인자(pool/timeout 3종)가 통째로 사라져 운영이 조용히 무제한으로 돈다.
        """
        url = "postgresql+psycopg://u:p@h:5432/sqlite_backup"
        self.assertFalse(ds.is_sqlite_url(url))
        self.assertTrue("sqlite" in url, "이 반례의 전제 — 문자열엔 sqlite 가 들어 있다")
        kwargs = ds.engine_kwargs(url, ds.ONLINE)
        self.assertIn("pool_timeout", kwargs, "PG 로 판정됐는데 PG kwargs 가 없다")

    def test_real_sqlite_urls_are_sqlite(self):
        for url in ("sqlite:///:memory:", "sqlite:////tmp/a.db", "sqlite+pysqlite:///x.db"):
            with self.subTest(url=url):
                self.assertTrue(ds.is_sqlite_url(url))


class TestEngineKwargs(unittest.TestCase):
    def test_sqlite_gets_no_postgres_only_kwargs(self):
        """⛔ 이 `assertEqual` **완전일치를 부분검사로 완화하지 마라.**

        `pool_size` 누수는 연결 시험으로 **안 잡힌다** — `SingletonThreadPool` 이 조용히
        받는다(실측). 그걸 잡는 지점이 이 완전일치다. 반대로 `engine_kwargs` 를 **우회한**
        하드코딩에는 실명하며, 그건 `tests/test_pg_engine_binding.py` 의 subprocess 시험이 문다.
        """
        for profile in ds.PROFILES:
            with self.subTest(profile=profile):
                kwargs = ds.engine_kwargs("sqlite:///:memory:", profile)
                self.assertEqual(kwargs, {"connect_args": {"check_same_thread": False}})

    def test_postgres_carries_all_three_timeouts(self):
        for profile in ds.PROFILES:
            with self.subTest(profile=profile):
                kwargs = ds.engine_kwargs("postgresql+psycopg://u@h/d", profile)
                self.assertEqual(kwargs["pool_timeout"], ds.POOL_TIMEOUT_SECONDS[profile])
                self.assertEqual(kwargs["connect_args"]["connect_timeout"],
                                 ds.CONNECT_TIMEOUT_SECONDS[profile])
                self.assertEqual(kwargs["connect_args"]["options"],
                                 f"-c statement_timeout={ds.STATEMENT_TIMEOUT_MS[profile]}")

    def test_pool_capacity_is_unchanged_by_this_slice(self):
        """⛔ 상한과 **용량**을 같이 움직이면 개선의 귀속이 불가능해진다."""
        for profile in ds.PROFILES:
            with self.subTest(profile=profile):
                kwargs = ds.engine_kwargs("postgresql+psycopg://u@h/d", profile)
                self.assertEqual(kwargs["pool_size"], 3)
                self.assertEqual(kwargs["max_overflow"], 2)

    def test_maintenance_is_strictly_more_permissive_than_online(self):
        """두 profile 이 **실제로 다른 값**이어야 한다 — 같으면 profile 이 무의미하다."""
        self.assertGreater(ds.STATEMENT_TIMEOUT_MS[ds.MAINTENANCE],
                           ds.STATEMENT_TIMEOUT_MS[ds.ONLINE])
        self.assertGreater(ds.POOL_TIMEOUT_SECONDS[ds.MAINTENANCE],
                           ds.POOL_TIMEOUT_SECONDS[ds.ONLINE])

    def test_no_timeout_is_zero(self):
        """⛔ PostgreSQL 에서 `statement_timeout=0` 은 **무제한**이다 — 0 은 '끔' 이지 '빠름' 이 아니다."""
        for profile in ds.PROFILES:
            with self.subTest(profile=profile):
                self.assertGreater(ds.STATEMENT_TIMEOUT_MS[profile], 0)
                self.assertGreater(ds.POOL_TIMEOUT_SECONDS[profile], 0)
                self.assertGreater(ds.CONNECT_TIMEOUT_SECONDS[profile], 0)

    def test_the_defense_layer_raises_a_distinguishable_subclass(self):
        """⛔ 하위클래스가 사라지면 검증-우회 변이가 **방어층 덕분에** 죽으면서 검증이 잡은
        것처럼 보인다. 계열(`InvalidDbWorkloadProfile`)은 유지해야 기존 except 가 산다."""
        with self.assertRaises(ds.UnvalidatedDbWorkloadProfile) as ctx:
            ds.engine_kwargs("postgresql+psycopg://u@h/d", "turbo")
        self.assertIsInstance(ctx.exception, ds.InvalidDbWorkloadProfile)

    def test_resolve_profile_does_not_raise_the_defense_subclass(self):
        """두 층이 **다른** 예외를 던져야 구분이 성립한다."""
        with self.assertRaises(ds.InvalidDbWorkloadProfile) as ctx:
            ds.resolve_profile("maintenence")
        self.assertNotIsInstance(ctx.exception, ds.UnvalidatedDbWorkloadProfile)


class TestValidationPrecedesEngineCreation(unittest.TestCase):
    """⛔ 검증이 `create_engine` **뒤로** 밀리면, 잘못된 profile 로 engine 이 **먼저** 만들어진다.

    ⚠️ 이건 행동으로 관측하기 어렵다 — 순서가 바뀌어도 결국 예외는 나므로 "import 가 죽는다"
       계약(`TestInvalidProfileIsAStartupFailure`)은 **그대로 통과한다**. 그래서 주장하는 대상
       자체가 소스 순서인 이 경우에 한해 소스 순서를 본다.

    ⚠️ **이 판정식이 잠그는 것은 실행 순서가 아니라 진실원 단일성이다.** 호출을 위쪽 함수
       본문으로 hoist 하면 lineno 비교는 참인데 실행 순서는 뒤집힌다(외부 검토 + codex 독립
       재현). 실행 순서의 소유자는
       `tests/test_pg_engine_binding.py::TestValidationPrecedesEngineCreationAtRuntime` 이고,
       그 계약은 `S2-11` 이 문다. 여기서 잠그는 것은 "판정 호출 1개 · create_engine 1개" 이며
       그건 런타임 판정식이 못 잡는다(호출 2개짜리 사본에서 AST 만 red 였다).
    """

    def _lineno_of(self, pred):
        tree = ast.parse((REPO / "app" / "database.py").read_text())
        hits = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and pred(n)]
        return hits

    def test_profile_is_resolved_before_create_engine_in_source_order(self):
        resolves = self._lineno_of(
            lambda n: isinstance(n.func, ast.Attribute)
            and n.func.attr == "resolve_profile_from_env")
        engines = self._lineno_of(
            lambda n: isinstance(n.func, ast.Name) and n.func.id == "create_engine")
        self.assertEqual(len(resolves), 1, f"profile 판정 호출이 {len(resolves)}개다 — 진실원이 하나여야 한다")
        self.assertEqual(len(engines), 1, f"create_engine 호출이 {len(engines)}개다")
        self.assertLess(resolves[0], engines[0],
                        "profile 검증이 create_engine 뒤에 있다 — 잘못된 profile 로 engine 이 먼저 생긴다")


class TestModuleIsSelfContained(unittest.TestCase):
    """⚠️ 초판은 **거짓 계약**이었다(실측으로 red).

    "이 모듈이 `app.config` 를 끌어오면 부작용이 82개 진입점으로 퍼진다" 고 적었는데,
    `app/__init__.py` 가 `from app.logging import logger` 를 하고 `app/logging.py` 가
    `app/config.py` 를 import 한다 — 즉 `app.database` 는 **이 슬라이스 전에도 이미**
    `load_dotenv()`/`mkdir()` 를 거쳤다. 부작용 방지는 이 모듈이 줄 수 있는 것이 아니다.

    실제로 잠글 값어치가 있는 것은 **자기완결성**이다: 이 파일은 `app.*` 를 하나도 import 하지
    않아 패키지 없이 단독 로드된다 → 순수 함수 시험과 변이 겨냥이 engine 생성과 분리된다.
    """

    def test_source_does_not_import_app_config(self):
        tree = ast.parse((REPO / "app" / "database_settings.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(f"{node.module}.{a.name}" for a in node.names)
        self.assertNotIn("app.config", imported, f"부작용 있는 모듈을 끌어왔다: {sorted(imported)}")

    def test_it_does_not_pull_app_into_sys_modules(self):
        """⛔ AST 검사만으론 부족하다 — 간접 체인으로도 끌려온다. 실제로 로드해서 본다.

        ⚠️ 구 이름은 "패키지 **없이** 로드한다" 고 광고했는데 **거짓이었다**: `python -c` 는
           `sys.path[0]=''`(cwd) 이고 cwd 가 REPO 라 `app` 은 그대로 import 가능하다. 그래서
           함께 있던 `PYTHONPATH` 제거 줄은 아무 일도 하지 않았다(no-op, 실측).
           cwd 를 REPO 밖으로 옮기는 것도 기각한다 — 실패 경로에서 `assert not leaked` 가
           도달 불가해지고(ModuleNotFoundError 선행), `PYTHONPATH=/app` 을 심는 실 스크립트가
           있어 격리가 조용히 증발할 수 있다.

        실제 계약은 하나다: **`sys.modules` 에 `app.*` 가 들어오지 않는다.**
        """
        code = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('_ds_standalone',\n"
            f"    {str(REPO / 'app' / 'database_settings.py')!r})\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "leaked = sorted(m for m in sys.modules if m == 'app' or m.startswith('app.'))\n"
            "assert not leaked, f'app.* 를 끌어왔다: {leaked}'\n"
            "assert mod.resolve_profile(None) == 'online'\n"
            "assert mod.engine_kwargs('sqlite:///:memory:', 'online')\n"
            "print('STANDALONE=1')\n"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd=REPO, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertIn("STANDALONE=1", r.stdout)


if __name__ == "__main__":
    unittest.main()
