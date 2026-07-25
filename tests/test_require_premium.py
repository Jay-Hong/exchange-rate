"""`require_premium` fail-closed 계약 (ADR-039 §8.1 E3 후속, codex).

구 코드는 "PENDING·INACTIVE가 **아니면** True"였다. 현재 `PremiumStatus`는 정확히 3값이고
`verify_premium_status`의 모든 return이 그 3값 중 하나라 오늘은 `ACTIVE → True`와 동치지만,
**새 상태가 추가되면 그 상태가 자동으로 프리미엄 통과**가 된다(fail-open). E3로 이 helper가
topic snapshot 게이트까지 떠받치게 되면서 그 잠재 결함의 영향 범위가 넓어졌다.

이 파일이 잠그는 것:
1. 알려진 3상태 × `allow_empty` 2값 = 6-case 매트릭스 (**변경 전후 동일** = behavior-change-0 영수증)
2. 알 수 없는 상태는 **fail-closed**이되 `allow_empty` 계약을 보존한다
   (blanket 503은 `allow_empty=True` read endpoint를 잠재 fail-open에서 *실제 장애*로 바꾼다)
3. enum 닫힌집합 trip-wire — 상태를 추가하면 여기서 먼저 red가 되어 위 정책을 재검토하게 만든다

helper를 **직접** 호출한다(16개 endpoint TestClient 매트릭스는 FastAPI 재검증일 뿐).
"""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

# conftest.py가 firebase stub + DATABASE_URL=sqlite를 import 전에 설정.
from app.main import require_premium
from app.subscription import PremiumStatus


def _status(value):
    return patch("app.main.verify_premium_status", new=AsyncMock(return_value=value))


class TestRequirePremiumKnownStates(unittest.IsolatedAsyncioTestCase):
    """6-case 매트릭스 — 이 6건이 변경 전후 동일해야 behavior-change-0."""

    async def test_active_allows_write(self):
        with _status(PremiumStatus.ACTIVE):
            self.assertIs(await require_premium("u", allow_empty=False), True)

    async def test_active_allows_read(self):
        with _status(PremiumStatus.ACTIVE):
            self.assertIs(await require_premium("u", allow_empty=True), True)

    async def test_inactive_write_403(self):
        with _status(PremiumStatus.INACTIVE):
            with self.assertRaises(HTTPException) as ctx:
                await require_premium("u", allow_empty=False)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_inactive_read_returns_false(self):
        """read endpoint는 403이 아니라 '빈 목록'으로 응답한다(기존 정책)."""
        with _status(PremiumStatus.INACTIVE):
            self.assertIs(await require_premium("u", allow_empty=True), False)

    async def test_pending_write_503_with_retry_after(self):
        with _status(PremiumStatus.PENDING):
            with self.assertRaises(HTTPException) as ctx:
                await require_premium("u", allow_empty=False)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("Retry-After", ctx.exception.headers or {})

    async def test_pending_read_also_503(self):
        """PENDING은 '판정 중'이라 allow_empty와 무관하게 재시도 대상 — 빈 목록으로 접지 않는다."""
        with _status(PremiumStatus.PENDING):
            with self.assertRaises(HTTPException) as ctx:
                await require_premium("u", allow_empty=True)
        self.assertEqual(ctx.exception.status_code, 503)


class TestRequirePremiumUnknownState(unittest.IsolatedAsyncioTestCase):
    """알 수 없는 상태 = fail-closed. 현 enum 3값에선 도달 불가 — 미래 상태 추가에 대한 가드다.

    주입값은 미래에 실제로 생길 법한 형태(`PremiumStatus`에 없는 문자열)를 쓴다.
    ⚠️ `PremiumStatus(str, Enum)`이라 `PremiumStatus.ACTIVE == "active"`가 **True**다 —
    주입 문자열이 기존 3값과 겹치면 테스트가 조용히 통과한다.
    """

    _UNKNOWN = "suspended"

    async def test_unknown_state_write_is_denied(self):
        with _status(self._UNKNOWN):
            with self.assertRaises(HTTPException) as ctx:
                await require_premium("u", allow_empty=False)
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_unknown_state_read_returns_false_not_error(self):
        """fail-closed이되 read endpoint는 **빈 목록**으로 계속 동작해야 한다.

        여기서 503을 던지면 `allow_empty=True` 호출부(알림/로그 조회 6곳)가 잠재 fail-open에서
        실제 장애로 바뀐다 — 가드가 만든 회귀가 가드가 막는 결함보다 커진다.
        """
        with _status(self._UNKNOWN):
            self.assertIs(await require_premium("u", allow_empty=True), False)

    async def test_unknown_state_is_logged(self):
        """도달 불가 분기가 조용히 지나가면 운영이 enum 확장 사고를 못 본다."""
        with _status(self._UNKNOWN), \
             patch("app.main.logger") as mock_logger:
            await require_premium("u", allow_empty=True)
        self.assertTrue(mock_logger.error.called)

    async def test_injection_value_is_actually_unknown(self):
        """위 세 테스트가 vacuous하지 않음을 보증 — 주입값이 기존 enum과 같으면 안 된다."""
        self.assertNotIn(self._UNKNOWN, {s.value for s in PremiumStatus})


class TestPremiumStatusClosedSet(unittest.TestCase):
    """enum 닫힌집합 trip-wire — 상태를 추가하면 여기서 먼저 red.

    `require_premium`의 fail-closed 분기는 '알 수 없는 상태'를 거부로 처리한다. 새 상태가
    실제로는 **허용**되어야 한다면(예: TRIAL) 이 테스트가 그 판단을 강제로 거치게 만든다.
    """

    def test_known_states_are_exactly_three(self):
        self.assertEqual({s.value for s in PremiumStatus},
                         {"active", "inactive", "pending"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
