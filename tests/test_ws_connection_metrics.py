"""`/ws` 연결 계측 — **리스트와 카운터가 어긋나지 않는가**를 행동으로 본다.

⛔ 이 계측의 유일한 실패 모드는 "제거 경로가 여럿이라 한쪽에서만 감소가 빠지는 것"이다.
   그래서 **제거가 일어나는 네 방향**을 전부 잠근다: 정상 종료 / broadcast 실패 제거 /
   초기 전송 예외(등록 전) / 중복 disconnect.
"""

import asyncio

import pytest

from app import ws_connection_metrics
from app.main import ConnectionManager


class _FakeWebSocket:
    """`accept`/`send_json` 만 있는 최소 대역. 실패를 주입할 수 있다."""

    def __init__(self, ip: str | None = "203.0.113.7", fail_send: bool = False):
        self.headers = {} if ip is None else {"x-real-ip": ip}
        self.fail_send = fail_send
        self.sent: list = []

    async def accept(self):
        return None

    async def send_json(self, message):
        if self.fail_send:
            raise RuntimeError("전송 실패")
        self.sent.append(message)


def _run(coro):
    return asyncio.run(coro)


def _metrics(manager: ConnectionManager) -> dict:
    return manager.connection_metrics()


# ── 제거 4방향 ──────────────────────────────────────────────────────────────


def test_normal_disconnect_decrements_exactly_once():
    manager = ConnectionManager()
    ws = _FakeWebSocket()
    _run(manager.connect(ws))
    assert _metrics(manager)["active_connections"] == 1
    assert _metrics(manager)["per_ip"]["distinct_ips"] == 1

    manager.disconnect(ws)
    m = _metrics(manager)
    assert m["active_connections"] == 0
    assert m["registered_sockets"] == 0
    assert m["per_ip"]["distinct_ips"] == 0, "IP map 이 단조 증가하면 사실상 IP history 가 된다"


def test_broadcast_failure_removal_goes_through_disconnect():
    """⛔ 한때 broadcast 실패 처리가 `active_connections.remove()` 를 **직접** 불렀다.
    그 경로만 계측을 우회하면 gauge 는 줄어드는데 IP 카운터는 남아 **영구히 어긋난다**."""
    manager = ConnectionManager()
    good = _FakeWebSocket(ip="203.0.113.7")
    bad = _FakeWebSocket(ip="198.51.100.9", fail_send=True)
    _run(manager.connect(good))
    _run(manager.connect(bad))
    assert _metrics(manager)["per_ip"]["distinct_ips"] == 2

    _run(manager.broadcast({"type": "x"}))

    m = _metrics(manager)
    assert m["active_connections"] == 1, "실패한 연결이 제거되지 않았다"
    assert m["registered_sockets"] == 1, "리스트만 줄고 등록 상태가 남았다 — 어긋났다"
    assert m["per_ip"]["distinct_ips"] == 1, "IP 카운터가 리스트와 어긋났다"


def test_disconnect_before_registration_is_a_noop():
    """초기 전송 예외 등으로 **등록 전에** 정리가 도는 경우 — 카운터를 건드리면 음수가 된다."""
    manager = ConnectionManager()
    never_connected = _FakeWebSocket()
    manager.disconnect(never_connected)
    m = _metrics(manager)
    assert m["active_connections"] == 0
    assert m["registered_sockets"] == 0
    assert m["per_ip"]["max_connections_per_ip"] == 0


def test_duplicate_disconnect_decrements_only_once():
    manager = ConnectionManager()
    a = _FakeWebSocket(ip="203.0.113.7")
    b = _FakeWebSocket(ip="203.0.113.7")          # 같은 IP 2연결
    _run(manager.connect(a))
    _run(manager.connect(b))
    assert _metrics(manager)["per_ip"]["max_connections_per_ip"] == 2

    manager.disconnect(a)
    manager.disconnect(a)                          # 중복
    m = _metrics(manager)
    assert m["active_connections"] == 1, "중복 호출이 남은 연결까지 지웠다"
    assert m["per_ip"]["max_connections_per_ip"] == 1, "중복 호출이 IP 카운터를 두 번 깎았다"


# ── IP 출처 ─────────────────────────────────────────────────────────────────


def test_client_ip_uses_x_real_ip_and_ignores_forwarded_for():
    """⛔ `X-Forwarded-For` 첫 값은 **클라가 보낸 값**이 앞에 붙어(nginx 는 append) spoof 가 된다.
    `/ws` nginx 는 `X-Real-IP` 를 `$remote_addr` 로 **덮어쓴다** — 그것만 믿는다."""
    headers = {"x-real-ip": "203.0.113.7", "x-forwarded-for": "1.2.3.4, 203.0.113.7"}
    assert ws_connection_metrics.client_ip_from_headers(headers) == "203.0.113.7"


@pytest.mark.parametrize("headers", [
    {},                                   # nginx 우회 직접 접근
    {"x-real-ip": ""},
    {"x-real-ip": "not-an-ip"},
    {"x-forwarded-for": "203.0.113.7"},   # XFF 만 있으면 신뢰하지 않는다
])
def test_unusable_ip_falls_back_to_unknown(headers):
    assert ws_connection_metrics.client_ip_from_headers(headers) == ws_connection_metrics.UNKNOWN_IP


# ── 노출 계약 ───────────────────────────────────────────────────────────────


def test_snapshot_never_exposes_raw_ips():
    """⛔ 원시 IP 를 endpoint·로그로 내보내지 않는다 — 히스토그램/최댓값/총계만."""
    manager = ConnectionManager()
    for ip in ("203.0.113.7", "203.0.113.7", "198.51.100.9"):
        _run(manager.connect(_FakeWebSocket(ip=ip)))
    snapshot = _metrics(manager)
    rendered = repr(snapshot)
    assert "203.0.113.7" not in rendered and "198.51.100.9" not in rendered
    assert snapshot["per_ip"]["histogram"] == {"2": 1, "1": 1}
    assert snapshot["per_ip"]["max_connections_per_ip"] == 2


# ── handshake 버킷 ──────────────────────────────────────────────────────────


def test_handshake_peak_needs_buckets_not_a_running_total():
    """⛔ 누적 합계로는 **피크**를 낼 수 없다 — 재연결 폭주가 정상 상한을 정하는데."""
    buckets = ws_connection_metrics.HandshakeBuckets(bucket_seconds=10, buckets_kept=3)
    for _ in range(5):
        buckets.record(now=0)          # 첫 버킷에 5건
    buckets.record(now=15)             # 다음 버킷에 1건
    snapshot = buckets.snapshot(now=15)
    assert snapshot["max_in_bucket"] == 5, "피크가 보이지 않는다"
    assert snapshot["current_bucket"] == 1


def test_handshake_buckets_are_bounded():
    """⛔ 무제한 시계열을 만들지 않는다 — 메모리가 유입에 비례해 자란다."""
    buckets = ws_connection_metrics.HandshakeBuckets(bucket_seconds=10, buckets_kept=3)
    for i in range(100):
        buckets.record(now=i * 10)
    assert buckets.snapshot(now=990)["buckets_present"] <= 3
