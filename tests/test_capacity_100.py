"""No production traffic. Real loopback HTTP/WS plus failure/cancellation tests."""
import asyncio
import base64
from dataclasses import replace
from http import HTTPStatus
import json
from pathlib import Path
import time

import pytest
from websockets.asyncio.server import serve

from scripts import capacity_100 as cap
from scripts import capacity_probe as probe


def jwt(uid='fake-test-identity', exp=None):
    payload = {'sub': uid, 'exp': time.time() + 4000 if exp is None else exp}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return 'header.' + encoded + '.fake-signature'


@pytest.mark.parametrize('change', [
    {'stages': ()}, {'stages': (101,)}, {'stages': (30, 10)}, {'stages': (10, 10)},
    {'ramp_seconds': float('nan')}, {'hold_seconds': float('inf')},
    {'origin': 'https://other.example'}, {'origin': 'https://fxi.kr?secret=x'},
    {'origin': 'https://user:pass@fxi.kr'}, {'container': 'app; pwd'},
    {'ssh_host': '-oProxyCommand=x'}, {'rest_window_seconds': 151},
    {'auth_interval_seconds': .99},
])
def test_plan_rejects_out_of_scope_and_unbounded_inputs(change):
    with pytest.raises(ValueError):
        replace(cap.Plan(), **change).validate()


def test_default_plan_is_bounded_and_hash_covers_code_and_target():
    p = cap.Plan()
    p.validate()
    report = p.public()
    assert report['max_seconds'] < 2700
    assert report['planned_subscribes'] == 190
    assert report['planned_graph_catalog_gets'] == 380
    reconnect = replace(p, scenario='reconnect')
    reconnect.validate()
    assert report['max_seconds'] == 1880
    assert reconnect.maximum_seconds() == 2680
    assert cap.digest(report) != cap.digest({**report, 'origin': 'http://localhost:8000'})
    assert cap.digest(report) != cap.digest({**report, 'tool_sha256': 'changed'})


def test_private_tokens_count_unique_users_not_number_of_tokens(tmp_path):
    path = tmp_path / 'private'
    path.write_text(json.dumps([jwt('one'), jwt('one'), jwt('two')]))
    path.chmod(0o600)
    values, users = cap.tokens_from_file(path, 900)
    assert len(values) == 3 and users == 2
    path.chmod(0o644)
    with pytest.raises(ValueError):
        cap.tokens_from_file(path, 900)
    path.chmod(0o600)
    link = tmp_path / 'link'
    link.symlink_to(path)
    with pytest.raises(OSError):
        cap.tokens_from_file(link, 900)


@pytest.mark.parametrize('expiration', [time.time() - 10, time.time() + 5, float('nan'), float('inf'), True])
def test_expired_or_nonfinite_token_is_rejected(tmp_path, expiration):
    path = tmp_path / 'private'
    path.write_text(json.dumps([jwt(exp=expiration)]))
    path.chmod(0o600)
    with pytest.raises(ValueError, match='invalid_or_short_lived_token'):
        cap.tokens_from_file(path, 900)


def ack(topics=cap.TOPICS):
    entries = [{'topic': t, 'lease_id': 'lease', 'lease_duration_seconds': 900} for t in topics]
    return {'type': 'subscription_ack', 'operation': 'subscribe', 'accepted_topics': entries,
            'active_subscriptions': entries, 'rejected_topics': []}


@pytest.mark.parametrize('bad', ['empty', 'short', 'rejected', 'bool', 'missing'])
def test_ack_does_not_false_pass_on_flag_off_or_missing_lease(bad):
    frame = ack()
    assert cap.accepted_leases(frame, cap.TOPICS, 650)
    if bad == 'empty':
        frame['accepted_topics'] = []
    elif bad == 'short':
        frame['accepted_topics'][0]['lease_duration_seconds'] = 30
    elif bad == 'rejected':
        frame['rejected_topics'] = [{'topic': cap.TOPICS[0], 'error': 'disabled'}]
    elif bad == 'bool':
        frame['accepted_topics'][0]['lease_duration_seconds'] = True
    else:
        frame['active_subscriptions'] = []
    assert not cap.accepted_leases(frame, cap.TOPICS, 650)


def telemetry(**kwargs):
    return {'type': 'telemetry', 'healthy': True, 'unix': time.time(),
            'cpu_quota_percent': 10, 'memory_percent': 40,
            'cpu_cores': .9, 'memory_limit_bytes': 800 * 1024 * 1024,
            'throttled_usec_delta': 0, 'app_pid': 123, 'app_started_at': 100.0,
            'premium_started_total': 50, 'premium_unavailable_transient': 2,
            'premium_unavailable_persistent': 1, **kwargs}


@pytest.mark.parametrize('changes,reason', [
    ({'premium_unavailable_transient': 3}, 'premium_rc_unavailable_increased'),
    ({'premium_unavailable_persistent': 2}, 'premium_rc_unavailable_increased'),
    ({'app_pid': 456}, 'premium_metrics_reset_or_process_changed'),
    ({'app_started_at': 110.0}, 'premium_metrics_reset_or_process_changed'),
    ({'premium_unavailable_transient': 0}, 'premium_metrics_reset_or_process_changed'),
    ({'premium_started_total': 0}, 'premium_metrics_reset_or_process_changed'),
    ({'premium_unavailable_transient': None}, 'invalid_premium_telemetry'),
])
def test_premium_gate_uses_deltas_and_pins_process_identity(changes, reason):
    gate = cap.TelemetryGate()
    gate.check(telemetry(), 0)  # historical failures are not current failures
    gate.check(telemetry(premium_started_total=51), 1)
    assert gate.samples == 2
    with pytest.raises(cap.StopRun, match=reason):
        gate.check(telemetry(premium_started_total=51) | changes, 2)


def test_pacer_does_not_catch_up_after_scheduler_delay():
    async def run():
        clock = [0.0]
        delays = [5.0, 0.0]
        async def oversleep(seconds):
            clock[0] += seconds + delays.pop(0)
        pacer = cap.RequestPacer(1, clock=lambda: clock[0], sleep=oversleep)
        starts = []
        for _ in range(3):
            await pacer.wait()
            starts.append(clock[0])
        assert starts == [0, 6, 7]  # never 0, 6, 6
    asyncio.run(run())


def test_cpu_stop_uses_container_quota_and_continuous_30_seconds():
    gate = cap.TelemetryGate()
    gate.check(telemetry(cpu_quota_percent=91), 0)
    gate.check(telemetry(cpu_quota_percent=95), 29.9)
    gate.check(telemetry(cpu_quota_percent=10), 30)
    gate.check(telemetry(cpu_quota_percent=95), 31)
    with pytest.raises(cap.StopRun, match='cpu_threshold'):
        gate.check(telemetry(cpu_quota_percent=95), 61)


@pytest.mark.parametrize('row', [telemetry(cpu_cores=3), telemetry(memory_percent=90),
    telemetry(unix=time.time()-60), telemetry(healthy=False), telemetry(cpu_quota_percent=float('nan'))])
def test_bad_missing_or_old_server_evidence_stops(row):
    with pytest.raises(cap.StopRun):
        cap.TelemetryGate().check(row, 1)


def test_probe_normalizes_cpu_to_quota_not_host():
    first = {'mono': 0, 'cpu_usec': 100, 'throttled_usec': 0, 'cpu_cores': .9,
             'memory_bytes': 400, 'memory_limit_bytes': 800}
    second = {**first, 'mono': 2, 'cpu_usec': 1800100, 'throttled_usec': 100}
    result = probe.sample(first, second)
    assert result['cpu_quota_percent'] == pytest.approx(100)
    assert result['memory_percent'] == 50


def metric_payload():
    return {'metrics': {'topic_auth_rollout': {'pid': 1, 'started_at_epoch_seconds': 100},
        'subscribe_load': {'contract_version': 'subscribe-load/9', 'scope': 'process',
            'premium_rc': {'started_total': 9, 'by_outcome': {
                'unavailable_transient': 2, 'unavailable_persistent': 0}}}},
        'other_fields': 'DO_NOT_LOG_THIS_BODY'}


def test_probe_uses_existing_local_admin_auth_and_exports_only_counters(monkeypatch):
    import io
    monkeypatch.setenv('ADMIN_PASSWORD', 'DO_NOT_LOG_THIS_PASSWORD')
    def fetch(request, timeout):
        assert request.full_url == 'http://127.0.0.1:8000/admin/api/ws-connection-metrics'
        assert timeout == 3
        header = request.get_header('Authorization').split()[1]
        assert base64.b64decode(header).decode() == 'admin:DO_NOT_LOG_THIS_PASSWORD'
        body = io.BytesIO(json.dumps(metric_payload()).encode())
        body.status = 200
        return body
    monkeypatch.setattr(probe, 'urlopen', fetch)
    result = probe.read_premium_counters()
    assert result['premium_unavailable_transient'] == 2
    assert len(result) == 5
    assert 'DO_NOT_LOG_THIS' not in json.dumps(result)


@pytest.mark.parametrize('mode', ['missing', 'schema', 'scope', 'type'])
def test_probe_refuses_unverifiable_premium_counters(mode):
    payload = metric_payload()
    load = payload['metrics']['subscribe_load']
    if mode == 'missing':
        del load['premium_rc']['by_outcome']['unavailable_transient']
    elif mode == 'schema':
        load['contract_version'] = 'changed'
    elif mode == 'scope':
        load['scope'] = 'unknown'
    else:
        load['premium_rc']['started_total'] = 'DO_NOT_LOG_THIS_BODY'
    with pytest.raises((KeyError, ValueError)):
        probe.premium_counters(payload)


class FakeService:
    def __init__(self, mode='good'):
        self.mode = mode
        self.requests = []
        self.open = 0
        self.peak = 0

    async def process_request(self, connection, request):
        if request.path == '/ws':
            return None
        self.requests.append(request.path)
        if request.path == '/health':
            body = {'status': 'healthy'}
        elif request.path.endswith('/catalog'):
            body = {'tabs': [{'id': t} for t in cap.TABS]}
        elif '/graph/tab?' in request.path:
            if self.mode == '429':
                return connection.respond(HTTPStatus.TOO_MANY_REQUESTS, 'DO_NOT_LOG_THIS_BODY')
            from urllib.parse import parse_qs, urlsplit
            tab = parse_qs(urlsplit(request.path).query)['tab'][0]
            body = {'tab': tab, 'period': '1d', 'series': [{'data': [
                {'rate': 1360.5, 'ts': '2026-09-16T00:00:00Z'}]}]}
        else:
            return connection.respond(HTTPStatus.NOT_FOUND, '')
        return connection.respond(HTTPStatus.OK, json.dumps(body))

    async def ws(self, connection):
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            req = json.loads(await connection.recv())
            response = {**ack(), 'request_id': req['request_id']}
            if self.mode == 'disabled':
                response['accepted_topics'] = []
            if self.mode == 'premium_unavailable':
                response = {'type': 'subscription_error', 'request_id': req['request_id'],
                            'error': 'temporarily_unavailable', 'retry_after_seconds': 30,
                            'private_detail': 'DO_NOT_LOG_THIS_BODY'}
            await connection.send(json.dumps(response))
            if self.mode == 'premium_unavailable':
                await connection.wait_closed()
                return
            if self.mode != 'missing_data':
                for topic in cap.TOPICS:
                    entry = {'source': 'test', 'rate': 1.0, 'timestamp': '2026-09-16T00:00:00Z'}
                    data = ({'dxy': entry} if topic == 'dxy:spot' else
                            {'usdt_krw': [entry]} if topic == 'usdt:krw' else {'banks': [entry]})
                    await connection.send(json.dumps({'type': 'snapshot', 'version': 1,
                                                      'topic': topic, 'data': data}))
            if self.mode == 'early_close':
                await connection.close(code=1013)
            else:
                await connection.wait_closed()
        finally:
            self.open -= 1


async def quiet_monitor(plan, evidence, ready):
    ready.set()
    await asyncio.Future()


async def exercise(tmp_path, mode='good', clients=3, scenario='mixed', monitor_fn=quiet_monitor):
    service = FakeService(mode)
    async with serve(service.ws, '127.0.0.1', 0, process_request=service.process_request) as server:
        port = server.sockets[0].getsockname()[1]
        plan = cap.Plan(origin=f'http://127.0.0.1:{port}', stages=(clients,), scenario=scenario,
            ramp_seconds=.05, hold_seconds=1.5, small_hold_seconds=1.5,
            rest_window_seconds=.1, reconnect_window_seconds=.05, reconnect_hold_seconds=.15,
            cooldown_seconds=.01, auth_interval_seconds=.002,
            timeout_seconds=2 if mode != 'missing_data' else .15)
        plan.validate()
        evidence = cap.Evidence(tmp_path / 'run')
        try:
            result = await cap.execute(plan, ['DO_NOT_LOG_THIS_TOKEN'], evidence, monitor_fn=monitor_fn)
        finally:
            evidence.handle.close()
    assert service.open == 0
    log = (tmp_path / 'run' / 'events.jsonl').read_text()
    assert 'DO_NOT_LOG_THIS' not in log
    assert result['open_ws_after_cleanup'] == 0
    return result, service, [json.loads(line) for line in log.splitlines()]


def test_real_100_loopback_connections_and_200_rest_requests(tmp_path):
    result, service, events = asyncio.run(exercise(tmp_path, clients=100))
    assert result['status'] == 'completed_bounded_scenario'
    assert result['peak_ws_connections'] == service.peak == 100
    assert result['completed_stages'] == [100]
    assert len([p for p in service.requests if '/graph/tab?' in p]) == 100
    assert result['latencies']['100-initial:ack']['count'] == 100
    assert result['latencies']['100-initial:ready']['count'] == 100
    assert result['sessions_with_updates_on_all_topics'] == 0  # initial data isn't sustained delivery
    holds = [e for e in events if e['event'] == 'hold_begin']
    finishes = [e for e in events if e['event'] == 'wave_complete']
    assert holds[0]['ready_connections'] == 100
    assert finishes[0]['elapsed'] - holds[0]['elapsed'] >= 1.5
    starts = [e for e in events if e['event'] == 'auth_request_start']
    assert len(starts) == 301  # shared pacer: preflight + 100 subscribe + 200 REST
    assert {e['kind'] for e in starts} == {'subscribe', 'catalog', 'graph'}
    assert all(b['elapsed'] - a['elapsed'] >= .0019 for a, b in zip(starts, starts[1:]))


@pytest.mark.parametrize('mode', ['disabled', '429', 'missing_data', 'early_close'])
def test_failure_aborts_and_closes_every_test_socket(tmp_path, mode):
    result, _, _ = asyncio.run(exercise(tmp_path, mode=mode))
    assert result['status'] == 'aborted'
    assert result['completed_stages'] == []


def test_premium_wire_failure_stops_without_retry_or_logging_response(tmp_path):
    result, service, events = asyncio.run(exercise(tmp_path, mode='premium_unavailable'))
    assert result['reason'] == 'subscribe_temporarily_unavailable'
    assert result['status'] == 'aborted'
    assert not any('/graph/tab?' in path for path in service.requests)
    assert not any(e['event'] == 'wave_complete' for e in events)


def test_server_wide_premium_failure_cancels_healthy_test_connections(tmp_path):
    async def failing(plan, evidence, ready):
        gate = cap.TelemetryGate()
        gate.check(telemetry(), 0)
        ready.set()
        await asyncio.sleep(.3)
        gate.check(telemetry(premium_unavailable_transient=3), 1)
    result, _, _ = asyncio.run(exercise(tmp_path, monitor_fn=failing))
    assert result['reason'] == 'premium_rc_unavailable_increased'
    assert result['status'] == 'aborted'
    assert result['open_ws_after_cleanup'] == 0


def test_reconnect_only_closes_test_sockets_and_sends_one_subscribe_per_wave(tmp_path):
    result, _, _ = asyncio.run(exercise(tmp_path, scenario='reconnect'))
    assert result['status'] == 'completed_bounded_scenario'
    assert result['latencies']['3-initial:ack']['count'] == 3
    assert result['latencies']['3-reconnect:ack']['count'] == 3


def test_watchdog_failure_cancels_load(tmp_path):
    async def failing(plan, evidence, ready):
        ready.set()
        await asyncio.sleep(.7)
        raise cap.StopRun('cpu_threshold')
    result, _, _ = asyncio.run(exercise(tmp_path, monitor_fn=failing))
    assert result['status'] == 'aborted' and result['reason'] == 'cpu_threshold'


def test_dead_watchdog_never_starts_traffic(tmp_path):
    async def dead(plan, evidence, ready):
        raise cap.StopRun('telemetry_lost')
    result, service, _ = asyncio.run(exercise(tmp_path, monitor_fn=dead))
    assert result['reason'] == 'telemetry_lost'
    assert service.requests == [] and service.peak == 0


def test_operator_cancellation_closes_connections_and_reports_interrupted(tmp_path):
    async def scenario():
        task = asyncio.current_task()
        callback = asyncio.get_running_loop().call_later(.8, task.cancel)
        try:
            return await exercise(tmp_path)
        finally:
            callback.cancel()
    result, service, _ = asyncio.run(scenario())
    assert service.peak > 0
    assert result['status'] == 'aborted' and result['reason'] == 'interrupted'


def test_probe_eof_exits_without_health_request(monkeypatch):
    import io
    monkeypatch.setattr(probe.sys, 'stdin', io.StringIO(''))
    monkeypatch.setattr(probe, 'read_cgroup', lambda: {})
    def forbidden(*args, **kwargs):
        raise AssertionError('health called after controller EOF')
    monkeypatch.setattr(probe, 'urlopen', forbidden)
    probe.main()


def test_dry_plan_does_not_load_credentials_or_connect(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError('dry plan attempted execution')
    monkeypatch.setattr(cap, 'execute', forbidden)
    monkeypatch.setattr(cap, 'tokens_from_file', forbidden)
    assert cap.main([]) == 0
    assert json.loads(capsys.readouterr().out)['plan']['max_ws_connections'] == 100


def test_old_digest_refuses_execution_before_reading_secret(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('credential read before manifest check')
    monkeypatch.setattr(cap, 'tokens_from_file', forbidden)
    with pytest.raises(ValueError, match='exact_plan'):
        cap.main(['--execute', '--plan-sha256', 'stale', '--token-file', 'not-read', '--output', str(tmp_path/'x')])


def test_existing_evidence_is_never_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        cap.Evidence(tmp_path)


@pytest.mark.parametrize('data', [{'banks': []}, {'banks': [1]}, {'banks': [{'source': 'a', 'rate': float('nan')}]},
                                {'banks': [{'source': 'a', 'rate': 1, 'timestamp': '2026-09-16T00:00:00'}]}])
def test_snapshot_requires_actual_finite_timestamped_data(data):
    assert not cap.valid_snapshot({'type': 'snapshot', 'version': 1, 'topic': cap.TOPICS[0], 'data': data}, cap.TOPICS)
