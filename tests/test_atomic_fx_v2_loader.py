"""P1b C6-3 — single-snapshot v2 loader 단위 테스트 (§15:215, dormant).

FxV2LoadResult invariant(effective ⊆ present) + _extract_v2_revision(v2-valid만, codex amend 2 broader) +
_read_v2_source(same-read, stale, miss) + load_fx_topic_payload_with_revisions(all-v2 effective==present /
mixed·v1·DB-fallback effective⊊present / client None) + present==B2a 추출 + dormancy(live caller 0).
"""
from __future__ import annotations

import ast
import json
import pathlib
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app import atomic_fx_v2_loader as m
from app.atomic_build import _extract_present_sources
from app.atomic_fx_v2_loader import (
    FxV2LoadResult,
    _extract_v2_revision,
    _read_v2_source,
    load_fx_topic_payload_with_revisions,
)
from app.atomic_value_schema import make_rate_key, make_revision_key
from app.crud import BANK_DISPLAY_ORDER
from app.fx_membership import FX_MEMBERSHIP_SOURCES
from app.latest_rates_cache import latest_key_bank, latest_key_investing

_REV = make_revision_key(1_700_000_000_000_000, 5)


def _fresh_mirrored():
    return datetime.now(timezone.utc).isoformat()


def _v2_raw(rate, *, revision=_REV, rate_key=None, schema=2, mirrored=None):
    obj = {
        "rate": rate, "timestamp": "2026-06-19T09:00:00+09:00",
        "mirrored_at": mirrored or _fresh_mirrored(),
        "revision_key": revision, "rate_key": rate_key if rate_key is not None else make_rate_key(rate),
    }
    if schema is not None:
        obj["schema_version"] = schema
    return json.dumps(obj)


def _v1_raw(rate, *, mirrored=None):
    # v1 = schema_version 부재 + revision_key/rate_key 없음
    return json.dumps({"rate": rate, "timestamp": "2026-06-19T09:00:00+09:00",
                       "mirrored_at": mirrored or _fresh_mirrored()})


class _FakeClient:
    def __init__(self, kv):
        self.kv = kv

    def get(self, key):
        return self.kv.get(key)


class TestExtractV2Revision(unittest.TestCase):

    def test_v2_valid(self):
        self.assertEqual(_extract_v2_revision(_v2_raw(1300.0), 1300.0), _REV)

    def test_v1_no_schema(self):
        self.assertIsNone(_extract_v2_revision(_v1_raw(1300.0), 1300.0))

    def test_wrong_schema(self):
        self.assertIsNone(_extract_v2_revision(_v2_raw(1300.0, schema=3), 1300.0))

    def test_non_json(self):
        self.assertIsNone(_extract_v2_revision("not-json", 1300.0))

    def test_bad_revision_key(self):
        self.assertIsNone(_extract_v2_revision(_v2_raw(1300.0, revision="not-a-rev"), 1300.0))

    def test_non_str_revision(self):
        self.assertIsNone(_extract_v2_revision(json.dumps(
            {"schema_version": 2, "revision_key": 123, "rate_key": make_rate_key(1300.0)}), 1300.0))

    def test_rate_key_mismatch_fail_closed(self):
        # codex amend 2: rate_key != make_rate_key(public rate) → omit (tamper/drift)
        self.assertIsNone(_extract_v2_revision(_v2_raw(1300.0, rate_key="999.9"), 1300.0))

    def test_rate_key_missing(self):
        self.assertIsNone(_extract_v2_revision(json.dumps(
            {"schema_version": 2, "revision_key": _REV}), 1300.0))

    def test_schema_float_2_0_fail_closed(self):
        # float 2.0은 ==2 True지만 type-strict로 거부 (revision_key/rate_key isinstance와 fail-closed parity)
        self.assertIsNone(_extract_v2_revision(_v2_raw(1300.0, schema=2.0), 1300.0))

    def test_schema_bool_fail_closed(self):
        # JSON true → bool True (type(True) is bool) → 거부
        self.assertIsNone(_extract_v2_revision(_v2_raw(1300.0, schema=True), 1300.0))


class TestReadV2Source(unittest.TestCase):

    def test_v2_hit_returns_entry_and_revision(self):
        c = _FakeClient({latest_key_bank("kb", "usd-krw"): _v2_raw(1300.0)})
        entry, rev = _read_v2_source(c, latest_key_bank("kb", "usd-krw"), "kb", "usd-krw")
        self.assertEqual(entry, {"source": "kb", "asset": "usd-krw", "rate": 1300.0,
                                 "timestamp": "2026-06-19T09:00:00+09:00"})
        self.assertEqual(rev, _REV)

    def test_v1_hit_entry_no_revision(self):
        c = _FakeClient({latest_key_bank("kb", "usd-krw"): _v1_raw(1300.0)})
        entry, rev = _read_v2_source(c, latest_key_bank("kb", "usd-krw"), "kb", "usd-krw")
        self.assertIsNotNone(entry)
        self.assertIsNone(rev)   # v1 → payload엔 들어가나 vector 없음

    def test_miss_none(self):
        c = _FakeClient({})
        self.assertEqual(_read_v2_source(c, "absent", "kb", "usd-krw"), (None, None))

    def test_stale_none(self):
        old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
        c = _FakeClient({latest_key_bank("kb", "usd-krw"): _v2_raw(1300.0, mirrored=old)})
        self.assertEqual(_read_v2_source(c, latest_key_bank("kb", "usd-krw"), "kb", "usd-krw"), (None, None))

    def test_get_exception_none(self):
        class _Boom:
            def get(self, key): raise RuntimeError("redis down")
        self.assertEqual(_read_v2_source(_Boom(), "k", "kb", "usd-krw"), (None, None))

    def test_timestamp_none_entry_kept_revision_omitted(self):
        # Workflow HIGH: v2-valid이나 timestamp=None → _normalize_entry가 drop할 entry → revision omit.
        # entry는 유지(live parity: append→normalize drop, DB-fallback 미유발).
        raw = json.dumps({"rate": 1300.0, "timestamp": None, "mirrored_at": _fresh_mirrored(),
                          "revision_key": _REV, "rate_key": make_rate_key(1300.0), "schema_version": 2})
        c = _FakeClient({latest_key_bank("kb", "usd-krw"): raw})
        entry, rev = _read_v2_source(c, latest_key_bank("kb", "usd-krw"), "kb", "usd-krw")
        self.assertIsNotNone(entry)              # entry 유지
        self.assertEqual(entry["timestamp"], None)
        self.assertIsNone(rev)                   # revision omit (effective ⊆ present 보존)


class TestFxV2LoadResultInvariant(unittest.TestCase):

    def _payload(self, sources):
        banks = [{"source": s, "asset": "usd-krw", "rate": 1300.0, "timestamp": "t"}
                 for s in sources if s != "investing"]
        data = {"banks": banks}
        if "investing" in sources:
            data["reference"] = {"source": "investing", "asset": "usd-krw", "rate": 1301.0, "timestamp": "t"}
        return {"type": "snapshot", "version": 1, "data": data}

    def test_effective_subset_ok(self):
        pl = self._payload(["kb", "hana", "investing"])
        r = FxV2LoadResult(payload=pl, effective_revision_vector={"kb": _REV},
                           present_sources=tuple(sorted(_extract_present_sources(pl))))
        self.assertEqual(r.effective_revision_vector, {"kb": _REV})

    def test_effective_exceeds_present_raises(self):
        pl = self._payload(["kb"])
        with self.assertRaises(ValueError):
            FxV2LoadResult(payload=pl, effective_revision_vector={"hana": _REV}, present_sources=("kb",))

    def test_present_exceeds_membership_raises(self):
        with self.assertRaises(ValueError):
            FxV2LoadResult(payload={"type": "s", "version": 1, "data": {"banks": []}},
                           effective_revision_vector={}, present_sources=("ghost",))

    def test_bad_revision_value_raises(self):
        pl = self._payload(["kb"])
        with self.assertRaises(ValueError):
            FxV2LoadResult(payload=pl, effective_revision_vector={"kb": "not-a-rev"}, present_sources=("kb",))


class TestLoader(unittest.TestCase):
    """load_fx_topic_payload_with_revisions — Redis/DB mock으로 effective ⊆ present 분기 검증."""

    def _run(self, *, kv, db_banks=None, db_ref=None):
        client = _FakeClient(kv)
        with patch.object(m, "_get_sync_client", return_value=client), \
             patch.object(m, "select_latest_bank_rates_from_db", return_value=db_banks or []), \
             patch.object(m, "select_a_latest_investing_rate_from_db", return_value=db_ref):
            return load_fx_topic_payload_with_revisions(db=object(), asset="usd-krw")

    def _all_v2_kv(self):
        kv = {latest_key_bank(b, "usd-krw"): _v2_raw(1300.0 + i) for i, b in enumerate(BANK_DISPLAY_ORDER)}
        kv[latest_key_investing("usd-krw")] = _v2_raw(1301.0)
        return kv

    def test_all_v2_effective_equals_present(self):
        r = self._run(kv=self._all_v2_kv())
        self.assertEqual(set(r.effective_revision_vector), set(r.present_sources))   # 완전 migration
        self.assertEqual(set(r.present_sources), FX_MEMBERSHIP_SOURCES)
        self.assertEqual(set(r.present_sources), _extract_present_sources(r.payload))

    def test_mixed_v1_v2_effective_strict_subset(self):
        kv = self._all_v2_kv()
        kv[latest_key_bank("kb", "usd-krw")] = _v1_raw(1300.0)   # kb만 v1
        r = self._run(kv=kv)
        self.assertIn("kb", r.present_sources)            # payload엔 있음
        self.assertNotIn("kb", r.effective_revision_vector)  # vector엔 없음(v1)
        self.assertTrue(set(r.effective_revision_vector) < set(r.present_sources))  # strict subset

    def test_db_fallback_in_payload_not_in_vector(self):
        kv = self._all_v2_kv()
        del kv[latest_key_bank("hana", "usd-krw")]   # hana Redis miss
        r = self._run(kv=kv, db_banks=[{"bank": "hana", "asset": "usd-krw", "rate": 1305.0, "timestamp": "t"}])
        self.assertIn("hana", r.present_sources)              # DB fallback로 payload 포함
        self.assertNotIn("hana", r.effective_revision_vector)  # revision 없음(no fabrication)

    def test_all_v1_empty_vector(self):
        kv = {latest_key_bank(b, "usd-krw"): _v1_raw(1300.0) for b in BANK_DISPLAY_ORDER}
        kv[latest_key_investing("usd-krw")] = _v1_raw(1301.0)
        r = self._run(kv=kv)
        self.assertEqual(r.effective_revision_vector, {})           # 전부 v1 → vector empty
        self.assertEqual(set(r.present_sources), FX_MEMBERSHIP_SOURCES)  # payload는 full

    def test_client_none_all_db_fallback(self):
        with patch.object(m, "_get_sync_client", return_value=None), \
             patch.object(m, "select_latest_bank_rates_from_db",
                          return_value=[{"bank": b, "asset": "usd-krw", "rate": 1300.0, "timestamp": "t"}
                                        for b in BANK_DISPLAY_ORDER]), \
             patch.object(m, "select_a_latest_investing_rate_from_db",
                          return_value={"source": "investing", "asset": "usd-krw", "rate": 1301.0, "timestamp": "t"}):
            r = load_fx_topic_payload_with_revisions(db=object(), asset="usd-krw")
        self.assertEqual(r.effective_revision_vector, {})   # client 없음 → revision 0

    def test_invalid_asset_raises(self):
        # invalid asset은 _validate_fx_asset에서 즉시 ValueError (Redis/DB 접근 전)
        with self.assertRaises(ValueError):
            load_fx_topic_payload_with_revisions(db=object(), asset="usdt-krw")

    def test_timestamp_none_absent_from_present_and_effective(self):
        # Workflow HIGH trip-wire: all-v2이나 kb timestamp=None → present·effective 둘 다에서 빠짐
        # (effective-only 금지), ValueError 없이 정상 반환 (live silent-drop parity).
        kv = self._all_v2_kv()
        kv[latest_key_bank("kb", "usd-krw")] = json.dumps(
            {"rate": 1300.0, "timestamp": None, "mirrored_at": _fresh_mirrored(),
             "revision_key": _REV, "rate_key": make_rate_key(1300.0), "schema_version": 2})
        r = self._run(kv=kv)   # no crash
        self.assertNotIn("kb", r.present_sources)
        self.assertNotIn("kb", r.effective_revision_vector)

    def test_investing_v1_reference_in_payload_not_in_vector(self):
        # 참조축 subset: investing만 v1 → reference는 payload/present에 있되 effective엔 없음
        kv = self._all_v2_kv()
        kv[latest_key_investing("usd-krw")] = _v1_raw(1301.0)
        r = self._run(kv=kv)
        self.assertIn("investing", r.present_sources)
        self.assertNotIn("investing", r.effective_revision_vector)
        self.assertTrue(set(r.effective_revision_vector) < set(r.present_sources))

    def test_investing_stale_db_fallback_not_in_vector(self):
        # 참조축 subset: investing v2 stale → DB-fallback로 reference 채움 + effective omit (codex amend 1)
        old = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()
        kv = self._all_v2_kv()
        kv[latest_key_investing("usd-krw")] = _v2_raw(1301.0, mirrored=old)
        r = self._run(kv=kv, db_ref={"source": "investing", "asset": "usd-krw",
                                     "rate": 1302.0, "timestamp": "t"})
        self.assertIn("investing", r.present_sources)
        self.assertNotIn("investing", r.effective_revision_vector)
        self.assertTrue(set(r.effective_revision_vector) < set(r.present_sources))

    def test_payload_byte_identical_to_live_loader(self):
        # Workflow MEDIUM: payload == live load_and_build_fx_topic_payload (동일 Redis/DB fixture).
        # C6-6 publish 등가의 핵심 불변 — 두 loader를 같은 fixture로 돌려 payload 일치 잠금.
        from app import fx_topic_payload as live
        from app import latest_rates_cache as lrc

        def _both(kv, db_banks, db_ref):
            cl_live = _FakeClient(kv) if kv is not None else None
            cl_c6 = _FakeClient(kv) if kv is not None else None
            with patch.object(lrc, "_get_sync_client", return_value=cl_live), \
                 patch.object(live, "select_latest_bank_rates_from_db", return_value=db_banks), \
                 patch.object(live, "select_a_latest_investing_rate_from_db", return_value=db_ref):
                lp = live.load_and_build_fx_topic_payload(db=object(), asset="usd-krw")
            with patch.object(m, "_get_sync_client", return_value=cl_c6), \
                 patch.object(m, "select_latest_bank_rates_from_db", return_value=db_banks), \
                 patch.object(m, "select_a_latest_investing_rate_from_db", return_value=db_ref):
                cp = load_fx_topic_payload_with_revisions(db=object(), asset="usd-krw").payload
            return lp, cp

        kv_db_miss = {k: v for k, v in self._all_v2_kv().items()
                      if k != latest_key_bank("hana", "usd-krw")}
        db_banks_full = [{"bank": b, "asset": "usd-krw", "rate": 1300.0, "timestamp": "t"}
                         for b in BANK_DISPLAY_ORDER]
        db_ref_full = {"source": "investing", "asset": "usd-krw", "rate": 1301.0, "timestamp": "t"}
        ts_none_kv = {**self._all_v2_kv(), latest_key_bank("kb", "usd-krw"): json.dumps(
            {"rate": 1300.0, "timestamp": None, "mirrored_at": _fresh_mirrored(),
             "revision_key": _REV, "rate_key": make_rate_key(1300.0), "schema_version": 2})}
        scenarios = [
            ("all_v2", self._all_v2_kv(), [], None),
            ("mixed_v1", {**self._all_v2_kv(), latest_key_bank("kb", "usd-krw"): _v1_raw(1300.0)}, [], None),
            # Workflow HIGH parity 회귀 잠금: timestamp=None → live·C6 둘 다 kb drop한 동일 payload
            ("timestamp_none", ts_none_kv, [], None),
            ("db_fallback", kv_db_miss,
             [{"bank": "hana", "asset": "usd-krw", "rate": 1305.0, "timestamp": "t"}], None),
            ("client_none", None, db_banks_full, db_ref_full),
        ]
        for name, kv, db_banks, db_ref in scenarios:
            with self.subTest(name=name):
                lp, cp = _both(kv, db_banks, db_ref)
                self.assertEqual(cp, lp)


class TestDormancy(unittest.TestCase):
    _ISLAND = frozenset({
        "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py", "atomic_write_outcome.py",
        "atomic_cutover.py", "atomic_watermark.py", "atomic_build.py", "atomic_reconcile.py",
        "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py",
        "atomic_fx_v2_loader.py", "atomic_fx_publisher.py", "atomic_watermark_store.py", "atomic_fx_live.py",
    })

    def test_no_live_module_imports_loader(self):
        app_dir = pathlib.Path(m.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "atomic_fx_v2_loader" in node.module:
                    self.fail(f"{rel}: from atomic_fx_v2_loader import — dormant 위반")
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_fx_v2_loader" in a.name:
                            self.fail(f"{rel}: import atomic_fx_v2_loader — dormant 위반")

    def test_no_scheduling(self):
        src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        for needle in ("add_job", "create_task", "Thread(", ".start()", "AsyncIOScheduler"):
            self.assertNotIn(needle, src, f"loader에 scheduling needle '{needle}' — dormant 위반")


if __name__ == "__main__":
    unittest.main()
