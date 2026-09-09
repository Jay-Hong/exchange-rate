"""IBK 경보 전달 — 접수와 발송을 분리하고, 억제 이음매의 최종 실패를 남기는지 잠근다.

핵심 계약: 부모는 **큐 접수가 성공하면 같은 문제의 반복 알림을 억제한다.** 따라서
"접수 성공 → 발송 실패" 회차는 부모가 다시 알려주지 않는다. 그 회차를 유한 재시도하고
최종 실패로 기록하는 것이 이 모듈의 유일한 존재 이유다.

여기서는 **실제 큐와 실제 소비자 스레드**를 돌리고, 바깥으로 나가는 Telegram 요청만 바꿔 끼운다.
실제 사용자에게 발송하지 않는다.

⛔ 여기서 잠그지 않는 것: 사용자가 알림을 **열람**했는지. 전달 계층은 그걸 관측할 수 없고
   `sent` 는 발송 콜러블의 성공 보고일 뿐이다.
⛔ 이 모듈은 아직 배선되지 않았다 — 여기 테스트는 배선 없이 모듈 계약만 본다.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import logging
import pathlib
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from app.ibk_notice_delivery import (
    IbkNoticeDelivery,
    build_default_delivery,
    format_notice,
)
from app.ibk_parent_runner import IbkCleanupBlock, IbkNotice, IbkParentRunner
from app.ibk_result_protocol import (
    PAIRS, IbkReason, IbkResult, IbkSource, IbkStatus, encode_ibk_result,
)
from app.ibk_run_context import IbkRunContext
from app.ibk_subprocess_capture import IbkProcessCapture

DEADLINE = 3.0


def notice(kind="attention", run_id="run-1", classification="DEGRADED",
           reason="SELENIUM_STRICT_REJECTED", cleanup_block=None):
    return IbkNotice(kind, run_id, classification, reason, cleanup_block)


def wait_until(predicate, deadline=DEADLINE):
    """스레드 경계라 고정 sleep 대신 조건 폴링으로 기다린다."""
    limit = time.monotonic() + deadline
    while time.monotonic() < limit:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class RecordingSend:
    """바깥으로 나가는 요청 자리. 실제 Telegram 을 호출하지 않는다."""

    def __init__(self, outcomes=None, block=None):
        self.calls, self._outcomes, self._block = [], list(outcomes or []), block
        self.entered = threading.Event()

    def __call__(self, text):
        self.calls.append(text)
        self.entered.set()
        if self._block is not None:
            self._block.wait(DEADLINE)
        if not self._outcomes:
            return True
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class DeliveryTestCase(unittest.TestCase):
    def build(self, send, **kwargs):
        kwargs.setdefault("retry_seconds", 0)
        kwargs.setdefault("poll_seconds", 0.01)
        delivery = IbkNoticeDelivery(send=send, **kwargs)
        self.addCleanup(delivery.close, DEADLINE)
        return delivery


class AcceptanceIsNotDelivery(DeliveryTestCase):
    def test_a_slow_send_does_not_delay_acceptance(self):
        release = threading.Event()
        self.addCleanup(release.set)
        send = RecordingSend(block=release)
        delivery = self.build(send).start()

        self.assertTrue(delivery.event_sink(notice(run_id="slow")))
        self.assertTrue(send.entered.wait(DEADLINE), "소비자가 발송을 시작해야 한다")

        started = time.monotonic()
        self.assertTrue(delivery.event_sink(notice(run_id="second")))
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5, f"발송이 막혀 있어도 접수는 즉시여야 한다 (실측 {elapsed:.3f}s)")
        self.assertEqual(delivery.stats()["accepted"], 2)
        self.assertEqual(delivery.stats()["sent"], 0, "아직 아무것도 보내지지 않았다")

    def test_acceptance_and_send_success_are_counted_separately(self):
        send = RecordingSend()
        delivery = self.build(send).start()
        self.assertTrue(delivery.event_sink(notice()))
        self.assertTrue(wait_until(lambda: delivery.stats()["sent"] == 1))
        stats = delivery.stats()
        self.assertEqual((stats["accepted"], stats["sent"], stats["send_failed"]), (1, 1, 0))
        self.assertEqual(len(send.calls), 1)


class TheSuppressionSeam(DeliveryTestCase):
    """부모가 억제로 넘어간 뒤 발송이 실패하는 구간 — 이 모듈이 존재하는 이유."""

    def test_enqueue_succeeds_but_the_send_fails_and_is_finally_recorded(self):
        send = RecordingSend([False, False, False])
        delivery = self.build(send, max_attempts=3).start()

        self.assertTrue(delivery.event_sink(notice(run_id="seam")),
                        "접수는 성공한다 — 여기서 부모는 억제 상태로 넘어간다")
        self.assertTrue(wait_until(lambda: delivery.stats()["send_failed_final"] == 1))

        stats = delivery.stats()
        self.assertEqual(stats["send_attempts"], 3, "유한 재시도")
        self.assertEqual(stats["sent"], 0)
        self.assertEqual([r["run_id"] for r in delivery.unsent()], ["seam"],
                         "부모는 다시 알려주지 않으므로 여기 기록이 유일한 흔적이다")
        self.assertEqual(delivery.unsent()[0]["outcome"], "send_failed_final")

    def test_a_transient_failure_is_retried_and_then_succeeds(self):
        send = RecordingSend([False, True])
        delivery = self.build(send, max_attempts=3).start()
        self.assertTrue(delivery.event_sink(notice()))
        self.assertTrue(wait_until(lambda: delivery.stats()["sent"] == 1))
        stats = delivery.stats()
        self.assertEqual((stats["send_attempts"], stats["send_failed"]), (2, 1))
        self.assertEqual(stats["send_failed_final"], 0)
        self.assertEqual(delivery.unsent(), ())

    def test_retries_are_bounded_by_max_attempts(self):
        send = RecordingSend([False] * 20)
        delivery = self.build(send, max_attempts=2).start()
        self.assertTrue(delivery.event_sink(notice()))
        self.assertTrue(wait_until(lambda: delivery.stats()["send_failed_final"] == 1))
        time.sleep(0.05)  # 상한을 넘겨 계속 시도하지 않는지 본다
        self.assertEqual(delivery.stats()["send_attempts"], 2)

    def test_no_wait_follows_the_final_attempt(self):
        # 마지막 시도 뒤에 재시도 대기를 넣으면 최종 실패 기록이 그만큼 늦어진다.
        send = RecordingSend([False])
        delivery = self.build(send, max_attempts=1, retry_seconds=30).start()
        started = time.monotonic()
        self.assertTrue(delivery.event_sink(notice()))
        self.assertTrue(wait_until(lambda: delivery.stats()["send_failed_final"] == 1))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, f"마지막 시도 뒤 대기는 없어야 한다 (실측 {elapsed:.3f}s)")

    def test_a_raising_send_is_a_failure_and_the_consumer_survives(self):
        send = RecordingSend([RuntimeError("PRIVATE_SEND_DETAIL"), True])
        delivery = self.build(send, max_attempts=2).start()
        self.assertTrue(delivery.event_sink(notice()))
        self.assertTrue(wait_until(lambda: delivery.stats()["sent"] == 1))
        self.assertTrue(delivery.is_running(), "예외가 소비자를 죽이면 안 된다")

    def test_a_send_exception_does_not_leak_its_detail(self):
        send = RecordingSend([RuntimeError("PRIVATE_SEND_DETAIL")] * 2)
        delivery = self.build(send, max_attempts=2).start()
        with self.assertLogs("exchange_rate.ibk.notice_delivery", level="WARNING") as captured:
            delivery.event_sink(notice())
            self.assertTrue(wait_until(lambda: delivery.stats()["send_failed_final"] == 1))
        # ⛔ captured.output 은 포맷된 메시지뿐이라 extra 를 담지 않는다 — 그것만 보면
        #    "없다" 가 공허하게 통과한다. 레코드 속성과 exc_info 까지 합쳐서 본다.
        joined = "\n".join(captured.output)
        for record in captured.records:
            joined += "\n" + str(record.__dict__)
            if record.exc_info:
                joined += "\n" + logging.Formatter().formatException(record.exc_info)
        self.assertIn("RuntimeError", joined, "양성 대조 — 검사 대상에 실제 내용이 있어야 한다")
        self.assertNotIn("PRIVATE_SEND_DETAIL", joined)
        self.assertNotIn("PRIVATE_SEND_DETAIL", str(delivery.unsent()))


class RejectionKeepsTheParentUnsuppressed(DeliveryTestCase):
    def test_an_inactive_delivery_rejects_and_never_sends(self):
        send = RecordingSend()
        delivery = self.build(send, enabled=False).start()
        self.assertFalse(delivery.event_sink(notice()),
                         "비활성이면 접수를 주장하지 않는다 — 그래야 부모가 억제되지 않는다")
        self.assertEqual(delivery.stats()["rejected_inactive"], 1)
        self.assertEqual(delivery.stats()["accepted"], 0)
        time.sleep(0.05)
        self.assertEqual(send.calls, [])

    def test_a_full_queue_rejects_without_blocking(self):
        release = threading.Event()
        self.addCleanup(release.set)
        send = RecordingSend(block=release)
        delivery = self.build(send, queue_size=1).start()

        self.assertTrue(delivery.event_sink(notice(run_id="in-flight")))
        self.assertTrue(send.entered.wait(DEADLINE))
        self.assertTrue(delivery.event_sink(notice(run_id="fills-queue")))

        started = time.monotonic()
        self.assertFalse(delivery.event_sink(notice(run_id="overflow")))
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5, f"포화 거부도 즉시여야 한다 (실측 {elapsed:.3f}s)")
        self.assertEqual(delivery.stats()["rejected_saturated"], 1)
        self.assertEqual(delivery.stats()["accepted"], 2)

    def test_the_sink_rejects_before_start_and_after_close(self):
        send = RecordingSend()
        delivery = self.build(send)
        self.assertFalse(delivery.event_sink(notice()), "기동 전에는 책임질 수 없다")
        delivery.start()
        self.assertTrue(delivery.event_sink(notice()))
        self.assertTrue(wait_until(lambda: delivery.stats()["sent"] == 1))
        self.assertTrue(delivery.close(DEADLINE))
        self.assertFalse(delivery.event_sink(notice()))
        self.assertEqual(delivery.stats()["rejected_not_running"], 2)


class ShutdownIsBounded(DeliveryTestCase):
    def test_close_reports_that_it_drained(self):
        delivery = self.build(RecordingSend()).start()
        self.assertTrue(delivery.event_sink(notice()))
        self.assertTrue(wait_until(lambda: delivery.stats()["sent"] == 1))
        started = time.monotonic()
        self.assertTrue(delivery.close(DEADLINE))
        self.assertLess(time.monotonic() - started, DEADLINE)
        self.assertFalse(delivery.is_running())

    def test_close_without_start_is_safe(self):
        self.assertTrue(self.build(RecordingSend()).close(DEADLINE))

    def test_a_pending_retry_is_abandoned_at_shutdown(self):
        send = RecordingSend([False] * 10)
        delivery = self.build(send, max_attempts=5, retry_seconds=30).start()
        self.assertTrue(delivery.event_sink(notice(run_id="abandoned")))
        self.assertTrue(send.entered.wait(DEADLINE))

        started = time.monotonic()
        self.assertTrue(delivery.close(DEADLINE), "재시도 대기가 종료를 붙잡으면 안 된다")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0, f"30초 재시도 대기를 즉시 깨워야 한다 (실측 {elapsed:.3f}s)")
        self.assertEqual(delivery.stats()["abandoned_at_shutdown"], 1)
        self.assertEqual(delivery.unsent()[0]["outcome"], "abandoned_at_shutdown")

    def test_queued_notices_left_at_shutdown_are_recorded_not_silently_lost(self):
        # ⛔ 발송을 막지 않은 채 close() 하면 소비자가 두 번째 경보를 정상 발송하는 **합법적
        #    실행 순서**가 있어 결과가 스케줄링에 따라 뒤집힌다(실측). 순서를 확정한다.
        release = threading.Event()
        self.addCleanup(release.set)
        send = RecordingSend(block=release)
        delivery = self.build(send, queue_size=4).start()
        self.assertTrue(delivery.event_sink(notice(run_id="in-flight")))
        self.assertTrue(send.entered.wait(DEADLINE))
        self.assertTrue(delivery.event_sink(notice(run_id="left-behind")))

        self.assertFalse(delivery.close(0.2), "발송이 막혀 있으면 종료가 끝나지 않는다")
        release.set()
        self.assertTrue(delivery.close(DEADLINE))
        self.assertEqual(delivery.stats()["dropped_at_shutdown"], 1)
        self.assertIn("left-behind", [r["run_id"] for r in delivery.unsent()])


class TheModuleCannotRestartTheCrawler(unittest.TestCase):
    """경보 실패가 크롤러·Chrome 재실행으로 번지지 않는다 — 수단 자체가 없다."""

    FORBIDDEN = ("subprocess", "Popen", "crawlers", "selenium", "webdriver",
                 "capture_ibk", "execute_with_timeout", "scheduler")

    def test_the_module_holds_no_process_control_surface(self):
        source = pathlib.Path("app/ibk_notice_delivery.py").read_text()
        found = [token for token in self.FORBIDDEN if token in source]
        self.assertEqual(found, [], f"프로세스 제어 표면이 생겼다: {found}")

    def test_a_final_failure_touches_only_counters_and_records(self):
        delivery = IbkNoticeDelivery(send=lambda _: False, max_attempts=1,
                                     retry_seconds=0, poll_seconds=0.01)
        self.addCleanup(delivery.close, DEADLINE)
        delivery.start()
        self.assertTrue(delivery.event_sink(notice(run_id="final")))
        self.assertTrue(wait_until(lambda: delivery.stats()["send_failed_final"] == 1))
        self.assertEqual(delivery.stats()["accepted"], 1)
        self.assertTrue(delivery.is_running(), "실패 뒤에도 소비자는 그대로 돌 뿐이다")


class EveryAcceptedNoticeIsAccountedFor(DeliveryTestCase):
    """접수한 경보는 반드시 하나의 종말 상태로 귀결된다 — 조용히 사라지는 경로가 없다."""

    TERMINAL = ("sent", "send_failed_final", "abandoned_at_shutdown", "dropped_at_shutdown")

    def assert_balanced(self, delivery):
        stats = delivery.stats()
        self.assertFalse(stats["running"], "종료가 끝난 뒤에만 계수가 안정된다")
        self.assertEqual(stats["accepted"], sum(stats[k] for k in self.TERMINAL),
                         f"접수와 종말 상태가 어긋난다: {stats}")

    def test_a_close_during_acceptance_does_not_swallow_the_notice(self):
        # ⛔ 실행 확인과 enqueue 사이에 close() 가 완주하면, 소비자 없는 큐에 넣고 True 를
        #    돌려주는 경합이 있었다(실측: accepted=1 / queued=1 / 발송·기록 0).
        delivery = self.build(RecordingSend())
        delivery.start()
        gate, resumed = threading.Event(), threading.Event()
        original = delivery.is_running

        def gated():
            allowed = original()
            if allowed and not gate.is_set():
                gate.set()
                resumed.wait(DEADLINE)
            return allowed

        delivery.is_running = gated
        outcome = {}
        worker = threading.Thread(
            target=lambda: outcome.update(accepted=delivery.event_sink(notice(run_id="raced"))))
        worker.start()
        self.assertTrue(gate.wait(DEADLINE))

        closing = threading.Event()

        def close_now():
            closing.set()
            outcome.update(closed=delivery.close(DEADLINE))

        closer = threading.Thread(target=close_now)
        closer.start()
        # closing 은 종료 스레드가 실행됐다는 관측이고, 이어지는 짧은 대기는 그 스레드가
        # 락까지 도달하도록 밀어 주는 것뿐이다 — **순서의 증명이 아니다.** 그래서 아래 단언은
        # 두 결과를 모두 허용하고, 어느 쪽이든 "조용히 사라지는 경보"가 없음을 요구한다.
        self.assertTrue(closing.wait(DEADLINE))
        time.sleep(0.1)
        resumed.set()
        worker.join(DEADLINE)
        closer.join(DEADLINE)

        self.assertTrue(outcome.get("closed"))
        if outcome.get("accepted"):
            self.assert_balanced(delivery)
        else:
            self.assertEqual(delivery.stats()["rejected_not_running"], 1)
            self.assertEqual(delivery.stats()["queued"], 0)

    def test_the_ledger_balances_across_success_failure_and_shutdown(self):
        send = RecordingSend([True, False, False, False])
        delivery = self.build(send, max_attempts=3, queue_size=8)
        delivery.start()
        for run_id in ("ok", "doomed"):
            self.assertTrue(delivery.event_sink(notice(run_id=run_id)))
        self.assertTrue(wait_until(lambda: delivery.stats()["send_failed_final"] == 1))
        self.assertTrue(delivery.close(DEADLINE))
        self.assert_balanced(delivery)

    def test_a_formatter_failure_is_a_terminal_outcome_not_a_dead_consumer(self):
        send = RecordingSend()
        delivery = self.build(send, formatter=lambda _: 1 / 0)
        delivery.start()
        self.assertTrue(delivery.event_sink(notice(run_id="unformattable")))
        self.assertTrue(wait_until(lambda: delivery.stats()["send_failed_final"] == 1))
        self.assertTrue(delivery.is_running(), "formatter 예외가 소비자를 죽이면 안 된다")
        self.assertEqual(send.calls, [], "형식화에 실패했으면 보내지 않는다")
        self.assertIn("ZeroDivisionError", delivery.unsent()[0]["outcome"])
        self.assertTrue(delivery.close(DEADLINE))
        self.assert_balanced(delivery)


class ThePlainTextContract(unittest.TestCase):
    """경보 본문은 서식 없이 나가야 한다 — Markdown 이면 재시도해도 계속 실패한다."""

    def test_the_alert_body_contains_an_odd_number_of_underscores(self):
        body = format_notice(notice(reason="SELENIUM_STRICT_REJECTED"))
        self.assertEqual(body.count("_") % 2, 1,
                         "홀수 밑줄 — Markdown 이면 닫히지 않는 서식이 된다")

    def test_the_default_builder_sends_without_markdown_parsing(self):
        from app.notifications import telegram

        payloads = []

        class Accepted:
            def raise_for_status(self):
                pass

        def fake_post(url, json=None, timeout=None):
            payloads.append(json)
            return Accepted()

        handler = telegram.telegram_handler
        with patch.object(handler, "enabled", True), \
             patch.object(handler, "bot_token", "1:x"), \
             patch.object(handler, "chat_id", "5"), \
             patch.object(telegram.requests, "post", fake_post):
            delivery = build_default_delivery(poll_seconds=0.01, retry_seconds=0)
            self.addCleanup(delivery.close, DEADLINE)
            delivery.start()
            self.assertTrue(delivery.event_sink(notice(reason="SELENIUM_STRICT_REJECTED")))
            self.assertTrue(wait_until(lambda: delivery.stats()["sent"] == 1))

        self.assertEqual(len(payloads), 1)
        self.assertFalse(payloads[0]["parse_mode"], "서식 해석을 켜면 안 된다")
        self.assertIn("SELENIUM_STRICT_REJECTED", payloads[0]["text"],
                      "본문은 이스케이프 없이 그대로 간다")


class MessageFormatting(unittest.TestCase):
    def test_an_attention_notice_carries_its_identifiers(self):
        text = format_notice(notice(run_id="r-9", classification="FAILED", reason="DB_ERROR"))
        self.assertIn("🚨 IBK 크롤러 경보", text)
        for token in ("r-9", "FAILED", "DB_ERROR"):
            self.assertIn(token, text)

    def test_a_recovered_notice_is_titled_differently(self):
        self.assertIn("복구", format_notice(notice(kind="recovered", reason=None)))

    def test_a_cleanup_block_is_surfaced(self):
        block = IbkCleanupBlock("r-1", "2026-09-09T00:00:00+00:00", "CANCELLED_WITHOUT_RECEIPT")
        text = format_notice(notice(cleanup_block=block))
        self.assertIn("CANCELLED_WITHOUT_RECEIPT", text)
        self.assertIn("정리 보류", text)

    def test_a_missing_reason_is_omitted_rather_than_printed_as_none(self):
        self.assertNotIn("None", format_notice(notice(reason=None)))


class DependencyValidation(unittest.TestCase):
    def test_bad_dependencies_are_rejected_at_construction(self):
        for kwargs in ({"send": None}, {"send": lambda _: True, "formatter": "x"},
                       {"send": lambda _: True, "enabled": 1},
                       {"send": lambda _: True, "queue_size": 0},
                       {"send": lambda _: True, "queue_size": True},
                       {"send": lambda _: True, "max_attempts": 0},
                       {"send": lambda _: True, "retry_seconds": -1},
                       {"send": lambda _: True, "poll_seconds": -1},
                       {"send": lambda _: True, "retry_seconds": float("nan")},
                       {"send": lambda _: True, "poll_seconds": float("inf")}):
            with self.subTest(kwargs=sorted(kwargs)):
                with self.assertRaises(ValueError):
                    IbkNoticeDelivery(**kwargs)

    def test_starting_twice_is_refused(self):
        delivery = IbkNoticeDelivery(send=lambda _: True, poll_seconds=0.01)
        self.addCleanup(delivery.close, DEADLINE)
        delivery.start()
        with self.assertRaises(RuntimeError):
            delivery.start()

    def test_the_default_builder_takes_enabled_from_telegram_config(self):
        from app.notifications import telegram
        with patch.object(telegram.telegram_handler, "enabled", False):
            delivery = build_default_delivery(poll_seconds=0.01)
        self.addCleanup(delivery.close, DEADLINE)
        self.assertFalse(delivery.enabled)
        self.assertFalse(delivery.event_sink(notice()))
        self.assertEqual(delivery.stats()["rejected_inactive"], 1)


class TheWiringIsGatedAndOwnedInOnePlace(unittest.TestCase):
    """배선됐다. 이 클래스가 잠그는 것은 **어디서 소유하는가**와 **꺼져 있을 때 무엇도 만들지
    않는가**다.

    ⛔ 참조가 여러 곳으로 퍼지면 lifecycle 주인이 흐려진다 — 누가 `close()` 를 부르는지,
       헬스체크 재시작이 같은 객체를 쓰는지가 코드에서 안 보이게 된다.
    """

    # `scripts` 도 함께 본다 — 부모가 실제로 띄우는 진입점(ibk_subprocess_bootstrap.py)이
    # 거기 있어서 배선이 app 밖에서 일어날 수 있다. 한쪽만 잠그면 보장이 산문보다 좁아진다.
    ROOTS = ("app", "scripts")
    OWNER = "app/scheduler.py"

    def test_only_the_owner_module_references_the_delivery(self):
        searched, callers = [], []
        for root in self.ROOTS:
            for path in sorted(pathlib.Path(root).rglob("*.py")):
                searched.append(path)
                if path.name == "ibk_notice_delivery.py":
                    continue
                if "ibk_notice_delivery" in path.read_text():
                    callers.append(str(path))
        # 양성 대조 — 검색이 실제로 파일을 훑었는지. 경로가 어긋나면 공허하게 통과한다.
        for root in self.ROOTS:
            self.assertTrue(any(p.parts[0] == root for p in searched), f"{root} 를 훑지 못했다")
        self.assertEqual(callers, [self.OWNER],
                         f"소유는 한 곳이어야 한다 — 지금 참조하는 곳: {callers}")


class TheGateDecidesWhetherAnythingIsBuilt(unittest.TestCase):
    """⛔ 꺼져 있으면 **객체도 소비자 스레드도 만들지 않는다.** "만들어 두고 안 쓴다" 는
    배포가 곧 동작 변화가 되지 않는다는 약속을 깨뜨린다."""

    def setUp(self):
        from app import scheduler

        self.scheduler = scheduler
        self.addCleanup(setattr, scheduler, "_ibk_parent", scheduler._ibk_parent)
        self.addCleanup(setattr, scheduler, "_ibk_notice_delivery",
                        scheduler._ibk_notice_delivery)
        scheduler._ibk_parent = None
        scheduler._ibk_notice_delivery = None

    def test_an_off_gate_builds_nothing(self):
        from app import config, ibk_notice_delivery

        builder = MagicMock()
        with patch.object(config, "IBK_RESULT_PATH_ENABLED", False), \
             patch.object(ibk_notice_delivery, "build_default_delivery", builder):
            self.assertFalse(self.scheduler.start_ibk_result_path())
        builder.assert_not_called()
        self.assertIsNone(self.scheduler._ibk_parent)
        self.assertIsNone(self.scheduler._ibk_notice_delivery)
        # ⛔ `wired` 는 부모 객체 유무다. worker 생존이 아니다 — 이름을 `running` 으로 두면
        #    운영자가 "돌고 있다" 로 읽는다.
        self.assertEqual(self.scheduler.ibk_result_path_snapshot(),
                         {"enabled": False, "wired": False})

    def test_an_on_gate_builds_once_and_keeps_the_same_parent(self):
        """⛔ 두 번째 호출이 새 부모를 만들면 헬스체크 재시작에서 `cleanup_blocked` 와 경보
        억제 상태가 사라진다."""
        from app import config, ibk_notice_delivery

        # ⛔ 여기서 `start()` 하지 않는다 — lifecycle 이 시작하는 것이 계약이고, 미리 띄우면
        #    두 번 시작해 `RuntimeError` 가 난다(실측).
        delivery = IbkNoticeDelivery(send=lambda text: True)
        self.addCleanup(delivery.close, 2.0)
        builder = MagicMock(return_value=delivery)
        with patch.object(config, "IBK_RESULT_PATH_ENABLED", True), \
             patch.object(ibk_notice_delivery, "build_default_delivery", builder):
            self.assertTrue(self.scheduler.start_ibk_result_path())
            first = self.scheduler._ibk_parent
            self.assertTrue(self.scheduler.start_ibk_result_path())

        self.assertEqual(builder.call_count, 1, "두 번 만들면 안 된다")
        self.assertIs(self.scheduler._ibk_parent, first, "같은 부모를 유지해야 한다")
        snapshot = self.scheduler.ibk_result_path_snapshot()
        self.assertTrue(snapshot["wired"], "부모 객체가 있다")
        self.assertIn("parent", snapshot)
        # 소비자 스레드 생존은 `notice.running` 이 낸다 — `wired` 와 다른 사실이다.
        self.assertTrue(snapshot["notice"]["running"])
        self.assertIn("enabled", snapshot["notice"])

    def test_an_assembly_failure_does_not_leave_a_consumer_thread(self):
        """⛔ 조립에 실패하면 소비자 스레드만 남기고 죽으면 안 된다. 그리고 조용히 legacy 로
        돌아가지도 않는다 — 예외를 올린다."""
        from app import config, ibk_notice_delivery
        from app import ibk_parent_runner

        delivery = IbkNoticeDelivery(send=lambda text: True)
        self.addCleanup(delivery.close, 2.0)
        with patch.object(config, "IBK_RESULT_PATH_ENABLED", True), \
             patch.object(ibk_notice_delivery, "build_default_delivery",
                          MagicMock(return_value=delivery)), \
             patch.object(ibk_parent_runner, "IbkParentRunner",
                          MagicMock(side_effect=RuntimeError("PRIVATE_ASSEMBLY"))):
            with self.assertRaises(RuntimeError):
                self.scheduler.start_ibk_result_path()

        self.assertFalse(delivery.is_running(), "띄운 소비자 스레드를 정리해야 한다")
        self.assertIsNone(self.scheduler._ibk_parent)


class ShutdownAlwaysLeavesARecord(unittest.TestCase):
    """⛔ 소비자가 **정상 종료하면서도** 남은 큐를 발송 없이 폐기할 수 있다
    (`dropped_at_shutdown`). `close()` 가 True 라고 조용히 넘기면 그 기록이 참조가 사라진
    객체에만 남아 아무도 못 본다 — 종료 결과는 **스레드 종료 여부와 독립적으로** 남긴다."""

    def setUp(self):
        from app import scheduler

        self.scheduler = scheduler
        self.addCleanup(setattr, scheduler, "_ibk_parent", scheduler._ibk_parent)
        self.addCleanup(setattr, scheduler, "_ibk_notice_delivery",
                        scheduler._ibk_notice_delivery)

    def _shutdown_with(self, delivery):
        import asyncio

        self.scheduler._ibk_notice_delivery = delivery
        self.scheduler._ibk_parent = object()
        with self.assertLogs("exchange_rate.scheduler", level="INFO") as captured:
            asyncio.run(self.scheduler.shutdown_ibk_result_path())
        return captured.records

    def test_a_clean_stop_that_dropped_work_is_still_recorded(self):
        """⛔ 폐기는 `_stop` 이 선 **뒤 남은 큐**에서만 일어난다. 발송이 빠르면 소비자가
        먼저 다 보내 버려 이 창이 안 열린다(실측: 폐기 0). 한 건을 붙잡아 둔다."""
        import time as _time

        def slow_send(text):
            _time.sleep(0.2)
            return True

        delivery = IbkNoticeDelivery(send=slow_send, poll_seconds=0.01)
        delivery.start()
        for index in range(2):
            self.assertTrue(delivery.event_sink(notice(run_id=f"drop{index}")))
        records = self._shutdown_with(delivery)

        shutdown = [r for r in records if r.getMessage() == "IBK_NOTICE_DELIVERY_SHUTDOWN"]
        self.assertEqual(len(shutdown), 1, [r.getMessage() for r in records])
        self.assertTrue(shutdown[0].stopped, "소비자는 정상 종료했다")
        self.assertGreaterEqual(shutdown[0].dropped_at_shutdown, 1,
                                "폐기한 회차를 기록해야 한다")
        self.assertEqual(shutdown[0].levelname, "WARNING",
                         "폐기가 있으면 경고로 남긴다")

    def test_a_quiet_shutdown_is_recorded_too(self):
        """양성 대조 — 폐기가 없어도 종료 결과는 남는다(정보 수준)."""
        delivery = IbkNoticeDelivery(send=lambda text: True)
        delivery.start()
        records = self._shutdown_with(delivery)

        shutdown = [r for r in records if r.getMessage() == "IBK_NOTICE_DELIVERY_SHUTDOWN"]
        self.assertEqual(len(shutdown), 1)
        self.assertEqual(shutdown[0].dropped_at_shutdown, 0)
        self.assertEqual(shutdown[0].levelname, "INFO")


class TheHealthcheckDoesNotResurrectAWorkerDuringShutdown(unittest.TestCase):
    """⛔ 헬스체크가 기존 worker 를 합류시키는 `await` **사이에** 종료가 시작될 수 있다.
    그 뒤에 새 worker 를 만들면 lifespan 이 기다린 객체와 전역 worker 가 달라지고, 새 worker
    가 부모를 들고 남아 다음 lifespan 과 겹친다.

    ⛔ 진입 시점 한 번만 보면 이 창을 못 막는다 — **합류 뒤에 다시** 봐야 한다.
    """

    def setUp(self):
        from app import scheduler

        self.scheduler = scheduler
        for name in ("selenium_worker_task", "selenium_queue",
                     "selenium_worker_current_job", "selenium_worker_last_heartbeat",
                     "_collector_shutdown_initiated"):
            self.addCleanup(setattr, scheduler, name, getattr(scheduler, name))

    def _run(self, *, shutdown_during_await):
        import asyncio
        import time as _time

        scheduler = self.scheduler
        # ⛔ 헬스체크는 재시작 전에 **실제 Chrome 프로세스를 죽인다.** 막지 않으면 시험이
        #    개발자 기계의 브라우저를 종료시킨다(실측: 렌더러 16개 종료).
        killer = patch.object(scheduler, "cleanup_zombie_chrome_processes", MagicMock())
        killer.start()
        self.addCleanup(killer.stop)

        async def scenario():
            scheduler._collector_shutdown_initiated = False
            scheduler.selenium_queue = asyncio.PriorityQueue(maxsize=1)
            scheduler.selenium_worker_current_job = "ibk"      # 작업 처리 중으로 보이게
            scheduler.selenium_worker_last_heartbeat = _time.time() - 999   # 멈춤 판정

            async def stuck():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    if shutdown_during_await:
                        # ⛔ 합류를 기다리는 **그 사이에** 종료가 시작된 상황.
                        scheduler._collector_shutdown_initiated = True
                    raise

            original = asyncio.get_event_loop().create_task(stuck())
            # ⛔ **한 번 돌려 놓고** 취소해야 한다. 시작 전에 취소하면 코루틴 본문이 아예
            #    실행되지 않아 `except CancelledError` 가 돌지 않는다 — 그러면 이 시험이
            #    노리는 "합류 사이에 종료가 시작되는" 창이 열리지 않는다(실측).
            await asyncio.sleep(0)
            self.assertFalse(original.done(), "양성 대조 — 기존 worker 가 살아 있다")
            scheduler.selenium_worker_task = original
            await scheduler.check_worker_health()
            return original, scheduler.selenium_worker_task

        return asyncio.run(scenario())

    def test_a_shutdown_that_starts_during_the_join_cancels_the_restart(self):
        original, current = self._run(shutdown_during_await=True)
        self.assertIs(current, original, "종료 중에는 새 worker 를 만들지 않는다")

    def test_without_a_shutdown_the_worker_is_restarted(self):
        """양성 대조 — 이게 없으면 위 시험이 '재시작 자체가 없어서' 통과해도 모른다."""
        original, current = self._run(shutdown_during_await=False)
        self.assertIsNot(current, original, "정상 상황에서는 재시작한다")
        current.cancel()


class TheParentSuppressesAfterAcceptance(unittest.TestCase):
    """부모 + 전달 모듈을 함께 돌려 억제 이음매를 실제로 재현한다."""

    @staticmethod
    def _capture(status):
        async def capture(argv, **kwargs):
            context = IbkRunContext.from_arguments(argv[3:])
            result = IbkResult(
                1, context.run_id, status,
                {IbkStatus.OBSERVED: IbkReason.NORMAL,
                 IbkStatus.DEGRADED: IbkReason.SELENIUM_STRICT_REJECTED}[status],
                context.reference_time.isoformat(), context.expected_service_date,
                context.expected_service_date if status is IbkStatus.OBSERVED else None,
                context.reference_time.isoformat() if status is IbkStatus.OBSERVED else None,
                IbkSource.OFFICIAL_POST if status is IbkStatus.OBSERVED else IbkSource.DB_SNAPSHOT,
                tuple(sorted(PAIRS)) if status is IbkStatus.OBSERVED else (),
                tuple(sorted(PAIRS)), (), None, None,
            )
            stdout = encode_ibk_result(result)
            return IbkProcessCapture(0, stdout, b"", len(stdout), 0, False, False, None)
        return capture

    def test_the_second_failure_is_suppressed_so_delivery_owns_the_final_record(self):
        # 발송을 막아 **억제가 확정된 뒤에** 발송이 실패하는 시간 순서를 강제한다.
        # 막지 않으면 "이미 실패가 끝난 상태를 관측"하는 테스트가 될 수 있다.
        release = threading.Event()
        self.addCleanup(release.set)
        send = RecordingSend([False, False, False], block=release)
        delivery = IbkNoticeDelivery(send=send, max_attempts=3, retry_seconds=0,
                                     poll_seconds=0.01)
        self.addCleanup(delivery.close, DEADLINE)
        delivery.start()

        async def scenario():
            parent = IbkParentRunner(capture=self._capture(IbkStatus.DEGRADED),
                                     event_sink=delivery.event_sink)
            for _ in range(2):
                await parent.execute()
            return parent

        parent = asyncio.run(scenario())

        self.assertEqual(parent.notice_enqueued, 1)
        self.assertEqual(parent.notice_suppressed, 1,
                         "접수 성공이 두 번째 회차를 억제한다 — 이것이 이음매다")
        self.assertEqual(delivery.stats()["send_failed_final"], 0,
                         "여기까지 발송은 아직 끝나지 않았다 — 억제가 먼저다")
        release.set()
        self.assertTrue(wait_until(lambda: delivery.stats()["send_failed_final"] == 1))
        self.assertEqual(delivery.stats()["sent"], 0)
        self.assertEqual(len(delivery.unsent()), 1,
                         "부모가 억제한 문제의 발송 실패는 여기서만 드러난다")

    def test_a_rejecting_delivery_leaves_the_parent_unsuppressed(self):
        delivery = IbkNoticeDelivery(send=lambda _: True, enabled=False, poll_seconds=0.01)
        self.addCleanup(delivery.close, DEADLINE)
        delivery.start()

        async def scenario():
            parent = IbkParentRunner(capture=self._capture(IbkStatus.DEGRADED),
                                     event_sink=delivery.event_sink)
            for _ in range(2):
                decision = await parent.execute()
                self.assertFalse(decision.should_retry, "경보 실패는 크롤러를 다시 돌리지 않는다")
            return parent

        parent = asyncio.run(scenario())
        self.assertEqual(parent.notice_enqueued, 0)
        self.assertEqual(parent.notice_enqueue_failed, 2)
        self.assertEqual(parent.notice_suppressed, 0, "거부는 억제를 만들지 않는다")


class TelegramFailureLogging(unittest.TestCase):
    """발송 실패 로그가 봇 토큰을 남기지 않는지 — 이 모듈이 그 경로를 처음 켠다."""

    TOKEN = "111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    def test_a_request_failure_logs_only_the_exception_type(self):
        import requests
        from app.notifications import telegram

        handler = telegram.TelegramHandler()
        handler.enabled, handler.bot_token, handler.chat_id = True, self.TOKEN, "5"
        url = f"https://api.telegram.org/bot{self.TOKEN}/sendMessage"
        boom = requests.exceptions.ConnectionError(f"Max retries exceeded with url: {url}")

        with patch.object(telegram.requests, "post", side_effect=boom):
            with self.assertLogs("exchange_rate.utils.telegram", level="ERROR") as captured:
                self.assertFalse(handler.send_message("hi"))

        # ⛔ 검사 축을 좁히면 `error_type` 은 유지한 채 다른 extra 키로 원문을 다시 흘리는
        #    변이가 살아남는다(실측). 레코드 전체 속성과 traceback 을 본다.
        records = "\n".join(captured.output)
        for record in captured.records:
            records += "\n" + str(record.__dict__)
            if record.exc_info:
                records += "\n" + logging.Formatter().formatException(record.exc_info)
        self.assertIn("ConnectionError", records, "양성 대조 — 검사 대상에 실제 내용이 있어야 한다")
        self.assertNotIn(self.TOKEN, records, "봇 토큰이 로그로 새면 안 된다")


if __name__ == "__main__":
    unittest.main()
