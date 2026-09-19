"""Zero-network wire/decoded bounds, requests decoding and wall-clock tests."""

import gzip
import hashlib
import signal
import time
import zlib
from unittest.mock import Mock

import pytest
import requests
from bs4 import BeautifulSoup
from requests.adapters import BaseAdapter

from tests._fixture_capture import getter, no_network, response
from tools.fixture_capture.errors import CaptureError, DeadlineExpired
from tools.fixture_capture.fetch import _Inflater, fetch_once
from tools.fixture_capture.limits import BODY_LIMIT, Deadline, wall_timeout
from tools.fixture_capture.registry import Registry
from tools.fixture_capture.roundtrip import parse_html


def test_exact_request_contract_and_unbuffered_response():
    get, result = getter('환율')
    registry = Registry()
    headers = registry.sources.constants.HEADERS
    fetched = fetch_once(registry.routes['bs_official'].url, headers, get=get)
    args, kwargs = get.call_args
    assert args == (registry.routes['bs_official'].url,)
    assert kwargs['headers'] is headers
    assert kwargs['allow_redirects'] is False and kwargs['stream'] is True
    assert 0 < kwargs['timeout'] <= 10
    assert set(kwargs) == {'headers', 'allow_redirects', 'stream', 'timeout', 'hooks'}
    assert result._content is False  # Never used the original Response.text/content.
    assert result.raw.closed
    assert fetched.original_body_sha256 == hashlib.sha256('환율'.encode()).hexdigest()


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308, 400, 500])
def test_http_failure_consumes_one_request_without_read_or_retry(status):
    get, result = getter(b'secret', status=status)
    with pytest.raises(CaptureError, match='http_status'):
        fetch_once('https://example.invalid/', {}, get=get)
    assert get.call_count == 1
    assert result.raw.reads == [] and result.raw.closed


def test_real_requests_redirect_preparation_cannot_consume_body(monkeypatch):
    result = response(b'secret', status=302)
    result.headers['Location'] = 'https://example.invalid/next'

    class FakeAdapter(BaseAdapter):
        calls = 0

        def send(self, request, **kwargs):
            self.calls += 1
            result.request = request
            return result

        def close(self):
            pass

    adapter = FakeAdapter()
    session = requests.Session()
    session.mount('https://', adapter)
    with pytest.raises(CaptureError, match='http_status'):
        fetch_once('https://example.invalid/', {}, get=session.get)
    assert adapter.calls == 1 and result.raw.reads == []
    assert result.raw.closed


@pytest.mark.parametrize('size,accepted', [(BODY_LIMIT - 1, True), (BODY_LIMIT, True), (BODY_LIMIT + 1, False)])
def test_wire_limit_is_checked_while_receiving(size, accepted):
    get, result = getter(b'x' * size)
    if accepted:
        assert len(fetch_once('https://example.invalid/', {}, get=get).text) == size
    else:
        with pytest.raises(CaptureError, match='wire_body_limit'):
            fetch_once('https://example.invalid/', {}, get=get)
    assert result.raw.body.tell() <= BODY_LIMIT + 1
    assert result.raw.closed


@pytest.mark.parametrize('encoding,compress', [('gzip', gzip.compress), ('deflate', zlib.compress),
                                            ('deflate', lambda data: zlib.compress(data)[2:-4])])
@pytest.mark.parametrize('size,accepted', [(BODY_LIMIT, True), (BODY_LIMIT + 1, False)])
def test_decompressed_limit_is_independent_of_wire_limit(encoding, compress, size, accepted):
    payload = compress(b'x' * size)
    assert len(payload) < BODY_LIMIT
    get, result = getter(payload, encoding=encoding)
    if accepted:
        assert len(fetch_once('https://example.invalid/', {}, get=get).text) == size
    else:
        with pytest.raises(CaptureError, match='decoded_body_limit'):
            fetch_once('https://example.invalid/', {}, get=get)
    assert result.raw.closed


def test_wire_limit_also_applies_to_compressed_bytes(monkeypatch):
    # gzip header comments can be large while the decoded body is tiny.
    payload = gzip.compress(b'x')
    payload = payload[:3] + bytes([payload[3] | 16]) + payload[4:10] + b'a' * BODY_LIMIT + b'\0' + payload[10:]
    get, result = getter(payload, encoding='gzip')
    with pytest.raises(CaptureError, match='wire_body_limit'):
        fetch_once('https://example.invalid/', {}, get=get)
    assert result.raw.body.tell() == BODY_LIMIT + 1


def test_concatenated_gzip_members_have_one_combined_limit():
    get, _ = getter(gzip.compress(b'a') + gzip.compress(b'b'), encoding='gzip')
    assert fetch_once('https://example.invalid/', {}, get=get).text == 'ab'
    get, _ = getter(gzip.compress(b'a' * BODY_LIMIT) + gzip.compress(b'b'), encoding='gzip')
    with pytest.raises(CaptureError, match='decoded_body_limit'):
        fetch_once('https://example.invalid/', {}, get=get)


@pytest.mark.parametrize('encoding,compress', [
    ('gzip', gzip.compress), ('deflate', zlib.compress),
    ('deflate', lambda data: zlib.compress(data)[2:-4]),
    ('gzip', lambda data: gzip.compress(b'a' * 8) + gzip.compress(data)),
])
@pytest.mark.parametrize('remaining', [0, 64])
def test_inflater_never_requests_unbounded_output(monkeypatch, encoding, compress, remaining):
    # Observe the actual zlib boundary, not just the outer fetch rejection.
    payload = compress(b'x' * 100_000)
    decompressobj = zlib.decompressobj
    calls = []

    class BoundedDecoder:
        def __init__(self, *args, **kwargs):
            self.decoder = decompressobj(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

        def decompress(self, data, max_length=0):
            calls.append(max_length)
            # Check before delegating: max_length=0 must never allocate the bomb.
            assert 0 < max_length <= remaining + 1
            output = self.decoder.decompress(data, max_length)
            assert len(output) <= max_length
            return output

    monkeypatch.setattr('tools.fixture_capture.fetch.zlib.decompressobj', BoundedDecoder)
    with pytest.raises(CaptureError, match='decoded_body_limit'):
        _Inflater(encoding).feed(payload, remaining)
    assert calls


@pytest.mark.parametrize('encoding', ['br', ' BR ', 'gzip, br'])
def test_unsupported_encoding_rejects_before_reading_body(encoding):
    get, result = getter(b'body must not be read', encoding=encoding)
    with pytest.raises(CaptureError) as caught:
        fetch_once('https://example.invalid/', {}, get=get)
    assert caught.value.rule == 'unsupported_content_encoding'
    assert caught.value.location == 'response'
    assert get.call_count == 1
    assert result.raw.reads == []
    assert result.raw.closed


@pytest.mark.parametrize('payload,encoding', [(b'not gzip', 'gzip'), (gzip.compress(b'x')[:-2], 'gzip'),
                                          (b'', 'gzip'), (b'x', 'deflate'), (b'', 'br'), (b'', 'gzip, br')])
def test_bad_or_unbounded_encoding_rejects_without_retry(payload, encoding):
    get, result = getter(payload, encoding=encoding)
    with pytest.raises(CaptureError):
        fetch_once('https://example.invalid/', {}, get=get)
    assert get.call_count == 1 and result.raw.closed


@pytest.mark.parametrize('body,content_type', [
    ('환율'.encode('utf-8'), 'text/html; charset=utf-8'),
    ('환율'.encode('cp949'), 'text/html; charset=cp949'),
    (b'\xffhello\x80', 'text/html; charset=utf-8'),
    ('환율'.encode(), 'text/html'),
    ('환율'.encode(), 'application/octet-stream'),
    ('환율'.encode(), 'text/html; charset=unknown-charset'),
    (b'', 'text/html; charset=utf-8'),
])
def test_decoding_exactly_matches_requests_response_text(body, content_type):
    get, streamed = getter(body, content_type=content_type)
    expected = requests.Response()
    expected.encoding = streamed.encoding
    expected._content = body
    assert fetch_once('https://example.invalid/', {}, get=get).text == expected.text


def test_elapsed_time_catches_slow_drip_without_socket_timeout():
    now = [0.0]
    deadline = Deadline(clock=lambda: now[0])
    get, result = getter(b'x' * 50)
    original = result.raw.read

    def drip(amount, decode_content):
        now[0] += 11
        return original(1, decode_content)
    result.raw.read = drip
    with pytest.raises(CaptureError, match='total_timeout'):
        fetch_once('https://example.invalid/', {}, get=get, deadline=deadline)
    assert result.raw.body.tell() == 3 and result.raw.closed


def test_blocking_get_is_actually_interrupted():
    def stalled(*args, **kwargs):
        time.sleep(1)
        pytest.fail('deadline did not interrupt blocking get')
    started = time.monotonic()
    with pytest.raises(CaptureError, match='total_timeout'):
        fetch_once('https://example.invalid/', {}, get=stalled, deadline=Deadline(seconds=0.03))
    assert time.monotonic() - started < 0.5


def test_blocking_read_is_actually_interrupted_and_closed():
    get, result = getter('x')
    result.raw.read = lambda *args, **kwargs: time.sleep(1)
    with pytest.raises(CaptureError, match='total_timeout'):
        fetch_once('https://example.invalid/', {}, get=get, deadline=Deadline(seconds=0.03))
    assert result.raw.closed


def test_parser_budget_interrupts_and_restores_alarm(monkeypatch):
    monkeypatch.setattr('tools.fixture_capture.roundtrip.PARSE_SECONDS', 0.03)
    monkeypatch.setattr('tools.fixture_capture.roundtrip.BeautifulSoup', lambda *args: time.sleep(1))
    before = signal.getsignal(signal.SIGALRM)
    with pytest.raises(DeadlineExpired, match='parse_timeout'):
        parse_html('x', Deadline())
    assert signal.getsignal(signal.SIGALRM) is before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_nested_parser_cannot_extend_total_budget():
    with pytest.raises(DeadlineExpired, match='total_timeout'):
        with wall_timeout(0.03, 'total_timeout'):
            with wall_timeout(10, 'parse_timeout'):
                time.sleep(1)


def test_two_parser_calls_share_the_ten_second_budget(monkeypatch):
    real_parser = BeautifulSoup
    budget = [0.08]

    def slow_parser(*args):
        time.sleep(0.05)
        return real_parser(*args)
    monkeypatch.setattr('tools.fixture_capture.roundtrip.BeautifulSoup', slow_parser)
    parse_html('<p>x</p>', Deadline(), budget)
    assert 0 < budget[0] < 0.05
    with pytest.raises(DeadlineExpired, match='parse_timeout'):
        parse_html('<p>x</p>', Deadline(), budget)


def test_subthread_rejects_before_request():
    from concurrent.futures import ThreadPoolExecutor  # Test-only execution boundary.
    get = Mock()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(CaptureError, match='posix_main_thread_required'):
            executor.submit(fetch_once, 'https://example.invalid/', {}, get=get).result()
    get.assert_not_called()
