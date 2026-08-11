"""`/ws` 연결 계측 — **리스트와 카운터가 어긋나지 않는가**를 행동으로 본다.

⛔ 이 계측의 유일한 실패 모드는 "제거 경로가 여럿이라 한쪽에서만 감소가 빠지는 것"이다.
   그래서 **제거가 일어나는 네 방향**을 전부 잠근다: 정상 종료 / broadcast 실패 제거 /
   초기 전송 예외(등록 전) / 중복 disconnect.
"""

import asyncio
import ast
import inspect
import textwrap
from unittest.mock import patch

import pytest

from app import config, main as app_main, ws_connection_metrics
from app.main import ConnectionManager
from app.topic_auth_rollout import TopicAuthRollout, TopicAuthStage


_FX_TOPICS = ("fx:usd-krw", "fx:jpy-krw", "fx:eur-krw")
_USDT_TOPIC = "usdt:krw"


def _rollout() -> TopicAuthRollout:
    return TopicAuthRollout(
        stage=TopicAuthStage.COMPATIBILITY,
        fx_topics=_FX_TOPICS,
        usdt_topic=_USDT_TOPIC,
        started_at_epoch_seconds=0,
    )


def _manager(auth_rollout: TopicAuthRollout | None = None) -> ConnectionManager:
    return ConnectionManager(auth_rollout=auth_rollout or _rollout())


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


def test_connection_manager_requires_a_keyword_only_rollout():
    parameter = inspect.signature(ConnectionManager).parameters["auth_rollout"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


# ── 제거 4방향 ──────────────────────────────────────────────────────────────


def test_normal_disconnect_decrements_exactly_once():
    manager = _manager()
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
    manager = _manager()
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
    manager = _manager()
    never_connected = _FakeWebSocket()
    manager.disconnect(never_connected)
    m = _metrics(manager)
    assert m["active_connections"] == 0
    assert m["registered_sockets"] == 0
    assert m["per_ip"]["max_connections_per_ip"] == 0


def test_duplicate_disconnect_decrements_only_once():
    manager = _manager()
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


def test_disconnect_cleans_rollout_state_before_unregistered_early_return():
    """rollout 관측은 연결 등록 전에도 가능하므로 early return 앞에서 정리해야 한다."""
    manager = _manager()
    websocket = _FakeWebSocket()
    manager.topic_auth_rollout.observe_anonymous_subscribe(websocket, ["fx:usd-krw"])
    assert manager.topic_auth_rollout.snapshot()["active_auth_scoped_connections_tracked"] == 1

    manager.disconnect(websocket)

    assert manager.topic_auth_rollout.snapshot()["active_auth_scoped_connections_tracked"] == 0


def test_rollout_cleanup_failure_does_not_block_core_disconnect():
    manager = _manager()
    websocket = _FakeWebSocket()
    _run(manager.connect(websocket))

    with patch.object(
        manager.topic_auth_rollout, "disconnect", side_effect=RuntimeError("telemetry cleanup")
    ):
        manager.disconnect(websocket)

    metrics = _metrics(manager)
    assert metrics["active_connections"] == 0
    assert metrics["registered_sockets"] == 0
    assert metrics["per_ip"]["distinct_ips"] == 0


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
    manager = _manager()
    for ip in ("203.0.113.7", "203.0.113.7", "198.51.100.9"):
        _run(manager.connect(_FakeWebSocket(ip=ip)))
    snapshot = _metrics(manager)
    rendered = repr(snapshot)
    assert "203.0.113.7" not in rendered and "198.51.100.9" not in rendered
    assert snapshot["per_ip"]["histogram"] == {"2": 1, "1": 1}
    assert snapshot["per_ip"]["max_connections_per_ip"] == 2


def test_unknown_ip_connections_never_pollute_the_nat_distribution():
    """⛔ **`limit_conn` 판단을 정반대로 만드는 결함이었다.** `X-Real-IP` 가 전부 누락된 100 연결이
    `histogram={"100": 1}` 로 보이면 **carrier NAT 한 IP 의 100 연결과 구분되지 않는다** —
    전자는 "IP 데이터가 아예 없다"이고 후자는 "상한을 높게 잡아야 한다"인데."""
    manager = _manager()
    known = [_FakeWebSocket(ip="203.0.113.7") for _ in range(2)]
    unknown = [_FakeWebSocket(ip=None) for _ in range(3)]      # 헤더 누락
    broken = _FakeWebSocket(ip="not-an-ip")                    # 헤더 손상
    for ws in known + unknown + [broken]:
        _run(manager.connect(ws))

    per_ip = _metrics(manager)["per_ip"]
    assert per_ip["unknown_connections"] == 4, "unknown 이 따로 세지지 않는다"
    assert per_ip["distinct_ips"] == 1, "unknown 이 실제 IP 처럼 섞였다"
    assert per_ip["max_connections_per_ip"] == 2, "unknown 이 max 를 오염시켰다"
    assert per_ip["histogram"] == {"2": 1}, "unknown 이 histogram 에 섞였다"


def test_connection_total_equals_known_plus_unknown():
    """⚠️ 이 불변식이 깨지면 어느 쪽 통계도 믿을 수 없다."""
    manager = _manager()
    for ws in [_FakeWebSocket(ip="203.0.113.7"), _FakeWebSocket(ip="198.51.100.9"),
               _FakeWebSocket(ip=None), _FakeWebSocket(ip="203.0.113.7")]:
        _run(manager.connect(ws))
    m = _metrics(manager)
    assert m["per_ip"]["known_connections"] + m["per_ip"]["unknown_connections"] \
        == m["active_connections"]


def test_unknown_connections_do_not_move_known_statistics():
    """반대 방향 — unknown 이 늘어도 known 쪽 숫자는 **그대로여야** 한다."""
    manager = _manager()
    _run(manager.connect(_FakeWebSocket(ip="203.0.113.7")))
    before = _metrics(manager)["per_ip"]
    for _ in range(5):
        _run(manager.connect(_FakeWebSocket(ip=None)))
    after = _metrics(manager)["per_ip"]
    assert after["unknown_connections"] == 5
    for key in ("distinct_ips", "max_connections_per_ip", "histogram", "known_connections"):
        assert after[key] == before[key], f"unknown 이 known 통계({key})를 움직였다"


def test_connect_records_a_handshake_through_the_manager():
    """⛔ **프로덕션 배선을 본다.** `HandshakeBuckets` 를 직접 부르는 테스트만 두면
    `ConnectionManager.connect()` 의 `record()` 를 **지워도 통과한다**(이 세션에서 반복된 형태)."""
    manager = _manager()
    assert _metrics(manager)["handshakes"]["current_bucket"] == 0

    first = _FakeWebSocket(ip="203.0.113.7")
    _run(manager.connect(first))
    assert _metrics(manager)["handshakes"]["current_bucket"] == 1, "connect 가 handshake 를 안 셌다"

    second = _FakeWebSocket(ip="198.51.100.9")
    _run(manager.connect(second))
    assert _metrics(manager)["handshakes"]["current_bucket"] == 2

    # ⚠️ 해제는 handshake 를 되돌리지 않는다 — **유입** 카운터이지 gauge 가 아니다.
    # ⛔ 반드시 **실제 등록된 소켓**을 해제한다. 한때 새 `_FakeWebSocket()` 을 넘겼는데, 그건
    #    `disconnect` 의 "등록된 적 없음" 분기로 빠져 **아무것도 건드리지 않으므로** 단언이
    #    공허했다 — 등록 소켓 해제 시 handshake 를 깎는 구현이 생겨도 통과한다.
    manager.disconnect(first)
    m = _metrics(manager)
    assert m["active_connections"] == 1, "gauge 가 줄지 않았다"
    assert m["handshakes"]["current_bucket"] == 2, "해제가 handshake 유입 카운터를 깎았다"


def test_production_manager_uses_canonical_publisher_topics():
    """`.values()` 누락이나 다른 runtime 배선은 시작 시점에 드러나야 한다."""
    snapshot = app_main.manager.topic_auth_rollout.snapshot()
    actual_topics = set(snapshot["per_topic_attempts"])
    expected_topics = set(app_main.fx_topic_publisher.FX_TOPICS.values()) | {
        app_main.tether_topic_publisher.TETHER_TOPIC
    }

    assert actual_topics == expected_topics
    assert snapshot["stage"] == config.WS_TOPIC_AUTH_STAGE.value


def test_websocket_endpoint_injects_the_manager_owned_rollout():
    """요청 처리와 disconnect/admin 이 서로 다른 runtime 을 보면 연결 계수가 누수된다."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app_main.websocket_endpoint)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "handle_client_message"
    ]
    assert len(calls) == 1, "production handler 호출 위치가 바뀌었다 — 배선 검사를 갱신하라"
    values = {kw.arg: kw.value for kw in calls[0].keywords}
    assert "topic_auth_rollout" in values
    assert ast.unparse(values["topic_auth_rollout"]) == "manager.topic_auth_rollout"


def test_admin_endpoint_merges_rollout_snapshot_and_runtime_flag():
    manager = _manager()
    with patch.object(app_main, "manager", manager), patch.object(
        config, "TOPIC_DISPATCHER_ENABLED", False
    ):
        response = _run(app_main.get_ws_connection_metrics())

    rollout = response["metrics"]["topic_auth_rollout"]
    assert rollout["stage"] == TopicAuthStage.COMPATIBILITY.value
    assert rollout["topic_dispatcher_enabled"] is False
    assert response["metrics"]["active_connections"] == 0


def test_admin_rollout_failure_preserves_connection_metrics():
    manager = _manager()
    with patch.object(app_main, "manager", manager), patch.object(
        manager.topic_auth_rollout, "snapshot", side_effect=RuntimeError("snapshot failed")
    ), patch.object(config, "TOPIC_DISPATCHER_ENABLED", True):
        response = _run(app_main.get_ws_connection_metrics())

    assert response["metrics"]["active_connections"] == 0
    assert response["metrics"]["registered_sockets"] == 0
    assert response["metrics"]["topic_auth_rollout"] == {
        "stage": config.WS_TOPIC_AUTH_STAGE.value,
        "error": "unavailable",
        "topic_dispatcher_enabled": True,
    }


def test_admin_malformed_rollout_snapshot_is_also_isolated():
    manager = _manager()
    with patch.object(app_main, "manager", manager), patch.object(
        manager.topic_auth_rollout, "snapshot", return_value=None
    ):
        response = _run(app_main.get_ws_connection_metrics())

    assert response["metrics"]["active_connections"] == 0
    assert response["metrics"]["topic_auth_rollout"]["error"] == "unavailable"


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
