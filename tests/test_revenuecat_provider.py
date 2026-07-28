"""RevenueCat provider — characterization + typed 결과 계약 (ADR-039 §8.1 A4-1).

## 왜 characterization인가

`_check_revenuecat_entitlement`는 `(is_premium, should_cache)` 튜플을 돌려주는데,
`(False, False)`가 **성격이 전혀 다른 5개 경로**에서 나온다 — API key 미설정(config) /
JSON 파싱 실패(계약 위반) / `expires_date` 파싱 실패 / 4xx·5xx 일괄(config ⊎ transient) /
`except Exception`(transient ⊎ programming). strict WS 인가 경로가 이걸 그대로 소비하면
§8.1 A6의 3-bucket 계약(terminal / transient / 내부 예외)이 즉시 깨진다.

그래서 **아래에 typed provider를 두고 REST adapter가 기존 동작을 그대로 되접는다.**
이 파일의 절반은 그 되접기가 **behavior-change-0**임을 잠그는 characterization이다 —
리팩터 *이전*에 작성해 현행 코드에서 green임을 확인하고, 리팩터 이후에도 green이어야 한다.

⚠️ **behavior-change-0의 최강 증거는 이 파일이 아니라 `tests/test_subscription_clock.py`가
무수정으로 green이라는 사실**이다(상태머신 + 샘플링 지점 + laziness까지 잠근 38+건).
그 파일을 한 줄이라도 고쳐야 한다면 그건 동작 변경 신호다.

⚠️ **축 주의**: `expires_dt > clock.wall()`의 wall 축은 **구독 유효성** 판정이라 맞다.
lease horizon의 monotonic 축(§8.1 A2)과 목적이 다르므로 "일관성" 명목으로 바꾸지 말 것.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from app import subscription
from app.clock import Clock
from app.subscription import (
    BadRequest,
    Determined,
    ProviderMisconfigured,
    ProviderUnavailable,
    ProtocolViolation,
    _check_revenuecat_entitlement,
    fetch_revenuecat_result,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _clock(instant: datetime = T0) -> Clock:
    """wall 전용 — 이 모듈은 monotonic 축을 읽지 않는다(§8.1 A4)."""

    def _poison_mono() -> float:
        raise AssertionError("subscription은 wall 축만 읽는다 (ADR-039 §8.1 A4)")

    return Clock(wall=lambda: instant, mono=_poison_mono)


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, raises: Exception | None = None):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._payload


class _FakeAsyncClient:
    """`httpx.AsyncClient(...)` 컨텍스트 매니저 대역."""

    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, *args, **kwargs):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _patch_http(result):
    return patch.object(subscription.httpx, "AsyncClient", lambda *a, **k: _FakeAsyncClient(result))


_FUTURE = "2026-06-01T00:00:00Z"
_PAST = "2025-06-01T00:00:00Z"

# ⚠️ `premium` 객체의 **truthiness가 분기를 가른다** — 현행 코드가 `if not premium: return (False, True)`
#    이므로 **빈 dict는 "구독 없음"으로 접힌다**. 리팩터 전 실측으로 확인했다:
#      premium={}                      -> (False, True)   ← lifetime 아님
#      premium={product_identifier:…}  -> (True,  True)   ← 진짜 lifetime (expires_date 없음)
#    초안에서 lifetime 픽스처를 `{}`로 잡아 기대값을 (True, True)로 적었다가 characterization이 잡았다.
_ACTIVE = {"product_identifier": "p", "expires_date": _FUTURE}
_EXPIRED = {"product_identifier": "p", "expires_date": _PAST}
_LIFETIME = {"product_identifier": "lifetime_x"}
_BAD_DATE = {"product_identifier": "p", "expires_date": "not-a-date"}


def _entitlement_payload(premium):
    return {"subscriber": {"entitlements": {"premium": premium}}}


def _no_premium_key_payload():
    return {"subscriber": {"entitlements": {}}}


# ── 시나리오 표: (라벨, 응답/예외, 기대 legacy 튜플, 기대 typed 변종) ────────────────

SCENARIOS = [
    ("200 active",        _FakeResponse(200, _entitlement_payload(_ACTIVE)),        (True, True),   Determined),
    ("200 lifetime",      _FakeResponse(200, _entitlement_payload(_LIFETIME)),      (True, True),   Determined),
    ("200 expired",       _FakeResponse(200, _entitlement_payload(_EXPIRED)),       (False, True),  Determined),
    ("200 empty premium", _FakeResponse(200, _entitlement_payload({})),             (False, True),  Determined),
    ("200 null premium",  _FakeResponse(200, _entitlement_payload(None)),           (False, True),  Determined),
    ("200 no premium key", _FakeResponse(200, _no_premium_key_payload()),           (False, True),  Determined),
    ("404 unknown user",  _FakeResponse(404),                                       (False, True),  Determined),
    ("400 bad request",   _FakeResponse(400),                                       (False, False), BadRequest),
    ("401 unauthorized",  _FakeResponse(401),                                       (False, False), ProviderMisconfigured),
    ("403 forbidden",     _FakeResponse(403),                                       (False, False), ProviderMisconfigured),
    ("408 timeout",       _FakeResponse(408),                                       (False, False), ProviderUnavailable),
    ("429 rate limited",  _FakeResponse(429),                                       (False, False), ProviderUnavailable),
    ("500 server error",  _FakeResponse(500),                                       (False, False), ProviderUnavailable),
    ("503 unavailable",   _FakeResponse(503),                                       (False, False), ProviderUnavailable),
    ("409 unexpected 4xx", _FakeResponse(409),                                      (False, False), ProtocolViolation),
    ("malformed JSON",    _FakeResponse(200, raises=ValueError("not json")),        (False, False), ProtocolViolation),
    ("malformed date",    _FakeResponse(200, _entitlement_payload(_BAD_DATE)),      (False, False), ProtocolViolation),
    ("transport error",   httpx.ConnectError("connection reset"),                   (False, False), ProviderUnavailable),
    ("read timeout",      httpx.ReadTimeout("slow"),                                (False, False), ProviderUnavailable),
    # 응답 **형식** 위반 — 구 broad except에서는 전부 ProviderUnavailable(retryable)로 접혔다.
    ("body not an object", _FakeResponse(200, [1, 2, 3]),                           (False, False), ProtocolViolation),
    ("subscriber is str", _FakeResponse(200, {"subscriber": "nope"}),               (False, False), ProtocolViolation),
    ("premium is str",    _FakeResponse(200, _entitlement_payload("yes")),          (False, False), ProtocolViolation),
    ("expires is number", _FakeResponse(200, _entitlement_payload({"expires_date": 12345})), (False, False), ProtocolViolation),
    # tz 없는 날짜 — aware/naive 비교라 TypeError. 계약 위반이므로 예외가 아니라 ProtocolViolation.
    ("expires without tz", _FakeResponse(200, _entitlement_payload({"product_identifier": "p", "expires_date": "2026-06-01T00:00:00"})), (False, False), ProtocolViolation),
]


class TestLegacyTupleCharacterization(unittest.IsolatedAsyncioTestCase):
    """리팩터 전후로 `(is_premium, should_cache)`가 **한 비트도** 달라지지 않아야 한다.

    ⚠️ 이 표에서 `(False, False)` 행이 여러 개인 것이 **핵심**이다 — typed provider가 내부적으로
    5가지로 구분해도 REST가 보는 결과는 전부 동일해야 한다.
    """

    async def test_each_scenario_preserves_legacy_tuple(self):
        for label, result, expected, _variant in SCENARIOS:
            with self.subTest(scenario=label):
                with _patch_http(result), patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                    got = await _check_revenuecat_entitlement("u", clock=_clock())
                self.assertEqual(got, expected, label)

    async def test_missing_api_key_short_circuits_before_http(self):
        """config 오류 — HTTP를 **타지 않고** 반환한다(현행 동작)."""
        boom = _patch_http(AssertionError("HTTP를 타면 안 된다"))
        with boom, patch.object(subscription, "REVENUECAT_API_KEY", ""):
            got = await _check_revenuecat_entitlement("u", clock=_clock())
        self.assertEqual(got, (False, False))

    async def test_expiry_boundary_uses_injected_wall_clock(self):
        """`expires_dt > clock.wall()` — **strict**이므로 정확히 같은 시각은 만료다.

        clock을 주입해 판정이 뒤집히는 것으로 wall 축 사용을 잠근다(축을 monotonic으로
        바꾸는 mutation은 여기서 `_poison_mono`로 터진다).
        """
        payload = _entitlement_payload({"product_identifier": "p", "expires_date": "2026-01-01T00:00:00Z"})
        with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
            with _patch_http(_FakeResponse(200, payload)):
                at_boundary = await _check_revenuecat_entitlement("u", clock=_clock(T0))
            with _patch_http(_FakeResponse(200, payload)):
                just_before = await _check_revenuecat_entitlement(
                    "u", clock=_clock(T0 - timedelta(seconds=1))
                )
        self.assertEqual(at_boundary, (False, True), "경계는 만료")
        self.assertEqual(just_before, (True, True), "1초 전은 유효")


class TestTypedResult(unittest.IsolatedAsyncioTestCase):
    """typed provider — REST가 평탄화한 것을 **구분해서** 돌려준다(strict가 소비할 축)."""

    async def test_each_scenario_maps_to_expected_variant(self):
        for label, result, _expected, variant in SCENARIOS:
            with self.subTest(scenario=label):
                with _patch_http(result), patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                    got = await fetch_revenuecat_result("u", clock=_clock())
                self.assertIsInstance(got, variant, label)

    async def test_missing_api_key_is_misconfigured_not_unavailable(self):
        """⚠️ 이 구분이 이 슬라이스의 존재 이유다 — 재시도로 낫지 않는 오류다."""
        boom = _patch_http(AssertionError("HTTP를 타면 안 된다"))
        with boom, patch.object(subscription, "REVENUECAT_API_KEY", ""):
            got = await fetch_revenuecat_result("u", clock=_clock())
        self.assertIsInstance(got, ProviderMisconfigured)

    async def test_determined_carries_is_premium(self):
        with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
            with _patch_http(_FakeResponse(200, _entitlement_payload(_ACTIVE))):
                active = await fetch_revenuecat_result("u", clock=_clock())
            with _patch_http(_FakeResponse(404)):
                unknown = await fetch_revenuecat_result("u", clock=_clock())
        self.assertEqual((active.is_premium, unknown.is_premium), (True, False))

    async def test_429_is_not_misconfigured(self):
        """rate limit을 config 오류로 접으면 **영구 포기 + 허위 알람**이 된다.

        "4xx 대 5xx"로만 나누는 mutation을 잡는 유일한 단언 계열이다.
        """
        with _patch_http(_FakeResponse(429)), patch.object(subscription, "REVENUECAT_API_KEY", "k"):
            got = await fetch_revenuecat_result("u", clock=_clock())
        self.assertIsInstance(got, ProviderUnavailable)
        self.assertNotIsInstance(got, ProviderMisconfigured)

    async def test_unavailable_carries_status_for_telemetry(self):
        with _patch_http(_FakeResponse(503)), patch.object(subscription, "REVENUECAT_API_KEY", "k"):
            got = await fetch_revenuecat_result("u", clock=_clock())
        self.assertEqual(got.status, 503)

    async def test_transport_error_has_no_status(self):
        """HTTP 응답이 없었으므로 status는 None — 있는 척하면 텔레메트리가 거짓이 된다."""
        with _patch_http(httpx.ConnectError("boom")), patch.object(subscription, "REVENUECAT_API_KEY", "k"):
            got = await fetch_revenuecat_result("u", clock=_clock())
        self.assertIsInstance(got, ProviderUnavailable)
        self.assertIsNone(got.status)


class TestAdapterEquivalence(unittest.IsolatedAsyncioTestCase):
    """adapter가 typed 결과를 legacy 튜플로 되접는 규칙을 **직접** 잠근다.

    위 두 클래스는 같은 입력에 대한 두 출력을 각각 보지만, 여기서는 **매핑 자체**를 본다 —
    `Determined`만 `should_cache=True`이고 나머지 4변종은 전부 `(False, False)`.
    """

    def test_determined_maps_to_should_cache_true(self):
        self.assertEqual(subscription._result_to_legacy_tuple(Determined(is_premium=True)), (True, True))
        self.assertEqual(subscription._result_to_legacy_tuple(Determined(is_premium=False)), (False, True))

    def test_every_non_determined_variant_maps_to_false_false(self):
        for result in (
            ProviderUnavailable(status=503),
            ProviderUnavailable(status=None),
            ProviderMisconfigured(status=401),
            BadRequest(status=400),
            ProtocolViolation(detail="malformed"),
        ):
            with self.subTest(variant=type(result).__name__):
                self.assertEqual(subscription._result_to_legacy_tuple(result), (False, False))


if __name__ == "__main__":
    unittest.main()


class TestUnexpectedErrorsPropagate(unittest.IsolatedAsyncioTestCase):
    """A6-1 핵심 — **programming 오류는 verdict가 아니다.**

    provider는 예상 밖 예외를 **그대로 전파**해야 strict 경로가 그것을 retryable로 오인하지
    않는다. REST 호환(`(False, False)`)은 **adapter가 떠안는다**.

    ⚠️ 이 클래스가 잡는 회귀: provider를 광범위 `except Exception`으로 되돌리는 변경.
    구 코드가 정확히 그랬고, JSON이 list인 경우·`premium`이 문자열인 경우까지
    `ProviderUnavailable`(retryable)로 접혔다(실측 확인 후 이 슬라이스에서 교정).
    """

    async def test_provider_propagates_unexpected_exception(self):
        with _patch_http(RuntimeError("bug in our code")):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                with self.assertRaises(RuntimeError):
                    await fetch_revenuecat_result("u", clock=_clock())

    async def test_adapter_absorbs_it_for_rest_compatibility(self):
        """같은 예외가 REST에서는 구 동작 그대로 `(False, False)`여야 한다."""
        with _patch_http(RuntimeError("bug in our code")):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                got = await _check_revenuecat_entitlement("u", clock=_clock())
        self.assertEqual(got, (False, False))

    async def test_clock_failure_is_not_swallowed_as_unavailable(self):
        """주입 clock이 터지는 건 우리 버그다 — retryable로 접히면 원인이 영영 안 보인다."""

        def _boom() -> datetime:
            raise RuntimeError("clock exploded")

        clock = Clock(wall=_boom, mono=lambda: 0.0)
        with _patch_http(_FakeResponse(200, _entitlement_payload(_ACTIVE))):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                with self.assertRaises(RuntimeError):
                    await fetch_revenuecat_result("u", clock=clock)

    async def test_date_block_does_not_swallow_arbitrary_exceptions(self):
        """`expires_date` 해석 블록도 **좁게** 잡아야 한다.

        ⚠️ 이 테스트가 없으면 그 블록의 `except (AttributeError, TypeError, ValueError)`를
        `except Exception`으로 되돌려도 **아무 테스트도 red가 되지 않는다**(무력화로 실증).
        형식 오류(ValueError·TypeError·AttributeError)는 ProtocolViolation이 맞지만,
        그 밖의 예외는 우리 버그이므로 전파돼야 한다.
        """

        class _ExplodingExpires:
            def __bool__(self):
                return True

            def replace(self, *args, **kwargs):
                raise RuntimeError("bug in our parsing")

        payload = _entitlement_payload(
            {"product_identifier": "p", "expires_date": _ExplodingExpires()}
        )
        with _patch_http(_FakeResponse(200, payload)):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                with self.assertRaises(RuntimeError):
                    await fetch_revenuecat_result("u", clock=_clock())


class TestMalformedBodyStillPassesAsAuthoritative(unittest.IsolatedAsyncioTestCase):
    """⚠️ **알려진 gap — N-3으로 이월**. 여기 값들은 계약 위반인데 `Determined`로 통과한다.

    실측(2026-07-28):

    | 입력 | typed | legacy(REST) |
    |---|---|---|
    | `{}` / `subscriber` 누락 / `premium=[]` | `Determined(False)` | `(False, True)` |
    | `expires_date=""` / `0` | `Determined(True)` ← **프리미엄 부여** | `(True, True)` |

    누락 키의 `{}` 기본값과 truthiness 분기 때문이며, strict가 이걸 **관측으로 저장**하면
    정상 사용자를 "구독 없음"으로 굳히거나 malformed 값으로 프리미엄을 줄 수 있다.

    **이번 슬라이스에서 고치지 않는 이유**: `ProtocolViolation`으로 바꾸면 legacy 튜플이
    `(False, True)`→`(False, False)`, `(True, True)`→`(False, False)`로 **REST 동작이 바뀐다**.
    이번 GO는 "REST behavior-change-0"이라 그 변경은 범위 밖이다(별도 승인 필요).

    → 그래서 **현재 동작을 그대로 못 박아 둔다**. N-3에서 조이면 이 클래스가 red가 되고,
    그 red가 곧 **REST에 미치는 영향의 목록**이 된다(조용히 바뀌지 않게 하는 장치).
    """

    CASES = [
        ("body {}", {}, (False, True), True),
        ("subscriber 누락", {"other": 1}, (False, True), True),
        ("premium=[]", _entitlement_payload([]), (False, True), True),
        ('expires_date=""', _entitlement_payload({"product_identifier": "p", "expires_date": ""}), (True, True), True),
        ("expires_date=0", _entitlement_payload({"product_identifier": "p", "expires_date": 0}), (True, True), True),
    ]

    async def test_current_behavior_is_pinned(self):
        for label, payload, legacy, is_determined in self.CASES:
            with self.subTest(case=label):
                with _patch_http(_FakeResponse(200, payload)):
                    with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                        typed = await fetch_revenuecat_result("u", clock=_clock())
                with _patch_http(_FakeResponse(200, payload)):
                    with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                        got = await _check_revenuecat_entitlement("u", clock=_clock())
                self.assertEqual(isinstance(typed, Determined), is_determined, label)
                self.assertEqual(got, legacy, label)

    async def test_empty_expires_date_currently_grants_premium(self):
        """가장 위험한 항목을 따로 세운다 — malformed 입력에 **프리미엄이 부여**된다."""
        payload = _entitlement_payload({"product_identifier": "p", "expires_date": ""})
        with _patch_http(_FakeResponse(200, payload)):
            with patch.object(subscription, "REVENUECAT_API_KEY", "k"):
                typed = await fetch_revenuecat_result("u", clock=_clock())
        self.assertEqual(typed, Determined(is_premium=True), "N-3에서 조일 때 여기가 red가 된다")
