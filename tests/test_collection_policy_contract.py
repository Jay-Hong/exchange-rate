"""collection_expected S2 계약 — 독립 정책 선언 ↔ 운영 등록 구조 일치 + revision 지문.

설계: SOURCE_HEALTH_COLLECTION_EXPECTED.md §2(기대 슬롯과 정책의 독립성)와 S2·S3 인터페이스 r3(Claude·Codex 합의).
정책 표(`app.collection_policy.POLICY`)는 사람이 적는 독립 선언이고, 이 파일은 그것이 운영 등록(`switch_jobs`)과
**구조로** 같음을 잠근다. 등록 코드와 POLICY 를 함께 바꾸면 이 일치 시험은 통과하므로, 의미가 바뀌었는지는 지문과
모드 원본 해시 확인선이 따로 잡는다.
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import pathlib
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

import app.scheduler as sched
from app import collection_policy as cp

REPO = pathlib.Path(__file__).resolve().parent.parent
KST = ZoneInfo("Asia/Seoul")
MODES = ("IN", "BREAK1", "BREAK2", "OUT")
CRAWLERS = ("investing", "dxy", "kb", "hana", "woori", "bs", "citi", "shinhan", "ibk", "nh", "sc")
_UNSET = object()


def _registered(mode, enabled):
    """미기동 운영 스케줄러에 switch_jobs(mode) 를 돌려 등록된 task_* 를 얻고 곧바로 치운다."""
    with patch.object(sched.crawler_manager, "is_enabled", side_effect=enabled):
        sched.switch_jobs(mode)
    try:
        return {job.id: job for job in sched.scheduler.get_jobs() if job.id.startswith("task_")}
    finally:
        for job in list(sched.scheduler.get_jobs()):
            if job.id.startswith("task_"):
                sched.scheduler.remove_job(job.id)


def _cron_struct(trigger):
    assert type(trigger) is CronTrigger, type(trigger)
    explicit = tuple(sorted((f.name, str(f)) for f in trigger.fields if not f.is_default))
    return ("cron", explicit, str(trigger.timezone), trigger.start_date, trigger.end_date, trigger.jitter)


def _trigger_struct(trigger):
    if type(trigger) is OrTrigger:
        return ("or", tuple(_cron_struct(child) for child in trigger.triggers), trigger.jitter)
    return ("single", (_cron_struct(trigger),), None)


def _policy_trigger_struct(policy):
    children = tuple(
        _cron_struct(CronTrigger(**dict(spec.fields), timezone=ZoneInfo(spec.timezone)))
        for spec in policy.triggers
    )
    if len(children) == 1:
        return ("single", children, None)
    return ("or", children, None)


def _effective(job, name):
    """등록 kwargs 에 있으면 그 값, 없으면 **운영 스케줄러 객체의** job 기본값(미기동 Job 에는 속성이 없다)."""
    value = getattr(job, name, _UNSET)
    return sched.scheduler._job_defaults[name] if value is _UNSET else value


def _crawler_from_func(job):
    name = job.func.__name__
    for prefix in ("request_wrapper_", "selenium_wrapper_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    raise AssertionError(f"알 수 없는 래퍼 이름: {name}")


def _states():
    yield "all_on", lambda name: True
    for off in CRAWLERS:
        yield f"off_{off}", (lambda off: lambda name: name != off)(off)
    yield "all_off", lambda name: False


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("state", [s for s, _ in _states()])
def test_policy_matches_registration_structure(mode, state):
    enabled = dict(_states())[state]
    registered = _registered(mode, enabled)
    expected = {jid: p for jid, p in cp.POLICY[mode].items() if enabled(p.crawler)}
    assert set(registered) == set(expected), (mode, state)
    for jid, policy in expected.items():
        job = registered[jid]
        assert _trigger_struct(job.trigger) == _policy_trigger_struct(policy), (mode, jid)
        assert _effective(job, "misfire_grace_time") == policy.misfire_grace_s, (mode, jid)
        assert _effective(job, "coalesce") == policy.coalesce, (mode, jid)
        assert _effective(job, "max_instances") == policy.max_instances, (mode, jid)
        assert _crawler_from_func(job) == policy.crawler == cp.crawler_of(jid), (mode, jid)


def test_ibk_off_also_removes_the_break2_terminal_job():
    registered = _registered("BREAK2", lambda name: name != "ibk")
    assert "task_ibk_terminal" not in registered
    assert "task_ibk_terminal" in cp.POLICY["BREAK2"]


def test_effective_coalesce_reads_the_live_scheduler_default():
    """가짜로 True 를 고정하면 기본값 변경을 놓친다 — 비교기가 실제 기본값을 읽는지 대조로 확인."""
    registered = _registered("IN", lambda name: True)
    job = registered["task_hana"]
    assert getattr(job, "coalesce", _UNSET) is _UNSET, "등록이 coalesce 를 명시하게 되면 이 대조를 다시 설계할 것"
    with patch.dict(sched.scheduler._job_defaults, {"coalesce": False}):
        assert _effective(job, "coalesce") is False
    assert _effective(job, "coalesce") is True


def test_policy_declares_exactly_four_modes_and_coalesce_is_a_real_bool():
    assert set(cp.POLICY) == set(MODES)
    for mode, jobs in cp.POLICY.items():
        for jid, policy in jobs.items():
            assert policy.job_id == jid
            assert isinstance(policy.coalesce, bool)
            assert policy.triggers, (mode, jid)
            for spec in policy.triggers:
                assert spec.timezone == "Asia/Seoul"
                names = [name for name, _ in spec.fields]
                assert len(names) == len(set(names)), (mode, jid)


def test_crawler_of_maps_every_policy_job_and_rejects_unknown():
    for jobs in cp.POLICY.values():
        for jid, policy in jobs.items():
            assert cp.crawler_of(jid) == policy.crawler
    assert cp.crawler_of("task_ibk") == "ibk"
    assert cp.crawler_of("task_ibk_terminal") == "ibk"
    with pytest.raises(KeyError):
        cp.crawler_of("task_unknown")
    with pytest.raises(KeyError):
        cp.crawler_of("websocket_broadcast")


def test_policy_module_is_independent_of_the_scheduler():
    tree = ast.parse((REPO / "app" / "collection_policy.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    forbidden = {name for name in imported if name.startswith(("app.scheduler", "app.main"))}
    assert not forbidden, forbidden


# ── revision 지문 ──────────────────────────────────────────────────────────────

def test_fingerprint_matches_the_approved_value_for_this_revision():
    assert cp.policy_fingerprint() == cp.APPROVED_POLICY_FINGERPRINTS[cp.POLICY_REVISION]


def test_fingerprint_changes_when_a_policy_field_changes():
    base = cp.policy_fingerprint()
    job = cp.POLICY["OUT"]["task_dxy"]
    changed = {mode: dict(jobs) for mode, jobs in cp.POLICY.items()}
    changed["OUT"]["task_dxy"] = dataclasses.replace(job, misfire_grace_s=job.misfire_grace_s + 1)
    assert cp.policy_fingerprint(policy=changed) != base


def test_fingerprint_changes_when_a_mode_boundary_moves_by_seconds():
    """분 표본으로는 못 잡는 06:00:00 → 05:59:30 이동(Codex 반례)을 초 단위 전이 목록이 잡는다."""
    base = cp.policy_fingerprint()
    original = cp.get_market_mode

    def shifted(now):
        local = now.astimezone(KST)
        if local.weekday() == 0 and local.hour == 5 and local.minute == 59 and local.second >= 30:
            return "BREAK2"
        return original(now)

    with patch.object(cp, "get_market_mode", shifted):
        assert cp.policy_fingerprint() != base


def test_fingerprint_changes_when_mode_labels_swap():
    base = cp.policy_fingerprint()
    original = cp.get_market_mode
    swap = {"IN": "BREAK1", "BREAK1": "IN"}

    with patch.object(cp, "get_market_mode", lambda now: swap.get(original(now), original(now))):
        assert cp.policy_fingerprint() != base


def test_market_mode_source_hash_is_the_reviewed_one():
    """모드 함수가 바뀌면 revision 을 올릴지 사람이 검토하게 만드는 확인선(지문은 요일·시각 전제에서만 완전하다)."""
    actual = hashlib.sha256((REPO / "app" / "market_mode.py").read_bytes()).hexdigest()
    assert actual == cp.APPROVED_MODE_SOURCE_SHA256[cp.POLICY_REVISION]


def test_fingerprint_is_deterministic_and_hex():
    first, second = cp.policy_fingerprint(), cp.policy_fingerprint()
    assert first == second
    assert len(first) == 64 and int(first, 16) >= 0


def test_reference_window_is_the_agreed_two_weeks():
    assert cp.FINGERPRINT_WINDOW_KST == (datetime(2026, 9, 28, tzinfo=KST), datetime(2026, 10, 12, tzinfo=KST))
