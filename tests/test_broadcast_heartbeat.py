"""broadcast heartbeat — **scheduler 가 도는가**를 재는가, 아니면 전송 성공만 보는가.

⛔ 처음 만들었을 때 `age_seconds` 를 `last_broadcast_time`(실제 전송 **성공** 시각) 기준으로
   냈다. 그런데 "연결 없음"·"변경 없음"은 **정상 스킵**이라 그 값이 갱신되지 않는다 —
   연결 0인 canary baseline 60초에서 **정상 서버를 'broadcast 정지'로 오판**하고,
   watchdog 이 시작하자마자 창을 닫았을 경로다.
→ `last_cycle_time` 은 성공·실패·스킵 **전부** 갱신한다. 멈추는 경우는 cycle 자체가 죽었을 때뿐.
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.admin.stats import BroadcastStats, broadcast_stats
from app.main import app, verify_admin

PATH = "/admin/api/broadcast-heartbeat"


def _fresh() -> BroadcastStats:
    return BroadcastStats()


# ── cycle heartbeat 의미 ────────────────────────────────────────────────────


def test_no_connections_skip_keeps_the_heartbeat_fresh():
    """⛔ **이것이 이 필드가 존재하는 이유다.** 연결 0은 정상이고, 그때도 cycle 은 돈다."""
    stats = _fresh()
    for _ in range(30):
        stats.record_skip(reason="no_connections")
    assert stats.last_cycle_time is not None, "정상 스킵인데 heartbeat 이 갱신되지 않았다"
    assert stats.last_broadcast_time is None, "스킵은 '전송 성공'이 아니다"


def test_no_changes_skip_keeps_the_heartbeat_fresh():
    stats = _fresh()
    stats.record_skip(reason="no_changes")
    assert stats.last_cycle_time is not None


def test_failure_also_updates_the_cycle_heartbeat():
    """⚠️ 실패해도 **cycle 은 돌았다** — 실패 자체는 ERROR 로그 조건이 따로 잡는다.
    여기서 heartbeat 을 멈추면 같은 사건에 중단 사유가 둘 붙어 원인 귀속이 흐려진다."""
    stats = _fresh()
    stats.record_failure("boom")
    assert stats.last_cycle_time is not None
    assert stats.last_broadcast_time is None


def test_success_updates_both():
    stats = _fresh()
    stats.record_success(data_size_bytes=100, rate_count=3)
    assert stats.last_cycle_time is not None and stats.last_broadcast_time is not None


def test_heartbeat_goes_stale_only_when_the_cycle_itself_stops():
    """⛔ 호출이 아예 멈춘 경우 — 그때만 stale 이어야 한다."""
    stats = _fresh()
    stats.record_skip(reason="no_connections")
    stats.last_cycle_time = datetime.now() - timedelta(seconds=31)
    age = (datetime.now() - stats.last_cycle_time).total_seconds()
    assert age >= 30, "cycle 이 멈췄는데 stale 로 보이지 않는다"


# ── endpoint ────────────────────────────────────────────────────────────────


class _Client:
    def __enter__(self):
        app.dependency_overrides[verify_admin] = lambda: "admin"
        self.client = TestClient(app)
        return self.client

    def __exit__(self, *exc):
        app.dependency_overrides.pop(verify_admin, None)


def test_endpoint_reports_age_from_the_cycle_not_from_the_last_send():
    """⛔ endpoint 가 `last_broadcast_time` 을 쓰면 정상 스킵 구간이 전부 '정지'로 보인다."""
    saved = (broadcast_stats.last_cycle_time, broadcast_stats.last_broadcast_time)
    try:
        broadcast_stats.last_cycle_time = datetime.now()
        broadcast_stats.last_broadcast_time = datetime.now() - timedelta(seconds=300)
        with _Client() as client:
            body = client.get(PATH).json()
        assert body["age_seconds"] is not None and body["age_seconds"] < 5, \
            "전송 성공 시각을 heartbeat 으로 쓰고 있다"
        assert body["last_broadcast_time"] is not None, "전송 시각도 함께 보여야 한다(진단용)"
    finally:
        broadcast_stats.last_cycle_time, broadcast_stats.last_broadcast_time = saved


def test_endpoint_reports_null_age_when_no_cycle_has_run():
    """⚠️ `null` 은 "cycle 이 한 번도 안 돌았다" — **0 과 다르다**(0 으로 접으면 정지를 정상으로 읽는다)."""
    saved = broadcast_stats.last_cycle_time
    try:
        broadcast_stats.last_cycle_time = None
        with _Client() as client:
            body = client.get(PATH).json()
        assert body["age_seconds"] is None and body["last_cycle_time"] is None
    finally:
        broadcast_stats.last_cycle_time = saved
