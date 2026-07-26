"""`app/subscription.py` 클럭 주입 seam + 무커버리지 상태머신 baseline.

ADR-039 §8.1 harness 선행 요건 (1). 계획 문서가 지목한 문제: `EntitlementCache`가
`datetime.now()`를 **메서드 안에서 직접** 읽어 TTL·만료 분기를 테스트가 제어할 수 없다.

⚠️ 이 파일이 생기기 전 `tests/` 전체에서 `EntitlementCache`/`PendingCache`/`CACHE_STALE_TTL`/
`PENDING_TTL`/`invalidate_user_cache`를 참조하는 테스트는 **0건**이었다. 즉 seam 도입이
behavior-change-0인지 확인할 red-green 기준선 자체가 없었다 — 그래서 seam보다 **이 baseline이
본체**다. (`CACHE_TTL` grep 히트는 `ALERT_CACHE_TTL_SEC` 부분문자열 오탐이었다.)

## 관측 설계 (codex 검토 반영)

1. **저장 시각은 분기가 아니라 직접 단언한다.** "주입 시각을 과거로 두고 분기가 뒤집히는지" 보는
   방식은 **실클럭 누수를 놓친다**: `set`이 주입값을 무시하고 실클럭(2026)을 저장한 뒤 2000년으로
   `get`하면 `age`가 **음수**가 되어 `age < CACHE_TTL`이 참 — 누수가 오히려 *fresh*로 위장된다
   (실측 확인). 그래서 `set`/`mark`는 캐시에 들어간 값을 직접 본다.
2. **laziness를 함께 잠근다.** 현행 `get`은 miss에서, `state`는 미등록에서 클럭을 읽기 **전에** 반환한다
   (라인 참조 대신 심볼로 적는다 — 라인은 편집마다 밀린다). 클럭 호출 횟수를 세어 이 성질을 보존한다 — seam이 값을 미리
   평가하는 형태로 바뀌면 여기서 red.
3. **부정 단언에는 짝이 되는 positive control을 둔다** (호출 0회 단언 옆에 호출되는 케이스).
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app import subscription
from app.clock import Clock          # 정본은 app.clock (§8.1 harness (1) — 2026-07-27 이동)
from app.subscription import (
    CACHE_STALE_TTL,
    CACHE_TTL,
    PENDING_TTL,
    EntitlementCache,
    PendingCache,
    PremiumStatus,
    verify_premium_status,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class _RecordingClock:
    """스크립트된 시각을 순서대로 돌려주고 **호출 횟수를 센다**.

    마지막 값은 이후 호출에서 계속 재사용된다 — 유한 `side_effect` 리스트가
    `StopIteration`으로 깨지는 리포 내 다른 테스트들의 취약점을 답습하지 않기 위함.
    """

    def __init__(self, *instants: datetime):
        self._instants = list(instants) or [T0]
        self.calls = 0

    def __call__(self) -> datetime:
        value = self._instants[min(self.calls, len(self._instants) - 1)]
        self.calls += 1
        return value


def _poison_mono() -> float:
    """이 모듈은 monotonic 축을 읽지 않는다 — 그 계약을 실행 시점에 잠그는 trip-wire.

    결정적 fake(카운터)를 넣으면 subscription이 mono를 읽기 시작해도 아무도 모른다.
    §8.1 A4의 strict cache(`verified_at_monotonic`)가 이 모듈로 들어오는 날 여기가 red가
    되어 "wall 축 전용" 전제를 **명시적으로** 재검토하게 만든다.
    """
    raise AssertionError(
        "app/subscription.py는 wall 축만 읽는다 — mono 소비가 생겼다면 이 전제를 갱신할 것 "
        "(ADR-039 §8.1 A4 strict cache)"
    )


def _clock(*instants: datetime):
    """`Clock` 생성 단일 지점 (wall 축 전용).

    lease 도입으로 `Clock.mono`가 필수 필드가 됐지만 **반환 shape은 2-tuple 그대로**다
    (호출부 61곳 무변경, 두 번째 원소는 계속 wall recorder — `rec.calls` 단언 6곳이 산다).

    ⚠️ 두 축을 **독립 제어**해야 하는 lease 테스트는 `tests/test_topic_lease.py`가 자체
    `_clock`을 갖는다. 이 리포는 test 파일 간 헬퍼 import가 0건이라 조립부가 둘로 나뉜다 —
    `Clock`에 축이 또 늘면 **두 곳 다** 고칠 것(`app/clock.py` docstring의 조립부 목록 참조).
    """
    recorder = _RecordingClock(*instants)
    return Clock(wall=recorder, mono=_poison_mono), recorder


class TestEntitlementCacheBoundaries(unittest.TestCase):
    """TTL 3분기의 **경계**를 잠근다 — 부등호가 비대칭이라 경계값이 계약이다."""

    def setUp(self):
        self.cache = EntitlementCache()

    def test_miss_returns_none_without_reading_clock(self):
        clock, rec = _clock(T0)
        self.assertEqual(self.cache.get("u", clock=clock), (None, False))
        self.assertEqual(rec.calls, 0, "miss는 클럭을 읽기 전에 반환한다")

    def test_hit_reads_clock(self):
        """위 0회 단언의 positive control — 클럭을 아예 안 쓰는 구현과 구별한다."""
        set_clock, _ = _clock(T0)
        self.cache.set("u", True, clock=set_clock)
        get_clock, rec = _clock(T0)
        self.cache.get("u", clock=get_clock)
        self.assertEqual(rec.calls, 1)

    def test_age_below_ttl_is_fresh(self):
        self.cache.set("u", True, clock=_clock(T0)[0])
        got = self.cache.get("u", clock=_clock(T0 + CACHE_TTL - timedelta(seconds=1))[0])
        self.assertEqual(got, (True, True))

    def test_age_exactly_ttl_is_stale(self):
        """`age < CACHE_TTL`은 **strict** — 경계는 이미 stale이다.

        `<` → `<=` 무력화를 잡는 건 일반 케이스가 아니라 **이 equality 케이스뿐**이다(codex).
        """
        self.cache.set("u", True, clock=_clock(T0)[0])
        got = self.cache.get("u", clock=_clock(T0 + CACHE_TTL)[0])
        self.assertEqual(got, (True, False), "정확히 TTL이면 stale(fallback용)")

    def test_age_just_below_stale_ttl_still_returns_value(self):
        self.cache.set("u", False, clock=_clock(T0)[0])
        got = self.cache.get("u", clock=_clock(T0 + CACHE_STALE_TTL - timedelta(seconds=1))[0])
        self.assertEqual(got, (False, False))

    def test_age_exactly_stale_ttl_evicts_entry(self):
        """`age >= CACHE_STALE_TTL`은 경계 **포함** 삭제 — `>` 로 바꾸면 red."""
        self.cache.set("u", True, clock=_clock(T0)[0])
        got = self.cache.get("u", clock=_clock(T0 + CACHE_STALE_TTL)[0])
        self.assertEqual(got, (None, False))
        self.assertNotIn("u", self.cache._cache, "stale TTL 초과는 엔트리를 삭제한다")


class TestEntitlementCacheStorage(unittest.TestCase):
    """저장 시각을 **직접** 단언 — 분기 관측으로는 실클럭 누수가 fresh로 위장된다."""

    def test_set_stores_injected_instant_exactly(self):
        cache = EntitlementCache()
        clock, _ = _clock(T0)
        cache.set("u", True, clock=clock)
        is_premium, cached_at = cache._cache["u"]
        self.assertIs(is_premium, True)
        self.assertEqual(cached_at, T0, "주입 시각이 그대로 저장돼야 한다(실클럭이면 여기서 red)")

    def test_lru_evicts_oldest_and_get_refreshes_recency(self):
        cache = EntitlementCache(max_size=2)
        clock, _ = _clock(T0)
        cache.set("a", True, clock=clock)
        cache.set("b", True, clock=clock)
        cache.get("a", clock=clock)          # a를 최근으로 끌어올림
        cache.set("c", True, clock=clock)    # 초과 → 가장 오래된 b가 밀려남
        self.assertEqual(set(cache._cache), {"a", "c"})

    def test_clear_empties_cache(self):
        cache = EntitlementCache()
        cache.set("u", True, clock=_clock(T0)[0])
        cache.clear()
        self.assertEqual(cache.get("u", clock=_clock(T0)[0]), (None, False))


class TestPendingCache(unittest.TestCase):
    def setUp(self):
        self.pending = PendingCache()

    def test_unknown_returns_none_without_reading_clock(self):
        clock, rec = _clock(T0)
        self.assertEqual(self.pending.state("u", clock=clock), "none")
        self.assertEqual(rec.calls, 0, "미등록은 클럭을 읽기 전에 반환한다")

    def test_known_reads_clock(self):
        """위 0회 단언의 positive control."""
        self.pending.mark("u", clock=_clock(T0)[0])
        clock, rec = _clock(T0)
        self.pending.state("u", clock=clock)
        self.assertEqual(rec.calls, 1)

    def test_state_at_exact_deadline_is_active(self):
        """`now <= expires_at`은 경계 **포함** — `<` 로 바꾸면 red."""
        self.pending.mark("u", clock=_clock(T0)[0])
        at_deadline = T0 + PENDING_TTL
        self.assertEqual(self.pending.state("u", clock=_clock(at_deadline)[0]), "active")

    def test_state_after_deadline_is_expired(self):
        self.pending.mark("u", clock=_clock(T0)[0])
        after = T0 + PENDING_TTL + timedelta(microseconds=1)
        self.assertEqual(self.pending.state("u", clock=_clock(after)[0]), "expired")

    def test_mark_stores_absolute_deadline(self):
        """저장 shape가 `EntitlementCache.set`과 **반대**다(생성시각이 아니라 만료시각)."""
        self.pending.mark("u", clock=_clock(T0)[0])
        self.assertEqual(self.pending._cache["u"], T0 + PENDING_TTL)

    def test_clear_all_empties(self):
        self.pending.mark("u", clock=_clock(T0)[0])
        self.pending.clear_all()
        self.assertEqual(self.pending.state("u", clock=_clock(T0)[0]), "none")


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, bad_json: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("bad json")
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        return self._response


def _revenuecat(payload=None, status_code: int = 200, bad_json: bool = False):
    response = _FakeResponse(status_code, payload, bad_json)
    return patch.object(subscription.httpx, "AsyncClient", lambda **kw: _FakeAsyncClient(response))


def _entitlement(expires: str | None, present: bool = True):
    """RevenueCat 응답 fixture.

    ⚠️ lifetime은 `premium`이 **truthy이면서** `expires_date`가 없는 경우다.
    빈 dict를 쓰면 `if not premium`에 걸려 "구독 없음" 분기로 가버려, lifetime 테스트와
    no-entitlement 테스트가 **같은 경로를 보게 된다**(초안이 그랬다 — 테스트가 잡았다).
    """
    if not present:
        return {"subscriber": {"entitlements": {}}}
    premium = {"product_identifier": "premium_yearly"}
    if expires:
        premium["expires_date"] = expires
    return {"subscriber": {"entitlements": {"premium": premium}}}


class TestRevenueCatExpiryBoundary(unittest.IsolatedAsyncioTestCase):
    """`expires_dt > now`의 **strict** 경계 — 정확히 같으면 만료다."""

    async def _check(self, payload, clock):
        with patch.object(subscription, "REVENUECAT_API_KEY", "k"), _revenuecat(payload):
            return await subscription._check_revenuecat_entitlement("u", clock=clock)

    async def test_future_expiry_is_premium(self):
        payload = _entitlement((T0 + timedelta(seconds=1)).isoformat())
        self.assertEqual(await self._check(payload, _clock(T0)[0]), (True, True))

    async def test_expiry_exactly_now_is_not_premium(self):
        payload = _entitlement(T0.isoformat())
        self.assertEqual(await self._check(payload, _clock(T0)[0]), (False, True))

    async def test_past_expiry_is_not_premium(self):
        payload = _entitlement((T0 - timedelta(seconds=1)).isoformat())
        self.assertEqual(await self._check(payload, _clock(T0)[0]), (False, True))

    async def test_lifetime_entitlement_does_not_read_clock(self):
        clock, rec = _clock(T0)
        self.assertEqual(await self._check(_entitlement(None), clock), (True, True))
        self.assertEqual(rec.calls, 0, "만료일이 없으면 시각 비교 자체가 없다")

    async def test_no_entitlement_does_not_read_clock(self):
        clock, rec = _clock(T0)
        self.assertEqual(await self._check(_entitlement(None, present=False), clock), (False, True))
        self.assertEqual(rec.calls, 0)

    async def test_http_404_is_non_premium_but_cacheable(self):
        with patch.object(subscription, "REVENUECAT_API_KEY", "k"), _revenuecat(status_code=404):
            got = await subscription._check_revenuecat_entitlement("u", clock=_clock(T0)[0])
        self.assertEqual(got, (False, True))

    async def test_http_500_is_not_cacheable(self):
        with patch.object(subscription, "REVENUECAT_API_KEY", "k"), _revenuecat(status_code=500):
            got = await subscription._check_revenuecat_entitlement("u", clock=_clock(T0)[0])
        self.assertEqual(got, (False, False), "일시 오류를 캐시하면 유료 사용자를 차단한다")

    async def test_bad_json_is_not_cacheable(self):
        with patch.object(subscription, "REVENUECAT_API_KEY", "k"), _revenuecat(bad_json=True):
            got = await subscription._check_revenuecat_entitlement("u", clock=_clock(T0)[0])
        self.assertEqual(got, (False, False))


class TestVerifyPremiumStatusStateMachine(unittest.IsolatedAsyncioTestCase):
    """`verify_premium_status` 분기 baseline — seam 전후 동일해야 behavior-change-0.

    ⚠️ 완전한 곱 매트릭스가 아니라 **주요 분기** 커버다(codex).
    """

    def setUp(self):
        subscription._cache.clear()
        subscription._pending.clear_all()

    def tearDown(self):
        subscription._cache.clear()
        subscription._pending.clear_all()

    def _api(self, is_premium: bool, should_cache: bool):
        return patch.object(
            subscription, "_check_revenuecat_entitlement",
            new=AsyncMock(return_value=(is_premium, should_cache)))

    async def test_empty_user_id_is_inactive(self):
        self.assertEqual(await verify_premium_status("", clock=_clock(T0)[0]), PremiumStatus.INACTIVE)

    async def test_fresh_cache_short_circuits_api(self):
        subscription._cache.set("u", True, clock=_clock(T0)[0])
        api = AsyncMock()
        with patch.object(subscription, "_check_revenuecat_entitlement", new=api):
            got = await verify_premium_status("u", clock=_clock(T0)[0])
        self.assertEqual(got, PremiumStatus.ACTIVE)
        api.assert_not_awaited()

    async def test_fresh_cache_false_is_inactive(self):
        """short-circuit까지 단언한다 — 결과만 보면 API가 (False, *)를 내도 통과해 판별력이 없다(codex)."""
        subscription._cache.set("u", False, clock=_clock(T0)[0])
        api = AsyncMock()
        with patch.object(subscription, "_check_revenuecat_entitlement", new=api):
            got = await verify_premium_status("u", clock=_clock(T0)[0])
        self.assertEqual(got, PremiumStatus.INACTIVE)
        api.assert_not_awaited()

    async def test_stale_cache_with_api_success_refreshes(self):
        subscription._cache.set("u", False, clock=_clock(T0)[0])
        later = T0 + CACHE_TTL
        with self._api(True, True):
            got = await verify_premium_status("u", clock=_clock(later)[0])
        self.assertEqual(got, PremiumStatus.ACTIVE)
        self.assertEqual(subscription._cache._cache["u"], (True, later))

    async def test_stale_cache_with_api_error_falls_back(self):
        subscription._cache.set("u", True, clock=_clock(T0)[0])
        later = T0 + CACHE_TTL
        with self._api(False, False):
            got = await verify_premium_status("u", clock=_clock(later)[0])
        self.assertEqual(got, PremiumStatus.ACTIVE, "장애 시 stale 값으로 유료 사용자를 보호한다")
        self.assertEqual(subscription._cache._cache["u"], (True, T0), "fallback은 캐시를 갱신하지 않는다")

    async def test_miss_with_api_error_marks_pending(self):
        with self._api(False, False):
            got = await verify_premium_status("u", clock=_clock(T0)[0])
        self.assertEqual(got, PremiumStatus.PENDING)
        self.assertEqual(subscription._pending._cache["u"], T0 + PENDING_TTL)

    async def test_miss_with_api_inactive_first_time_is_pending(self):
        with self._api(False, True):
            got = await verify_premium_status("u", clock=_clock(T0)[0])
        self.assertEqual(got, PremiumStatus.PENDING)
        self.assertIn("u", subscription._pending._cache)

    async def test_miss_with_api_inactive_while_pending_active_stays_pending(self):
        subscription._pending.mark("u", clock=_clock(T0)[0])
        with self._api(False, True):
            got = await verify_premium_status("u", clock=_clock(T0 + PENDING_TTL)[0])
        self.assertEqual(got, PremiumStatus.PENDING)

    async def test_stale_cache_true_with_api_inactive_is_downgraded(self):
        """stale True → API False 경로(`cached_value is not None` 분기). 반대 방향만 덮여 있었다(codex)."""
        subscription._cache.set("u", True, clock=_clock(T0)[0])
        later = T0 + CACHE_TTL
        with self._api(False, True):
            got = await verify_premium_status("u", clock=_clock(later)[0])
        self.assertEqual(got, PremiumStatus.INACTIVE)
        self.assertEqual(subscription._cache._cache["u"], (False, later))

    async def test_stale_cache_false_with_api_error_falls_back_to_false(self):
        """stale fallback의 False 방향 — True 방향만 덮으면 '항상 ACTIVE 반환'과 구별되지 않는다."""
        subscription._cache.set("u", False, clock=_clock(T0)[0])
        with self._api(False, False):
            got = await verify_premium_status("u", clock=_clock(T0 + CACHE_TTL)[0])
        self.assertEqual(got, PremiumStatus.INACTIVE)

    async def test_miss_with_api_error_while_pending_active_does_not_extend(self):
        """API 실패 + pending active — 이미 pending이면 deadline을 **연장하지 않는다**."""
        subscription._pending.mark("u", clock=_clock(T0)[0])
        original = subscription._pending._cache["u"]
        with self._api(False, False):
            got = await verify_premium_status("u", clock=_clock(T0 + timedelta(seconds=1))[0])
        self.assertEqual(got, PremiumStatus.PENDING)
        self.assertEqual(subscription._pending._cache["u"], original, "재마킹하면 pending이 무한 연장된다")

    async def test_miss_with_api_error_after_pending_expired_does_not_remark(self):
        """API 실패 + pending expired — `state != "none"`이라 재마킹되지 않는다(현행 계약 고정)."""
        subscription._pending.mark("u", clock=_clock(T0)[0])
        original = subscription._pending._cache["u"]
        after = T0 + PENDING_TTL + timedelta(seconds=1)
        with self._api(False, False):
            got = await verify_premium_status("u", clock=_clock(after)[0])
        self.assertEqual(got, PremiumStatus.PENDING)
        self.assertEqual(subscription._pending._cache["u"], original)

    async def test_verify_premium_wrapper_forwards_same_clock(self):
        """`verify_premium`은 export 표면 — clock을 그대로 넘겨야 wrapper 경유 결정적 테스트가 가능하다.

        주입 clock이 무시되면 실클럭으로 fresh 판정이 뒤집혀 red가 된다.
        """
        subscription._cache.set("u", True, clock=_clock(T0)[0])
        api = AsyncMock()
        with patch.object(subscription, "_check_revenuecat_entitlement", new=api):
            self.assertIs(await subscription.verify_premium("u", clock=_clock(T0)[0]), True)
            api.assert_not_awaited()
        # positive control — 같은 clock으로 stale 구간을 주면 API 경로로 내려간다(전달이 실제로 결정한다)
        with self._api(False, True):
            self.assertIs(await subscription.verify_premium("u", clock=_clock(T0 + CACHE_STALE_TTL)[0]), False)

    async def test_miss_with_api_inactive_after_pending_expired_is_inactive(self):
        subscription._pending.mark("u", clock=_clock(T0)[0])
        after = T0 + PENDING_TTL + timedelta(seconds=1)
        with self._api(False, True):
            got = await verify_premium_status("u", clock=_clock(after)[0])
        self.assertEqual(got, PremiumStatus.INACTIVE)
        self.assertEqual(subscription._cache._cache["u"], (False, after))
        self.assertNotIn("u", subscription._pending._cache, "확정되면 pending을 비운다")


class TestSamplingFidelity(unittest.IsolatedAsyncioTestCase):
    """패스 내 **샘플링 지점**을 보존한다 — 시작 시각 하나를 공유하면 안 된다.

    `_cache.get`과 `_cache.set`은 `_check_revenuecat_entitlement`의 **HTTP 왕복**을 사이에
    두고 있다. 패스 시작 시각으로 하나 캡처해 공유하면 `cached_at`이 API 호출 *이전* 시각으로
    찍혀 캐시가 그만큼 일찍 만료된다. (⚠️ `timeout=5.0`은 connect/read/write/pool **단계별**
    값이라 왕복 총시간의 상한이 5초인 것이 아니다 — 편차는 더 클 수 있다.)
    """

    def setUp(self):
        subscription._cache.clear()
        subscription._pending.clear_all()

    def tearDown(self):
        subscription._cache.clear()
        subscription._pending.clear_all()

    async def test_cached_at_uses_set_time_not_pass_start(self):
        subscription._cache.set("u", False, clock=_clock(T0)[0])
        get_at = T0 + CACHE_TTL           # stale → API 경로 진입
        set_at = get_at + timedelta(seconds=3)   # HTTP 왕복 후
        clock, _ = _clock(get_at, set_at)
        with patch.object(subscription, "_check_revenuecat_entitlement",
                          new=AsyncMock(return_value=(True, True))):
            await verify_premium_status("u", clock=clock)
        self.assertEqual(subscription._cache._cache["u"][1], set_at,
                         "cached_at은 set 시점 — 패스 시작 시각을 공유하면 red")


if __name__ == "__main__":
    unittest.main()
