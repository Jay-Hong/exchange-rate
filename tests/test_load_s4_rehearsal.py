from __future__ import annotations

import asyncio
import hashlib
import pathlib
import subprocess
import sys

import pytest
import yaml

from scripts import rehearse_topic_snapshot_load as load


def test_backend_urls_are_dedicated_and_loopback_only():
    load.validate_backend_url(
        "postgresql+psycopg://u:p@127.0.0.1:55434/fxi_load_s4", kind="postgres"
    )
    load.validate_backend_url("redis://localhost:6394/14", kind="redis")
    for url, kind in (
        ("postgresql://u:p@prod.example:5432/fxi_load_s4", "postgres"),
        ("postgresql://u:p@127.0.0.1:5432/fxi_load_s4", "postgres"),
        ("redis://127.0.0.1:6379/0", "redis"),
    ):
        with pytest.raises(load.RehearsalError):
            load.validate_backend_url(url, kind=kind)


def test_ios_policy_is_read_from_the_pinned_source(tmp_path, monkeypatch):
    constants = """
    static let maxReconnectAttempts = 5
    static let baseReconnectDelay: TimeInterval = 2
    static let reconnectJitterFraction: Double = 0.2
    static let reconnectResubscribeJitterMaxSeconds: TimeInterval = 2
    """

    class Result:
        returncode = 0
        stdout = constants

    def run(command, **kwargs):
        result = Result()
        if command[-1].endswith("WebSocketService.swift"):
            result.stdout = "func reconnectDelayNanoseconds() {}\nfunc reconnectResubscribeDelayNanoseconds() {}"
        return result

    monkeypatch.setattr(load.subprocess, "run", run)
    assert load.load_ios_policy(tmp_path, "a" * 40) == load.IOSPolicy(5, 2.0, 0.2, 2.0)


def _metrics(*, queued=1, leaders=5, armed=0, suppressed=0):
    return {
        "contract_version": "subscribe-load/9",
        "metrics_internal_errors_total": 0,
        "snapshot_admission": {"queued_now": 0, "in_flight": 0, "queued_max": queued,
                               "wait_ms_max": 10.0},
        "snapshot_singleflight": {"waiters_now": 0, "leaders_total": leaders},
        "snapshot_build": {"callers_awaiting": 0, "worker_in_flight": 0},
        "snapshot_failure_cooldown": {
            "armed_total": armed, "suppressed_total": suppressed,
            "by_failure_class": {
                "deadline": 0,
                "transient_db": 0,
                "redis": armed,
                "unclassified": 0,
            },
        },
    }


def _clients(*, clients=load.CLIENTS, closes=0, first_1013=None):
    return {"clients": clients, "succeeded": clients, "close_1013": closes, "close_1011": 0,
            "first_1013_min_ms": first_1013}


def test_evaluator_requires_queue_cooldown_pool_timeout_and_drain():
    result = {
        "elapsed_seconds": 45.0,
        "active_load_seconds": 45.0,
        "lanes": {
            "healthy": {"clients": _clients(), "metrics": _metrics()},
            "cooldown": {"clients": _clients(closes=100),
                         "metrics": _metrics(armed=5, suppressed=95)},
            "pool": {"clients": _clients(closes=100, first_1013=8000.0),
                     "metrics": _metrics(), "pool_timeouts": 5,
                     "pool_checked_out_final": 0},
            "sustained": {
                "clients": _clients(clients=1000), "metrics": _metrics(leaders=50),
                "waves": 10, "generation_by_topic": {topic: 10 for topic in load.TOPICS},
            },
        },
    }
    assert load.evaluate(result) == []
    result["lanes"]["pool"]["pool_timeouts"] = 0
    assert "real SQLAlchemy pool timeout" in " ".join(load.evaluate(result))
    result["lanes"]["pool"]["pool_timeouts"] = 5
    result["active_load_seconds"] = 44.999
    assert "active load window" in " ".join(load.evaluate(result))


def test_evaluator_rejects_a_1013_that_fires_too_early():
    result = {
        "elapsed_seconds": 45.0,
        "active_load_seconds": 45.0,
        "lanes": {
            "healthy": {"clients": _clients(), "metrics": _metrics()},
            "cooldown": {"clients": _clients(closes=100),
                         "metrics": _metrics(armed=5, suppressed=95)},
            "pool": {"clients": _clients(closes=100, first_1013=7899.0),
                     "metrics": _metrics(), "pool_timeouts": 4,
                     "pool_checked_out_final": 0},
            "sustained": {
                "clients": _clients(clients=1000), "metrics": _metrics(leaders=50),
                "waves": 10, "generation_by_topic": {topic: 10 for topic in load.TOPICS},
            },
        },
    }
    assert "only after the snapshot budget" in " ".join(load.evaluate(result))


def test_compose_is_local_ephemeral_backends_only():
    compose = yaml.safe_load((load.REPO / "docker-compose.load-s4.yml").read_text())
    assert set(compose["services"]) == {"postgres", "redis"}
    assert compose["services"]["postgres"]["image"] == "postgres:17-alpine"
    assert compose["services"]["redis"]["image"] == "redis:7.4-alpine"
    assert compose["services"]["postgres"]["ports"] == ["127.0.0.1:55434:5432"]
    assert compose["services"]["redis"]["ports"] == ["127.0.0.1:6394:6379"]
    assert "/var/lib/postgresql/data" in compose["services"]["postgres"]["tmpfs"]
    assert "/data" in compose["services"]["redis"]["tmpfs"]


def test_executor_and_compose_hashes_bind_exact_file_bytes():
    assert load._file_sha256(pathlib.Path(load.__file__)) == hashlib.sha256(
        pathlib.Path(load.__file__).read_bytes()
    ).hexdigest()


def test_rehearsal_go_never_claims_activation_go():
    assert load._verdicts([]) == {
        "verdict": "REHEARSAL_GO_ACTIVATION_BLOCKED",
        "rehearsal_verdict": "GO",
        "activation_verdict": "BLOCKED",
    }
    assert load._verdicts(["failure"])["verdict"] == "NO_GO"


def test_admission_queue_has_no_topic_minus_permit_count_cap():
    from app import subscribe_load_metrics as metrics
    from app.topic_initial_snapshot import SnapshotRequestBudget, _SnapshotAdmission

    async def scenario():
        metrics.reset_subscribe_load_metrics()
        admission = _SnapshotAdmission(1)
        holder = await admission.acquire(
            "holder", SnapshotRequestBudget(
                ack_deadline_seconds=5,
                snapshot_budget_seconds=10,
                request_deadline_seconds=20,
            ),
        )

        async def acquire_then_release(index: int) -> None:
            permit = await admission.acquire(
                f"queued-{index}", SnapshotRequestBudget(
                    ack_deadline_seconds=5,
                    snapshot_budget_seconds=10,
                    request_deadline_seconds=20,
                ),
            )
            permit.release()

        tasks = [
            asyncio.create_task(acquire_then_release(index))
            for index in range(3)
        ]
        try:
            for _ in range(100):
                if metrics.subscribe_load_metrics()["snapshot_admission"]["queued_now"] == 3:
                    break
                await asyncio.sleep(0.001)
            block = metrics.subscribe_load_metrics()["snapshot_admission"]
            assert block["queued_now"] == 3
            assert block["queued_max"] == 3
        finally:
            holder.release()
        await asyncio.gather(*tasks)
        assert metrics.subscribe_load_metrics()["snapshot_admission"]["queued_now"] == 0

    asyncio.run(scenario())


def test_artifact_and_checksum_are_exclusive_and_private(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    artifact = private / "result.json"
    load._write_artifact(artifact, {"verdict": "GO"})
    sidecar = private / "result.json.sha256"
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert sidecar.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        load._write_artifact(artifact, {"verdict": "NO_GO"})


def test_artifact_rejects_a_shared_parent(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(load.RehearsalError):
        load._write_artifact(shared / "result.json", {})
    assert load._file_sha256(load.COMPOSE_FILE) == hashlib.sha256(
        load.COMPOSE_FILE.read_bytes()
    ).hexdigest()


def test_dry_run_does_not_import_the_application(monkeypatch, capsys):
    assert load.main([]) == 0
    assert '"mode": "dry-run"' in capsys.readouterr().out


def test_script_entrypoint_runs_from_the_repository_root():
    result = subprocess.run(
        [sys.executable, "scripts/rehearse_topic_snapshot_load.py"],
        cwd=load.REPO, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert '"mode": "dry-run"' in result.stdout
