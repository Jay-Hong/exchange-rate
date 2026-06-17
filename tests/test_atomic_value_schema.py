"""P1b A3-1 — v2 value schema serialization 단위 테스트 (§17, pure stdlib, dormant).

검증:
- make_revision_key: 고정폭 + lex==numeric order + non-negative guard (음수/non-int fail-closed)
- make_rate_key: canonical decimal(no exponent/trailing-zero), -0.0→"0", NaN/Inf/음수 fail-closed
- serialize_v2_value: public(rate/timestamp/mirrored_at) UNCHANGED + internal(schema_version/
  revision_key/rate_key) additive, raw id 미노출
- atomic_value_schema stdlib-only (Redis/crud import 0)
"""
from __future__ import annotations

import json
import pathlib
import unittest
from datetime import datetime, timezone

from app import atomic_value_schema as avs
from app.atomic_revision import to_canonical_epoch_us


class TestMakeRevisionKey(unittest.TestCase):

    def test_fixed_width_format(self):
        self.assertEqual(avs.make_revision_key(0, 0), "0" * 20 + ":" + "0" * 20)

    def test_lex_order_matches_numeric_order(self):
        # 작은 epoch < 큰 epoch (lex string compare == numeric)
        k_small = avs.make_revision_key(1_700_000_000_000_000, 5)
        k_large = avs.make_revision_key(1_700_000_000_000_001, 5)
        self.assertLess(k_small, k_large)
        # 같은 epoch, id tie-break
        k_id_lo = avs.make_revision_key(1_700_000_000_000_000, 5)
        k_id_hi = avs.make_revision_key(1_700_000_000_000_000, 6)
        self.assertLess(k_id_lo, k_id_hi)

    def test_from_revision_tuple(self):
        self.assertEqual(
            avs.make_revision_key_from_revision((1_700_000_000_000_000, 42)),
            avs.make_revision_key(1_700_000_000_000_000, 42),
        )

    def test_negative_epoch_raises(self):
        with self.assertRaises(ValueError):
            avs.make_revision_key(-1, 5)

    def test_negative_id_raises(self):
        with self.assertRaises(ValueError):
            avs.make_revision_key(1_700_000_000_000_000, -1)

    def test_bool_rejected(self):
        # bool은 int subclass지만 id/epoch로 부적절 — fail-closed
        with self.assertRaises(ValueError):
            avs.make_revision_key(True, 5)
        with self.assertRaises(ValueError):
            avs.make_revision_key(1_700_000_000_000_000, True)

    def test_upper_bound_boundary(self):
        # 10^20-1은 정확히 20자리라 허용, 10^20은 폭 초과라 reject (lex 계약)
        avs.make_revision_key(10 ** 20 - 1, 0)  # OK (20자리)
        with self.assertRaises(ValueError):
            avs.make_revision_key(10 ** 20, 0)
        with self.assertRaises(ValueError):
            avs.make_revision_key(0, 10 ** 20)


class TestMakeRateKey(unittest.TestCase):

    def test_trailing_zero_stripped(self):
        self.assertEqual(avs.make_rate_key(1300.0), "1300")
        self.assertEqual(avs.make_rate_key(1300.50), "1300.5")

    def test_no_exponent(self):
        # normalize()가 1E+3 만들 수 있는 값도 'f' 포맷이라 fixed-point
        self.assertEqual(avs.make_rate_key(1000.0), "1000")
        self.assertNotIn("E", avs.make_rate_key(1000.0))
        self.assertNotIn("e", avs.make_rate_key(100000.0))

    def test_jpy_small_decimal(self):
        self.assertEqual(avs.make_rate_key(957.52), "957.52")

    def test_same_value_same_key(self):
        self.assertEqual(avs.make_rate_key(1300.0), avs.make_rate_key(1300.00))

    def test_signed_zero_normalized(self):
        self.assertEqual(avs.make_rate_key(0.0), "0")
        self.assertEqual(avs.make_rate_key(-0.0), "0")

    def test_nan_raises(self):
        with self.assertRaises(ValueError):
            avs.make_rate_key(float("nan"))

    def test_inf_raises(self):
        with self.assertRaises(ValueError):
            avs.make_rate_key(float("inf"))
        with self.assertRaises(ValueError):
            avs.make_rate_key(float("-inf"))

    def test_negative_rate_raises(self):
        with self.assertRaises(ValueError):
            avs.make_rate_key(-1300.0)

    def test_int_rate_allowed(self):
        # int rate(예: 1300)는 numeric이라 허용
        self.assertEqual(avs.make_rate_key(1300), "1300")

    def test_bool_and_non_numeric_rejected(self):
        # bool/str/None은 일관된 ValueError fail-closed (InvalidOperation/TypeError 새지 않음)
        for bad in (True, False, "1300", None):
            with self.assertRaises(ValueError):
                avs.make_rate_key(bad)


class TestSerializeV2Value(unittest.TestCase):

    def _value(self, **overrides):
        kwargs = dict(
            rate=1300.5,
            timestamp="2026-06-17T10:00:00+09:00",
            mirrored_at=datetime(2026, 6, 17, 1, 0, 0, tzinfo=timezone.utc),
            revision=(to_canonical_epoch_us(datetime(2026, 6, 17, 1, 0, 0, tzinfo=timezone.utc)), 42),
        )
        kwargs.update(overrides)
        return json.loads(avs.serialize_v2_value(**kwargs))

    def test_public_fields_unchanged(self):
        v = self._value()
        self.assertEqual(v["rate"], 1300.5)
        self.assertEqual(v["timestamp"], "2026-06-17T10:00:00+09:00")
        self.assertEqual(v["mirrored_at"], "2026-06-17T01:00:00+00:00")

    def test_internal_fields_present(self):
        v = self._value()
        self.assertEqual(v["schema_version"], 2)
        self.assertEqual(
            v["revision_key"],
            avs.make_revision_key_from_revision(
                (to_canonical_epoch_us(datetime(2026, 6, 17, 1, 0, 0, tzinfo=timezone.utc)), 42)
            ),
        )
        self.assertEqual(v["rate_key"], "1300.5")

    def test_raw_id_not_leaked(self):
        # payload에 raw integer id(42) 미노출 — revision_key(fixed-width string)만
        raw = avs.serialize_v2_value(
            rate=1300.5,
            timestamp="2026-06-17T10:00:00+09:00",
            mirrored_at=datetime(2026, 6, 17, 1, 0, 0, tzinfo=timezone.utc),
            revision=(1_700_000_000_000_000, 42),
        )
        v = json.loads(raw)
        self.assertNotIn("id", v)
        self.assertNotIn("revision", v)

    def test_source_asset_optional(self):
        v_without = self._value()
        self.assertNotIn("source", v_without)
        self.assertNotIn("asset", v_without)
        v_with = self._value(source="kb", asset="usd-krw")
        self.assertEqual(v_with["source"], "kb")
        self.assertEqual(v_with["asset"], "usd-krw")


class TestModuleStdlibOnly(unittest.TestCase):

    def test_no_redis_or_crud_import(self):
        src = pathlib.Path(avs.__file__).read_text(encoding="utf-8")
        for forbidden in ("import redis", "from app import crud", "from app import models",
                          "_get_sync_client", "from app.cache"):
            self.assertNotIn(forbidden, src, f"atomic_value_schema에 '{forbidden}' — stdlib-only 위반")


class TestDormancy(unittest.TestCase):
    """A3-1 dormant — live writer 경로(crud/latest_rates_cache/scheduler/main)가 v2 serializer를
    import/호출하지 않음 (behavior-change-0; 호출 시 Redis value 포맷 drift). AST 노드 검사."""

    _LIVE_MODULES = ("crud.py", "latest_rates_cache.py", "scheduler.py", "main.py")

    def test_live_modules_do_not_use_v2_schema(self):
        import ast

        app_dir = pathlib.Path(avs.__file__).resolve().parent
        for mod in self._LIVE_MODULES:
            tree = ast.parse((app_dir / mod).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_value_schema" in node.module:
                        self.fail(f"{mod}: from atomic_value_schema import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_value_schema" for a in node.names):
                        self.fail(f"{mod}: from app import atomic_value_schema — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_value_schema" in a.name:
                            self.fail(f"{mod}: import atomic_value_schema — dormant 위반")
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name in ("serialize_v2_value", "make_revision_key", "make_rate_key"):
                        self.fail(f"{mod}: {name}() 호출 — dormant 위반")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # dynamic import/getattr(importlib.import_module("app.atomic_value_schema") /
                    # getattr(m, "serialize_v2_value")) false-negative 차단 — 문자열 리터럴도 금지
                    for needle in ("atomic_value_schema", "serialize_v2_value"):
                        if needle in node.value:
                            self.fail(f"{mod}: 문자열 '{node.value}'에 '{needle}' — dynamic 호출 의심(dormant 위반)")


if __name__ == "__main__":
    unittest.main()
