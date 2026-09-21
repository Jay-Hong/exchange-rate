"""여러 줄 내부 구조에서도 값 전체가 지워지고 바깥 진단은 남는가.

⛔ 이 파일은 **수정하는 쪽과 다른 쪽이** 쓴 계약이다. 4차 검토에서 드러난 결함은
구현과 시험을 같은 쪽이 쓸 때 생기는 사각을 그대로 보여 줬다 — 변이 17종이 전부
KILLED 였는데도 이 경로가 비어 있었다. 변이 배터리는 **내가 겨냥한 것에만** 민감하다.

결함의 모양: 바깥은 정상적인 한 줄 JSON 인데 `message` 값이 `indent=2` 로 직렬화된
여러 줄 JSON 이면, 그 문자열을 재귀 처리할 때 **구조 파싱보다 줄 분리가 먼저** 일어나
값의 뒷줄이 통째로 살아남는다.
"""

import json

import pytest

from app.crawlers.child_output import child_log_fields, mask_secrets, stdout_tail

CANARY = 'CANARY7391'


def _wrap(inner):
    """바깥은 정상 한 줄 JSON, 안쪽만 여러 줄."""
    return json.dumps({'message': inner, 'exc_info': 'ValueError: boom'})


@pytest.mark.parametrize('inner', [
    pytest.param(json.dumps({'api_key': [CANARY]}, indent=2), id='배열'),
    pytest.param(json.dumps({'credentials': {'v': CANARY}}, indent=2), id='객체'),
    pytest.param(json.dumps({'api_key': [CANARY, 'x']}, indent=2), id='배열-여러항목'),
    pytest.param(json.dumps({'token': {'a': {'b': CANARY}}}, indent=2), id='깊은객체'),
])
@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_a_multiline_inner_structure_is_fully_masked(inner, newline):
    raw = _wrap(inner.replace('\n', newline))
    for name, value in (('mask_secrets', mask_secrets(raw)),
                        ('stdout_tail', stdout_tail(raw)),
                        ('child_log_fields', child_log_fields(raw, prefix='c')['c_tail'])):
        assert CANARY not in value, f'{name} 에서 누출'


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_the_outer_diagnostic_survives_that_masking(newline):
    # 지우는 것과 남기는 것을 **한 시험에서 함께** 본다. 따로 두면 "전부 지움" 도 통과한다.
    raw = _wrap(json.dumps({'api_key': [CANARY]}, indent=2).replace('\n', newline))
    out = mask_secrets(raw)
    assert CANARY not in out
    assert 'ValueError: boom' in out


def test_a_credential_in_an_object_key_is_masked_too():
    # 키도 페이지·환경에서 온 값일 수 있다. 값만 훑으면 키에 든 자격증명이 남는다.
    raw = json.dumps({'https://user:%s@host' % CANARY: 'v'})
    assert CANARY not in mask_secrets(raw)


def test_the_leak_does_not_reach_the_parent_log_field(monkeypatch, caplog):
    """연결부까지 내려가 본다 — 도우미만 보면 실제 노출 경로를 못 본다."""
    import logging
    import subprocess

    from app.crawlers import hana

    raw = _wrap(json.dumps({'api_key': [CANARY]}, indent=2))

    class _Result:
        returncode, stdout, stderr = 1, raw, ''

    monkeypatch.setattr('subprocess.run', lambda *a, **k: _Result())
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            hana._run_selenium_subprocess_fallback('x_selenium', timeout=45)
    for record in caplog.records:
        assert CANARY not in str(getattr(record, 'child_stdout_tail', ''))
        assert CANARY not in record.getMessage()
    assert subprocess.run is not None  # monkeypatch 복원 확인용 앵커


# ── 문맥이 붙어도 보장이 약해지지 않는가 ────────────────────────────────────
# 5차 검토에서 드러난 것: 여러 줄 구조 **단독**은 막히는데, 앞뒤에 문구가 붙거나
# 구조가 둘이거나 출력이 중간에 끊기면 전체 파싱이 실패하고 줄별 처리로 떨어져
# 값의 뒷줄이 살아남는다. 자식 로그는 `request body: {...}` 처럼 문구와 구조를 같은
# 필드에 담는 일이 흔하므로 이것이 실제 경로다.

def _inner():
    return json.dumps({'api_key': [CANARY]}, indent=2)


@pytest.mark.parametrize('shape', [
    pytest.param(lambda i: 'request body: ' + i, id='앞에문구'),
    pytest.param(lambda i: i + '\nafter: ok', id='뒤에진단'),
    pytest.param(lambda i: i.rstrip().rstrip('}'), id='끊긴출력'),
    pytest.param(lambda i: i + '\n' + i, id='구조둘'),
    pytest.param(lambda i: 'head ' + i + '\ntail', id='앞뒤모두'),
])
@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_surrounding_context_does_not_weaken_the_guarantee(shape, newline):
    raw = _wrap(shape(_inner()).replace('\n', newline))
    for name, value in (('mask_secrets', mask_secrets(raw)),
                        ('stdout_tail', stdout_tail(raw)),
                        ('child_log_fields', child_log_fields(raw, prefix='c')['c_tail'])):
        assert CANARY not in value, f'{name} 에서 누출'


@pytest.mark.parametrize('shape', [
    pytest.param(lambda i: 'request body: ' + i, id='앞에문구'),
    pytest.param(lambda i: i + '\nafter: ok', id='뒤에진단'),
])
def test_the_outer_diagnostic_still_survives_with_context(shape):
    # 지우는 것과 남기는 것을 함께 본다 — 따로 두면 "전부 지움" 도 통과한다.
    raw = _wrap(shape(_inner()))
    out = mask_secrets(raw)
    assert CANARY not in out
    assert 'ValueError: boom' in out


# ── 같은 비밀이 값에서 키로 옮겨가도 보호되는가 ──────────────────────────────

@pytest.mark.parametrize('key', [
    pytest.param('api_key=%s' % CANARY, id='대입형'),
    pytest.param(json.dumps({'api_key': CANARY}), id='직렬화된JSON'),
    pytest.param('Authorization: Bearer %s' % CANARY, id='헤더형'),
])
def test_a_secret_moved_into_an_object_key_is_still_masked(key):
    # 키도 외부 입력이다. 값만 훑으면 같은 문자열이 키로 옮겨가는 것만으로 노출된다.
    assert CANARY not in mask_secrets(json.dumps({key: 'v'}))


# ── 7차 검토: 파싱 안 되는 덩어리에서 비밀을 도려내려는 시도의 한계 ─────────
# 여기부터는 "경우를 하나씩 막는다" 가 아니라 **원칙**을 잠근다: 깨끗이 파싱되지
# 않는 텍스트에 비밀 낱말이 있으면, 그 안에서 정확한 값 경계를 찾으려 하지 말고
# 가린다. 아래 셋은 경계 추측이 틀리는 대표 모양이다.

@pytest.mark.parametrize('inner', [
    pytest.param('body: {"api_key":\n "%s"}' % CANARY, id='다음줄스칼라'),
    pytest.param('body: {"api_key"\n: "%s"}' % CANARY, id='다음줄콜론'),
    pytest.param('body: {"api_key": """%s\nmore"""}' % CANARY, id='여러줄삼중따옴표'),
])
def test_an_unparseable_fragment_with_a_secret_name_does_not_leak(inner):
    raw = _wrap(inner)
    for name, value in (('mask_secrets', mask_secrets(raw)),
                        ('stdout_tail', stdout_tail(raw)),
                        ('child_log_fields', child_log_fields(raw, prefix='c')['c_tail'])):
        assert CANARY not in value, f'{name} 에서 누출'
    assert 'ValueError: boom' in mask_secrets(raw)   # 바깥 진단은 별도 필드라 남는다


@pytest.mark.parametrize('literal', [
    pytest.param("{('api_key=%s',): 'v'}" % CANARY, id='튜플키'),
    pytest.param("{(('x', 'token=%s'),): 'v'}" % CANARY, id='중첩튜플키'),
])
def test_non_string_keys_are_traversed_too(literal):
    # 키가 문자열이 아니어도(Python repr 의 튜플) 그 안의 문자열은 외부 입력이다.
    assert CANARY not in mask_secrets(literal)


# ── 깊이 상한은 **끝값의 종류와 무관하게** 실패 표지를 남겨야 한다 ──────────
# 7차 검토가 내 결론을 뒤집었다. 나는 `_mask_value` 의 깊이 가드만 지운 변이가
# 살아남은 것을 "변이 오류" 라고 적었는데, 끝값이 **스칼라**(123·None·True)면 그 가드
# 하나만으로 동작이 바뀐다(실측: 실패 표지가 사라진다). 기존 깊이 시험이 문자열 키가 든
# 입력 **하나만** 봐서 그 경로를 지나쳤을 뿐이다. 입력 하나로 재고 일반화한 내 실수다.

@pytest.mark.parametrize('leaf', [
    pytest.param({'api_key': CANARY}, id='비밀객체'),
    pytest.param(123, id='정수'),
    pytest.param(None, id='None'),
    pytest.param(True, id='불리언'),
    pytest.param('plain', id='문자열'),
])
def test_the_depth_limit_marks_failure_whatever_the_leaf_is(leaf):
    from app.crawlers.child_output import MASK_FAILED

    value = leaf
    for _ in range(80):
        value = [value]
    out = mask_secrets(json.dumps({'details': value, 'exc_info': 'Traceback: boom'}))
    assert MASK_FAILED in out
    assert CANARY not in out
    assert json.loads(out)['exc_info'] == 'Traceback: boom'


# ── 이 조각의 존재 이유: 멀쩡한 로그 줄의 추적 기록은 **다른 줄 때문에** 사라지면 안 된다 ──
# 8차에서 비정형 덩어리를 통째로 가리는 원칙으로 바꾼 뒤, 깨진 줄이 하나라도 섞이면
# **출력 전체**를 한 덩어리로 보게 됐다. 그래서 비밀 대입이 든 비JSON 줄 하나가 멀쩡한
# JSON 줄의 `exc_info` 까지 함께 가렸다(실측). 누출은 없었지만 이 조각이 전하려던 것이 사라졌다.
#
# 원칙: 완전한 JSON 로그 줄은 **독립 레코드**다. 깨진 줄은 **연속된 것끼리만** 묶는다.
# 그래야 여러 줄에 걸친 비밀은 함께 가려지고, 멀쩡한 줄의 추적 기록은 남는다.

def _log(msg, **extra):
    d = {'timestamp': '2026-09-21T00:00:59', 'level': 'INFO', 'message': msg}
    d.update(extra)
    return json.dumps(d, ensure_ascii=False)


_TRACEBACK = 'Traceback (most recent call last):\n  ValueError: selector failed'
_FAILURE = _log('subprocess failed', level='ERROR', exc_info=_TRACEBACK)


@pytest.mark.parametrize('noise', [
    pytest.param(['api_key=%s' % CANARY], id='비JSON줄에비밀'),
    pytest.param(['{"timestamp": "2026', 'token=%s' % CANARY], id='잘린줄과비밀줄'),
    pytest.param(['body: {"api_key":', ' "%s"}' % CANARY], id='여러줄에걸친비밀'),
])
@pytest.mark.parametrize('position', ['앞', '뒤'])
def test_a_well_formed_log_line_keeps_its_traceback_despite_other_lines(noise, position):
    lines = [_log('start')] + (noise + [_FAILURE] if position == '앞' else [_FAILURE] + noise)
    stdout = '\n'.join(lines)
    for name, value in (('mask_secrets', mask_secrets(stdout)),
                        ('stdout_tail', stdout_tail(stdout))):
        assert CANARY not in value, f'{name} 에서 누출'
        assert 'selector failed' in value, f'{name} 에서 추적 기록 소실 — 이 조각의 목적'


# ── 깊이 가드 **둘 다** 각자의 자리에서 일한다 ─────────────────────────────
# 10차 검토에서 D2(`_mask_text` 쪽 가드만 제거) 생존이 **시험 빈틈**으로 판정됐다. 위의 깊이
# 시험(80겹 목록)은 항상 `_mask_value` 쪽 가드가 먼저 걸려 `_mask_text` 가드가 먼저 발화하는
# 경로를 밟지 않았다. 문자열이 `_mask_value(depth=31)` 을 지나 `_mask_text(depth=32)` 로 들어가는
# 입력이 그 경로다. 나는 중첩 JSON 문자열로 그 경로를 만들려다 이스케이프 폭증으로 포기했는데,
# **목록 30겹 + 문자열** 이면 67자로 충분했다(검토가 찾음).

def test_the_text_level_depth_guard_fires_on_its_own_path():
    from app.crawlers.child_output import MASK_FAILED

    assert MASK_FAILED in mask_secrets('[' * 30 + '"plain"' + ']' * 30)


def test_the_text_level_depth_guard_keeps_the_outer_traceback():
    # 로그 레코드로 감싸면 목록 29겹이 같은 경로다. 실패 표지는 남고 추적 기록은 보존된다.
    from app.crawlers.child_output import MASK_FAILED

    value = 'plain'
    for _ in range(29):
        value = [value]
    raw = json.dumps({'details': value, 'exc_info': 'Traceback: boom'})
    for name, out in (('mask_secrets', mask_secrets(raw)), ('stdout_tail', stdout_tail(raw)),
                      ('child_log_fields', child_log_fields(raw, prefix='c')['c_tail'])):
        assert MASK_FAILED in out, f'{name} 에서 실패 표지 없음'
        assert 'Traceback: boom' in out, f'{name} 에서 추적 소실'
