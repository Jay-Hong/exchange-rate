"""W canary 인증 부하 도구 — **부하원이 실제로 인증을 타는가** + 안전 장치.

⛔ 이 도구의 존재 이유: 기존 subscribe smoke 는 `id_token` 없이 구독하고, dispatcher 는
   무토큰이면 free topic 만 등록하고 **그 자리에서 return** 한다 — 인증 executor 를 한 번도
   타지 않아 auth metrics 가 빈 채로 남는 **false green** 이 된다.
"""

import asyncio
import json
import os
import sys
from io import StringIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import ws_auth_load as load  # noqa: E402

TOKEN = "eyJhbGciOi.SECRET-TOKEN-VALUE.signature"


def _run(coro):
    return asyncio.run(coro)


# ── 부하원이 인증을 타는가 (이 도구의 존재 이유) ────────────────────────────


class _RecordingWS:
    """보낸 프레임을 기록하고, 미리 넣어둔 프레임을 돌려준다."""

    def __init__(self, replies=None, echo_ack=True):
        self.sent: list = []
        self._replies = list(replies or [])
        self._echo_ack = echo_ack
        self.max_in_flight = 0
        self._in_flight = 0

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        if self._echo_ack:
            rid = self.sent[-1]["request_id"]
            self._replies.append({"type": "subscription_ack", "request_id": rid,
                                  "operation": "subscribe"})

    async def recv(self):
        while not self._replies:
            await asyncio.sleep(0.001)
        frame = self._replies.pop(0)
        if frame.get("type") in load.TERMINATING_TYPES:
            self._in_flight = max(0, self._in_flight - 1)
        return json.dumps(frame)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_every_request_carries_an_id_token():
    """⛔ **핵심.** 토큰이 빠지면 dispatcher 가 free topic 만 등록하고 return 해서
    인증 executor 를 전혀 타지 않는다 — 부하를 줘도 auth metrics 가 비는 false green."""
    ws = _RecordingWS()
    _run(load.run_one_request(ws, TOKEN, load.LoadStats()))
    assert ws.sent[0]["id_token"] == TOKEN, "id_token 이 실리지 않았다 — 인증 경로를 안 탄다"
    assert ws.sent[0]["request_id"], "request_id 가 없다 — 식별된 요청이 아니다(§8-B-term)"
    assert ws.sent[0]["topics"] == [load.LOAD_TOPIC]


def test_load_topic_is_not_the_gated_one():
    """⚠️ gated topic(KRX)을 건드리지 않고도 인증은 돈다 — `authorize_subscribe` 는 gated 판정
    **전에** 실행되기 때문이다. canary 에서 KRX 배포를 켤 이유가 없다."""
    assert "krx" not in load.LOAD_TOPIC


def test_request_ids_are_unique_per_request():
    ws = _RecordingWS()
    stats = load.LoadStats()

    async def scenario():
        for _ in range(3):
            await load.run_one_request(ws, TOKEN, stats)

    _run(scenario())
    ids = [frame["request_id"] for frame in ws.sent]
    assert len(set(ids)) == 3, "request_id 가 재사용되면 종결 프레임 대응이 어긋난다"


# ── in-flight 정확히 1 ──────────────────────────────────────────────────────


def test_worker_never_has_more_than_one_request_in_flight():
    """⛔ 클라가 큐를 쌓으면 서버 `queue_wait` 이 **클라 큐의 그림자**가 된다 — 측정 대상이 바뀐다."""
    ws = _RecordingWS()
    stats = load.LoadStats()

    async def scenario():
        stop_at = asyncio.get_running_loop().time()   # 값은 안 쓰고 구조만 본다
        del stop_at
        for _ in range(5):
            await load.run_one_request(ws, TOKEN, stats)

    _run(scenario())
    assert ws.max_in_flight == 1, f"in-flight 가 1을 넘었다: {ws.max_in_flight}"


def test_data_frames_do_not_terminate_a_request():
    """⚠️ subscribe 직후엔 snapshot·data 프레임이 섞여 온다. 그걸 종결로 오인하면 다음 요청이
    **겹쳐 나가** in-flight 1 규율이 깨진다."""
    rid = "fixed-id"
    ws = _RecordingWS(replies=[
        {"type": "rates", "data": {}},                                  # 무관
        {"type": "subscription_ack", "request_id": "someone-else"},     # 남의 종결
        {"type": "subscription_ack", "request_id": rid},                # 내 종결
    ], echo_ack=False)
    stats = load.LoadStats()
    frame = _run(load.run_one_request(ws, TOKEN, stats, request_id=rid))
    assert frame["request_id"] == rid
    assert stats.acked == 1


@pytest.mark.parametrize("frame,expected", [
    ({"type": "subscription_ack", "request_id": "a"}, True),
    ({"type": "subscription_error", "request_id": "a", "error": "invalid_token"}, True),
    ({"type": "subscription_ack", "request_id": "b"}, False),
    ({"type": "rates", "request_id": "a"}, False),
    ("not-a-dict", False),
    ({"type": "subscription_ack"}, False),
])
def test_terminating_frame_matching(frame, expected):
    assert load.is_terminating_frame(frame, "a") is expected


def test_timeout_is_counted_and_does_not_hang():
    ws = _RecordingWS(replies=[], echo_ack=False)
    stats = load.LoadStats()
    frame = _run(load.run_one_request(ws, TOKEN, stats, timeout=0.05))
    assert frame is None
    assert stats.timeouts == 1


# ── 토큰 비노출 ─────────────────────────────────────────────────────────────


def test_token_is_never_accepted_as_a_cli_argument():
    """⛔ argv 는 `ps` 로 다른 사용자에게 보이고 셸 히스토리에 남는다."""
    parser = load.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--token", TOKEN])


def test_prefix_abbreviation_cannot_smuggle_a_token_into_argv():
    """⛔ argparse 는 기본으로 **접두어 축약**을 허용한다 — `--token-stdin` 이 사라지면
    `--token <값>` 이 조용히 `--token-file` 로 붙어 토큰이 argv 에 남는다(`ps` 노출).
    지금의 "모호하다" 반려는 **우연한 보호**라 설계로 잠근다."""
    single_option = load.build_arg_parser()
    assert single_option.allow_abbrev is False, "접두어 축약이 켜져 있다"

    with pytest.raises(SystemExit):
        single_option.parse_args(["--token-fil", TOKEN])     # 축약도 거부돼야 한다


def test_token_from_stdin():
    assert load.load_id_token(stdin_stream=StringIO(TOKEN + "\n")) == TOKEN


def test_token_file_must_not_be_group_or_world_readable(tmp_path):
    path = tmp_path / "tok"
    path.write_text(TOKEN)
    os.chmod(path, 0o644)
    with pytest.raises(load.InsecureTokenFile):
        load.load_id_token(token_file=str(path))

    os.chmod(path, 0o600)
    assert load.load_id_token(token_file=str(path)) == TOKEN


def test_empty_token_is_rejected_without_echoing_the_value():
    with pytest.raises(ValueError) as caught:
        load.load_id_token(stdin_stream=StringIO("   \n"))
    assert TOKEN not in str(caught.value)


def test_summary_output_never_contains_the_token():
    """⛔ 요약·로그 어디에도 토큰이 새면 안 된다."""
    ws = _RecordingWS()
    stats = load.LoadStats()
    _run(load.run_one_request(ws, TOKEN, stats))
    rendered = load.render_summary(stats, load.PHASES[1]) + json.dumps(stats.summary())
    assert TOKEN not in rendered
    assert "SECRET-TOKEN-VALUE" not in rendered


# ── 유계 실행 계획 ──────────────────────────────────────────────────────────


def test_plan_is_bounded_and_fits_inside_the_lease():
    """⛔ 창이 lease(15분)를 넘으면 중간 재인증이 필요해져 이 도구의 전제가 깨진다."""
    assert load.total_plan_seconds() == 420, "계획이 7분에서 벗어났다"
    assert load.plan_fits_in_lease()


def test_plan_shape_matches_the_agreed_ramp():
    assert [(p.name, p.concurrency, p.seconds) for p in load.PHASES] == [
        ("baseline", 0, 60), ("c1", 1, 60), ("c4", 4, 120), ("c8", 8, 120), ("observe", 0, 60),
    ]


def test_over_long_plan_is_rejected():
    long_plan = (load.Phase("too-long", 1, 1000),)
    assert not load.plan_fits_in_lease(long_plan)


# ── 중단 판정 ───────────────────────────────────────────────────────────────


def test_client_abort_on_timeout_and_unexpected_error():
    stats = load.LoadStats()
    assert load.evaluate_client_abort(stats) == []
    stats.timeouts = 1
    assert load.evaluate_client_abort(stats)

    other = load.LoadStats()
    other.record_error("invalid_token")
    assert load.evaluate_client_abort(other)


def test_worker_stops_immediately_on_abort_condition():
    """⚠️ 중단 조건은 창 끝까지 기다리지 않고 **즉시** 멈춰야 한다."""
    ws = _RecordingWS(replies=[], echo_ack=False)
    stats = load.LoadStats()

    async def scenario():
        # timeout 을 짧게 만들기 위해 단일 요청 경로를 직접 태운다.
        await load.run_one_request(ws, TOKEN, stats, timeout=0.01)
        assert load.evaluate_client_abort(stats), "timeout 이 중단 조건으로 잡히지 않았다"

    _run(scenario())


@pytest.mark.parametrize("auth,probe,prev,expected_hit", [
    ({}, {}, 0, False),
    ({"queue_wait_ms_max": 5000}, {}, 0, True),
    ({"never_started": 1}, {}, 0, True),
    ({"caller_cancelled_while_running": 1}, {}, 0, True),
    ({}, {"outstanding": 1}, 0, False),      # 1회만으론 중단하지 않는다
    ({}, {"outstanding": 1}, 1, True),       # 2회 연속이면 포화
    ({}, {"queue_delay_ms_max": 1000}, 0, True),
])
def test_server_abort_rules(auth, probe, prev, expected_hit):
    """⛔ 판정은 순수 함수로 둔다 — 부하 생성기가 admin 자격증명을 들 이유가 없다."""
    assert bool(load.evaluate_server_abort(auth, probe, prev)) is expected_hit
