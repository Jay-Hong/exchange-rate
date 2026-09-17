"""Investing 보고 전용 증거. 저장·재시도·crawler_stats 판단에 사용하지 않는다.

SOURCE_HEALTH_PLAN §7: 시도별 증거를 보존하고 통화별 수집/쓰기/최종 DB와
실행 결과를 분리한다. writer 반환 건수는 commit 또는 통화별 쓰기 증거가 아니다.
"""

import asyncio
import concurrent.futures
import json
import math
from uuid import uuid4

from requests.exceptions import Timeout as RequestsTimeout

from app.crawlers import constants

try:
    from curl_cffi.requests.exceptions import Timeout as CurlTimeout
except Exception:
    # investing.py의 curl_cffi 로드 실패 시 requests 폴백과 같은 경계.
    CurlTimeout = RequestsTimeout


def _result(status, reason, **evidence):
    return {"status": status, "reason": reason, **evidence}


def _execution(error):
    if isinstance(error, (TimeoutError, RequestsTimeout, CurlTimeout)):
        return _result("timeout", "timeout", error_type=type(error).__name__)
    if isinstance(error, (asyncio.CancelledError, concurrent.futures.CancelledError)):
        return _result("cancelled", "cancelled", error_type=type(error).__name__)
    if error is not None:
        return _result("abnormal", "exception", error_type=type(error).__name__)
    return _result("normal", "returned")


def start_report(logger, pairs):
    try:
        report = InvestingReport(logger, pairs)
    except Exception:
        # 보고 초기화 실패도 수집·저장에 영향을 주면 안 된다.
        return None
    safely_report(report, "emit", "investing_round_started")
    return report


def safely_report(report, method, *args, **kwargs):
    """계측 계산/직렬화/로그 실패가 재시도나 원래 예외를 바꾸지 않는 경계."""
    if report is None:
        return
    try:
        getattr(report, method)(*args, **kwargs)
    except Exception:
        # 로거 자체가 실패할 수 있어 여기서 다시 로깅하지 않는다. 보고만 유실된다.
        try:
            report.telemetry_failed(method, *args, **kwargs)
        except Exception:
            pass


class InvestingReport:
    def __init__(self, logger, pairs):
        self.logger = logger
        self.round_id = uuid4().hex
        self.pairs = tuple(pairs)
        self.telemetry_errors = []
        self.session = "creating"
        self.execution = _result("running", "round_started")
        self.attempts = {
            attempt_id: {
                "attempt_id": attempt_id,
                "status": "not_reached",
                "reason": "not_started",
                "collection": {
                    pair: _result("not_attempted", "not_started") for pair in self.pairs
                },
                "writer": {"called": False, "input_pairs": [], "returned_count": None,
                           "error_type": None},
                "execution": _result("not_attempted", "not_started"),
            }
            for attempt_id in (1, 2)
        }

    def telemetry_failed(self, method, *args, **kwargs):
        self.telemetry_errors.append(method)
        if method not in ("start_attempt", "observation", "finish_attempt", "writer_started"):
            return
        attempt_id = args[0] if args else kwargs["attempt_id"]
        attempt = self.attempts[attempt_id]
        if method == "writer_started":
            if attempt["writer"]["called"] is False:
                attempt["writer"]["called"] = None
            return
        if method in ("start_attempt", "finish_attempt"):
            if attempt["status"] in ("not_reached", "attempted"):
                attempt.update(status="unknown", reason="telemetry_error")
            if attempt["execution"]["status"] in ("not_attempted", "running"):
                attempt["execution"] = _result("unknown", "telemetry_error")
        pairs = ((args[1] if len(args) > 1 else kwargs["pair"],)
                 if method == "observation" else self.pairs)
        for pair in pairs:
            # 부분 기록 후 실패해도 이미 확보한 valid/missing 증거는 보존한다.
            if attempt["collection"][pair]["status"] in ("not_attempted", "unknown"):
                attempt["collection"][pair] = _result("unknown", "telemetry_error")

    def session_state(self, state):
        self.session = state

    def start_attempt(self, attempt_id):
        attempt = self.attempts[attempt_id]
        attempt.update(status="attempted", reason="started")
        attempt["execution"] = _result("running", "attempt_started")
        attempt["collection"] = {
            pair: _result("unknown", "not_observed") for pair in self.pairs
        }

    def observation(self, attempt_id, pair, *, reason=None, text=None, rate=None):
        if reason == "parse_failed" and (not text or text.upper() in ("-", "N/A")):
            reason = "empty_or_placeholder"
        if reason is None:
            if not text or text.upper() in ("-", "N/A"):
                reason = "empty_or_placeholder"
            elif math.isnan(rate):
                reason = "nan_value"
            else:
                # 계약 v4: MIBANK 이름 그대로 공유. JPY ×100 이후 보고에만 적용.
                low, high = constants.MIBANK_RATE_RANGES[pair]
                if not low <= rate <= high:
                    reason = "out_of_range"
        evidence = {}
        if rate is not None:
            # JSON 표준을 지키되 writer에는 원래 float(NaN/±inf 포함)가 전달된다.
            evidence["normalized_rate"] = rate if math.isfinite(rate) else str(rate)
        self.attempts[attempt_id]["collection"][pair] = _result(
            "missing" if reason else "valid", reason or "validated", **evidence
        )

    def writer_started(self, attempt_id, rates):
        self.attempts[attempt_id]["writer"].update(called=True, input_pairs=list(rates))

    def writer_finished(self, attempt_id, *, count=None, error=None):
        self.attempts[attempt_id]["writer"].update(
            returned_count=count, error_type=type(error).__name__ if error else None
        )

    def finish_attempt(self, attempt_id, error=None, reason=None):
        attempt = self.attempts[attempt_id]
        attempt.update(status="failed" if error else "succeeded",
                       reason=reason or ("exception" if error else "routine_returned"))
        attempt["execution"] = _execution(error)
        if reason == "http_403":
            for pair, observed in attempt["collection"].items():
                if observed["status"] in ("not_attempted", "unknown"):
                    attempt["collection"][pair] = _result("missing", reason)
        if error is None and attempt_id == 1:
            self.attempts[2].update(status="unnecessary", reason="previous_attempt_succeeded")

    def cooldown(self):
        for attempt in self.attempts.values():
            attempt.update(status="policy_skipped", reason="cooldown")
            attempt["collection"] = {
                pair: _result("not_attempted", "cooldown") for pair in self.pairs
            }

    def _collection(self):
        collection = {}
        for pair in self.pairs:
            observations = [
                {**attempt["collection"][pair], "attempt_id": attempt["attempt_id"]}
                for attempt in self.attempts.values()
            ]
            # 뒤 시도의 timeout/실패가 앞 시도의 유효 관측이나 판정 증거를 지우지 않는다.
            for status in ("valid", "missing", "unknown", "not_attempted"):
                candidates = [item for item in observations if item["status"] == status]
                if candidates:
                    collection[pair] = candidates[-1]
                    break
        return collection

    def _writing(self):
        writing = {}
        for pair in self.pairs:
            submitted = [attempt["attempt_id"] for attempt in self.attempts.values()
                         if pair in attempt["writer"]["input_pairs"]]
            writing[pair] = _result(
                "unknown" if submitted else "not_attempted",
                "per_currency_write_unverified" if submitted else "not_submitted_to_writer",
                attempt_ids=submitted,
            )
            if "writer_started" in self.telemetry_errors and not submitted:
                writing[pair] = _result("unknown", "telemetry_error", attempt_ids=[])
        return writing

    def finish(self, error=None):
        self.execution = _execution(error)
        if error is None:
            attempted = [a for a in self.attempts.values()
                         if a["status"] in ("succeeded", "failed")]
            if attempted and attempted[-1]["execution"]["status"] in ("timeout", "cancelled"):
                self.execution = dict(attempted[-1]["execution"])
            elif attempted and attempted[-1]["status"] == "failed":
                self.execution["reason"] = "attempts_exhausted"
            elif self.attempts[1]["status"] == "policy_skipped":
                self.execution["reason"] = "cooldown"
        self.execution["exception_propagated"] = error is not None
        self.emit("investing_round_finished")

    def _can_compact(self, event, attempt_id):
        """첫 시도의 완전한 FX 증거만 축약. 재시도/계측 유실은 상세 보존."""
        first, second = self.attempts[1], self.attempts[2]
        writer = first["writer"]
        if (not self.pairs or self.telemetry_errors
                or second["status"] not in ("not_reached", "unnecessary")
                or not all(item["status"] == "valid" for item in first["collection"].values())
                or writer["called"] is not True or writer["error_type"] is not None
                or type(writer["returned_count"]) is not int
                or writer["returned_count"] < 0
                or set(writer["input_pairs"]) != set(self.pairs)):
            return False
        if event == "investing_fx_evidence":
            return (attempt_id == 1 and first["status"] == "attempted"
                    and first["execution"]["status"] == "running" and self.session == "open")
        return (event == "investing_round_finished" and first["status"] == "succeeded"
                and self.execution["status"] == "normal"
                and self.execution["reason"] == "returned" and self.session == "closed")

    def emit(self, event, attempt_id=None):
        payload = {
            "event": event,
            "schema_version": 2,
            "source": "investing",
            "round_id": self.round_id,
            "attempt_id": attempt_id,
        }
        if event == "investing_round_started":
            # 아직 수집 증거가 없다. 생성 전 전체 빈 스냅샷을 반복하지 않는다.
            payload["format"] = "lifecycle"
        else:
            payload.update(execution=self.execution, session=self.session,
                           final_db="not_checked")
            if self._can_compact(event, attempt_id):
                # all_valid는 FX 관측만 뜻한다. writer 0도 저장/정책 판정 불가.
                payload.update(
                    format="compact", outcome="all_valid", fx_attempt_id=1,
                    rates={pair: item["normalized_rate"]
                           for pair, item in self.attempts[1]["collection"].items()},
                    writer_returned_count=self.attempts[1]["writer"]["returned_count"],
                    writing="per_currency_write_unverified",
                )
            else:
                # 원본 collection은 시도에 한 번만. 회차 요약은 선택된 시도를 참조한다.
                # 실행 전/불필요 시도는 상태·사유만으로 복원 가능하다.
                payload.update(
                    format="detail",
                    collection_attempts={pair: item["attempt_id"]
                                         for pair, item in self._collection().items()},
                    attempts=[
                        {key: attempt[key] for key in ("attempt_id", "status", "reason")}
                        if (attempt["status"] in ("not_reached", "unnecessary")
                            and not self.telemetry_errors) else attempt
                        for attempt in self.attempts.values()
                    ],
                    writing=self._writing(), telemetry_errors=self.telemetry_errors,
                )
        self.logger.info(json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":")))
