import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ops import notify_topic_auth_capture_failure as alert


TOKEN = "123456:abcdefghijklmnopqrstuvwxyz_ABCD"
CHAT_ID = "-123456789"


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self._body


class TestTelegramDelivery(unittest.TestCase):
    def _environment(self):
        return patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID},
            clear=False,
        )

    def test_success_uses_form_body_and_does_not_put_secrets_in_output(self):
        observed = {}

        def opener(request, *, timeout):
            observed["request"] = request
            observed["timeout"] = timeout
            return _Response(b'{"ok":true}')

        with self._environment():
            alert.send_telegram("plain message", opener=opener)

        payload = urllib.parse.parse_qs(observed["request"].data.decode())
        self.assertEqual(payload, {"chat_id": [CHAT_ID], "text": ["plain message"]})
        self.assertNotIn(TOKEN, observed["request"].data.decode())
        self.assertEqual(observed["timeout"], alert.TELEGRAM_TIMEOUT_SECONDS)

    def test_http_failure_never_surfaces_credential_url(self):
        def opener(request, *, timeout):
            del timeout
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, None)

        with self._environment(), self.assertRaises(alert.AlertError) as raised:
            alert.send_telegram("failure", opener=opener)

        self.assertEqual(str(raised.exception), "Telegram HTTP status 500")
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_transport_failure_reports_only_exception_class(self):
        secret_detail = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

        def opener(_request, *, timeout):
            del timeout
            raise urllib.error.URLError(secret_detail)

        with self._environment(), self.assertRaises(alert.AlertError) as raised:
            alert.send_telegram("failure", opener=opener)

        self.assertEqual(str(raised.exception), "Telegram transport failure (URLError)")
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_unexpected_failure_also_cannot_surface_credential_url(self):
        secret_detail = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

        def opener(_request, *, timeout):
            del timeout
            raise RuntimeError(secret_detail)

        with self._environment(), self.assertRaises(alert.AlertError) as raised:
            alert.send_telegram("failure", opener=opener)

        self.assertEqual(str(raised.exception), "Telegram unexpected failure (RuntimeError)")
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_rejected_or_malformed_response_is_failure(self):
        for body in (b'{"ok":false}', b"not-json"):
            with self.subTest(body=body), self._environment(), self.assertRaises(
                alert.AlertError
            ):
                alert.send_telegram("failure", opener=lambda *_a, **_k: _Response(body))

    def test_credentials_are_required_and_never_echoed(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(alert.AlertError) as raised:
            alert.send_telegram("failure")
        self.assertNotIn(TOKEN, str(raised.exception))


class TestFailureContext(unittest.TestCase):
    def test_latest_capture_summary_reads_only_small_provenance_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "20260813T100000Z.meta.json").write_text(
                json.dumps(
                    {
                        "capture_id": "20260813T100000Z",
                        "window": {"status": "changed", "secret": "not copied"},
                        "metric_schema": {"valid": True, "payload": "not copied"},
                    }
                )
            )

            summary = alert.latest_capture_summary(root)

        self.assertEqual(
            summary,
            {
                "capture": "20260813T100000Z",
                "artifact_status": "raw_missing",
                "window": "changed",
                "schema_valid": True,
            },
        )

    def test_latest_raw_without_meta_never_falls_back_to_stale_meta(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "20260813T100000Z.raw.json").write_text("{}")
            (root / "20260813T100000Z.meta.json").write_text(
                json.dumps(
                    {
                        "capture_id": "20260813T100000Z",
                        "window": {"status": "matched"},
                        "metric_schema": {"valid": True},
                    }
                )
            )
            (root / "20260813T110000Z.raw.json").write_text("incomplete")

            summary = alert.latest_capture_summary(root)

        self.assertEqual(
            summary,
            {
                "capture": "20260813T110000Z",
                "artifact_status": "raw_only",
                "window": "unknown",
                "schema_valid": None,
            },
        )

    def test_malformed_meta_values_cannot_expand_or_spoof_the_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "20260813T100000Z.raw.json").write_text("{}")
            (root / "20260813T100000Z.meta.json").write_text(
                json.dumps(
                    {
                        "capture_id": "spoof\n" + "x" * 10000,
                        "window": {"status": "matched\nFAKE SUCCESS"},
                        "metric_schema": {"valid": {"not": "a boolean"}},
                    }
                )
            )

            summary = alert.latest_capture_summary(root)

        self.assertEqual(
            summary,
            {
                "capture": "20260813T100000Z",
                "artifact_status": "meta_invalid",
                "window": "unknown",
                "schema_valid": None,
            },
        )

    def test_message_distinguishes_install_test_from_failure(self):
        capture = {"capture": "c1", "window": "matched", "schema_valid": True}
        healthy = alert.build_message(
            unit=alert.CAPTURE_UNIT,
            state={
                "ActiveState": "inactive",
                "SubState": "dead",
                "Result": "success",
                "ExecMainStatus": "0",
            },
            capture=capture,
        )
        failed = alert.build_message(
            unit=alert.CAPTURE_UNIT,
            state={"ActiveState": "failed", "Result": "exit-code", "ExecMainStatus": "3"},
            capture=capture,
        )
        self.assertIn("INSTALLATION TEST", healthy)
        self.assertIn("FAILURE", failed)
        self.assertIn("window=matched", failed)
        lookup_failed = alert.build_message(
            unit=alert.CAPTURE_UNIT,
            state={"lookup": "failed"},
            capture=capture,
        )
        self.assertIn("FAILURE", lookup_failed)
        self.assertIn("unit_lookup=failed", lookup_failed)

    def test_incomplete_systemd_success_state_is_fail_closed(self):
        message = alert.build_message(
            unit=alert.CAPTURE_UNIT,
            state={"ActiveState": "inactive", "Result": "success"},
            capture={"capture": "none"},
        )
        self.assertIn("FAILURE", message)

    def test_systemctl_lookup_failure_is_explicit_without_stderr_copy(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=f"secret {TOKEN}"
        )
        with patch("subprocess.run", return_value=result):
            self.assertEqual(alert.read_unit_state(alert.CAPTURE_UNIT), {"lookup": "failed"})

    def test_main_returns_nonzero_when_delivery_fails(self):
        stderr = io.StringIO()
        stdout = io.StringIO()
        with patch.object(alert, "read_unit_state", return_value={"Result": "success"}), patch.object(
            alert, "latest_capture_summary", return_value={"capture": "none"}
        ), patch.object(alert, "send_telegram", side_effect=alert.AlertError("safe failure")), redirect_stderr(
            stderr
        ), redirect_stdout(stdout):
            code = alert.main([])

        self.assertEqual(code, 1)
        self.assertIn("safe failure", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
