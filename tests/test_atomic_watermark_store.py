"""P1b C6-5 — AtomicWatermarkStore 단위 테스트 (watermark Redis reader/writer, island, dormant).

reader(miss→None / valid bytes round-trip / corrupt→None / schema-mismatch→None / outage→propagate) +
writer(success→True / asset-mismatch→ValueError / outage→warning+False) + dormancy(positive
_scan_direct_live_io self-arm[pure-by-injection] + negative no-live-importer + no-scheduling).
"""
from __future__ import annotations

import ast
import json
import pathlib
import unittest
from unittest.mock import patch

from app import atomic_watermark_store as m
from app.atomic_watermark import Watermark, serialize_watermark, watermark_content_equal, watermark_key
from app.atomic_watermark_store import AtomicWatermarkStore
from app.atomic_value_schema import make_revision_key

_REV = make_revision_key(1_700_000_000_000_000, 5)


def _wm(asset="usd-krw", *, seq=3):
    return Watermark(
        asset=asset, lineage_id="sess:1", publish_sequence=seq, membership_version=1,
        present_revision_vector={"kb": _REV}, missing_sources=("hana",),
        sent_at="2026-06-19T09:00:00+09:00",
    )


class _FakeClient:
    """get/set 만 — bytes 저장(decode_responses=False parity). raise_on으로 outage 주입."""

    def __init__(self, kv=None, *, raise_on=()):
        self.kv = dict(kv or {})
        self.raise_on = set(raise_on)

    def get(self, key):
        if "get" in self.raise_on:
            raise ConnectionError("redis down")
        return self.kv.get(key)

    def set(self, key, value):
        if "set" in self.raise_on:
            raise ConnectionError("redis down")
        # decode_responses=False parity: 실 redis-py는 str을 utf-8 bytes로 encode해 저장 → GET은 bytes 반환
        self.kv[key] = value.encode("utf-8") if isinstance(value, str) else value
        return True


class TestRead(unittest.TestCase):

    def test_key_miss_returns_none(self):
        store = AtomicWatermarkStore(_FakeClient())
        self.assertIsNone(store.read("usd-krw"))

    def test_valid_bytes_round_trip(self):
        wm = _wm()
        raw = serialize_watermark(wm).encode("utf-8")   # bytes (decode_responses=False)
        store = AtomicWatermarkStore(_FakeClient({watermark_key("usd-krw"): raw}))
        got = store.read("usd-krw")
        self.assertIsNotNone(got)
        self.assertTrue(watermark_content_equal(got, wm))
        self.assertEqual(got, wm)   # __eq__ 전 필드 content

    def test_corrupt_json_returns_none(self):
        store = AtomicWatermarkStore(_FakeClient({watermark_key("usd-krw"): b"{bad json"}))
        self.assertIsNone(store.read("usd-krw"))   # WatermarkSchemaError → fail-closed bootstrap

    def test_schema_mismatch_returns_none(self):
        bad = json.dumps({"watermark_schema_version": 2, "asset": "usd-krw"})
        store = AtomicWatermarkStore(_FakeClient({watermark_key("usd-krw"): bad}))
        self.assertIsNone(store.read("usd-krw"))

    def test_outage_propagates(self):
        # outage ≠ bootstrap → None mask 금지, propagate (read는 send 이전이라 clean retry)
        store = AtomicWatermarkStore(_FakeClient(raise_on=("get",)))
        with self.assertRaises(ConnectionError):
            store.read("usd-krw")

    def test_non_schema_error_propagates(self):
        # read는 WatermarkSchemaError만 catch — deserialize의 예기치 못한 bug는 propagate(fail-loud).
        # narrow-catch 의도 잠금(향후 bare Exception 확장 방지).
        store = AtomicWatermarkStore(_FakeClient({watermark_key("usd-krw"): b"x"}))
        with patch.object(m, "deserialize_watermark", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                store.read("usd-krw")


class TestWrite(unittest.TestCase):

    def test_success_returns_true_and_sets(self):
        fc = _FakeClient()
        store = AtomicWatermarkStore(fc)
        wm = _wm()
        self.assertTrue(store.write("usd-krw", wm))
        # fake가 str→bytes encode (decode_responses=False parity)
        self.assertEqual(fc.kv[watermark_key("usd-krw")], serialize_watermark(wm).encode("utf-8"))

    def test_write_then_read_round_trip(self):
        # end-to-end: write(str) → fake stores bytes → read(bytes) → Watermark content-equal (실 production cycle)
        fc = _FakeClient()
        store = AtomicWatermarkStore(fc)
        wm = _wm()
        self.assertTrue(store.write("usd-krw", wm))
        got = store.read("usd-krw")
        self.assertEqual(got, wm)
        self.assertTrue(watermark_content_equal(got, wm))

    def test_asset_mismatch_raises_no_set(self):
        fc = _FakeClient()
        store = AtomicWatermarkStore(fc)
        with self.assertRaises(ValueError):
            store.write("usd-krw", _wm(asset="jpy-krw"))   # key asset vs wm.asset mismatch = caller bug
        self.assertEqual(fc.kv, {})   # SET 미수행

    def test_outage_returns_false_with_warning(self):
        # write는 send 이후 + coordinator try/except 없음 → raise 금지(orphan), False로 sent_but_uncommitted 구동
        store = AtomicWatermarkStore(_FakeClient(raise_on=("set",)))
        with self.assertLogs("exchange_rate.atomic_watermark_store", level="WARNING"):
            ok = store.write("usd-krw", _wm())
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# Dormancy — island 멤버 (pure-by-injection → positive trip-wire 보유)
# ---------------------------------------------------------------------------

_ISLAND = frozenset({
    "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py", "atomic_write_outcome.py",
    "atomic_cutover.py", "atomic_watermark.py", "atomic_build.py", "atomic_reconcile.py",
    "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py",
    "atomic_fx_v2_loader.py", "atomic_fx_publisher.py", "atomic_watermark_store.py", "atomic_fx_live.py",
})

_LIVE_IO_FORBIDDEN = frozenset({
    "cache", "latest_rates_cache", "topic_dispatcher", "fx_topic_publisher",
    "fx_topic_payload", "crud", "database", "redis",
})
# client.get/set은 needle 아님 — store는 주입 client로 정당하게 Redis I/O (codex nuance).
_LIVE_CALL_NEEDLES = frozenset({
    "publish_topic", "publish_topic_detailed", "safe_publish_fx_snapshot", "_publish_fx_snapshot",
    "send_json",
})


def _scan_direct_live_io(src):
    violations = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            tail = (node.module or "").split(".")[-1]
            if tail in _LIVE_IO_FORBIDDEN:
                violations.append(f"from {node.module} import — live I/O")
            if node.module == "app":
                for a in node.names:
                    if a.name in _LIVE_IO_FORBIDDEN:
                        violations.append(f"from app import {a.name} — live I/O")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[-1] in _LIVE_IO_FORBIDDEN:
                    violations.append(f"import {a.name} — live I/O")
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _LIVE_CALL_NEEDLES:
                violations.append(f"{name}() — live publish/redis call")
    return violations


class TestPositiveDormancy(unittest.TestCase):
    """client 주입 pure 모듈 — 직접 live I/O import/publish call 0 (client.get/set은 정당)."""

    def test_no_direct_live_io(self):
        src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        self.assertEqual(_scan_direct_live_io(src), [],
                         "atomic_watermark_store.py에 직접 live I/O import/publish call — dormant 위반")

    def test_scanner_self_arms(self):
        for planted in (
            "from app.latest_rates_cache import _get_sync_client\n",
            "from app import cache\n",
            "import app.topic_dispatcher\n",
            "async def f(c):\n    await c.send_json({})\n",
            "def g():\n    publish_topic('t', {})\n",
        ):
            self.assertTrue(_scan_direct_live_io(planted), f"planted 위반 미검출: {planted!r}")


class TestDormancy(unittest.TestCase):

    def test_no_live_module_imports_store(self):
        app_dir = pathlib.Path(m.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in _ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "atomic_watermark_store" in node.module:
                    self.fail(f"{rel}: from atomic_watermark_store import — dormant 위반")
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_watermark_store" in a.name:
                            self.fail(f"{rel}: import atomic_watermark_store — dormant 위반")

    def test_no_scheduling(self):
        src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        for needle in ("add_job", "create_task", "Thread(", ".start()", "AsyncIOScheduler"):
            self.assertNotIn(needle, src, f"store에 scheduling needle '{needle}' — dormant 위반")


if __name__ == "__main__":
    unittest.main()
