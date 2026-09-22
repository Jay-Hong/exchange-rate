"""collection_expected S3 계약 — 순수 슬롯 후보 생성기와 설정 분류.

설계: SOURCE_HEALTH_COLLECTION_EXPECTED.md §2.1(due 시각 기준 mode·grace 고정, 설정 unknown 보존)과
S2·S3 인터페이스 r3(Claude·Codex 합의). 기대 수치는 새 모듈과 **독립적으로** 운영 등록의 실제 트리거를 모드
구간별로 열거해 얻었고(2주 489,230), Codex 가 따로 재현한 값과 같다.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app import collection_policy as cp
from app import collection_slots as cs
from app.market_mode import get_market_mode

KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc
START = datetime(2026, 9, 28, tzinfo=KST)   # 월
END = datetime(2026, 10, 12, tzinfo=KST)    # 다음다음 월, 제외

TWO_WEEK_COUNTS = {
    "task_bs": 20160, "task_citi": 14520, "task_dxy": 92760, "task_hana": 92760,
    "task_ibk": 13200, "task_ibk_terminal": 20, "task_investing": 92760, "task_kb": 92760,
    "task_nh": 20160, "task_sc": 6600, "task_shinhan": 17040, "task_woori": 26490,
}
# §7.13 보완 표(주간)와 같은 값. 두 주라 요일마다 두 번 센다.
WEEKDAY_COUNTS = {
    "task_hana": {"Mon": 6840, "Tue": 8640, "Wed": 8640, "Thu": 8640, "Fri": 8640, "Sat": 3540, "Sun": 1440},
    "task_woori": {"Mon": 1920, "Tue": 2649, "Wed": 2649, "Thu": 2649, "Fri": 2649, "Sat": 729, "Sun": 0},
    "task_ibk_terminal": {"Mon": 0, "Tue": 2, "Wed": 2, "Thu": 2, "Fri": 2, "Sat": 2, "Sun": 0},
}


def _kst(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s, tzinfo=KST)


def _gen(a, b):
    return cs.generate_candidates(a.astimezone(UTC), b.astimezone(UTC))


@pytest.fixture(scope="module")
def two_weeks():
    return _gen(START, END)


def _index(candidates):
    return {(c.job_id, c.due_at_utc): c for c in candidates}


def _at(index, job_id, local):
    return index.get((job_id, local.astimezone(UTC)))


# ── 수량과 모양 ────────────────────────────────────────────────────────────────

def test_two_week_counts_per_job(two_weeks):
    assert Counter(c.job_id for c in two_weeks) == Counter(TWO_WEEK_COUNTS)
    assert len(two_weeks) == 489230


def test_weekday_counts_cross_check_the_713_table(two_weeks):
    counts = Counter((c.job_id, c.due_at_utc.astimezone(KST).strftime("%a")) for c in two_weeks)
    for job_id, per_day in WEEKDAY_COUNTS.items():
        for day, weekly in per_day.items():
            assert counts[(job_id, day)] == 2 * weekly, (job_id, day)


def test_candidates_are_sorted_unique_and_well_formed(two_weeks):
    keys = [(c.due_at_utc, c.job_id) for c in two_weeks]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)
    for c in two_weeks[:: max(1, len(two_weeks) // 5000)]:
        assert c.due_at_utc.tzinfo is not None and c.due_at_utc.utcoffset() == timedelta(0)
        assert c.due_at_utc.microsecond == 0
        assert c.mode == get_market_mode(c.due_at_utc.astimezone(KST))
        policy = cp.POLICY[c.mode][c.job_id]
        assert c.policy_grace_s == policy.misfire_grace_s
        assert c.dispatch_deadline_utc == c.due_at_utc + timedelta(seconds=c.policy_grace_s)
        assert c.crawler == policy.crawler == cp.crawler_of(c.job_id)
        assert c.policy_revision == cp.POLICY_REVISION


# ── 분할 불변 ──────────────────────────────────────────────────────────────────

def _cuts_in(a, b):
    """모드 전이 시각·자정·초 경계를 섞은 절단점(구간 안쪽만)."""
    cuts, prev, t = set(), None, a
    while t < b:
        mode = get_market_mode(t)
        if prev is not None and mode != prev:
            cuts.update({t, t - timedelta(seconds=1), t + timedelta(seconds=1)})
        prev = mode
        t += timedelta(seconds=1)
    day = a.replace(hour=0, minute=0, second=0) + timedelta(days=1)
    while day < b:
        cuts.add(day)
        day += timedelta(days=1)
    cuts.update({a + timedelta(seconds=7), a + timedelta(hours=5, seconds=59), b - timedelta(seconds=1)})
    return sorted(c for c in cuts if a < c < b)


@pytest.mark.parametrize("a,b", [
    (_kst(2026, 10, 2, 18, 0, 0), _kst(2026, 10, 3, 8, 0, 0)),    # 금 IN→BREAK1→토 BREAK2→OUT
    (_kst(2026, 10, 4, 23, 0, 0), _kst(2026, 10, 5, 9, 0, 0)),    # 일 OUT→월 BREAK2→IN
])
def test_split_union_equals_single_enumeration(a, b):
    whole = [(c.job_id, c.due_at_utc) for c in _gen(a, b)]
    points = [a, *_cuts_in(a, b), b]
    parts = []
    for left, right in zip(points, points[1:]):
        parts.extend((c.job_id, c.due_at_utc) for c in _gen(left, right))
    assert parts == whole


def test_two_weeks_split_at_one_midpoint(two_weeks):
    mid = _kst(2026, 10, 5, 6, 0, 0)   # 월 OUT→BREAK2 경계 그 자체
    parts = _gen(START, mid) + _gen(mid, END)
    assert [(c.job_id, c.due_at_utc) for c in parts] == [(c.job_id, c.due_at_utc) for c in two_weeks]


# ── 경계 ───────────────────────────────────────────────────────────────────────

def test_mode_boundaries(two_weeks):
    idx = _index(two_weeks)
    # 금 18:59:58 sc 마지막, 19:00 부터 다음 영업일 08:00 전까지 sc 없음
    assert _at(idx, "task_sc", _kst(2026, 10, 2, 18, 59, 58)).mode == "IN"
    lo, hi = _kst(2026, 10, 2, 19, 0, 0).astimezone(UTC), _kst(2026, 10, 5, 8, 0, 0).astimezone(UTC)
    assert not [c for c in two_weeks if c.job_id == "task_sc" and lo <= c.due_at_utc < hi]
    # 화 02:59:18 shinhan 마지막(그날 08:00 전까지 없음), 05:59:44 woori BREAK1 마지막
    assert _at(idx, "task_shinhan", _kst(2026, 9, 29, 2, 59, 18)).mode == "BREAK1"
    lo, hi = _kst(2026, 9, 29, 2, 59, 19).astimezone(UTC), _kst(2026, 9, 29, 8, 0, 0).astimezone(UTC)
    assert not [c for c in two_weeks if c.job_id == "task_shinhan" and lo <= c.due_at_utc < hi]
    assert _at(idx, "task_woori", _kst(2026, 9, 29, 5, 59, 44)).mode == "BREAK1"
    # 화 06시 woori 마무리 9회와 ibk terminal 2회(모두 BREAK2)
    finish = [(6, m, s) for m in range(4) for s in (14, 44)] + [(6, 4, 53)]
    for h, m, s in finish:
        assert _at(idx, "task_woori", _kst(2026, 9, 29, h, m, s)).mode == "BREAK2"
    for m in (0, 1):
        assert _at(idx, "task_ibk_terminal", _kst(2026, 9, 29, 6, m, 34)).mode == "BREAK2"
    # 토(공휴일 개천절이지만 수집 시계는 공휴일을 보지 않는다) 06:59:57 → 07:00:08
    assert _at(idx, "task_investing", _kst(2026, 10, 3, 6, 59, 57)).mode == "BREAK2"
    assert _at(idx, "task_investing", _kst(2026, 10, 3, 7, 0, 8)).mode == "OUT"
    assert _at(idx, "task_investing", _kst(2026, 10, 3, 7, 0, 7)) is None
    # 월 05:59:08(OUT) → 06:00:07(BREAK2), 07:59:57 → 08:00:07(IN)
    assert _at(idx, "task_investing", _kst(2026, 10, 5, 5, 59, 8)).mode == "OUT"
    assert _at(idx, "task_investing", _kst(2026, 10, 5, 6, 0, 7)).mode == "BREAK2"
    assert _at(idx, "task_investing", _kst(2026, 10, 5, 6, 0, 8)) is None
    assert _at(idx, "task_investing", _kst(2026, 10, 5, 7, 59, 57)).mode == "BREAK2"
    assert _at(idx, "task_investing", _kst(2026, 10, 5, 8, 0, 7)).mode == "IN"


def test_monday_break2_has_no_finish_or_terminal_candidates(two_weeks):
    for c in two_weeks:
        local = c.due_at_utc.astimezone(KST)
        if local.weekday() == 0 and c.job_id in ("task_woori", "task_ibk_terminal"):
            assert local.hour >= 8, c   # 월요일은 IN 의 우리만


def test_adr044_woori_first_two_finish_candidates_exist(two_weeks):
    idx = _index(two_weeks)
    for s in (14, 44):
        c = _at(idx, "task_woori", _kst(2026, 9, 29, 6, 0, s))
        assert c is not None and c.mode == "BREAK2" and c.policy_grace_s == 30


def test_dxy_mode_boundary_and_deadline_inversion(two_weeks):
    idx = _index(two_weeks)
    out = _at(idx, "task_dxy", _kst(2026, 10, 5, 5, 59, 21))
    first_b2 = _at(idx, "task_dxy", _kst(2026, 10, 5, 6, 0, 1))
    later_b2 = _at(idx, "task_dxy", _kst(2026, 10, 5, 6, 0, 21))
    assert (out.mode, out.policy_grace_s) == ("OUT", 120)
    assert out.dispatch_deadline_utc == _kst(2026, 10, 5, 6, 1, 21).astimezone(UTC)
    assert (first_b2.mode, first_b2.policy_grace_s) == ("BREAK2", 5)
    assert first_b2.dispatch_deadline_utc == _kst(2026, 10, 5, 6, 0, 6).astimezone(UTC)
    assert first_b2.dispatch_deadline_utc < out.dispatch_deadline_utc   # deadline 역전
    assert (later_b2.mode, later_b2.policy_grace_s) == ("BREAK2", 5)
    same_due = [c for c in two_weeks if c.job_id == "task_dxy"
                and c.due_at_utc == _kst(2026, 10, 5, 6, 0, 21).astimezone(UTC)]
    assert len(same_due) == 1   # 네 모드 트리거 모두 발화하지만 후보는 하나
    for m in (57, 58):          # OUT 연속 분 후보는 grace 가 겹쳐도 각각 남는다
        assert _at(idx, "task_dxy", _kst(2026, 10, 5, 5, m, 21)).mode == "OUT"
    classified = cs.classify([out, first_b2], lambda crawler, due: cs.ConfigState(True, "r"))
    assert [c.candidate for c in classified] == [out, first_b2]


# ── 입력 계약 ──────────────────────────────────────────────────────────────────

def test_empty_range_is_empty_and_reversed_or_naive_is_rejected():
    t = _kst(2026, 10, 1, 12, 0, 0).astimezone(UTC)
    assert cs.generate_candidates(t, t) == []
    with pytest.raises(ValueError):
        cs.generate_candidates(t, t - timedelta(seconds=1))
    naive = datetime(2026, 10, 1, 3, 0, 0)
    with pytest.raises(ValueError):
        cs.generate_candidates(naive, t)
    with pytest.raises(ValueError):
        cs.generate_candidates(t, naive + timedelta(hours=1))


def test_half_open_interval_includes_start_and_excludes_end():
    due = _kst(2026, 10, 1, 12, 0, 7)   # 목 IN investing
    got = _gen(due, due + timedelta(seconds=1))
    assert [c.job_id for c in got] == ["task_investing"]
    assert not [c for c in _gen(due - timedelta(seconds=1), due) if c.job_id == "task_investing"]


def test_holidays_and_dxy_policy_clock_are_not_inputs():
    a, b = _kst(2026, 10, 2, 18, 0, 0), _kst(2026, 10, 3, 9, 0, 0)
    base = [(c.job_id, c.due_at_utc, c.mode, c.policy_grace_s) for c in _gen(a, b)]
    with patch("app.calendars.kr_holidays.is_kr_holiday", return_value=True), \
         patch("app.calendars.hana_business_days.is_hana_business_day", return_value=False), \
         patch("app.calendars.krx_calendar.is_krx_regular_business_day", return_value=False), \
         patch("app.market_mode.get_dxy_policy_state", return_value="EXTERNAL_BLOCKED"):
        again = [(c.job_id, c.due_at_utc, c.mode, c.policy_grace_s) for c in _gen(a, b)]
    assert again == base


# ── slot_id ────────────────────────────────────────────────────────────────────

def test_slot_id_formula_and_uniqueness(two_weeks):
    sample = two_weeks[12345]
    expected = hashlib.sha256(
        f"{cs.SLOT_SCHEMA_VERSION}|{sample.policy_revision}|{sample.job_id}|{sample.due_at_utc.isoformat()}".encode()
    ).hexdigest()[:32]
    assert sample.slot_id == expected
    assert len({c.slot_id for c in two_weeks}) == len(two_weeks)
    again = _gen(START, START + timedelta(hours=1))
    assert [c.slot_id for c in again] == [c.slot_id for c in two_weeks[:len(again)]]


# ── classify ───────────────────────────────────────────────────────────────────

@pytest.fixture
def morning():
    return _gen(_kst(2026, 9, 29, 5, 59, 0), _kst(2026, 9, 29, 6, 5, 0))


def test_classify_all_off_keeps_every_candidate_as_admin_disabled(morning):
    out = cs.classify(morning, lambda crawler, due: cs.ConfigState(False, "rev-9"))
    assert [c.candidate for c in out] == morning
    assert {c.expectation_state for c in out} == {"admin_disabled"}
    assert {c.config_revision for c in out} == {"rev-9"}


def test_classify_unknown_is_kept_as_unknown(morning):
    out = cs.classify(morning, lambda crawler, due: cs.ConfigState(None, None))
    assert [c.candidate for c in out] == morning
    assert {c.expectation_state for c in out} == {"unknown"}
    assert {c.config_revision for c in out} == {None}


def test_classify_applies_the_effective_boundary_by_due_time(morning):
    boundary = _kst(2026, 9, 29, 6, 1, 0).astimezone(UTC)

    def config_at(crawler, due):
        return cs.ConfigState(True, "old") if due < boundary else cs.ConfigState(False, "new")

    out = cs.classify(morning, config_at)
    for item in out:
        before = item.candidate.due_at_utc < boundary
        assert item.expectation_state == ("expected" if before else "admin_disabled")
        assert item.config_revision == ("old" if before else "new")


def test_classify_calls_config_once_per_candidate_with_crawler_and_due(morning):
    calls = []

    def config_at(crawler, due):
        calls.append((crawler, due))
        return cs.ConfigState(True, "r")

    cs.classify(morning, config_at)
    assert calls == [(c.crawler, c.due_at_utc) for c in morning]


def test_slots_module_is_independent_of_the_scheduler():
    import ast
    import pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "app" / "collection_slots.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not {n for n in imported if n.startswith(("app.scheduler", "app.main"))}


def test_grace_comes_from_the_due_time_mode_even_for_a_shared_trigger():
    """같은 트리거를 두 모드가 공유하고 grace 가 다르면, 후보 grace 는 due 시각 모드의 것이어야 한다.

    현행 POLICY 에서는 공유 트리거의 grace 가 모든 모드에서 같아 이 규칙이 드러나지 않는다(변이 생존) —
    합성 정책으로 직접 잠근다.
    """
    spec = cp.CronSpec((("minute", "*"), ("second", "5")))
    policy = {
        "OUT": {"task_x": cp.JobPolicy("task_x", "x", (spec,), 120)},
        "BREAK2": {"task_x": cp.JobPolicy("task_x", "x", (spec,), 5)},
    }
    got = cs.generate_candidates(_kst(2026, 10, 5, 5, 59, 0).astimezone(UTC),
                                 _kst(2026, 10, 5, 6, 1, 0).astimezone(UTC), policy=policy, revision="t")
    by_due = {c.due_at_utc.astimezone(KST).strftime("%H:%M:%S"): (c.mode, c.policy_grace_s) for c in got}
    assert by_due == {"05:59:05": ("OUT", 120), "06:00:05": ("BREAK2", 5)}


@pytest.mark.parametrize("bad", [1, 0, "true", "false"])
def test_classify_rejects_non_bool_enabled(morning, bad):
    """설정 값이 bool/None 이 아니면 조용히 분류하지 않고 즉시 실패한다(crash early)."""
    with pytest.raises(ValueError):
        cs.classify(morning[:1], lambda crawler, due: cs.ConfigState(bad, "r"))
