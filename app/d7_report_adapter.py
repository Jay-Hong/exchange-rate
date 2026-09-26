"""Project producer reports into D7 ledger arguments without side effects."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.crawlers import bank_report, investing_report


AdapterResult = dict[str, Any]
_SOURCES = ("investing", "bs", "citi")


def _failed(reason: str) -> AdapterResult:
    return {"ok": False, "reason": reason}


def _identity(report: Any, expected_source: str) -> AdapterResult:
    if report is None:
        return _failed("report_missing")
    if expected_source not in _SOURCES:
        return _failed("unsupported_report")

    if isinstance(report, investing_report.InvestingReport):
        source = "investing"
        producer = investing_report
    elif isinstance(report, bank_report.BankReport):
        try:
            source = report.bank
        except Exception:
            return _failed("projection_failed")
        if source not in ("bs", "citi"):
            return _failed("unsupported_report")
        producer = bank_report
    else:
        return _failed("unsupported_report")

    if source != expected_source:
        return _failed("source_mismatch")
    try:
        round_id = report.round_id
    except AttributeError:
        return _failed("invalid_identity")
    except Exception:
        return _failed("projection_failed")
    try:
        schema = producer.SCHEMA_VERSION
        contract = producer.VALIDITY_CONTRACT
    except Exception:
        return _failed("projection_failed")
    if (type(round_id) is not str or not round_id
            or len(round_id) > 128):
        return _failed("invalid_identity")
    try:
        if len(round_id.encode("utf-8")) > 128:
            return _failed("invalid_identity")
    except UnicodeEncodeError:
        return _failed("invalid_identity")

    return {
        "ok": True,
        "source": source,
        "round_id": round_id,
        "report_schema": schema,
        "validity_contract": contract,
    }


def report_link_args(
    report: investing_report.InvestingReport | bank_report.BankReport | None,
    *,
    expected_source: str,
    received_at: int,
    received_mono: int,
) -> AdapterResult:
    """Read the identity available immediately after producer construction."""
    result = _identity(report, expected_source)
    if not result["ok"]:
        return result
    return {
        **result,
        "selected_summary": None,
        "telemetry_error_present": None,
        "received_at": received_at,
        "received_mono": received_mono,
    }


def report_finish_args(
    report: investing_report.InvestingReport | bank_report.BankReport | None,
    *,
    expected_source: str,
    finished_wall: int,
    finished_mono: int,
    received_at: int,
    received_mono: int,
) -> AdapterResult:
    """Read the finalized producer's own selection and snapshot its nested data."""
    result = _identity(report, expected_source)
    if not result["ok"]:
        return result
    try:
        status = report.execution["status"]
        if status == "running":
            return _failed("report_not_finalized")

        if isinstance(report, investing_report.InvestingReport):
            selected_summary = {
                "collection": report._collection(),
                "writing": report._writing(),
                "final_db": "not_checked",
            }
            telemetry_error_present = bool(report.telemetry_errors)
        else:
            attempts = report._attempt_payloads()
            summary = report._summary(attempts)
            selected_summary = {
                "collection": summary["collection"],
                "writing": summary["writing"],
                "final_db": summary["final_db"],
            }
            telemetry_error_present = (
                bool(report.telemetry_errors) or bool(report.telemetry_error_counts)
            )
        selected_summary = deepcopy(selected_summary)
    except Exception:
        return _failed("projection_failed")

    return {
        **result,
        "selected_summary": selected_summary,
        "telemetry_error_present": telemetry_error_present,
        "received_at": received_at,
        "received_mono": received_mono,
        "finished_wall": finished_wall,
        "finished_mono": finished_mono,
    }
