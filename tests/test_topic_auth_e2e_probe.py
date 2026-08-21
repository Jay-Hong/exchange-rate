import asyncio
import io
import json
import unittest

from scripts import topic_auth_e2e_probe as probe


LEGACY_RATES = {
    "type": "rates",
    "data": {
        "rates": [{
            "bank": "kb",
            "currency": "usd-krw",
            "rate": 1398.0,
            "timestamp": "2026-08-21T14:30:00+09:00",
        }]
    },
}
DXY_SNAPSHOT = {
    "type": "snapshot",
    "version": 1,
    "topic": probe.DXY_TOPIC,
    "data": {
        "dxy": {
            "rate": 104.52,
            "timestamp": "2026-08-21T14:30:00+09:00",
            "source": "investing",
        }
    },
}
USDT_SNAPSHOT = {
    "type": "snapshot",
    "version": 1,
    "topic": probe.USDT_TOPIC,
    "data": {
        "usdt_krw": [{
            "source": "upbit",
            "asset": "usdt-krw",
            "rate": 1399.0,
            "timestamp": "2026-08-21T14:30:00+09:00",
        }]
    },
}


def leased(topic):
    return {
        "topic": topic,
        "lease_id": f"lease-{topic}",
        "lease_duration_seconds": 600,
    }


class FakeWebSocket:
    def __init__(self, on_send=None):
        self.frames = asyncio.Queue()
        self.frames.put_nowait(json.dumps(LEGACY_RATES))
        self.on_send = on_send
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, value):
        self.sent.append(value)
        if self.on_send is not None:
            self.on_send(self, value)

    async def recv(self):
        return await self.frames.get()

    def push(self, frame):
        self.frames.put_nowait(json.dumps(frame))


class ConnectSequence:
    def __init__(self, sockets):
        self.sockets = list(sockets)

    def __call__(self):
        if not self.sockets:
            raise AssertionError("unexpected extra websocket connection")
        return self.sockets.pop(0)


def anonymous_socket(*, leak=False, live_rates=True):
    def on_send(ws, raw):
        if raw == "ping":
            ws.push({"type": "pong"})
            if live_rates:
                ws.push(LEGACY_RATES)
            return
        message = json.loads(raw)
        if message.get("type") == "subscribe" and leak:
            ws.push(USDT_SNAPSHOT)

    return FakeWebSocket(on_send)


def nonpremium_socket(*, accepts=False):
    def on_send(ws, raw):
        message = json.loads(raw)
        if message.get("type") != "subscribe":
            return
        request_id = message["request_id"]
        if accepts:
            ws.push({
                "type": "subscription_ack",
                "request_id": request_id,
                "operation": "subscribe",
                "accepted_topics": [leased(probe.DXY_TOPIC)],
                "rejected_topics": [],
                "removed_topics": [],
                "active_subscriptions": [leased(probe.DXY_TOPIC)],
            })
            ws.push(DXY_SNAPSHOT)
            return
        ws.push({
            "type": "subscription_ack",
            "request_id": request_id,
            "operation": "subscribe",
            "accepted_topics": [],
            "rejected_topics": [{
                "topic": probe.DXY_TOPIC,
                "error": "premium_required",
            }],
            "removed_topics": [],
            "active_subscriptions": [],
        })

    return FakeWebSocket(on_send)


def premium_socket(*, omit_dxy_lease=False):
    def on_send(ws, raw):
        message = json.loads(raw)
        if message.get("type") != "subscribe":
            return
        request_id = message["request_id"]
        topics = [probe.USDT_TOPIC] if omit_dxy_lease else list(probe.PREMIUM_TOPICS)
        ws.push({
            "type": "subscription_ack",
            "request_id": request_id,
            "operation": "subscribe",
            "accepted_topics": [leased(topic) for topic in topics],
            "rejected_topics": [],
            "removed_topics": [],
            "active_subscriptions": [leased(topic) for topic in sorted(topics)],
        })
        ws.push(USDT_SNAPSHOT)
        ws.push(DXY_SNAPSHOT)

    return FakeWebSocket(on_send)


class TestTokenInput(unittest.TestCase):
    def test_exactly_two_distinct_tokens(self):
        self.assertEqual(
            probe.read_token_pair(io.StringIO("premium\nnonpremium\n")),
            ("premium", "nonpremium"),
        )
        for raw in ("", "one\n", "same\nsame\n", "a\nb\nc\n"):
            with self.subTest(raw=raw), self.assertRaises(probe.ProbeFailure):
                probe.read_token_pair(io.StringIO(raw))


class TestWireValidators(unittest.TestCase):
    def test_premium_ack_requires_a_lease_for_both_topics(self):
        frame = {
            "type": "subscription_ack",
            "request_id": "r1",
            "operation": "subscribe",
            "accepted_topics": [leased(probe.USDT_TOPIC)],
            "rejected_topics": [],
            "active_subscriptions": [leased(probe.USDT_TOPIC)],
        }
        with self.assertRaises(probe.ProbeFailure):
            probe.validate_premium_ack(frame, "r1")

    def test_dxy_snapshot_requires_aware_timestamp_and_positive_rate(self):
        probe.validate_dxy_snapshot(DXY_SNAPSHOT)
        bad = json.loads(json.dumps(DXY_SNAPSHOT))
        for value in ("2026-08-21T14:30:00", "2026-08-21T05:30:00+00:00"):
            bad = json.loads(json.dumps(DXY_SNAPSHOT))
            bad["data"]["dxy"]["timestamp"] = value
            with self.subTest(value=value), self.assertRaises(probe.ProbeFailure):
                probe.validate_dxy_snapshot(bad)

    def test_usdt_snapshot_requires_aware_timestamp_on_every_row(self):
        """⛔ DXY 만 검사하면 비대칭이라 USDT 쪽이 false-green 이 된다.

        `timestamp` 는 공통 entry 필수다 — 클라가 (source,asset) merge 를 이걸로 판정하므로
        누락·naive 면 merge 자체가 불가능한데, 검사 없이는 그 snapshot 이 통과한다.
        """
        probe.validate_usdt_snapshot(USDT_SNAPSHOT)
        for mutate in (
            lambda row: row.pop("timestamp"),
            lambda row: row.__setitem__("timestamp", "2026-08-21T14:30:00"),   # naive
            lambda row: row.__setitem__("timestamp", "not-a-timestamp"),
            lambda row: row.__setitem__("timestamp", 1755780600),              # 숫자
            # ⛔ 계약은 `ISO8601 KST` 다 — `+00:00` 은 같은 순간이지만 계약 위반이고,
            #    aware 검사만으로는 통과한다(dxy 문자열 분기가 원문을 통과시키는 경로).
            lambda row: row.__setitem__("timestamp", "2026-08-21T05:30:00+00:00"),
        ):
            bad = json.loads(json.dumps(USDT_SNAPSHOT))
            mutate(bad["data"]["usdt_krw"][0])
            with self.subTest(mutate=mutate.__name__), self.assertRaises(probe.ProbeFailure):
                probe.validate_usdt_snapshot(bad)

    def test_usdt_optional_rate_changed_at_is_format_locked_when_present(self):
        """optional 이라 없어도 되지만, 있으면 same-bucket ordering 계약의 입력이다."""
        ok = json.loads(json.dumps(USDT_SNAPSHOT))
        ok["data"]["usdt_krw"][0]["rate_changed_at"] = "2026-08-21T14:29:58+09:00"
        probe.validate_usdt_snapshot(ok)
        for value in ("2026-08-21T14:29:58", "nope", 17557806, "2026-08-21T05:29:58+00:00"):
            bad = json.loads(json.dumps(USDT_SNAPSHOT))
            bad["data"]["usdt_krw"][0]["rate_changed_at"] = value
            with self.subTest(value=value), self.assertRaises(probe.ProbeFailure):
                probe.validate_usdt_snapshot(bad)


class TestFunctionalProbe(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.timings = probe.ProbeTimings(
            frame_timeout_seconds=0.2,
            anonymous_silence_seconds=0.02,
            denied_silence_seconds=0.02,
        )

    async def test_full_matrix_passes_and_summary_contains_no_tokens(self):
        sockets = [
            anonymous_socket(live_rates=False),
            nonpremium_socket(),
            premium_socket(),
        ]
        connect = ConnectSequence(sockets)
        counts = iter((0, 0))

        def request_snapshot(topic, token):
            self.assertEqual(topic, probe.DXY_TOPIC)
            if token is None:
                return probe.HttpResult(401, {"detail": "unauthorized"}, {})
            if token == "nonpremium-secret":
                return probe.HttpResult(403, {"detail": "premium required"}, {})
            if token == "premium-secret":
                return probe.HttpResult(200, DXY_SNAPSHOT, {})
            raise AssertionError("unexpected token")

        result = await probe.run_probe(
            "premium-secret",
            "nonpremium-secret",
            connect=connect,
            request_snapshot=request_snapshot,
            request_legacy=lambda: probe.HttpResult(
                200,
                {"rates": LEGACY_RATES["data"]["rates"], "metadata": {"total_count": 1}},
                {},
            ),
            admin_count=lambda: next(counts),
            timings=self.timings,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["rest"]["legacy_anonymous"], 200)
        self.assertEqual(result["anonymous_ws"]["legacy_updates_seen"], 0)
        rendered = json.dumps(result)
        self.assertNotIn("premium-secret", rendered)
        self.assertNotIn("nonpremium-secret", rendered)
        self.assertEqual(connect.sockets, [])

    async def test_anonymous_snapshot_leak_is_rejected(self):
        counts = iter((0, 0))
        with self.assertRaisesRegex(probe.ProbeFailure, "익명 subscribe"):
            await probe.probe_anonymous_ws(
                ConnectSequence([anonymous_socket(leak=True)]),
                lambda: next(counts),
                self.timings,
            )

    async def test_anonymous_registry_delta_is_rejected(self):
        counts = iter((0, 1))
        with self.assertRaisesRegex(probe.ProbeFailure, "registry"):
            await probe.probe_anonymous_ws(
                ConnectSequence([anonymous_socket()]),
                lambda: next(counts),
                self.timings,
            )

    async def test_nonzero_registry_baseline_is_rejected(self):
        with self.assertRaisesRegex(probe.ProbeFailure, "시작 전 registry"):
            await probe.probe_anonymous_ws(
                ConnectSequence([anonymous_socket()]),
                lambda: 2,
                self.timings,
            )

    def test_legacy_rest_requires_nonempty_consistent_payload(self):
        probe.validate_legacy_rest(probe.HttpResult(
            200,
            {"rates": LEGACY_RATES["data"]["rates"], "metadata": {"total_count": 1}},
            {},
        ))
        with self.assertRaises(probe.ProbeFailure):
            probe.validate_legacy_rest(probe.HttpResult(
                200,
                {"rates": [], "metadata": {"total_count": 0}},
                {},
            ))

    async def test_nonpremium_acceptance_is_rejected(self):
        with self.assertRaises(probe.ProbeFailure):
            await probe.probe_nonpremium_ws(
                ConnectSequence([nonpremium_socket(accepts=True)]),
                "nonpremium-secret",
                self.timings,
            )

    async def test_premium_missing_dxy_lease_is_rejected(self):
        with self.assertRaises(probe.ProbeFailure):
            await probe.probe_premium_ws(
                ConnectSequence([premium_socket(omit_dxy_lease=True)]),
                "premium-secret",
                self.timings,
            )


if __name__ == "__main__":
    unittest.main()
