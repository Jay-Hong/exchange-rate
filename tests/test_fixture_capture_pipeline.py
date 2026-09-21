"""Independent identifier scans, complete output pairs and logging boundaries."""

import hashlib
import io
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid1

import pytest
from bs4 import BeautifulSoup

from tests._fixture_capture import (artifact_output, citi_html, getter, mibank_html, mibank_row,
                                    no_network, official_html, response)
from tools.fixture_capture.capture import (REPO_ROOT, ROUTES, SOURCE_FILES, capture_route, main, outside_repository,
                                           parser_versions, source_identity, write_artifact)
from tools.fixture_capture.detector import (METADATA_POLICY, scan_fixture, scan_string,
                                            validate_metadata, validate_recording)
from tools.fixture_capture.errors import CaptureError
from tools.fixture_capture.limits import Deadline
from tools.fixture_capture.registry import Registry
from tools.fixture_capture.roundtrip import roundtrip
from tools.fixture_capture.runtime import load_sources, quiet_logging


@pytest.fixture
def registry():
    return Registry()


@pytest.mark.parametrize('value,rule', [
    ('UserSession0123456789abcdef', 'long_alphanumeric'),
    ('abcdef1234567890', 'long_hex'), ('user@example.invalid', 'at_token'),
    ('https://example.invalid', 'url_scheme'), ('12345678901', 'long_number'),
    ('901231-1234567', 'rrn_shape'), ('000000-1000000', 'rrn_shape'),
    # The seventh digit is not narrowed, so 0 and 9 are caught like 1 and 8.
    ('123456-0123456', 'rrn_shape'), ('123456-9123456', 'rrn_shape'),
    ('123456-8123456', 'rrn_shape'),
    # search(), not fullmatch(): a longer digit string that contains the shape is caught.
    ('1234567-1234567', 'rrn_shape'), ('1234-5678-9012-3456-7890', 'card_shape'),
    ('1234-5678-9012-3456', 'card_shape'),
])
def test_representative_identifier_detection_reports_location_only(value, rule):
    with pytest.raises(CaptureError) as caught:
        scan_string(value, 'fixture.nodes[2].text')
    assert caught.value.rule == rule
    assert str(caught.value) == 'fixture.nodes[2].text: ' + rule
    assert value not in str(caught.value)


@pytest.mark.parametrize('value', ['1,300.25', '900.500', '기준환율', 'column_index_unresolved',
                                 'GhJkLmNpQrStUvWx', 'token-part-one-part-two'])
def test_normal_text_and_documented_regex_blind_spots(value):
    scan_string(value, 'text')


# The two shape rules are exactly two shapes, not an identifier detector. These values
# occur in the captured bank pages (corporate phone numbers, a business registration
# number, a date) and must keep passing; the last two are shapes the rules do NOT cover.
@pytest.mark.parametrize('value', ['82-2-1588-6200', '1588-6200', '102-81-14717',
                                 '2026-09-21', '010-1234-5678', '1234 5678 9012 3456',
                                 # Group counts and widths are the rule, so one group short
                                 # or one digit short is a different shape and must pass.
                                 # Without these two a narrowed card_shape and a 5-digit
                                 # rrn_shape both survived the battery.
                                 '1234-5678-9012', '12345-1234567', '123456-123456'])
def test_shape_rules_leave_corporate_values_and_their_own_blind_spots_alone(value):
    scan_string(value, 'text')


# The scan runs per text node, so a shape broken across inline tags is not seen. This
# is a stated limit of the two shape rules, not an accident: recording it here keeps a
# later reader from believing the fixture scan proves such a shape is absent.
@pytest.mark.parametrize('html', ['<p>901231-<b>1234567</b></p>',
                                 '<p>1234-<b>5678-9012-3456</b></p>'])
def test_shape_rules_do_not_see_across_inline_tag_boundaries(html):
    scan_fixture(BeautifulSoup(html, 'html.parser'))


@pytest.mark.parametrize('html', ['<p>user@example.invalid</p>', '<a href="https://example.invalid">x</a>',
                                 '<div class="abcdef1234567890">x</div>'])
def test_all_fixture_text_and_preserved_attribute_values_are_scanned(html):
    with pytest.raises(CaptureError):
        scan_fixture(BeautifulSoup(html, 'html.parser'))


def _meta(registry):
    get, _ = getter(mibank_html(header=None))
    artifact = capture_route('bs_mibank', registry=registry, get=get)
    return artifact, json.loads(artifact.metadata)


def _validate(meta, artifact, registry):
    commit, contract = source_identity(registry)
    validate_metadata(meta, route=registry.routes['bs_mibank'], registry=registry,
                      source_commit=commit, contract=contract, parser=parser_versions(),
                      original_hash=hashlib.sha256(mibank_html(header=None).encode()).hexdigest(),
                      fixture=artifact.fixture)


def test_generated_url_uuid_hash_and_long_enums_use_field_contracts(registry):
    artifact, meta = _meta(registry)
    assert set(meta) == set(METADATA_POLICY)
    assert '://' in meta['source_url_base'] and len(meta['capture_id']) == 36
    assert 'column_index_unresolved' in artifact.metadata.decode()
    _validate(meta, artifact, registry)


@pytest.mark.parametrize('field,value', [
    ('source_url_base', 'https://attacker.invalid/'), ('capture_id', 'user@example.invalid'),
    ('source_commit', 'f' * 40), ('original_body_sha256', 'f' * 64),
    ('fixture_sha256', 'f' * 64), ('extraction_contract', 'c1b/1:' + 'f' * 64),
    ('parser', {'name': 'different'}), ('origin', 'production'), ('fetched_at', 'today'),
    ('http_status', 302), ('content_type', 'text/html; user=user@example.invalid'),
    ('charset', 'user@example.invalid'),
])
def test_code_provenance_and_response_fields_are_not_blanket_exempt(registry, field, value):
    artifact, meta = _meta(registry)
    meta[field] = value
    with pytest.raises(CaptureError) as caught:
        _validate(meta, artifact, registry)
    assert 'user@example.invalid' not in str(caught.value)
    assert 'attacker.invalid' not in str(caught.value)


@pytest.mark.parametrize('target', ['rate_text', 'label', 'found_code', 'enum', 'unknown_field'])
def test_recorded_extraction_is_scanned_and_schema_closed(registry, target):
    artifact, meta = _meta(registry)
    record = meta['recorded_extraction']
    observed = next(e for e in record['events'] if e['kind'] == 'observed')
    if target == 'rate_text':
        observed['facts']['rate_text'] = 'user@example.invalid'
    elif target == 'label':
        observed['labels']['row_text'] = 'user@example.invalid'
    elif target == 'found_code':
        record['returned'][1].append('UserSession0123456789abcdef')
    elif target == 'enum':
        observed['facts']['value_basis']['reason'] = 'user@example.invalid'
    else:
        record['user@example.invalid'] = 'x'
    with pytest.raises(CaptureError) as caught:
        validate_recording(record, registry.routes['bs_mibank'], registry)
    assert 'user@example.invalid' not in str(caught.value)


@pytest.mark.parametrize('nested', [False, True])
def test_exception_argument_strings_are_scanned(registry, nested):
    route = registry.routes['bs_mibank']
    _, _, record, _replacements = roundtrip(mibank_html(mibank_row(rate='broken')), route, registry, Deadline())
    # First prove this is an otherwise valid record, including its real AST site.
    validate_recording(record, route, registry)
    secret = 'user@example.invalid'
    assert secret not in json.dumps(record)
    record['exception']['args'] = [[secret]] if nested else [secret]
    with pytest.raises(CaptureError) as caught:
        validate_recording(record, route, registry)
    assert caught.value.rule == 'at_token'
    assert caught.value.location == 'recorded_extraction.exception.args[0]'
    assert secret not in str(caught.value)


@pytest.mark.parametrize('kind', ['uuid1', 'uppercase_v4'])
def test_capture_id_requires_canonical_uuid4(registry, kind):
    artifact, meta = _meta(registry)
    _validate(meta, artifact, registry)
    # Explicit synthetic node avoids reading the machine's MAC address.
    value = str(uuid1(node=0x010203040506, clock_seq=1)) if kind == 'uuid1' else (
        'A1B2C3D4-E5F6-4789-ABCD-0123456789AB')
    parsed = UUID(value)  # Both inputs pass UUID parsing; only the contract rejects.
    assert parsed.version == (1 if kind == 'uuid1' else 4)
    meta['capture_id'] = value
    with pytest.raises(CaptureError) as caught:
        _validate(meta, artifact, registry)
    assert (caught.value.rule, caught.value.location) == ('field_contract', 'metadata.capture_id')


@pytest.mark.parametrize('route_name,html', [
    ('bs_official', official_html()), ('citi_primary', citi_html()),
    ('citi_secondary', official_html('citi_secondary')),
])
def test_selector_fact_requires_registry_provenance(registry, route_name, html):
    route = registry.routes[route_name]
    get, _ = getter(html)
    record = json.loads(capture_route(route_name, registry=registry, get=get).metadata)['recorded_extraction']
    validate_recording(record, route, registry)
    selector = '.unregistered'
    scan_string(selector, 'test')  # Benign text still requires code provenance.
    assert selector not in registry.evidence_selectors
    index, event = next((i, e) for i, e in enumerate(record['events']) if 'selector' in e['facts'])
    event['facts']['selector'] = selector
    with pytest.raises(CaptureError) as caught:
        validate_recording(record, route, registry)
    assert caught.value.rule == 'field_contract'
    assert caught.value.location == f'recorded_extraction.events[{index}].selector'


@pytest.mark.parametrize('returncode', [1, 128])
def test_dirty_or_unverifiable_extraction_source_rejects_before_request(registry, monkeypatch, returncode):
    run = Mock(side_effect=[SimpleNamespace(stdout='a' * 40 + '\n'), SimpleNamespace(returncode=returncode)])
    monkeypatch.setattr('tools.fixture_capture.capture.subprocess.run', run)
    with pytest.raises(CaptureError) as caught:
        source_identity(registry)
    assert (caught.value.rule, caught.value.location) == ('dirty_extraction_source', 'provenance')
    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == ['git', 'rev-parse', 'HEAD']
    assert run.call_args_list[1].args[0] == ['git', 'diff', '--quiet', 'HEAD', '--', *SOURCE_FILES]
    assert all(call.kwargs['cwd'] == REPO_ROOT for call in run.call_args_list)

    run.reset_mock(side_effect=True)
    run.side_effect = [SimpleNamespace(stdout='a' * 40 + '\n'), SimpleNamespace(returncode=returncode)]
    get = Mock()
    with pytest.raises(CaptureError, match='dirty_extraction_source'):
        capture_route('bs_official', registry=registry, get=get)
    get.assert_not_called()


def test_nonfinite_values_and_selector_misses_are_complete_json(registry):
    get, _ = getter(official_html(rates=('nan', 'inf', '-inf')))
    artifact = capture_route('bs_official', registry=registry, get=get)
    returned = json.loads(artifact.metadata)['recorded_extraction']['returned']
    assert returned == {'usd-krw': {'nonfinite': 'nan'}, 'jpy-krw': {'nonfinite': 'inf'}, 'eur-krw': {'nonfinite': '-inf'}}
    get, _ = getter('<html><body>empty page</body></html>')
    record = json.loads(capture_route('bs_official', registry=registry, get=get).metadata)['recorded_extraction']
    assert [e['kind'] for e in record['events']] == ['selector_miss'] * 3 + ['loop_completed']


def test_metadata_limit_rejects_complete_record_instead_of_truncating(registry):
    rows = ''.join(mibank_row(rate=str(1300 + i)) for i in range(100))
    get, _ = getter(mibank_html(rows))
    with pytest.raises(CaptureError, match='metadata_size'):
        capture_route('bs_mibank', registry=registry, get=get)


def test_html_limit_is_independent_of_metadata_limit(registry):
    get, _ = getter(official_html(extra='<aside>' + '한 ' * 90000 + '</aside>'))
    with pytest.raises(CaptureError, match='fixture_size'):
        capture_route('bs_official', registry=registry, get=get)


def test_output_is_only_verified_pair(registry, artifact_output):
    tmp_path = artifact_output
    artifact, meta = _meta(registry)
    destination = write_artifact(artifact, tmp_path)
    assert set(p.name for p in destination.iterdir()) == {'fixture.html', 'metadata.json'}
    assert (destination / 'fixture.html').read_bytes() == artifact.fixture
    assert (destination / 'metadata.json').read_bytes() == artifact.metadata
    assert meta['fixture_sha256'] == hashlib.sha256(artifact.fixture).hexdigest()
    with pytest.raises(CaptureError, match='capture_already_exists'):
        write_artifact(artifact, tmp_path)
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize('relative', ['.', 'tests/fixtures', 'new-capture-directory'])
def test_repository_output_rejected_before_request(relative, capsys):
    get = Mock()
    assert main(['--route', 'bs_official', '--output', str(REPO_ROOT / relative)], get=get) == 1
    get.assert_not_called()
    assert json.loads(capsys.readouterr().out)['rule'] == 'output_inside_repository'


def test_outside_repository_accepts_external_path():
    # Validation alone does not create this directory.
    path = Path('/fixture-capture-synthetic-output')
    assert outside_repository(path) == path.resolve()


def test_symlink_or_other_worktree_output_is_rejected(tmp_path, monkeypatch):
    # A synthetic current checkout keeps this test valid even if pytest scratch
    # itself is inside the actual checkout. The sibling exercises .git checking.
    current = tmp_path / 'current'
    current.mkdir()
    monkeypatch.setattr('tools.fixture_capture.capture.REPO_ROOT', current)
    link = tmp_path / 'link'
    link.symlink_to(current, target_is_directory=True)
    with pytest.raises(CaptureError, match='output_inside_repository'):
        outside_repository(link / 'elsewhere')
    worktree = tmp_path / 'other'
    worktree.mkdir()
    (worktree / '.git').write_text('gitdir: somewhere')
    with pytest.raises(CaptureError, match='output_inside_repository'):
        outside_repository(worktree / 'captures')


def test_partial_write_is_removed(registry, artifact_output, monkeypatch):
    tmp_path = artifact_output
    artifact, _ = _meta(registry)
    original = Path.write_bytes

    def fail_metadata(path, data):
        if path.name == 'metadata.json':
            raise OSError('synthetic write failure')
        return original(path, data)
    monkeypatch.setattr(Path, 'write_bytes', fail_metadata)
    with pytest.raises(OSError):
        write_artifact(artifact, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cli_all_attempts_each_route_once_and_writes_only_passing_routes(artifact_output, capsys):
    tmp_path = artifact_output
    htmls = [official_html(), citi_html(), official_html('citi_secondary'), mibank_html(), mibank_html()]
    responses = [response(html, status=302 if i == 1 else 200) for i, html in enumerate(htmls)]
    get = Mock(side_effect=responses)
    rc = main(['--route', 'all', '--output', str(tmp_path)], get=get)
    assert rc == 1 and get.call_count == 5
    summaries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [s['route'] for s in summaries] == list(ROUTES)
    assert [s['status'] for s in summaries] == ['captured', 'rejected', 'captured', 'captured', 'captured']
    assert len(list(tmp_path.iterdir())) == 4


@pytest.mark.parametrize('failure', ['fetch', 'parse', 'extract', 'serialize', 'detector'])
def test_all_failure_paths_suppress_original_logs_errors_and_output(registry, artifact_output, capsys, monkeypatch, failure):
    tmp_path = artifact_output
    secret = 'user@example.invalid'
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    named = logging.getLogger('capture.test.named')
    named.addHandler(handler)

    def explode(*args, **kwargs):
        root.error(secret)
        named.error(secret)
        print(secret)
        raise RuntimeError(secret)
    get, _ = getter(official_html(extra=f'<aside>{secret}</aside>') if failure == 'detector' else official_html())
    if failure == 'fetch':
        get = explode
    elif failure == 'parse':
        monkeypatch.setattr('tools.fixture_capture.roundtrip.BeautifulSoup', explode)
    elif failure == 'extract':
        monkeypatch.setattr(registry.sources.utils, 'extract_selector_rates', explode)
    elif failure == 'serialize':
        monkeypatch.setattr(BeautifulSoup, 'encode', explode)
    try:
        assert main(['--route', 'bs_official', '--output', str(tmp_path)], get=get) == 1
        captured = capsys.readouterr()
        assert secret not in captured.out + captured.err + stream.getvalue()
        assert not captured.err
        assert list(tmp_path.iterdir()) == []
    finally:
        root.removeHandler(handler)
        named.removeHandler(handler)
        handler.close()


def test_import_boundary_detaches_root_and_restores_caller_handler():
    root = logging.getLogger()
    handler = logging.NullHandler()
    root.addHandler(handler)
    before = list(root.handlers)
    try:
        with quiet_logging():
            load_sources()
            assert root.handlers == []
            assert not root.isEnabledFor(logging.CRITICAL)
        assert root.handlers == before
    finally:
        root.removeHandler(handler)


@pytest.mark.parametrize('import_fails', [False, True])
def test_app_handlers_are_closed_before_first_crawler_import(monkeypatch, import_fails):
    root = logging.getLogger()
    before = list(root.handlers)
    level, disabled = root.level, root.manager.disable
    closed = []
    imports = []

    class AppHandler(logging.Handler):
        def close(self):
            closed.append(self)
            super().close()

    handler = AppHandler()

    def import_module(name):
        imports.append(name)
        if name == 'app':
            root.addHandler(handler)
        else:
            # Observe DURING loading: quiet_logging's finally has not run yet.
            assert root.handlers == []
            assert closed == [handler]
            assert not root.isEnabledFor(logging.CRITICAL)
            if import_fails:
                raise ImportError('synthetic crawler import failure')
        return SimpleNamespace()

    monkeypatch.setattr('tools.fixture_capture.runtime.importlib.import_module', import_module)
    if import_fails:
        with pytest.raises(ImportError, match='synthetic crawler import failure'):
            load_sources()
    else:
        load_sources()
    assert imports[:2] == ['app', 'app.crawlers.bs']
    assert closed == [handler]
    assert root.handlers == before
    assert (root.level, root.manager.disable) == (level, disabled)


def test_fresh_process_import_closes_app_file_handlers_without_raw_logs(tmp_path):
    files = [tmp_path / name for name in ('app.log', 'error.log')]
    code = '''
import logging
import socket
import sys
socket.socket.connect = lambda *args: (_ for _ in ()).throw(AssertionError("network forbidden"))
socket.socket.connect_ex = socket.socket.connect
socket.create_connection = socket.socket.connect
from tests._fixture_capture import redirect_app_logs
from tools.fixture_capture.runtime import quiet_logging, load_sources
with redirect_app_logs(sys.argv[1]), quiet_logging():
    sources = load_sources()
    assert logging.getLogger().handlers == []
    logging.getLogger().error("user@example.invalid")
    sources.bs.logger.error("user@example.invalid")
print("ok")
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(tmp_path)], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout == 'ok\n' and result.stderr == ''
    assert {path.name: path.stat().st_size for path in files} == {'app.log': 0, 'error.log': 0}


@pytest.mark.parametrize('field', ['html', 'metadata'])
@pytest.mark.parametrize('overflow', [False, True])
def test_serialized_size_limits_accept_exact_boundary_and_reject_one_byte_over(registry, monkeypatch, field, overflow):
    get, _ = getter(official_html())
    artifact = capture_route('bs_official', registry=registry, get=get)
    if field == 'html':
        target = 'tools.fixture_capture.roundtrip.HTML_LIMIT'
        size = len(artifact.fixture)
        rule = 'fixture_size'
    else:
        target = 'tools.fixture_capture.capture.METADATA_LIMIT'
        size = len(artifact.metadata)
        rule = 'metadata_size'
    monkeypatch.setattr(target, size - int(overflow))
    get, _ = getter(official_html())
    if overflow:
        with pytest.raises(CaptureError, match=rule):
            capture_route('bs_official', registry=registry, get=get)
    else:
        capture_route('bs_official', registry=registry, get=get)
