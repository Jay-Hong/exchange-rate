#!/usr/bin/env python3
"""Capture the topic-auth rollout admin snapshot without generating traffic.

The script runs on the Docker host. It performs one loopback admin GET inside
the application container, writes the response body byte-for-byte as the
canonical artifact, and writes derived provenance to a separate metadata file.

The observation window is pinned by both the rollout process start value and
the immutable image ID. A mismatch is still captured, but exits non-zero so a
restart or deployment cannot be silently combined with the previous window.
This script never opens a WebSocket and never calls Firebase or RevenueCat.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping, Sequence


ADMIN_PATH = "/admin/api/ws-connection-metrics"
DEFAULT_CONTAINER = "exchange-rate-app"
DEFAULT_OUTPUT_DIR = Path.home() / "logs" / "topic-auth-rollout"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
EXIT_CAPTURE_FAILED = 1
EXIT_WINDOW_CHANGED = 3
IMAGE_ID = re.compile(r"^[0-9a-f]{64}$")
INSPECT_SEPARATOR = "\t"
TOKEN_COUNT_FIELDS = (
    "unverified_token_bearing_subscribe_attempts_total",
    "unverified_token_bearing_policy_attempts_total",
    "unverified_token_bearing_final_stage_rc_candidate_attempts_total",
    "unverified_token_bearing_policy_first_seen_connections_total",
    "unverified_token_bearing_active_connections_tracked",
)
TOKEN_MAP_FIELDS = (
    "unverified_token_bearing_per_topic_attempts",
    "unverified_token_bearing_per_topic_first_seen_connections",
)
ARRIVAL_FIELDS = {
    "anonymous_subscribe_arrival": "anonymous_subscribe_attempts_total",
    "unverified_token_bearing_subscribe_arrival": (
        "unverified_token_bearing_subscribe_attempts_total"
    ),
}
ARRIVAL_KEYS = frozenset({
    "bucket_seconds",
    "buckets_kept",
    "buckets_present",
    "max_in_bucket",
    "current_bucket",
    "peak_in_bucket_since_start",
})
ARRIVAL_BUCKET_SECONDS = 10
ARRIVAL_BUCKETS_KEPT = 60
EPOCH_HIGH_WATER_FIELD = (
    "unverified_token_bearing_attempts_on_one_observation_epoch_max"
)

# The password is read by the child from its own environment and never appears
# in docker/curl argv, host logs, metadata, or the captured response.
ADMIN_FETCH_PY = (
    "import base64,os,sys,urllib.error,urllib.request\n"
    "try:\n"
    " p=os.environ['ADMIN_PASSWORD']\n"
    " c=base64.b64encode(('admin:'+p).encode()).decode()\n"
    " q=urllib.request.Request('http://localhost:8000"
    + ADMIN_PATH
    + "',headers={'Authorization':'Basic '+c})\n"
    " with urllib.request.urlopen(q,timeout=8) as r:\n"
    "  if r.status != 200: raise RuntimeError('unexpected HTTP status')\n"
    "  b=r.read()\n"
    "except urllib.error.HTTPError as e:\n"
    " sys.stderr.write('HTTP '+str(e.code)+'\\n');raise SystemExit(3)\n"
    "except Exception as e:\n"
    " sys.stderr.write(type(e).__name__+'\\n');raise SystemExit(4)\n"
    "sys.stdout.buffer.write(b)\n"
)

CommandRunner = Callable[[Sequence[str], float], bytes]


class CaptureError(RuntimeError):
    """The snapshot could not be captured or proven internally consistent."""


@dataclass(frozen=True)
class RuntimeIdentity:
    container_id: str
    image_id: str
    container_started_at: str
    running: bool


@dataclass(frozen=True)
class CaptureResult:
    capture_id: str
    raw_path: Path
    meta_path: Path
    window_changed: bool
    metric_schema_valid: bool
    summary: Mapping[str, object]


def _run(command: Sequence[str], timeout: float) -> bytes:
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaptureError(f"command timed out after {timeout:g}s: {command[0]}") from exc
    except OSError as exc:
        raise CaptureError(f"could not execute command: {command[0]} ({exc})") from exc
    if completed.returncode != 0:
        # The container-side fetch prints only an error class or HTTP status.
        detail = completed.stderr.decode("utf-8", "replace").strip()[:200]
        raise CaptureError(
            f"command failed with rc={completed.returncode}: {command[0]}"
            + (f" ({detail})" if detail else "")
        )
    return completed.stdout


def _normalize_image_id(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
    if not IMAGE_ID.fullmatch(normalized):
        raise CaptureError("expected image ID must be a full 64-character SHA-256")
    return normalized


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise CaptureError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CaptureError(f"{field} must be a finite number") from exc
    if not result.is_finite():
        raise CaptureError(f"{field} must be a finite number")
    return result


def _inspect_command(container: str) -> list[str]:
    # Do not inspect Config.Env: it contains production secrets.
    fields = ("{{.Id}}", "{{.Image}}", "{{.State.StartedAt}}", "{{.State.Running}}")
    return [
        "docker",
        "inspect",
        container,
        "--format",
        INSPECT_SEPARATOR.join(fields),
    ]


def _parse_runtime_identity(raw: bytes) -> RuntimeIdentity:
    try:
        lines = raw.decode("utf-8", "strict").rstrip("\n").split(INSPECT_SEPARATOR)
    except UnicodeDecodeError as exc:
        raise CaptureError("docker inspect output is not UTF-8") from exc
    if len(lines) != 4:
        raise CaptureError("docker inspect returned an unexpected shape")
    container_id, image_id, started_at, running = (line.strip() for line in lines)
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise CaptureError("docker inspect returned an invalid container ID")
    image_id = _normalize_image_id(image_id)
    if running not in ("true", "false"):
        raise CaptureError("docker inspect returned an invalid running state")
    if not started_at:
        raise CaptureError("docker inspect returned an empty StartedAt")
    return RuntimeIdentity(container_id, image_id, started_at, running == "true")


def _admin_fetch_command(container: str) -> list[str]:
    return ["docker", "exec", "-i", container, "python", "-c", ADMIN_FETCH_PY]


def _parse_rollout(raw_body: bytes) -> tuple[dict, Decimal]:
    try:
        document = json.loads(raw_body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError(f"admin response is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise CaptureError("admin response must be a JSON object")
    metrics = document.get("metrics")
    rollout = metrics.get("topic_auth_rollout") if isinstance(metrics, dict) else None
    if not isinstance(rollout, dict):
        raise CaptureError("admin response lacks metrics.topic_auth_rollout")
    observed = _decimal(
        rollout.get("started_at_epoch_seconds"),
        field="metrics.topic_auth_rollout.started_at_epoch_seconds",
    )
    return rollout, observed


def _safe_count(rollout: Mapping[str, object], key: str) -> int | None:
    value = rollout.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _arrival_schema_errors(rollout: Mapping[str, object]) -> list[str]:
    errors = []
    for field, total_field in ARRIVAL_FIELDS.items():
        arrival = rollout.get(field)
        if not isinstance(arrival, dict):
            errors.append(f"{field} must be an object")
            continue
        if set(arrival) != ARRIVAL_KEYS:
            errors.append(f"{field} must have the fixed HandshakeBuckets schema")
            continue

        values = {key: _safe_count(arrival, key) for key in ARRIVAL_KEYS}
        invalid = sorted(key for key, value in values.items() if value is None)
        if invalid:
            errors.append(
                f"{field} fields must be non-negative integers: {', '.join(invalid)}"
            )
            continue
        if (
            values["bucket_seconds"] != ARRIVAL_BUCKET_SECONDS
            or values["buckets_kept"] != ARRIVAL_BUCKETS_KEPT
        ):
            errors.append(
                f"{field} bucket identity must be "
                f"{ARRIVAL_BUCKET_SECONDS}s x {ARRIVAL_BUCKETS_KEPT}"
            )
        if values["buckets_present"] > values["buckets_kept"]:
            errors.append(f"{field} buckets_present must not exceed buckets_kept")
        if (values["buckets_present"] == 0) != (values["max_in_bucket"] == 0):
            errors.append(
                f"{field} buckets_present and max_in_bucket emptiness must agree"
            )
        if not (
            values["current_bucket"]
            <= values["max_in_bucket"]
            <= values["peak_in_bucket_since_start"]
        ):
            errors.append(
                f"{field} must satisfy current_bucket <= max_in_bucket "
                "<= peak_in_bucket_since_start"
            )

        total = _safe_count(rollout, total_field)
        if total is None and total_field not in TOKEN_COUNT_FIELDS:
            errors.append(f"{total_field} must be a non-negative integer")
        if total is not None and values["peak_in_bucket_since_start"] > total:
            errors.append(f"{field} peak must not exceed {total_field}")

    high_water = _safe_count(rollout, EPOCH_HIGH_WATER_FIELD)
    if high_water is None:
        errors.append(f"{EPOCH_HIGH_WATER_FIELD} must be a non-negative integer")
    token_total = _safe_count(
        rollout, "unverified_token_bearing_subscribe_attempts_total"
    )
    if high_water is not None and token_total is not None and high_water > token_total:
        errors.append(
            f"{EPOCH_HIGH_WATER_FIELD} must not exceed "
            "unverified_token_bearing_subscribe_attempts_total"
        )
    return errors


SUBSCRIBE_LOAD_CONTRACT = "subscribe-load/7"
# ⛔ worker 축에만 있어야 한다 — caller-only 축에 생기면 제출 시각이 없는 자리에서 0 이 쌓여
#    "대기 없음"으로 읽힌다. 축 목록은 **여기 literal** 이다(원본 파생을 쓰면 함께 줄어든다).
SUBSCRIBE_LOAD_WORKER_AXES = ("krx_entitlement", "snapshot_build")
SUBSCRIBE_LOAD_QUEUE_WAIT_FIELDS = (
    "queue_wait_observed_total", "queue_wait_ms_sum", "queue_wait_ms_max",
)
SUBSCRIBE_LOAD_SINGLEFLIGHT_FIELDS = (
    "requests_total", "leaders_total", "joined_total", "cache_hits_total",
    "successes_cached_total",
)


def _subscribe_load_schema_errors(raw_body: bytes) -> list[str]:
    """subscribe_load 블록의 계약을 캡처 시점에 잠근다.

    ⛔ 없으면 필드가 사라져도 캡처는 "성공"으로 남고, 그 창의 분석이 조용히 틀린다
       (rollout 축이 S6 에서 같은 이유로 identity 검증을 받았다).
    """
    errors: list[str] = []
    try:
        document = json.loads(raw_body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ["admin response is not valid JSON (subscribe_load)"]
    metrics = document.get("metrics") if isinstance(document, dict) else None
    block = metrics.get("subscribe_load") if isinstance(metrics, dict) else None
    if not isinstance(block, dict):
        return ["admin response lacks metrics.subscribe_load"]
    version = block.get("contract_version")
    if version != SUBSCRIBE_LOAD_CONTRACT:
        errors.append(
            f"subscribe_load contract must be {SUBSCRIBE_LOAD_CONTRACT}, got {version!r}")
    for axis in SUBSCRIBE_LOAD_WORKER_AXES:
        axis_block = block.get(axis)
        if not isinstance(axis_block, dict):
            errors.append(f"subscribe_load.{axis} must be an object")
            continue
        missing = [f for f in SUBSCRIBE_LOAD_QUEUE_WAIT_FIELDS if f not in axis_block]
        if missing:
            errors.append(f"subscribe_load.{axis} lacks {', '.join(missing)}")
    singleflight = block.get("snapshot_singleflight")
    if not isinstance(singleflight, dict):
        errors.append("subscribe_load.snapshot_singleflight must be an object")
    else:
        counts = {
            field: _safe_count(singleflight, field)
            for field in SUBSCRIBE_LOAD_SINGLEFLIGHT_FIELDS
        }
        for field, value in counts.items():
            if value is None:
                errors.append(
                    f"subscribe_load.snapshot_singleflight.{field} must be a non-negative integer"
                )
        if all(value is not None for value in counts.values()):
            classified = (
                counts["leaders_total"]
                + counts["joined_total"]
                + counts["cache_hits_total"]
            )
            if counts["requests_total"] != classified:
                errors.append(
                    "subscribe_load.snapshot_singleflight requests must equal "
                    "leaders + joined + cache_hits"
                )
            if counts["successes_cached_total"] > counts["leaders_total"]:
                errors.append(
                    "subscribe_load.snapshot_singleflight cached successes must not exceed leaders"
                )
    return errors


def _metric_schema_errors(rollout: Mapping[str, object]) -> list[str]:
    errors = []
    if not isinstance(rollout.get("stage"), str) or not rollout["stage"]:
        errors.append("stage must be a non-empty string")
    if not isinstance(rollout.get("topic_dispatcher_enabled"), bool):
        errors.append("topic_dispatcher_enabled must be a boolean")
    counts = {}
    for key in TOKEN_COUNT_FIELDS:
        value = _safe_count(rollout, key)
        if value is None:
            errors.append(f"{key} must be a non-negative integer")
        else:
            counts[key] = value

    if len(counts) == len(TOKEN_COUNT_FIELDS):
        total, policy, candidate, first_seen, active = (
            counts[key] for key in TOKEN_COUNT_FIELDS
        )
        if not candidate <= policy <= total:
            errors.append(
                "token-bearing aggregate counts must satisfy "
                "RC candidates <= policy attempts <= subscribe attempts"
            )
        if first_seen > policy:
            errors.append(
                "token-bearing policy first-seen connections must not exceed policy attempts"
            )
        if active > total:
            errors.append(
                "active token-bearing connections must not exceed subscribe attempts"
            )

    errors.extend(_arrival_schema_errors(rollout))

    topics = rollout.get("unverified_token_bearing_final_stage_rc_candidate_topics")
    topics_valid = not (
        not isinstance(topics, list)
        or not topics
        or any(not isinstance(topic, str) or not topic for topic in topics)
        or len(topics) != len(set(topics))
        or topics != sorted(topics)
    )
    if not topics_valid:
        errors.append(
            "unverified_token_bearing_final_stage_rc_candidate_topics must be a "
            "non-empty sorted unique string list"
        )

    map_keys = {}
    for key in TOKEN_MAP_FIELDS:
        values = rollout.get(key)
        valid = not (
            not isinstance(values, dict)
            or not values
            or any(not isinstance(topic, str) or not topic for topic in values)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in values.values()
            )
        )
        if not valid:
            errors.append(f"{key} must map fixed topic strings to non-negative integers")
        else:
            map_keys[key] = set(values)
    if len(map_keys) == len(TOKEN_MAP_FIELDS):
        attempts_by_topic = rollout[TOKEN_MAP_FIELDS[0]]
        first_seen_by_topic = rollout[TOKEN_MAP_FIELDS[1]]
        if map_keys[TOKEN_MAP_FIELDS[0]] != map_keys[TOKEN_MAP_FIELDS[1]]:
            errors.append("token-bearing per-topic maps must have identical fixed keys")
        else:
            impossible_topics = sorted(
                topic
                for topic in attempts_by_topic
                if first_seen_by_topic[topic] > attempts_by_topic[topic]
            )
            if impossible_topics:
                errors.append(
                    "per-topic first-seen counts exceed attempts for: "
                    f"{impossible_topics}"
                )
            if len(counts) == len(TOKEN_COUNT_FIELDS):
                if sum(attempts_by_topic.values()) < counts[TOKEN_COUNT_FIELDS[1]]:
                    errors.append(
                        "per-topic attempts cannot account for policy attempts"
                    )
                if sum(first_seen_by_topic.values()) < counts[TOKEN_COUNT_FIELDS[3]]:
                    errors.append(
                        "per-topic first-seen counts cannot account for policy first-seen "
                        "connections"
                    )
        if topics_valid and not set(topics).issubset(map_keys[TOKEN_MAP_FIELDS[0]]):
            errors.append("final-stage RC candidate topics must be policy-map keys")
    return errors


def _prepare_output_dir(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        if expanded.is_symlink():
            raise CaptureError(f"output directory must not be a symlink: {expanded}")
        expanded.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not expanded.is_dir():
            raise CaptureError(f"output path is not a directory: {expanded}")
        expanded.chmod(0o700)
        return expanded.resolve()
    except OSError as exc:
        raise CaptureError(f"could not prepare output directory {expanded}: {exc}") from exc


def _publish_artifacts(output_dir: Path, artifacts: Mapping[str, bytes]) -> None:
    finals = {name: output_dir / name for name in artifacts}
    collisions = [path.name for path in finals.values() if path.exists() or path.is_symlink()]
    if collisions:
        raise CaptureError(f"capture artifacts already exist: {sorted(collisions)}")

    staged: dict[str, Path] = {}
    published: list[Path] = []
    try:
        for name, content in artifacts.items():
            fd, temp_name = tempfile.mkstemp(prefix=f".{name}.", dir=output_dir)
            temp = Path(temp_name)
            staged[name] = temp
            with os.fdopen(fd, "wb", closefd=True) as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

        # Hard links publish without overwriting an artifact created by a race.
        for name, temp in staged.items():
            final = finals[name]
            os.link(temp, final)
            published.append(final)
        directory_fd = os.open(output_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception as exc:
        for path in published:
            path.unlink(missing_ok=True)
        if isinstance(exc, CaptureError):
            raise
        raise CaptureError(f"could not publish capture artifacts: {exc}") from exc
    finally:
        for temp in staged.values():
            temp.unlink(missing_ok=True)


def _assert_artifact_names_available(output_dir: Path, names: Sequence[str]) -> None:
    collisions = [
        name
        for name in names
        if (output_dir / name).exists() or (output_dir / name).is_symlink()
    ]
    if collisions:
        raise CaptureError(f"capture artifacts already exist: {sorted(collisions)}")


def capture(
    *,
    expected_rollout_started_at: str,
    expected_image_id: str,
    expected_stage: str,
    expected_dispatcher_enabled: bool,
    checkpoint: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    repo_root: Path = DEFAULT_REPO_ROOT,
    container: str = DEFAULT_CONTAINER,
    deployment_label: str = "",
    timeout: float = 15.0,
    runner: CommandRunner = _run,
    now: datetime | None = None,
) -> CaptureResult:
    if not checkpoint.strip():
        raise CaptureError("checkpoint must not be empty")
    if not expected_stage.strip():
        raise CaptureError("expected stage must not be empty")
    if not isinstance(expected_dispatcher_enabled, bool):
        raise CaptureError("expected dispatcher state must be a boolean")
    if not math.isfinite(timeout) or timeout <= 0:
        raise CaptureError("timeout must be finite and positive")

    expected_started = _decimal(
        expected_rollout_started_at, field="expected rollout started_at"
    )
    expected_image = _normalize_image_id(expected_image_id)
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise CaptureError("capture time must be timezone-aware")
    timestamp = timestamp.astimezone(timezone.utc)
    capture_id = timestamp.strftime("%Y%m%dT%H%M%SZ")
    captured_at = timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")
    destination = _prepare_output_dir(output_dir)
    raw_name = f"{capture_id}.raw.json"
    meta_name = f"{capture_id}.meta.json"
    _assert_artifact_names_available(
        destination,
        (raw_name, meta_name, f"{raw_name}.sha256", f"{meta_name}.sha256"),
    )
    program_path = Path(__file__).resolve()
    try:
        program_sha = hashlib.sha256(program_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CaptureError(f"could not hash capture program {program_path}: {exc}") from exc

    before = _parse_runtime_identity(runner(_inspect_command(container), timeout))
    if not before.running:
        raise CaptureError(f"container is not running: {container}")
    raw_body = runner(_admin_fetch_command(container), timeout)
    raw_sha = hashlib.sha256(raw_body).hexdigest()
    _publish_artifacts(
        destination,
        {
            raw_name: raw_body,
            f"{raw_name}.sha256": f"{raw_sha}  {raw_name}\n".encode(),
        },
    )
    parse_error = None
    try:
        rollout, observed_started = _parse_rollout(raw_body)
        metric_schema_errors = _metric_schema_errors(rollout)
        metric_schema_errors.extend(_subscribe_load_schema_errors(raw_body))
    except CaptureError as exc:
        # A successful HTTP body is evidence even when its schema is broken.
        # Preserve it and make the window unverifiable instead of discarding it.
        rollout = {}
        observed_started = None
        parse_error = str(exc)
        metric_schema_errors = [parse_error]
    after = _parse_runtime_identity(runner(_inspect_command(container), timeout))

    try:
        host_repo_head = runner(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"], timeout
        ).decode("ascii", "strict").strip()
    except UnicodeDecodeError as exc:
        raise CaptureError("host repository HEAD is not ASCII") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", host_repo_head):
        raise CaptureError("host repository HEAD is not a full commit SHA")

    container_changed = before != after
    rollout_restarted = (
        observed_started != expected_started if observed_started is not None else None
    )
    image_changed = after.image_id != expected_image
    observed_stage = rollout.get("stage") if isinstance(rollout.get("stage"), str) else None
    observed_dispatcher = rollout.get("topic_dispatcher_enabled")
    if not isinstance(observed_dispatcher, bool):
        observed_dispatcher = None
    stage_changed = observed_stage != expected_stage
    dispatcher_changed = observed_dispatcher is not expected_dispatcher_enabled
    window_changed = (
        container_changed
        or rollout_restarted is not False
        or image_changed
        or stage_changed
        or dispatcher_changed
        or not after.running
    )
    status = (
        "unverifiable"
        if observed_started is None
        else "changed" if window_changed else "matched"
    )

    meta = {
        "schema_version": 1,
        "capture_id": capture_id,
        "captured_at_utc": captured_at,
        "checkpoint": checkpoint,
        "capture_kind": "passive_admin_get",
        "manual_websocket_probe_sent": False,
        "firebase_or_revenuecat_called": False,
        "source": {
            "endpoint": ADMIN_PATH,
            "transport": "docker_exec_loopback",
            "raw_is_exact_http_response_body": True,
            "capture_program_path": str(program_path),
            "capture_program_sha256": program_sha,
        },
        "runtime": {
            "host": socket.gethostname(),
            "container_name": container,
            "container_id_before": before.container_id,
            "container_id_after": after.container_id,
            "container_image_id": after.image_id,
            "container_started_at": after.container_started_at,
            "host_repo_head": host_repo_head,
            "deployment_label": deployment_label or None,
            "deployment_label_evidence": "operator_supplied_not_runtime_verified",
        },
        "window": {
            "status": status,
            "expected_rollout_started_at_epoch_seconds": str(expected_started),
            "observed_rollout_started_at_epoch_seconds": (
                str(observed_started) if observed_started is not None else None
            ),
            "expected_image_id": expected_image,
            "observed_image_id": after.image_id,
            "expected_stage": expected_stage,
            "observed_stage": observed_stage,
            "expected_topic_dispatcher_enabled": expected_dispatcher_enabled,
            "observed_topic_dispatcher_enabled": observed_dispatcher,
            "rollout_restarted": rollout_restarted,
            "image_changed": image_changed,
            "stage_changed": stage_changed,
            "dispatcher_changed": dispatcher_changed,
            "container_changed_during_capture": container_changed,
            "verifiable": observed_started is not None,
        },
        "metric_schema": {
            "valid": not metric_schema_errors,
            "errors": metric_schema_errors,
            "response_parse_error": parse_error,
            "required_token_count_fields": list(TOKEN_COUNT_FIELDS),
            "required_token_map_fields": list(TOKEN_MAP_FIELDS),
            "required_arrival_fields": list(ARRIVAL_FIELDS),
            "required_epoch_high_water_field": EPOCH_HIGH_WATER_FIELD,
        },
        "artifacts": {
            "raw_file": raw_name,
            "raw_sha256": raw_sha,
            "sidecars_use_relative_paths": True,
        },
        "interpretation": (
            "Counters are process-local request/connection events. Token-bearing values are "
            "measured before Firebase verification and do not establish users, valid tokens, "
            "or actual RevenueCat calls. Observation windows must not be summed."
        ),
    }
    meta_body = (json.dumps(meta, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()
    meta_sha = hashlib.sha256(meta_body).hexdigest()
    artifacts = {
        meta_name: meta_body,
        f"{meta_name}.sha256": f"{meta_sha}  {meta_name}\n".encode(),
    }
    _publish_artifacts(destination, artifacts)

    summary = {
        "capture_id": capture_id,
        "checkpoint": checkpoint,
        "window_status": status,
        "metric_schema_valid": not metric_schema_errors,
        "raw_sha256": raw_sha,
        "meta_sha256": meta_sha,
        "unverified_token_bearing_subscribe_attempts_total": _safe_count(
            rollout, "unverified_token_bearing_subscribe_attempts_total"
        ),
        "unverified_token_bearing_policy_first_seen_connections_total": _safe_count(
            rollout, "unverified_token_bearing_policy_first_seen_connections_total"
        ),
    }
    return CaptureResult(
        capture_id=capture_id,
        raw_path=destination / raw_name,
        meta_path=destination / meta_name,
        window_changed=window_changed,
        metric_schema_valid=not metric_schema_errors,
        summary=summary,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-rollout-started-at", required=True)
    parser.add_argument("--expected-image-id", required=True)
    parser.add_argument("--expected-stage", required=True)
    parser.add_argument(
        "--expected-dispatcher-enabled", required=True, choices=("true", "false")
    )
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--deployment-label", default="")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = capture(
            expected_rollout_started_at=args.expected_rollout_started_at,
            expected_image_id=args.expected_image_id,
            expected_stage=args.expected_stage,
            expected_dispatcher_enabled=args.expected_dispatcher_enabled == "true",
            checkpoint=args.checkpoint,
            output_dir=args.output_dir,
            repo_root=args.repo_root,
            container=args.container,
            deployment_label=args.deployment_label,
            timeout=args.timeout,
        )
    except CaptureError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return EXIT_CAPTURE_FAILED
    ok = not result.window_changed and result.metric_schema_valid
    print(json.dumps({"ok": ok, **result.summary}, sort_keys=True))
    if not result.metric_schema_valid:
        return EXIT_CAPTURE_FAILED
    return EXIT_WINDOW_CHANGED if result.window_changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
