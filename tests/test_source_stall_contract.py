"""수집 멈춤 알림 계약 시험 — S1 유효 수신 기록 + S2 1분 점검기.

설계: design/source-health/stall-alert/plan_r1.md + plan_r2_delta.md (Claude·Codex 합의 2026-10-02).
이 파일이 구현 계약이다. 구현은 아래 공개 인터페이스를 따른다.

app/source_seen.py
    SEEN_KEY = "source_health:last_valid_seen"
    valid_pairs(current_rates) -> dict[str, float]
        MIBANK_REQUIRED_PAIRS 의 키만, bool 이 아닌 int/float, math.isfinite,
        MIBANK_RATE_RANGES 의 low <= v <= high 인 값만 남긴다. 입력은 바꾸지 않는다.
    record_valid_seen(source, current_rates, *, now_ms=None) -> None
        유효 값이 하나라도 있으면 `app.latest_rates_cache._get_sync_client()` (호출 시점에 모듈
        속성으로 조회) 로 HSET(SEEN_KEY, mapping={f"{source}:{pair}": now_ms}) 1회. 없으면 Redis 호출 0.
        어떤 예외도 밖으로 내보내지 않는다. now_ms 기본값은 현재 UTC epoch 밀리초.

app/crud.py
    insert_bank_rates_into_db: 미초기화 검사(return 0) 뒤, `atomic_write_runtime.snapshot()` 앞에서
        `source_seen.record_valid_seen(bank_name, current_rates)` 를 모듈 속성으로 1회 호출.
    insert_investing_rates_into_db: 같은 자리에서 `source_seen.record_valid_seen("investing", current_rates)`.

app/source_stall_monitor.py
    TARGET_SOURCES = ("investing","kb","hana","woori","shinhan","nh","sc","ibk","bs","citi")
    QUEUE_SOURCES = frozenset({"shinhan","nh","sc","ibk"})
    StallConfig(min_gap_s=600, lag_request_s=120, lag_queue_s=300, lookback_s=2700)  (frozen dataclass)
    STATE_KEY_PREFIX = "source_health:stall_state:"   → 실제 키는 + "shadow" | "live"
    MonitorRuntime(started_utc)   — 프로세스 메모리 상태(enabled_since, 읽기 실패 시각, 재개 하한 등)
    run_tick(runtime, *, now_utc, client, is_enabled, send, send_enabled, config=StallConfig(),
             candidates_fn=collection_slots.generate_candidates) -> None
        client: hgetall/hset(name, mapping=...)/hdel(name, *fields) 를 가진 Redis 유사 객체.
        is_enabled(source) -> bool, send(text) -> bool.
    register(scheduler) -> None   — id="source_stall_check", 1분 주기, coalesce=True, max_instances=1.
    default_send(text) -> bool     — telegram_handler.send_message(text, parse_mode="").

판정 규칙(키 = f"{source}:{pair}"):
    start = max(last_seen, runtime.started, enabled_since[source], 재개 하한, now - lookback)
    U = {source 의 회차 c : start < c.due_at_utc <= now - lag(source)}
    멈춤 = |U| >= 2 and max(due) - min(due) >= min_gap_s.  U 가 비면 보류(복구 아님).
    is_enabled(source) 가 False 인 source 는 평가하지 않는다(상태 유지).
    복구 = 멈춤 상태에서 last_seen 이 멈춤 당시 값보다 새로워짐(수집 시간대와 무관).
발송/상태 순서(plan_r2 §3): 의도 상태를 먼저 쓰고, 쓰기 실패 tick 은 발송 0. 발송 True 뒤 최종 상태.
한 tick 의 전이는 send 1회(메시지 1통). shadow(send_enabled=False)는 send 0회, 로그
"source_stall_transition" 을 남기고 상태는 발송 성공처럼 마무리(같은 전이를 반복 기록하지 않음).
메시지는 평문 한국어: 멈춤 전이는 "멈춤", 복구는 "복구", 읽기 실패는 "감시 불가", 회복은 "감시 재개" 를 포함하고
source 이름과 통화(pair)를 적는다.
Redis 읽기 실패가 1800초 이어지면 '감시 불가' 1통, 읽기가 회복되면 '감시 재개' 1통(실패가 있었으면
재개 시각을 모든 source 의 start 하한으로 둔다 — 오래된 키가 한꺼번에 멈춤이 되지 않게).
"""

import inspect
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app import source_seen, source_stall_monitor as mon
from app.collection_slots import generate_candidates

KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc
PAIRS = ("usd-krw", "jpy-krw", "eur-krw")
GOOD = {"usd-krw": 1390.5, "jpy-krw": 930.12, "eur-krw": 1620.0}


def kst(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=KST).astimezone(UTC)


def ms(dt):
    return int(dt.timestamp() * 1000)


# ── S1: valid_pairs / record_valid_seen ─────────────────────────────────────────────

class RecordingClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def hset(self, name, mapping=None, **kw):
        if self.fail:
            raise ConnectionError("redis down")
        self.calls.append((name, dict(mapping)))
        return len(mapping)


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -float("inf"), 0, True, False,
                                 "1390.5", 999.99, 2000.01, [1390.0]])
def test_valid_pairs_rejects_non_values(bad):
    assert source_seen.valid_pairs({"usd-krw": bad}) == {}


def test_valid_pairs_keeps_required_pairs_in_range_inclusive_and_does_not_mutate():
    rates = {"usd-krw": 1000, "jpy-krw": 1400.0, "eur-krw": 1620.0, "cny-krw": 190.0}
    before = dict(rates)
    assert source_seen.valid_pairs(rates) == {"usd-krw": 1000, "jpy-krw": 1400.0, "eur-krw": 1620.0}
    assert rates == before


def test_record_writes_one_hset_with_source_pair_fields(monkeypatch):
    client = RecordingClient()
    monkeypatch.setattr("app.latest_rates_cache._get_sync_client", lambda: client)
    source_seen.record_valid_seen("kb", {**GOOD, "jpy-krw": float("nan")}, now_ms=1234)
    assert client.calls == [(source_seen.SEEN_KEY, {"kb:usd-krw": 1234, "kb:eur-krw": 1234})]


def test_record_without_valid_values_makes_no_redis_call(monkeypatch):
    calls = []
    monkeypatch.setattr("app.latest_rates_cache._get_sync_client", lambda: calls.append(1))
    source_seen.record_valid_seen("kb", {"usd-krw": None})
    assert calls == []


@pytest.mark.parametrize("factory", [lambda: None, lambda: RecordingClient(fail=True),
                                     lambda: (_ for _ in ()).throw(RuntimeError("init"))])
def test_record_never_raises(monkeypatch, factory):
    monkeypatch.setattr("app.latest_rates_cache._get_sync_client", factory)
    source_seen.record_valid_seen("kb", GOOD, now_ms=1)


# ── S1: crud 배선 위치 ───────────────────────────────────────────────────────────────

def _wire(monkeypatch, initialized, log):
    import app.crud as crud
    from app.atomic_write_control import WriterMode

    def snapshot():
        log.append("snapshot")
        return SimpleNamespace(enforced_action=WriterMode.HALT)

    monkeypatch.setattr(crud.atomic_write_runtime, "is_initialized", lambda: initialized)
    monkeypatch.setattr(crud.atomic_write_runtime, "snapshot", snapshot)
    monkeypatch.setattr("app.source_seen.record_valid_seen",
                        lambda source, rates, **kw: log.append(("record", source, rates)))
    return crud


@pytest.mark.parametrize("call", ["bank", "investing"])
def test_crud_records_after_uninitialized_guard_and_before_mode_snapshot(monkeypatch, call):
    log = []
    crud = _wire(monkeypatch, True, log)
    if call == "bank":
        assert crud.insert_bank_rates_into_db(None, GOOD, "kb") == 0
        assert log == [("record", "kb", GOOD), "snapshot"]
    else:
        assert crud.insert_investing_rates_into_db(None, GOOD) == 0
        assert log == [("record", "investing", GOOD), "snapshot"]


@pytest.mark.parametrize("call", ["bank", "investing"])
def test_crud_uninitialized_writer_records_nothing(monkeypatch, call):
    log = []
    crud = _wire(monkeypatch, False, log)
    if call == "bank":
        assert crud.insert_bank_rates_into_db(None, GOOD, "kb") == 0
    else:
        assert crud.insert_investing_rates_into_db(None, GOOD) == 0
    assert log == []


# ── S2: 점검기 ──────────────────────────────────────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self.h, self.fail_read, self.fail_write = {}, False, False
        self.fail_hset_if, self.fail_hdel = None, False   # 최종 쓰기만 골라 실패시키는 장치

    def hgetall(self, name):
        if self.fail_read:
            raise ConnectionError("read")
        return {k.encode(): str(v).encode() for k, v in self.h.get(name, {}).items()}

    def hset(self, name, mapping=None, **kw):
        if self.fail_write or (self.fail_hset_if is not None and self.fail_hset_if(mapping)):
            raise ConnectionError("write")
        self.h.setdefault(name, {}).update({k: v for k, v in mapping.items()})
        return len(mapping)

    def hdel(self, name, *fields):
        if self.fail_write or self.fail_hdel:
            raise ConnectionError("write")
        for f in fields:
            self.h.get(name, {}).pop(f, None)
        return len(fields)

    def seen(self, source, at, pairs=PAIRS):
        self.h.setdefault(source_seen.SEEN_KEY, {}).update({f"{source}:{p}": ms(at) for p in pairs})

    def state(self, ns="live"):
        raw = self.h.get(mon.STATE_KEY_PREFIX + ns, {})
        return {k: (json.loads(v) if isinstance(v, (str, bytes)) else v) for k, v in raw.items()}


class Sender:
    def __init__(self, ok=True):
        self.texts, self.ok = [], ok

    def __call__(self, text):
        self.texts.append(text)
        return self.ok


def fresh_all(redis, at, skip=()):
    for s in mon.TARGET_SOURCES:
        if s not in skip:
            redis.seen(s, at)


def tick(runtime, redis, now, sender, *, enabled=lambda s: True, send_enabled=True):
    mon.run_tick(runtime, now_utc=now, client=redis, is_enabled=enabled, send=sender,
                 send_enabled=send_enabled)


def stalled_keys(redis, ns="live"):
    return sorted(redis.state(ns))


MON = (2026, 10, 5)   # 월요일 IN 모드


def test_kb_stall_waits_for_gap_then_alerts_once():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 30, 9))
    tick(rt, redis, kst(*MON, 9, 40, 30), sender)
    assert sender.texts == []
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb",))
    tick(rt, redis, kst(*MON, 9, 43, 0), sender)
    assert len(sender.texts) == 1 and "kb" in sender.texts[0] and "멈춤" in sender.texts[0]
    assert stalled_keys(redis) == ["kb:eur-krw", "kb:jpy-krw", "kb:usd-krw"]
    tick(rt, redis, kst(*MON, 9, 44, 0), sender)
    assert len(sender.texts) == 1


def test_single_currency_stall_alerts_only_that_currency():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 42, 59))
    redis.seen("kb", kst(*MON, 9, 30, 9), pairs=("jpy-krw",))
    tick(rt, redis, kst(*MON, 9, 43, 0), sender)
    assert stalled_keys(redis) == ["kb:jpy-krw"]


def test_queue_source_uses_longer_lag():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    redis.seen("shinhan", kst(*MON, 9, 30, 20))
    # due <= now-300: 09:31:18 .. 09:40:18 → span 9분 < 10분
    fresh_all(redis, kst(*MON, 9, 45, 20), skip=("shinhan",))
    tick(rt, redis, kst(*MON, 9, 45, 30), sender)
    assert sender.texts == []
    fresh_all(redis, kst(*MON, 9, 47, 20), skip=("shinhan",))
    tick(rt, redis, kst(*MON, 9, 47, 30), sender)
    assert stalled_keys(redis) == ["shinhan:eur-krw", "shinhan:jpy-krw", "shinhan:usd-krw"]


def test_no_alert_outside_collection_window_shinhan_overnight():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(2026, 10, 6, 2, 0))
    fresh_all(redis, kst(2026, 10, 6, 7, 29), skip=("shinhan",))
    redis.seen("shinhan", kst(2026, 10, 6, 2, 59, 20))
    for h, m in ((3, 30), (5, 0), (7, 30)):
        fresh_all(redis, kst(2026, 10, 6, h, m) - timedelta(seconds=30), skip=("shinhan",))
        tick(rt, redis, kst(2026, 10, 6, h, m), sender)
    assert sender.texts == [] and stalled_keys(redis) == []


def test_sc_after_close_is_quiet_but_stop_before_close_alerts():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 18, 0))
    fresh_all(redis, kst(*MON, 19, 29), skip=("sc",))
    redis.seen("sc", kst(*MON, 19, 0, 0))
    tick(rt, redis, kst(*MON, 19, 30), sender)
    assert sender.texts == []

    redis2, sender2 = FakeRedis(), Sender()
    rt2 = mon.MonitorRuntime(kst(*MON, 18, 0))
    fresh_all(redis2, kst(*MON, 19, 9), skip=("sc",))
    redis2.seen("sc", kst(*MON, 18, 40, 58))
    tick(rt2, redis2, kst(*MON, 19, 10), sender2)
    assert stalled_keys(redis2) == ["sc:eur-krw", "sc:jpy-krw", "sc:usd-krw"]


def test_admin_disabled_source_is_held_and_reenable_restarts_the_clock():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 59), skip=("kb",))
    redis.seen("kb", kst(*MON, 9, 1))
    off = lambda s: s != "kb"
    tick(rt, redis, kst(*MON, 10, 0), sender, enabled=off)
    assert sender.texts == []
    tick(rt, redis, kst(*MON, 10, 1), sender)            # 다시 켜짐 → enabled_since=10:01
    fresh_all(redis, kst(*MON, 10, 8, 59), skip=("kb",))
    tick(rt, redis, kst(*MON, 10, 9), sender)
    assert sender.texts == []
    fresh_all(redis, kst(*MON, 10, 13, 59), skip=("kb",))
    tick(rt, redis, kst(*MON, 10, 14), sender)
    assert stalled_keys(redis) == ["kb:eur-krw", "kb:jpy-krw", "kb:usd-krw"]


def test_recovery_alerts_once_and_clears_state():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb",))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    tick(rt, redis, kst(*MON, 9, 43, 0), sender)
    redis.seen("kb", kst(*MON, 9, 50, 9))
    fresh_all(redis, kst(*MON, 9, 50, 50), skip=("kb",))
    tick(rt, redis, kst(*MON, 9, 51, 0), sender)
    assert len(sender.texts) == 2 and "복구" in sender.texts[1] and "kb" in sender.texts[1]
    assert stalled_keys(redis) == []
    tick(rt, redis, kst(*MON, 9, 52, 0), sender)
    assert len(sender.texts) == 2


def test_restart_does_not_resend_persisted_stall_and_recovers_once():
    redis, sender = FakeRedis(), Sender()
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb",))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    tick(mon.MonitorRuntime(kst(*MON, 9, 0)), redis, kst(*MON, 9, 43, 0), sender)
    assert len(sender.texts) == 1
    rt2 = mon.MonitorRuntime(kst(*MON, 9, 45))            # 재시작
    fresh_all(redis, kst(*MON, 9, 59, 50), skip=("kb",))
    tick(rt2, redis, kst(*MON, 10, 0), sender)
    assert len(sender.texts) == 1
    redis.seen("kb", kst(*MON, 10, 0, 29))
    tick(rt2, redis, kst(*MON, 10, 1), sender)
    assert len(sender.texts) == 2 and "복구" in sender.texts[1]


def test_simultaneous_transitions_are_one_message():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb", "hana"))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    redis.seen("hana", kst(*MON, 9, 30, 2))
    tick(rt, redis, kst(*MON, 9, 43, 0), sender)
    assert len(sender.texts) == 1 and "kb" in sender.texts[0] and "hana" in sender.texts[0]


def test_state_write_failure_means_no_send_then_retry():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb",))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    redis.fail_write = True
    tick(rt, redis, kst(*MON, 9, 43, 0), sender)
    assert sender.texts == []
    redis.fail_write = False
    tick(rt, redis, kst(*MON, 9, 44, 0), sender)
    assert len(sender.texts) == 1


def test_send_failure_keeps_pending_and_resends_until_success():
    redis = FakeRedis()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb",))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    failing = Sender(ok=False)
    tick(rt, redis, kst(*MON, 9, 43, 0), failing)
    assert len(failing.texts) == 1
    assert all(entry["notified"] is False for entry in redis.state().values())
    ok = Sender()
    tick(rt, redis, kst(*MON, 9, 44, 0), ok)
    tick(rt, redis, kst(*MON, 9, 45, 0), ok)
    assert len(ok.texts) == 1
    assert all(entry["notified"] is True for entry in redis.state().values())


def test_shadow_mode_never_sends_logs_once_and_uses_shadow_namespace(caplog):
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb",))
    redis.seen("kb", kst(*MON, 9, 30, 9))

    def transitions():
        return [r for r in caplog.records if "source_stall_transition" in r.getMessage()]

    with caplog.at_level(logging.INFO):
        tick(rt, redis, kst(*MON, 9, 43, 0), sender, send_enabled=False)
    assert len(transitions()) >= 1
    caplog.clear()
    with caplog.at_level(logging.INFO):
        tick(rt, redis, kst(*MON, 9, 44, 0), sender, send_enabled=False)
    assert transitions() == []                 # 같은 전이를 tick 마다 반복 기록하지 않음
    assert sender.texts == []
    assert stalled_keys(redis, "shadow") == ["kb:eur-krw", "kb:jpy-krw", "kb:usd-krw"]
    assert stalled_keys(redis, "live") == []


def test_redis_read_outage_alerts_once_resumes_once_without_stall_burst():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 59, 50))
    tick(rt, redis, kst(*MON, 10, 0), sender)
    redis.fail_read = True
    for m in range(1, 31):
        tick(rt, redis, kst(*MON, 10, m), sender)
    assert sender.texts == []
    tick(rt, redis, kst(*MON, 10, 31), sender)
    assert len(sender.texts) == 1 and "감시" in sender.texts[0]
    tick(rt, redis, kst(*MON, 10, 32), sender)
    assert len(sender.texts) == 1
    redis.fail_read = False
    fresh_all(redis, kst(*MON, 10, 32, 50), skip=("kb",))   # kb 는 10:00 이후 수신 없음
    tick(rt, redis, kst(*MON, 10, 33), sender)
    assert len(sender.texts) == 2 and "재개" in sender.texts[1]
    assert stalled_keys(redis) == []                         # 재개 하한 → 즉시 멈춤 폭발 없음


def test_register_adds_one_minute_job_not_cleared_by_mode_switch():
    calls = []

    class Sched:
        def add_job(self, func, trigger, **kw):
            calls.append((func, trigger, kw))

    mon.register(Sched())
    assert len(calls) == 1
    _, trigger, kw = calls[0]
    assert kw["id"] == "source_stall_check" and not kw["id"].startswith("task_")
    assert kw.get("coalesce") is True and kw.get("max_instances") == 1
    fires = []
    t = trigger.get_next_fire_time(None, kst(*MON, 10, 0))
    while len(fires) < 3:
        fires.append(t)
        t = trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    assert [(b - a).total_seconds() for a, b in zip(fires, fires[1:])] == [60.0, 60.0]


def test_start_scheduler_registers_the_monitor():
    import app.scheduler as scheduler
    assert "source_stall_monitor.register(" in inspect.getsource(scheduler.start_scheduler)


def test_default_send_is_plain_text(monkeypatch):
    seen = []
    monkeypatch.setattr("app.notifications.telegram.telegram_handler.send_message",
                        lambda text, parse_mode="Markdown": seen.append(parse_mode) or True)
    assert mon.default_send("x") is True
    assert seen == [""]


def test_target_sources_match_policy_crawlers():
    names = {c.crawler for c in generate_candidates(kst(*MON, 9, 0), kst(*MON, 10, 0))}
    assert set(mon.TARGET_SOURCES) <= names
    assert "dxy" not in mon.TARGET_SOURCES
    assert mon.QUEUE_SOURCES == frozenset({"shinhan", "nh", "sc", "ibk"})
    assert math.isclose(mon.StallConfig().min_gap_s, 600)


# ── 1회차 검토(Codex REJECT) 반영 계약 — 2026-10-02 ──────────────────────────────────
# run_tick 은 bool 을 돌려준다: Redis 읽기·상태 쓰기가 모두 성공하고(발송할 것이 있었다면) 발송까지
# 성공한 tick 만 True. 읽기뿐 아니라 상태 쓰기 실패도 1800초 '감시 불가' 시간에 포함한다.
# 감시 재개 tick 은 재개 1통만 보내고(발송 모드), 그 tick 의 전이 발송은 다음 tick 으로 미룬다.

def _stall_kb(redis, sender, rt):
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb",))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    assert mon.run_tick(rt, now_utc=kst(*MON, 9, 43, 0), client=redis, is_enabled=lambda s: True,
                        send=sender, send_enabled=True) is True
    assert len(sender.texts) == 1


def test_state_write_outage_counts_toward_unavailable_notice():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb",))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    redis.fail_write = True                      # 읽기는 되고 쓰기만 실패
    results = []
    for m in range(43, 73):                      # 09:43 .. 10:12 — 29분
        results.append(mon.run_tick(rt, now_utc=kst(*MON, 9, 0) + timedelta(minutes=m),
                                    client=redis, is_enabled=lambda s: True, send=sender,
                                    send_enabled=True))
    assert results == [False] * 30 and sender.texts == []
    tick(rt, redis, kst(*MON, 10, 13), sender)   # 실패 시작 09:43 + 30분
    assert len(sender.texts) == 1 and "감시 불가" in sender.texts[0]
    tick(rt, redis, kst(*MON, 10, 14), sender)
    assert len(sender.texts) == 1
    redis.fail_write = False
    tick(rt, redis, kst(*MON, 10, 15), sender)
    assert len(sender.texts) == 2 and "감시 재개" in sender.texts[1]


def test_resume_tick_sends_only_the_resume_notice_then_recovery_next_tick():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    _stall_kb(redis, sender, rt)
    redis.fail_read = True
    for m in range(44, 75):                      # 09:44 .. 10:14 (31 tick)
        tick(rt, redis, kst(*MON, 9, 0) + timedelta(minutes=m), sender)
    assert len(sender.texts) == 2 and "감시 불가" in sender.texts[1]
    redis.fail_read = False
    redis.seen("kb", kst(*MON, 10, 14, 59))      # 장애 중 kb 수신 재개
    fresh_all(redis, kst(*MON, 10, 14, 59), skip=("kb",))
    tick(rt, redis, kst(*MON, 10, 15), sender)
    assert len(sender.texts) == 3 and "감시 재개" in sender.texts[2] and "복구" not in sender.texts[2]
    tick(rt, redis, kst(*MON, 10, 16), sender)
    assert len(sender.texts) == 4 and "복구" in sender.texts[3] and "kb" in sender.texts[3]


def test_run_tick_reports_failure_on_intent_write_and_send_failure():
    redis = FakeRedis()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb",))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    redis.fail_write = True
    assert mon.run_tick(rt, now_utc=kst(*MON, 9, 43, 0), client=redis, is_enabled=lambda s: True,
                        send=Sender(), send_enabled=True) is False
    redis.fail_write = False
    assert mon.run_tick(rt, now_utc=kst(*MON, 9, 44, 0), client=redis, is_enabled=lambda s: True,
                        send=Sender(ok=False), send_enabled=True) is False
    assert mon.run_tick(rt, now_utc=kst(*MON, 9, 45, 0), client=redis, is_enabled=lambda s: True,
                        send=Sender(), send_enabled=True) is True


@pytest.mark.parametrize("healthy, expected", [(False, 0), (True, 1)])
def test_startup_notice_only_after_a_healthy_tick(monkeypatch, healthy, expected):
    import app.config as config
    jobs, sent = [], []

    class Sched:
        def add_job(self, func, trigger, **kw):
            jobs.append(func)

    monkeypatch.setattr(config, "SOURCE_STALL_ALERT_SEND", True)
    monkeypatch.setattr("app.latest_rates_cache._get_sync_client", lambda: FakeRedis())
    monkeypatch.setattr(mon, "run_tick", lambda *a, **kw: healthy)
    monkeypatch.setattr(mon, "default_send", lambda text: sent.append(text) or True)
    mon.register(Sched())
    jobs[0]()
    assert len([t for t in sent if "감시 시작" in t]) == expected


def test_messages_name_last_receipt_and_stall_duration():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    _stall_kb(redis, sender, rt)
    assert "마지막 수신" in sender.texts[0] and "09:30" in sender.texts[0]
    redis.seen("kb", kst(*MON, 9, 50, 9))
    fresh_all(redis, kst(*MON, 9, 50, 50), skip=("kb",))
    tick(rt, redis, kst(*MON, 9, 51, 0), sender)
    assert "복구" in sender.texts[1] and "20분" in sender.texts[1]


# ── 2회차 검토(Codex REJECT) 반영 계약 — 2026-10-02 ──────────────────────────────────
# 최종 상태 쓰기(notified=true HSET, 복구 HDEL)만 실패해도 (a) 같은 전이를 다시 보내지 않고
# (b) 실패가 1800초 이어지면 '감시 불가' 1통. 복구의 멈춘 시간은 처음 판정 때 고정, 수신 기록 없이
# 멈췄다면 '첫 수신'.

def _final_notified(mapping):
    return any('"notified":true' in str(v) for v in mapping.values())


def test_final_hset_outage_does_not_resend_and_alerts_unavailable():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis, kst(*MON, 9, 42, 59), skip=("kb",))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    redis.fail_hset_if = _final_notified
    for m in range(43, 76):                      # 09:43 .. 10:15
        fresh_all(redis, kst(*MON, 9, 0) + timedelta(minutes=m) - timedelta(seconds=1), skip=("kb",))
        tick(rt, redis, kst(*MON, 9, 0) + timedelta(minutes=m), sender)
    stalls = [t for t in sender.texts if "멈춤" in t and "kb" in t]
    unavailable = [t for t in sender.texts if "감시 불가" in t]
    assert len(stalls) == 1 and len(unavailable) == 1


def test_final_hdel_outage_does_not_resend_recovery_and_alerts_unavailable():
    redis, sender = FakeRedis(), Sender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    _stall_kb(redis, sender, rt)
    redis.seen("kb", kst(*MON, 9, 50, 9))
    redis.fail_hdel = True
    for m in range(51, 84):                      # 09:51 .. 10:23
        fresh_all(redis, kst(*MON, 9, 0) + timedelta(minutes=m) - timedelta(seconds=1))
        tick(rt, redis, kst(*MON, 9, 0) + timedelta(minutes=m), sender)
    recoveries = [t for t in sender.texts if "복구" in t]
    unavailable = [t for t in sender.texts if "감시 불가" in t]
    assert len(recoveries) == 1 and len(unavailable) == 1


def test_recovery_duration_fixed_at_first_detection_and_first_receipt():
    redis = FakeRedis()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    sender = Sender()
    _stall_kb(redis, sender, rt)
    redis.seen("kb", kst(*MON, 9, 50, 9))
    fresh_all(redis, kst(*MON, 9, 50, 50), skip=("kb",))
    failing = Sender(ok=False)
    tick(rt, redis, kst(*MON, 9, 51, 0), failing)
    redis.seen("kb", kst(*MON, 10, 0, 9))
    fresh_all(redis, kst(*MON, 10, 0, 50), skip=("kb",))
    tick(rt, redis, kst(*MON, 10, 1, 0), sender)
    assert "복구" in sender.texts[-1] and "20분" in sender.texts[-1]

    redis2, sender2 = FakeRedis(), Sender()
    rt2 = mon.MonitorRuntime(kst(*MON, 9, 0))
    fresh_all(redis2, kst(*MON, 9, 12, 59), skip=("kb",))        # kb 는 수신 기록 없음
    tick(rt2, redis2, kst(*MON, 9, 13, 0), sender2)
    assert "kb" in sender2.texts[0] and "수신 기록 없음" in sender2.texts[0]
    redis2.seen("kb", kst(*MON, 9, 13, 30))
    fresh_all(redis2, kst(*MON, 9, 13, 50), skip=("kb",))
    tick(rt2, redis2, kst(*MON, 9, 14, 0), sender2)
    assert "복구" in sender2.texts[1] and "첫 수신" in sender2.texts[1]


# ── 3회차 검토(Codex REJECT) 반영 계약 — 2026-10-02 ──────────────────────────────────
# (a) '감시 재개'는 그 tick 의 Redis 읽기·의도 쓰기·남은 최종 쓰기가 모두 성공했을 때만 보낸다.
#     최종 쓰기 장애가 이어지는 동안 재개·재차 '감시 불가'를 보내지 않는다(shadow 로그도 같음).
# (b) 모든 발송(전이·감시 불가·감시 재개)을 합쳐 tick 당 최대 1통. 밀린 메시지는 다음 tick 에 간다.

class TickSender(Sender):
    """send 호출을 tick 별로 세어 tick 당 1통 불변을 검사한다."""

    def __init__(self, ok=True):
        super().__init__(ok)
        self.current, self.per_tick = None, {}

    def __call__(self, text):
        self.per_tick[self.current] = self.per_tick.get(self.current, 0) + 1
        return super().__call__(text)


def _run(rt, redis, sender, start, minutes, *, skip=(), send_enabled=True):
    for m in range(minutes):
        now = start + timedelta(minutes=m)
        fresh_all(redis, now - timedelta(seconds=1), skip=skip)
        sender.current = now
        tick(rt, redis, now, sender, send_enabled=send_enabled)


def test_no_resume_while_final_writes_keep_failing_then_one_resume():
    redis, sender = FakeRedis(), TickSender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    redis.fail_hset_if = _final_notified
    _run(rt, redis, sender, kst(*MON, 9, 43), 66, skip=("kb",))      # 09:43 .. 10:48
    assert [t for t in sender.texts if "감시 재개" in t] == []
    assert len([t for t in sender.texts if "감시 불가" in t]) == 1
    assert len([t for t in sender.texts if "멈춤" in t and "kb" in t]) == 1
    redis.fail_hset_if = None
    _run(rt, redis, sender, kst(*MON, 10, 49), 3, skip=("kb",))      # 10:49 .. 10:51
    assert len([t for t in sender.texts if "감시 재개" in t]) == 1
    assert len([t for t in sender.texts if "멈춤" in t and "kb" in t]) == 1   # 재발송 없음
    assert all(entry["notified"] is True for entry in redis.state().values())
    assert max(sender.per_tick.values()) == 1


def test_shadow_logs_no_resume_while_final_writes_keep_failing(caplog):
    redis, sender = FakeRedis(), TickSender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    redis.fail_hset_if = _final_notified
    with caplog.at_level(logging.INFO):
        _run(rt, redis, sender, kst(*MON, 9, 43), 66, skip=("kb",), send_enabled=False)
    messages = [r.getMessage() for r in caplog.records]
    assert not [m for m in messages if "monitor_resumed" in m]
    assert len([m for m in messages if "monitor_unavailable" in m]) == 1
    assert sender.texts == []


def test_at_most_one_message_per_tick_when_transition_and_outage_coincide():
    redis, sender = FakeRedis(), TickSender()
    rt = mon.MonitorRuntime(kst(*MON, 9, 0))
    redis.seen("kb", kst(*MON, 9, 30, 9))
    redis.fail_hset_if = _final_notified                              # kb 최종 저장 장애 09:43 시작
    _run(rt, redis, sender, kst(*MON, 9, 43), 18, skip=("kb",))       # 09:43 .. 10:00
    redis.seen("hana", kst(*MON, 10, 0, 2))                           # hana 는 10:00:02 뒤 멈춤
    _run(rt, redis, sender, kst(*MON, 10, 1), 16, skip=("kb", "hana"))  # 10:01 .. 10:16 (10:13 겹침)
    assert max(sender.per_tick.values()) == 1
    assert len([t for t in sender.texts if "감시 불가" in t]) == 1
    assert len([t for t in sender.texts if "멈춤" in t and "hana" in t]) == 1
