"""자식 stdout 꼬리 — 무엇을 남기고 무엇을 지우는가.

지우는 쪽 시험을 먼저 둔다: 이 도우미가 생긴 순간부터 자식 로그가 컨테이너 로그로
옮겨지고, 그게 곧 새 노출이기 때문이다.
"""

import ast
import json
import time

import pytest

from app.crawlers.child_output import (MASK, MASK_FAILED, TAIL_LIMIT, child_log_fields,
                                       mask_secrets, stdout_tail)


# ── 지우는 쪽 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,secret', [
    ('{"api_key": "sk-abcdef123456"}', 'sk-abcdef123456'),
    ('{"APP_SECRET": "hunter2"}', 'hunter2'),
    ("PASSWORD='p@ssw0rd'", 'p@ssw0rd'),
    ('KIS_APP_KEY=PSabc123def', 'PSabc123def'),
    ('{"access_token": "eyJhbGciOi"}', 'eyJhbGciOi'),
    ('db_password=letmein;host=x', 'letmein'),
    ('{"REFRESH_TOKEN":"r-9911"}', 'r-9911'),
    # 아래 다섯은 1차 구현이 **그대로 통과시켰다**(검토에서 재현됨).
    # 자식 로그는 JSON 이고, 그 안의 message 에는 따옴표가 이스케이프돼 들어온다.
    ("{'api_key': 'FAKESECRET123'}", 'FAKESECRET123'),
    ('{"message": "{\\"api_key\\": \\"FAKESECRET123\\"}"}', 'FAKESECRET123'),
    ('{"api_key": "CUT-OFF-SECRET', 'CUT-OFF-SECRET'),
    ('Authorization: Bearer eyJhbGciOiJIUzI1', 'eyJhbGciOiJIUzI1'),
    ('postgresql://usr:SuperPw@host:5432/db', 'SuperPw'),
    # 2차 검토에서 재현된 셋. 이름을 **정확히 일치**로 보거나 URL 사용자명을 `+` 로 두면 샌다.
    ('Proxy-Authorization: Basic FAKESECRET123', 'FAKESECRET123'),
    ('redis://:FAKESECRET123@localhost:6379/0', 'FAKESECRET123'),
    ("Set-Cookie: ['theme=light; Path=/', 'sid=FAKESECRET123; Path=/']", 'FAKESECRET123'),
    # 4차: 값 전체가 배열·객체·bytes인 경우도 내부 일부만 지우면 안 된다.
    ('{"tokens": ["dummy", "SECRET"]}', 'SECRET'),
    ('{"credentials": {"username": "d", "value": "SECRET"}}', 'SECRET'),
    ("{'api_key': b'head SECRET'}", 'SECRET'),
    ('Cookie: s=abc; t=SECRET', 'SECRET'),
])
def test_secret_values_never_survive(text, secret):
    out = mask_secrets(text)
    assert secret not in out
    assert '***MASKED***' in out


def test_the_name_survives_so_the_log_still_says_what_was_dropped():
    # 이름까지 지우면 "무엇이 비었는지" 를 못 읽어 진단이 안 된다.
    out = mask_secrets('{"api_key": "sk-1"}')
    assert 'api_key' in out


@pytest.mark.parametrize('text', [
    '{"bank": "hana", "rate": 1393.5}',
    'Traceback (most recent call last):',
    '{"message": "MIBANK_HANA_URL 시도"}',
    # 낱말이 산문에 나오지만 `:`·`=` 가 뒤따르지 않으면 값이 아니다.
    'cookies enabled',
    # 자격증명 없는 평범한 URL 을 건드리면 안 된다.
    'https://fx.kbstar.com/quics?page=C016always',
    'https://fx.kbstar.com/quics?page=C016',
    # 누락 키가 없으면 환율 파싱 실패를 진단하기 어렵다. 비밀 대입식은 별도로 검사한다.
    "KeyError: 'USD'",
    '{"message":"cookies enabled", "rate":1393.5}',
])
def test_ordinary_lines_are_left_alone(text):
    assert mask_secrets(text) == text


def test_masking_happens_before_truncation():
    # 자르고 나서 지우면 경계가 이름을 잘라내 값 조각만 남는다. 그 순서였다면
    # 이 입력의 꼬리는 `def123456"}` 로 끝나고 규칙은 그것을 못 알아본다.
    text = 'x' * 400 + '{"api_key": "sk-abcdef123456"}' + 'y' * 5
    out = stdout_tail(text, limit=20)
    assert 'abcdef123456' not in out
    assert out == MASK  # 파싱 불가인 한 덩어리는 잘리기 전에 전체가 지워진다.


def test_a_secret_far_from_the_tail_is_still_masked_before_it_is_cut():
    # 전체를 먼저 지우므로, 꼬리 밖에 있던 비밀도 노출 경로가 생기지 않는다.
    text = '{"api_key": "sk-zzz"}' + 'a' * 5000
    assert 'sk-zzz' not in mask_secrets(text)


# ── 남기는 쪽 ────────────────────────────────────────────────────────────────

def test_the_tail_is_kept_not_the_head():
    # 자식 stdout 은 JSON 로그 줄의 연속이고 추적 기록은 **끝**에 있다.
    text = 'START' + 'm' * 3000 + 'TRACEBACK-HERE'
    out = stdout_tail(text, limit=50)
    assert out.endswith('TRACEBACK-HERE')
    assert 'START' not in out


def test_short_output_is_returned_whole():
    assert stdout_tail('boom', limit=TAIL_LIMIT) == 'boom'


@pytest.mark.parametrize('value', [None, ''])
def test_absent_output_is_an_empty_string_not_a_crash(value):
    # 자식이 아무것도 안 남기고 죽는 경우가 실제로 있었다(stderr 가 비어 있던 그 실패).
    assert stdout_tail(value) == ''
    assert mask_secrets(value) == ''


def test_zero_limit_keeps_nothing():
    # 경계값: 잘못 읽으면 `[-0:]` 가 전체를 돌려준다.
    assert stdout_tail('abcdef', limit=0) == ''


def test_default_limit_is_applied():
    assert len(stdout_tail('z' * 9000)) == TAIL_LIMIT


# ── 타임아웃 경로가 넘기는 것 ────────────────────────────────────────────────
# `subprocess.run(text=True, timeout=...)` 이 시간을 넘기면 TimeoutExpired 의 stdout 은
# **bytes** 이고 stderr 는 None 이다(3.13.5 실측). str 만 가정하면 가장 드문 경로에서만 터진다.

def test_bytes_output_is_accepted():
    assert stdout_tail(b'TRACEBACK-HERE', limit=50) == 'TRACEBACK-HERE'


def test_bytes_secrets_are_masked_too():
    out = stdout_tail(b'{"api_key": "sk-bytes-1"}')
    assert 'sk-bytes-1' not in out
    assert '***MASKED***' in out


def test_undecodable_bytes_do_not_crash():
    # 자식이 죽는 순간 잘린 UTF-8 조각이 남을 수 있다.
    out = stdout_tail(b'ok \xed\xa0 tail')
    assert 'tail' in out


def test_the_timeout_path_really_hands_over_bytes():
    # 위 세 시험의 전제를 실제 subprocess 로 고정한다. 이 전제가 바뀌면 여기서 먼저 깨진다.
    import subprocess
    import sys
    code = "import sys,time; print('BEFORE-KILL'); sys.stdout.flush(); time.sleep(5)"
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=0.7, text=True)
    assert isinstance(caught.value.stdout, bytes)
    assert stdout_tail(caught.value.stdout) == 'BEFORE-KILL\n'


# ── 로그 필드 묶음 ───────────────────────────────────────────────────────────

def test_fields_report_character_count_not_bytes():
    # 한글이 섞이면 바이트 수와 문자 수가 다르다. 이름이 `_chars` 인 이유.
    f = child_log_fields("환율 데이터 추출 실패", prefix="child_stdout")
    assert f["child_stdout_chars"] == 12


def test_truncation_is_reported_separately_from_length():
    # 마스킹이 길이를 바꾸므로 원문 길이와 꼬리 길이 비교로는 잘림을 알 수 없다.
    short = child_log_fields("boom", prefix="child_stdout")
    assert short["child_stdout_truncated"] is False
    long = child_log_fields("z" * 9000, prefix="child_stdout")
    assert long["child_stdout_truncated"] is True
    assert len(long["child_stdout_tail"]) == TAIL_LIMIT


def test_a_masked_secret_does_not_look_like_truncation():
    # 마스킹으로 길이가 늘거나 줄어도 잘림 표시는 사실이어야 한다.
    f = child_log_fields('{"api_key": "x"}', prefix="child_stdout")
    assert f["child_stdout_truncated"] is False
    assert "***MASKED***" in f["child_stdout_tail"]


def test_masking_failure_yields_a_marker_not_the_raw_text(monkeypatch):
    # 진단 하나 때문에 비밀을 흘리는 교환은 하지 않는다.
    import app.crawlers.child_output as mod

    def boom(_):
        raise RuntimeError("regex exploded")

    monkeypatch.setattr(mod, "mask_secrets", boom)
    assert mod.stdout_tail('{"api_key": "sk-live"}') == MASK_FAILED


def test_prefix_is_applied_to_every_field():
    f = child_log_fields("x", prefix="child_stderr")
    assert set(f) == {"child_stderr_tail", "child_stderr_chars", "child_stderr_truncated"}


# ── 처리 시간 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('make', [
    pytest.param(lambda n: 'x' * n, id='낱말없음'),
    # ⛔ `x` 만으로는 이름 확장 경로를 안 밟는다. 낱말이 이어진 입력이 제곱을 드러낸다
    # (2차 검토 실측: 16,000자 9.9초). 한 축만 재면 다른 축의 제곱을 못 본다.
    pytest.param(lambda n: ('key' * ((n + 2) // 3))[:n], id='낱말반복'),
])
def test_masking_is_linear_on_both_axes(make):
    import time

    start = time.perf_counter()
    child_log_fields(make(1_000_000), prefix='child_stdout')
    assert time.perf_counter() - start < 3.0


def test_masking_is_linear_not_quadratic():
    """1차 구현은 8,000자에 8초, 200,000자에 113초였다(실측).

    이 후처리는 **subprocess.run(timeout=45)의 반환/시간초과 뒤**에 돈다. 자식의 45초
    창에는 포함되지 않지만, 그 지연만큼 부모의 예외 전파와 다음 폴백이 늦어진다.
    원인은 이름 양쪽에 `*` 를 둔 정규식을 **모든 위치에서** 시도한 것과, URL 패턴을
    `[a-zA-Z][a-zA-Z0-9+.-]*://` 로 시작한 것이었다.

    1,000,000자를 2초 안에 끝내야 한다. 제곱으로 돌아가면 여기서 걸린다.
    """
    import time

    start = time.perf_counter()
    child_log_fields('x' * 1_000_000, prefix='child_stdout')
    assert time.perf_counter() - start < 2.0


def test_masking_runs_once_per_call():
    # 1차 구현은 `child_log_fields` 에서 한 번, `stdout_tail` 안에서 또 한 번 돌렸다.
    import app.crawlers.child_output as mod

    calls = []
    real = mod.mask_secrets

    def counting(text):
        calls.append(1)
        return real(text)

    mod.mask_secrets = counting
    try:
        mod.child_log_fields('{"api_key": "x"}', prefix='child_stdout')
    finally:
        mod.mask_secrets = real
    assert len(calls) == 1


# ── 값의 경계 ────────────────────────────────────────────────────────────────

def test_an_unstructured_header_masks_the_whole_chunk():
    # `Authorization: Bearer eyJ...` 는 공백 뒤가 본체다. 공백에서 끊으면
    # `Bearer` 만 지우고 토큰을 남긴다(1차 구현 실패, 실측).
    out = mask_secrets('Authorization: Bearer eyJhbGciOi\nnext line kept')
    assert out == MASK


def test_a_bare_value_stops_at_its_delimiter():
    # 줄 끝까지 먹으면 뒤따르는 진단 정보를 잃는다.
    out = mask_secrets('{"api_key": "x", "bank": "hana"}')
    assert '"bank": "hana"' in out


def test_the_name_is_matched_whole_not_by_substring_alone():
    # 낱말이 들어 있어도 `:`·`=` 가 없으면 값이 아니다.
    assert mask_secrets('monkey business') == 'monkey business'


# ── 값이 끝나는 자리 ─────────────────────────────────────────────────────────

def test_an_escaped_value_does_not_swallow_what_follows():
    """이 조각이 존재하는 이유가 걸려 있다.

    닫는 `\\"` 를 역슬래시 분기보다 **나중에** 보면 그 분기가 닫는 따옴표 자신을 건너뛰어
    값이 줄 끝까지 번지고, 전하려던 `exc_info` 가 통째로 사라진다(2차 검토 실측).
    비밀은 지워졌으니 출력만 보면 맞아 보인다 — 그래서 눈으로는 안 잡혔다.
    """
    text = ('{"message": "{\\"api_key\\": \\"FAKESECRET123\\"}", '
            '"exc_info": "Traceback: ValueError: boom"}')
    out = mask_secrets(text)
    assert 'FAKESECRET123' not in out
    assert 'ValueError: boom' in out


def test_a_header_name_with_a_prefix_is_still_a_header():
    # `Proxy-Authorization` 은 정확히 일치가 아니다. 부분 일치로 봐야 잡힌다.
    assert 'FAKESECRET123' not in mask_secrets('Proxy-Authorization: Basic FAKESECRET123')


def test_a_url_with_no_username_is_still_a_credential():
    # `redis://:password@host` 는 실제로 쓰이는 형태다.
    assert 'FAKESECRET123' not in mask_secrets('redis://:FAKESECRET123@localhost:6379/0')


# ── 파싱된 값 전체와 바깥 진단의 보존 ──────────────────────────────────────

@pytest.mark.parametrize('name', [
    'api_key', 'tokens', 'credentials', 'Proxy-Authorization', 'Set-Cookie',
])
@pytest.mark.parametrize('value', [
    'head"SECRET', ['dummy', 'SECRET'], {'username': 'd', 'value': 'SECRET'},
    12345, True, None,
])
def test_sensitive_json_values_are_replaced_whole(name, value):
    """값의 자료형·인용부호를 추측하지 않고, 해당 필드 전체를 하나의 표지로 바꾼다."""
    record = {name: value, 'bank': 'hana', 'rate': 1393.5,
              'exc_info': 'Traceback: ValueError: boom'}
    masked = json.loads(mask_secrets(json.dumps(record)))
    assert masked == {**record, name: MASK}


@pytest.mark.parametrize('message', [
    json.dumps({'api_key': 'SECRET'}),
    # 바깥 JSON이 이스케이프를 한 겹 더 만든다. 직접 조립하지 않아 실제 formatter와 같다.
    json.dumps({'api_key': 'head"SECRET'}),
    json.dumps({'api_key': 'head\\"SECRET'}),
    json.dumps({'tokens': ['dummy', 'SECRET']}),
    json.dumps({'credentials': {'username': 'd', 'value': 'SECRET'}}),
    "{'api_key': b'head SECRET'}",
    "{'api_key': 'head\"SECRET'}",
    'Authorization: Bearer SECRET',
    'Proxy-Authorization: Basic SECRET',
    'Cookie: s=abc; t=SECRET',
    "Set-Cookie: ['a=1', 'sid=SECRET']",
    'postgresql://usr:SECRET@host',
    'redis://:SECRET@host',
    '{"api_key": "CUT-OFF-SECRET',
    # 파싱 불가인 message도 바깥 exc_info와는 별개다.
    'prefix {"tokens": ["dummy", "SECRET"]}',
])
@pytest.mark.parametrize('serialize,parse', [
    pytest.param(json.dumps, json.loads, id='json'),
    pytest.param(repr, ast.literal_eval, id='python-repr'),
])
def test_nested_messages_never_consume_sibling_traceback(message, serialize, parse):
    """네 라운드의 누출 입력 모두에 exc_info를 붙여 누출과 진단 손실을 동시에 검증한다."""
    trace = 'Traceback (most recent call last):\nKeyError: \'USD\'\nValueError: boom'
    record = {'message': message, 'exc_info': trace, 'bank': 'hana'}
    out = mask_secrets(serialize(record))
    assert 'SECRET' not in out
    assert MASK in out
    masked = parse(out)
    assert masked['exc_info'] == trace
    assert masked['bank'] == 'hana'


def test_cookie_status_does_not_consume_sibling_traceback():
    """cookie를 포함한 진단 이름을 보수적으로 가려도 바깥 추적 기록은 보존한다."""
    record = {'message': 'cookie_status: expired', 'exc_info': 'Traceback: ...'}
    assert json.loads(mask_secrets(json.dumps(record)))['exc_info'] == record['exc_info']


def test_nested_containers_and_encoded_messages_are_all_visited():
    """message 외의 문자열과 배열 속 객체에서도 동일한 비밀 규칙을 적용한다."""
    record = {'details': [{'api_key': 'SECRET-1'},
                          {'body': json.dumps({'tokens': ['SECRET-2']})},
                          'Proxy-Authorization: Basic SECRET-3'],
              'exc_info': 'Traceback: boom'}
    out = mask_secrets(json.dumps(record))
    assert 'SECRET' not in out
    assert json.loads(out)['exc_info'] == record['exc_info']


def test_multiple_json_encoding_layers_are_decoded():
    """JSON 안 JSON이 다시 문자열로 인코딩되어도 가장 안쪽 값을 지운다."""
    payload = json.dumps({'api_key': 'head"SECRET'})
    for _ in range(4):
        payload = json.dumps(payload)
    masked = mask_secrets(json.dumps({'message': payload, 'exc_info': 'boom'}))
    assert 'SECRET' not in masked
    assert json.loads(masked)['exc_info'] == 'boom'


@pytest.mark.parametrize('literal', [
    '{"message": "api_key=SECRET", "message": "ordinary", "exc_info": "boom"}',
    "{'message': 'api_key=SECRET', 'message': 'ordinary', 'exc_info': 'boom'}",
    '{"api_key": "SECRET", "api_key": "***MASKED***", "exc_info": "boom"}',
    # 중첩 객체 안의 중복도 파서가 버린 원문을 되살리면 안 된다.
    '{"details": {"message": "api_key=SECRET", "message": "ordinary"}, "exc_info": "boom"}',
])
def test_duplicate_keys_do_not_restore_unvisited_original_values(literal):
    """파서가 앞의 중복 값을 버렸다면 변경 없는 원문으로 복귀하지 않는다."""
    out = mask_secrets(literal)
    assert 'SECRET' not in out
    assert ast.literal_eval(out)['exc_info'] == 'boom'


def test_an_embedded_multiline_header_keeps_only_the_separate_traceback():
    """문자열 안에서 비밀의 끝을 추측하지 않는다. 바깥 exc_info만 독립이다."""
    record = {'message': 'Authorization: Bearer SECRET\nTraceback: boom',
              'exc_info': 'ValueError: boom'}
    out = json.loads(mask_secrets(json.dumps(record)))
    assert out == {**record, 'message': MASK}


@pytest.mark.parametrize('literal', [
    r'{\"api_key\": \"CUT-OFF-SECRET',
    '{"tokens": ["dummy", "SECRET"',
    '{"credentials": {"username": "d", "value": "SECRET"',
    "{'api_key': b'head SECRET",
])
def test_incomplete_structures_mask_the_entire_chunk(literal):
    """스칼라·컨테이너 구분 없이 파싱 실패한 문자열 전체를 지운다."""
    out = mask_secrets(literal + '\nTraceback: boom')
    assert out == MASK


@pytest.mark.parametrize('name', ['KEY', 'ſecret', 'cookıe', 'API_KEY', '인증_api_key'])
def test_unicode_name_matching_does_not_depend_on_ascii_expansion(name):
    """IGNORECASE의 Unicode 특수 일치와 이름 토큰화가 어긋났던 경로를 잠근다."""
    assert 'SECRET' not in mask_secrets(f'{name}=SECRET')
    assert json.loads(mask_secrets(json.dumps({name: 'SECRET'}))) == {name: MASK}


def test_json_escaped_key_names_are_matched_after_decoding():
    """JSON key의 유니코드 이스케이프도 키 이름으로 해석해야 한다."""
    assert json.loads(mask_secrets(r'{"api\u005fkey": "SECRET"}')) == {'api_key': MASK}


@pytest.mark.parametrize('separator', ['\n', '\r\n', '\r'])
def test_mixed_broken_log_lines_are_one_unstructured_chunk(separator):
    """깨진 구조의 뒷줄을 독립 진단이라 추측하면 여러 줄 비밀값이 살아남는다."""
    parts = ['{"api_key": "CUT-OFF-SECRET',
             '{"bank": "hana", "rate": 1393.5}',
             'Traceback (most recent call last):', "KeyError: 'USD'", '']
    masked = mask_secrets(separator.join(parts))
    assert masked == MASK


@pytest.mark.parametrize('separator', ['\n', '\r\n', '\r'])
def test_complete_log_records_preserve_siblings_and_line_endings(separator):
    records = ['{"api_key": "SECRET", "exc_info": "ValueError: boom"}',
               '{"bank": "hana", "rate": 1393.5}',
               json.dumps({'exc_info': "Traceback:\nKeyError: 'USD'"})]
    raw = '\t' + (separator * 2).join(records) + separator
    expected = raw.replace('SECRET', MASK)
    assert mask_secrets(raw) == expected


def test_unicode_line_separator_inside_json_is_not_a_record_boundary():
    """str.splitlines()가 JSON 문자열 안의 U+2028까지 분리해 경계를 잃는 것을 막는다."""
    record = {'api_key': 'head\u2028SECRET', 'exc_info': 'Traceback: boom'}
    out = mask_secrets(json.dumps(record, ensure_ascii=False))
    assert json.loads(out) == {**record, 'api_key': MASK}


@pytest.mark.parametrize('text', [
    "KeyError: 'api_key=SECRET'",
    'KeyError=SECRET',
    '{"KeyError": "SECRET"}',
    "{'KeyError': 'SECRET'}",
])
def test_preserving_keyerror_does_not_exempt_secret_assignments(text):
    """예외명 하나를 보존하는 결정이 비밀 대입이나 객체 필드의 우회로가 되면 안 된다."""
    assert 'SECRET' not in mask_secrets(text)


def test_a_deep_value_fails_closed_without_losing_its_siblings():
    """상한을 넘는 자료구조는 해당 값만 실패 표지로 바꾸고 정상 깊이의 진단은 남긴다."""
    value = {'api_key': 'SECRET'}
    for _ in range(80):
        value = [value]
    out = mask_secrets(json.dumps({'details': value, 'exc_info': 'Traceback: boom'}))
    assert 'SECRET' not in out
    assert MASK_FAILED in out
    assert json.loads(out)['exc_info'] == 'Traceback: boom'


@pytest.mark.parametrize('limit', [0, 1, 20, None, -1])
def test_all_public_helpers_fail_closed_without_truncating_the_failure_marker(monkeypatch, limit):
    """파싱 이후의 예기치 않은 오류도 원문을 반환하거나 실패 표지를 잘라선 안 된다."""
    import app.crawlers.child_output as mod

    def boom(*args):
        raise RuntimeError('masking failed')

    monkeypatch.setattr(mod, '_mask_value', boom)
    raw = '{"api_key": "SECRET"}'
    assert mod.mask_secrets(raw) == MASK_FAILED
    assert mod.stdout_tail(raw, limit=limit) == MASK_FAILED
    assert mod.child_log_fields(raw, prefix='child_stdout', limit=limit) == {
        'child_stdout_tail': MASK_FAILED,
        'child_stdout_chars': len(raw),
        'child_stdout_truncated': False,
    }


@pytest.mark.parametrize('limit', [0, 1, 20, None, -1])
@pytest.mark.parametrize('output', [None, b'\xed\xa0tail', '환율\napi_key=SECRET'])
def test_fields_and_tail_apply_the_same_limit_after_masking(output, limit):
    """bytes/None/한글과 특수 limit에서도 두 공개 진입점의 문자 수·잘림 계약은 같다."""
    text = output.decode('utf-8', 'replace') if isinstance(output, bytes) else output or ''
    masked = mask_secrets(output)
    fields = child_log_fields(output, prefix='child_stderr', limit=limit)
    assert fields['child_stderr_tail'] == stdout_tail(output, limit=limit)
    assert fields['child_stderr_chars'] == len(text)
    assert fields['child_stderr_truncated'] is (len(masked) > len(fields['child_stderr_tail']))


@pytest.mark.parametrize('unit', ['Key', 'key ', '{"message":"key words"}\n'])
def test_tokenized_and_structured_million_character_inputs_are_bounded(unit):
    """Unicode 반복·떨어진 낱말·실제 JSON 줄에서도 파싱/토큰 탐색의 제곱 경로를 막는다."""
    text = (unit * (1_000_000 // len(unit))) + '\nTraceback: boom'
    started = time.perf_counter()
    fields = child_log_fields(text, prefix='child_stdout')
    elapsed = time.perf_counter() - started
    assert fields['child_stdout_tail'].endswith('Traceback: boom')
    assert elapsed < 3.0


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('inner,parse', [
    pytest.param('[\n {"details": {"tokens": ["INNER-CANARY"]}},\n'
                 ' {"rate": 1393.5}\n]', json.loads, id='json-array'),
    pytest.param("{'details': [\n {'api_key': [b'INNER-CANARY']},\n"
                 " {'rate': 1393.5}\n]}", ast.literal_eval, id='python-repr'),
])
def test_multiline_containers_survive_multiple_string_encoding_layers(inner, parse, newline):
    """구조가 문자열로 여러 겹 인코딩돼도 내부 비밀 제거와 바깥 진단 보존을 함께 본다."""
    message = json.dumps(inner.replace('\n', newline))
    trace = "Traceback (most recent call last):\nKeyError: 'USD'"
    raw = json.dumps({'message': message, 'exc_info': trace})
    out = mask_secrets(raw)
    assert 'INNER-CANARY' not in out
    outer = json.loads(out)
    assert outer['exc_info'] == trace
    decoded = parse(json.loads(outer['message']))
    items = decoded if isinstance(decoded, list) else decoded['details']
    assert items[-1] == {'rate': 1393.5}
    assert MASK in str(items[0])


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_whole_multiline_json_preserves_outer_whitespace_and_safe_records(newline):
    """단일 구조와 JSON 로그 줄 묶음을 구분하고, 정상 진단의 원문은 보존한다."""
    safe = '\t' + json.dumps({'bank': 'hana', 'rate': 1393.5}, indent=2) + '\n\n'
    safe = safe.replace('\n', newline)
    assert mask_secrets(safe) == safe
    raw = '\t' + json.dumps({'tokens': ['INNER-CANARY'], 'exc_info': 'boom'}, indent=2) + '\n\n'
    out = mask_secrets(raw.replace('\n', newline))
    assert out.startswith('\t')
    assert out.endswith(newline * 2)
    assert json.loads(out) == {'tokens': MASK, 'exc_info': 'boom'}


@pytest.mark.parametrize('key,masked_key', [
    ('https://user:URL-CANARY@host', f'https://user:{MASK}@host'),
    ('redis://:URL-CANARY@host', f'redis://:{MASK}@host'),
    (b'https://user:URL-CANARY@host', f'https://user:{MASK}@host'.encode()),
    (b'\xffordinary', b'\xffordinary'),
    ('https://host/rates', 'https://host/rates'),
    (42, 42),
])
def test_url_key_masking_keeps_value_diagnostics_and_other_key_types(key, masked_key):
    """키만 바뀌어도 재직렬화하고, 키의 자료형과 정상 값·형제 진단은 유지한다."""
    raw = repr({'details': {key: {'rate': 1393.5}}, 'exc_info': "KeyError: 'USD'"})
    out = mask_secrets(raw)
    assert 'URL-CANARY' not in out
    assert ast.literal_eval(out) == {
        'details': {masked_key: {'rate': 1393.5}}, 'exc_info': "KeyError: 'USD'"}


def test_sensitive_value_is_decided_before_the_url_key_is_masked():
    """URL을 지우며 민감 낱말이 사라져도 원래 키의 값 전체 마스킹 규칙은 유지한다."""
    raw = json.dumps({'https://user:token-CANARY@host': ['VALUE-CANARY'], 'exc_info': 'boom'})
    out = mask_secrets(raw)
    assert 'CANARY' not in out
    assert json.loads(out) == {f'https://user:{MASK}@host': MASK, 'exc_info': 'boom'}


@pytest.mark.parametrize('message', [
    '{\n "details": {"api_key": ["INNER-CANARY"]}\n}',
    '{"api_key": "INNER-CANARY"',
    '{"api_key": "INNER-CANARY"\nTraceback: boom',
])
def test_each_decoded_structure_is_parsed_once_before_any_line_fallback(monkeypatch, message):
    """전체 문자열을 줄마다 재시도하거나 한 줄 실패를 다시 파싱하지 않는다."""
    import app.crawlers.child_output as mod

    raw = json.dumps({'message': message, 'exc_info': 'ValueError: boom'})
    calls = []
    real_loads = json.loads

    def counting(text, *args, **kwargs):
        calls.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(mod.json, 'loads', counting)
    out = mask_secrets(raw)
    assert 'INNER-CANARY' not in out
    assert 'ValueError: boom' in out
    assert calls.count(raw) == 1
    assert calls.count(message) == 1


def test_large_multiline_structure_inside_one_json_record_is_bounded():
    """JSON 줄 반복과 별도로, 한 값 안에 많은 줄·객체가 있는 입력의 비용을 잠근다."""
    inner = json.dumps({'details': [{'api_key': ['INNER-CANARY'], 'rate': 1393.5}] * 12_000},
                       indent=2)
    raw = json.dumps({'message': inner, 'exc_info': "KeyError: 'USD'"})
    assert len(raw) > 1_000_000
    started = time.perf_counter()
    out = mask_secrets(raw)
    elapsed = time.perf_counter() - started
    assert 'INNER-CANARY' not in out
    masked = json.loads(out)
    assert masked['exc_info'] == "KeyError: 'USD'"
    assert json.loads(masked['message'])['details'] == [{'api_key': MASK, 'rate': 1393.5}] * 12_000
    assert elapsed < 3.0


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('value', [
    '[]', '{}', '()',
    '[\n "SECRET", {"brackets": "]})", "quote": "escaped \\\" ]"}\n]',
    "{\n 'v': ('SECRET', b'brackets ]})', 'it\\'s }')\n}",
    '(\n ["SECRET", "backslash \\\\", "]"], {"v": ")"}\n)',
    '[\n """a quote " and ]\n SECRET\n """, "tail"\n]',
])
def test_context_fallback_masks_whole_regardless_of_container_or_quotes(value, newline):
    """값의 모양·닫힘 여부에 상관없이 같은 문자열 전체를 가린다."""
    message = ('request body: api_key=\n ' + value
               + '; token=(\n "SECOND-SECRET",\n); bank=hana\n'
               + "KeyError: 'USD'").replace('\n', newline)
    raw = json.dumps({'message': message, 'exc_info': 'ValueError: boom'})
    for out in (mask_secrets(raw), stdout_tail(raw),
                child_log_fields(raw, prefix='child')['child_tail']):
        assert 'SECRET' not in out
        assert json.loads(out) == {'message': MASK, 'exc_info': 'ValueError: boom'}


@pytest.mark.parametrize('value', [
    '[\n "SECRET"', '{\n "v": "SECRET"', '(\n "SECRET",',
    '[\n {"v": "SECRET"]\n }', '[\n "unterminated ] SECRET\n ]',
])
def test_broken_container_consumes_the_remainder_but_not_outer_diagnostics(value):
    """진단처럼 보이는 뒷줄도 닫히지 않은 값의 일부일 수 있다. 바깥 exc_info만 독립이다."""
    raw = json.dumps({'message': 'body: token=' + value + '\nTraceback: SECRET-TAIL',
                      'exc_info': "KeyError: 'USD'"})
    for out in (mask_secrets(raw), stdout_tail(raw),
                child_log_fields(raw, prefix='child')['child_tail']):
        assert 'SECRET' not in out
        assert json.loads(out) == {'message': MASK, 'exc_info': "KeyError: 'USD'"}


@pytest.mark.parametrize('key', [
    'api_key=SECRET', 'Authorization: Bearer SECRET',
    json.dumps({'api_key': ['SECRET']}, indent=2),
    'body: credentials={\n "v": "SECRET"\n}',
])
@pytest.mark.parametrize('as_bytes', [False, True])
def test_object_keys_use_the_same_masking_as_string_values(key, as_bytes):
    """키만 바뀌어도 변경으로 기록하고, bytes 자료형과 형제 진단을 보존한다."""
    original_key = key.encode() if as_bytes else key
    masked_key = mask_secrets(original_key)
    if as_bytes:
        masked_key = masked_key.encode()
    raw = repr({original_key: MASK, 'exc_info': "KeyError: 'USD'"})
    out = mask_secrets(raw)
    assert 'SECRET' not in out
    assert ast.literal_eval(out) == {masked_key: MASK, 'exc_info': "KeyError: 'USD'"}


@pytest.mark.parametrize('shape', ['many-values', 'one-multiline-value', 'unclosed-value'])
def test_large_context_fallback_masks_whole_in_bounded_time(shape):
    """많은 짧은 값·하나의 긴 값·닫히지 않은 중첩 모두 경계 탐색 없이 가린다."""
    if shape == 'many-values':
        message = 'body: ' + 'api_key=["SECRET"]; ' * 55_000 + 'bank=hana'
    elif shape == 'one-multiline-value':
        message = 'body: api_key=[\n' + '  "SECRET",\n' * 90_000 + ']; bank=hana'
    else:
        message = 'body: api_key=[' + 'api_key=[\n' * 110_000 + 'SECRET'
    raw = json.dumps({'message': message, 'exc_info': "KeyError: 'USD'"})
    assert len(raw) > 1_000_000
    started = time.perf_counter()
    out = mask_secrets(raw)
    elapsed = time.perf_counter() - started
    assert 'SECRET' not in out
    masked = json.loads(out)
    assert masked['exc_info'] == "KeyError: 'USD'"
    assert masked['message'] == MASK
    assert elapsed < 3.0


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('message', [
    'body: {"api_key":\n "CANARY7391"}',
    'body: {"api_key"\n: "CANARY7391"}',
    'body: {"api_key": """CANARY7391\nmore"""}',
    'body: {"""api_key"""\n: "CANARY7391"}',
    # 뒷줄이 독립적으로 파싱되더라도 비밀값의 연속일 수 있다.
    'api_key=\n{"value": "CANARY7391"}',
    'before: ok\napi_key=\n"CANARY7391"\nafter: ok',
])
def test_unstructured_secrets_mask_the_entire_string_at_every_entry(message, newline):
    message = message.replace('\n', newline)
    trace = "Traceback (most recent call last):\nKeyError: 'USD'"
    for raw, expected in (
        (message, MASK),
        (json.dumps({'message': message, 'exc_info': trace}),
         json.dumps({'message': MASK, 'exc_info': trace})),
    ):
        assert mask_secrets(raw) == expected
        assert stdout_tail(raw) == expected
        assert child_log_fields(raw, prefix='c')['c_tail'] == expected


@pytest.mark.parametrize('message', [
    r'body: {"api_\u006bey": "CANARY7391"}',
    r'body: {"\u0061\u0070\u0069\u005f\u006b\u0065\u0079": "CANARY7391"}',
    r"body: {'api_\x6bey': 'CANARY7391'}",
    r"body: {'api_\153ey': 'CANARY7391'}",
    r'body: {"api_\U0000006bey": "CANARY7391"}',
    r'body: {"api_\u005cu006bey": "CANARY7391"}',
    r'body: {\"api_key\"\n: \"CANARY7391\"}',
])
def test_escaped_names_in_unparseable_text_mask_whole(message):
    assert mask_secrets(message) == MASK
    record = {'message': message, 'exc_info': "KeyError: 'USD'"}
    assert json.loads(mask_secrets(json.dumps(record))) == {**record, 'message': MASK}


def test_the_escaped_key_reproducer_has_one_literal_backslash():
    raw = bytes.fromhex(
        '626f64793a207b226170695f5c75303036626579223a202243414e41525937333931227d')
    assert raw == rb'body: {"api_\u006bey": "CANARY7391"}'
    assert mask_secrets(raw) == MASK


@pytest.mark.parametrize('text', [
    'body: {"rate":\n 1393.5}\nafter: ok',
    'cookies enabled\nTraceback (most recent call last):\nKeyError: \'USD\'',
    r'body: {"\u0072ate": 1393.5}',
    r'cookies\u0020enabled',
    r'C:\new\rates.csv',
    'body: {"api_key"',  # 대입이 없으면 낱말만으로 지우지 않는다.
    'https://host/rates\nKeyError: \'USD\'',
])
def test_unstructured_text_without_sensitive_assignments_is_unchanged(text):
    assert mask_secrets(text) == text
    record = {'message': text, 'exc_info': "KeyError: 'USD'"}
    assert mask_secrets(json.dumps(record)) == json.dumps(record)


@pytest.mark.parametrize('key,expected', [
    (('api_key=CANARY7391',), (MASK,)),
    ((('x', 'token=CANARY7391'),), (('x', MASK),)),
    ((b'api_key=CANARY7391', 42, None), (MASK.encode(), 42, None)),
    (('https://user:CANARY7391@host',), (f'https://user:{MASK}@host',)),
    (('ordinary', (1, None, b'\xffordinary')), ('ordinary', (1, None, b'\xffordinary'))),
])
def test_tuple_keys_are_traversed_without_losing_value_diagnostics(key, expected):
    record = {key: {'rate': 1393.5}, 'exc_info': "KeyError: 'USD'"}
    out = mask_secrets(repr(record))
    assert 'CANARY7391' not in out
    assert ast.literal_eval(out) == {expected: record[key], 'exc_info': record['exc_info']}


def test_escape_decoding_has_a_fixed_limit_and_preserves_siblings():
    message = 'body: {"api_\\' + 'u005c' * 80 + 'u006bey": "CANARY7391"}'
    record = {'message': message, 'exc_info': "KeyError: 'USD'"}
    assert json.loads(mask_secrets(json.dumps(record))) == {**record, 'message': MASK_FAILED}


@pytest.mark.parametrize('unit', [r'\u0078', r'\u006bey ', r'key\n '])
def test_many_escaped_names_remain_linear_and_unchanged_without_assignment(unit):
    text = (unit * (1_000_000 // len(unit)))
    started = time.perf_counter()
    assert mask_secrets(text) == text
    assert time.perf_counter() - started < 3.0


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('serialize', [json.dumps, repr])
@pytest.mark.parametrize('output_type', [str, bytes, bytearray])
def test_log_record_separates_secret_runs_at_all_public_entries(newline, serialize, output_type):
    """양쪽 구간은 각각 가리고, 가운데 로그의 추적·구분자·자체 마스킹은 함께 보존한다."""
    trace = "Traceback (most recent call last):\nKeyError: 'USD'"
    record = {'message': 'subprocess failed', 'exc_info': trace, 'api_key': 'RECORD-CANARY'}
    before = r'body: {"api_\u006bey":' + newline + ' "BEFORE-CANARY"}'
    after = 'body: {"token"' + newline + ': "AFTER-CANARY"}'
    text = before + newline * 2 + '\t' + serialize(record) + newline * 2 + after
    raw = text if output_type is str else output_type(text.encode())
    expected = (MASK + newline * 2 + '\t' + serialize({**record, 'api_key': MASK})
                + newline * 2 + MASK)
    assert mask_secrets(raw) == expected
    assert stdout_tail(raw, limit=None) == expected
    assert child_log_fields(raw, prefix='child', limit=None) == {
        'child_tail': expected, 'child_chars': len(text), 'child_truncated': False,
    }


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('inner', [
    '"CANARY-INNER"', '123', 'true', 'null', "b'CANARY-INNER'",
    '["CANARY-INNER"]', "('CANARY-INNER',)", "{'CANARY-INNER'}",
    '{"value": "CANARY-INNER"}', '{"message": "CANARY-INNER"}',
    "{'value': b'CANARY-INNER'}",
])
def test_complete_non_log_values_cannot_split_a_multiline_secret(inner, newline):
    """중간 값을 레코드로 보면 이름 없는 뒷부분의 비밀값까지 새어 나온다."""
    trace = json.dumps({'exc_info': "KeyError: 'USD'"})
    fragment = newline.join(['body: api_key=[', inner, ', "CANARY-TAIL"', ']'])
    raw = fragment + newline + trace
    expected = MASK + newline + trace
    assert mask_secrets(raw) == expected
    assert stdout_tail(raw) == expected
    assert child_log_fields(raw, prefix='child')['child_tail'] == expected


@pytest.mark.parametrize('serialize', [json.dumps, repr])
@pytest.mark.parametrize('position', ['before', 'between', 'after'])
def test_log_objects_without_tracebacks_also_end_broken_runs(serialize, position):
    records = [serialize({'timestamp': '2026-09-21T00:00:59', 'level': 'INFO',
                          'message': message}) for message in ('start', 'finished')]
    noise = 'body: {"api_key":\n "CANARY"}'
    index = {'before': 0, 'between': 1, 'after': 2}[position]
    raw = records[:index] + [noise] + records[index:]
    expected = records[:index] + [MASK] + records[index:]
    assert mask_secrets('\n'.join(raw)) == '\n'.join(expected)


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_mixed_safe_noise_keeps_whitespace_and_the_truncated_last_line(newline):
    record = json.dumps({'exc_info': "KeyError: 'USD'"})
    raw = newline.join(['\t', 'cookies enabled', '', record, '', '{"timestamp": "2026'])
    assert mask_secrets(raw) == raw


def test_a_whole_secret_structure_takes_priority_over_inner_log_shaped_lines():
    """완전한 여러 줄 구조 안에서는 로그처럼 생긴 객체도 민감 필드의 값이다."""
    raw = ('{"api_key": [\n'
           '{"timestamp": "2026", "level": "INFO", "message": "CANARY"}\n'
           '], "exc_info": "ValueError: boom"}')
    assert json.loads(mask_secrets(raw)) == {'api_key': MASK, 'exc_info': 'ValueError: boom'}


def test_log_shaped_lines_inside_a_message_do_not_create_record_boundaries():
    message = ('body: token=[\n'
               '{"timestamp": "2026", "level": "INFO", "message": "CANARY"}\n'
               ', "CANARY-TAIL"]')
    record = {'message': message, 'exc_info': "KeyError: 'USD'"}
    assert json.loads(mask_secrets(json.dumps(record))) == {**record, 'message': MASK}


def test_many_alternating_log_records_and_secret_runs_remain_linear():
    """구간이 늘 때 앞부분을 다시 훑거나 누적 문자열을 복사하는 경로를 잠근다."""
    record = json.dumps({'exc_info': "KeyError: 'USD'"})
    fragment = 'body: {"api_key":\n "CANARY"}\n'
    text = (fragment + record + '\n') * 20_000
    assert len(text) > 1_000_000
    started = time.perf_counter()
    out = mask_secrets(text)
    elapsed = time.perf_counter() - started
    assert out == (MASK + '\n' + record + '\n') * 20_000
    assert elapsed < 3.0
