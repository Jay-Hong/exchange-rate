#!/usr/bin/env python3
"""Bounded public-path, single-egress WS/REST capacity preparation tool.

Default is an offline plan. Execution requires its exact digest, private token input,
and read-only server telemetry. This is NOT an iOS emulator or a 100-distinct-IP test.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import sys
import time
from urllib.parse import urlsplit
import uuid


TOPICS = ('fx:usd-krw', 'fx:jpy-krw', 'fx:eur-krw', 'usdt:krw', 'dxy:spot')
TABS = ('usd', 'jpy', 'eur', 'tether')
MAX_BODY = 2 * 1024 * 1024
PROBE = Path(__file__).with_name('capacity_probe.py')


class StopRun(Exception):
    """Only constant reason codes belong in this exception."""


@dataclass(frozen=True)
class Plan:
    origin: str = 'https://fxi.kr'
    stages: tuple[int, ...] = (10, 30, 50, 100)
    ramp_seconds: float = 120
    hold_seconds: float = 600
    small_hold_seconds: float = 150
    rest_window_seconds: float = 120
    reconnect_window_seconds: float = 120
    reconnect_hold_seconds: float = 60
    cooldown_seconds: float = 30
    timeout_seconds: float = 15
    auth_interval_seconds: float = 1
    scenario: str = 'mixed'
    ssh_host: str = 'ubuntu@fxi.kr'
    ssh_key: str = '/Users/jay/fxi-server-key-pair.pem'
    container: str = 'exchange-rate-app'

    def validate(self):
        url = urlsplit(self.origin)
        if url.username or url.password or url.query or url.fragment or url.path:
            raise ValueError('origin_only_no_credentials')
        if self.origin != 'https://fxi.kr' and not (
                url.scheme == 'http' and url.hostname in {'127.0.0.1', 'localhost', '::1'}):
            raise ValueError('target_not_allowlisted')
        if not self.stages or tuple(sorted(set(self.stages))) != self.stages:
            raise ValueError('stages_must_increase')
        if len(self.stages) > 4 or any(type(n) is not int or not 1 <= n <= 100 for n in self.stages):
            raise ValueError('client_cap_100')
        for name, value in asdict(self).items():
            if name.endswith('_seconds') and (not math.isfinite(value) or not 0 < value <= 660):
                raise ValueError('invalid_duration')
        if self.origin == 'https://fxi.kr' and self.auth_interval_seconds < 1:
            raise ValueError('production_auth_rate_exceeds_one_per_second')
        if self.scenario not in {'steady', 'mixed', 'reconnect'}:
            raise ValueError('invalid_scenario')
        if self.scenario == 'mixed' and min(self.hold_seconds, self.small_hold_seconds) < self.rest_window_seconds:
            raise ValueError('rest_wave_outlasts_hold')
        if self.maximum_seconds() > 2700:
            raise ValueError('wall_clock_cap_45_minutes')
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+', self.ssh_host) or self.ssh_host.startswith('-'):
            raise ValueError('invalid_ssh_host')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', self.container):
            raise ValueError('invalid_container')

    def hold(self, count):
        return self.hold_seconds if count == 100 else self.small_hold_seconds

    def maximum_seconds(self):
        # Initial telemetry + pre/post checks + drain/cancellation allowance.
        total = 150 + sum(self.ramp_seconds + self.hold(n) + self.cooldown_seconds + 20 for n in self.stages)
        if self.scenario == 'reconnect':
            total += len(self.stages) * (self.reconnect_window_seconds + self.reconnect_hold_seconds + 20)
        return total

    def public(self):
        return {
            **asdict(self), 'topics': TOPICS, 'network_model': 'single_egress_shared_IP',
            'max_seconds': self.maximum_seconds(),
            'max_ws_connections': max(self.stages),
            'planned_subscribes': sum(self.stages) * (2 if self.scenario == 'reconnect' else 1),
            'planned_graph_catalog_gets': sum(self.stages) * 2 if self.scenario == 'mixed' else 0,
            'automatic_retries': 0, 'server_changes': False,
            'auth_pacing_scope': 'shared_WS_subscribe_and_authenticated_REST_starts',
            'external_provider_arrival_rate_guaranteed': False,
            'revenuecat_project_quota_verified': False,
            'server_event_loop_lag_measured': False,
            'stops': {'http_non_200_or_invalid_payload': 'first', 'rest_p95_seconds_gt': 3,
                      'ack_p95_seconds_gt': 5, 'ready_p95_seconds_gt': 10,
                      'cpu_quota_percent_gt': 90, 'cpu_duration_seconds': 30,
                      'memory_percent_gte': 90, 'telemetry_stale_seconds': 10,
                      'unexpected_ws_close': 'first', 'invalid_ack_or_snapshot': 'first',
                      'premium_rc_unavailable_counter_increase': 'first_observation'},
            'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'probe_sha256': hashlib.sha256(PROBE.read_bytes()).hexdigest(),
        }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def percentile(values, q):
    return sorted(values)[max(0, math.ceil(len(values) * q) - 1)] if values else None


def tokens_from_file(path, required_seconds):
    # O_NOFOLLOW prevents accidentally reading an unrelated secret via symlink.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError('token_file_must_be_owned_private_regular_file')
        raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('token_file_too_large')
    return decode_tokens(raw, required_seconds)


def decode_tokens(raw, required_seconds):
    if len(raw) > 1024 * 1024:
        raise ValueError('token_input_too_large')
    tokens = json.loads(raw)
    if not isinstance(tokens, list) or not 1 <= len(tokens) <= 100:
        raise ValueError('token_array_required')
    users = set()
    for token in tokens:
        try:
            if not isinstance(token, str) or len(token) > 16384:
                raise ValueError()
            fields = token.split('.')
            if len(fields) != 3:
                raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(fields[1] + '=' * (-len(fields[1]) % 4)))
            exp, uid = payload['exp'], payload['sub']
            if type(exp) not in (int, float) or not math.isfinite(exp) or exp < time.time() + required_seconds + 60:
                raise ValueError()
            if not isinstance(uid, str) or not uid:
                raise ValueError()
            users.add(uid)
        except Exception:
            raise ValueError('invalid_or_short_lived_token') from None
    # Unverified claims are used ONLY for expiry screening and cohort counts.
    # The real server still verifies signatures, revocation and entitlement.
    return tokens, len(users)


def accepted_leases(frame, topics, needed_seconds):
    if frame.get('operation') != 'subscribe' or frame.get('rejected_topics') != []:
        return False
    for field in ('accepted_topics', 'active_subscriptions'):
        entries = frame.get(field)
        if not isinstance(entries, list):
            return False
        mapped = {e.get('topic'): e for e in entries if isinstance(e, dict)}
        for topic in topics:
            entry = mapped.get(topic, {})
            duration = entry.get('lease_duration_seconds')
            if not isinstance(entry.get('lease_id'), str) or not entry['lease_id']:
                return False
            if type(duration) is not int or duration < needed_seconds:
                return False
    return True


def valid_snapshot(frame, topics):
    envelope = (isinstance(frame, dict) and frame.get('type') == 'snapshot'
            and type(frame.get('version')) is int and frame['version'] == 1
            and frame.get('topic') in topics and isinstance(frame.get('data'), dict)
            and bool(frame['data']))
    if not envelope:
        return False
    data, topic = frame['data'], frame['topic']
    if topic == 'dxy:spot':
        entries = [data.get('dxy')]
    elif topic == 'usdt:krw':
        entries = data.get('usdt_krw')
    else:
        entries = data.get('banks')
    if not isinstance(entries, list) or not entries:
        return False
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get('source'), str)
                or not entry['source'] or type(entry.get('rate')) not in (int, float)
                or not math.isfinite(entry['rate'])):
            return False
        try:
            stamp = datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                return False
        except (KeyError, AttributeError, ValueError):
            return False
    return True


def validate_rest(kind, payload, tab):
    if not isinstance(payload, dict):
        return False
    if kind == 'health':
        return payload.get('status') == 'healthy'
    if kind == 'catalog':
        rows = payload.get('tabs')
        return isinstance(rows, list) and set(TABS) <= {r.get('id') for r in rows if isinstance(r, dict)}
    rows = payload.get('series')
    if payload.get('tab') != tab or payload.get('period') != '1d' or not isinstance(rows, list):
        return False
    points = [p for row in rows if isinstance(row, dict) for p in row.get('data', [])]
    if not points:
        return False
    for point in points:
        if not isinstance(point, dict) or type(point.get('rate')) not in (int, float) or not math.isfinite(point['rate']):
            return False
        try:
            datetime.fromisoformat(point['ts'].replace('Z', '+00:00'))
        except (KeyError, ValueError, AttributeError):
            return False
    return True


class Evidence:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.handle = (self.directory / 'events.jsonl').open('x')
        self.started = time.monotonic()
        self.seq = 0
        self.rows = defaultdict(list)
        self.statuses = Counter()

    def event(self, event, **fields):
        self.seq += 1
        self.handle.write(json.dumps({'seq': self.seq, 'event': event,
            'at': datetime.now(timezone.utc).isoformat(), 'elapsed': time.monotonic() - self.started,
            **fields}, allow_nan=False) + '\n')
        self.handle.flush()

    def latency(self, stage, kind, elapsed, status=200):
        key = f'{stage}:{kind}'
        self.rows[key].append(elapsed)
        self.statuses[f'{key}:{status}'] += 1
        self.event('request_result', stage=stage, kind=kind, seconds=elapsed, status=status)
        if len(self.rows[key]) >= 20:
            limit = {'ack': 5, 'ready': 10, 'graph': 3, 'catalog': 3}.get(kind)
            if limit and percentile(self.rows[key], .95) > limit:
                raise StopRun('latency_threshold')

    def summary(self):
        return {key: {'count': len(vals), 'p50': percentile(vals, .5), 'p95': percentile(vals, .95),
                      'max': max(vals)} for key, vals in self.rows.items()}


class TelemetryGate:
    def __init__(self):
        self.high_since = None
        self.premium_previous = None
        self.samples = 0

    def check(self, row, now):
        if row.get('type') != 'telemetry' or row.get('healthy') is not True:
            raise StopRun('telemetry_or_health_failure')
        for key in ('cpu_quota_percent', 'memory_percent', 'cpu_cores', 'memory_limit_bytes',
                    'throttled_usec_delta', 'unix'):
            v = row.get(key)
            if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
                raise StopRun('invalid_telemetry')
        if abs(time.time() - row['unix']) > 10:
            raise StopRun('stale_telemetry_or_clock_skew')
        if abs(row['cpu_cores'] - .9) > .001 or row['memory_limit_bytes'] != 800 * 1024 * 1024:
            raise StopRun('resource_limits_changed')
        if row['memory_percent'] >= 90:
            raise StopRun('memory_threshold')
        if row['cpu_quota_percent'] > 90:
            self.high_since = now if self.high_since is None else self.high_since
            if now - self.high_since >= 30:
                raise StopRun('cpu_threshold')
        else:
            self.high_since = None
        keys = ('app_pid', 'app_started_at', 'premium_started_total',
                'premium_unavailable_transient', 'premium_unavailable_persistent')
        for key in keys:
            value = row.get(key)
            if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                    or (key != 'app_started_at' and type(value) is not int)):
                raise StopRun('invalid_premium_telemetry')
        current = tuple(row[key] for key in keys)
        if self.premium_previous is not None:
            old = self.premium_previous
            if current[:2] != old[:2] or any(new < before for new, before in zip(current[2:], old[2:])):
                raise StopRun('premium_metrics_reset_or_process_changed')
            if current[3:] != old[3:]:
                raise StopRun('premium_rc_unavailable_increased')
        self.premium_previous = current
        self.samples += 1


async def monitor(plan, evidence, ready):
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'ServerAliveInterval=5',
               '-o', 'ServerAliveCountMax=1', '-i', plan.ssh_key, plan.ssh_host,
               shlex.join(['docker', 'exec', '-i', plan.container, 'python3', '-u', '-c', PROBE.read_text()])]
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    async def heartbeat():
        while True:
            process.stdin.write(b'keepalive\n')
            await process.stdin.drain()
            await asyncio.sleep(2)
    beats = asyncio.create_task(heartbeat())
    try:
        gate = TelemetryGate()
        while True:
            raw = await asyncio.wait_for(process.stdout.readline(), 10)
            try:
                row = json.loads(raw)
                gate.check(row, time.monotonic())
            except StopRun as exc:
                fields = {k: row[k] for k in ('premium_started_total',
                    'premium_unavailable_transient', 'premium_unavailable_persistent')
                    if type(row.get(k)) is int and row[k] >= 0}
                evidence.event('telemetry_stop', reason=str(exc), **fields)
                raise
            except Exception:
                raise StopRun('telemetry_lost') from None
            # Explicit allowlist prevents a remote message from injecting private values.
            evidence.event('telemetry', **{k: row[k] for k in (
                'cpu_quota_percent', 'memory_percent', 'cpu_cores', 'memory_limit_bytes',
                'throttled_usec_delta', 'healthy', 'app_pid', 'app_started_at',
                'premium_started_total', 'premium_unavailable_transient', 'premium_unavailable_persistent')})
            if gate.samples >= 2:
                ready.set()
    finally:
        beats.cancel()
        await asyncio.gather(beats, return_exceptions=True)
        process.stdin.close()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


class RequestPacer:
    """One shared admission clock; idle time never accumulates burst credits.

    Limits this process's request starts, not remote arrivals or other clients.
    The caller starts its operation immediately after wait(), without another await.
    """
    def __init__(self, interval, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self.next_start = 0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            while self.clock() < self.next_start:
                await self.sleep(self.next_start - self.clock())
            self.next_start = self.clock() + self.interval


class Runner:
    def __init__(self, plan, tokens, evidence, *, connect=None, http_factory=None):
        # Optional dependencies are not imported by offline plan mode.
        if connect is None:
            from websockets.asyncio.client import connect
        if http_factory is None:
            import httpx
            http_factory = httpx.AsyncClient
        self.plan, self.tokens, self.evidence = plan, tokens, evidence
        self.connect, self.http_factory = connect, http_factory
        self.http = None
        self.open_connections = 0
        self.peak_connections = 0
        self.completed_stages = []
        self.delivery_observations = []
        self.auth_pacer = RequestPacer(plan.auth_interval_seconds)

    async def get(self, kind, token=None, tab='usd', stage='preflight'):
        paths = {'health': '/health', 'catalog': '/api/v2/graph/catalog',
                 'graph': f'/api/v2/graph/tab?tab={tab}&period=1d'}
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        if token:
            await self.auth_pacer.wait()
            self.evidence.event('auth_request_start', kind=kind, stage=stage)
        started = time.monotonic()
        # Overall deadline as well as HTTPX per-operation timeouts.
        async with asyncio.timeout(self.plan.timeout_seconds):
            async with self.http.stream('GET', self.plan.origin + paths[kind], headers=headers) as response:
                status = response.status_code
                if status != 200:
                    self.evidence.latency(stage, kind, time.monotonic() - started, status)
                    raise StopRun('http_non_200')
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise StopRun('http_body_limit')
                    chunks.append(chunk)
                try:
                    valid = validate_rest(kind, json.loads(b''.join(chunks)), tab)
                except Exception:
                    valid = False
                if not valid:
                    raise StopRun('invalid_rest_payload')
        self.evidence.latency(stage, kind, time.monotonic() - started)

    async def session(self, index, stage, anchor, ramp, wave):
        count = wave['count']
        await asyncio.sleep(max(0, anchor + ramp * index / count - time.monotonic()))
        token = self.tokens[index % len(self.tokens)]
        url = self.plan.origin.replace('https://', 'wss://').replace('http://', 'ws://') + '/ws'
        started = time.monotonic()
        frame_counts = Counter()
        gaps = []
        last_received = {}
        async with self.connect(url, open_timeout=self.plan.timeout_seconds, close_timeout=3,
                ping_interval=20, ping_timeout=10, max_size=MAX_BODY, max_queue=16,
                proxy=None, user_agent_header='FXi-capacity-100/1') as ws:
            self.evidence.latency(stage, 'handshake', time.monotonic() - started, 101)
            self.open_connections += 1
            self.peak_connections = max(self.peak_connections, self.open_connections)
            try:
                rid = str(uuid.uuid4())
                await self.auth_pacer.wait()
                self.evidence.event('auth_request_start', kind='subscribe', stage=stage)
                sent = time.monotonic()
                async with asyncio.timeout(self.plan.timeout_seconds):
                    await ws.send(json.dumps({'type': 'subscribe', 'request_id': rid,
                                             'topics': TOPICS, 'id_token': token}))
                acked = False
                snapshots = set()
                ready = False
                while wave['end'] is None or time.monotonic() < wave['end']:
                    now = time.monotonic()
                    deadline = now + 1 if ready else sent + self.plan.timeout_seconds
                    if wave['end'] is not None:
                        deadline = min(deadline, wave['end'])
                    try:
                        raw = await asyncio.wait_for(ws.recv(), max(.001, deadline - now))
                    except asyncio.TimeoutError:
                        if ready:
                            continue
                        raise StopRun('initial_data_timeout') from None
                    frame = json.loads(raw)
                    if not isinstance(frame, dict):
                        raise StopRun('invalid_ws_frame')
                    if frame.get('type') in {'subscription_ack', 'subscription_error'} and frame.get('request_id') == rid:
                        if frame.get('type') == 'subscription_error':
                            # This wire error is not specific to RevenueCat. Server counters
                            # provide separate evidence; never log arbitrary response text.
                            transient = frame.get('error') == 'temporarily_unavailable'
                            raise StopRun('subscribe_temporarily_unavailable' if transient else 'subscribe_rejected')
                        needed = anchor + ramp + self.plan.timeout_seconds + wave['hold'] - time.monotonic() + 5
                        if acked or not accepted_leases(frame, TOPICS, needed):
                            raise StopRun('invalid_ack_or_short_lease')
                        acked = True
                        self.evidence.latency(stage, 'ack', time.monotonic() - sent)
                    if frame.get('topic') in TOPICS:
                        if not valid_snapshot(frame, TOPICS):
                            raise StopRun('invalid_snapshot')
                        topic = frame['topic']
                        received = time.monotonic()
                        if topic in last_received:
                            # Receive interval only. No server-to-client latency claim.
                            gaps.append(received - last_received[topic])
                            if len(gaps) > 10000:
                                gaps.pop(0)
                        last_received[topic] = received
                        frame_counts[topic] += 1
                        snapshots.add(topic)
                    if not ready and acked and snapshots == set(TOPICS):
                        ready = True
                        self.evidence.latency(stage, 'ready', time.monotonic() - started)
                        wave['ready_count'] += 1
                        if wave['ready_count'] == count:
                            wave['end'] = time.monotonic() + wave['hold']
                            wave['ready'].set()
                            self.evidence.event('hold_begin', stage=stage, ready_connections=count,
                                                hold_seconds=wave['hold'])
                if not ready:
                    raise StopRun('never_ready')
            finally:
                self.open_connections -= 1
                self.delivery_observations.append({'stage': stage,
                    'updates_on_all_topics': all(frame_counts[t] > 1 for t in TOPICS)})
                self.evidence.event('ws_end', stage=stage, client=index, frames=dict(frame_counts),
                    receive_gap_p95=percentile(gaps, .95), receive_gap_samples=len(gaps))

    async def rest_wave(self, count, stage, wave):
        await wave['ready'].wait()
        anchor = time.monotonic()
        async def one(index):
            await asyncio.sleep(max(0, anchor + self.plan.rest_window_seconds * index / count - time.monotonic()))
            token, tab = self.tokens[index % len(self.tokens)], TABS[index % len(TABS)]
            # At most one REST request in flight per logical client. No retry amplification.
            await self.get('catalog', token, tab, stage)
            await self.get('graph', token, tab, stage)
        await fail_fast([one(i) for i in range(count)])
        if time.monotonic() > wave['end']:
            raise StopRun('rest_wave_outlasted_ws_hold')

    async def wave(self, count, name, ramp, hold, with_rest=False):
        anchor = time.monotonic()
        wave = {'count': count, 'hold': hold, 'ready_count': 0, 'end': None, 'ready': asyncio.Event()}
        work = [self.session(i, name, anchor, ramp, wave) for i in range(count)]
        if with_rest:
            work.append(self.rest_wave(count, name, wave))
        self.evidence.event('wave_begin', stage=name, clients=count, ramp=ramp, hold=hold)
        await fail_fast(work)
        self.evidence.event('wave_complete', stage=name, clients=count)

    async def run(self):
        async with self.http_factory(timeout=self.plan.timeout_seconds, follow_redirects=False,
                trust_env=False, headers={'User-Agent': 'FXi-capacity-100/1'}) as self.http:
            await self.get('health')
            # Validate every supplied identity through premium REST before the WS ramp.
            for token in self.tokens:
                await self.get('catalog', token)
            for count in self.plan.stages:
                name = f'{count}-initial'
                await self.wave(count, name, self.plan.ramp_seconds, self.plan.hold(count),
                                self.plan.scenario == 'mixed')
                if self.plan.scenario == 'reconnect':
                    # Only test-owned sockets are closed. No server restart/disconnection.
                    await self.wave(count, f'{count}-reconnect', self.plan.reconnect_window_seconds,
                                    self.plan.reconnect_hold_seconds)
                self.completed_stages.append(count)
                await asyncio.sleep(self.plan.cooldown_seconds)
            await self.get('health', stage='postflight')


async def fail_fast(coroutines):
    tasks = [asyncio.create_task(c) for c in coroutines]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def execute(plan, tokens, evidence, *, monitor_fn=monitor, runner_factory=Runner):
    ready = asyncio.Event()
    watcher = asyncio.create_task(monitor_fn(plan, evidence, ready))
    runner = runner_factory(plan, tokens, evidence)
    async def workload():
        await asyncio.wait_for(ready.wait(), 12)
        await runner.run()
    task = asyncio.create_task(workload())
    status, reason = 'aborted', None
    try:
        done, _ = await asyncio.wait([task, watcher], timeout=plan.maximum_seconds(),
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise StopRun('wall_clock_deadline')
        if watcher in done:
            await watcher
            raise StopRun('telemetry_ended')
        await task
        status = 'completed_bounded_scenario'
    except asyncio.CancelledError:
        reason = 'interrupted'
    except StopRun as exc:
        reason = str(exc)
    except Exception as exc:
        # Never persist exception text, HTTP bodies, tokens, UIDs or arbitrary server codes.
        reason = 'operation_failed'
        status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
        close_code = getattr(getattr(exc, 'rcvd', None), 'code', None)
        evidence.event('operation_failure', error_type=type(exc).__name__,
            handshake_status=status_code if type(status_code) is int else None,
            ws_close_code=close_code if type(close_code) is int else None)
    finally:
        for pending in (task, watcher):
            pending.cancel()
        await asyncio.gather(task, watcher, return_exceptions=True)
    report = {'status': status, 'reason': reason, 'completed_stages': runner.completed_stages,
              'peak_ws_connections': runner.peak_connections,
              'open_ws_after_cleanup': runner.open_connections, 'latencies': evidence.summary(),
              'sessions_with_updates_on_all_topics': sum(r['updates_on_all_topics'] for r in runner.delivery_observations),
              'sessions_observed': len(runner.delivery_observations),
              'statuses': dict(evidence.statuses), 'scope': 'single_egress_shared_IP; reused_test_accounts',
              'not_proven': ['100_distinct_users', '100_distinct_IPs', 'iOS_429_recovery',
                             'source_to_client_latency', 'server_event_loop_lag']}
    evidence.event('run_end', **report)
    return report


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--stages', default='10,30,50,100')
    p.add_argument('--scenario', choices=['steady', 'mixed', 'reconnect'], default='mixed')
    p.add_argument('--execute', action='store_true')
    p.add_argument('--plan-sha256')
    source = p.add_mutually_exclusive_group()
    source.add_argument('--token-file')
    source.add_argument('--tokens-stdin', action='store_true', help='Private JSON token array via pipe; never argv')
    p.add_argument('--output')
    p.add_argument('--ssh-key', default='/Users/jay/fxi-server-key-pair.pem')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    plan = Plan(stages=tuple(map(int, args.stages.split(','))), scenario=args.scenario, ssh_key=args.ssh_key)
    plan.validate()
    public = plan.public()
    sha = digest(public)
    if not args.execute:
        print(json.dumps({'plan_sha256': sha, 'plan': public}, indent=2))
        return 0
    if args.plan_sha256 != sha or not (args.token_file or args.tokens_stdin) or not args.output:
        raise ValueError('exact_plan_token_file_and_new_output_required')
    if args.tokens_stdin:
        if sys.stdin.isatty():
            raise ValueError('tokens_stdin_requires_pipe_not_terminal')
        tokens, identities = decode_tokens(sys.stdin.read(1024 * 1024 + 1), plan.maximum_seconds())
    else:
        tokens, identities = tokens_from_file(args.token_file, plan.maximum_seconds())
    evidence = Evidence(args.output)
    (evidence.directory / 'plan.json').write_text(json.dumps(public, indent=2) + '\n')
    evidence.event('run_begin', plan_sha256=sha, token_count=len(tokens),
                   distinct_unverified_subject_count=identities)
    # Disable third-party debug logging: WS debug logs may include subscribe tokens.
    logging.disable(logging.CRITICAL)
    async def run():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        return await execute(plan, tokens, evidence)
    try:
        report = asyncio.run(run())
        (evidence.directory / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'status': report['status'], 'reason': report['reason']}))
        return 0 if report['status'] == 'completed_bounded_scenario' else 2
    finally:
        evidence.handle.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('capacity tool: preflight failed (details withheld)', file=sys.stderr)
        raise SystemExit(2)
