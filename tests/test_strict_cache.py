"""N-3b strict cache store — WS strict 인가용 관측 저장소.

계약의 근거는 `FREE_TIER_ACCESS_MODEL_PLAN.md` §8.1 A4 / A6-1 / A6-2다.

이 파일은 **test-first**로 작성됐다. 각 테스트는 불변식 하나씩을 잠그고, 그 불변식이
load-bearing인지는 mutation으로 확인한다(구현 커밋 메시지에 결과 기록).
"""
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
    StrictObservationCache,
)

UID = "uid-1"
WATERMARK_MS = 1_700_000_000_000


def _premium(*, uid=UID, active=True, at=100.0, epoch=0):
    return PremiumObservation(uid=uid, active=active, verified_at_mono=at, epoch=epoch)


def _identity(*, uid=UID, disabled=False, at=100.0, epoch=0):
    return IdentityAuthorityFound(
        uid=uid,
        disabled=disabled,
        tokens_valid_after_ms=WATERMARK_MS,
        verified_at_mono=at,
        epoch=epoch,
    )


class TestBasicRoundTrip(unittest.TestCase):
    def setUp(self):
        self.cache = StrictObservationCache()

    def test_new_uid_starts_at_epoch_zero_with_no_observations(self):
        self.assertEqual(self.cache.current_epoch(UID), 0)
        self.assertIsNone(self.cache.get_premium(UID))
        self.assertIsNone(self.cache.get_identity(UID))

    def test_put_with_matching_epoch_is_accepted_and_readable(self):
        obs = _premium()
        result = self.cache.put_premium(obs, expected_epoch=0)
        self.assertEqual(result, PutAccepted(epoch=0))
        self.assertEqual(self.cache.get_premium(UID), obs)

    def test_concerns_are_stored_independently(self):
        self.cache.put_premium(_premium(), expected_epoch=0)
        self.assertIsNone(self.cache.get_identity(UID))
        self.cache.put_identity(_identity(), expected_epoch=0)
        self.assertIsNotNone(self.cache.get_premium(UID))

    def test_uids_are_isolated(self):
        self.cache.put_premium(_premium(uid="a"), expected_epoch=0)
        self.assertIsNone(self.cache.get_premium("b"))

    def test_not_found_identity_round_trips(self):
        obs = IdentityAuthorityNotFound(uid=UID, verified_at_mono=100.0, epoch=0)
        self.assertEqual(self.cache.put_identity(obs, expected_epoch=0), PutAccepted(epoch=0))
        self.assertEqual(self.cache.get_identity(UID), obs)


class TestEpochFence(unittest.TestCase):
    """§8.1 A4 — 게시(put)와 소비(get)를 **둘 다** fence해야 한다."""

    def setUp(self):
        self.cache = StrictObservationCache()

    def test_bump_increments_epoch_and_drops_observations(self):
        self.cache.put_premium(_premium(), expected_epoch=0)
        self.cache.put_identity(_identity(), expected_epoch=0)
        self.assertEqual(self.cache.bump(UID), 1)
        self.assertEqual(self.cache.current_epoch(UID), 1)
        self.assertIsNone(self.cache.get_premium(UID))
        self.assertIsNone(self.cache.get_identity(UID))

    def test_stale_epoch_put_is_rejected(self):
        """검증 시작 → webhook invalidate → 구 검증 완료. 그 쓰기는 거부돼야 한다."""
        captured = self.cache.current_epoch(UID)
        self.cache.bump(UID)
        result = self.cache.put_premium(_premium(epoch=captured), expected_epoch=captured)
        self.assertEqual(result, PutRejected(expected_epoch=0, current_epoch=1))
        self.assertIsNone(self.cache.get_premium(UID))

    def test_rejected_put_does_not_clobber_a_fresh_record(self):
        """⛔ 핵심 — lookup 시점 검사만으로는 이걸 못 막는다(§A4 2026-07-27).

        구 owner가 **쓰기 자체를 하면** 이미 기록된 fresh 항목이 파괴된다. CAS는 쓰기를 막는다.
        """
        stale_epoch = self.cache.current_epoch(UID)
        self.cache.bump(UID)
        fresh = _premium(active=True, at=200.0, epoch=1)
        self.cache.put_premium(fresh, expected_epoch=1)

        rejected = self.cache.put_premium(
            _premium(active=False, at=100.0, epoch=stale_epoch), expected_epoch=stale_epoch
        )

        self.assertIsInstance(rejected, PutRejected)
        self.assertEqual(self.cache.get_premium(UID), fresh)  # 그대로 살아 있어야 한다

    def test_future_epoch_put_is_also_rejected(self):
        """위조·버그로 앞선 epoch를 들고 와도 통과하면 안 된다 (CAS는 '같음'이지 '이상'이 아니다)."""
        result = self.cache.put_premium(_premium(epoch=5), expected_epoch=5)
        self.assertEqual(result, PutRejected(expected_epoch=5, current_epoch=0))
        self.assertIsNone(self.cache.get_premium(UID))

    def test_bump_is_per_uid(self):
        self.cache.put_premium(_premium(uid="a"), expected_epoch=0)
        self.cache.bump("b")
        self.assertIsNotNone(self.cache.get_premium("a"))
        self.assertEqual(self.cache.current_epoch("a"), 0)


class TestObservationEpochMustMatch(unittest.TestCase):
    """관측이 스스로 들고 있는 `epoch`와 CAS 인자가 어긋나면 **오용**이다.

    두 곳에 같은 상태가 있으면 반드시 어긋난다. 저장 시점에 일치를 강제해 그 가능성을 없앤다.
    """

    def setUp(self):
        self.cache = StrictObservationCache()

    def test_mismatched_observation_epoch_is_rejected_as_programming_error(self):
        with self.assertRaises(ValueError):
            self.cache.put_premium(_premium(epoch=3), expected_epoch=0)

    def test_mismatched_uid_is_rejected_as_programming_error(self):
        with self.assertRaises(ValueError):
            self.cache.put_premium(_premium(uid="other"), expected_epoch=0, key=UID)


class TestMonotonicGuard(unittest.TestCase):
    """같은 epoch 안에서도 **역행 쓰기**를 막아야 한다.

    epoch가 같으면 CAS는 통과한다. 그런데 겹친 두 검증에서 **느린 쪽이 나중에 끝나면**
    오래된 관측이 새 관측을 덮는다:

        A 시작(t0) → B 시작(t1) → B 완료(t2, inactive) → A 완료(t3, t0의 active)

    결과적으로 **취소된 구독이 freshness 창만큼 되살아난다**(보안 방향 손실). 리포의
    USDT Redis `<` 역행 가드와 같은 문제다.
    """

    def setUp(self):
        self.cache = StrictObservationCache()

    def test_older_observation_does_not_overwrite_newer(self):
        newer = _premium(active=False, at=200.0)
        self.cache.put_premium(newer, expected_epoch=0)
        result = self.cache.put_premium(_premium(active=True, at=100.0), expected_epoch=0)
        self.assertIsInstance(result, PutRejected)
        self.assertEqual(self.cache.get_premium(UID), newer)

    def test_newer_observation_overwrites(self):
        self.cache.put_premium(_premium(active=True, at=100.0), expected_epoch=0)
        newer = _premium(active=False, at=200.0)
        self.assertEqual(self.cache.put_premium(newer, expected_epoch=0), PutAccepted(epoch=0))
        self.assertEqual(self.cache.get_premium(UID), newer)

    def test_equal_timestamp_is_accepted_as_refresh(self):
        """동률은 거부하지 않는다 — 같은 관측의 재기록은 무해하고, 거부하면 재시도를 유발한다."""
        first = _premium(at=100.0)
        self.cache.put_premium(first, expected_epoch=0)
        self.assertIsInstance(self.cache.put_premium(_premium(at=100.0), expected_epoch=0), PutAccepted)

    def test_guard_is_per_concern(self):
        """premium의 시각이 identity의 쓰기를 막으면 안 된다."""
        self.cache.put_premium(_premium(at=500.0), expected_epoch=0)
        self.assertIsInstance(self.cache.put_identity(_identity(at=100.0), expected_epoch=0), PutAccepted)

    def test_bump_clears_the_monotonic_floor(self):
        """무효화 후에는 더 오래된 시각의 관측도 받아야 한다 — 아니면 재검증이 영영 못 쓰인다."""
        self.cache.put_premium(_premium(at=500.0), expected_epoch=0)
        self.cache.bump(UID)
        result = self.cache.put_premium(_premium(at=100.0, epoch=1), expected_epoch=1)
        self.assertIsInstance(result, PutAccepted)


class TestThreadSafety(unittest.TestCase):
    """`threading.Lock`이어야 하는 이유는 리포에 `asyncio.to_thread`가 86곳 있고
    동기 DB 조회(`app/entitlements.py:28 has_entitlement`)가 그 안으로 들어갈 후보이기 때문이다.
    `asyncio.Lock`은 스레드를 직렬화하지 못한다.
    """

    def test_concurrent_puts_and_bumps_never_tear_state(self):
        cache = StrictObservationCache()
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def writer(n: int) -> None:
            try:
                barrier.wait(timeout=5)
                for i in range(200):
                    epoch = cache.current_epoch(UID)
                    try:
                        cache.put_premium(
                            _premium(at=float(n * 1000 + i), epoch=epoch), expected_epoch=epoch
                        )
                    except ValueError:
                        pass  # epoch가 그 사이 올라간 경우 — 경쟁의 정상 결과
                    if i % 50 == 0:
                        cache.bump(UID)
            except BaseException as exc:  # noqa: BLE001 - 스레드 예외를 본 스레드로 옮긴다
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertFalse(any(t.is_alive() for t in threads), "스레드가 lock에 갇혔다")
        stored = cache.get_premium(UID)
        if stored is not None:
            # 저장된 게 있으면 반드시 현재 epoch 것이어야 한다 (찢어진 상태 부재)
            self.assertEqual(stored.epoch, cache.current_epoch(UID))


class _BlockingObservation:
    """lock 안에서 **멈추는** 관측 스탠드인.

    `_put_locked`는 역행 검사에서 `observation.verified_at_mono`를 읽는다. 그 읽기가 블록하면
    스레드는 **lock을 쥔 채** 멈춘다 — 상호배제를 결정론적으로 관찰할 수 있는 이음매다.
    """

    def __init__(self, uid, epoch, entered, release):
        self.uid = uid
        self.epoch = epoch
        self._entered = entered
        self._release = release
        self._blocked_once = False

    @property
    def verified_at_mono(self):
        if not self._blocked_once:
            self._blocked_once = True
            self._entered.set()
            self._release.wait(timeout=10)
        return 999.0


class TestMutualExclusion(unittest.TestCase):
    """lock이 **실제로 상호배제를 제공하는지**.

    경쟁은 확률적이라 "동시에 두드려 보기"로는 lock 제거를 못 잡는다(GIL 때문에 dict 연산이
    원자적으로 보인다). 그래서 한 스레드를 lock 안에 붙잡아 두고 다른 스레드가 **진입하지
    못함**을 직접 관찰한다.
    """

    def test_bump_cannot_interleave_with_an_in_progress_put(self):
        cache = StrictObservationCache()
        cache.put_premium(_premium(at=100.0), expected_epoch=0)  # existing 만들기

        entered, release = threading.Event(), threading.Event()
        bump_done = threading.Event()
        blocker = _BlockingObservation(UID, 0, entered, release)

        writer = threading.Thread(target=lambda: cache.put_premium(blocker, expected_epoch=0))
        writer.start()
        self.assertTrue(entered.wait(timeout=5), "writer가 lock 구간에 진입하지 못했다")

        bumper = threading.Thread(target=lambda: (cache.bump(UID), bump_done.set()))
        bumper.start()
        try:
            # writer가 lock을 쥔 동안 bump는 **완료될 수 없다**.
            self.assertFalse(
                bump_done.wait(timeout=0.5),
                "lock을 쥔 put 중에 bump가 끼어들었다 — 상호배제 없음",
            )
        finally:
            release.set()
            writer.join(timeout=5)
            bumper.join(timeout=5)

        self.assertTrue(bump_done.is_set(), "release 후 bump가 완료되지 않았다 (deadlock)")
        self.assertIsNone(cache.get_premium(UID), "bump가 마지막이므로 관측은 비어야 한다")


class TestIsolationHelpers(unittest.TestCase):
    def test_clear_resets_everything(self):
        cache = StrictObservationCache()
        cache.put_premium(_premium(), expected_epoch=0)
        cache.bump(UID)
        cache.clear()
        self.assertEqual(cache.current_epoch(UID), 0)
        self.assertIsNone(self.__class__ and cache.get_premium(UID))


if __name__ == "__main__":
    unittest.main()
