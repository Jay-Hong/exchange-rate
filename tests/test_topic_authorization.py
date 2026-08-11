"""KRX per-user 인가 판정기 (`app/topic_authorization.py`).

⛔ 이 모듈은 한때 **테스트 0건**으로 추가됐다 — 그 상태로 dispatcher 에 배선하면
판정 로직이 검증 없이 보안 경계에 들어간다.
"""
import ast
import pathlib
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import exc as sqlalchemy_exc

from app import subscription, topic_authorization as ta

REPO = pathlib.Path(__file__).resolve().parent.parent


class _Clock:
    """호출마다 전진하는 mono — 어느 시점에 찍혔는지 값으로 구분한다.

    ⛔ **fake I/O 가 이 시계를 전진시켜야** "관측을 I/O **직전**에 찍는다"가 관측 가능해진다.
    fake 가 시간을 안 쓰면 timestamp 를 완료 **뒤**로 옮겨도 같은 값이 나와 변이가 생존한다
    (실측으로 겪었다 — 두 시각이 *다르다*는 것만 보면 순서를 못 잠근다).
    """

    def __init__(self, start=1000.0, step=1.0):
        self.t, self.step, self.reads = start, step, []

    def __call__(self):
        self.reads.append(self.t)
        value, self.t = self.t, self.t + self.step
        return value

    def advance(self, seconds):
        """fake I/O 의 **지연**. 이 만큼은 lease 지평에 들어가면 안 된다."""
        self.t += seconds


class TestClassifyPremium(unittest.TestCase):
    def test_determined_true_yields_only_the_intermediate_type(self):
        """⛔ **entitlement 를 관측하기 전에 최종 `Granted` 를 만들지 않는다.**

        만들면 관측하지 않은 entitlement 시각을 **지어내게** 된다 — 이 리포가 두 번 고친
        "다른 축 인자에 값 밀어 넣기"와 같은 형태다.
        """
        verdict = ta.classify_premium(subscription.Determined(is_premium=True),
                                      premium_observed_at_mono=100.0)
        self.assertIsInstance(verdict, ta.PremiumGranted)
        self.assertNotIsInstance(verdict, ta.Granted)
        self.assertEqual(verdict.premium_observed_at_mono, 100.0)

    def test_determined_false_is_a_per_topic_denial(self):
        v = ta.classify_premium(subscription.Determined(is_premium=False),
                                premium_observed_at_mono=100.0)
        self.assertEqual(v, ta.Denied("premium_required"))

    def test_provider_results_split_transient_from_persistent(self):
        cases = (
            (subscription.ProviderUnavailable(), ta.UnavailableKind.TRANSIENT),
            (subscription.ProviderMisconfigured(), ta.UnavailableKind.PERSISTENT),
            (subscription.BadRequest(), ta.UnavailableKind.PERSISTENT),
            (subscription.ProtocolViolation(detail="x"), ta.UnavailableKind.PERSISTENT),
        )
        for result, kind in cases:
            with self.subTest(result=type(result).__name__):
                v = ta.classify_premium(result, premium_observed_at_mono=1.0)
                self.assertIsInstance(v, ta.Unavailable)
                self.assertIs(v.kind, kind)

    def test_unknown_result_type_is_not_swallowed(self):
        """⛔ 분류 불가는 삼키지 않는다 — 미지의 결과를 가용성 verdict 로 바꾸면 조용해진다."""
        with self.assertRaises(TypeError):
            ta.classify_premium(object(), premium_observed_at_mono=1.0)

    def test_denial_vocabulary_is_enforced_at_construction(self):
        for bad in ("typo", "", "topic_unavailable", "invalid_token"):
            with self.subTest(error=bad), self.assertRaises(ValueError):
                ta.Denied(bad)


class TestAuthorizeGatedSubscription(unittest.IsolatedAsyncioTestCase):
    def _patch_rc(self, result):
        return patch("app.subscription.fetch_revenuecat_result",
                     new=AsyncMock(return_value=result))

    async def test_premium_denial_does_not_touch_the_database(self):
        """⛔ premium 이 없으면 **entitlement 조회 0회** — 없는 권한을 위해 커넥션(3+2)을 쓰지 않는다."""
        session_factory = MagicMock()
        with self._patch_rc(subscription.Determined(is_premium=False)), \
             patch("app.database.SessionLocal", new=session_factory):
            verdict = await ta.authorize_gated_subscription("u1", mono=_Clock())
        self.assertEqual(verdict, ta.Denied("premium_required"))
        session_factory.assert_not_called()

    async def test_observations_are_taken_before_their_io_not_after(self):
        """⛔ 관측 시각은 **I/O 직전**이어야 한다 — 완료 후면 그 **지연만큼 lease 가 연장**된다.

        ⚠️ fake 가 시계를 전진시키지 않으면 이 성질이 **관측되지 않는다**: timestamp 를 뒤로
        옮겨도 같은 값이 나와 변이가 생존한다(실측). 그래서 두 fake 가 각각 지연을 만든다.
        """
        clock = _Clock(start=500.0, step=1.0)
        rc_latency, db_latency = 100.0, 40.0

        async def rc(user_id, **kw):
            clock.advance(rc_latency)
            return subscription.Determined(is_premium=True)

        def query(db, user_id, key):
            clock.advance(db_latency)
            return True

        with patch("app.subscription.fetch_revenuecat_result", new=rc), \
             patch("app.database.SessionLocal", new=MagicMock()), \
             patch("app.entitlements.has_entitlement", new=query):
            verdict = await ta.authorize_gated_subscription("u1", mono=clock)

        self.assertIsInstance(verdict, ta.Granted)
        # premium 은 RC **전**(500) — 뒤라면 600+ 이 된다.
        self.assertEqual(
            verdict.premium_observed_at_mono, 500.0,
            "premium 관측이 RevenueCat 왕복 뒤에 찍혔다 — 그 지연만큼 lease 가 늘어난다",
        )
        # entitlement 는 쿼리 **전**(= RC 지연 뒤 첫 읽기) — 뒤라면 DB 지연이 더해진다.
        self.assertEqual(
            verdict.entitlement_observed_at_mono, 601.0,
            "entitlement 관측이 쿼리 뒤에 찍혔다 — DB 지연만큼 lease 가 늘어난다",
        )

    async def test_missing_entitlement_is_a_per_topic_denial(self):
        with self._patch_rc(subscription.Determined(is_premium=True)), \
             patch("app.database.SessionLocal", new=MagicMock()), \
             patch("app.entitlements.has_entitlement", return_value=False):
            verdict = await ta.authorize_gated_subscription("u1", mono=_Clock())
        self.assertEqual(verdict, ta.Denied("krx_entitlement_required"))

    async def test_transient_db_error_warns_and_is_retryable(self):
        exc = sqlalchemy_exc.OperationalError("stmt", {}, Exception("conn refused"))
        with self._patch_rc(subscription.Determined(is_premium=True)), \
             patch("app.database.SessionLocal", new=MagicMock()), \
             patch("app.entitlements.has_entitlement", side_effect=exc), \
             self.assertLogs("exchange_rate.topic_authorization", level="WARNING") as logs:
            verdict = await ta.authorize_gated_subscription("u1", mono=_Clock())
        self.assertIs(verdict.kind, ta.UnavailableKind.TRANSIENT)
        self.assertFalse([r for r in logs.records if r.levelname == "ERROR"],
                         "transient 인데 ERROR 를 냈다 — 운영자 신호가 희석된다")

    async def test_permanent_db_error_logs_error(self):
        """⚠️ 영구 결함을 WARNING 으로 내면 운영자가 "재시도하면 낫는다"로 읽는다."""
        with self._patch_rc(subscription.Determined(is_premium=True)), \
             patch("app.database.SessionLocal", new=MagicMock()), \
             patch("app.entitlements.has_entitlement",
                   side_effect=sqlalchemy_exc.ProgrammingError("stmt", {}, Exception("no table"))), \
             self.assertLogs("exchange_rate.topic_authorization", level="ERROR"):
            verdict = await ta.authorize_gated_subscription("u1", mono=_Clock())
        self.assertIs(verdict.kind, ta.UnavailableKind.PERSISTENT)

    async def test_permanent_sqlstate_inside_operational_error_is_persistent(self):
        """⛔ 영구 결함이 **`OperationalError` 로 도착**한다(잘못된 비밀번호·권한 등).
        클래스만 보면 transient 로 오분류되어 **재시도로 절대 안 풀리는 문제를 5초마다** 두드린다.
        이 축이 없으면 `is_transient_db_error()` 호출을 지우는 변이가 살아남는다.
        """
        orig = Exception("password authentication failed")
        orig.sqlstate = "28P01"
        exc = sqlalchemy_exc.OperationalError("stmt", {}, orig)
        with self._patch_rc(subscription.Determined(is_premium=True)), \
             patch("app.database.SessionLocal", new=MagicMock()), \
             patch("app.entitlements.has_entitlement", side_effect=exc), \
             self.assertLogs("exchange_rate.topic_authorization", level="ERROR"):
            verdict = await ta.authorize_gated_subscription("u1", mono=_Clock())
        self.assertIs(
            verdict.kind, ta.UnavailableKind.PERSISTENT,
            "영구 SQLSTATE 가 transient 로 분류됐다 — 재시도로 안 낫는 것을 계속 두드린다",
        )

    async def test_programming_errors_are_reraised_not_turned_into_a_verdict(self):
        """⛔ `AttributeError` 같은 **프로그래밍 오류를 가용성 verdict 로 바꾸지 않는다.**

        바꾸면 클라는 "재시도하면 되는 일시 장애"로 읽고 서버는 영영 안 낫는다.
        blast radius(재접속 폭주)는 별도의 운영 방어 문제이지, 버그를 숨길 근거가 아니다.
        """
        with self._patch_rc(subscription.Determined(is_premium=True)), \
             patch("app.database.SessionLocal", new=MagicMock()), \
             patch("app.entitlements.has_entitlement", side_effect=AttributeError("boom")), \
             self.assertRaises(AttributeError):
            await ta.authorize_gated_subscription("u1", mono=_Clock())

    async def test_provider_failure_skips_the_database(self):
        session_factory = MagicMock()
        with self._patch_rc(subscription.ProviderUnavailable()), \
             patch("app.database.SessionLocal", new=session_factory):
            verdict = await ta.authorize_gated_subscription("u1", mono=_Clock())
        self.assertIs(verdict.kind, ta.UnavailableKind.TRANSIENT)
        session_factory.assert_not_called()


class TestGatedSubscriptionCallsRevenueCatOnce(unittest.IsolatedAsyncioTestCase):
    """추출 characterization — RC 왕복이 요청당 **정확히 1회**여야 한다.

    ⛔ 추출은 helper 를 한 번 더 부르는 실수를 쉽게 만든다. 외부 HTTP 가 2회면 지연이
       두 배가 되고 **관측 시각이 둘로 갈려** lease horizon 이 topic 별로 어긋난다.
    """

    async def test_exactly_one_roundtrip_per_authorization(self):
        rc = AsyncMock(return_value=subscription.Determined(is_premium=True))
        with patch("app.subscription.fetch_revenuecat_result", new=rc), \
             patch("app.database.SessionLocal", new=MagicMock()), \
             patch("app.entitlements.has_entitlement", return_value=True):
            await ta.authorize_gated_subscription("u1", mono=_Clock())
        self.assertEqual(rc.await_count, 1, "RevenueCat 왕복이 1회가 아니다")



class TestSingleGatedTopicTripwire(unittest.TestCase):
    """⛔ 이 판정기는 KRX entitlement **하나**만 조회한다. gated topic 이 늘면 그 verdict 가
    **다른 상품에도 적용**되어 조용히 열린다 — 추가하는 사람이 판정기부터 고치게 강제한다."""

    def test_passes_today(self):
        ta.assert_single_gated_topic()

    def test_fails_when_a_second_gated_topic_appears(self):
        with patch("app.topic_initial_snapshot.per_user_gated_snapshot_topics",
                   return_value=frozenset({"krx:usd-krw-futures", "krx:cme-futures"})), \
             self.assertRaises(RuntimeError):
            ta.assert_single_gated_topic()


if __name__ == "__main__":
    unittest.main()
