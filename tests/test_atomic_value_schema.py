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


class TestParseRevisionKey(unittest.TestCase):
    """make_revision_key 역함수 — round-trip + fail-closed (B1/B2b watermark vector 인코딩 single-source)."""

    def test_round_trip(self):
        for epoch, rid in [(0, 0), (1_700_000_000_000_000, 42), (10 ** 20 - 1, 10 ** 20 - 1)]:
            key = avs.make_revision_key(epoch, rid)
            self.assertEqual(avs.parse_revision_key(key), (epoch, rid))
            self.assertEqual(avs.make_revision_key(*avs.parse_revision_key(key)), key)

    def test_non_str(self):
        with self.assertRaises(ValueError):
            avs.parse_revision_key(123)

    def test_wrong_segment_count(self):
        with self.assertRaises(ValueError):
            avs.parse_revision_key("123")
        with self.assertRaises(ValueError):
            avs.parse_revision_key("1:2:3")

    def test_not_fixed_width(self):
        # 20자리 고정폭 아님 (lex order 보장 깨짐)
        with self.assertRaises(ValueError):
            avs.parse_revision_key("1:2")

    def test_non_ascii_digit(self):
        # str.isdigit True지만 ascii 아님 / 부호 / 공백 → 거부
        with self.assertRaises(ValueError):
            avs.parse_revision_key("-0000000000000000001:" + "0" * 20)


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

    def test_naive_mirrored_at_raises(self):
        # A3-3 review L2: naive mirrored_at → old reader deserialize 실패 → fail-closed
        from datetime import datetime as _dt
        with self.assertRaises(ValueError):
            avs.serialize_v2_value(
                rate=1300.5, timestamp="2026-06-17T10:00:00+09:00",
                mirrored_at=_dt(2026, 6, 17, 1, 0, 0),  # naive
                revision=(to_canonical_epoch_us(datetime(2026, 6, 17, 1, 0, 0, tzinfo=timezone.utc)), 42),
            )

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


# dormant island — 서로 정당 교차 import하는 primitive들만 skip (atomic_lua/migration이 v2 schema 사용 등).
# live atomic 모듈(atomic_write_control/runtime/refresh/revision = crud/scheduler/main 등이 import)은 scan
# 대상으로 남겨 미래 회귀(live atomic이 dormant primitive import)도 잡음 (codex holistic cross-check —
# startswith("atomic_") 광역 skip은 live atomic까지 가려 약함).
_DORMANT_ISLAND = frozenset({
    "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py",
    "atomic_write_outcome.py", "atomic_cutover.py", "atomic_watermark.py",
    "atomic_build.py", "atomic_reconcile.py",  # B2a/B2b-1 dormant (non-island이나 docstring이 dormant siblings 언급 — live-scan skip)
})


class TestDormancy(unittest.TestCase):
    """A3-1 dormant — **app/ 전체** 어떤 live 모듈도 v2 serializer를 import/호출 0 (crawler live-writer +
    live atomic 모듈 포함 — dormant island만 skip, A 시리즈 holistic 검토 + codex cross-check)."""

    def test_no_app_module_uses_v2_schema(self):
        import ast

        app_dir = pathlib.Path(avs.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in _DORMANT_ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_value_schema" in node.module.split("."):
                        self.fail(f"{rel}: from atomic_value_schema import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_value_schema" for a in node.names):
                        self.fail(f"{rel}: from app import atomic_value_schema — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_value_schema" in a.name.split("."):
                            self.fail(f"{rel}: import atomic_value_schema — dormant 위반")
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name in ("serialize_v2_value", "make_revision_key", "make_rate_key",
                                "parse_revision_key"):
                        self.fail(f"{rel}: {name}() 호출 — dormant 위반")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    # dynamic import/getattr false-negative 차단 — 문자열 리터럴도 금지
                    for needle in ("atomic_value_schema", "serialize_v2_value"):
                        if needle in node.value:
                            self.fail(f"{rel}: 문자열 '{node.value}'에 '{needle}' — dynamic 호출 의심(dormant 위반)")


if __name__ == "__main__":
    unittest.main()
