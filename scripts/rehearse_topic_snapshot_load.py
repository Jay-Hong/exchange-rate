#!/usr/bin/env python3
"""LOAD-S4 isolated 45-second topic snapshot integration rehearsal.

The production leaf builders are first exercised against local PostgreSQL 17 and Redis 7.
The timed lanes then retain the production S3/S5/S6/S7 wrapper and WS close path while a
controlled leaf supplies repeatable latency/failure. This is activation evidence, not a
production traffic generator and not proof that waiter memory has a count hard cap.

Run from the repository root::

    docker compose -p fxi-load-s4 -f docker-compose.load-s4.yml up -d --wait
    PG_TEST_URL=postgresql+psycopg://fxi_load_s4:fxi-load-s4-local@127.0.0.1:55434/fxi_load_s4 \
    REDIS_TEST_URL=redis://127.0.0.1:6394/14 \
      python scripts/rehearse_topic_snapshot_load.py --execute --artifact /exclusive/result.json
    docker compose -p fxi-load-s4 -f docker-compose.load-s4.yml down -v
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import re
import stat
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
IOS_DEFAULT = REPO.parent / "ios"
COMPOSE_FILE = REPO / "docker-compose.load-s4.yml"
PG_DATABASE = "fxi_load_s4"
REDIS_DATABASE = 14
WINDOW_SECONDS = 45.0
CLIENTS = 100
DEADLINE_EARLY_TOLERANCE_MS = 100.0
TOPICS = (
    "fx:usd-krw",
    "fx:jpy-krw",
    "fx:eur-krw",
    "usdt:krw",
    "krx:usd-krw-futures",
)


class RehearsalError(RuntimeError):
    pass


@dataclass(frozen=True)
class IOSPolicy:
    max_reconnect_attempts: int
    base_reconnect_delay: float
    reconnect_jitter_fraction: float
    reconnect_resubscribe_jitter_max: float


@dataclass
class ClientResult:
    client_id: int
    topic: str
    attempts: int = 0
    succeeded: bool = False
    close_1013: int = 0
    close_1011: int = 0
    first_1013_ms: Optional[float] = None
    latency_ms: list[float] = field(default_factory=list)


def _number(source: str, name: str) -> float:
    match = re.search(rf"static let {re.escape(name)}(?:\s*:\s*[^=]+)?\s*=\s*([0-9.]+)", source)
    if match is None:
        raise RehearsalError(f"iOS constant missing: {name}")
    return float(match.group(1))


def load_ios_policy(ios_root: Path, commit: str) -> IOSPolicy:
    def show(path: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(ios_root), "show", f"{commit}:{path}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise RehearsalError(f"cannot read pinned iOS source: {path}")
        return result.stdout

    constants = show("FXi/Utils/Constants.swift")
    service = show("FXi/Services/WebSocketService.swift")
    if "reconnectDelayNanoseconds" not in service or "reconnectResubscribeDelayNanoseconds" not in service:
        raise RehearsalError("pinned iOS jitter implementation missing")
    return IOSPolicy(
        max_reconnect_attempts=int(_number(constants, "maxReconnectAttempts")),
        base_reconnect_delay=_number(constants, "baseReconnectDelay"),
        reconnect_jitter_fraction=_number(constants, "reconnectJitterFraction"),
        reconnect_resubscribe_jitter_max=_number(constants, "reconnectResubscribeJitterMaxSeconds"),
    )


def validate_backend_url(url: str, *, kind: str) -> None:
    parsed = urlparse(url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RehearsalError(f"{kind} must be loopback-only")
    if kind == "postgres":
        if parsed.path.removeprefix("/") != PG_DATABASE or parsed.port != 55434:
            raise RehearsalError("postgres must use the dedicated fxi_load_s4 database on port 55434")
    elif kind == "redis":
        try:
            database = int(parsed.path.removeprefix("/") or "0")
        except ValueError as exc:
            raise RehearsalError("redis database must be numeric") from exc
        if database != REDIS_DATABASE or parsed.port != 6394:
            raise RehearsalError("redis must use dedicated database 14 on port 6394")
    else:
        raise ValueError(kind)


def _configure_environment(pg_url: str, redis_url: str) -> None:
    values = {
        "DATABASE_URL": pg_url,
        "DB_WORKLOAD_PROFILE": "online",
        "REDIS_URL": redis_url,
        "REDIS_PASSWORD": "",
        "TOPIC_DISPATCHER_ENABLED": "true",
        "FX_TOPIC_ENABLED": "true",
        "KRX_FUTURES_ENABLED": "true",
        "KRX_CLIENT_DISTRIBUTION_ENABLED": "true",
        "WS_TOPIC_SNAPSHOT_MAX_CONCURRENT_BUILDS": "4",
        "WS_TOPIC_SNAPSHOT_FAILURE_COOLDOWN_SECONDS": "1.0",
        "WS_TOPIC_SNAPSHOT_BUDGET_SECONDS": "8",
        "WS_TOPIC_REQUEST_DEADLINE_SECONDS": "25",
    }
    os.environ.update(values)


def percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil((p / 100.0) * len(ordered)) - 1))
    return round(ordered[index], 3)


class ProbeWebSocket:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.close_codes: list[int] = []

    async def send_json(self, payload: dict) -> None:
        self.frames.append(payload)

    async def close(self, *, code: int) -> None:
        self.close_codes.append(code)


class ControlledLeaf:
    def __init__(self, redis_client: Any, session_factory: Any) -> None:
        self.redis = redis_client
        self.session_factory = session_factory
        self.mode = "healthy"
        self._lock = threading.Lock()
        self._calls: dict[tuple[str, str], int] = {}

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self.mode = mode

    def __call__(self, topic: str) -> dict:
        from app import topic_initial_snapshot as snapshot
        from redis.exceptions import ConnectionError as RedisConnectionError
        from sqlalchemy import text

        snapshot._worker_checkpoint()
        self.redis.get(f"load-s4:{topic}")
        snapshot._worker_checkpoint()
        with self._lock:
            mode = self.mode
            key = (mode, topic)
            self._calls[key] = self._calls.get(key, 0) + 1
            call = self._calls[key]
        if mode == "cooldown" and call == 1:
            raise RedisConnectionError("LOAD-S4 controlled transient")

        db = self.session_factory()
        try:
            delay = 0.20 if mode == "healthy" else 0.02
            db.execute(text("SELECT pg_sleep(:delay)"), {"delay": delay}).scalar()
        finally:
            db.close()
        return {"type": "snapshot", "version": 1, "topic": topic, "data": {"probe": True}}


def _zero_gauges(metrics: dict) -> bool:
    return (
        metrics["snapshot_admission"]["queued_now"] == 0
        and metrics["snapshot_admission"]["in_flight"] == 0
        and metrics["snapshot_singleflight"]["waiters_now"] == 0
        and metrics["snapshot_build"]["callers_awaiting"] == 0
        and metrics["snapshot_build"]["worker_in_flight"] == 0
    )


def _metrics_clean(metrics: dict) -> bool:
    cooldown = metrics["snapshot_failure_cooldown"]
    return (
        metrics.get("contract_version") == "subscribe-load/9"
        and metrics.get("metrics_internal_errors_total") == 0
        and cooldown["by_failure_class"].get("unclassified") == 0
    )


async def _wait_for_drain(metrics_module: Any, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _zero_gauges(metrics_module.subscribe_load_metrics()):
            return
        await asyncio.sleep(0.05)
    raise RehearsalError("snapshot workers/gauges did not drain")


async def _attempt(topic: str) -> tuple[bool, Optional[int], float]:
    from app import topic_dispatcher
    from app.topic_initial_snapshot import send_initial_snapshots
    from app.topic_wire import (
        InitialSnapshotDeadlineExceeded,
        InitialSnapshotFatalFailure,
        InitialSnapshotTransientFailure,
    )

    ws = ProbeWebSocket()
    topic_dispatcher.registry.register(ws, [topic])
    started = time.monotonic()
    try:
        sent = await send_initial_snapshots(ws, [topic], channel="anonymous")
        return sent == 1, None, (time.monotonic() - started) * 1000.0
    except (InitialSnapshotDeadlineExceeded, InitialSnapshotTransientFailure):
        return False, ws.close_codes[-1] if ws.close_codes else None, (time.monotonic() - started) * 1000.0
    except InitialSnapshotFatalFailure:
        return False, ws.close_codes[-1] if ws.close_codes else None, (time.monotonic() - started) * 1000.0
    finally:
        topic_dispatcher.registry.remove_websocket(ws)


async def _client(
    client_id: int, topic: str, policy: IOSPolicy, *, initial_delay: float, seed: int
) -> ClientResult:
    result = ClientResult(client_id=client_id, topic=topic)
    rng = random.Random(seed + client_id)
    await asyncio.sleep(initial_delay)
    while True:
        result.attempts += 1
        ok, close_code, latency = await _attempt(topic)
        result.latency_ms.append(latency)
        if ok:
            result.succeeded = True
            return result
        if close_code == 1011:
            result.close_1011 += 1
            return result
        if close_code != 1013:
            return result
        result.close_1013 += 1
        if result.first_1013_ms is None:
            result.first_1013_ms = latency
        reconnect_attempt = result.close_1013
        if reconnect_attempt > policy.max_reconnect_attempts:
            return result
        signed = (rng.random() * 2.0) - 1.0
        reconnect = (
            policy.base_reconnect_delay
            * reconnect_attempt
            * (1.0 + policy.reconnect_jitter_fraction * signed)
        )
        resubscribe = policy.reconnect_resubscribe_jitter_max * rng.random()
        await asyncio.sleep(reconnect + resubscribe)


def _summarize_clients(results: list[ClientResult]) -> dict:
    latencies = [latency for result in results for latency in result.latency_ms]
    first_1013 = [result.first_1013_ms for result in results if result.first_1013_ms is not None]
    return {
        "clients": len(results),
        "succeeded": sum(result.succeeded for result in results),
        "close_1013": sum(result.close_1013 for result in results),
        "close_1011": sum(result.close_1011 for result in results),
        "attempts_max": max((result.attempts for result in results), default=0),
        "latency_samples": len(latencies),
        "latency_p95_ms": percentile(latencies, 95),
        "latency_p99_ms": percentile(latencies, 99) if len(latencies) >= 100 else None,
        "latency_max_ms": round(max(latencies), 3) if latencies else None,
        "first_1013_min_ms": round(min(first_1013), 3) if first_1013 else None,
    }


async def _run_clients(policy: IOSPolicy, *, late_arrivals: bool, seed: int) -> list[ClientResult]:
    tasks = []
    for client_id in range(CLIENTS):
        initial_delay = 0.15 if late_arrivals and client_id >= len(TOPICS) else 0.0
        tasks.append(asyncio.create_task(_client(
            client_id, TOPICS[client_id % len(TOPICS)], policy,
            initial_delay=initial_delay, seed=seed,
        )))
    return await asyncio.gather(*tasks)


def _reset_lane(snapshot: Any, metrics_module: Any, dispatcher: Any) -> None:
    metrics_module.reset_subscribe_load_metrics()
    dispatcher._topic_payload_generations.clear()
    snapshot._snapshot_states.pop(asyncio.get_running_loop(), None)


async def _run_rehearsal(policy: IOSPolicy, leaf: ControlledLeaf, engine: Any) -> dict:
    from app import subscribe_load_metrics as metrics_module
    from app import topic_dispatcher
    from app import topic_initial_snapshot as snapshot
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    started = time.monotonic()
    lanes: dict[str, dict] = {}
    with patch.object(snapshot, "_build_snapshot_sync", side_effect=leaf):
        _reset_lane(snapshot, metrics_module, topic_dispatcher)
        leaf.set_mode("healthy")
        healthy = await _run_clients(policy, late_arrivals=False, seed=1100)
        await _wait_for_drain(metrics_module)
        lanes["healthy"] = {
            "clients": _summarize_clients(healthy),
            "metrics": metrics_module.subscribe_load_metrics(),
        }

        _reset_lane(snapshot, metrics_module, topic_dispatcher)
        leaf.set_mode("cooldown")
        cooldown = await _run_clients(policy, late_arrivals=True, seed=2200)
        await _wait_for_drain(metrics_module)
        lanes["cooldown"] = {
            "clients": _summarize_clients(cooldown),
            "metrics": metrics_module.subscribe_load_metrics(),
        }

        _reset_lane(snapshot, metrics_module, topic_dispatcher)
        leaf.set_mode("pool")
        held = await asyncio.to_thread(lambda: [engine.connect() for _ in range(5)])
        pool_timeouts = 0
        timeout_lock = threading.Lock()
        original_connect = engine.pool.connect

        def counted_connect():
            nonlocal pool_timeouts
            try:
                return original_connect()
            except SATimeoutError:
                with timeout_lock:
                    pool_timeouts += 1
                raise

        async def release_pool() -> None:
            await asyncio.sleep(12.0)
            await asyncio.to_thread(lambda: [connection.close() for connection in held])

        release_task = asyncio.create_task(release_pool())
        try:
            with patch.object(engine.pool, "connect", side_effect=counted_connect):
                pool = await _run_clients(policy, late_arrivals=False, seed=3300)
                await release_task
                await _wait_for_drain(metrics_module)
        finally:
            if not release_task.done():
                release_task.cancel()
            for connection in held:
                if not connection.closed:
                    connection.close()
        lanes["pool"] = {
            "clients": _summarize_clients(pool),
            "metrics": metrics_module.subscribe_load_metrics(),
            "pool_timeouts": pool_timeouts,
            "pool_checked_out_final": engine.pool.checkedout(),
        }

        # Do not pad the 45-second contract with idle sleep. Keep real 100-client waves
        # active until the observation deadline, and invalidate through the production
        # publish path so each wave must build a fresh generation.
        _reset_lane(snapshot, metrics_module, topic_dispatcher)
        leaf.set_mode("sustained")
        sustained_results: list[ClientResult] = []
        sustained_started = time.monotonic()
        waves = 0
        while time.monotonic() < started + WINDOW_SECONDS:
            for topic in TOPICS:
                await topic_dispatcher.publish_topic(
                    topic,
                    {"type": "rate_update", "version": 1, "topic": topic,
                     "data": {"load_s4_probe": True}},
                )
            sustained_results.extend(await _run_clients(
                policy, late_arrivals=False, seed=4400 + waves * CLIENTS,
            ))
            waves += 1
        await _wait_for_drain(metrics_module)
        sustained_metrics = metrics_module.subscribe_load_metrics()
        lanes["sustained"] = {
            "clients": _summarize_clients(sustained_results),
            "metrics": sustained_metrics,
            "waves": waves,
            "duration_seconds": round(time.monotonic() - sustained_started, 3),
            "generation_by_topic": {
                topic: topic_dispatcher.topic_payload_generation(topic)
                for topic in TOPICS
            },
        }

    return {
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "active_load_seconds": round(time.monotonic() - started, 3),
        "lanes": lanes,
    }


def evaluate(result: dict) -> list[str]:
    errors: list[str] = []
    lanes = result["lanes"]
    healthy = lanes["healthy"]
    hclients, hmetrics = healthy["clients"], healthy["metrics"]
    if hclients["succeeded"] != CLIENTS or hclients["close_1013"] or hclients["close_1011"]:
        errors.append("healthy burst did not complete without closes")
    if hmetrics["snapshot_singleflight"]["leaders_total"] != len(TOPICS):
        errors.append("healthy burst did not collapse to one leader per key")
    if hmetrics["snapshot_admission"]["queued_max"] < 1:
        errors.append("healthy burst never exercised the FIFO admission queue")
    if hmetrics["snapshot_admission"]["wait_ms_max"] >= 8000:
        errors.append("healthy queue wait reached the snapshot budget")
    if not _zero_gauges(hmetrics):
        errors.append("healthy gauges did not return to zero")
    if not _metrics_clean(hmetrics):
        errors.append("healthy metrics contract is stale or internally corrupt")

    cooldown = lanes["cooldown"]
    cclients, cmetrics = cooldown["clients"], cooldown["metrics"]
    cblock = cmetrics["snapshot_failure_cooldown"]
    if cclients["succeeded"] != CLIENTS or cclients["close_1011"]:
        errors.append("cooldown lane did not recover all clients")
    if cblock["armed_total"] != len(TOPICS) or cblock["suppressed_total"] < 1:
        errors.append("cooldown did not arm per key and suppress late arrivals")
    if not _zero_gauges(cmetrics):
        errors.append("cooldown gauges did not return to zero")
    if cblock["by_failure_class"].get("redis") != len(TOPICS):
        errors.append("cooldown failures were not classified as Redis transient")
    if not _metrics_clean(cmetrics):
        errors.append("cooldown metrics contract is stale or internally corrupt")
    if cclients["close_1013"] != CLIENTS:
        errors.append("cooldown did not terminate every first attempt with 1013")

    pool = lanes["pool"]
    pclients, pmetrics = pool["clients"], pool["metrics"]
    if pclients["succeeded"] != CLIENTS or pclients["close_1011"]:
        errors.append("pool lane did not recover all clients")
    if pool["pool_timeouts"] < 1:
        errors.append("pool pressure did not produce a real SQLAlchemy pool timeout")
    minimum_1013_ms = 8000.0 - DEADLINE_EARLY_TOLERANCE_MS
    if pclients["close_1013"] < 1 or (pclients["first_1013_min_ms"] or 0) < minimum_1013_ms:
        errors.append("pool pressure did not close 1013 only after the snapshot budget")
    if pool["pool_checked_out_final"] != 0 or not _zero_gauges(pmetrics):
        errors.append("pool/orphan workers did not fully drain")
    if pmetrics["snapshot_failure_cooldown"]["by_failure_class"].get("unclassified") != 0:
        errors.append("pool failures entered the unclassified cooldown bucket")
    if not _metrics_clean(pmetrics):
        errors.append("pool metrics contract is stale or internally corrupt")
    if pclients["close_1013"] != CLIENTS:
        errors.append("pool pressure did not terminate every first attempt with 1013")

    sustained = lanes["sustained"]
    sclients, smetrics = sustained["clients"], sustained["metrics"]
    if sustained["waves"] < 10 or sclients["clients"] != sustained["waves"] * CLIENTS:
        errors.append("45-second window did not sustain repeated 100-client waves")
    if sclients["succeeded"] != sclients["clients"] or sclients["close_1013"] or sclients["close_1011"]:
        errors.append("sustained waves did not complete without closes")
    expected_leaders = sustained["waves"] * len(TOPICS)
    if smetrics["snapshot_singleflight"]["leaders_total"] != expected_leaders:
        errors.append("sustained waves did not rebuild one leader per key generation")
    if set(sustained["generation_by_topic"].values()) != {sustained["waves"]}:
        errors.append("sustained waves did not use production generation invalidation")
    if smetrics["snapshot_admission"]["queued_max"] < 1:
        errors.append("sustained waves never exercised FIFO admission")
    if not _zero_gauges(smetrics) or not _metrics_clean(smetrics):
        errors.append("sustained metrics did not finish clean and drained")
    if result.get("active_load_seconds", 0) < WINDOW_SECONDS:
        errors.append("45-second active load window was not held")
    return errors


def _prepare_backends() -> tuple[Any, Any, dict]:
    from app import latest_rates_cache
    from app.database import SessionLocal, create_all_app_tables, engine
    from app.models import SourceRate
    from sqlalchemy import text

    create_all_app_tables(engine)
    with SessionLocal() as db:
        db.query(SourceRate).delete()
        db.add(SourceRate(source="krx", asset="usd-krw-futures", rate=1400.0,
                          timestamp=datetime.now(timezone.utc).replace(tzinfo=None)))
        db.commit()
    latest_rates_cache._sync_client = None
    redis_client = latest_rates_cache._get_sync_client()
    if redis_client is None or not redis_client.ping():
        raise RehearsalError("Redis preflight failed")
    redis_client.flushdb()
    with engine.connect() as connection:
        pg_version = connection.execute(text("SHOW server_version")).scalar()
    return engine, redis_client, {"postgres": str(pg_version), "redis": str(redis_client.info()["redis_version"])}


def _actual_leaf_preflight() -> dict[str, str]:
    from app import topic_initial_snapshot as snapshot

    outcomes = {}
    for topic in TOPICS:
        payload = snapshot._build_snapshot_sync(topic)
        if not isinstance(payload, dict) or payload.get("topic") != topic:
            raise RehearsalError(f"actual snapshot leaf failed: {topic}")
        outcomes[topic] = "payload"
    return outcomes


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verdicts(errors: list[str]) -> dict[str, str]:
    if errors:
        return {
            "verdict": "NO_GO",
            "rehearsal_verdict": "NO_GO",
            "activation_verdict": "BLOCKED",
        }
    return {
        "verdict": "REHEARSAL_GO_ACTIVATION_BLOCKED",
        "rehearsal_verdict": "GO",
        "activation_verdict": "BLOCKED",
    }


def _write_artifact(path: Path, document: dict) -> None:
    if not path.parent.is_dir():
        raise RehearsalError("artifact parent must already exist")
    parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
    if parent_mode & 0o077:
        raise RehearsalError("artifact parent must not be accessible by group or others")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    try:
        sidecar_fd = os.open(sidecar, flags, 0o600)
    except Exception:
        path.unlink()
        raise
    with os.fdopen(sidecar_fd, "w", encoding="ascii") as handle:
        handle.write(f"{digest}  {path.name}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--ios-root", type=Path, default=IOS_DEFAULT)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if not args.execute:
        print(json.dumps({
            "mode": "dry-run", "window_seconds": WINDOW_SECONDS, "clients": CLIENTS,
            "topics": list(TOPICS), "compose_file": str(COMPOSE_FILE),
        }, sort_keys=True))
        return 0
    if args.artifact is None:
        raise RehearsalError("--artifact is required with --execute")
    pg_url = os.environ.get("PG_TEST_URL", "")
    redis_url = os.environ.get("REDIS_TEST_URL", "")
    validate_backend_url(pg_url, kind="postgres")
    validate_backend_url(redis_url, kind="redis")
    lock = json.loads((REPO / "spec/topic-only.lock.json").read_text())
    ios_pin = lock["pinned_commit"]["ios"]
    policy = load_ios_policy(args.ios_root, ios_pin)
    _configure_environment(pg_url, redis_url)
    engine, redis_client, versions = _prepare_backends()
    actual_leaf = _actual_leaf_preflight()
    timed = asyncio.run(_run_rehearsal(policy, ControlledLeaf(redis_client, __import__("app.database", fromlist=["SessionLocal"]).SessionLocal), engine))
    document = {
        "schema": "load-s4-rehearsal/2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "server_commit": subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip(),
        "executor_sha256": _file_sha256(Path(__file__).resolve()),
        "compose_sha256": _file_sha256(COMPOSE_FILE),
        "ios_pin": ios_pin,
        "ios_policy": asdict(policy),
        "backends": versions,
        "actual_leaf_preflight": actual_leaf,
        "timed_rehearsal": timed,
        "waiter_boundary": {
            "singleflight_followers": "time-bounded by S3 deadline; no count hard cap",
            "admission_queue": "time-bounded by S3 deadline; no count hard cap",
            "observed_singleflight_waiters_max": max(
                lane["metrics"]["snapshot_singleflight"]["waiters_max"]
                for lane in timed["lanes"].values()
            ),
            "observed_admission_queued_max": max(
                lane["metrics"]["snapshot_admission"]["queued_max"]
                for lane in timed["lanes"].values()
            ),
            "interpretation": (
                "observed admission depth reflects this lane topology and is not a structural cap; "
                "new generations can create additional flights while older builds are active"
            ),
        },
        "activation_scope": {
            "status": "BLOCKED",
            "allowed_next_step": "deploy server code with TOPIC_DISPATCHER_ENABLED=false",
            "production_flag_must_remain_off": True,
            "open_prerequisites": [
                "verify rollback image and server feature-flag safety-stop on the deployment canary",
                "capture activation before/after with the same subscribe-load/9 dashboard/log schema",
            ],
        },
        "candidate_values": {
            "max_concurrent_builds": 4,
            "failure_cooldown_seconds": 1.0,
            "snapshot_budget_seconds": 8.0,
        },
    }
    errors = evaluate(timed)
    document.update(_verdicts(errors))
    document["errors"] = errors
    _write_artifact(args.artifact, document)
    print(json.dumps({
        "verdict": document["verdict"],
        "rehearsal_verdict": document["rehearsal_verdict"],
        "activation_verdict": document["activation_verdict"],
        "artifact": str(args.artifact),
        "errors": errors,
    }))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
