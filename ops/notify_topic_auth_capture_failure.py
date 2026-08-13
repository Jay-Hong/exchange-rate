#!/usr/bin/env python3
"""Send a bounded Telegram alert for the topic-auth capture unit.

This is intentionally separate from ``app.notifications.telegram``. The app
switch is disabled in production, and its exception logging may include the
bot-token URL. This sender never prints credentials, request URLs, response
bodies, or exception strings.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


CAPTURE_UNIT = "fxi-topic-auth-capture.service"
DEFAULT_OUTPUT_DIR = Path("/home/ubuntu/logs/topic-auth-rollout")
TELEGRAM_TIMEOUT_SECONDS = 10.0
_TOKEN = re.compile(r"^[0-9]+:[A-Za-z0-9_-]{20,}$")
_CHAT_ID = re.compile(r"^-?[0-9]+$")
_CAPTURE_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_STATE_FIELDS = ("ActiveState", "SubState", "Result", "ExecMainStatus")
_WINDOW_STATES = frozenset({"matched", "changed", "unverifiable"})


class AlertError(RuntimeError):
    """The alert could not be constructed or delivered safely."""


def read_unit_state(unit: str) -> dict[str, str]:
    """Read only non-secret systemd result fields; lookup failure is explicit."""
    try:
        completed = subprocess.run(
            [
                "systemctl",
                "show",
                unit,
                "--no-pager",
                *(f"--property={field}" for field in _STATE_FIELDS),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"lookup": "failed"}
    if completed.returncode != 0:
        return {"lookup": "failed"}

    observed = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in _STATE_FIELDS:
            observed[key] = value
    return observed if observed else {"lookup": "failed"}


def latest_capture_summary(output_dir: Path) -> dict[str, object]:
    """Return a small provenance summary without copying the admin payload."""
    raw_by_id = {
        path.name.removesuffix(".raw.json"): path
        for path in output_dir.glob("*.raw.json")
        if _CAPTURE_ID.fullmatch(path.name.removesuffix(".raw.json"))
    }
    meta_by_id = {
        path.name.removesuffix(".meta.json"): path
        for path in output_dir.glob("*.meta.json")
        if _CAPTURE_ID.fullmatch(path.name.removesuffix(".meta.json"))
    }
    capture_ids = sorted(raw_by_id.keys() | meta_by_id.keys())
    if not capture_ids:
        return {"capture": "none"}
    capture_id = capture_ids[-1]
    path = meta_by_id.get(capture_id)
    if path is None:
        return {
            "capture": capture_id,
            "artifact_status": "raw_only",
            "window": "unknown",
            "schema_valid": None,
        }
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "capture": capture_id,
            "artifact_status": "meta_unreadable",
            "window": "unknown",
            "schema_valid": None,
        }
    if not isinstance(document, dict):
        return {
            "capture": capture_id,
            "artifact_status": "meta_invalid",
            "window": "unknown",
            "schema_valid": None,
        }

    window = document.get("window")
    schema = document.get("metric_schema")
    window_status = window.get("status") if isinstance(window, dict) else None
    schema_valid = schema.get("valid") if isinstance(schema, dict) else None
    if (
        document.get("capture_id") != capture_id
        or window_status not in _WINDOW_STATES
        or not isinstance(schema_valid, bool)
    ):
        return {
            "capture": capture_id,
            "artifact_status": "meta_invalid",
            "window": "unknown",
            "schema_valid": None,
        }
    return {
        "capture": capture_id,
        "artifact_status": "complete" if capture_id in raw_by_id else "raw_missing",
        "window": window_status,
        "schema_valid": schema_valid,
    }


def build_message(
    *, unit: str, state: Mapping[str, str], capture: Mapping[str, object]
) -> str:
    """Build plain text only, so no user-controlled Markdown needs escaping."""
    completed_successfully = (
        state.get("lookup") != "failed"
        and state.get("ActiveState") == "inactive"
        and state.get("SubState") == "dead"
        and state.get("Result") == "success"
        and state.get("ExecMainStatus") == "0"
    )
    kind = "INSTALLATION TEST" if completed_successfully else "FAILURE"
    lines = [
        f"FXi topic-auth capture {kind}",
        f"utc={datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"host={socket.gethostname()}",
        f"unit={unit}",
    ]
    for key in _STATE_FIELDS:
        lines.append(f"{key}={state.get(key, 'unknown')}")
    if state.get("lookup") == "failed":
        lines.append("unit_lookup=failed")
    lines.extend(
        (
            f"latest_capture={capture.get('capture', 'unknown')}",
            f"artifact_status={capture.get('artifact_status', 'unknown')}",
            f"window={capture.get('window', 'unknown')}",
            f"schema_valid={capture.get('schema_valid', 'unknown')}",
        )
    )
    return "\n".join(lines)


def send_telegram(message: str, *, opener=None) -> None:
    """Send without ever surfacing the credential-bearing request URL."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not _TOKEN.fullmatch(token):
        raise AlertError("TELEGRAM_BOT_TOKEN is missing or malformed")
    if not _CHAT_ID.fullmatch(chat_id):
        raise AlertError("TELEGRAM_CHAT_ID is missing or malformed")
    if opener is None:
        opener = urllib.request.urlopen

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode(),
        method="POST",
    )
    try:
        with opener(request, timeout=TELEGRAM_TIMEOUT_SECONDS) as response:
            body = response.read(65537)
    except urllib.error.HTTPError as exc:
        raise AlertError(f"Telegram HTTP status {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AlertError(f"Telegram transport failure ({type(exc).__name__})") from None
    except Exception as exc:  # Security boundary: never let a credential-bearing URL escape.
        raise AlertError(f"Telegram unexpected failure ({type(exc).__name__})") from None
    if len(body) > 65536:
        raise AlertError("Telegram response is too large")
    try:
        result = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise AlertError("Telegram response is not valid JSON") from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise AlertError("Telegram rejected the alert")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--unit", choices=(CAPTURE_UNIT,), default=CAPTURE_UNIT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    try:
        state = read_unit_state(args.unit)
        capture = latest_capture_summary(args.output_dir)
        send_telegram(build_message(unit=args.unit, state=state, capture=capture))
    except AlertError as exc:
        print(f"topic-auth capture alert failed: {exc}", file=sys.stderr)
        return 1
    print("topic-auth capture alert delivered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
