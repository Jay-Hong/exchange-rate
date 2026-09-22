"""S1a 계약 — 설정 효력 관측(commit 경계·baseline·추적)과 배선의 동작 불변.

설계: SOURCE_HEALTH_COLLECTION_EXPECTED.md §3·부록 A §4, design/s1a/interface_r4.md(Claude·Codex 합의).
효력 경계는 `db.commit()` 이 **정상 반환한 관측 시각**이다. 캐시·재등록·`updated_at` 은 경계를 미루지 않고,
관측 실패는 수집·토글 동작을 바꾸지 않되 **조용히 known 을 유지하지도 않는다**.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import collection_config_observer as cco
from app import crud
from app import scheduler as sched
from app.collection_slots import ConfigState, SlotCandidate, classify

UTC = timezone.utc
# ⛔ 고정 날짜를 쓰면 그 시각이 지난 뒤 baseline 종료(실제 now)보다 과거가 되어 known 구간이 사라진다 —
#    Codex 가 구현 보고에서 짚은 시간 의존성. 항상 "지금보다 뒤" 를 기준으로 잡는다.
T0 = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
CRAWLERS = ("investing", "dxy", "kb", "hana", "woori", "bs", "citi", "shinhan", "ibk", "nh", "sc")


@pytest.fixture
def obs():
    observer = cco.ConfigObserver()
    observer.start_epoch("epoch-1")
    return observer


def _baseline(observer, rows, *, baseline_id=None, ok=True, missing=()):
    bid = baseline_id or observer.baseline_begin()
    for crawler, enabled in rows.items():
        observer.baseline_row(crawler, enabled, baseline_id=bid)
    observer.baseline_end(baseline_id=bid, ok=ok, missing=tuple(missing))
    return bid


def _toggle(observer, crawler, enabled, ack_at, *, call_id="c1"):
    observer.commit_started(crawler, enabled, call_id=call_id)
    observer.commit_ack(call_id=call_id, ack_at=ack_at)


# ── 1. 효력 경계 ───────────────────────────────────────────────────────────────

def test_effective_boundary_is_the_ack_and_not_the_cache(obs):
    _baseline(obs, {"kb": True})
    ack = T0 + timedelta(hours=1)
    _toggle(obs, "kb", False, ack)
    obs.cache_applied("kb", False, ack + timedelta(seconds=5))
    assert obs.config_at("kb", ack - timedelta(microseconds=1)).enabled is True
    assert obs.config_at("kb", ack).enabled is False
    assert obs.config_at("kb", ack + timedelta(hours=1)).enabled is False


def test_consecutive_toggles_both_directions_keep_order(obs):
    _baseline(obs, {"kb": True})
    first, second, third = (T0 + timedelta(hours=h) for h in (1, 2, 3))
    _toggle(obs, "kb", False, first, call_id="a")
    _toggle(obs, "kb", True, second, call_id="b")
    _toggle(obs, "kb", False, third, call_id="c")
    assert [obs.config_at("kb", t).enabled for t in (first, second, third)] == [False, True, False]
    assert obs.config_at("kb", second - timedelta(microseconds=1)).enabled is False


def test_two_acks_with_the_same_timestamp_use_proven_revision_order(obs):
    _baseline(obs, {"kb": True})
    ack = T0 + timedelta(hours=1)
    _toggle(obs, "kb", False, ack, call_id="a")
    _toggle(obs, "kb", True, ack, call_id="b")
    state = obs.config_at("kb", ack)
    assert state.enabled is True                      # 나중 revision
    assert state.config_revision.endswith(":2")


def test_registration_failure_does_not_delay_the_boundary(obs):
    _baseline(obs, {"kb": True})
    ack = T0 + timedelta(hours=1)
    _toggle(obs, "kb", False, ack)
    # 캐시 적용·재등록 증거가 전혀 없어도 경계는 ack 다
    assert obs.config_at("kb", ack).enabled is False


# ── 2. commit 결과 세 갈래 ─────────────────────────────────────────────────────

def test_commit_not_entered_keeps_the_previous_known_value(obs):
    _baseline(obs, {"kb": True})
    obs.commit_started("kb", False, call_id="x")
    obs.commit_not_entered(call_id="x", error_type="ValueError")
    later = T0 + timedelta(hours=2)
    assert obs.config_at("kb", later).enabled is True


def test_commit_result_unknown_makes_that_crawler_unknown_onwards(obs):
    _baseline(obs, {"kb": True, "hana": True})
    obs.commit_started("kb", False, call_id="x")
    obs.commit_result_unknown(call_id="x", error_type="OperationalError")
    later = T0 + timedelta(hours=2)
    assert obs.config_at("kb", later) == ConfigState(None, None)
    assert obs.config_at("hana", later).enabled is True       # 다른 crawler 는 독립


def test_duplicate_ack_bumps_the_revision_only_once(obs):
    _baseline(obs, {"kb": True})
    ack = T0 + timedelta(hours=1)
    obs.commit_started("kb", False, call_id="x")
    obs.commit_ack(call_id="x", ack_at=ack)
    first = obs.config_at("kb", ack).config_revision
    obs.commit_ack(call_id="x", ack_at=ack + timedelta(minutes=1))
    assert obs.config_at("kb", ack).config_revision == first
    assert obs.snapshot()["counters"]["duplicate_ack"] == 1


# ── 3. baseline ────────────────────────────────────────────────────────────────

def test_known_starts_at_baseline_end_not_earlier(obs):
    bid = obs.baseline_begin()
    obs.baseline_row("kb", True, baseline_id=bid)
    obs.baseline_end(baseline_id=bid, ok=True)
    end_at = obs.snapshot()["baseline"]["ended_at"]
    assert obs.config_at("kb", end_at).enabled is True
    assert obs.config_at("kb", end_at - timedelta(microseconds=1)) == ConfigState(None, None)


def test_toggle_in_flight_across_the_baseline_read_keeps_that_crawler_unknown(obs):
    obs.commit_started("kb", False, call_id="x")          # baseline 전부터 진행 중
    bid = obs.baseline_begin()
    obs.baseline_row("kb", True, baseline_id=bid)
    obs.baseline_row("hana", True, baseline_id=bid)
    obs.baseline_end(baseline_id=bid, ok=True)
    obs.commit_ack(call_id="x", ack_at=T0 + timedelta(hours=1))   # 읽기 뒤에 끝남
    later = T0 + timedelta(hours=2)
    assert obs.config_at("kb", later) == ConfigState(None, None)
    assert obs.config_at("hana", later).enabled is True


def test_failed_or_partial_baseline_is_unknown(obs):
    bid = obs.baseline_begin()
    obs.baseline_row("kb", True, baseline_id=bid)
    obs.baseline_end(baseline_id=bid, ok=True, missing=("sc",))
    later = T0 + timedelta(hours=1)
    assert obs.config_at("sc", later) == ConfigState(None, None)
    assert obs.config_at("kb", later).enabled is True
    other = cco.ConfigObserver()
    other.start_epoch("epoch-2")
    bid2 = other.baseline_begin()
    other.baseline_end(baseline_id=bid2, ok=False)
    for crawler in CRAWLERS:
        assert other.config_at(crawler, later) == ConfigState(None, None)


def test_without_any_baseline_everything_is_unknown(obs):
    assert obs.config_at("kb", T0 + timedelta(hours=1)) == ConfigState(None, None)


# ── 4. 겹침·추적 ───────────────────────────────────────────────────────────────

def test_overlapping_unfinished_calls_make_the_crawler_unknown(obs):
    _baseline(obs, {"kb": True, "hana": True})
    obs.commit_started("kb", False, call_id="a")
    obs.commit_started("kb", True, call_id="b")            # 앞 호출이 끝나기 전에 두 번째
    later = T0 + timedelta(hours=3)
    assert obs.config_at("kb", later) == ConfigState(None, None)
    assert obs.config_at("hana", later).enabled is True


def test_tracking_degraded_cannot_be_cleared_by_a_new_baseline(obs):
    _baseline(obs, {"kb": True})
    obs.commit_started(None, None, call_id="a")            # crawler 귀속 불가 → 추적 유실
    assert obs.snapshot()["readiness"]["state"] == "degraded"
    _baseline(obs, {"kb": True})
    later = T0 + timedelta(hours=5)
    assert obs.config_at("kb", later) == ConfigState(None, None)
    fresh = cco.ConfigObserver()
    fresh.start_epoch("epoch-2")
    _baseline(fresh, {"kb": True})
    assert fresh.config_at("kb", later).enabled is True    # 새 epoch 에서만 풀린다


def test_inflight_cap_bounds_memory_and_keeps_existing_records(obs):
    _baseline(obs, {"kb": True})
    cap = obs.snapshot()["limits"]["inflight_cap"]
    for i in range(cap):
        obs.commit_started("kb", False, call_id=f"keep-{i}")
    for i in range(cap * 2):
        obs.commit_started("kb", False, call_id=f"drop-{i}")
    snap = obs.snapshot()
    assert snap["limits"]["inflight_len"] <= cap
    assert snap["readiness"]["state"] == "degraded"
    obs.commit_ack(call_id="keep-0", ack_at=T0 + timedelta(hours=1))   # 기존 기록은 살아 있어 정상 종료
    assert obs.snapshot()["limits"]["inflight_len"] <= cap
    assert obs.config_at("kb", T0 + timedelta(hours=2)) == ConfigState(None, None)


def test_late_ack_of_a_discarded_completed_id_is_only_a_duplicate(obs):
    _baseline(obs, {"kb": True})
    ack = T0 + timedelta(hours=1)
    _toggle(obs, "kb", False, ack, call_id="old")
    cap = obs.snapshot()["limits"]["completed_ids_cap"]
    for i in range(cap + 10):                                # FIFO 를 넘겨 old 를 밀어낸다
        _toggle(obs, "hana", i % 2 == 0, ack + timedelta(seconds=i + 1), call_id=f"f{i}")
    before = obs.config_at("kb", ack).config_revision
    obs.commit_ack(call_id="old", ack_at=ack + timedelta(hours=1))
    assert obs.config_at("kb", ack).config_revision == before
    assert obs.snapshot()["counters"]["stale_ack_ignored"] >= 1


def test_history_truncation_keeps_the_current_value_known(obs):
    _baseline(obs, {"kb": True})
    cap = obs.snapshot()["limits"]["history_cap"]
    for i in range(cap + 20):
        _toggle(obs, "kb", i % 2 == 0, T0 + timedelta(seconds=i + 1), call_id=f"h{i}")
    last_at = T0 + timedelta(seconds=cap + 20)
    assert obs.config_at("kb", last_at).enabled is ((cap + 19) % 2 == 0)
    assert obs.config_at("kb", T0 + timedelta(seconds=1)) == ConfigState(None, None)   # 잘린 구간
    assert obs.snapshot()["crawlers"]["kb"]["truncated_before"] is not None


# ── 5. epoch ──────────────────────────────────────────────────────────────────

def test_old_epoch_events_are_ignored(obs):
    _baseline(obs, {"kb": True})
    stale_id = obs.baseline_begin()
    obs.start_epoch("epoch-2")                       # 재시작
    later = T0 + timedelta(hours=1)
    assert obs.config_at("kb", later) == ConfigState(None, None)
    obs.baseline_row("kb", True, baseline_id=stale_id)
    obs.baseline_end(baseline_id=stale_id, ok=True)
    assert obs.config_at("kb", later) == ConfigState(None, None)
    obs.commit_ack(call_id="c1", ack_at=later)
    assert obs.config_at("kb", later) == ConfigState(None, None)


# ── 6. 조회 계약 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("at", [None, "2026-09-23", 0, datetime(2026, 9, 23)])
def test_config_at_never_raises_on_bad_input(obs, at):
    """S3 는 bool/None 만 받으므로 관측기가 먼저 막는다 — naive datetime·잘못된 타입도 예외가 아니라 unknown."""
    _baseline(obs, {"kb": True})
    assert obs.config_at("kb", at) == ConfigState(None, None)


def test_config_at_only_returns_bool_or_none(obs):
    _baseline(obs, {"kb": True})
    for crawler in ("kb", "unknown-crawler", ""):
        state = obs.config_at(crawler, T0 + timedelta(hours=1))
        assert state.enabled in (True, False, None)
        assert state.config_revision is None or isinstance(state.config_revision, str)


def test_snapshot_carries_the_agreed_fields(obs):
    _baseline(obs, {"kb": True})
    snap = obs.snapshot()
    assert set(snap) >= {"epoch", "readiness", "baseline", "crawlers", "counters", "limits"}
    assert set(snap["baseline"]) >= {"id", "started_at", "ended_at", "ok", "known", "unknown", "missing"}
    assert set(snap["crawlers"]["kb"]) >= {"enabled", "config_revision", "effective_at", "unknown_reason",
                                           "history_len", "truncated_before"}
    assert set(snap["counters"]) >= {"ack", "not_entered", "result_unknown", "duplicate_ack",
                                     "stale_ack_ignored", "hook_failure", "cache_applied"}
    assert set(snap["limits"]) >= {"inflight_cap", "inflight_len", "history_cap", "completed_ids_cap",
                                   "completed_ids_len"}


def test_observer_feeds_classify_directly(obs):
    _baseline(obs, {"kb": True, "hana": True})
    ack = T0 + timedelta(hours=1)
    _toggle(obs, "kb", False, ack)
    obs.commit_started("hana", False, call_id="stuck")        # 미종료 → unknown
    candidates = [
        SlotCandidate("s1", "kb", "task_kb", ack - timedelta(seconds=1), "IN", "rev", 10, ack),
        SlotCandidate("s2", "kb", "task_kb", ack + timedelta(seconds=1), "IN", "rev", 10, ack),
        SlotCandidate("s3", "hana", "task_hana", ack + timedelta(seconds=2), "IN", "rev", 10, ack),
    ]
    out = classify(candidates, obs.config_at)
    assert [item.expectation_state for item in out] == ["expected", "admin_disabled", "unknown"]
    assert out[0].config_revision and out[1].config_revision
    assert out[2].config_revision is None


def test_ack_while_blocked_does_not_create_known_evidence(obs):
    """기준이 unknown 인 상태의 ack 는 known 으로 승격하지 않는다(보수적 선택을 고정한다).

    ack 만으로 그 시점 값을 안다고 볼 여지도 있지만, 이 계약은 **일관된 baseline 이후**에만 known 을 만든다.
    `config_at` 뿐 아니라 **진단 스냅샷까지** 값을 드러내지 않아야 한다(스냅샷도 공개 계약이다).
    """
    obs.commit_started("kb", False, call_id="x")          # baseline 없이 시작·ack
    obs.commit_ack(call_id="x", ack_at=datetime.now(UTC))
    assert obs.config_at("kb", T0) == ConfigState(None, None)
    row = obs.snapshot()["crawlers"]["kb"]
    assert row["enabled"] is None and row["config_revision"] is None and row["effective_at"] is None


def test_times_before_an_in_flight_call_stay_known(obs):
    """진행 중 토글은 **그 시작 이후**만 가린다 — 시작 전 구간의 known 을 지운다면 과잉이다."""
    _baseline(obs, {"kb": True})
    before = obs.snapshot()["baseline"]["ended_at"] + timedelta(microseconds=1)   # 호출 시작보다 앞
    obs.commit_started("kb", False, call_id="x")
    after = T0 + timedelta(hours=1)
    assert obs.config_at("kb", before).enabled is True
    assert obs.config_at("kb", after) == ConfigState(None, None)


def test_a_toggle_that_completes_during_the_baseline_read_is_not_known(obs):
    """읽는 동안 토글이 **끝나면** 어느 값을 읽었는지 알 수 없다 — 경계의 미종료 호출만으로는 부족하다."""
    bid = obs.baseline_begin()
    obs.baseline_row("kb", True, baseline_id=bid)
    # ⚠️ ack 는 **시작 기록 뒤에** 실제 시계로 잡는다. 인자를 먼저 평가해 시작보다 이른 ack 를 주면
    #    관측기가 시계 역행으로 보고 degraded 가 된다(아래 별도 시험에서 그 규칙을 따로 잠근다).
    obs.commit_started("kb", False, call_id="mid")                        # 읽는 중 시작·종료
    obs.commit_ack(call_id="mid", ack_at=datetime.now(UTC))
    obs.baseline_row("hana", True, baseline_id=bid)
    obs.baseline_end(baseline_id=bid, ok=True)
    later = T0 + timedelta(hours=2)
    assert obs.config_at("kb", later) == ConfigState(None, None)
    assert obs.config_at("hana", later).enabled is True


def test_inflight_overflow_is_tracking_degraded_not_just_unknown(obs):
    _baseline(obs, {"kb": True, "hana": True})
    cap = obs.snapshot()["limits"]["inflight_cap"]
    for i in range(cap + 5):
        obs.commit_started("hana", False, call_id=f"o-{i}")
    snap = obs.snapshot()
    assert snap["readiness"]["reason"] == "tracking_degraded"
    _baseline(obs, {"kb": True, "hana": True})            # 새 baseline 으로도 풀리지 않는다
    later = T0 + timedelta(hours=9)
    assert obs.config_at("kb", later) == ConfigState(None, None)
    assert obs.config_at("hana", later) == ConfigState(None, None)


def test_an_ack_earlier_than_its_own_start_is_a_clock_regression(obs):
    """같은 시계를 쓰는 운영에서는 없어야 할 순서 — 일어나면 효력 순서를 못 세우므로 degraded.

    ⚠️ ack 를 baseline 보다도 이르게 주면 **이력 역행 검사**가 대신 잡아 이 규칙이 안 잠긴다.
    그래서 `baseline_end < ack_at < call.started_at` 사이에 넣는다.
    """
    base = datetime.now(UTC).replace(microsecond=0)
    clock = {"now": base}                                  # 연속 now() 가 같은 값이 될 수 있어 시계를 고정한다
    with patch.object(cco, "_now", lambda: clock["now"]):
        _baseline(obs, {"kb": True})                       # baseline 종료 = base
        clock["now"] = base + timedelta(seconds=2)
        obs.commit_started("kb", False, call_id="back")    # 시작 = base+2s
        obs.commit_ack(call_id="back", ack_at=base + timedelta(seconds=1))   # ack 가 시작보다 1초 이르다
        clock["now"] = base + timedelta(seconds=3)
        assert obs.snapshot()["readiness"]["reason"] == "tracking_degraded"
    assert obs.config_at("kb", T0) == ConfigState(None, None)
