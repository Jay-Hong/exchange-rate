"""`run_ibk_dated_result` — 세션 수명만 책임지는 wrapper 의 회귀 잠금.

이 wrapper 가 하는 일은 하나다: `crawler(context)` 1-인자 계약에 맞춰 DB 세션을 만들고
정리한다. 여기 시험은 그 하나가 **모든 종료 경로에서** 지켜지는지, 그리고 결과의 의미와
runner 의 기존 기술 오류 계약을 바꾸지 않는지를 잠근다.

실제 HTTP·Redis·FCM 은 전부 차단한다 — 생성기 자체는 대역으로 세우고 세션 축만 본다.
"""

import datetime
import unittest
from unittest.mock import patch

from app.crawlers import ibk
from app.ibk_result_builder import (
    IbkDbSnapshot,
    IbkIntendedState,
    IbkPreservationGrant,
    IbkRetentionReason,
    build_ibk_result,
)
from app.ibk_result_protocol import IbkReason, encode_ibk_result
from app.ibk_run_context import IbkRunContext

RUN_ID = "b" * 32
REFERENCE = datetime.datetime(2026, 8, 28, 6, 5, tzinfo=datetime.timezone.utc)
CONTEXT = IbkRunContext(RUN_ID, REFERENCE)
PAIRS = ("usd-krw", "jpy-krw", "eur-krw")
RATES = {"usd-krw": 1382.0, "jpy-krw": 867.06, "eur-krw": 1610.31}


def _result(context):
    """프레임은 **실제 builder 로** 만든다 — 손으로 쓰면 계약과 어긋난 것을 못 본다.

    직접 IbkResult 를 조립했을 때 observed_at 을 datetime 으로 넣어 codec 이 거부하는
    프레임을 '유효' 라고 부르고 있었다(INVALID_RESULT_OBJECT). source·observed_pairs 도
    실제 무세션 보존 분기와 달랐다. builder 를 거치면 그 어긋남이 구성으로 배제된다.
    """
    return build_ibk_result(
        run_id=context.run_id,
        expected_service_date=datetime.date.fromisoformat(context.expected_service_date),
        observed_at=REFERENCE,
        db_after=IbkDbSnapshot(checked=True, rates=dict(RATES), usable=PAIRS, missing=()),
        intended=IbkIntendedState(
            submitted={},
            retained=dict(RATES),
            retention={pair: IbkRetentionReason.NOT_OBSERVED for pair in PAIRS},
            unavailable=(),
        ),
        preservation=IbkPreservationGrant(IbkReason.OFFICIAL_NO_SESSION),
    )


class SpySession:
    def __init__(self, *, close_raises=False, close_raises_base=False):
        self.closed = 0
        self._close_raises = close_raises
        self._close_raises_base = close_raises_base

    def close(self):
        self.closed += 1
        if self._close_raises_base:
            raise KeyboardInterrupt
        if self._close_raises:
            raise RuntimeError("CLOSE_BOOM")


class SessionWrapperTest(unittest.TestCase):
    def _run(self, session, produce):
        with patch.object(ibk, "SessionLocal", return_value=session), \
             patch.object(ibk, "produce_ibk_dated_result", side_effect=produce) as spy:
            return ibk.run_ibk_dated_result(CONTEXT), spy

    def test_returns_the_generator_result_unchanged_and_closes(self):
        """결과를 그대로 통과시킨다 — 의미를 만들지도 바꾸지도 않는다."""
        session = SpySession()
        expected = _result(CONTEXT)
        result, spy = self._run(session, lambda db, ctx, **kw: expected)

        self.assertIs(result, expected, "wrapper 가 결과를 재구성하면 판정이 위조된다")
        self.assertEqual(session.closed, 1)
        self.assertIs(spy.call_args.args[0], session, "생성기는 wrapper 가 연 세션을 받아야 한다")
        self.assertIs(spy.call_args.args[1], CONTEXT)

    def test_passes_through_timeout_and_now(self):
        """주입점(timeout·now)을 삼키지 않는다 — 시험 가능성이 여기에 걸려 있다."""
        session = SpySession()
        seen = {}

        def produce(db, ctx, *, timeout=None, now=None):
            seen.update(timeout=timeout, now=now)
            return _result(ctx)

        with patch.object(ibk, "SessionLocal", return_value=session), \
             patch.object(ibk, "produce_ibk_dated_result", side_effect=produce):
            ibk.run_ibk_dated_result(CONTEXT, timeout=3, now=REFERENCE)

        self.assertEqual(seen, {"timeout": 3, "now": REFERENCE})

    def test_generator_exception_propagates_and_still_closes(self):
        """예상 밖 예외를 가짜 FAILED 로 포장하지 않는다 — runner 의 exit 1 계약을 그대로 쓴다."""
        session = SpySession()

        def boom(db, ctx, **kw):
            raise ValueError("GENERATOR_BOOM")

        with patch.object(ibk, "SessionLocal", return_value=session), \
             patch.object(ibk, "produce_ibk_dated_result", side_effect=boom):
            with self.assertRaises(ValueError) as caught:
                ibk.run_ibk_dated_result(CONTEXT)

        self.assertEqual(str(caught.exception), "GENERATOR_BOOM")
        self.assertEqual(session.closed, 1, "예외 경로에서도 세션은 닫혀야 한다")

    def test_session_creation_failure_propagates_and_does_not_call_the_generator(self):
        """세션을 못 열면 생성기를 부르지 않는다 — 세션 없이 호출하면 TypeError 가 난다."""
        with patch.object(ibk, "SessionLocal", side_effect=RuntimeError("NO_SESSION")), \
             patch.object(ibk, "produce_ibk_dated_result") as spy:
            with self.assertRaises(RuntimeError) as caught:
                ibk.run_ibk_dated_result(CONTEXT)

        self.assertEqual(str(caught.exception), "NO_SESSION")
        spy.assert_not_called()

    def test_close_failure_does_not_discard_a_valid_result(self):
        """정리 실패가 결과를 가리면, 이미 일어난 관측과 DB 반영을 버리고 재시도하게 된다."""
        session = SpySession(close_raises=True)
        expected = _result(CONTEXT)
        result, _ = self._run(session, lambda db, ctx, **kw: expected)

        self.assertIs(result, expected)
        self.assertEqual(session.closed, 1)

    def test_close_failure_does_not_mask_the_original_exception(self):
        """정리 실패가 최초 원인을 덮으면 진단이 CLOSE_BOOM 으로 오도된다."""
        session = SpySession(close_raises=True)

        def boom(db, ctx, **kw):
            raise ValueError("GENERATOR_BOOM")

        with patch.object(ibk, "SessionLocal", return_value=session), \
             patch.object(ibk, "produce_ibk_dated_result", side_effect=boom):
            with self.assertRaises(ValueError) as caught:
                ibk.run_ibk_dated_result(CONTEXT)

        self.assertEqual(str(caught.exception), "GENERATOR_BOOM")

    def test_wrapper_matches_the_runner_one_argument_contract(self):
        """runner 는 `crawler(context)` 로 부른다 — 위치 인자 1개로 호출 가능해야 한다."""
        import inspect

        sig = inspect.signature(ibk.run_ibk_dated_result)
        positional = [
            p for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        self.assertEqual([p.name for p in positional], ["context"])
        self.assertTrue(
            all(p.default is not p.empty for p in sig.parameters.values() if p.kind is p.KEYWORD_ONLY),
            "키워드 인자에 기본값이 없으면 runner 의 1-인자 호출이 깨진다",
        )

    def test_a_base_exception_from_close_is_not_absorbed(self):
        """정리 비차폐의 **경계**를 박제한다 — 가드는 `except Exception` 이라 여기까지다.

        KeyboardInterrupt·SystemExit 이 close 에서 나오면 결과도 최초 원인도 가려진다.
        BaseException 을 삼키면 프로세스 종료 신호를 먹으므로 넓히지 않는다. 대신 "정리
        실패는 아무것도 가리지 않는다" 를 무조건적으로 주장하지 않기 위해 여기 남긴다.
        """
        session = SpySession(close_raises_base=True)
        with patch.object(ibk, "SessionLocal", return_value=session), \
             patch.object(ibk, "produce_ibk_dated_result", side_effect=lambda db, ctx, **kw: _result(ctx)):
            with self.assertRaises(KeyboardInterrupt):
                ibk.run_ibk_dated_result(CONTEXT)

        self.assertEqual(session.closed, 1)

    def test_the_fixture_is_a_frame_the_real_codec_accepts(self):
        """fixture 가 실제 계약을 통과하는지 여기서 강제한다 — 대역이 계약과 갈라지면 위 시험이 공허해진다."""
        encode_ibk_result(_result(CONTEXT))

    def test_operational_binding_is_still_off(self):
        """이번 단계는 미연결이다 — 바인딩은 별도 단계이며 여기서 조용히 켜지면 안 된다."""
        from app.crawlers import runner

        self.assertIsNone(runner.IBK_RESULT_CRAWLER)

    def test_the_wrapper_has_no_direct_operational_reference(self):
        """'호출자 0' 을 검색 결과가 아니라 **잠금**으로 만든다 — 다만 잠그는 범위는 유한하다.

        탐지하는 것: app/·scripts/ 안의 직접 Name·Attribute·import 참조.
        ⛔ 탐지하지 못하는 것: `getattr(ibk, "run_ibk_dated_result")`, `vars(ibk)[...]`,
           `importlib.import_module(...)` 같은 **동적 참조**(실제로 실행해 미탐지 확인).
           이 시험을 "어떤 연결도 불가능" 으로 읽으면 안 된다 — 통상적인 바인딩을 잡을 뿐이다.
        바인딩은 별도 단계에서 이 시험을 함께 고치며 이뤄진다.
        """
        import ast
        import pathlib

        repo = pathlib.Path(ibk.__file__).resolve().parents[2]
        name = "run_ibk_dated_result"
        references = []
        for root in ("app", "scripts"):
            for path in sorted((repo / root).rglob("*.py")):
                tree = ast.parse(path.read_text(), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name == name:
                        continue
                    hit = (
                        (isinstance(node, ast.Name) and node.id == name)
                        or (isinstance(node, ast.Attribute) and node.attr == name)
                        or (isinstance(node, ast.alias) and node.name == name)
                    )
                    if hit:
                        references.append(f"{path.relative_to(repo)}:{node.lineno}")

        self.assertEqual(references, [], f"운영 호출자가 생겼다: {references}")


if __name__ == "__main__":
    unittest.main()
