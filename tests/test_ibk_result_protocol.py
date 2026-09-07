"""IBK 메시지 경계/재등록 정책의 단위 테스트. 실제 runner/Queue 통합은 후속이다."""

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from app.ibk_result_protocol import (
    MAX_RESULT_BYTES,
    MAX_STDOUT_BYTES,
    PAIRS,
    SENTINEL,
    IbkExecutionFailure,
    IbkParentDecision,
    IbkProtocolError,
    IbkReason,
    IbkSource,
    IbkStatus,
    decode_ibk_result,
    decide_ibk_process_result,
    encode_ibk_result,
)

RUN_ID = "ibk_20260907_080034_test"


@pytest.fixture
def payload():
    return {
        "schema_version": 1,
        "run_id": RUN_ID,
        "status": "OBSERVED",
        "reason": "NORMAL",
        "observed_at": "2026-09-07T08:30:36.123456+09:00",
        "expected_service_date": "2026-09-07",
        "observed_service_date": "2026-09-07",
        "official_completed_at": "2026-09-07T08:30:00+09:00",
        "source": "official_post",
        "observed_pairs": sorted(PAIRS),
        "preserved_pairs": sorted(PAIRS),  # final verified DB contents, including newly written pairs
        "missing_pairs": [],
        "changed_count": 3,
        "db_snapshot_complete": True,
    }


def frame(payload):
    return SENTINEL + json.dumps(payload).encode("utf-8") + b"\n"


def decide(stdout, **kwargs):
    return decide_ibk_process_result(stdout, expected_run_id=RUN_ID, returncode=0, **kwargs)


@pytest.mark.parametrize("status,reason,source,changed,complete,attention", [
    ("OBSERVED", "NORMAL", "official_get", 0, True, False),
    ("OBSERVED", "NORMAL", "official_post", 3, True, False),
    ("OBSERVED", "NORMAL", "official_selenium", 1, True, False),
    ("PRESERVED", "OFFICIAL_NO_SESSION", "official_post", 2, True, False),
    ("PRESERVED", "PREOPEN_PENDING", "db_snapshot", 0, True, False),
    ("DEGRADED", "SELENIUM_STRICT_REJECTED", "db_snapshot", 0, True, True),
    ("DEGRADED", "WRITE_POLICY_BLOCKED", "official_post", 0, True, True),
    ("FAILED", "DB_ERROR", None, None, None, True),
    ("FAILED", "DB_ERROR", "db_snapshot", None, True, True),
    ("FAILED", "SELENIUM_STRICT_REJECTED", None, 0, False, True),
])
@pytest.mark.parametrize("is_retry", [False, True])
def test_all_semantic_results_no_immediate_retry(
    payload, status, reason, source, changed, complete, attention, is_retry,
):
    payload.update(status=status, reason=reason, source=source,
                   changed_count=changed, db_snapshot_complete=complete)
    if status != "OBSERVED":
        payload.update(observed_service_date=None, official_completed_at=None,
                       observed_pairs=[], preserved_pairs=sorted(PAIRS))
    if complete is None:
        payload.update(observed_pairs=None, preserved_pairs=None, missing_pairs=None)
    elif complete is False:
        payload.update(preserved_pairs=[], missing_pairs=sorted(PAIRS))
    decision = decide(frame(payload), is_retry=is_retry)
    assert decision.failure is None
    assert decision.protocol_error is None
    assert decision.result.status == IbkStatus(status)
    assert decision.result.changed_count == changed
    assert decision.needs_attention is attention
    assert decision.should_retry is False
    assert decode_ibk_result(encode_ibk_result(decision.result), RUN_ID) == decision.result


@pytest.mark.parametrize("changed", [0, 1, 2, 3])
def test_preserved_can_catch_up_or_be_unchanged(payload, changed):
    payload.update(status="PRESERVED", reason="PREOPEN_PENDING", changed_count=changed,
                   observed_service_date="2026-09-04",
                   official_completed_at="2026-09-05T06:00:02+09:00")
    assert decode_ibk_result(frame(payload), RUN_ID).changed_count == changed


def test_official_observation_does_not_imply_database_application(payload):
    payload.update(status="FAILED", reason="WRITE_POLICY_BLOCKED", changed_count=0,
                   db_snapshot_complete=False, preserved_pairs=[], missing_pairs=sorted(PAIRS))
    decision = decide(frame(payload))
    assert decision.failure is None  # valid FAILED, not a protocol failure
    assert decision.result.status is IbkStatus.FAILED
    assert set(decision.result.observed_pairs) == set(decision.result.missing_pairs) == PAIRS
    assert decision.needs_attention is True
    assert decision.should_retry is False


@pytest.mark.parametrize("preserved", [None, [], ["usd-krw"], ["usd-krw", "eur-krw"]])
def test_official_pairs_cannot_supply_missing_db_evidence(payload, preserved):
    payload.update(changed_count=0, preserved_pairs=preserved)
    decision = decide(frame(payload))
    assert decision.failure is IbkExecutionFailure.RESULT_PROTOCOL_ERROR
    assert decision.protocol_error == "INCONSISTENT_DB_COMPLETE"
    assert decision.needs_attention is True


@pytest.mark.parametrize("changed", [0, 3])
def test_unchanged_and_new_write_both_require_final_db_pairs(payload, changed):
    payload["changed_count"] = changed
    result = decode_ibk_result(frame(payload), RUN_ID)
    assert set(result.preserved_pairs) == PAIRS
    assert result.status is IbkStatus.OBSERVED


def test_known_partial_db_cannot_leave_currency_unaccounted_for(payload):
    payload.update(status="FAILED", reason="WRITE_POLICY_BLOCKED", db_snapshot_complete=False,
                   preserved_pairs=["usd-krw"], missing_pairs=["jpy-krw"])
    assert decide(frame(payload)).protocol_error == "INCONSISTENT_DB_COMPLETE"


def test_unrelated_logs_are_not_results_or_retained(payload):
    noise = b'ordinary log: subscriber=PRIVATE_TEST_VALUE \xff\n'
    # A JSON logger quoting the sentinel isn't a second result frame.
    noise += b'{"message":"IBK_RESULT_JSON={}"}\n'
    result = decode_ibk_result(noise + frame(payload) + noise, RUN_ID)
    assert result.source is IbkSource.OFFICIAL_POST
    assert result.reason is IbkReason.NORMAL
    assert "PRIVATE_TEST_VALUE" not in repr(result)
    assert "PRIVATE_TEST_VALUE" not in encode_ibk_result(result).decode()
    with pytest.raises(FrozenInstanceError):
        result.changed_count = 0


@pytest.mark.parametrize("change,error", [
    ({"schema_version": True}, "INVALID_VERSION"),
    ({"schema_version": 1.0}, "INVALID_VERSION"),
    ({"schema_version": 2}, "INVALID_VERSION"),
    ({"run_id": "other_run"}, "RUN_ID_MISMATCH"),
    ({"run_id": None}, "RUN_ID_MISMATCH"),
    ({"run_id": "a" * 65}, "RUN_ID_MISMATCH"),
    ({"status": "SELENIUM_RETURNED"}, "INVALID_ENUM"),
    ({"status": ["OBSERVED"]}, "INVALID_ENUM"),
    ({"reason": "success"}, "INVALID_ENUM"),
    ({"reason": "RESULT_PROTOCOL_ERROR"}, "INVALID_ENUM"),  # parent-owned, not child semantic
    ({"source": "mibank"}, "INVALID_ENUM"),
    ({"source": "PRIVATE_TEST_VALUE"}, "INVALID_ENUM"),
    ({"changed_count": True}, "INVALID_CHANGED_COUNT"),
    ({"changed_count": -1}, "INVALID_CHANGED_COUNT"),
    ({"changed_count": 4}, "INVALID_CHANGED_COUNT"),
    ({"changed_count": 0.0}, "INVALID_CHANGED_COUNT"),
    ({"db_snapshot_complete": 1}, "INVALID_DB_COMPLETE"),
    ({"db_snapshot_complete": "true"}, "INVALID_DB_COMPLETE"),
    ({"observed_at": None}, "MISSING_OBSERVED_AT"),
    ({"observed_at": "2026-09-07T08:30:36"}, "INVALID_DATE"),
    ({"observed_at": "2026-09-07 08:30:36+09:00"}, "INVALID_DATE"),
    ({"observed_at": "2026-09-07T24:00:03+09:00"}, "INVALID_DATE"),
    ({"observed_at": True}, "INVALID_DATE"),
    ({"expected_service_date": "2026-02-30"}, "INVALID_DATE"),
    ({"expected_service_date": "20260907"}, "INVALID_DATE"),
    ({"observed_pairs": ["usd-krw"] * 3}, "INVALID_PAIRS"),
    ({"observed_pairs": ["usd-krw", "cny-krw"]}, "INVALID_PAIRS"),
    ({"observed_pairs": [None]}, "INVALID_PAIRS"),
    ({"observed_pairs": [["usd-krw"]]}, "INVALID_PAIRS"),
    ({"observed_pairs": "usd-krw"}, "INVALID_PAIRS"),
    ({"preserved_pairs": ["usd-krw"], "missing_pairs": ["usd-krw"]}, "INCONSISTENT_PAIRS"),
    ({"missing_pairs": ["usd-krw"]}, "INCONSISTENT_PAIRS"),
    ({"missing_pairs": None}, "INCONSISTENT_DB_COMPLETE"),
    ({"db_snapshot_complete": False}, "INCONSISTENT_DB_COMPLETE"),
    ({"db_snapshot_complete": False, "missing_pairs": None}, "INCONSISTENT_DB_COMPLETE"),
    ({"db_snapshot_complete": None}, "INCOMPLETE_NONFAILED_RESULT"),
    ({"observed_pairs": [], "preserved_pairs": []}, "INCONSISTENT_DB_COMPLETE"),
    ({"observed_service_date": "2026-09-04"}, "INVALID_OBSERVED"),
    ({"source": "db_snapshot"}, "INVALID_OBSERVED"),
    ({"source": None}, "INVALID_OBSERVED"),
    ({"reason": "REGRESSION_GUARD"}, "INVALID_OBSERVED"),
    ({"official_completed_at": None}, "INVALID_OBSERVED"),
    ({"changed_count": None}, "INVALID_OBSERVED"),
    ({"expected_service_date": None}, "INVALID_OBSERVED"),
    ({"status": "PRESERVED", "reason": "REGRESSION_GUARD"}, "INVALID_PRESERVED"),
    ({"status": "PRESERVED", "reason": "PREOPEN_PENDING", "expected_service_date": None}, "INVALID_PRESERVED"),
    ({"status": "PRESERVED", "reason": "PREOPEN_PENDING", "observed_service_date": "2026-09-08"}, "FUTURE_SERVICE_DATE"),
    ({"status": "DEGRADED"}, "INVALID_FAILURE_REASON"),
    ({"status": "FAILED"}, "INVALID_FAILURE_REASON"),
    ({"status": "FAILED", "reason": "SELENIUM_STRICT_REJECTED"}, "INVALID_REJECTED_STATUS"),
])
def test_invalid_payload_is_failure_not_retry_or_success(payload, change, error):
    payload.update(change)
    decision = decide(frame(payload))
    assert decision.result is None
    assert decision.failure is IbkExecutionFailure.RESULT_PROTOCOL_ERROR
    assert decision.protocol_error == error
    assert decision.needs_attention is True
    assert decision.should_retry is False
    assert "PRIVATE_TEST_VALUE" not in repr(decision)


@pytest.mark.parametrize("key", [
    "run_id", "status", "reason", "observed_at", "expected_service_date", "source",
    "observed_pairs", "preserved_pairs", "missing_pairs", "changed_count", "db_snapshot_complete",
])
def test_unknown_is_explicit_null_not_missing_field(payload, key):
    del payload[key]
    assert decide(frame(payload)).protocol_error == "INVALID_FIELDS"


def test_extra_field_is_not_forwarded_even_in_failed_result(payload):
    payload.update(status="FAILED", reason="DB_ERROR", message="PRIVATE_TEST_VALUE")
    decision = decide(frame(payload))
    assert decision.protocol_error == "INVALID_FIELDS"
    assert "PRIVATE_TEST_VALUE" not in repr(decision)


@pytest.mark.parametrize("payload", [None, [], "PRIVATE_TEST_VALUE", 1, True])
def test_non_object_json(payload):
    assert decide(frame(payload)).protocol_error == "INVALID_FIELDS"


@pytest.mark.parametrize("stdout,error", [
    (b"", "RESULT_COUNT"),
    (b"ordinary logs only\n", "RESULT_COUNT"),
    (SENTINEL + b"null\n" + SENTINEL + b"null\n", "RESULT_COUNT"),
    (SENTINEL + b"{PRIVATE_TEST_VALUE\n", "INVALID_JSON"),
    (SENTINEL + b"\xff\n", "INVALID_JSON"),
    (SENTINEL + b"{}", "INCOMPLETE_FRAME"),
    (SENTINEL + b'{"status":"OBSERVED","status":"FAILED"}\n', "DUPLICATE_JSON_KEY"),
    (SENTINEL + b'{"changed_count":NaN}\n', "NONFINITE_JSON"),
    (SENTINEL + b'{"changed_count":Infinity}\n', "NONFINITE_JSON"),
])
def test_bad_frames_have_safe_error_codes(stdout, error):
    decision = decide(stdout)
    assert decision.failure is IbkExecutionFailure.RESULT_PROTOCOL_ERROR
    assert decision.protocol_error == error
    assert decision.should_retry is False
    assert decision.needs_attention is True
    assert "PRIVATE_TEST_VALUE" not in repr(decision)


def test_deep_json_is_rejected_regardless_of_interpreter_recursion_limit():
    decision = decide(SENTINEL + b"[" * 1500 + b"]" * 1500 + b"\n")
    assert decision.failure is IbkExecutionFailure.RESULT_PROTOCOL_ERROR
    assert decision.protocol_error in {"INVALID_JSON", "INVALID_FIELDS"}
    assert decision.should_retry is False


def test_decoder_recursion_error_is_contained(monkeypatch, payload):
    stdout = frame(payload)
    def exhausted(*args, **kwargs):
        raise RecursionError("PRIVATE_TEST_VALUE")
    monkeypatch.setattr("app.ibk_result_protocol.json.loads", exhausted)
    decision = decide(stdout)
    assert decision.protocol_error == "INVALID_JSON"
    assert decision.should_retry is False
    assert "PRIVATE_TEST_VALUE" not in repr(decision)


def test_duplicate_even_if_one_frame_valid_or_identical(payload):
    valid = frame(payload)
    for other in (valid, SENTINEL + b"{broken\n"):
        for stdout in (valid + other, other + valid):
            assert decide(stdout).protocol_error == "RESULT_COUNT"


def test_payload_byte_boundary_and_crlf(payload):
    data = json.dumps(payload).encode()
    exact = SENTINEL + data + b" " * (MAX_RESULT_BYTES - len(data))
    assert decide(exact + b"\n").failure is None
    assert decide(exact + b"\r\n").failure is None
    assert decide(exact + b" \n").protocol_error == "RESULT_LIMIT"


def test_bare_cr_is_not_a_frame_boundary(payload):
    valid = frame(payload)
    assert decide(b"progress\r" + valid).protocol_error == "RESULT_COUNT"
    # CR-attached text in a log must not be counted as a second standalone frame.
    assert decide(b"progress\r" + valid + valid).failure is None
    assert decide(b"progress\r\n" + valid).failure is None
    assert decide(b"progress\n" + valid).failure is None


def test_total_output_and_truncation_are_fail_closed(payload):
    valid = frame(payload)
    exact = b"L" * (MAX_STDOUT_BYTES - len(valid) - 1) + b"\n" + valid
    assert decide(exact).failure is None
    assert decide(b"L" + exact).protocol_error == "OUTPUT_LIMIT"
    assert decide(valid, stdout_truncated=True).protocol_error == "OUTPUT_LIMIT"
    assert decide("not bytes").protocol_error == "INVALID_STDOUT_TYPE"


@pytest.mark.parametrize("timestamp", [
    "2026-09-06T23:30:36Z", "2026-09-06T23:30:36+00:00", "2026-09-07T08:30:36+09:00",
])
def test_explicit_timezones_are_accepted(payload, timestamp):
    payload["observed_at"] = timestamp
    assert decode_ibk_result(frame(payload), RUN_ID).observed_at == timestamp


def test_encoder_rejects_unvalidated_dataclass(payload):
    result = decode_ibk_result(frame(payload), RUN_ID)
    with pytest.raises(IbkProtocolError, match="INVALID_CHANGED_COUNT"):
        encode_ibk_result(replace(result, changed_count=True))


@pytest.mark.parametrize("returncode,timed_out,failure", [
    (1, False, IbkExecutionFailure.PROCESS_ERROR),
    (2, False, IbkExecutionFailure.PROCESS_ERROR),
    (-9, False, IbkExecutionFailure.PROCESS_ERROR),
    (None, False, IbkExecutionFailure.PROCESS_ERROR),  # process creation failure
    (-9, True, IbkExecutionFailure.PROCESS_TIMEOUT),
    (None, True, IbkExecutionFailure.PROCESS_TIMEOUT),
    (0, True, IbkExecutionFailure.PROCESS_TIMEOUT),
])
@pytest.mark.parametrize("is_retry", [False, True])
@pytest.mark.parametrize("valid_stdout", [False, True])
def test_process_failure_precedes_payload_and_retries_at_most_once(
    payload, returncode, timed_out, failure, is_retry, valid_stdout,
):
    decision = decide_ibk_process_result(
        frame(payload) if valid_stdout else b"broken", expected_run_id=RUN_ID,
        returncode=returncode, timed_out=timed_out, is_retry=is_retry,
    )
    assert decision.result is None
    assert decision.failure is failure
    assert decision.protocol_error is None
    assert decision.needs_attention is True
    assert decision.should_retry is (not is_retry)


@pytest.mark.parametrize("expected", [None, "", "a" * 65, "bad id", "\n"])
def test_invalid_parent_run_id_is_not_normal_success(payload, expected):
    decision = decide_ibk_process_result(frame(payload), expected_run_id=expected, returncode=0)
    assert decision.protocol_error == "INVALID_EXPECTED_RUN_ID"
    assert decision.should_retry is False


@pytest.mark.parametrize("override", [
    {"returncode": False}, {"returncode": True}, {"returncode": 0.0}, {"returncode": "0"},
    {"timed_out": "false"}, {"timed_out": 0}, {"is_retry": "false"}, {"is_retry": 1},
    {"stdout_truncated": "false"}, {"stdout_truncated": 0},
])
def test_process_metadata_does_not_coerce_booleans_or_strings(payload, override):
    kwargs = dict(expected_run_id=RUN_ID, returncode=0)
    kwargs.update(override)
    decision = decide_ibk_process_result(frame(payload), **kwargs)
    assert decision.failure is IbkExecutionFailure.RESULT_PROTOCOL_ERROR
    assert decision.protocol_error == "INVALID_PROCESS_METADATA"
    assert decision.should_retry is False
    assert decision.needs_attention is True


@pytest.mark.parametrize("kind", [
    "empty", "both", "result_retry", "result_protocol_error", "protocol_without_code",
    "protocol_retry", "process_with_protocol_code", "wrong_retry_type", "wrong_result_type",
    "wrong_failure_type",
])
def test_invalid_parent_decisions_fail_at_construction(payload, kind):
    result = decode_ibk_result(frame(payload), RUN_ID)
    process = IbkExecutionFailure.PROCESS_ERROR
    protocol = IbkExecutionFailure.RESULT_PROTOCOL_ERROR
    args = {
        "empty": (None, None, None, False),
        "both": (result, process, None, False),
        "result_retry": (result, None, None, True),
        "result_protocol_error": (result, None, "INVALID_JSON", False),
        "protocol_without_code": (None, protocol, None, False),
        "protocol_retry": (None, protocol, "INVALID_JSON", True),
        "process_with_protocol_code": (None, process, "INVALID_JSON", True),
        "wrong_retry_type": (None, process, None, 1),
        "wrong_result_type": ("OBSERVED", None, None, False),
        "wrong_failure_type": (None, "PROCESS_ERROR", None, True),
    }[kind]
    with pytest.raises(ValueError, match="INVALID_PARENT_DECISION"):
        IbkParentDecision(*args)


def test_sixty_regular_runs_with_protocol_failure_never_request_immediate_retry():
    decisions = [decide(b"normal exit without result\n") for _ in range(60)]
    assert sum(d.should_retry for d in decisions) == 0
    assert all(d.failure is IbkExecutionFailure.RESULT_PROTOCOL_ERROR for d in decisions)
    # Pure decision test, not a claim that the real Queue/alerts are already wired.
