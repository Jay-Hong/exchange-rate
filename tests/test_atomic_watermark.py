"""P1b B1 — Redis success watermark 단위 테스트 (§19 B1, pure dormant).

serialize/deserialize round-trip + fail-closed(version/타입/중복/교집합 오염) + content-identity 동등성
(§12 line 219) + 순수 relation classifier 결정표 전수 + membership/schema compatibility predicate +
watermark_key + dormancy(app/ live 모듈 import 0). I/O·arbitration·seq 발급 없음(B2b 소유) 검증.
"""
from __future__ import annotations

import json
import pathlib
import unittest

from app import atomic_watermark as aw
from app.atomic_value_schema import make_revision_key
from app.atomic_watermark import Watermark, WatermarkRelation, WatermarkSchemaError

_RK_A = make_revision_key(1_700_000_000_000_000, 42)   # kb
_RK_B = make_revision_key(1_700_000_000_500_000, 99)   # hana


def _wm(**over) -> Watermark:
    base = dict(
        asset="usd-krw",
        lineage_id="sess-1:7",
        publish_sequence=5,
        membership_version=3,
        present_revision_vector={"kb": _RK_A, "hana": _RK_B},
        missing_sources=("citi", "sc"),
        sent_at="2026-06-18T09:00:00+09:00",
    )
    base.update(over)
    return Watermark(**base)


class TestSerializationRoundTrip(unittest.TestCase):

    def test_round_trip_eq(self):
        wm = _wm()
        self.assertEqual(aw.deserialize_watermark(aw.serialize_watermark(wm)), wm)

    def test_round_trip_from_bytes(self):
        wm = _wm()
        self.assertEqual(aw.deserialize_watermark(aw.serialize_watermark(wm).encode("utf-8")), wm)

    def test_missing_sources_normalized_to_sorted_tuple(self):
        # JSON array(비정렬) → 정렬 tuple 정규화 (frozen __eq__ 일관)
        raw = aw.serialize_watermark(_wm(missing_sources=("sc", "citi")))
        obj = json.loads(raw)
        obj["missing_sources"] = ["sc", "citi"]  # JSON array(역순)
        wm = aw.deserialize_watermark(json.dumps(obj))
        self.assertEqual(wm.missing_sources, ("citi", "sc"))

    def test_empty_vector_and_missing_ok(self):
        wm = _wm(present_revision_vector={}, missing_sources=())
        self.assertEqual(aw.deserialize_watermark(aw.serialize_watermark(wm)), wm)


class TestConstructionInvariants(unittest.TestCase):
    """직접 Watermark(...) 생성도 __post_init__이 validate + canonicalize (codex P1 — deserialize만 정렬하면
    B2b 직접 생성이 비정렬 missing_sources로 content_equal 오판)."""

    def test_direct_unsorted_missing_canonicalized(self):
        self.assertEqual(_wm(missing_sources=("sc", "citi")).missing_sources, ("citi", "sc"))

    def test_content_equal_across_missing_order(self):
        # 직접 생성 두 watermark의 missing_sources 순서가 달라도 canonicalize되어 content 동일
        self.assertTrue(
            aw.watermark_content_equal(_wm(missing_sources=("sc", "citi")), _wm(missing_sources=("citi", "sc")))
        )

    def test_direct_bad_vector_value_raises(self):
        with self.assertRaises(ValueError):
            _wm(present_revision_vector={"kb": "not-a-key"})

    def test_direct_duplicate_missing_raises(self):
        with self.assertRaises(ValueError):
            _wm(missing_sources=("citi", "citi"))

    def test_direct_present_missing_overlap_raises(self):
        with self.assertRaises(ValueError):
            _wm(present_revision_vector={"kb": _RK_A}, missing_sources=("kb",))

    def test_direct_bool_sequence_raises(self):
        with self.assertRaises(ValueError):
            _wm(publish_sequence=True)

    def test_direct_float_sequence_raises(self):
        with self.assertRaises(ValueError):
            _wm(publish_sequence=5.0)

    def test_vector_is_read_only(self):
        # 생성 후 변경 차단 (MappingProxyType, codex caveat) — round-trip eq는 무영향
        wm = _wm()
        with self.assertRaises(TypeError):
            wm.present_revision_vector["kb"] = _RK_B

    def test_vector_defensive_copy_decouples_caller(self):
        # caller dict 변경이 watermark에 bleed 안 됨
        d = {"kb": _RK_A}
        wm = _wm(present_revision_vector=d)
        d["hana"] = _RK_B
        self.assertNotIn("hana", wm.present_revision_vector)


class TestDeserializeFailClosed(unittest.TestCase):

    def _mut(self, **patch) -> str:
        obj = json.loads(aw.serialize_watermark(_wm()))
        obj.update(patch)
        return json.dumps(obj)

    def test_not_str_or_bytes(self):
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(123)

    def test_not_json_object(self):
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark("[1,2,3]")

    def test_bad_json(self):
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark("{not json")

    def test_schema_version_mismatch(self):
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(self._mut(watermark_schema_version=2))

    def test_schema_version_bool_rejected(self):
        # True==1 이지만 bool은 명시 차단 (corrupt `true`가 v1로 통과 방지)
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(self._mut(watermark_schema_version=True))

    def test_schema_version_float_rejected(self):
        # 1.0 == 1 이지만 type-strict (codex P2 — float discriminator 차단)
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(self._mut(watermark_schema_version=1.0))

    def test_missing_field(self):
        obj = json.loads(aw.serialize_watermark(_wm()))
        del obj["asset"]
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(json.dumps(obj))

    def test_publish_sequence_bool_rejected(self):
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(self._mut(publish_sequence=True))

    def test_negative_sequence_rejected(self):
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(self._mut(publish_sequence=-1))

    def test_vector_value_not_revision_key(self):
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(self._mut(present_revision_vector={"kb": "not-a-key"}))

    def test_vector_value_none_rejected(self):
        # effective-only invariant — None 성분 금지
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(self._mut(present_revision_vector={"kb": None}))

    def test_missing_sources_duplicate_rejected(self):
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(self._mut(missing_sources=["citi", "citi"]))

    def test_missing_present_overlap_rejected(self):
        # source는 present/missing 동시 불가
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(
                self._mut(present_revision_vector={"kb": _RK_A}, missing_sources=["kb"])
            )

    def test_missing_sources_non_str_rejected(self):
        with self.assertRaises(WatermarkSchemaError):
            aw.deserialize_watermark(self._mut(missing_sources=[1]))


class TestContentEqual(unittest.TestCase):

    def test_equal_ignores_lineage_seq_sentat(self):
        a = _wm(lineage_id="sess-1:7", publish_sequence=5, sent_at="2026-06-18T09:00:00+09:00")
        b = _wm(lineage_id="sess-9:2", publish_sequence=99, sent_at="2026-06-18T10:00:00+09:00")
        self.assertTrue(aw.watermark_content_equal(a, b))

    def test_diff_vector_not_equal(self):
        self.assertFalse(aw.watermark_content_equal(_wm(), _wm(present_revision_vector={"kb": _RK_A})))

    def test_diff_missing_not_equal(self):
        self.assertFalse(aw.watermark_content_equal(_wm(), _wm(missing_sources=("citi",))))

    def test_diff_membership_not_equal(self):
        self.assertFalse(aw.watermark_content_equal(_wm(), _wm(membership_version=4)))

    def test_asset_mismatch_raises(self):
        with self.assertRaises(ValueError):
            aw.watermark_content_equal(_wm(asset="usd-krw"), _wm(asset="jpy-krw"))


class TestClassify(unittest.TestCase):

    def test_no_current(self):
        self.assertEqual(aw.classify_watermark_relation(None, _wm()), WatermarkRelation.NO_CURRENT)

    def test_lineage_mismatch(self):
        r = aw.classify_watermark_relation(_wm(lineage_id="sess-1:7"), _wm(lineage_id="sess-2:1"))
        self.assertEqual(r, WatermarkRelation.LINEAGE_MISMATCH)

    def test_newer(self):
        r = aw.classify_watermark_relation(_wm(publish_sequence=5), _wm(publish_sequence=6))
        self.assertEqual(r, WatermarkRelation.NEWER)

    def test_older(self):
        r = aw.classify_watermark_relation(_wm(publish_sequence=5), _wm(publish_sequence=4))
        self.assertEqual(r, WatermarkRelation.OLDER)

    def test_same(self):
        # same lineage + same seq + same content → SAME (idempotent). sent_at 달라도 content 동일.
        a = _wm(publish_sequence=5, sent_at="2026-06-18T09:00:00+09:00")
        b = _wm(publish_sequence=5, sent_at="2026-06-18T09:00:05+09:00")
        self.assertEqual(aw.classify_watermark_relation(a, b), WatermarkRelation.SAME)

    def test_same_seq_content_divergent(self):
        # same lineage + same seq + content 다름 → coordinator invariant 위반 신호
        a = _wm(publish_sequence=5, membership_version=3)
        b = _wm(publish_sequence=5, membership_version=4)
        self.assertEqual(
            aw.classify_watermark_relation(a, b), WatermarkRelation.SAME_SEQ_CONTENT_DIVERGENT
        )

    def test_asset_mismatch_raises(self):
        with self.assertRaises(ValueError):
            aw.classify_watermark_relation(_wm(asset="usd-krw"), _wm(asset="eur-krw"))


class TestCompatible(unittest.TestCase):

    def test_compatible(self):
        self.assertTrue(aw.is_watermark_compatible(_wm(membership_version=3), 3, aw.WATERMARK_SCHEMA_VERSION))

    def test_membership_mismatch_incompatible(self):
        self.assertFalse(aw.is_watermark_compatible(_wm(membership_version=3), 4, aw.WATERMARK_SCHEMA_VERSION))

    def test_schema_mismatch_incompatible(self):
        self.assertFalse(aw.is_watermark_compatible(_wm(membership_version=3), 3, 2))

    def test_bool_float_incoming_incompatible(self):
        # type-strict (codex P2) — True/1.0 incoming이 equality로 호환 오판되지 않고 비호환(bootstrap)
        self.assertFalse(aw.is_watermark_compatible(_wm(membership_version=1), 1, True))
        self.assertFalse(aw.is_watermark_compatible(_wm(membership_version=1), True, aw.WATERMARK_SCHEMA_VERSION))
        self.assertFalse(aw.is_watermark_compatible(_wm(membership_version=1), 1.0, aw.WATERMARK_SCHEMA_VERSION))


class TestWatermarkKey(unittest.TestCase):

    def test_format(self):
        self.assertEqual(aw.watermark_key("usd-krw"), "watermark:fx:usd-krw")


class TestDormancy(unittest.TestCase):
    """B1 dormant — app/ 어떤 live 모듈도 atomic_watermark import/호출 0 (live atomic 모듈 포함 —
    dormant island만 skip). atomic_revision/atomic_value_schema는 atomic_watermark가 정당 import."""

    _DORMANT_ISLAND = frozenset({
        "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py",
        "atomic_write_outcome.py", "atomic_cutover.py", "atomic_watermark.py",
        "atomic_build.py", "atomic_reconcile.py", "atomic_coordinator.py",  # B2a/B2b-1/B2b-4a dormant (docstring이 dormant siblings 언급 — live-scan skip)
    })
    _CALL_NEEDLES = (
        "serialize_watermark", "deserialize_watermark", "classify_watermark_relation",
        "watermark_content_equal", "is_watermark_compatible", "watermark_key",
    )

    def test_no_app_module_uses_atomic_watermark(self):
        import ast

        app_dir = pathlib.Path(aw.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in self._DORMANT_ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.module and "atomic_watermark" in node.module.split("."):
                        self.fail(f"{rel}: from atomic_watermark import — dormant 위반")
                    if node.module == "app" and any(a.name == "atomic_watermark" for a in node.names):
                        self.fail(f"{rel}: from app import atomic_watermark — dormant 위반")
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_watermark" in a.name.split("."):
                            self.fail(f"{rel}: import atomic_watermark — dormant 위반")
                elif isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name in self._CALL_NEEDLES:
                        self.fail(f"{rel}: {name}() 호출 — dormant 위반")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "atomic_watermark" in node.value or "serialize_watermark" in node.value:
                        self.fail(f"{rel}: 문자열 '{node.value}'에 watermark — dynamic 호출 의심")


if __name__ == "__main__":
    unittest.main()
