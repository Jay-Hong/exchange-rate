# tests/test_subscribe_load_snapshot_wiring.py
"""S3 — seam ③(topic_initial_snapshot) + channel 배선 계약.

설계 §7 S3 체크리스트를 잠근다:
  ① `topics_deduped_total` 은 dedupe **통과분만** 센다(앞이면 중복 topic 이 부풀어 red).
     build 실패 topic 도 수요이므로 **build 앞**에서 센다.
  ② `lease_skipped` / `connection_closed` 가 각각 해당 분기에서 오른다.
  ③ `channel` 미지정 호출은 `unattributed` 로 간다(조용히 병합하면 red).
  ④ allowlist 밖 문자열은 새 key 를 만들지 않는다(unclassified + 진단 — 모듈 계약).
  ⑤ `send_initial_snapshots` 반환 타입/값 불변.
  ⑥ `_build_snapshot_sync` 본문에 record 호출이 없다(trip-wire — REST twin 혼입 방지).
  ⑦ send 기록 catch 는 **`Exception` 한정** — CancelledError 를 `raised` 로 접으면 red.
"""

# 표준 라이브러리
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# 로컬
from app import subscribe_load_metrics as slm
from app import topic_dispatcher
from app.topic_initial_snapshot import send_initial_snapshots


def _payload(topic):
    return {"type": "snapshot", "version": 1, "topic": topic, "data": {}}


class SnapshotWiringTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._hard_reset()
        self._original_registry = topic_dispatcher.registry
        topic_dispatcher.registry = topic_dispatcher.TopicRegistry()
        self.ws = MagicMock()
        self.ws.send_json = AsyncMock()
        self.ws.close = AsyncMock()

    async def asyncTearDown(self):
        topic_dispatcher.registry = self._original_registry
        self._hard_reset()

    @staticmethod
    def _hard_reset():
        with slm._lock:
            slm._metrics.clear()
            slm._metrics.update(slm._blank())

    @staticmethod
    def _snap():
        return slm.subscribe_load_metrics()

    def _register(self, topics):
        topic_dispatcher.registry.register(self.ws, topics)

    def _patch_build(self, side_effect):
        return patch("app.topic_initial_snapshot._build_snapshot_sync",
                     side_effect=side_effect)


class TestChannelAttribution(SnapshotWiringTestCase):
    async def test_unspecified_channel_lands_in_unattributed(self):
        """③ — 미지정 호출 = 배선 gap 신호. 조용히 다른 채널에 병합되면 red."""
        self._register(["fx:usd-krw"])
        with self._patch_build(_payload):
            await send_initial_snapshots(self.ws, ["fx:usd-krw"])
        calls = self._snap()["snapshot_send"]["calls_by_channel"]
        self.assertEqual(calls["unattributed"], 1)
        self.assertEqual(calls["anonymous"] + calls["token_bearing"], 0)

    async def test_explicit_channels_land_in_their_buckets(self):
        for ch in ("anonymous", "token_bearing"):
            with self.subTest(channel=ch):
                self._hard_reset()
                self._register(["fx:usd-krw"])
                with self._patch_build(_payload):
                    await send_initial_snapshots(self.ws, ["fx:usd-krw"], channel=ch)
                self.assertEqual(
                    self._snap()["snapshot_send"]["calls_by_channel"][ch], 1)

    async def test_dispatcher_call_sites_pass_their_channels(self):
        """두 dispatcher 호출이 channel과 같은 request budget을 함께 넘긴다."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(topic_dispatcher))
        observed = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "send_initial_snapshots" or len(node.args) < 2:
                continue
            topic_arg = node.args[1]
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
            if not isinstance(topic_arg, ast.Name):
                continue
            channel = kwargs.get("channel")
            budget = kwargs.get("budget")
            if isinstance(channel, ast.Constant) and isinstance(budget, ast.Name):
                observed.add((topic_arg.id, channel.value, budget.id))
        self.assertEqual(observed, {
            ("free_topics", "anonymous", "request_budget"),
            ("accepted_names", "token_bearing", "request_budget"),
        })

    async def test_unknown_channel_folds_without_new_keys(self):
        """④ — allowlist 밖 문자열은 key 를 늘리지 않고 unclassified + 진단."""
        self._register(["fx:usd-krw"])
        with self._patch_build(_payload):
            with self.assertLogs("exchange_rate.subscribe_load", level="WARNING"):
                await send_initial_snapshots(self.ws, ["fx:usd-krw"], channel="typo")
        snap = self._snap()
        calls = snap["snapshot_send"]["calls_by_channel"]
        self.assertEqual(sorted(calls), sorted(slm.CHANNEL_KEYS), "새 key 가 생겼다")
        self.assertEqual(calls["unclassified"], 1)
        self.assertEqual(snap["metrics_internal_errors_total"], 1)


class TestRequestDenominatorIsExactlyOnce(SnapshotWiringTestCase):
    async def test_empty_topics_still_counts_one_request(self):
        """빈 요청도 분모 1 — 미래의 직접 호출부(unattributed 가 겨냥하는 대상)가 빈 리스트로
        불러도 요청 수는 정직해야 한다. record 앞 early-return 변이가 이걸 깬다."""
        sent = await send_initial_snapshots(self.ws, [])
        self.assertEqual(sent, 0)
        snap = self._snap()
        self.assertEqual(snap["snapshot_send"]["calls_by_channel"]["unattributed"], 1)
        self.assertEqual(snap["snapshot_send"]["topics_deduped_total"], 0)


class TestDedupeAndBuildAxis(SnapshotWiringTestCase):
    async def test_duplicate_success_topic_counts_and_builds_once(self):
        """dedupe 전 계수 변이는 성공 경로에서만 두 번째 topic까지 도달해 드러난다."""
        self._register(["fx:usd-krw"])
        with self._patch_build(_payload) as build:
            sent = await send_initial_snapshots(
                self.ws, ["fx:usd-krw", "fx:usd-krw"]
            )
        self.assertEqual(sent, 1)
        self.assertEqual(build.call_count, 1)
        self.assertEqual(
            self._snap()["snapshot_send"]["topics_deduped_total"], 1
        )

    async def test_fatal_topic_is_counted_before_build_and_stops_remaining_topics(self):
        """fatal build도 수요로 센 뒤 1011로 끝내며, 남은 topic은 시작하지 않는다."""
        self._register(["fx:usd-krw", "usdt:krw"])

        def build(topic):
            if topic == "fx:usd-krw":
                raise RuntimeError("build boom")
            return _payload(topic)
        from app.topic_wire import InitialSnapshotFatalFailure
        with self._patch_build(build):
            with self.assertRaises(InitialSnapshotFatalFailure):
                await send_initial_snapshots(
                    self.ws, ["fx:usd-krw", "fx:usd-krw", "usdt:krw"])
        snap = self._snap()
        self.assertEqual(snap["snapshot_send"]["topics_deduped_total"], 1)
        # ⛔ multi-topic 요청 1건 = 요청 분모 1 — loop 안으로 옮기면 topic 수만큼 부푼다.
        self.assertEqual(snap["snapshot_send"]["calls_by_channel"]["unattributed"], 1)
        block = snap[slm.SNAPSHOT_BUILD]
        self.assertEqual(block["by_outcome"]["build_failed"], 1, "build 예외는 파생 기록")
        self.assertEqual(block["by_outcome"]["built"], 0)
        self.assertEqual(block["callers_awaiting"], 0)

    async def test_none_payload_is_a_domain_outcome_not_a_failure(self):
        with self._patch_build(lambda t: None):
            sent = await send_initial_snapshots(self.ws, ["dxy"])
        self.assertEqual(sent, 0)
        block = self._snap()[slm.SNAPSHOT_BUILD]
        self.assertEqual(block["by_outcome"]["none_payload"], 1)
        self.assertEqual(block["by_outcome"]["build_failed"], 0)

    async def test_worker_axis_counts_through_timed_call(self):
        self._register(["fx:usd-krw"])
        with self._patch_build(_payload):
            await send_initial_snapshots(self.ws, ["fx:usd-krw"])
        block = self._snap()[slm.SNAPSHOT_BUILD]
        self.assertEqual(block["worker_started_total"], 1)
        self.assertEqual(block["worker_finished_total"], 1)

    async def test_metrics_contract_error_is_not_swallowed_as_build_failure(self):
        """축·handle 배선 오류는 fail-fast하고 외부 build_failed 버킷을 오염시키지 않는다."""
        def contract_boom(*args, **kwargs):
            raise slm.SubscribeLoadContractError("axis/handle mismatch")

        from app.topic_wire import InitialSnapshotFatalFailure

        with patch.object(slm, "timed_call", new=contract_boom):
            with self.assertRaises(InitialSnapshotFatalFailure) as raised:
                await send_initial_snapshots(self.ws, ["fx:usd-krw"])
        self.assertIsInstance(raised.exception.__cause__, slm.SubscribeLoadContractError)
        snap = self._snap()
        block = snap[slm.SNAPSHOT_BUILD]
        self.assertEqual(block["by_outcome"]["build_failed"], 0)
        self.assertEqual(block["by_outcome"]["unclassified"], 1)
        self.assertEqual(block["callers_awaiting"], 0)
        self.assertEqual(snap["metrics_internal_errors_total"], 1)

    def test_the_shared_builder_contains_no_instrumentation(self):
        """⑥ — 공유 builder 본문에는 계측을 두지 않는다.

        ⚠️ **근거가 S0(`a2621df`)에서 바뀌었다.** 구 근거는 *"REST 호출이 WS 축으로 계측된다"*
        였는데, 이제 REST twin 합산은 **의도**다(`subscribe-load/7`: snapshot_build 는 WS+twin).
        지금의 근거는 둘이다:
          1. `queue_wait` = (worker 시작 − caller 제출)이라 **caller 쪽 시각**이 필요하다.
             본문은 worker 시작 이후만 보므로 그 값을 잴 수 없다.
          2. 공유 래퍼(`build_snapshot_observed`)가 이미 감싸므로 본문 계측은 **이중 계수**다.
        """
        import inspect

        from app import topic_initial_snapshot as tis

        src = inspect.getsource(tis._build_snapshot_sync)
        for token in ("subscribe_load", "record_", "observe(", "timed_call"):
            self.assertNotIn(
                token, src,
                f"공유 builder 본문에 계측({token})이 들어왔다 — 계측은 공유 래퍼가 소유한다")


class TestSendOutcomes(SnapshotWiringTestCase):
    async def test_lease_skipped_counts_the_wasted_build(self):
        """② — 등록 없이 호출(구독 소멸 모델) → build 후 skip. 낭비의 신호."""
        with self._patch_build(_payload):
            sent = await send_initial_snapshots(self.ws, ["fx:usd-krw"])
        self.assertEqual(sent, 0)
        send = self._snap()["snapshot_send"]
        self.assertEqual(send["sends_by_outcome"]["lease_skipped"], 1)
        self.assertEqual(send["sends_by_outcome"]["sent"], 0)
        self.ws.send_json.assert_not_called()

    async def test_connection_closed_counts_and_the_signal_still_propagates(self):
        """② — WebSocketDisconnect → connection_closed + 기존 신호 전파(⑤ wire 불변)."""
        from starlette.websockets import WebSocketDisconnect

        from app.topic_wire import InitialSnapshotConnectionClosed

        self.ws.send_json = AsyncMock(side_effect=WebSocketDisconnect(code=1006))
        self._register(["fx:usd-krw", "usdt:krw"])
        with self._patch_build(_payload):
            with self.assertRaises(InitialSnapshotConnectionClosed):
                await send_initial_snapshots(self.ws, ["fx:usd-krw", "usdt:krw"])
        send = self._snap()["snapshot_send"]
        self.assertEqual(send["sends_by_outcome"]["connection_closed"], 1)
        self.assertEqual(send["sends_by_outcome"]["sent"], 0)
        # ⛔ 조기 이탈에도 요청 분모는 남는다 — record 를 loop 뒤로 옮기면 여기가 0 이 된다.
        self.assertEqual(send["calls_by_channel"]["unattributed"], 1)

    async def test_programming_error_records_raised_and_propagates(self):
        """⑦의 절반 — send 의 일반 예외는 raised 로 기록되고 **그대로 전파**(접지 않는다)."""
        self.ws.send_json = AsyncMock(side_effect=TypeError("not serializable"))
        self._register(["fx:usd-krw"])
        with self._patch_build(_payload):
            with self.assertRaises(TypeError):
                await send_initial_snapshots(self.ws, ["fx:usd-krw"])
        send = self._snap()["snapshot_send"]
        self.assertEqual(send["sends_by_outcome"]["raised"], 1)
        self.assertEqual(send["sends_by_outcome"]["connection_closed"], 0,
                         "프로그래밍 오류가 정상 종료로 접혔다")
        self.assertEqual(send["calls_by_channel"]["unattributed"], 1, "조기 이탈에도 분모 유지")

    async def test_cancellation_is_not_recorded_as_raised(self):
        """⑦ — catch 를 BaseException 으로 넓히면 취소가 raised 를 오염시킨다."""
        self.ws.send_json = AsyncMock(side_effect=asyncio.CancelledError())
        self._register(["fx:usd-krw"])
        with self._patch_build(_payload):
            with self.assertRaises(asyncio.CancelledError):
                await send_initial_snapshots(self.ws, ["fx:usd-krw"])
        send = self._snap()["snapshot_send"]
        self.assertEqual(send["sends_by_outcome"]["raised"], 0,
                         "취소가 실제 전송 예외 신호를 오염시켰다")
        self.assertEqual(sum(send["sends_by_outcome"].values()), 0)
        self.assertEqual(send["calls_by_channel"]["unattributed"], 1, "조기 이탈에도 분모 유지")

    async def test_sent_outcome_and_return_value_stay_in_lockstep(self):
        """⑤ — 반환값과 sent 버킷이 같은 사실을 센다."""
        self._register(["fx:usd-krw", "usdt:krw"])
        with self._patch_build(_payload):
            sent = await send_initial_snapshots(self.ws, ["fx:usd-krw", "usdt:krw"])
        self.assertEqual(sent, 2)
        self.assertEqual(self._snap()["snapshot_send"]["sends_by_outcome"]["sent"], 2)


if __name__ == "__main__":
    unittest.main()
