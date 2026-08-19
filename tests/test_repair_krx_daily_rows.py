"""scripts/repair_krx_daily_rows.py 테스트 — 순수 계약 + **실제 PostgreSQL 통합**.

in-memory 스캐폴드만으로는 `FOR UPDATE`·unique 충돌·rowcount·rollback 의미를
검증할 수 없다(SQLite도 마찬가지). 그래서 disposable PostgreSQL을 띄워
`SessionLocal` 대신 실제 engine으로 `build_db_adapters` 전 경로를 통과시킨다.

Postgres가 없으면 **명시적으로 skip**한다 — 조용히 통과시키지 않는다.

실행:
    python -m pytest tests/test_repair_krx_daily_rows.py -p no:asyncio -q
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import unittest
import uuid
from datetime import date
from decimal import Decimal

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import repair_krx_daily_rows as R  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FEB = date(2026, 2, 13)
AUG = date(2026, 8, 14)

# PostgreSQL. **이름은 리포 정본 `PG_TEST_URL` 하나뿐**이다.
# ⛔ 전에는 `REPAIR_TEST_PG_URL`이라는 자체 이름을 썼는데, CI workflow 는
#    `PG_TEST_URL` 만 준다 → 이 파일의 PG 통합 11건이 **CI 에서 조용히 skip** 됐다.
#    "로컬에서 돌려봤다"가 "CI가 지킨다"를 뜻하지 않았다.
PG_URL = os.getenv("PG_TEST_URL", "").strip()
ON_CI = os.getenv("GITHUB_ACTIONS", "").lower() == "true"


def _noop_persist(fingerprint, sha):
    """preimage 기록을 주입 지점으로만 두고 실제 IO는 하지 않는 테스트용."""



def _converted(contract="A75603", d=FEB, close="1441.0", high="1445.0",
               low="1438.0", **over) -> dict:
    """`krx_row_to_source_daily` 출력 형태."""
    def _dec(v):
        return None if v is None else Decimal(v)

    base = {
        "source": R.SOURCE, "asset": R.ASSET, "date_kst": d,
        "rate": _dec(close), "close": _dec(close),
        "high": _dec(high), "low": _dec(low),
        "ohlc_quality": R.OHLC_QUALITY, "close_basis": R.CLOSE_BASIS,
        "source_method": R.SOURCE_METHOD, "contract_code": contract,
        "basis_date": None, "published_at": None,
        "metadata_json": {"contract_short_code": contract},
    }
    base.update(over)
    return base


def _stored(contract="A75602", d=FEB, close="1441.0", metadata=None,
            **over) -> dict:
    """DB에서 읽은 full row 형태 (PREIMAGE_COLUMNS 전부).

    `metadata` 기본값은 **교정 후 기대 상태**(= 대상 계약의 provenance)다.
    구 계약월의 metadata가 남은 경우는 명시적으로 넘겨서 만든다.
    """
    base = {c: None for c in R.PREIMAGE_COLUMNS}
    base.update({
        "id": 1, "source": R.SOURCE, "asset": R.ASSET, "date_kst": d,
        "rate": Decimal(close), "close": Decimal(close),
        "high": Decimal("1445.0"), "low": Decimal("1438.0"),
        "ohlc_quality": R.OHLC_QUALITY, "close_basis": R.CLOSE_BASIS,
        "source_method": R.SOURCE_METHOD, "contract_code": contract,
        "metadata_json": ({"contract_short_code": contract}
                          if metadata is None else metadata),
    })
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 순수 계약
# ---------------------------------------------------------------------------

class TestAllowlist(unittest.TestCase):

    def test_only_two_dates_allowed(self):
        self.assertEqual({t.date_kst for t in R.REPAIR_ALLOWLIST}, {FEB, AUG})

    def test_targets(self):
        feb, aug = R.resolve_target(FEB), R.resolve_target(AUG)
        self.assertEqual((feb.action, feb.expected_existing_contract,
                          feb.target_contract, feb.target_contract_month),
                         ("update", "A75602", "A75603", "202603"))
        self.assertEqual((aug.action, aug.expected_existing_contract,
                          aug.target_contract, aug.target_contract_month),
                         ("insert", None, "A75609", "202609"))

    def test_other_dates_abort(self):
        for bad in (date(2026, 2, 12), date(2026, 8, 17), date(2026, 5, 18)):
            with self.subTest(bad=bad):
                with self.assertRaises(R.RepairAbort):
                    R.resolve_target(bad)


class TestPrecondition(unittest.TestCase):

    def test_update_requires_exact_existing_contract(self):
        t = R.resolve_target(FEB)
        R.check_precondition(t, _stored(contract="A75602"))
        with self.assertRaises(R.RepairAbort) as ctx:
            R.check_precondition(t, _stored(contract="A75603"))
        self.assertIn("이미 교정", str(ctx.exception))
        with self.assertRaises(R.RepairAbort):
            R.check_precondition(t, None)

    def test_insert_requires_absence(self):
        t = R.resolve_target(AUG)
        R.check_precondition(t, None)
        with self.assertRaises(R.RepairAbort):
            R.check_precondition(t, _stored(d=AUG))


class TestFetchedRowValidation(unittest.TestCase):

    def test_valid_row_passes(self):
        R.validate_fetched_row(R.resolve_target(FEB), _converted())

    def test_rejects(self):
        t = R.resolve_target(FEB)
        cases = {
            "contract": _converted(contract="A75602"),
            "date": _converted(d=date(2026, 2, 12)),
            "missing_close": _converted(close=None),
            "ohlc_order": _converted(close="1500.0"),
            "nonpositive": _converted(low="0"),
            "rate_mismatch": _converted(rate=Decimal("1442.0")),
            "basis_date_set": _converted(basis_date=date(2026, 2, 12)),
            "wrong_source": _converted(source="hana"),
        }
        for name, row in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(R.RepairAbort):
                    R.validate_fetched_row(t, row)


class TestPostVerify(unittest.TestCase):

    def test_valid_passes(self):
        fetched = _converted()
        R.verify_post_state(R.resolve_target(FEB), _stored(
            contract="A75603", close="1441.0", high=Decimal("1445.0"),
            low=Decimal("1438.0")), fetched)

    def test_stale_values_caught(self):
        """contract만 갈아끼우고 가격을 남긴 write."""
        fetched = _converted(close="1443.5", high="1446.0", low="1440.0")
        stale = _stored(contract="A75603", close="1441.0")
        with self.assertRaises(R.RepairAbort) as ctx:
            R.verify_post_state(R.resolve_target(FEB), stale, fetched)
        self.assertIn("반영하지 않았다", str(ctx.exception))

    def test_stale_metadata_caught(self):
        """가격은 맞는데 **provenance metadata가 구 계약월** 그대로인 write."""
        fetched = _converted(contract="A75603")
        stale = _stored(contract="A75603",
                        metadata={"contract_short_code": "A75602"})
        with self.assertRaises(R.RepairAbort) as ctx:
            R.verify_post_state(R.resolve_target(FEB), stale, fetched)
        self.assertIn("provenance 미교체", str(ctx.exception))

    def test_missing_row(self):
        with self.assertRaises(R.RepairAbort):
            R.verify_post_state(R.resolve_target(FEB), None, _converted())

    def test_provenance_and_nullable_locked(self):
        fetched = _converted()
        for field, bad in (("source_method", "close_finalizer"),
                           ("close_basis", "hana_observed_eod"),
                           ("ohlc_quality", "observed_rollup"),
                           ("basis_date", date(2026, 2, 12))):
            with self.subTest(field=field):
                row = _stored(contract="A75603", **{field: bad})
                with self.assertRaises(R.RepairAbort):
                    R.verify_post_state(R.resolve_target(FEB), row, fetched)


class TestPreimage(unittest.TestCase):

    def test_covers_all_table_columns(self):
        from app.models import SourceDailyRate

        self.assertEqual(set(R.PREIMAGE_COLUMNS),
                         {c.name for c in SourceDailyRate.__table__.columns})

    def test_fingerprint_actually_contains_id(self):
        """선언만 맞고 배선이 빠지면 fingerprint에 `id: null`이 들어간다.

        범용 `row_to_dict`는 `id`를 돌려주지 않는다 — 그래서 전용 reader를 쓴다.
        컬럼 집합만 비교하는 테스트는 이 누락을 못 잡는다 (codex 감사 지적).
        """
        fp = json.loads(R.preimage_fingerprint({FEB: _stored(id=4242)}))
        self.assertEqual(fp[str(FEB)]["id"], "4242")

    def test_records_absence(self):
        fp = json.loads(R.preimage_fingerprint({FEB: _stored(), AUG: None}))
        self.assertIsNone(fp[str(AUG)])

    def test_sha_changes_with_content(self):
        a = R.preimage_sha(R.preimage_fingerprint({FEB: _stored(contract="A75602")}))
        b = R.preimage_sha(R.preimage_fingerprint({FEB: _stored(contract="A75603")}))
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 64)


class TestProductionGuard(unittest.TestCase):

    def test_dry_run_never_blocked(self):
        R.check_production_write_guard("postgresql+psycopg://x", write=False,
                                       allow_production=False)

    def test_production_write_requires_flag(self):
        with self.assertRaises(R.RepairAbort):
            R.check_production_write_guard("postgresql+psycopg://x", write=True,
                                           allow_production=False)

    def test_allowed_with_flag(self):
        R.check_production_write_guard("postgresql+psycopg://x", write=True,
                                       allow_production=True)

    def test_sqlite_not_blocked(self):
        R.check_production_write_guard("sqlite:///tmp.db", write=True,
                                       allow_production=False)


class TestProductionGuardUsesSessionUrl(unittest.TestCase):
    """⭐ guard가 **세션이 실제로 bind한 URL**로 판정하는가 (subprocess).

    `DATABASE_URL`을 process env에서 **제거**하고 `.env`에만 두는 조건을 만든다.
    구 구현은 `os.getenv`를 먼저 읽어 빈 문자열을 보고 통과한 뒤, import가 dotenv를
    로드해 세션이 production에 붙었다 (codex 감사 지적).
    """

    def setUp(self):
        self.tmp_out = pathlib.Path(
            os.environ.get("TMPDIR", "/tmp")) / f"krx-entry-{uuid.uuid4().hex}.json"

    def tearDown(self):
        self.tmp_out.unlink(missing_ok=True)
        self.tmp_out.with_suffix(self.tmp_out.suffix + ".committed").unlink(
            missing_ok=True)

    def _run_script(self, *args, database_url=None, auth_key="dummy"):
        """⛔ `PYTHONPATH`를 **주입하지 않는다.**

        주입하면 스크립트가 스스로 `app`을 import하지 못하는 결함을 테스트가
        **보상**해 버린다 — 실제로 그랬다: 운영 형태
        `python scripts/repair_krx_daily_rows.py ...`는 `ModuleNotFoundError: app`
        으로 죽는데 전 테스트가 초록이었다. 진입점 계약은 스크립트가 지는 것이지
        호출자가 지는 게 아니다.
        """
        env = {k: v for k, v in os.environ.items()
               if k not in ("DATABASE_URL", "PYTHONPATH")}
        env[R.AUTH_KEY_ENV] = auth_key
        if database_url:
            env["DATABASE_URL"] = database_url
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "repair_krx_daily_rows.py"),
             *args],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120)

    def test_documented_invocation_form_actually_runs(self):
        """⭐ 문서에 적힌 실행 형태가 **그대로** 도는가 (진입점 계약).

        `python scripts/repair_krx_daily_rows.py ...` — PYTHONPATH 없이.
        이게 없으면 "도구를 만들었다"가 "도구를 실행할 수 있다"를 뜻하지 않는다.
        """
        proc = self._run_script("--help")
        self.assertNotIn("ModuleNotFoundError", proc.stderr,
                         f"진입점이 import 단계에서 죽는다:\n{proc.stderr[-600:]}")
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
        self.assertIn("--preimage-out", proc.stdout)

    def test_sys_path_setup_precedes_repo_local_imports(self):
        """⭐ 진입점 계약을 **구조로** 잠근다.

        실행 테스트만으로는 부족했다: 정본 모듈이 자기 `sys.path`를 넣는 부작용에
        얹혀 있으면, 이 파일의 `sys.path` 한 줄을 지워도 아무것도 안 깨진다
        (변이 검사 실측 — 그래서 그 의존을 끊었다). 여기서는 "리포 루트를 넣는
        문장이 리포-로컬 import보다 **앞에** 있는가"를 AST로 본다.
        """
        src = (REPO_ROOT / "scripts" / "repair_krx_daily_rows.py").read_text()
        tree = ast.parse(src)
        insert_line = None
        for node in tree.body:
            if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                    and ast.unparse(node.value).startswith("sys.path.insert")):
                insert_line = node.lineno
                break
        self.assertIsNotNone(
            insert_line,
            "module level 에 sys.path.insert 가 없다 — "
            "`python scripts/…py` 형태가 ModuleNotFoundError 로 죽는다")

        local_import_lines = [
            n.lineno for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom))
            and any(
                (getattr(n, "module", "") or "").startswith(("app", "backfill_"))
                or a.name.startswith(("app", "backfill_"))
                for a in n.names)
        ]
        for line in local_import_lines:
            self.assertLess(insert_line, line,
                            f"리포-로컬 import(line {line})가 sys.path 설정보다 앞선다")

    def test_auth_env_name_matches_repo_canonical(self):
        """⭐ 인증 env 이름이 리포 정본과 같은가.

        이름이 어긋나면 운영자가 정본 이름으로 넣어둔 값을 도구가 못 보고,
        "미설정"으로 중단한다 — 실제로 존재하지 않는 이름을 하드코딩했었다.
        """
        canonical = (REPO_ROOT / "scripts"
                     / "backfill_krx_openapi_source_daily_rates.py").read_text()
        m = re.search(r'KRX_OPENAPI_AUTH_KEY_ENV\s*=\s*"([^"]+)"', canonical)
        self.assertIsNotNone(m, "정본에서 env 상수를 찾지 못했다")
        self.assertEqual(R.AUTH_KEY_ENV, m.group(1))
        # 하드코딩 방지: repair 소스에 리터럴로 박혀 있으면 정본이 바뀔 때 갈린다
        src = (REPO_ROOT / "scripts" / "repair_krx_daily_rows.py").read_text()
        self.assertNotIn('"KRX_AUTH_KEY"', src)

    def test_missing_auth_key_names_the_canonical_env(self):
        """미설정 안내가 **정본 이름**을 말해야 운영자가 고칠 수 있다."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("DATABASE_URL", "PYTHONPATH", R.AUTH_KEY_ENV)}
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "repair_krx_daily_rows.py"),
             "--preimage-out", str(self.tmp_out)],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120)
        self.assertNotIn("ModuleNotFoundError", proc.stderr)
        self.assertEqual(proc.returncode, 1, proc.stdout[-400:] + proc.stderr[-400:])
        self.assertIn(R.AUTH_KEY_ENV, proc.stderr)

    def test_revert_mode_reaches_revert_not_forward_repair(self):
        """실행 형태로도 확인 — revert 모드가 정방향 교정으로 새지 않는다."""
        pre = self.tmp_out.with_name("pre-src.json")
        pre.write_text(json.dumps({
            "preimage_sha256": "0" * 64,
            "preimage": json.dumps({str(FEB): None, str(AUG): None},
                                   ensure_ascii=False, sort_keys=True),
        }), encoding="utf-8")
        proc = self._run_script("--revert-from", str(pre),
                                "--preimage-out", str(self.tmp_out),
                                database_url="sqlite:///:memory:")
        pre.unlink(missing_ok=True)
        self.assertNotIn("ModuleNotFoundError", proc.stderr)
        # 정방향 교정의 사전조건 메시지가 나오면 안 된다 (revert 로 갔어야 한다)
        self.assertNotIn("UPDATE 대상 행이 없다", proc.stderr + proc.stdout)
        self.assertIn("revert", (proc.stderr + proc.stdout).lower())

    def test_revert_production_guard_applies(self):
        """⭐ revert 도 **DB 를 바꾼다** — production guard 가 write 전용이면 안 된다."""
        pre = self.tmp_out.with_name("pre-guard.json")
        pre.write_text(json.dumps({
            "preimage_sha256": "0" * 64,
            "preimage": json.dumps({str(FEB): None, str(AUG): None},
                                   ensure_ascii=False, sort_keys=True),
        }), encoding="utf-8")
        proc = self._run_script(
            "--revert-from", str(pre), "--preimage-out", str(self.tmp_out),
            database_url="postgresql+psycopg://u:p@example.invalid:5432/db")
        pre.unlink(missing_ok=True)
        self.assertEqual(proc.returncode, 1, proc.stdout[-400:] + proc.stderr[-400:])
        self.assertIn("allow-production-write", proc.stderr)

    def test_production_write_aborts_before_any_api_or_db_work(self):
        """guard가 **세션 URL**로 판정하고, 외부 호출 전에 막는가.

        `KRX_AUTH_KEY`를 넣어 뒀는데도 그 단계에 도달하지 않아야 한다 — guard가
        앞에서 끝냈다는 증거다. 늦게 걸리면 이미 API를 때린 뒤라 token rotation
        위험이 있다.
        """
        preimage_out = pathlib.Path(
            os.environ.get("TMPDIR", "/tmp")) / f"guard-{uuid.uuid4().hex}.json"
        proc = self._run_script(
            "--write", "--expect-preimage-sha", "a" * 64,
            "--preimage-out", str(preimage_out),
            database_url="postgresql+psycopg://u:p@example.invalid:5432/db")
        self.assertFalse(preimage_out.exists(),
                         "guard가 막았는데 preimage 파일이 생겼다")
        self.assertEqual(proc.returncode, 1, f"stdout={proc.stdout} stderr={proc.stderr}")
        self.assertIn("allow-production-write", proc.stderr)
        self.assertNotIn("KRX_AUTH_KEY", proc.stderr)

    def test_guard_reads_session_bound_url_not_separate_getenv(self):
        """`main`이 `app.database`의 URL로 guard를 부르는지 AST로 잠근다.

        별도 `os.getenv("DATABASE_URL")`로 읽으면 세션이 bind한 값과 갈릴 수 있다
        (dotenv 로드 시점에 따라). 같은 출처를 쓰는 것이 계약이다.
        """
        import ast

        src = (REPO_ROOT / "scripts" / "repair_krx_daily_rows.py").read_text("utf-8")
        tree = ast.parse(src)
        main_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        imports_db_url = any(
            isinstance(n, ast.ImportFrom) and n.module == "app.database"
            and any(a.name == "DATABASE_URL" for a in n.names)
            for n in ast.walk(main_fn))
        self.assertTrue(imports_db_url, "main이 app.database.DATABASE_URL을 쓰지 않는다")
        getenv_db = [
            n for n in ast.walk(main_fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "getenv"
            and n.args and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == "DATABASE_URL"]
        self.assertEqual(getenv_db, [], "main이 DATABASE_URL을 별도 getenv로 읽는다")


# ---------------------------------------------------------------------------
# 실제 PostgreSQL 통합 — 잠금·rowcount·unique 충돌·rollback
# ---------------------------------------------------------------------------

class TestCommitReceipt(unittest.TestCase):
    """commit **이후**의 실패를 중단으로 보고하지 않는다."""

    def setUp(self):
        self.tmp = pathlib.Path(
            os.environ.get("TMPDIR", "/tmp")) / f"krx-receipt-{uuid.uuid4().hex}"
        self.tmp.mkdir()

    def tearDown(self):
        for child in self.tmp.iterdir():
            child.unlink()
        self.tmp.rmdir()

    def test_receipt_is_exclusive_and_durable(self):
        pre = self.tmp / "pre.json"
        result = {"preimage_sha": "abc", "applied": [{"date_kst": "2026-02-13"}]}
        R.write_commit_receipt(pre, result)
        receipt = self.tmp / "pre.json.committed"
        self.assertTrue(receipt.exists())
        self.assertEqual(json.loads(receipt.read_text())["preimage_sha256"], "abc")
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(R.RepairAbort):
            R.write_commit_receipt(pre, result)

    def _main_handlers(self):
        source = (REPO_ROOT / "scripts" / "repair_krx_daily_rows.py").read_text()
        tree = ast.parse(source)
        main_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        handlers = [n for n in ast.walk(main_fn) if isinstance(n, ast.ExceptHandler)]
        self.assertTrue(handlers, "main에 예외 처리가 없다")
        return main_fn, handlers

    def _phase_branch(self, handlers, phase_const: str):
        for h in handlers:
            for n in ast.walk(h):
                if isinstance(n, ast.If) and phase_const in ast.unparse(n.test):
                    return n
        return None

    def _calls_rollback(self, node) -> bool:
        """실제 `rollback()` **호출**만 본다.

        문자열 substring으로 보면 안내 문구 속 단어에 걸린다 — 실제로 걸렸다.
        """
        return any(isinstance(n, ast.Call)
                   and getattr(n.func, "attr", None) == "rollback"
                   for n in ast.walk(node))

    def test_post_commit_failure_is_not_reported_as_abort(self):
        """⭐ commit 성공 뒤 영수증/출력이 실패해도 "중단"이 아니다.

        rollback을 부르고 `aborted`로 적으면, 운영자는 아무것도 안 쓰인 줄 알고
        상태 판단을 틀리게 한다 (codex 감사 지적). exit code로 구분한다.
        """
        _main, handlers = self._main_handlers()
        branch = self._phase_branch(handlers, "PHASE_COMMITTED")
        self.assertIsNotNone(branch, "commit 완료 분기가 없다")
        self.assertIn("COMMITTED_RECEIPT_UNVERIFIED", ast.unparse(branch))
        self.assertFalse(self._calls_rollback(branch),
                         "commit 완료 뒤에 rollback을 부른다")

    def test_in_doubt_commit_is_not_reported_as_abort(self):
        """⭐ `commit()` **자체가 예외**여도 미commit이 아닐 수 있다.

        서버가 COMMIT을 적용한 직후 연결이 끊겨 ACK만 유실되면(in-doubt) DB는
        이미 바뀌었다. rollback은 되돌리지 못하고, "중단"으로 적으면 거짓
        보고다 (codex 감사 지적). 결과 미확정으로 분류해야 한다.
        """
        main_fn, handlers = self._main_handlers()
        main_src = ast.unparse(main_fn)
        # commit **호출 전에** COMMITTING 단계로 들어가야 한다 — 뒤에 두면
        # 예외 시 여전히 PRE_COMMIT이라 중단으로 오분류된다.
        commit_pos = main_src.index("db.commit()")
        committing_pos = main_src.index("PHASE_COMMITTING")
        self.assertLess(committing_pos, commit_pos,
                        "COMMITTING 단계 진입이 commit() 뒤에 있다")

        branch = self._phase_branch(handlers, "PHASE_COMMITTING")
        self.assertIsNotNone(branch, "in-doubt 분기가 없다")
        self.assertIn("COMMIT_OUTCOME_UNVERIFIED", ast.unparse(branch))
        self.assertFalse(self._calls_rollback(branch),
                         "in-doubt에서 rollback을 부른다 — 되돌리지 못한다")

    def test_three_phases_are_distinct(self):
        self.assertEqual(
            len({R.PHASE_PRE_COMMIT, R.PHASE_COMMITTING, R.PHASE_COMMITTED}), 3)
        self.assertNotEqual(R.COMMIT_OUTCOME_UNVERIFIED,
                            R.COMMITTED_RECEIPT_UNVERIFIED)

    def test_receipt_absence_is_documented_as_inconclusive(self):
        """부재를 "미commit"으로 읽으면 안 된다 — 문서화가 계약의 일부다."""
        self.assertIn("부재는 미commit의 증거가 아니다",
                      R.write_commit_receipt.__doc__)


class TestWriteBinding(unittest.TestCase):
    """dry-run 이 검토한 상태와 실제 write 대상을 **결속**한다."""

    def test_expected_preimage_sha_mismatch_aborts(self):
        """⭐ dry-run 이후 누가 행을 바꿨으면 write 를 하면 안 된다.

        리포 관용구(`--expected-rows-count`)의 repair 판. 이게 없으면 "검토한
        것"과 "덮어쓰는 것"이 다를 수 있고, preimage 는 그 사실을 기록만 할 뿐
        막지 못한다 (외부 검토 지적).
        """
        calls = []
        with self.assertRaises(R.RepairAbort) as ctx:
            R.repair_all(
                lock_and_read=lambda d: ({"contract_code": "A75602"} if d == FEB else None),
                fetch_official=_valid_fetch,
                apply_update=lambda *a: calls.append("update") or 1,
                apply_insert=lambda *a: calls.append("insert"),
                reread=lambda d: None,
                persist_preimage=lambda fp, sha: None,
                write=True, expected_preimage_sha="0" * 64)
        self.assertIn("preimage", str(ctx.exception))
        self.assertEqual(calls, [], "불일치인데 write 가 실행됐다")

    def test_expected_preimage_sha_match_proceeds(self):
        seen = {}
        R.repair_all(
            lock_and_read=lambda d: ({"contract_code": "A75602"} if d == FEB else None),
            fetch_official=_valid_fetch,
            apply_update=lambda *a: 1, apply_insert=lambda *a: None,
            reread=lambda d: _stored(contract="A75603", d=d, close="1443.5",
                                     high=Decimal("1446.0"), low=Decimal("1440.0"),
                                     metadata={"contract_month":
                                               R.resolve_target(d).target_contract_month}),
            persist_preimage=lambda fp, sha: seen.update(sha=sha),
            write=False)
        result = R.repair_all(
            lock_and_read=lambda d: ({"contract_code": "A75602"} if d == FEB else None),
            fetch_official=_valid_fetch,
            apply_update=lambda *a: 1, apply_insert=lambda *a: None,
            reread=lambda d: _stored(contract=R.resolve_target(d).target_contract,
                                     d=d, close="1443.5",
                                     high=Decimal("1446.0"), low=Decimal("1440.0"),
                                     metadata={"contract_month":
                                               R.resolve_target(d).target_contract_month}),
            persist_preimage=lambda fp, sha: None,
            write=True, expected_preimage_sha=None)
        self.assertEqual(result["mode"], "write")

    def test_write_requires_expected_sha_at_cli(self):
        """CLI 수준에서 `--write` 는 `--expect-preimage-sha` 를 요구한다."""
        parser = R.build_parser()
        args = parser.parse_args(["--write", "--preimage-out", "/tmp/p.json"])
        with self.assertRaises(R.RepairAbort) as ctx:
            R._validate_cli(args)
        self.assertIn("expect-preimage-sha", str(ctx.exception))

    def test_production_write_requires_snapshot_attestation(self):
        """production write 는 snapshot 식별자를 **기록**하게 강제한다.

        ⚠️ 이 도구는 AWS 에 접근하지 않으므로 snapshot 존재를 **증명하지 못한다**.
        강제하는 것은 "사람이 그 값을 적었다"는 사실이고, 그 값은 산출물에 남아
        되돌릴 때 기준이 된다. 증명이 아니라 기록이라는 점을 help 가 밝힌다.
        """
        parser = R.build_parser()
        args = parser.parse_args(["--write", "--allow-production-write",
                                  "--expect-preimage-sha", "a" * 64,
                                  "--preimage-out", "/tmp/p.json"])
        with self.assertRaises(R.RepairAbort) as ctx:
            R._validate_cli(args)
        self.assertIn("snapshot", str(ctx.exception))


class TestRevertPath(unittest.TestCase):
    """commit 이후 되돌리는 **실행 경로**가 존재한다."""

    def test_revert_plan_is_exact_inverse(self):
        pre = {
            str(FEB): {"id": 7, "contract_code": "A75602", "rate": "1441.0",
                       "close": "1441.0", "high": "1445.0", "low": "1438.0",
                       "ohlc_quality": "source_ohlc",
                       "close_basis": "krx_cf_close_1545",
                       "source_method": "krx_openapi_daily",
                       "basis_date": None, "published_at": None,
                       "captured_at": "2026-02-14 00:00:00",
                       "metadata_json": {"contract_short_code": "A75602"},
                       "source": "krx", "asset": "usd-krw-futures",
                       "date_kst": "2026-02-13"},
            str(AUG): None,
        }
        plan = R.build_revert_plan(pre)
        by_date = {p["date_kst"]: p for p in plan}
        self.assertEqual(by_date["2026-02-13"]["action"], "restore")
        self.assertEqual(by_date["2026-02-13"]["values"]["contract_code"], "A75602")
        # 우리가 넣은 행은 지우는 것이 역연산이다
        self.assertEqual(by_date["2026-08-14"]["action"], "delete")

    def test_main_actually_dispatches_to_revert(self):
        """⭐ **argparse 가 파싱한다 ≠ main 이 그걸 쓴다.**

        전 판에서 `--revert-from` 은 파싱·검증만 되고 `main()` 은 그 값을 한
        번도 읽지 않은 채 정방향 dry-run 으로 떨어졌다. 그런데 테스트는 함수
        존재와 argparse 결과만 봐서 초록이었다 — **배선을 검사하지 않는
        테스트**였다(외부 검토 지적). AST 로 dispatch 를 잠근다.
        """
        self.assertTrue(callable(getattr(R, "apply_revert_plan", None)))
        src = (REPO_ROOT / "scripts" / "repair_krx_daily_rows.py").read_text()
        tree = ast.parse(src)
        main_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        body = ast.unparse(main_fn)
        self.assertIn("revert_from", body, "main 이 revert_from 을 읽지 않는다")
        self.assertRegex(body, r"_run_revert|apply_revert_plan",
                         "main 이 revert 실행 경로로 분기하지 않는다")

    def test_preimage_artifact_sha_is_reverified_on_load(self):
        """⭐ 손댄 preimage 를 그대로 되먹이면 원 상태를 재현하지 못한다.

        sha 를 기록만 하고 로드 시 확인하지 않으면 그 sha 는 장식이다.
        """
        tmp = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / f"pre-{uuid.uuid4().hex}.json"
        body = json.dumps({str(FEB): None, str(AUG): None},
                          ensure_ascii=False, sort_keys=True)
        try:
            tmp.write_text(json.dumps({"preimage_sha256": R.preimage_sha(body),
                                       "preimage": body}), encoding="utf-8")
            rows, sha = R.load_preimage_artifact(tmp)      # 정상
            self.assertEqual(sha, R.preimage_sha(body))
            self.assertEqual(set(rows), {str(FEB), str(AUG)})

            tampered = json.dumps({str(FEB): None, str(AUG): {"id": 9}},
                                  ensure_ascii=False, sort_keys=True)
            tmp.write_text(json.dumps({"preimage_sha256": R.preimage_sha(body),
                                       "preimage": tampered}), encoding="utf-8")
            with self.assertRaises(R.RepairAbort) as ctx:
                R.load_preimage_artifact(tmp)
            self.assertIn("sha", str(ctx.exception))
        finally:
            tmp.unlink(missing_ok=True)

    def test_revert_and_write_are_mutually_exclusive(self):
        parser = R.build_parser()
        args = parser.parse_args(["--write", "--expect-preimage-sha", "a" * 64,
                                  "--revert-from", "/tmp/pre.json",
                                  "--preimage-out", "/tmp/x.json"])
        with self.assertRaises(R.RepairAbort) as ctx:
            R._validate_cli(args)
        self.assertIn("revert", str(ctx.exception))

    def test_revert_postcondition_is_original_preimage_sha(self):
        """되돌림이 옳다는 것을 무엇이 증명하는가 — **지문 복원**이다.

        전 컬럼을 원래대로 돌려놓았으면 preimage 지문을 다시 떴을 때 원래
        sha 가 그대로 나와야 한다. 하나라도 어긋나면 나오지 않는다.
        """
        pre = {FEB: _stored(contract="A75602", metadata={"x": 1}), AUG: None}
        original_sha = R.preimage_sha(R.preimage_fingerprint(pre))

        state = {FEB: _stored(contract="A75603", metadata={"y": 2}),
                 AUG: _stored(d=AUG, contract="A75609")}
        applied = []

        def restore(target_date, values):
            state[target_date] = dict(values)
            applied.append(("restore", target_date))
            return 1

        def delete(target_date):
            state[target_date] = None
            applied.append(("delete", target_date))
            return 1

        result = R.apply_revert_plan(
            plan=R.build_revert_plan({str(d): (None if r is None else
                                               {k: r[k] for k in R.PREIMAGE_COLUMNS})
                                      for d, r in pre.items()}),
            expected_sha=original_sha,
            expected_postimage_sha=R._NO_RECEIPT_SENTINEL,
            lock_and_read=lambda d: state[d],
            restore=restore, delete=delete,
            reread=lambda d: state[d])
        self.assertEqual(sorted(applied), [("delete", AUG), ("restore", FEB)])
        self.assertEqual(result["restored_preimage_sha"], original_sha)

    def test_revert_aborts_when_result_does_not_match_preimage(self):
        """복원 결과가 원래 지문과 다르면 중단(caller 가 rollback)."""
        pre = {FEB: _stored(contract="A75602"), AUG: None}
        original_sha = R.preimage_sha(R.preimage_fingerprint(pre))
        state = {FEB: _stored(contract="A75603"),
                 AUG: _stored(d=AUG, contract="A75609")}

        def sloppy_restore(target_date, values):
            bad = dict(values)
            bad["close"] = Decimal("9999.0")     # 한 컬럼만 틀리게
            state[target_date] = bad
            return 1

        with self.assertRaises(R.RepairAbort) as ctx:
            R.apply_revert_plan(
                plan=R.build_revert_plan(
                    {str(d): (None if r is None else
                              {k: r[k] for k in R.PREIMAGE_COLUMNS})
                     for d, r in pre.items()}),
                expected_sha=original_sha,
                expected_postimage_sha=R._NO_RECEIPT_SENTINEL,
                lock_and_read=lambda d: state[d],
                restore=sloppy_restore,
                delete=lambda d: (state.__setitem__(d, None), 1)[1],
                reread=lambda d: state[d])
        self.assertIn("지문", str(ctx.exception))

    def test_revert_requires_current_state_to_be_our_postimage(self):
        """⭐ **우리가 만든 상태만** 되돌린다.

        교정 후 다른 무언가가 그 행을 바꿨다면, 되돌리기는 그 변경까지 덮거나
        지운다. `lock_and_read` 결과를 버리고 무조건 UPDATE/DELETE 하면 그렇게
        된다 (외부 검토 지적). 현재 지문이 **교정 직후 지문(postimage)** 과
        같을 때만 진행한다.
        """
        pre = {FEB: _stored(contract="A75602"), AUG: None}
        pre_sha = R.preimage_sha(R.preimage_fingerprint(pre))
        post = {FEB: _stored(contract="A75603"), AUG: _stored(d=AUG, contract="A75609")}
        post_sha = R.preimage_sha(R.preimage_fingerprint(post))

        # 제3자가 2/13 을 또 바꿔 놓은 상태
        meddled = {FEB: _stored(contract="A75699"), AUG: post[AUG]}
        applied = []
        with self.assertRaises(R.RepairAbort) as ctx:
            R.apply_revert_plan(
                plan=R.build_revert_plan(
                    {str(d): (None if r is None else
                              {k: r[k] for k in R.PREIMAGE_COLUMNS})
                     for d, r in pre.items()}),
                expected_sha=pre_sha, expected_postimage_sha=post_sha,
                lock_and_read=lambda d: meddled[d],
                restore=lambda d, v: applied.append(("r", d)) or 1,
                delete=lambda d: applied.append(("d", d)) or 1,
                reread=lambda d: meddled[d])
        self.assertIn("postimage", str(ctx.exception))
        self.assertEqual(applied, [], "사전조건 위반인데 되돌림이 실행됐다")

    def test_revert_proceeds_when_postimage_matches(self):
        pre = {FEB: _stored(contract="A75602"), AUG: None}
        pre_sha = R.preimage_sha(R.preimage_fingerprint(pre))
        post = {FEB: _stored(contract="A75603"), AUG: _stored(d=AUG, contract="A75609")}
        post_sha = R.preimage_sha(R.preimage_fingerprint(post))
        state = dict(post)

        def restore(d, values):
            state[d] = {k: R._deserialize_value(k, v) for k, v in values.items()}
            return 1

        result = R.apply_revert_plan(
            plan=R.build_revert_plan(
                {str(d): (None if r is None else
                          {k: r[k] for k in R.PREIMAGE_COLUMNS})
                 for d, r in pre.items()}),
            expected_sha=pre_sha, expected_postimage_sha=post_sha,
            lock_and_read=lambda d: state[d],
            restore=restore,
            delete=lambda d: (state.__setitem__(d, None), 1)[1],
            reread=lambda d: state[d])
        self.assertEqual(result["restored_preimage_sha"], pre_sha)

    def test_revert_commit_then_failure_is_not_abort(self):
        """⭐ 정방향에서 고친 3단계 보고를 **revert 에도** 적용했는가.

        전 판은 `PHASE_COMMITTING` 만 다루고 `PHASE_COMMITTED` 를 빠뜨려,
        commit 성공 뒤 출력이 실패하면 rollback + "aborted" 로 거짓 보고했다
        (외부 검토 지적 — 같은 결함을 방향만 바꿔 반복했다).
        """
        src = (REPO_ROOT / "scripts" / "repair_krx_daily_rows.py").read_text()
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_run_revert")
        handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
        body = "\n".join(ast.unparse(h) for h in handlers)
        self.assertIn("PHASE_COMMITTED", body,
                      "_run_revert 가 commit 완료 단계를 구분하지 않는다")
        self.assertIn("COMMITTED_RECEIPT_UNVERIFIED", body)
        branch = next(
            (n for h in handlers for n in ast.walk(h)
             if isinstance(n, ast.If) and "PHASE_COMMITTED" in ast.unparse(n.test)),
            None)
        self.assertIsNotNone(branch)
        self.assertFalse(
            any(isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "rollback"
                for n in ast.walk(branch)),
            "commit 완료 뒤에 rollback 을 부른다")

    def test_revert_requires_rowcount_one(self):
        pre = {FEB: _stored(contract="A75602"), AUG: None}
        sha = R.preimage_sha(R.preimage_fingerprint(pre))
        state = {FEB: _stored(contract="A75603"),
                 AUG: _stored(d=AUG, contract="A75609")}
        with self.assertRaises(R.RepairAbort) as ctx:
            R.apply_revert_plan(
                plan=R.build_revert_plan(
                    {str(d): (None if r is None else
                              {k: r[k] for k in R.PREIMAGE_COLUMNS})
                     for d, r in pre.items()}),
                expected_sha=sha,
                expected_postimage_sha=R._NO_RECEIPT_SENTINEL,
                lock_and_read=lambda d: state[d],
                restore=lambda d, v: 0,          # 0행 — 대상이 바뀌었다
                delete=lambda d: 1,
                reread=lambda d: state[d])
        self.assertIn("rowcount", str(ctx.exception))

    def test_revert_refuses_unknown_dates(self):
        with self.assertRaises(R.RepairAbort):
            R.build_revert_plan({"2026-03-01": None})

    def test_revert_requires_full_preimage(self):
        """일부 컬럼만 있는 preimage 로는 복원할 수 없다."""
        with self.assertRaises(R.RepairAbort) as ctx:
            R.build_revert_plan({str(FEB): {"contract_code": "A75602"},
                                 str(AUG): None})
        self.assertIn("컬럼", str(ctx.exception))


class TestPreimageArtifact(unittest.TestCase):
    """되돌림 기준을 **write 전에** 내구 기록한다."""

    def setUp(self):
        self.tmp = pathlib.Path(
            os.environ.get("TMPDIR", "/tmp")) / f"krx-preimage-{uuid.uuid4().hex}"
        self.tmp.mkdir()

    def tearDown(self):
        for child in self.tmp.iterdir():
            child.unlink()
        self.tmp.rmdir()

    def test_exclusive_create_and_content(self):
        path = self.tmp / "pre.json"
        R.write_preimage_artifact(path, '{"x": 1}', "abc123")
        loaded = json.loads(path.read_text())
        self.assertEqual(loaded["preimage_sha256"], "abc123")
        self.assertEqual(loaded["preimage"], '{"x": 1}')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_refuses_to_overwrite(self):
        """이미 있는 파일을 덮으면 앞선 교정의 되돌림 기준이 사라진다."""
        path = self.tmp / "pre.json"
        R.write_preimage_artifact(path, '{"x": 1}', "abc")
        with self.assertRaises(R.RepairAbort):
            R.write_preimage_artifact(path, '{"y": 2}', "def")
        self.assertEqual(json.loads(path.read_text())["preimage_sha256"], "abc")

    def test_persist_runs_before_writes_and_failure_blocks_them(self):
        """기록이 실패하면 **write 자체가 없어야** 한다."""
        calls = []

        def failing_persist(fingerprint, sha):
            calls.append("persist")
            raise R.RepairAbort("디스크 가득")

        def track(name):
            def fn(*a, **k):
                calls.append(name)
                return 1 if name == "update" else None
            return fn

        with self.assertRaises(R.RepairAbort):
            R.repair_all(
                lock_and_read=lambda d: (
                    {"contract_code": "A75602"} if d == FEB else None),
                fetch_official=_valid_fetch,
                apply_update=track("update"), apply_insert=track("insert"),
                reread=lambda d: None, persist_preimage=failing_persist, write=True)
        self.assertEqual(calls, ["persist"], f"write가 실행됐다: {calls}")

    def test_dry_run_does_not_persist(self):
        """dry-run은 아무것도 바꾸지 않으므로 되돌림 기준도 만들지 않는다."""
        calls = []
        result = R.repair_all(
            lock_and_read=lambda d: ({"contract_code": "A75602"} if d == FEB else None),
            fetch_official=_valid_fetch,
            apply_update=lambda *a: 1, apply_insert=lambda *a: None,
            reread=lambda d: None,
            persist_preimage=lambda fp, sha: calls.append("persist"), write=False)
        self.assertEqual(calls, [])
        self.assertEqual(result["mode"], "dry-run")


def _valid_fetch(target):
    """`validate_fetched_row`를 통과하는 최소 행."""
    return {"source": R.SOURCE, "asset": R.ASSET, "date_kst": target.date_kst,
            "contract_code": target.target_contract,
            "rate": Decimal("1443.5"), "close": Decimal("1443.5"),
            "high": Decimal("1446.0"), "low": Decimal("1440.0"),
            "basis_date": None, "published_at": None,
            "metadata_json": {"contract_month": target.target_contract_month}}


@unittest.skipUnless(
    PG_URL or ON_CI,
    "로컬: PG_TEST_URL 미설정 — PostgreSQL 통합 테스트 skip (CI 에서는 fail). "
    "SQLite로는 FOR UPDATE·unique 충돌·rowcount 의미를 검증할 수 없다. "
    "실행: docker run -d --rm -e POSTGRES_PASSWORD=t -p 55432:5432 postgres:17-alpine && "
    "PG_TEST_URL=postgresql+psycopg://postgres:t@localhost:55432/postgres pytest ...")
class TestRepairAgainstRealPostgres(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """**전용 schema**를 배타 생성해 격리한다.

        public schema의 `source_daily_rates`를 직접 drop/create하면,
        `PG_TEST_URL`이 공유·운영 DB를 가리켰을 때 실테이블을
        삭제한다 (codex 감사 지적). `CREATE SCHEMA`는 `IF NOT EXISTS` 없이
        써서 이름 충돌 시 **실패**하게 둔다 — 남의 schema를 재사용하지 않는다.
        """
        import uuid

        if not PG_URL:
            # skipUnless 가 ON_CI 를 통과시킨 경우 — 여기서 **fail** 한다.
            raise AssertionError(
                "CI 인데 PG_TEST_URL 이 없다 — workflow 의 postgres service 또는 env "
                "배선이 끊겼다. skip 하면 계약이 사라진 것을 아무도 모른다.")

        from sqlalchemy import create_engine, text as sql_text
        from sqlalchemy.orm import sessionmaker

        from app.models import SourceDailyRate

        cls.schema = f"krx_repair_test_{uuid.uuid4().hex[:12]}"
        base = create_engine(PG_URL, future=True)
        with base.begin() as conn:
            conn.execute(sql_text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.base_engine = base
        # schema_translate_map: 모델의 schema=None을 전용 schema로 돌린다.
        cls.engine = base.execution_options(
            schema_translate_map={None: cls.schema})
        cls.Session = sessionmaker(bind=cls.engine, future=True)
        SourceDailyRate.__table__.create(cls.engine)
        cls.Model = SourceDailyRate

    @classmethod
    def tearDownClass(cls):
        from sqlalchemy import text as sql_text

        with cls.base_engine.begin() as conn:
            conn.execute(sql_text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.base_engine.dispose()

    def test_revert_roundtrip_against_real_postgres(self):
        """⭐ 교정 → 되돌리기 **왕복**이 원 상태를 재현하는가 (실 PostgreSQL).

        되돌림의 사후조건은 "preimage 지문이 원래 sha 와 같다"이고, 그건 전
        컬럼이 정확히 복원됐을 때만 성립한다. in-memory 스캐폴드로는 UPDATE
        rowcount·DELETE 의미를 검증할 수 없다.
        """
        self._seed_feb()
        db = self.Session()
        try:
            adapters = R.build_db_adapters(db, self._fetch_fn_ok())
            captured = {}
            result = R.repair_all(
                write=True,
                persist_preimage=lambda fp, sha: captured.update(fp=fp, sha=sha),
                **adapters)
            db.commit()
        finally:
            db.close()
        self.assertEqual(self._row(FEB).contract_code, "A75603")
        self.assertIsNotNone(self._row(AUG))
        self.assertEqual(result["preimage_sha"], captured["sha"])

        # --- 되돌리기 ---
        rows = json.loads(captured["fp"])
        plan = R.build_revert_plan(rows)
        db2 = self.Session()
        try:
            radapters = R.build_revert_adapters(db2)
            rres = R.apply_revert_plan(
                plan=plan, expected_sha=captured["sha"],
                expected_postimage_sha=result["postimage_sha"], **radapters)
            db2.commit()
        finally:
            db2.close()
        self.assertEqual(rres["restored_preimage_sha"], captured["sha"])
        # 원 상태 재현: 2/13 은 A75602 로, 8/14 는 다시 부재
        feb = self._row(FEB)
        self.assertEqual(feb.contract_code, "A75602")
        self.assertEqual(feb.close, Decimal("1441.000000"))
        self.assertIsNone(self._row(AUG))

    def test_revert_delete_rowcount_zero_aborts(self):
        """되돌릴 대상이 이미 없으면 조용히 넘기지 않는다."""
        db = self.Session()
        try:
            radapters = R.build_revert_adapters(db)
            plan = [{"date_kst": str(AUG), "action": "delete"}]
            with self.assertRaises(R.RepairAbort) as ctx:
                R.apply_revert_plan(plan=plan, expected_sha="0" * 64,
                                    expected_postimage_sha=R._NO_RECEIPT_SENTINEL,
                                    **radapters)
            db.rollback()
        finally:
            db.close()
        # 표식 단계가 rowcount 보다 **먼저** 잡는다 — 더 이른 방어가 맞다
        self.assertIn("이미 없다", str(ctx.exception))

    def test_isolated_schema_not_public(self):
        """격리가 실제로 걸렸는가 — public에 테이블을 만들지 않았다."""
        from sqlalchemy import text as sql_text

        with self.base_engine.connect() as conn:
            in_public = conn.execute(sql_text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='source_daily_rates'"
            )).scalar()
            in_mine = conn.execute(sql_text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=:s AND table_name='source_daily_rates'"
            ), {"s": self.schema}).scalar()
        self.assertIsNone(in_public, "public schema에 테이블을 만들었다 — 격리 실패")
        self.assertEqual(in_mine, 1)

    def setUp(self):
        with self.Session() as s:
            s.query(self.Model).delete()
            s.commit()

    def _seed_feb(self, contract="A75602"):
        with self.Session() as s:
            s.add(self.Model(
                source=R.SOURCE, asset=R.ASSET, date_kst=FEB,
                rate=Decimal("1441.0"), close=Decimal("1441.0"),
                high=Decimal("1445.0"), low=Decimal("1438.0"),
                ohlc_quality=R.OHLC_QUALITY, close_basis=R.CLOSE_BASIS,
                source_method=R.SOURCE_METHOD, contract_code=contract,
                metadata_json={}))
            s.commit()

    def _fetch_fn_ok(self):
        """실제 primitive 경로를 타되 HTTP만 대체 — raw KRX row를 돌려준다."""
        def fetch(d: date):
            month = "202603" if d == FEB else "202609"
            code = "A75603" if d == FEB else "A75609"
            return [{
                "BAS_DD": d.strftime("%Y%m%d"), "PROD_NM": "미국달러 선물",
                "MKT_NM": "정규", "ISU_NM": f"미국달러 F {month} (주간)",
                "ISU_CD": code, "TDD_CLSPRC": "1443.5", "TDD_HGPRC": "1446.0",
                "TDD_LWPRC": "1440.0", "TDD_OPNPRC": "1442.0",
                "SETL_PRC": "1443.5", "SPOT_PRC": "1443.0",
                "ACC_TRDVOL": "100", "ACC_OPNINT_QTY": "200",
            }]
        return fetch

    def _run(self, *, write, fetch_fn=None, session=None, persist=None):
        db = session or self.Session()
        self.persisted = []
        try:
            adapters = R.build_db_adapters(db, fetch_fn or self._fetch_fn_ok())
            result = R.repair_all(
                write=write,
                persist_preimage=persist or (
                    lambda fp, sha: self.persisted.append((fp, sha))),
                **adapters)
            if write:
                db.commit()
            else:
                db.rollback()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            if session is None:
                db.close()

    def _row(self, d):
        with self.Session() as s:
            return s.query(self.Model).filter_by(source=R.SOURCE, asset=R.ASSET,
                                                 date_kst=d).one_or_none()

    def test_write_applies_both_dates_atomically(self):
        self._seed_feb()
        result = self._run(write=True)
        self.assertEqual(result["mode"], "write")
        self.assertEqual(self._row(FEB).contract_code, "A75603")
        self.assertEqual(self._row(AUG).contract_code, "A75609")
        self.assertEqual(self._row(FEB).close, Decimal("1443.500000"))

    def test_update_refreshes_captured_at(self):
        """`captured_at` = "row 마지막 update 시각" 계약 (공용 upsert와 동일).

        정정된 행이 정정 **전** 시각을 달고 남으면, 나중에 "이 행은 언제 쓰인
        값인가"를 묻는 쪽이 틀린 답을 얻는다. 원래 값은 preimage에 보존된다.
        """
        from datetime import datetime, timedelta, timezone

        stale = datetime(2026, 2, 14, tzinfo=timezone.utc)
        self._seed_feb()
        with self.Session() as s:
            row = s.query(self.Model).filter_by(date_kst=FEB).one()
            row.captured_at = stale
            s.commit()

        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        self._run(write=True)
        got = self._row(FEB).captured_at
        self.assertIsNotNone(got)
        if got.tzinfo is None:
            got = got.replace(tzinfo=timezone.utc)
        self.assertGreater(got, before,
                           f"captured_at이 갱신되지 않았다: {got}")

    def test_dry_run_writes_nothing(self):
        self._seed_feb()
        result = self._run(write=False)
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(self._row(FEB).contract_code, "A75602")  # 원본 유지
        self.assertIsNone(self._row(AUG))

    def test_preimage_contains_real_id(self):
        self._seed_feb()
        result = self._run(write=False)
        fp = json.loads(result["preimage"])
        self.assertIsNotNone(fp[str(FEB)]["id"])
        self.assertEqual(fp[str(FEB)]["contract_code"], "A75602")
        self.assertIsNone(fp[str(AUG)])

    def test_second_date_failure_rolls_back_first(self):
        """⭐ 8/14 실패 시 2/13 UPDATE도 함께 사라져야 한다 (한 transaction)."""
        self._seed_feb()

        def bad_fetch(d):
            if d == AUG:
                raise R.RepairAbort("의도적 fetch 실패")
            return self._fetch_fn_ok()(d)

        with self.assertRaises(R.RepairAbort):
            self._run(write=True, fetch_fn=bad_fetch)
        self.assertEqual(self._row(FEB).contract_code, "A75602")  # rollback 확인
        self.assertIsNone(self._row(AUG))

    def test_post_verify_failure_rolls_back_everything(self):
        """write는 됐지만 post-verify가 실패하면 둘 다 되돌아간다."""
        self._seed_feb()

        def drifting_fetch(d):
            row = self._fetch_fn_ok()(d)
            if d == AUG:
                row[0]["TDD_CLSPRC"] = "1443.5"
            return row

        db = self.Session()
        adapters = R.build_db_adapters(db, drifting_fetch)
        original_reread = adapters["reread"]

        def sabotaged_reread(d):
            row = original_reread(d)
            if row and d == AUG:
                row = {**row, "close": Decimal("9999.0")}  # 반영 안 된 것처럼
            return row

        adapters["reread"] = sabotaged_reread
        try:
            with self.assertRaises(R.RepairAbort):
                R.repair_all(write=True, persist_preimage=_noop_persist,
                             **adapters)
            db.rollback()
        finally:
            db.close()
        self.assertEqual(self._row(FEB).contract_code, "A75602")
        self.assertIsNone(self._row(AUG))

    def test_update_rowcount_zero_aborts(self):
        """조건부 UPDATE가 0행이면 사전 조건 확인 이후 행이 바뀐 것 → abort."""
        self._seed_feb(contract="A75602")
        db = self.Session()
        adapters = R.build_db_adapters(db, self._fetch_fn_ok())
        original_update = adapters["apply_update"]

        def zero_update(target, row):
            original_update(target, row)
            return 0  # rowcount 0 시뮬레이션

        adapters["apply_update"] = zero_update
        try:
            with self.assertRaises(R.RepairAbort) as ctx:
                R.repair_all(write=True, persist_preimage=_noop_persist,
                             **adapters)
            db.rollback()
        finally:
            db.close()
        self.assertIn("rowcount", str(ctx.exception))

    def _insert_competing_aug_row(self):
        with self.Session() as s:
            s.add(self.Model(
                source=R.SOURCE, asset=R.ASSET, date_kst=AUG,
                rate=Decimal("1"), close=Decimal("1"), high=Decimal("1"),
                low=Decimal("1"), ohlc_quality=R.OHLC_QUALITY,
                close_basis=R.CLOSE_BASIS, source_method="close_finalizer",
                contract_code="OTHER", metadata_json={}))
            s.commit()

    def test_precondition_catches_preexisting_aug_row(self):
        """행이 **처음부터** 있으면 사전 조건에서 멈춘다 (write 0)."""
        self._seed_feb()
        self._insert_competing_aug_row()
        with self.assertRaises(R.RepairAbort) as ctx:
            self._run(write=True)
        self.assertIn("이미 있다", str(ctx.exception))
        self.assertEqual(self._row(AUG).contract_code, "OTHER")
        self.assertEqual(self._row(FEB).contract_code, "A75602")  # 2/13 미변경

    def test_insert_unique_conflict_aborts_not_overwrites(self):
        """⭐ **사전 조건 통과 후** 경합 writer가 끼어들면 덮지 않고 abort.

        공용 `upsert`는 `ON CONFLICT DO UPDATE`라 조용히 덮는다 — 그래서
        plain INSERT를 쓴다. 사전에 행을 넣어 두면 precondition에서 먼저
        걸려 **이 경로에 도달하지 못하므로**(codex 감사 지적), 경합 행을
        fetch 구간에 끼워 넣어 실제 `IntegrityError` 경로를 태운다.
        """
        self._seed_feb()
        base_fetch = self._fetch_fn_ok()
        state = {"injected": False}

        def fetch_with_race(d: date):
            # 사전 조건 통과 뒤, write 전에 다른 writer가 commit한 상황.
            if d == AUG and not state["injected"]:
                state["injected"] = True
                self._insert_competing_aug_row()
            return base_fetch(d)

        with self.assertRaises(R.RepairAbort) as ctx:
            self._run(write=True, fetch_fn=fetch_with_race)
        self.assertTrue(state["injected"], "경합 행이 주입되지 않았다")
        self.assertIn("unique 충돌", str(ctx.exception))
        # 경합 행이 그대로 남아 있어야 한다 (덮이지 않았다)
        self.assertEqual(self._row(AUG).contract_code, "OTHER")
        self.assertEqual(self._row(AUG).source_method, "close_finalizer")
        # 그리고 2/13 UPDATE도 함께 rollback돼야 한다 (원자성)
        self.assertEqual(self._row(FEB).contract_code, "A75602")

    def test_precondition_mismatch_writes_nothing(self):
        self._seed_feb(contract="A75603")  # 이미 교정된 상태
        with self.assertRaises(R.RepairAbort):
            self._run(write=True)
        self.assertIsNone(self._row(AUG))

    def test_stale_bas_dd_aborts(self):
        """요청일과 다른 BAS_DD는 쓰지 않는다 (2026-05-25 사고와 동형)."""
        self._seed_feb()

        def stale_fetch(d):
            rows = self._fetch_fn_ok()(d)
            rows[0]["BAS_DD"] = "20260212"
            return rows

        with self.assertRaises(R.RepairAbort) as ctx:
            self._run(write=True, fetch_fn=stale_fetch)
        self.assertIn("BAS_DD", str(ctx.exception))
        self.assertEqual(self._row(FEB).contract_code, "A75602")

    def test_for_update_lock_is_held(self):
        """`lock_and_read`가 실제로 행 잠금을 잡는가 — 두 번째 세션이 대기한다."""
        from sqlalchemy import text as sql_text

        self._seed_feb()
        db1 = self.Session()
        try:
            adapters = R.build_db_adapters(db1, self._fetch_fn_ok())
            adapters["lock_and_read"](FEB)          # 잠금 획득
            with self.Session() as db2:
                db2.execute(sql_text("SET LOCAL lock_timeout = '300ms'"))
                from sqlalchemy import select
                from sqlalchemy.exc import OperationalError
                with self.assertRaises(OperationalError):
                    # ORM select — 손으로 쓴 테이블 이름은 격리 schema를 우회한다.
                    db2.execute(select(self.Model.id)
                                .where(self.Model.date_kst == FEB)
                                .with_for_update())
                db2.rollback()
        finally:
            db1.rollback()
            db1.close()


if __name__ == "__main__":
    unittest.main()
