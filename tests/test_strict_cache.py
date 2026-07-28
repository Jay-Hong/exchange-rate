"""N-3b strict cache store — WS strict 인가용 관측 저장소.

계약 근거: `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A4 / A6-1 / A6-2.

test-first로 작성됐다. 각 테스트가 불변식 하나를 잠그고, load-bearing 여부는 mutation으로 확인한다.

⚠️ **epoch 절대값을 단언하지 않는다** — 전역 단조 시퀀스라 uid 간 값이 비연속이다.
epoch은 항상 `snapshot()`에서 얻는다(그게 capture primitive다).
"""
import math
import threading
import unittest

from app.strict_authz import (
    IdentityAuthorityFound,
    IdentityAuthorityNotFound,
    PremiumObservation,
)
from app.strict_cache import (
    PutAccepted,
    PutRejected,
    RejectReason,
    StrictObservationCache,
)

UID = "uid-1"
WATERMARK_MS = 1_700_000_000_000


def _premium(epoch, *, uid=UID, active=True, at=100.0):
    return PremiumObservation(uid=uid, active=active, verified_at_mono=at, epoch=epoch)


def _identity(epoch, *, uid=UID, disabled=False, at=100.0):
    return IdentityAuthorityFound(
        uid=uid,
        disabled=disabled,
        tokens_valid_after_ms=WATERMARK_MS,
        verified_at_mono=at,
        epoch=epoch,
    )


class _CacheTest(unittest.TestCase):
    def setUp(self):
        self.cache = StrictObservationCache()

    def epoch(self, uid=UID):
        return self.cache.snapshot(uid).epoch


class TestBasicRoundTrip(_CacheTest):
    def test_snapshot_of_a_new_uid_allocates_and_has_no_observations(self):
        snap = self.cache.snapshot(UID)
        self.assertEqual(snap.uid, UID)
        self.assertGreater(snap.epoch, 0)
        self.assertIsNone(snap.premium)
        self.assertIsNone(snap.identity)

    def test_put_with_matching_epoch_is_accepted_and_readable(self):
        obs = _premium(self.epoch())
        self.assertIsInstance(self.cache.put_premium(obs, expected_epoch=obs.epoch), PutAccepted)
        self.assertEqual(self.cache.snapshot(UID).premium, obs)

    def test_concerns_are_stored_independently(self):
        e = self.epoch()
        self.cache.put_premium(_premium(e), expected_epoch=e)
        self.assertIsNone(self.cache.snapshot(UID).identity)
        self.cache.put_identity(_identity(e), expected_epoch=e)
        self.assertIsNotNone(self.cache.snapshot(UID).premium)

    def test_uids_are_isolated(self):
        e = self.epoch("a")
        self.cache.put_premium(_premium(e, uid="a"), expected_epoch=e)
        self.assertIsNone(self.cache.snapshot("b").premium)

    def test_not_found_identity_round_trips(self):
        e = self.epoch()
        obs = IdentityAuthorityNotFound(uid=UID, verified_at_mono=100.0, epoch=e)
        self.assertIsInstance(self.cache.put_identity(obs, expected_epoch=e), PutAccepted)
        self.assertEqual(self.cache.snapshot(UID).identity, obs)


class TestCoherentSnapshot(_CacheTest):
    """§A4 — 소비(lease)까지 fence하려면 **일관된** 읽기와 재확인 수단이 있어야 한다."""

    def test_snapshot_returns_both_concerns_and_the_epoch_together(self):
        e = self.epoch()
        self.cache.put_premium(_premium(e), expected_epoch=e)
        self.cache.put_identity(_identity(e), expected_epoch=e)
        snap = self.cache.snapshot(UID)
        self.assertEqual(snap.epoch, e)
        self.assertIsNotNone(snap.premium)
        self.assertIsNotNone(snap.identity)

    def test_concern_wise_reads_are_not_exposed(self):
        """⛔ 표현할 수 없으면 실수할 수 없다 — 따로 읽으면 **동시에 존재한 적 없는 쌍**이 된다.

        두 read 사이에 `bump`가 끼면 premium은 epoch N, identity는 N+1이 되는데
        `derive_verdict`는 둘 다 필요하고 epoch은 보지 않는다.
        """
        for name in ("get_premium", "get_identity", "current_epoch"):
            self.assertFalse(hasattr(self.cache, name), f"{name}이 노출되면 straddle이 기본 경로가 된다")

    def test_is_current_detects_invalidation_after_the_snapshot(self):
        snap = self.cache.snapshot(UID)
        self.assertTrue(self.cache.is_current(snap))
        self.cache.bump(UID)
        self.assertFalse(self.cache.is_current(snap), "무효화 후 lease를 발급하면 fence가 무의미하다")

    def test_is_current_is_a_point_in_time_check_not_mutual_exclusion(self):
        """⛔ 계약 잠금 — 이 검사는 반환 직후 무효화될 수 있다.

        `bump()`는 이 저장소의 lock만 잡으므로 호출자가 어떤 외부 lock을 쥐고 있어도 배제되지
        않는다. 그래서 계약은 "등록 → 1회 재확인 → 활성화"이지 "확인했으니 안전"이 아니다.
        (구 docstring이 정확히 그 반대를 지시하고 있었다.)
        """
        snap = self.cache.snapshot(UID)
        self.assertTrue(self.cache.is_current(snap))
        self.cache.bump(UID)          # 반환 직후 무효화가 들어오는 상황
        self.assertFalse(self.cache.is_current(snap))

    def test_is_current_is_false_when_the_entry_is_gone(self):
        snap = self.cache.snapshot(UID)
        self.cache.clear()
        self.assertFalse(self.cache.is_current(snap))


class TestEpochFence(_CacheTest):
    def test_bump_allocates_a_new_epoch_and_drops_observations(self):
        e = self.epoch()
        self.cache.put_premium(_premium(e), expected_epoch=e)
        self.cache.put_identity(_identity(e), expected_epoch=e)
        self.assertTrue(self.cache.bump(UID))
        snap = self.cache.snapshot(UID)
        self.assertNotEqual(snap.epoch, e)
        self.assertIsNone(snap.premium)
        self.assertIsNone(snap.identity)

    def test_stale_epoch_put_is_rejected(self):
        """검증 시작 → webhook invalidate → 구 검증 완료. 그 쓰기는 거부돼야 한다."""
        captured = self.epoch()
        self.cache.bump(UID)
        result = self.cache.put_premium(_premium(captured), expected_epoch=captured)
        self.assertIsInstance(result, PutRejected)
        self.assertEqual(result.reason, RejectReason.STALE_EPOCH)
        self.assertIsNone(self.cache.snapshot(UID).premium)

    def test_rejected_put_does_not_clobber_a_fresh_record(self):
        """⛔ 핵심 — lookup 시점 검사만으로는 못 막는다(§A4 2026-07-27).

        구 owner가 **쓰기 자체를 하면** 이미 기록된 fresh 항목이 파괴된다.
        """
        stale = self.epoch()
        self.cache.bump(UID)
        current = self.epoch()
        fresh = _premium(current, active=True, at=200.0)
        self.cache.put_premium(fresh, expected_epoch=current)

        rejected = self.cache.put_premium(
            _premium(stale, active=False, at=100.0), expected_epoch=stale
        )
        self.assertIsInstance(rejected, PutRejected)
        self.assertEqual(self.cache.snapshot(UID).premium, fresh)

    def test_put_without_a_snapshot_is_rejected_not_created(self):
        """⛔ ABA 방지 — `put`이 없는 항목을 만들면 축출 후 stale write가 통과한다."""
        result = self.cache.put_premium(_premium(1), expected_epoch=1)
        self.assertIsInstance(result, PutRejected)
        self.assertEqual(result.reason, RejectReason.UNKNOWN_UID)
        self.assertIsNone(self.cache.snapshot(UID).premium)

    def test_epoch_is_not_reused_after_the_entry_disappears(self):
        """⛔ 실측 재현 — uid별 0-기반이면 항목이 사라진 뒤 stale put이 **통과**했다."""
        captured = self.epoch()
        self.cache.clear()
        self.cache.snapshot(UID)  # 재생성
        result = self.cache.put_premium(_premium(captured), expected_epoch=captured)
        self.assertIsInstance(result, PutRejected)

    def test_epoch_is_not_reused_when_a_bump_preceded_the_disappearance(self):
        """⛔ 실측 재현 — `bump`가 전역 시퀀스를 쓰지 않으면 여기서 ABA가 난다.

        bump가 `uid별 +1`이면 전역 카운터는 그대로여서, 항목 소멸 후 재생성 시 전역 카운터가
        **bump가 만든 값과 같은 값**을 내준다. 실측: captured=2, recreated=2 → `PutAccepted`.
        (bump 없는 위 테스트만으로는 이 경로를 못 밟는다.)
        """
        self.cache.snapshot(UID)
        self.cache.bump(UID)
        captured = self.epoch()
        self.cache.clear()
        self.cache.snapshot(UID)  # 재생성
        result = self.cache.put_premium(_premium(captured), expected_epoch=captured)
        self.assertIsInstance(result, PutRejected)

    def test_bump_on_an_unknown_uid_is_a_noop(self):
        """webhook은 alias마다 돈다 — 연결 없는 사용자로 항목을 새게 하지 않는다."""
        self.assertFalse(self.cache.bump("never-seen"))

    def test_bump_is_per_uid(self):
        e = self.epoch("a")
        self.cache.put_premium(_premium(e, uid="a"), expected_epoch=e)
        self.cache.snapshot("b")
        self.cache.bump("b")
        self.assertIsNotNone(self.cache.snapshot("a").premium)
        self.assertEqual(self.cache.snapshot("a").epoch, e)


class TestProgrammingErrorsRaise(_CacheTest):
    """§A6-1 — programming 오류는 verdict가 아니다. 캐시하지도, wire 오류로 접지도 않는다."""

    def test_mismatched_observation_epoch_raises(self):
        e = self.epoch()
        with self.assertRaises(ValueError):
            self.cache.put_premium(_premium(e + 999), expected_epoch=e)

    def test_mismatched_uid_raises(self):
        e = self.epoch()
        with self.assertRaises(ValueError):
            self.cache.put_premium(_premium(e, uid="other"), expected_epoch=e, key=UID)

    def test_wall_clock_instead_of_monotonic_raises(self):
        """⛔ 실측 — 받아주면 역행 가드가 그 값에 눌러앉아 uid가 **영구 브릭**된다.

        오염 후 정직한 재검증이 전부 거부됐고 `derive_verdict`는 매 요청 raise했다.
        """
        e = self.epoch()
        with self.assertRaises(ValueError):
            self.cache.put_premium(_premium(e, at=1.79e9), expected_epoch=e)

    def test_nan_raises(self):
        """⛔ 실측 — NaN 비교는 **항상 False**라 역행 가드를 통째로 무장 해제한다."""
        e = self.epoch()
        with self.assertRaises(ValueError):
            self.cache.put_premium(_premium(e, at=math.nan), expected_epoch=e)

    def test_infinity_raises(self):
        e = self.epoch()
        with self.assertRaises(ValueError):
            self.cache.put_premium(_premium(e, at=math.inf), expected_epoch=e)

    def test_negative_raises(self):
        e = self.epoch()
        with self.assertRaises(ValueError):
            self.cache.put_premium(_premium(e, at=-1.0), expected_epoch=e)


class TestMonotonicGuard(_CacheTest):
    """같은 epoch 안에서도 **역행 쓰기**를 막아야 한다.

        A 시작(t0) → B 시작(t1) → B 완료(t2, inactive) → A 완료(t3, t0의 active)

    A가 B를 덮으면 **취소된 구독이 freshness 창만큼 되살아난다**(보안 방향 손실).
    """

    def test_older_observation_does_not_overwrite_newer(self):
        e = self.epoch()
        newer = _premium(e, active=False, at=200.0)
        self.cache.put_premium(newer, expected_epoch=e)
        result = self.cache.put_premium(_premium(e, active=True, at=100.0), expected_epoch=e)
        self.assertIsInstance(result, PutRejected)
        self.assertEqual(result.reason, RejectReason.REGRESSION)
        self.assertEqual(self.cache.snapshot(UID).premium, newer)

    def test_regression_and_stale_epoch_are_distinguishable(self):
        """거부 사유가 다르면 호출자의 다음 행동도 다르다 — 하나로 뭉치면 알 수 없다."""
        e = self.epoch()
        self.cache.put_premium(_premium(e, at=200.0), expected_epoch=e)
        regression = self.cache.put_premium(_premium(e, at=100.0), expected_epoch=e)
        self.cache.bump(UID)
        stale = self.cache.put_premium(_premium(e, at=300.0), expected_epoch=e)
        self.assertNotEqual(regression.reason, stale.reason)

    def test_newer_observation_overwrites(self):
        e = self.epoch()
        self.cache.put_premium(_premium(e, active=True, at=100.0), expected_epoch=e)
        newer = _premium(e, active=False, at=200.0)
        self.assertIsInstance(self.cache.put_premium(newer, expected_epoch=e), PutAccepted)
        self.assertEqual(self.cache.snapshot(UID).premium, newer)

    def test_identical_observation_at_equal_timestamp_is_idempotent(self):
        """동률을 받는 근거는 **"같은 관측의 재기록은 무해하다"**뿐이다."""
        e = self.epoch()
        self.cache.put_premium(_premium(e, at=100.0), expected_epoch=e)
        self.assertIsInstance(
            self.cache.put_premium(_premium(e, at=100.0), expected_epoch=e), PutAccepted
        )

    def test_conflict_invalidates_both_directions_premium(self):
        """⛔ 실측 재현 — "먼저 기록된 쪽 유지"는 **순서 의존**이라 보수적이지 않았다.

            inactive→active → inactive 생존 (막힘)
            active→inactive → **active 생존 = 해지가 막힌다**

        어느 값도 신뢰할 수 없으므로 둘 다 버리고 재검증을 강제한다. **양방향 모두** 검사한다.
        """
        for first_active, second_active in ((False, True), (True, False)):
            with self.subTest(order=f"{first_active}→{second_active}"):
                cache = StrictObservationCache()
                e = cache.snapshot(UID).epoch
                cache.put_premium(_premium(e, active=first_active, at=100.0), expected_epoch=e)
                result = cache.put_premium(_premium(e, active=second_active, at=100.0), expected_epoch=e)
                self.assertIsInstance(result, PutRejected)
                self.assertEqual(result.reason, RejectReason.CONFLICT)
                snap = cache.snapshot(UID)
                self.assertIsNone(snap.premium, "충돌 후 어느 값도 남으면 안 된다")
                self.assertNotEqual(snap.epoch, e, "재검증을 강제하려면 epoch가 전진해야 한다")

    def test_conflict_invalidates_both_directions_identity(self):
        """⛔ 실측 재현 — `enabled→disabled`에서 **계정 비활성화가 막혔다**."""
        for first_disabled, second_disabled in ((True, False), (False, True)):
            with self.subTest(order=f"{first_disabled}→{second_disabled}"):
                cache = StrictObservationCache()
                e = cache.snapshot(UID).epoch
                cache.put_identity(_identity(e, disabled=first_disabled, at=100.0), expected_epoch=e)
                result = cache.put_identity(_identity(e, disabled=second_disabled, at=100.0), expected_epoch=e)
                self.assertIsInstance(result, PutRejected)
                self.assertEqual(result.reason, RejectReason.CONFLICT)
                snap = cache.snapshot(UID)
                self.assertIsNone(snap.identity, "충돌 후 어느 값도 남으면 안 된다")
                self.assertNotEqual(snap.epoch, e)

    def test_conflict_fences_in_flight_writes(self):
        """무효화이므로 그 epoch를 캡처한 진행 중 검증은 전부 거부돼야 한다."""
        e = self.epoch()
        self.cache.put_premium(_premium(e, active=True, at=100.0), expected_epoch=e)
        self.cache.put_premium(_premium(e, active=False, at=100.0), expected_epoch=e)  # 충돌
        late = self.cache.put_identity(_identity(e, at=300.0), expected_epoch=e)
        self.assertIsInstance(late, PutRejected)
        self.assertEqual(late.reason, RejectReason.STALE_EPOCH)

    def test_conflict_drops_the_other_concern_too(self):
        """epoch는 UID당 하나라 무효화는 두 concern을 함께 덮는다 — 과잉이지만 fail-closed다."""
        e = self.epoch()
        self.cache.put_identity(_identity(e, at=50.0), expected_epoch=e)
        self.cache.put_premium(_premium(e, active=True, at=100.0), expected_epoch=e)
        self.cache.put_premium(_premium(e, active=False, at=100.0), expected_epoch=e)  # 충돌
        self.assertIsNone(self.cache.snapshot(UID).identity)

    def test_conflict_is_distinguishable_from_regression(self):
        """호출자의 다음 행동이 다르다 — 충돌은 시계 해상도/중복 검증 신호다."""
        e = self.epoch()
        self.cache.put_premium(_premium(e, active=False, at=100.0), expected_epoch=e)
        conflict = self.cache.put_premium(_premium(e, active=True, at=100.0), expected_epoch=e)

        other = StrictObservationCache()
        e2 = other.snapshot(UID).epoch
        other.put_premium(_premium(e2, active=False, at=100.0), expected_epoch=e2)
        regression = other.put_premium(_premium(e2, active=False, at=50.0), expected_epoch=e2)
        self.assertNotEqual(conflict.reason, regression.reason)

    def test_guard_is_per_concern(self):
        e = self.epoch()
        self.cache.put_premium(_premium(e, at=500.0), expected_epoch=e)
        self.assertIsInstance(
            self.cache.put_identity(_identity(e, at=100.0), expected_epoch=e), PutAccepted
        )

    def test_bump_clears_the_monotonic_floor(self):
        """무효화 후에는 더 오래된 시각도 받아야 한다 — 아니면 재검증 결과가 영영 못 쓰인다."""
        e = self.epoch()
        self.cache.put_premium(_premium(e, at=500.0), expected_epoch=e)
        self.cache.bump(UID)
        e2 = self.epoch()
        self.assertIsInstance(
            self.cache.put_premium(_premium(e2, at=100.0), expected_epoch=e2), PutAccepted
        )


class _BlockingObservation:
    """lock 안에서 **멈추는** 관측 스탠드인.

    `_put_locked`는 역행 검사에서 `observation.verified_at_mono`를 읽는다. 그 읽기가 블록하면
    스레드는 **lock을 쥔 채** 멈춘다 — 상호배제를 결정론적으로 관찰할 이음매다.
    """

    def __init__(self, uid, epoch, entered, release):
        self.uid = uid
        self.epoch = epoch
        self._entered = entered
        self._release = release
        self._reads = 0

    @property
    def verified_at_mono(self):
        self._reads += 1
        if self._reads == 2:  # 1회차는 lock 밖 검증(_require_match), 2회차가 lock 안
            self._entered.set()
            self._release.wait(timeout=10)
        return 999.0


class TestSnapshotCoherenceTripWire(unittest.TestCase):
    """`snapshot`은 두 concern을 **한 번의 lock 획득 안에서** 읽어야 한다.

    따로 획득하면 그 사이 `bump`가 끼어 premium은 epoch N, identity는 N+1인 — 실제로는
    동시에 존재한 적 없는 — 쌍이 조립되고, 그 쌍으로 `derive_verdict`가 `Active`를 내면
    무효화된 관측으로 lease가 나간다.

    ⚠️ 이건 **구조적** 성질이라 밖에서 관찰할 수 없다. 이음매를 심어 봐도 블록 지점이 항상
    어느 한 획득 **안**이라, 분리 획득이든 단일 획득이든 `bump`는 똑같이 대기한다 —
    구분이 안 된다(실측). 그래서 계획이 A1에서 쓴 것과 같은 **AST trip-wire**로 잠근다.
    """

    def test_snapshot_acquires_the_lock_exactly_once(self):
        import ast
        import inspect

        import app.strict_cache as module

        tree = ast.parse(inspect.getsource(module))
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "snapshot"
        )
        acquisitions = [n for n in ast.walk(fn) if isinstance(n, (ast.With, ast.AsyncWith))]
        self.assertEqual(
            len(acquisitions),
            1,
            "snapshot이 lock을 두 번 이상 잡으면 그 사이가 일관성 구멍이 된다",
        )


class TestMutualExclusion(unittest.TestCase):
    """경쟁은 확률적이라 "동시에 두드리기"로는 lock 제거를 못 잡는다(GIL 때문에 dict 연산이
    원자적으로 보인다). 한 스레드를 lock 안에 붙잡고 다른 스레드가 **진입 못 함**을 관찰한다.
    """

    def test_bump_cannot_interleave_with_an_in_progress_put(self):
        cache = StrictObservationCache()
        epoch = cache.snapshot(UID).epoch
        cache.put_premium(_premium(epoch, at=100.0), expected_epoch=epoch)

        entered, release, bump_done = threading.Event(), threading.Event(), threading.Event()
        blocker = _BlockingObservation(UID, epoch, entered, release)

        writer = threading.Thread(target=lambda: cache.put_premium(blocker, expected_epoch=epoch))
        writer.start()
        self.assertTrue(entered.wait(timeout=5), "writer가 lock 구간에 진입하지 못했다")

        bumper = threading.Thread(target=lambda: (cache.bump(UID), bump_done.set()))
        bumper.start()
        try:
            self.assertFalse(
                bump_done.wait(timeout=0.5), "lock을 쥔 put 중에 bump가 끼어들었다 — 상호배제 없음"
            )
        finally:
            release.set()
            writer.join(timeout=5)
            bumper.join(timeout=5)

        self.assertTrue(bump_done.is_set(), "release 후 bump가 완료되지 않았다 (deadlock)")
        self.assertIsNone(cache.snapshot(UID).premium)


class TestThreadSafety(unittest.TestCase):
    """`threading.Lock`이어야 하는 이유: 리포에 `asyncio.to_thread`가 86곳 있고 동기 DB 조회
    (`app/entitlements.py:28 has_entitlement`)가 그 안으로 들어갈 후보다. `asyncio.Lock`은
    스레드를 직렬화하지 못한다.
    """

    def test_concurrent_puts_and_bumps_never_tear_state(self):
        cache = StrictObservationCache()
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def writer(n: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(200):
                    snap = cache.snapshot(UID)
                    cache.put_premium(
                        _premium(snap.epoch, at=float(n * 1000 + i)), expected_epoch=snap.epoch
                    )
                    if i % 50 == 0:
                        cache.bump(UID)
            except BaseException as exc:  # noqa: BLE001 — 스레드 예외를 본 스레드로 옮긴다
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertFalse(any(t.is_alive() for t in threads), "스레드가 lock에 갇혔다")
        snap = cache.snapshot(UID)
        if snap.premium is not None:
            self.assertEqual(snap.premium.epoch, snap.epoch, "찢어진 상태")


class TestIsolationHelpers(unittest.TestCase):
    def test_clear_resets_everything(self):
        cache = StrictObservationCache()
        e = cache.snapshot(UID).epoch
        cache.put_premium(_premium(e), expected_epoch=e)
        cache.clear()
        self.assertIsNone(cache.snapshot(UID).premium)


if __name__ == "__main__":
    unittest.main()
