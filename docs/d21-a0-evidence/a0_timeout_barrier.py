#!/usr/bin/env python3
"""Local positive control: a client deadline does not cancel a server mutation.

The HTTP server is a temporary localhost-only harness. PostgreSQL is reached only
through docker exec into the already approved network-less disposable container.
This is deliberately not presented as an OkHttp/FastAPI end-to-end test.
"""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CONTAINER = sys.argv[1]


def psql(sql: str) -> str:
    proc = subprocess.run(
        [
            "docker", "exec", "-i", CONTAINER,
            "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
            "-U", "postgres", "-d", "postgres", "-A", "-t",
        ],
        input=sql,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"psql rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


SCHEMA = """
DROP SCHEMA IF EXISTS a0timeout CASCADE;
CREATE SCHEMA a0timeout;
CREATE TABLE a0timeout.device_rows (
    token text PRIMARY KEY,
    owner_uid text NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
"""


class HarnessState:
    def __init__(self, action: str) -> None:
        self.action = action
        self.reached = threading.Event()
        self.release = threading.Event()
        self.committed = threading.Event()
        self.events: dict[str, float | str] = {}


def make_handler(state: HarnessState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _run(self) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length:
                self.rfile.read(content_length)
            state.events["barrier_reached"] = time.monotonic()
            state.reached.set()
            if not state.release.wait(timeout=10):
                state.events["handler_error"] = "release timeout"
                return
            state.events["mutation_started"] = time.monotonic()
            if state.action == "POST":
                psql(
                    "INSERT INTO a0timeout.device_rows(token, owner_uid) "
                    "VALUES ('T','A') "
                    "ON CONFLICT(token) DO UPDATE SET "
                    "owner_uid=excluded.owner_uid, committed_at=clock_timestamp();"
                )
            else:
                psql("DELETE FROM a0timeout.device_rows WHERE token='T' AND owner_uid='A';")
            state.events["db_commit_observed"] = time.monotonic()
            state.committed.set()
            payload = b'{"ok":true}'
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                state.events["response_delivery"] = "client_already_gone"

        def do_POST(self) -> None:
            self._run()

        def do_DELETE(self) -> None:
            self._run()

    return Handler


def run_case(action: str) -> dict[str, object]:
    if action == "POST":
        psql("TRUNCATE a0timeout.device_rows;")
    else:
        psql(
            "TRUNCATE a0timeout.device_rows;"
            "INSERT INTO a0timeout.device_rows(token, owner_uid) VALUES ('T','A');"
        )

    state = HarnessState(action)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=0.25)
    client_error = ""
    try:
        if action == "POST":
            connection.request("POST", "/device", body=b"{}", headers={"Content-Type": "application/json"})
        else:
            connection.request("DELETE", "/device")
        connection.getresponse().read()
    except Exception as exc:  # exact class is reported as evidence
        client_error = type(exc).__name__
        state.events["client_failure"] = time.monotonic()
    finally:
        connection.close()

    if not state.reached.wait(timeout=2):
        raise RuntimeError(f"{action}: handler never reached barrier")
    state.release.set()
    if not state.committed.wait(timeout=5):
        raise RuntimeError(f"{action}: mutation did not commit after client failure")

    count = int(psql("SELECT count(*) FROM a0timeout.device_rows WHERE token='T';"))
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

    barrier = float(state.events["barrier_reached"])
    client_failure = float(state.events["client_failure"])
    commit = float(state.events["db_commit_observed"])
    expected_count = 1 if action == "POST" else 0
    passed = (
        client_error in {"TimeoutError", "socket.timeout"}
        and barrier < client_failure < commit
        and count == expected_count
    )
    return {
        "action": action,
        "client_error": client_error,
        "ordering": {
            "barrier_before_client_failure": barrier < client_failure,
            "client_failure_before_db_commit": client_failure < commit,
        },
        "row_count_after_late_commit": count,
        "expected_row_count": expected_count,
        "response_delivery": state.events.get("response_delivery", "not_observed"),
        "passed": passed,
    }


def main() -> int:
    psql(SCHEMA)
    results = [run_case("POST"), run_case("DELETE")]
    for result in results:
        print(json.dumps(result, sort_keys=True))
    passed = all(bool(result["passed"]) for result in results)
    print(json.dumps({"summary": {"total": len(results), "passed": sum(bool(r["passed"]) for r in results)}}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
