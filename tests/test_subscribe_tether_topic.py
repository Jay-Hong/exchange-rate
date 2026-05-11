"""subscribe_tether_topic.py 단위 테스트 (PR Z-2b Level 3 시험 도구).

검증:
    - legacy payload 분류 (type=rates 등 → [LEGACY])
    - topic snapshot 분류 (type=snapshot + version=1 + usdt_krw → [TOPIC])
    - invalid JSON / 알 수 없는 형식 → [RAW]
    - timeout 종료 시 summary 출력
    - 종료 전 unsubscribe 시도 (best-effort, 닫힌 연결도 격리)

main() 통합 테스트는 websockets 실제 연결 없이 mock으로 검증.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# scripts/ 경로 추가 (observe_kis_master / preview_tether_tab 패턴 일관)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import subscribe_tether_topic  # noqa: E402


class TestClassifyMessage(unittest.TestCase):
    """classify_message 분류 정확성."""

    def test_topic_snapshot_with_usdt_krw(self):
        msg = json.dumps({
            "type": "snapshot",
            "version": 1,
            "data": {"usdt_krw": [], "usd_krw_banks": []},
        })
        kind, data = subscribe_tether_topic.classify_message(msg)
        self.assertEqual(kind, "TOPIC")
        self.assertEqual(data["type"], "snapshot")

    def test_topic_snapshot_without_usdt_krw_is_legacy(self):
        """version=1이지만 usdt_krw 없으면 topic 아님."""
        msg = json.dumps({
            "type": "snapshot",
            "version": 1,
            "data": {"other": "..."},
        })
        kind, _ = subscribe_tether_topic.classify_message(msg)
        self.assertEqual(kind, "LEGACY")

    def test_legacy_rates_payload(self):
        """기존 broadcast (type=rates) → LEGACY."""
        msg = json.dumps({
            "type": "rates",
            "data": {"rates": [], "indices": {}, "metadata": {}},
        })
        kind, _ = subscribe_tether_topic.classify_message(msg)
        self.assertEqual(kind, "LEGACY")

    def test_pong_is_pong(self):
        msg = json.dumps({"type": "pong"})
        kind, _ = subscribe_tether_topic.classify_message(msg)
        self.assertEqual(kind, "PONG")

    def test_invalid_json_is_raw(self):
        msg = "this is not json {"
        kind, data = subscribe_tether_topic.classify_message(msg)
        self.assertEqual(kind, "RAW")
        self.assertEqual(data, {})

    def test_non_dict_json_is_raw(self):
        msg = json.dumps([1, 2, 3])  # JSON array
        kind, _ = subscribe_tether_topic.classify_message(msg)
        self.assertEqual(kind, "RAW")


class TestRun(unittest.IsolatedAsyncioTestCase):
    """run() 통합 — websockets connect mock."""

    async def _run_with_messages(
        self,
        messages: list,
        timeout: float = 0.05,
    ) -> tuple:
        """messages는 ws.recv가 순차 반환할 raw 문자열 list.
        반환: (sent_messages, exit_code, captured_stdout).

        recv_timeout=0.01로 wait_for 차단 시간 단축 (Codex 권고). 모든 run 테스트
        stdout 캡처해 회귀 실행 시 누출 차단.
        """
        sent = []

        class FakeWS:
            async def send(self, msg):
                sent.append(msg)

            async def recv(self):
                if messages:
                    return messages.pop(0)
                # 메시지 소진 후 timeout 시뮬레이션 — asyncio.wait_for가 짧게 자름
                await asyncio.sleep(10)
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        def fake_connect(url, **kwargs):
            return FakeWS()

        import io
        captured = io.StringIO()
        with patch.object(subscribe_tether_topic.websockets, "connect", fake_connect), \
             patch("sys.stdout", captured):
            rc = await subscribe_tether_topic.run(
                url="wss://test/ws",
                timeout=timeout,
                topic="usdt:krw",
                recv_timeout=0.01,  # 테스트 빠른 실행 (Codex 권고)
            )
        return sent, rc, captured.getvalue()

    async def test_subscribe_and_unsubscribe_sent(self):
        """연결 시 subscribe 전송 + 종료 시 unsubscribe 전송."""
        sent, rc, _ = await self._run_with_messages(
            messages=[json.dumps({"type": "pong"})],
        )
        self.assertEqual(rc, 0)
        # 첫 메시지: subscribe
        self.assertIn("subscribe", sent[0])
        self.assertIn("usdt:krw", sent[0])
        # 마지막 메시지: unsubscribe
        self.assertIn("unsubscribe", sent[-1])

    async def test_topic_message_classified_and_counted(self):
        """topic snapshot 메시지를 TOPIC으로 분류 + summary 카운트."""
        topic_payload = json.dumps({
            "type": "snapshot",
            "version": 1,
            "data": {"usdt_krw": [{"source": "upbit"}]},
        })
        legacy_payload = json.dumps({
            "type": "rates",
            "data": {"rates": []},
        })
        sent, rc, out = await self._run_with_messages(
            messages=[legacy_payload, topic_payload],
        )
        self.assertEqual(rc, 0)
        self.assertIn("[TOPIC]", out)
        self.assertIn("[LEGACY]", out)
        self.assertIn("[SUMMARY]", out)
        self.assertIn("topic=1", out)
        self.assertIn("legacy=1", out)

    async def test_invalid_json_emits_raw(self):
        """JSON 파싱 실패 메시지는 [RAW]로 출력."""
        sent, rc, out = await self._run_with_messages(
            messages=["not json {{{"],
        )
        self.assertEqual(rc, 0)
        self.assertIn("[RAW]", out)
        self.assertIn("raw=1", out)

    async def test_timeout_summary_printed(self):
        """timeout 종료 후 summary 라인 출력 + 순서."""
        sent, rc, out = await self._run_with_messages(messages=[])
        self.assertEqual(rc, 0)
        self.assertIn("[CONNECTED]", out)
        self.assertIn("[SUBSCRIBED]", out)
        self.assertIn("[SUMMARY] topic=0 legacy=0 raw=0", out)
        self.assertIn("[UNSUBSCRIBED]", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
