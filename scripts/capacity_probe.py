#!/usr/bin/env python3
"""Read-only cgroup-v2 + health + existing premium counters. No app imports/mutations.

The controller supplies this source as Python code over SSH and sends stdin heartbeats.
EOF or missing heartbeats ends sampling; an independent 45-minute limit also applies.
The existing admin password stays inside this process; only numeric aggregates leave.
"""
import base64
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from urllib.request import Request, urlopen


def premium_counters(payload):
    metrics = payload['metrics']
    rollout, load = metrics['topic_auth_rollout'], metrics['subscribe_load']
    if load['contract_version'] != 'subscribe-load/9' or load['scope'] != 'process':
        raise ValueError('premium_metrics_contract_changed')
    premium = load['premium_rc']
    values = {
        'app_pid': rollout['pid'], 'app_started_at': rollout['started_at_epoch_seconds'],
        'premium_started_total': premium['started_total'],
        'premium_unavailable_transient': premium['by_outcome']['unavailable_transient'],
        'premium_unavailable_persistent': premium['by_outcome']['unavailable_persistent'],
    }
    for key, value in values.items():
        if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                or (key != 'app_started_at' and type(value) is not int)):
            raise ValueError('invalid_premium_metrics')
    return values


def read_premium_counters():
    password = os.environ['ADMIN_PASSWORD']
    if not password:
        raise ValueError('admin_credential_missing')
    credential = base64.b64encode(('admin:' + password).encode()).decode()
    request = Request('http://127.0.0.1:8000/admin/api/ws-connection-metrics',
                      headers={'Authorization': 'Basic ' + credential})
    with urlopen(request, timeout=3) as response:
        if response.status != 200:
            raise ValueError('premium_metrics_unavailable')
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError('premium_metrics_too_large')
        return premium_counters(json.loads(body))


def read_cgroup(root=Path('/sys/fs/cgroup')):
    quota, period = (root / 'cpu.max').read_text().split()
    if quota == 'max':
        raise ValueError('cpu_quota_missing')
    stat = dict(line.split() for line in (root / 'cpu.stat').read_text().splitlines())
    return {
        'mono': time.monotonic(), 'cpu_usec': int(stat['usage_usec']),
        'throttled_usec': int(stat.get('throttled_usec', 0)),
        'cpu_cores': int(quota) / int(period),
        'memory_bytes': int((root / 'memory.current').read_text()),
        'memory_limit_bytes': int((root / 'memory.max').read_text()),
    }


def sample(previous, current):
    seconds = current['mono'] - previous['mono']
    if seconds <= 0 or current['cpu_cores'] <= 0 or current['memory_limit_bytes'] <= 0:
        raise ValueError('invalid_cgroup')
    usage = current['cpu_usec'] - previous['cpu_usec']
    if usage < 0:
        raise ValueError('cgroup_reset')
    return {
        'cpu_quota_percent': usage / (seconds * 10000 * current['cpu_cores']),
        'memory_percent': 100 * current['memory_bytes'] / current['memory_limit_bytes'],
        'cpu_cores': current['cpu_cores'],
        'memory_limit_bytes': current['memory_limit_bytes'],
        'throttled_usec_delta': current['throttled_usec'] - previous['throttled_usec'],
    }


def main():
    stopped = threading.Event()
    heartbeat = [time.monotonic()]

    def read_heartbeat():
        for line in sys.stdin:
            if line.strip() != 'keepalive':
                break
            heartbeat[0] = time.monotonic()
        stopped.set()

    threading.Thread(target=read_heartbeat, daemon=True).start()
    deadline = time.monotonic() + 2700
    previous = read_cgroup()
    while time.monotonic() < deadline and time.monotonic() - heartbeat[0] < 6:
        if stopped.wait(2):
            return
        current = read_cgroup()
        result = sample(previous, current)
        with urlopen('http://127.0.0.1:8000/health', timeout=3) as response:
            healthy = response.status == 200 and json.loads(response.read(65536)).get('status') == 'healthy'
        result.update(read_premium_counters())
        if not all(math.isfinite(v) for v in result.values()):
            raise ValueError('nonfinite_cgroup')
        print(json.dumps({'type': 'telemetry', 'unix': time.time(), 'healthy': healthy,
                          **result}), flush=True)
        previous = current


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # No exception text: endpoint errors must not leak response bodies/credentials.
        print('{"type":"telemetry_error"}', flush=True)
        raise SystemExit(1)
