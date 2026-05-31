"""Daily append orchestrator 단위 테스트 (ADR-034 Phase 2d PR A1).

검증:
- evaluate_source_result fail-closed verdict 판정 (written/skipped/error/exit-nonzero/
  sentinel 누락·중복·malformed·schema mismatch·rows mismatch)
- build_command: write 시 --emit-daily-append-verdict 포함 / validation 시 미포함
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path

# scripts/ + repo root 경로
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daily_append_source_daily_rates as O  # noqa: E402
from app.daily_append_verdict import SENTINEL_PREFIX  # noqa: E402

D = date(2026, 5, 28)


def sentinel(*, source="bithumb", asset="usdt-krw", date_kst="2026-05-28",
            status="written", reason=None, rows=1, version=1) -> str:
    payload = {
        "version": version, "source": source, "asset": asset,
        "date_kst": date_kst, "status": status, "reason": reason, "rows": rows,
    }
    return SENTINEL_PREFIX + json.dumps(payload, ensure_ascii=False)


class TestEvaluateSourceResult(unittest.TestCase):

    def test_written_pass(self):
        out = "로그 라인\n" + sentinel(status="written", rows=1) + "\n추가 로그"
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "PASS", detail)

    def test_written_rows_mismatch_fail(self):
        s, detail = O.evaluate_source_result("bithumb", D, 0, sentinel(status="written", rows=2))
        self.assertEqual(s, "FAIL")
        self.assertIn("rows", detail)

    def test_skipped_weekend_pass(self):
        out = sentinel(source="hana", asset="usd-krw", status="skipped",
                       reason="weekend_no_changes", rows=0)
        s, detail = O.evaluate_source_result("hana", D, 0, out)
        self.assertEqual(s, "PASS", detail)

    def test_skipped_holiday_pass(self):
        out = sentinel(source="hana", asset="usd-krw", status="skipped",
                       reason="holiday_no_changes", rows=0)
        s, detail = O.evaluate_source_result("hana", D, 0, out)
        self.assertEqual(s, "PASS", detail)

    def test_skipped_disallowed_reason_fail(self):
        # hana skipped이나 business_day_no_changes는 허용 reason 아님 (hana로 — bithumb은 skipped 자체 불가)
        out = sentinel(source="hana", asset="usd-krw", status="skipped",
                       reason="business_day_no_changes", rows=0)
        s, detail = O.evaluate_source_result("hana", D, 0, out)
        self.assertEqual(s, "FAIL")
        self.assertIn("reason", detail)

    def test_skipped_nonzero_rows_fail(self):
        out = sentinel(source="hana", asset="usd-krw", status="skipped",
                       reason="weekend_no_changes", rows=1)
        s, detail = O.evaluate_source_result("hana", D, 0, out)
        self.assertEqual(s, "FAIL")

    def test_error_status_fail(self):
        out = sentinel(source="hana", asset="usd-krw", status="error",
                       reason="business_day_no_changes", rows=0)
        s, detail = O.evaluate_source_result("hana", D, 1, out)
        self.assertEqual(s, "FAIL")

    def test_exit_nonzero_always_fail(self):
        # written sentinel이 있어도 exit nonzero면 FAIL
        s, detail = O.evaluate_source_result("bithumb", D, 1, sentinel(status="written", rows=1))
        self.assertEqual(s, "FAIL")

    def test_exit0_no_sentinel_fail(self):
        out = "로그만 있고 sentinel 없음\n[Bithumb write 완료] 1 rows committed"
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")
        self.assertIn("정확히 1개", detail)

    def test_exit0_duplicate_sentinel_fail(self):
        out = sentinel(rows=1) + "\n" + sentinel(rows=1)
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")

    def test_malformed_json_fail(self):
        out = SENTINEL_PREFIX + "{not valid json"
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")
        self.assertIn("malformed", detail)

    def test_schema_source_mismatch_fail(self):
        # 기대 bithumb인데 verdict source=hana
        out = sentinel(source="hana", status="written", rows=1)
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")
        self.assertIn("source", detail)

    def test_schema_date_mismatch_fail(self):
        out = sentinel(date_kst="2026-05-27", status="written", rows=1)
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")
        self.assertIn("date_kst", detail)

    def test_schema_version_mismatch_fail(self):
        out = sentinel(version=999, status="written", rows=1)
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")

    # ── Codex adversarial Blockers (fail-closed 잠금) ──

    def test_non_object_json_fail(self):
        """B1: 유효 JSON이라도 object 아니면 crash 대신 FAIL (list/null/string/number)."""
        for raw in ("[]", "null", '"text"', "123"):
            out = SENTINEL_PREFIX + raw
            s, detail = O.evaluate_source_result("bithumb", D, 0, out)
            self.assertEqual(s, "FAIL", msg=f"{raw} should FAIL")
            self.assertIn("object 아님", detail)

    def test_asset_mismatch_fail(self):
        """B2: source별 expected asset 불일치 거부."""
        out = sentinel(asset="WRONG", status="written", rows=1)
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")
        self.assertIn("asset", detail)

    def test_bithumb_skipped_rejected(self):
        """B3: Bithumb은 24/7 source — skipped 비허용 (source-aware freshness guard)."""
        out = sentinel(source="bithumb", status="skipped", reason="weekend_no_changes", rows=0)
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")
        self.assertIn("비허용", detail)

    def test_bool_rows_rejected(self):
        """B4: rows=True는 type(rows) is int 검사로 거부 (True==1 회피)."""
        out = sentinel(status="written", reason=None, rows=True)
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")
        self.assertIn("rows type", detail)

    def test_str_rows_rejected(self):
        """B4: rows="1" (str)도 거부."""
        out = sentinel(status="written", reason=None, rows="1")
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")

    def test_written_nonnull_reason_rejected(self):
        """NB: written은 reason None 필수."""
        out = sentinel(status="written", reason="unexpected", rows=1)
        s, detail = O.evaluate_source_result("bithumb", D, 0, out)
        self.assertEqual(s, "FAIL")
        self.assertIn("reason", detail)


class TestBuildCommand(unittest.TestCase):

    def test_write_includes_emit_flag(self):
        cmd = O.build_command("bithumb", D, write=True, allow_production=False)
        self.assertIn("--write", cmd)
        self.assertIn("--emit-daily-append-verdict", cmd)

    def test_validation_command_no_emit_no_write(self):
        cmd = O.build_command("bithumb", D, write=False, allow_production=False)
        self.assertNotIn("--write", cmd)
        self.assertNotIn("--emit-daily-append-verdict", cmd)

    def test_allow_production_forwarded(self):
        cmd = O.build_command("bithumb", D, write=True, allow_production=True)
        self.assertIn("--allow-production-write", cmd)

    def test_unsupported_source_raises(self):
        with self.assertRaises(ValueError):
            O.build_command("krx", D, write=True, allow_production=False)


class TestCliEmitGuards(unittest.TestCase):
    """writer CLI의 --emit-daily-append-verdict 제약 (guard는 DB 접근 전 fire → DB 불필요).

    Codex finding: emit-without-write / multi-day+emit 차단을 영구 test로 잠금.
    """

    REPO = Path(__file__).resolve().parent.parent
    HANA = str(REPO / "scripts" / "backfill_hana_observed_eod_source_daily_rates.py")
    BITHUMB = str(REPO / "scripts" / "backfill_bithumb_source_daily_rates.py")

    def _run(self, script, args):
        import subprocess
        return subprocess.run(
            [sys.executable, script, *args],
            capture_output=True, text=True, cwd=str(self.REPO),
        )

    def test_hana_emit_without_write_rejected(self):
        r = self._run(self.HANA, ["--emit-daily-append-verdict"])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn("CONFIG 실패", r.stdout)

    def test_hana_multiday_emit_rejected(self):
        r = self._run(self.HANA, [
            "--write", "--start-date", "2026-05-27", "--end-date", "2026-05-28",
            "--emit-daily-append-verdict",
        ])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn("단일 날짜", r.stdout)

    def test_bithumb_emit_without_write_rejected(self):
        r = self._run(self.BITHUMB, ["--emit-daily-append-verdict"])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn("CONFIG 실패", r.stdout)

    def test_bithumb_multiday_emit_rejected(self):
        r = self._run(self.BITHUMB, [
            "--write", "--start-date", "2026-05-27", "--end-date", "2026-05-28",
            "--emit-daily-append-verdict",
        ])
        self.assertEqual(r.returncode, 1, msg=r.stdout[-300:])
        self.assertIn("단일 날짜", r.stdout)


if __name__ == "__main__":
    unittest.main()
