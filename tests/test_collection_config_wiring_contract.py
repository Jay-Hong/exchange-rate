"""S1a 배선 계약 — 관측 훅을 넣어도 설정 경로의 **동작이 그대로**여야 한다.

현행 사실(코드 확인): `update_crawler_config` 는 commit 뒤 `logger.info` 가 실패하면 True 를 돌려주지 않고
같은 예외를 전파한다. `load_config` 는 완료 로그 실패도 기존 `except` 로 흘러 ERROR 로그 뒤 캐시를 비우며,
그 ERROR 로그마저 실패하면 초기화 전에 예외가 전파된다. `toggle_crawler` 는 CRUD → close → 캐시 → 잠금 →
mode 읽기 → 재등록 순서이고 앞 단계가 실패하면 뒤로 가지 않는다. 관측 훅은 이 모두를 바꾸지 않는다.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import collection_config_observer as cco
from app import crud
from app import scheduler as sched


class RecordingObserver:
    """계약이 요구하는 호출만 기록하는 대역. 실패 주입은 `fail` 로 지정한다."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def _hook(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self.fail:
            raise RuntimeError(f"observer {name} failed")

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: self._hook(name, *a, **k)

    def names(self):
        return [name for name, _, _ in self.calls]


class FakeConfigRow(SimpleNamespace):
    pass


class FakeDB:
    def __init__(self, row=None, *, commit_error=None, close_error=None, log=None):
        self.row = row if row is not None else FakeConfigRow(crawler_name="kb", enabled=True, updated_at=None)
        self.commit_error = commit_error
        self.close_error = close_error
        self.log = log if log is not None else []

    # update_crawler_config 가 쓰는 최소 표면
    def query(self, _model):
        return self

    def filter(self, *_args):
        return self

    def first(self):
        return self.row

    def commit(self):
        self.log.append("commit")
        if self.commit_error:
            raise self.commit_error

    def close(self):
        self.log.append("close")
        if self.close_error:
            raise self.close_error


@pytest.fixture
def observer():
    stub = RecordingObserver()
    with patch.object(cco, "observer", stub):
        yield stub


# ── crud.update_crawler_config ────────────────────────────────────────────────

def test_normal_toggle_records_started_then_ack_and_returns_true(observer):
    db = FakeDB()
    assert crud.update_crawler_config(db, "kb", False) is True
    assert observer.names() == ["commit_started", "commit_ack"]
    assert db.row.enabled is False and db.row.updated_at is not None


def test_ack_is_observed_but_a_failing_completion_log_still_propagates(observer):
    """정정된 기대값 — 현행 코드는 여기서 True 를 돌려주지 않는다."""
    db = FakeDB()
    boom = RuntimeError("log down")
    with patch.object(crud.logger, "info", side_effect=boom):
        with pytest.raises(RuntimeError) as caught:
            crud.update_crawler_config(db, "kb", False)
    assert caught.value is boom
    assert observer.names() == ["commit_started", "commit_ack"]


def test_commit_failure_is_recorded_as_result_unknown_and_reraised(observer):
    boom = RuntimeError("db down")
    db = FakeDB(commit_error=boom)
    with pytest.raises(RuntimeError) as caught:
        crud.update_crawler_config(db, "kb", False)
    assert caught.value is boom
    assert observer.names() == ["commit_started", "commit_result_unknown"]


def test_missing_row_is_recorded_as_not_entered_and_raises_value_error(observer):
    db = FakeDB(row=None)
    db.row = None
    with pytest.raises(ValueError):
        crud.update_crawler_config(db, "kb", False)
    assert observer.names() == ["commit_started", "commit_not_entered"]
    assert "commit" not in db.log


@pytest.mark.parametrize("failing", ["commit_started", "commit_ack"])
def test_observer_failures_do_not_change_the_crud_result(failing):
    stub = RecordingObserver(fail=[failing])
    db = FakeDB()
    with patch.object(cco, "observer", stub):
        assert crud.update_crawler_config(db, "kb", False) is True
    assert db.row.enabled is False


# ── CrawlerManager.load_config ────────────────────────────────────────────────

def _manager():
    return sched.CrawlerManager()


def test_load_config_records_a_baseline_and_fills_the_cache(observer):
    manager = _manager()
    rows = [{"crawler_name": "kb", "enabled": True}, {"crawler_name": "sc", "enabled": False}]
    with patch.object(sched.crud, "get_all_crawler_configs", return_value=rows):
        manager.load_config(FakeDB())
    assert manager.config_cache == {"kb": True, "sc": False}
    assert observer.names()[0] == "baseline_begin"
    assert observer.names().count("baseline_row") == 2
    assert observer.names()[-1] == "baseline_end"
    assert observer.calls[-1][2].get("ok") is True


def test_db_failure_keeps_the_existing_fallback_and_ends_the_baseline_unverified(observer):
    manager = _manager()
    manager.config_cache = {"kb": False}
    with patch.object(sched.crud, "get_all_crawler_configs", side_effect=RuntimeError("db down")):
        manager.load_config(FakeDB())
    assert manager.config_cache == {}                      # 기존 폴백 보존
    assert observer.calls[-1][0] == "baseline_end" and observer.calls[-1][2].get("ok") is False


def test_completion_log_failure_still_clears_the_cache(observer):
    """완료 로그 실패 → ERROR 로그 → 캐시 초기화(현행 동작)."""
    manager = _manager()
    rows = [{"crawler_name": "kb", "enabled": True}]
    with patch.object(sched.crud, "get_all_crawler_configs", return_value=rows), \
         patch.object(sched.logger, "info", side_effect=RuntimeError("log down")), \
         patch.object(sched.logger, "error") as error_log:
        manager.load_config(FakeDB())
    assert manager.config_cache == {}
    assert error_log.called
    assert observer.calls[-1][0] == "baseline_end" and observer.calls[-1][2].get("ok") is False


def test_error_log_failure_propagates_before_the_cache_is_cleared(observer):
    manager = _manager()
    manager.config_cache = {"kb": True}
    with patch.object(sched.crud, "get_all_crawler_configs", side_effect=RuntimeError("db down")), \
         patch.object(sched.logger, "error", side_effect=RuntimeError("error log down")):
        with pytest.raises(RuntimeError):
            manager.load_config(FakeDB())
    assert manager.config_cache == {"kb": True}            # 초기화 전에 전파된다


@pytest.mark.parametrize("failing", ["baseline_begin", "baseline_row", "baseline_end"])
def test_observer_failure_never_triggers_the_cache_fallback(failing):
    manager = _manager()
    stub = RecordingObserver(fail=[failing])
    rows = [{"crawler_name": "kb", "enabled": True}]
    with patch.object(cco, "observer", stub), \
         patch.object(sched.crud, "get_all_crawler_configs", return_value=rows):
        manager.load_config(FakeDB())
    assert manager.config_cache == {"kb": True}


# ── CrawlerManager.toggle_crawler ─────────────────────────────────────────────

def _toggle_env(manager, order, *, mode="IN", crud_error=None, close_error=None, switch_error=None):
    db = FakeDB(close_error=close_error, log=order)

    def fake_update(_db, name, enabled):
        order.append("crud")
        if crud_error:
            raise crud_error
        return True

    def fake_switch(mode_value, cause=None):
        order.append(f"switch_jobs:{mode_value}:{cause}")
        assert manager.config_cache.get("kb") is not None, "재등록 전에 캐시가 적용돼 있어야 한다"
        if switch_error:
            raise switch_error

    return patch.object(sched, "SessionLocal", return_value=db), \
        patch.object(sched.crud, "update_crawler_config", side_effect=fake_update), \
        patch.object(sched, "switch_jobs", side_effect=fake_switch), \
        patch.object(sched, "current_mode", mode)


def test_toggle_order_is_preserved_with_the_observer_hook(observer):
    manager = _manager()
    order = []
    session, update, switch, mode = _toggle_env(manager, order)
    with session, update, switch, mode:
        manager.toggle_crawler("kb", False)
    assert order == ["crud", "close", "switch_jobs:IN:admin_toggle"]
    assert manager.config_cache["kb"] is False
    assert observer.names() == ["cache_applied"]


def test_crud_failure_stops_before_cache_and_registration(observer):
    manager = _manager()
    order = []
    session, update, switch, mode = _toggle_env(manager, order, crud_error=RuntimeError("crud down"))
    with session, update, switch, mode, pytest.raises(RuntimeError):
        manager.toggle_crawler("kb", False)
    assert order == ["crud", "close"]
    assert "kb" not in manager.config_cache
    assert observer.names() == []


def test_close_failure_stops_before_cache_and_registration(observer):
    manager = _manager()
    order = []
    session, update, switch, mode = _toggle_env(manager, order, close_error=RuntimeError("close down"))
    with session, update, switch, mode, pytest.raises(RuntimeError):
        manager.toggle_crawler("kb", False)
    assert order == ["crud", "close"]
    assert "kb" not in manager.config_cache
    assert observer.names() == []


def test_registration_failure_keeps_the_cache_and_the_observed_evidence(observer):
    manager = _manager()
    order = []
    session, update, switch, mode = _toggle_env(manager, order, switch_error=RuntimeError("switch down"))
    with session, update, switch, mode, pytest.raises(RuntimeError):
        manager.toggle_crawler("kb", False)
    assert manager.config_cache["kb"] is False
    assert observer.names() == ["cache_applied"]


def test_no_current_mode_skips_registration_but_keeps_the_cache(observer):
    manager = _manager()
    order = []
    session, update, switch, mode = _toggle_env(manager, order, mode=None)
    with session, update, switch, mode:
        manager.toggle_crawler("kb", False)
    assert order == ["crud", "close"]
    assert manager.config_cache["kb"] is False
    assert observer.names() == ["cache_applied"]


def test_observer_failure_does_not_break_the_toggle():
    manager = _manager()
    order = []
    stub = RecordingObserver(fail=["cache_applied"])
    session, update, switch, mode = _toggle_env(manager, order)
    with patch.object(cco, "observer", stub), session, update, switch, mode:
        manager.toggle_crawler("kb", False)
    assert order == ["crud", "close", "switch_jobs:IN:admin_toggle"]
    assert manager.config_cache["kb"] is False
