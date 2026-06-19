"""topic_dispatcher 단위 테스트 (PR Z-2b Stage 1).

TopicRegistry register/unregister/remove + publish_topic FF 분기 + 실패 격리.
"""
from __future__ import annotations

import ast
import pathlib
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import config, topic_dispatcher
from app.topic_dispatcher import TopicSendCounts, publish_topic_detailed


def _fresh_registry() -> topic_dispatcher.TopicRegistry:
    """test isolation — 모듈 싱글톤 대신 fresh 인스턴스."""
    return topic_dispatcher.TopicRegistry()


# ---------------------------------------------------------------------------
# TopicRegistry — synchronous registry ops
# ---------------------------------------------------------------------------

class TestTopicRegistry(unittest.TestCase):

    def test_register_creates_entry(self):
        reg = _fresh_registry()
        ws = MagicMock()
        result = reg.register(ws, ["usdt:krw"])
        self.assertEqual(result, {"usdt:krw"})
        self.assertEqual(reg.subscribed_connection_count, 1)

    def test_register_idempotent_union(self):
        reg = _fresh_registry()
        ws = MagicMock()
        reg.register(ws, ["usdt:krw"])
        result = reg.register(ws, ["usdt:krw", "krx:usd-krw-futures"])
        # 합집합 — 중복 추가는 그대로
        self.assertEqual(result, {"usdt:krw", "krx:usd-krw-futures"})
        self.assertEqual(reg.subscribed_connection_count, 1)

    def test_unregister_partial(self):
        reg = _fresh_registry()
        ws = MagicMock()
        reg.register(ws, ["usdt:krw", "krx:usd-krw-futures"])
        result = reg.unregister(ws, ["usdt:krw"])
        self.assertEqual(result, {"krx:usd-krw-futures"})
        self.assertEqual(reg.subscribed_connection_count, 1)

    def test_unregister_clears_entry_when_empty(self):
        """모든 topic 해제 시 entry 제거 — 메모리 누수 방지."""
        reg = _fresh_registry()
        ws = MagicMock()
        reg.register(ws, ["usdt:krw"])
        result = reg.unregister(ws, ["usdt:krw"])
        self.assertEqual(result, set())
        self.assertEqual(reg.subscribed_connection_count, 0)

    def test_unregister_unknown_websocket_safe(self):
        reg = _fresh_registry()
        ws = MagicMock()
        # 등록 X 상태에서 unregister — 예외 X, 빈 set
        result = reg.unregister(ws, ["usdt:krw"])
        self.assertEqual(result, set())
        self.assertEqual(reg.subscribed_connection_count, 0)

    def test_remove_websocket_clears_all_subscriptions(self):
        reg = _fresh_registry()
        ws = MagicMock()
        reg.register(ws, ["usdt:krw", "krx:usd-krw-futures"])
        reg.remove_websocket(ws)
        self.assertEqual(reg.subscribed_connection_count, 0)
        self.assertEqual(reg.get_subscriptions(ws), set())

    def test_get_subscribers_returns_only_topic_subscribers(self):
        reg = _fresh_registry()
        ws_a = MagicMock(name="ws_a")
        ws_b = MagicMock(name="ws_b")
        ws_c = MagicMock(name="ws_c")
        reg.register(ws_a, ["usdt:krw"])
        reg.register(ws_b, ["usdt:krw", "krx:usd-krw-futures"])
        reg.register(ws_c, ["krx:usd-krw-futures"])

        usdt_subs = reg.get_subscribers("usdt:krw")
        self.assertEqual(usdt_subs, {ws_a, ws_b})

        krx_subs = reg.get_subscribers("krx:usd-krw-futures")
        self.assertEqual(krx_subs, {ws_b, ws_c})

        # 미존재 topic — 빈 set
        self.assertEqual(reg.get_subscribers("non-existent"), set())

    def test_get_subscriptions_returns_snapshot_copy(self):
        """반환된 set 수정이 registry 내부에 영향 없는지 (snapshot)."""
        reg = _fresh_registry()
        ws = MagicMock()
        reg.register(ws, ["usdt:krw"])
        snap = reg.get_subscriptions(ws)
        snap.add("hacked")
        self.assertEqual(reg.get_subscriptions(ws), {"usdt:krw"})

    def test_subscriber_count_per_topic(self):
        """subscriber_count(topic) — 각 topic별 구독자 수 (multi-topic publisher guard용).

        subscribed_connection_count (전체 ws 수)와 분리. fx:usd-krw만 구독한 ws가
        있어도 usdt:krw의 subscriber_count는 0이어야 함 (Codex 권고).
        """
        reg = _fresh_registry()
        ws_a = MagicMock(name="ws_a")  # usdt:krw 구독
        ws_b = MagicMock(name="ws_b")  # fx:usd-krw 구독
        ws_c = MagicMock(name="ws_c")  # 둘 다 구독
        reg.register(ws_a, ["usdt:krw"])
        reg.register(ws_b, ["fx:usd-krw"])
        reg.register(ws_c, ["usdt:krw", "fx:usd-krw"])

        # 전체 connection 수 = 3
        self.assertEqual(reg.subscribed_connection_count, 3)
        # topic별 — usdt:krw는 ws_a + ws_c = 2명
        self.assertEqual(reg.subscriber_count("usdt:krw"), 2)
        # fx:usd-krw는 ws_b + ws_c = 2명
        self.assertEqual(reg.subscriber_count("fx:usd-krw"), 2)
        # 미존재 topic = 0
        self.assertEqual(reg.subscriber_count("nonexistent:topic"), 0)

    def test_subscriber_count_isolated_per_topic(self):
        """다른 topic 구독자 있어도 해당 topic의 count는 0 (multi-topic guard 핵심)."""
        reg = _fresh_registry()
        ws = MagicMock()
        # fx:usd-krw만 구독
        reg.register(ws, ["fx:usd-krw"])

        # 전체 connection은 1명이지만
        self.assertEqual(reg.subscribed_connection_count, 1)
        # usdt:krw subscriber는 0 (publisher가 builder 호출 차단해야 함)
        self.assertEqual(reg.subscriber_count("usdt:krw"), 0)
        # fx:usd-krw는 1
        self.assertEqual(reg.subscriber_count("fx:usd-krw"), 1)


# ---------------------------------------------------------------------------
# publish_topic — FF 분기 + 실패 격리
# ---------------------------------------------------------------------------

class TestPublishTopic(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # 모듈 싱글톤 registry를 격리된 인스턴스로 교체 — 다른 테스트와 분리
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def test_no_op_when_flag_disabled(self):
        """FF=false → subscribers 있어도 send 호출 X."""
        ws = MagicMock()
        ws.send_json = AsyncMock()
        topic_dispatcher.registry.register(ws, ["usdt:krw"])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            sent = await topic_dispatcher.publish_topic(
                "usdt:krw", {"type": "tick", "rate": 1450.0}
            )
        self.assertEqual(sent, 0)
        ws.send_json.assert_not_called()

    async def test_no_op_when_no_subscribers(self):
        """FF=true이지만 구독자 없으면 0 반환 (빈 loop)."""
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            sent = await topic_dispatcher.publish_topic(
                "usdt:krw", {"type": "tick"}
            )
        self.assertEqual(sent, 0)

    async def test_sends_to_topic_subscribers_only(self):
        """FF=true + 구독자 있음 → topic 일치하는 ws에만 send."""
        ws_a = MagicMock(name="ws_a")
        ws_a.send_json = AsyncMock()
        ws_b = MagicMock(name="ws_b")
        ws_b.send_json = AsyncMock()
        ws_c = MagicMock(name="ws_c")
        ws_c.send_json = AsyncMock()

        topic_dispatcher.registry.register(ws_a, ["usdt:krw"])
        topic_dispatcher.registry.register(ws_b, ["usdt:krw"])
        topic_dispatcher.registry.register(ws_c, ["krx:usd-krw-futures"])

        payload = {"type": "tick", "rate": 1450.0}
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            sent = await topic_dispatcher.publish_topic("usdt:krw", payload)

        self.assertEqual(sent, 2)
        ws_a.send_json.assert_awaited_once_with(payload)
        ws_b.send_json.assert_awaited_once_with(payload)
        ws_c.send_json.assert_not_called()

    async def test_send_failure_isolated_per_subscriber(self):
        """한 구독자 send 실패 → 격리 + registry에서 stale ws 즉시 제거."""
        ws_ok = MagicMock(name="ws_ok")
        ws_ok.send_json = AsyncMock()
        ws_fail = MagicMock(name="ws_fail")
        ws_fail.send_json = AsyncMock(side_effect=ConnectionError("closed"))

        topic_dispatcher.registry.register(ws_ok, ["usdt:krw"])
        topic_dispatcher.registry.register(ws_fail, ["usdt:krw"])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             self.assertLogs("exchange_rate.topic_dispatcher", level="WARNING") as cm:
            sent = await topic_dispatcher.publish_topic("usdt:krw", {})

        # 실패한 ws는 sent count 미반영
        self.assertEqual(sent, 1)
        ws_ok.send_json.assert_awaited_once()
        ws_fail.send_json.assert_awaited_once()
        self.assertTrue(any("topic publish 실패" in m for m in cm.output))

        # Codex 권고: 실패한 ws는 registry에서 즉시 제거 (stale entry 방지)
        subs_after = topic_dispatcher.registry.get_subscribers("usdt:krw")
        self.assertNotIn(ws_fail, subs_after)
        self.assertIn(ws_ok, subs_after)

    async def test_send_failure_remove_is_idempotent_with_disconnect_hook(self):
        """publish 실패 정리 + Stage 2 disconnect hook 중복 호출 안전성 (idempotent).

        실제 운영 시나리오: publish_topic이 ws_fail을 정리한 직후 main.py
        disconnect hook이 같은 ws를 remove_websocket() 호출 — 예외 없이 통과해야 함.
        """
        ws_fail = MagicMock(name="ws_fail")
        ws_fail.send_json = AsyncMock(side_effect=ConnectionError("closed"))
        topic_dispatcher.registry.register(ws_fail, ["usdt:krw", "krx:usd-krw-futures"])

        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             self.assertLogs("exchange_rate.topic_dispatcher", level="WARNING"):
            await topic_dispatcher.publish_topic("usdt:krw", {})

        # publish 실패 시 모든 구독에서 정리 (ws 자체가 끊겼으므로)
        self.assertEqual(topic_dispatcher.registry.get_subscriptions(ws_fail), set())

        # disconnect hook이 뒤늦게 호출돼도 예외 없음 (idempotent)
        topic_dispatcher.registry.remove_websocket(ws_fail)  # 무동작
        self.assertEqual(topic_dispatcher.registry.subscribed_connection_count, 0)


# ---------------------------------------------------------------------------
# C6-4 — publish_topic_detailed (rich-outcome sibling, dormant) + TopicSendCounts
# ---------------------------------------------------------------------------

class TestTopicSendCounts(unittest.TestCase):
    """TopicSendCounts invariant (codex: attempted/sent non-neg int, sent<=attempted, bool 거부)."""

    def test_valid(self):
        c = TopicSendCounts(attempted=3, sent=2, enabled=True)
        self.assertEqual((c.attempted, c.sent, c.enabled), (3, 2, True))

    def test_sent_exceeds_attempted_raises(self):
        with self.assertRaises(ValueError):
            TopicSendCounts(attempted=1, sent=2, enabled=True)

    def test_negative_raises(self):
        with self.assertRaises(ValueError):
            TopicSendCounts(attempted=-1, sent=0, enabled=True)

    def test_bool_as_int_rejected(self):
        # True==1이지만 bool은 거부 (SendResult.sent_count 패턴 정합)
        with self.assertRaises(ValueError):
            TopicSendCounts(attempted=True, sent=0, enabled=True)

    def test_enabled_must_be_bool(self):
        with self.assertRaises(ValueError):
            TopicSendCounts(attempted=0, sent=0, enabled=1)


class TestPublishTopicDetailed(unittest.IsolatedAsyncioTestCase):
    """publish_topic_detailed 5 case — attempted/sent/enabled + eviction side-effect."""

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    def _ws(self, *, ok=True):
        ws = MagicMock()
        ws.send_json = AsyncMock(side_effect=None if ok else ConnectionError("closed"))
        return ws

    async def test_ff_off(self):
        ws = self._ws()
        topic_dispatcher.registry.register(ws, ["usdt:krw"])
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", False):
            counts = await publish_topic_detailed("usdt:krw", {"type": "tick"})
        self.assertEqual(counts, TopicSendCounts(attempted=0, sent=0, enabled=False))
        ws.send_json.assert_not_called()

    async def test_no_subscribers(self):
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            counts = await publish_topic_detailed("usdt:krw", {})
        self.assertEqual(counts, TopicSendCounts(attempted=0, sent=0, enabled=True))

    async def test_all_failed(self):
        # ALL_FAILED 판별 핵심: attempted>0 + sent==0 (bare int=0이 no-sub와 conflate하는 case)
        a, b = self._ws(ok=False), self._ws(ok=False)
        topic_dispatcher.registry.register(a, ["usdt:krw"])
        topic_dispatcher.registry.register(b, ["usdt:krw"])
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             self.assertLogs("exchange_rate.topic_dispatcher", level="WARNING"):
            counts = await publish_topic_detailed("usdt:krw", {})
        self.assertEqual(counts, TopicSendCounts(attempted=2, sent=0, enabled=True))
        self.assertGreater(counts.attempted, 0)   # no-subscribers와 구분
        # 실패 ws 둘 다 eviction
        self.assertEqual(topic_dispatcher.registry.get_subscribers("usdt:krw"), set())

    async def test_partial(self):
        ok, fail = self._ws(ok=True), self._ws(ok=False)
        topic_dispatcher.registry.register(ok, ["usdt:krw"])
        topic_dispatcher.registry.register(fail, ["usdt:krw"])
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
             self.assertLogs("exchange_rate.topic_dispatcher", level="WARNING"):
            counts = await publish_topic_detailed("usdt:krw", {})
        self.assertEqual(counts, TopicSendCounts(attempted=2, sent=1, enabled=True))
        subs = topic_dispatcher.registry.get_subscribers("usdt:krw")
        self.assertIn(ok, subs)        # 성공 유지
        self.assertNotIn(fail, subs)   # 실패 eviction

    async def test_full(self):
        a, b = self._ws(ok=True), self._ws(ok=True)
        topic_dispatcher.registry.register(a, ["usdt:krw"])
        topic_dispatcher.registry.register(b, ["usdt:krw"])
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
            counts = await publish_topic_detailed("usdt:krw", {})
        self.assertEqual(counts, TopicSendCounts(attempted=2, sent=2, enabled=True))
        self.assertEqual(len(topic_dispatcher.registry.get_subscribers("usdt:krw")), 2)


class TestPublishTopicParity(unittest.IsolatedAsyncioTestCase):
    """publish_topic == publish_topic_detailed().sent + 동일 eviction (C7 delegation 사전 잠금).

    return 값뿐 아니라 eviction side-effect까지 — ALL_FAILED→remove→다음 호출 NO_SUBSCRIBERS 전이가
    eviction 동일성에 의존(§5.5)."""

    async def asyncSetUp(self):
        self._original_registry = topic_dispatcher.registry

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry

    async def _run(self, fn, success_flags, *, ff=True):
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()
        wss = []
        for ok in success_flags:
            ws = MagicMock()
            ws.send_json = AsyncMock(side_effect=None if ok else ConnectionError("x"))
            topic_dispatcher.registry.register(ws, ["t"])
            wss.append(ws)
        with patch.object(config, "TOPIC_DISPATCHER_ENABLED", ff):
            result = await fn("t", {})
        survivors = topic_dispatcher.registry.get_subscribers("t")
        survived = [ws in survivors for ws in wss]
        return result, survived

    async def test_parity_all_cases(self):
        # 핵심: partial 포함(sent != attempted라야 disambiguation 버그 노출)
        cases = [
            ("ff_off", [True, True], False),
            ("empty", [], True),
            ("all_fail", [False, False], True),
            ("partial", [True, False, True], True),
            ("full", [True, True], True),
        ]
        for name, flags, ff in cases:
            with self.subTest(case=name):
                int_sent, int_survived = await self._run(topic_dispatcher.publish_topic, flags, ff=ff)
                d_counts, d_survived = await self._run(publish_topic_detailed, flags, ff=ff)
                self.assertEqual(int_sent, d_counts.sent)     # return parity
                self.assertEqual(int_survived, d_survived)    # eviction parity

    async def test_all_failed_then_no_subscribers_transition(self):
        """§5.5: all_failed → evict → **다음 호출** no_subscribers. 두 함수 동일(multi-call 전이 잠금).

        single-call eviction snapshot만으론 전이를 명시 검증 못 함 — 2번째 호출까지 확인."""
        async def _two_calls(fn):
            topic_dispatcher.registry = topic_dispatcher.TopicRegistry()
            for _ in range(2):
                ws = MagicMock()
                ws.send_json = AsyncMock(side_effect=ConnectionError("x"))
                topic_dispatcher.registry.register(ws, ["t"])
            with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True), \
                 self.assertLogs("exchange_rate.topic_dispatcher", level="WARNING"):
                first = await fn("t", {})          # all fail → evict
            with patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
                second = await fn("t", {})         # 구독자 0 (evicted) → no_subscribers
            return first, second

        int_first, int_second = await _two_calls(topic_dispatcher.publish_topic)
        self.assertEqual(int_first, 0)             # all failed
        self.assertEqual(int_second, 0)            # no_subscribers (evicted)

        d_first, d_second = await _two_calls(publish_topic_detailed)
        self.assertEqual(d_first, TopicSendCounts(attempted=2, sent=0, enabled=True))   # ALL_FAILED
        self.assertEqual(d_second, TopicSendCounts(attempted=0, sent=0, enabled=True))  # NO_SUBSCRIBERS


def _scan_src_for_caller(filename, src, name):
    """단일 src에서 name import/call 검출 (pure — string 기반 self-arm 가능, 파일 write 불요)."""
    hits = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return hits
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names):
            hits.append(f"{filename}: from import {name}")
        elif isinstance(node, ast.Call):
            fn = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fn == name:
                hits.append(f"{filename}: {name}() call")
    return hits


def _scan_callers_of(name, *, exclude_files):
    """app/*.py에서 name을 import/call하는 파일 목록 (no-live-caller dormancy detector)."""
    app_dir = pathlib.Path(topic_dispatcher.__file__).resolve().parent
    hits = []
    for py in sorted(app_dir.rglob("*.py")):
        if py.name in exclude_files:
            continue
        hits.extend(_scan_src_for_caller(py.name, py.read_text(encoding="utf-8"), name))
    return hits


class TestPublishTopicDetailedDormancy(unittest.TestCase):
    """publish_topic_detailed는 live 모듈(topic_dispatcher) 안이라 island AST trip-wire 불가 →
    'no live caller' grep으로 dormancy 보장 (C6-6 adapter가 첫 caller)."""

    def test_no_live_caller(self):
        # topic_dispatcher.py 자신(정의) + atomic_fx_live.py(C6-6 의도된 dormant adapter caller, 자체
        # no-importer로 dormancy 보장) 제외 — 그 외 app/ 어디서도 호출/import 0
        hits = _scan_callers_of(
            "publish_topic_detailed", exclude_files={"topic_dispatcher.py", "atomic_fx_live.py"})
        self.assertEqual(hits, [], f"publish_topic_detailed live caller 발견 — dormant 위반: {hits}")

    def test_detector_self_arms(self):
        # HIGH8: detector가 실제 caller에 trip하는지 (green-only 가드 방지). string 기반 — 파일 write 없음
        # (leak/xdist 안전, 다른 C6 scanner self-arm 패턴 정합).
        import_hit = _scan_src_for_caller(
            "_probe.py", "from app.topic_dispatcher import publish_topic_detailed\n", "publish_topic_detailed")
        self.assertTrue(import_hit, "planted import caller 미검출")
        call_hit = _scan_src_for_caller(
            "_probe.py", "def f():\n    publish_topic_detailed('t', {})\n", "publish_topic_detailed")
        self.assertTrue(call_hit, "planted call caller 미검출")


if __name__ == "__main__":
    unittest.main(verbosity=2)
