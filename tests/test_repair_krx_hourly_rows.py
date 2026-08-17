"""scripts/repair_krx_hourly_rows.py — 순수 계약 + **실제 PostgreSQL 왕복**.

삭제/복원의 rowcount·원자성은 in-memory 스캐폴드로 검증할 수 없다. 전용 schema를
배타 생성해 격리한다(공유 DB의 실테이블을 건드리지 않기 위해).

실행:
    PG_TEST_URL=postgresql+psycopg://... pytest tests/test_repair_krx_hourly_rows.py -p no:asyncio -q
"""
from __future__ import annotations

import ast
import json
import os
import pathlib
import sys
import unittest
import uuid
from datetime import datetime
from decimal import Decimal

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import repair_krx_hourly_rows as H  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PG_URL = os.getenv("PG_TEST_URL", "").strip()
ON_CI = os.getenv("GITHUB_ACTIONS", "").lower() == "true"


def _row(ts: datetime, close="1416.5", **over) -> dict:
    base = {c: None for c in H.PREIMAGE_COLUMNS}
    base.update({
        "id": 1, "source": H.SOURCE, "asset": H.ASSET, "bucket_ts_kst": ts,
        "rate": Decimal(close), "close": Decimal(close),
        "high": Decimal("1417.6"), "low": Decimal("1415.8"),
        "ohlc_quality": "observed_rollup", "close_basis": "krx_observed_hourly",
        "source_method": "observed_rollup", "metadata_json": {"point_count": 57},
    })
    base.update(over)
    return base


def _all_present() -> dict:
    return {ts: _row(ts) for ts in H.DELETE_ALLOWLIST}



def _delete_kwargs(ad: dict) -> dict:
    """`delete_all` 이 받는 어댑터만 추린다 (`apply_insert` 는 복원 전용)."""
    return {k: ad[k] for k in ("lock_and_read", "keep_snapshot", "total_count",
                               "apply_delete", "reread")}

class TestAllowlist(unittest.TestCase):

    def test_exactly_four_buckets(self):
        self.assertEqual(len(H.DELETE_ALLOWLIST), 4)
        self.assertEqual([ts.hour for ts in H.DELETE_ALLOWLIST], [8, 9, 10, 11])
        self.assertTrue(all(ts.date() == datetime(2026, 8, 14).date()
                            for ts in H.DELETE_ALLOWLIST))

    def test_keep_window_is_night_session(self):
        """00~06시는 야간장 — 보정 후에도 A75608이 맞아 **보존**한다."""
        self.assertEqual([ts.hour for ts in H.KEEP_WINDOW], list(range(0, 7)))
        self.assertFalse(set(H.KEEP_WINDOW) & set(H.DELETE_ALLOWLIST))

    def test_noon_to_15_is_not_touched(self):
        """12~15시는 원천 tick이 없다 — 합성도 삭제도 대상이 아니다."""
        hours = {ts.hour for ts in H.DELETE_ALLOWLIST} | {ts.hour for ts in H.KEEP_WINDOW}
        for h in (12, 13, 14, 15):
            self.assertNotIn(h, hours)

    def test_preimage_columns_are_the_complete_model_schema(self):
        """전 컬럼 복원 계약이 모델 컬럼 추가로 조용히 낡지 않는다."""
        from app.models import SourceHourlyRate
        self.assertEqual(
            tuple(c.name for c in SourceHourlyRate.__table__.columns),
            H.PREIMAGE_COLUMNS)


class TestPrecondition(unittest.TestCase):

    def test_all_four_present_passes(self):
        H.check_precondition(_all_present())

    def test_missing_bucket_aborts(self):
        rows = _all_present()
        rows[H.DELETE_ALLOWLIST[0]] = None
        with self.assertRaises(H.RepairAbort) as ctx:
            H.check_precondition(rows)
        self.assertIn("없다", str(ctx.exception))

    def test_contract_code_present_aborts(self):
        """⭐ KRX 시간봉은 월물을 저장하지 않는다 — 값이 있으면 우리 행이 아니다."""
        rows = _all_present()
        rows[H.DELETE_ALLOWLIST[1]] = _row(H.DELETE_ALLOWLIST[1],
                                           contract_code="A75609")
        with self.assertRaises(H.RepairAbort) as ctx:
            H.check_precondition(rows)
        self.assertIn("contract_code", str(ctx.exception))

    def test_wrong_source_aborts(self):
        rows = _all_present()
        rows[H.DELETE_ALLOWLIST[2]] = _row(H.DELETE_ALLOWLIST[2], source="hana")
        with self.assertRaises(H.RepairAbort):
            H.check_precondition(rows)


class TestPostDeleteVerify(unittest.TestCase):

    def test_ok(self):
        H.verify_post_delete({ts: None for ts in H.DELETE_ALLOWLIST},
                             "keep", "keep", 183, 179)

    def test_row_still_present_aborts(self):
        remaining = {ts: None for ts in H.DELETE_ALLOWLIST}
        remaining[H.DELETE_ALLOWLIST[0]] = _row(H.DELETE_ALLOWLIST[0])
        with self.assertRaises(H.RepairAbort):
            H.verify_post_delete(remaining, "keep", "keep", 183, 179)

    def test_keep_window_changed_aborts(self):
        with self.assertRaises(H.RepairAbort) as ctx:
            H.verify_post_delete({ts: None for ts in H.DELETE_ALLOWLIST},
                                 "keep", "CHANGED", 183, 179)
        self.assertIn("보존 창", str(ctx.exception))

    def test_wrong_delta_aborts(self):
        with self.assertRaises(H.RepairAbort) as ctx:
            H.verify_post_delete({ts: None for ts in H.DELETE_ALLOWLIST},
                                 "keep", "keep", 183, 180)   # -3
        self.assertIn("delta", str(ctx.exception))


class TestBinding(unittest.TestCase):

    def _adapters(self, rows, deleted=4):
        state = {"rows": rows, "total": 183}

        def apply_delete():
            state["rows"] = {ts: None for ts in H.DELETE_ALLOWLIST}
            state["total"] -= deleted
            return deleted

        return dict(
            lock_and_read=lambda: state["rows"],
            keep_snapshot=lambda: "KEEP",
            total_count=lambda: state["total"],
            apply_delete=apply_delete,
            reread=lambda: state["rows"],
        )

    def test_sha_mismatch_aborts_before_delete(self):
        calls = []
        ad = self._adapters(_all_present())
        ad["apply_delete"] = lambda: calls.append("del") or 4
        with self.assertRaises(H.RepairAbort) as ctx:
            H.delete_all(write=True, expected_preimage_sha="0" * 64,
                         persist_preimage=lambda *a: None, **ad)
        self.assertIn("preimage", str(ctx.exception))
        self.assertEqual(calls, [], "불일치인데 삭제가 실행됐다")

    def test_persist_failure_blocks_delete(self):
        calls = []
        ad = self._adapters(_all_present())
        ad["apply_delete"] = lambda: calls.append("del") or 4

        def boom(fp, s):
            calls.append("persist")
            raise H.RepairAbort("디스크 가득")

        with self.assertRaises(H.RepairAbort):
            H.delete_all(write=True, persist_preimage=boom, **ad)
        self.assertEqual(calls, ["persist"])

    def test_rowcount_mismatch_aborts(self):
        ad = self._adapters(_all_present(), deleted=3)
        with self.assertRaises(H.RepairAbort) as ctx:
            H.delete_all(write=True, persist_preimage=lambda *a: None, **ad)
        self.assertIn("rowcount", str(ctx.exception))

    def test_dry_run_does_not_persist(self):
        calls = []
        ad = self._adapters(_all_present())
        result = H.delete_all(write=False,
                              persist_preimage=lambda *a: calls.append("p"), **ad)
        self.assertEqual(calls, [])
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(len(result["planned_delete"]), 4)


class TestRestoreContract(unittest.TestCase):
    """복원의 **실패 경로**. 정상 왕복만 보면 이 계약들이 무방비로 남는다."""

    def _plan(self):
        return H.build_restore_plan(json.loads(H.fingerprint(_all_present())))

    def _postimage_sha(self):
        return H.sha(H.fingerprint({ts: None for ts in H.DELETE_ALLOWLIST}))

    def test_restore_rowcount_short_aborts(self):
        """4행을 넣었어야 하는데 덜 들어갔으면 중단."""
        with self.assertRaises(H.RepairAbort) as ctx:
            H.restore_all(
                plan=self._plan(), expected_sha="0" * 64,
                expected_postimage_sha=self._postimage_sha(),
                lock_and_read=lambda: {ts: None for ts in H.DELETE_ALLOWLIST},
                apply_insert=lambda plan: 3,             # 한 건 누락
                reread=lambda: _all_present())
        self.assertIn("rowcount", str(ctx.exception))

    def test_restore_fingerprint_mismatch_aborts(self):
        """복원했는데 원래 지문을 재현하지 못하면 중단 (한 컬럼만 틀려도)."""
        broken = {ts: _row(ts, close="9999.9") for ts in H.DELETE_ALLOWLIST}
        with self.assertRaises(H.RepairAbort) as ctx:
            H.restore_all(
                plan=self._plan(),
                expected_sha=H.sha(H.fingerprint(_all_present())),
                expected_postimage_sha=self._postimage_sha(),
                lock_and_read=lambda: {ts: None for ts in H.DELETE_ALLOWLIST},
                apply_insert=lambda plan: 4,
                reread=lambda: broken)
        self.assertIn("지문", str(ctx.exception))

    def test_restore_refuses_when_rows_already_present(self):
        """복원 대상이 이미 있으면 덮지 않는다."""
        with self.assertRaises(H.RepairAbort) as ctx:
            H.restore_all(
                plan=self._plan(), expected_sha="0" * 64,
                expected_postimage_sha=self._postimage_sha(),
                lock_and_read=lambda: _all_present(),
                apply_insert=lambda plan: 4,
                reread=lambda: _all_present())
        self.assertIn("우리 postimage", str(ctx.exception))

    def test_restore_happy_path(self):
        expected = H.sha(H.fingerprint(_all_present()))
        r = H.restore_all(
            plan=self._plan(), expected_sha=expected,
            expected_postimage_sha=self._postimage_sha(),
            lock_and_read=lambda: {ts: None for ts in H.DELETE_ALLOWLIST},
            apply_insert=lambda plan: 4,
            reread=lambda: _all_present())
        self.assertEqual(r["restored_preimage_sha"], expected)

    def test_restore_requires_commit_receipt_postimage(self):
        with self.assertRaises(H.RepairAbort) as ctx:
            H.restore_all(
                plan=self._plan(), expected_sha="0" * 64,
                expected_postimage_sha=None,
                lock_and_read=lambda: {ts: None for ts in H.DELETE_ALLOWLIST},
                apply_insert=lambda plan: 4,
                reread=lambda: _all_present())
        self.assertIn("postimage", str(ctx.exception))

    def test_restore_rejects_state_changed_after_delete(self):
        with self.assertRaises(H.RepairAbort) as ctx:
            H.restore_all(
                plan=self._plan(), expected_sha="0" * 64,
                expected_postimage_sha="f" * 64,
                lock_and_read=lambda: {ts: None for ts in H.DELETE_ALLOWLIST},
                apply_insert=lambda plan: 4,
                reread=lambda: _all_present())
        self.assertIn("우리 postimage", str(ctx.exception))

    def test_restore_plan_rejects_partial_preimage(self):
        rows = json.loads(H.fingerprint(_all_present()))
        rows.pop(sorted(rows)[0])
        with self.assertRaises(H.RepairAbort) as ctx:
            H.build_restore_plan(rows)
        self.assertIn("빠진", str(ctx.exception))


class TestCliContract(unittest.TestCase):

    def test_write_requires_expected_sha(self):
        args = H.build_parser().parse_args(["--write", "--preimage-out", "/tmp/p"])
        with self.assertRaises(H.RepairAbort) as ctx:
            H._validate_cli(args)
        self.assertIn("expect-preimage-sha", str(ctx.exception))

    def test_revert_and_write_exclusive(self):
        args = H.build_parser().parse_args(
            ["--write", "--expect-preimage-sha", "a" * 64,
             "--revert-from", "/tmp/pre", "--preimage-out", "/tmp/p"])
        with self.assertRaises(H.RepairAbort):
            H._validate_cli(args)

    def test_main_dispatches_to_revert(self):
        """⭐ argparse 가 파싱한다 ≠ main 이 그걸 쓴다."""
        src = (REPO_ROOT / "scripts" / "repair_krx_hourly_rows.py").read_text()
        main_fn = next(n for n in ast.walk(ast.parse(src))
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        body = ast.unparse(main_fn)
        self.assertIn("revert_from", body)
        self.assertIn("_run_revert", body)

    def test_guard_covers_revert_too(self):
        """복원도 DB 를 바꾼다 — guard 가 write 전용이면 안 된다."""
        src = (REPO_ROOT / "scripts" / "repair_krx_hourly_rows.py").read_text()
        main_fn = next(n for n in ast.walk(ast.parse(src))
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        body = ast.unparse(main_fn)
        self.assertIn("mutates", body)

    def test_three_phase_reporting_in_both_paths(self):
        src = (REPO_ROOT / "scripts" / "repair_krx_hourly_rows.py").read_text()
        tree = ast.parse(src)
        for name in ("main", "_run_revert"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
            body = "\n".join(ast.unparse(h) for h in handlers)
            with self.subTest(fn=name):
                self.assertIn("PHASE_COMMITTED", body)
                self.assertIn("PHASE_COMMITTING", body)
                for phase in ("PHASE_COMMITTED", "PHASE_COMMITTING"):
                    branch = next(
                        (n for h in handlers for n in ast.walk(h)
                         if isinstance(n, ast.If) and phase in ast.unparse(n.test)),
                        None)
                    self.assertIsNotNone(branch, f"{name}: {phase} 분기 없음")
                    self.assertFalse(
                        any(isinstance(c, ast.Call)
                            and getattr(c.func, "attr", None) == "rollback"
                            for c in ast.walk(branch)),
                        f"{name}: {phase} 에서 rollback 을 부른다")


class TestArtifact(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / f"h-{uuid.uuid4().hex}"
        self.tmp.mkdir()

    def tearDown(self):
        for c in self.tmp.iterdir():
            c.unlink()
        self.tmp.rmdir()

    def test_exclusive_and_reverify(self):
        p = self.tmp / "pre.json"
        fp = H.fingerprint(_all_present())
        H.write_preimage_artifact(p, fp, H.sha(fp))
        rows, s = H.load_preimage_artifact(p)
        self.assertEqual(s, H.sha(fp))
        self.assertEqual(len(rows), 4)
        with self.assertRaises(H.RepairAbort):
            H.write_preimage_artifact(p, fp, H.sha(fp))

    def test_tampered_artifact_rejected(self):
        p = self.tmp / "pre.json"
        fp = H.fingerprint(_all_present())
        p.write_text(json.dumps({"preimage_sha256": H.sha(fp),
                                 "preimage": fp.replace("1416.5", "9999.9")}))
        with self.assertRaises(H.RepairAbort) as ctx:
            H.load_preimage_artifact(p)
        self.assertIn("sha", str(ctx.exception))

    def test_receipt_links_preimage_and_postimage(self):
        p = self.tmp / "pre.json"
        pre = H.fingerprint(_all_present())
        post = H.fingerprint({ts: None for ts in H.DELETE_ALLOWLIST})
        H.write_preimage_artifact(p, pre, H.sha(pre))
        H.write_commit_receipt(p, {
            "preimage_sha": H.sha(pre), "postimage": post,
            "postimage_sha": H.sha(post), "deleted": [],
            "total_before": 4, "total_after": 0,
        })
        self.assertEqual(
            H.load_postimage_sha(
                p, expected_preimage_sha=H.sha(pre), allow_missing=False),
            H.sha(post))

    def test_missing_receipt_is_fail_closed(self):
        p = self.tmp / "pre.json"
        pre = H.fingerprint(_all_present())
        H.write_preimage_artifact(p, pre, H.sha(pre))
        with self.assertRaises(H.RepairAbort):
            H.load_postimage_sha(
                p, expected_preimage_sha=H.sha(pre), allow_missing=False)


@unittest.skipUnless(
    PG_URL or ON_CI,
    "로컬: PG_TEST_URL 미설정 — PostgreSQL 왕복 skip (CI 에서는 fail)")
class TestAgainstRealPostgres(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not PG_URL:
            raise AssertionError("CI 인데 PG_TEST_URL 이 없다 — 배선이 끊겼다")
        from sqlalchemy import create_engine, text as sql_text
        from sqlalchemy.orm import sessionmaker

        from app.models import SourceHourlyRate

        cls.schema = f"krx_hourly_test_{uuid.uuid4().hex[:12]}"
        base = create_engine(PG_URL, future=True)
        with base.begin() as conn:
            conn.execute(sql_text(f'CREATE SCHEMA "{cls.schema}"'))
        cls.base_engine = base
        cls.engine = base.execution_options(schema_translate_map={None: cls.schema})
        cls.Session = sessionmaker(bind=cls.engine, future=True)
        SourceHourlyRate.__table__.create(cls.engine)
        cls.M = SourceHourlyRate

    @classmethod
    def tearDownClass(cls):
        from sqlalchemy import text as sql_text
        with cls.base_engine.begin() as conn:
            conn.execute(sql_text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        cls.base_engine.dispose()

    def setUp(self):
        with self.Session() as s:
            s.query(self.M).delete()
            s.commit()
        self._seed()

    def _seed(self):
        with self.Session() as s:
            for ts in H.KEEP_WINDOW + H.DELETE_ALLOWLIST:
                s.add(self.M(
                    source=H.SOURCE, asset=H.ASSET, bucket_ts_kst=ts,
                    rate=Decimal("1416.5"), close=Decimal("1416.5"),
                    high=Decimal("1417.6"), low=Decimal("1415.8"),
                    ohlc_quality="observed_rollup",
                    close_basis="krx_observed_hourly",
                    source_method="observed_rollup",
                    metadata_json={"point_count": 57}))
            # 다른 날짜 (불변이어야 함)
            s.add(self.M(
                source=H.SOURCE, asset=H.ASSET,
                bucket_ts_kst=datetime(2026, 8, 13, 14, 0, 0),
                rate=Decimal("1400"), close=Decimal("1400"),
                high=Decimal("1401"), low=Decimal("1399"),
                ohlc_quality="observed_rollup",
                close_basis="krx_observed_hourly",
                source_method="observed_rollup", metadata_json={}))
            s.commit()

    def _count(self):
        with self.Session() as s:
            return s.query(self.M).count()

    def test_delete_then_restore_roundtrip(self):
        """⭐ 삭제 → 복원 왕복이 지문을 재현하는가."""
        before = self._count()
        db = self.Session()
        try:
            ad = _delete_kwargs(H.build_adapters(db))
            captured = {}
            result = H.delete_all(
                write=True,
                persist_preimage=lambda fp, s: captured.update(fp=fp, sha=s),
                **ad)
            db.commit()
        finally:
            db.close()
        self.assertEqual(result["mode"], "write")
        self.assertEqual(self._count(), before - 4)

        db2 = self.Session()
        try:
            ad2 = H.build_adapters(db2)
            plan = H.build_restore_plan(json.loads(captured["fp"]))
            r = H.restore_all(plan=plan, expected_sha=captured["sha"],
                              expected_postimage_sha=H.sha(H.fingerprint(
                                  {ts: None for ts in H.DELETE_ALLOWLIST})),
                              lock_and_read=ad2["lock_and_read"],
                              apply_insert=ad2["apply_insert"],
                              reread=ad2["reread"])
            db2.commit()
        finally:
            db2.close()
        self.assertEqual(r["restored_preimage_sha"], captured["sha"])
        self.assertEqual(self._count(), before)

    def test_keep_window_and_other_days_untouched(self):
        with self.Session() as s:
            keep_before = {str(r.bucket_ts_kst): str(r.close)
                           for r in s.query(self.M)
                           .filter(self.M.bucket_ts_kst.in_(H.KEEP_WINDOW)).all()}
            other_before = s.query(self.M).filter(
                self.M.bucket_ts_kst == datetime(2026, 8, 13, 14, 0, 0)).one().close
        db = self.Session()
        try:
            H.delete_all(write=True, persist_preimage=lambda *a: None,
                         **_delete_kwargs(H.build_adapters(db)))
            db.commit()
        finally:
            db.close()
        with self.Session() as s:
            keep_after = {str(r.bucket_ts_kst): str(r.close)
                          for r in s.query(self.M)
                          .filter(self.M.bucket_ts_kst.in_(H.KEEP_WINDOW)).all()}
            other_after = s.query(self.M).filter(
                self.M.bucket_ts_kst == datetime(2026, 8, 13, 14, 0, 0)).one().close
        self.assertEqual(keep_before, keep_after)
        self.assertEqual(other_before, other_after)

    def test_rerun_after_delete_aborts(self):
        db = self.Session()
        try:
            H.delete_all(write=True, persist_preimage=lambda *a: None,
                         **_delete_kwargs(H.build_adapters(db)))
            db.commit()
        finally:
            db.close()
        db2 = self.Session()
        try:
            with self.assertRaises(H.RepairAbort) as ctx:
                H.delete_all(write=True, persist_preimage=lambda *a: None,
                             **_delete_kwargs(H.build_adapters(db2)))
            db2.rollback()
        finally:
            db2.close()
        self.assertIn("없다", str(ctx.exception))

    def test_post_verify_failure_rolls_back(self):
        """사후 검증 실패 시 삭제가 통째로 되돌아가는가."""
        before = self._count()
        db = self.Session()
        try:
            ad = _delete_kwargs(H.build_adapters(db))
            ad["keep_snapshot"] = lambda _c=[0]: ("A" if _c.append(1) or len(_c) < 3
                                                  else "B")
            with self.assertRaises(H.RepairAbort):
                H.delete_all(write=True, persist_preimage=lambda *a: None, **ad)
            db.rollback()
        finally:
            db.close()
        self.assertEqual(self._count(), before)


if __name__ == "__main__":
    unittest.main()
