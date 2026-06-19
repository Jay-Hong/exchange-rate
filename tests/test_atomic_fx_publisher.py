"""P1b C6-4 — atomic_fx_publisher 단위 테스트 (count→SendResult pure mapper, island, dormant).

send_counts_to_send_result 4-way 매핑(SENT/ALL_FAILED/NO_SUBSCRIBERS, EXCEPTION 절대 X) + SENT⟺count
invariant + dormancy(positive _scan_direct_live_io self-arm[순수 모듈] + negative no-live-importer +
no-scheduling). C6-3 loader는 live read import 있어 negative-only였으나 C6-4 mapper는 순수라 positive 보유.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

from app import atomic_fx_publisher as m
from app.atomic_coordinator import SendDisposition, SendResult
from app.atomic_fx_publisher import send_counts_to_send_result


class TestMapping(unittest.TestCase):
    """attempted/sent/enabled → SendResult 4-way."""

    def test_sent_positive_is_SENT(self):
        r = send_counts_to_send_result(attempted=3, sent=2, enabled=True)
        self.assertEqual(r, SendResult(disposition=SendDisposition.SENT, sent_count=2))

    def test_sent_single_is_SENT(self):
        r = send_counts_to_send_result(attempted=1, sent=1, enabled=True)
        self.assertEqual(r, SendResult(disposition=SendDisposition.SENT, sent_count=1))

    def test_attempted_positive_sent_zero_is_ALL_FAILED(self):
        # bare int=0이 no-subscribers와 conflate하는 case — attempted>0로 구분
        r = send_counts_to_send_result(attempted=2, sent=0, enabled=True)
        self.assertEqual(r, SendResult(disposition=SendDisposition.ALL_FAILED, sent_count=0))

    def test_no_subscribers_is_NO_SUBSCRIBERS(self):
        r = send_counts_to_send_result(attempted=0, sent=0, enabled=True)
        self.assertEqual(r, SendResult(disposition=SendDisposition.NO_SUBSCRIBERS, sent_count=0))

    def test_ff_disabled_collapses_to_NO_SUBSCRIBERS(self):
        # enabled=False → NO_SUBSCRIBERS collapse (SendDisposition에 DISABLED 없음, FF upstream gate)
        r = send_counts_to_send_result(attempted=0, sent=0, enabled=False)
        self.assertEqual(r, SendResult(disposition=SendDisposition.NO_SUBSCRIBERS, sent_count=0))

    def test_ff_disabled_with_attempted_still_NO_SUBSCRIBERS(self):
        # 비정상 input(FF-off인데 attempted>0 — 실제 primitive에선 불가) 방어: enabled=False면 collapse
        r = send_counts_to_send_result(attempted=5, sent=0, enabled=False)
        self.assertEqual(r, SendResult(disposition=SendDisposition.NO_SUBSCRIBERS, sent_count=0))

    def test_sent_wins_over_ff_off_collapse(self):
        # precedence: sent>0가 enabled=False collapse보다 우선 (sent는 ground truth). guard 재정렬 방지.
        r = send_counts_to_send_result(attempted=2, sent=2, enabled=False)
        self.assertEqual(r, SendResult(disposition=SendDisposition.SENT, sent_count=2))

    def test_never_returns_EXCEPTION(self):
        # valid domain(sent<=attempted) 전수: EXCEPTION 생성 안 함 + SENT⟺count invariant
        for attempted in range(0, 4):
            for sent in range(0, attempted + 1):
                for enabled in (True, False):
                    r = send_counts_to_send_result(attempted=attempted, sent=sent, enabled=enabled)
                    self.assertIsNot(r.disposition, SendDisposition.EXCEPTION)
                    self.assertEqual(r.disposition is SendDisposition.SENT, r.sent_count > 0)

    def test_out_of_domain_raises(self):
        # Crash-Early (SPLIT이 떨군 TopicSendCounts 검증 복원): sent>attempted / negative / bool → ValueError
        with self.assertRaises(ValueError):
            send_counts_to_send_result(attempted=1, sent=5, enabled=True)   # sent>attempted
        with self.assertRaises(ValueError):
            send_counts_to_send_result(attempted=2, sent=-1, enabled=True)  # negative sent
        with self.assertRaises(ValueError):
            send_counts_to_send_result(attempted=-1, sent=0, enabled=True)  # negative attempted
        with self.assertRaises(ValueError):
            send_counts_to_send_result(attempted=True, sent=0, enabled=True)  # bool-as-int
        with self.assertRaises(ValueError):
            send_counts_to_send_result(attempted=0, sent=0, enabled=1)      # enabled non-bool


# ---------------------------------------------------------------------------
# Dormancy — island 멤버
# ---------------------------------------------------------------------------

_ISLAND = frozenset({
    "atomic_value_schema.py", "atomic_lua.py", "atomic_migration.py", "atomic_write_outcome.py",
    "atomic_cutover.py", "atomic_watermark.py", "atomic_build.py", "atomic_reconcile.py",
    "atomic_coordinator.py", "atomic_retry.py", "atomic_cutover_durable.py", "atomic_cutover_runtime.py",
    "atomic_fx_v2_loader.py", "atomic_fx_publisher.py", "atomic_watermark_store.py",
})

_LIVE_IO_FORBIDDEN = frozenset({
    "cache", "latest_rates_cache", "topic_dispatcher", "fx_topic_publisher",
    "fx_topic_payload", "crud", "database", "redis",
})
_LIVE_CALL_NEEDLES = frozenset({
    "publish_topic", "publish_topic_detailed", "safe_publish_fx_snapshot", "_publish_fx_snapshot",
    "send_json",
})


def _scan_direct_live_io(src):
    """src에서 직접 live-I/O import / publish·redis call 위반 목록 (pure, self-arming 가능)."""
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
    """순수 island 모듈 — 직접 live I/O import/call 0 (skip-list가 island 멤버를 skip하는 blind spot 보완)."""

    def test_no_direct_live_io(self):
        src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        self.assertEqual(_scan_direct_live_io(src), [],
                         "atomic_fx_publisher.py에 직접 live I/O import/call — dormant 위반")

    def test_scanner_self_arms(self):
        # HIGH8: detector가 실제 위반에 trip (green-only 가드 방지)
        for planted in (
            "from app.topic_dispatcher import TopicSendCounts\n",
            "from app import cache\n",
            "import app.latest_rates_cache\n",
            "async def f(c):\n    await c.send_json({})\n",
            "def g():\n    publish_topic_detailed('t', {})\n",
        ):
            self.assertTrue(_scan_direct_live_io(planted),
                            f"planted 위반 미검출: {planted!r}")


class TestDormancy(unittest.TestCase):
    """app/ 어떤 live 모듈도 atomic_fx_publisher import 0 + scheduling 0."""

    def test_no_live_module_imports_publisher(self):
        app_dir = pathlib.Path(m.__file__).resolve().parent
        for py in sorted(app_dir.rglob("*.py")):
            if py.name in _ISLAND:
                continue
            rel = py.relative_to(app_dir)
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and "atomic_fx_publisher" in node.module:
                    self.fail(f"{rel}: from atomic_fx_publisher import — dormant 위반")
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if "atomic_fx_publisher" in a.name:
                            self.fail(f"{rel}: import atomic_fx_publisher — dormant 위반")

    def test_no_scheduling(self):
        src = pathlib.Path(m.__file__).read_text(encoding="utf-8")
        for needle in ("add_job", "create_task", "Thread(", ".start()", "AsyncIOScheduler"):
            self.assertNotIn(needle, src, f"mapper에 scheduling needle '{needle}' — dormant 위반")


if __name__ == "__main__":
    unittest.main()
