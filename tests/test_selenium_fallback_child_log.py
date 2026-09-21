"""hana·woori 폴백이 자식 stdout 을 실제로 로그에 남기는가.

도우미가 존재한다는 것과 호출된다는 것은 다르다. 2026-09-21 의 실패는 코드가 없어서가
아니라 **읽는 스트림이 틀려서** 원인을 잃었다 — 그래서 이 시험은 도우미가 아니라
연결부를 본다.
"""

import logging
import subprocess

import pytest

from app.crawlers import hana, woori

MODULES = [pytest.param(hana, id='hana'), pytest.param(woori, id='woori')]


class _Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _records(caplog, level):
    return [r for r in caplog.records if r.levelno == level]


@pytest.mark.parametrize('module', MODULES)
def test_failure_logs_the_child_stdout_tail(module, monkeypatch, caplog):
    child = 'startup noise\n' * 200 + 'Traceback (most recent call last):\nValueError: boom\n'
    # 두 크롤러 모두 함수 안에서 `import subprocess` 하므로 전역 이름을 갈면 잡힌다.
    monkeypatch.setattr('subprocess.run', lambda *a, **k: _Result(1, child, ''))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            module._run_selenium_subprocess_fallback('x_selenium', timeout=45)
    errors = _records(caplog, logging.ERROR)
    assert errors, '실패가 ERROR 로 남지 않았다'
    tails = [getattr(r, 'child_stdout_tail', None) for r in errors]
    assert any(t and 'ValueError: boom' in t for t in tails), \
        '자식 추적 기록이 로그에 실리지 않았다 — 이 슬라이스가 고치려던 바로 그 상태'
    assert any(getattr(r, 'child_stdout_chars', 0) == len(child) for r in errors)
    assert any(getattr(r, 'child_stdout_truncated', None) is True for r in errors)
    assert any(getattr(r, 'exit_code', None) == 1 for r in errors)
    assert any(getattr(r, 'subprocess_name', None) == 'x_selenium' for r in errors)


@pytest.mark.parametrize('module', MODULES)
def test_failure_still_raises_and_does_not_change_control_flow(module, monkeypatch):
    # ⛔ 보고 전용이다. 예외 종류·전파는 그대로여야 한다(§7.13 경계).
    monkeypatch.setattr('subprocess.run', lambda *a, **k: _Result(3, 'x', 'y'))
    with pytest.raises(RuntimeError, match='exit code 3'):
        module._run_selenium_subprocess_fallback('x_selenium', timeout=45)


@pytest.mark.parametrize('module', MODULES)
def test_timeout_logs_what_the_child_managed_to_write(module, monkeypatch, caplog):
    # TimeoutExpired.stdout 은 bytes 다(3.13.5 실측). 프레임 없는 타임아웃에서
    # 유일하게 남는 흔적이라 여기서도 남겨야 한다.
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd='x', timeout=45, output=b'partial work\n')
    monkeypatch.setattr('subprocess.run', boom)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RuntimeError, match='timeout'):
            module._run_selenium_subprocess_fallback('x_selenium', timeout=45)
    tails = [getattr(r, 'child_stdout_tail', None) for r in caplog.records]
    assert any(t and 'partial work' in t for t in tails)


@pytest.mark.parametrize('module', MODULES)
def test_secrets_in_child_output_never_reach_the_log(module, monkeypatch, caplog):
    monkeypatch.setattr('subprocess.run',
                        lambda *a, **k: _Result(1, '{"KIS_APP_KEY": "PS-live-secret"}', ''))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            module._run_selenium_subprocess_fallback('x_selenium', timeout=45)
    for record in caplog.records:
        assert 'PS-live-secret' not in str(getattr(record, 'child_stdout_tail', ''))
        assert 'PS-live-secret' not in record.getMessage()


@pytest.mark.parametrize('module', MODULES)
def test_secrets_in_child_stderr_never_reach_the_log(module, monkeypatch, caplog):
    # stderr 는 **메시지 본문**에 실린다. stdout 만 지우고 stderr 를 날것으로 두면
    # 같은 로그 줄로 그대로 새어 나간다.
    monkeypatch.setattr('subprocess.run',
                        lambda *a, **k: _Result(1, '', 'APP_SECRET=stderr-leak-1'))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            module._run_selenium_subprocess_fallback('x_selenium', timeout=45)
    for record in caplog.records:
        assert 'stderr-leak-1' not in record.getMessage()


@pytest.mark.parametrize('module', MODULES)
def test_empty_stderr_still_reads_as_no_error_output(module, monkeypatch, caplog):
    # 마스킹을 거치면서 빈 문자열이 되어도 기존 문구를 유지해야 한다(운영 로그 검색 호환).
    monkeypatch.setattr('subprocess.run', lambda *a, **k: _Result(1, 'x', ''))
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            module._run_selenium_subprocess_fallback('x_selenium', timeout=45)
    assert any('No error output' in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize('module', MODULES)
def test_success_path_logs_no_child_output(module, monkeypatch, caplog):
    # 성공은 99시간에 12,439회다. 매번 자식 로그를 실으면 로그량이 무의미하게 는다.
    monkeypatch.setattr('subprocess.run', lambda *a, **k: _Result(0, 'lots of output', ''))
    with caplog.at_level(logging.DEBUG):
        module._run_selenium_subprocess_fallback('x_selenium', timeout=45)
    assert not any(getattr(r, 'child_stdout_tail', None) for r in caplog.records)
