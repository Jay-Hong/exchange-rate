"""bs·citi 보고 전용 증거 (R1a). 저장·재시도·crawler_stats 판단에 사용하지 않는다.

SOURCE_HEALTH_PLAN §7.2 값 체계(`valid`=유효관측 / `missing`=누락 / `unknown`=확인 불가 /
`not_attempted`=미시도)를 그대로 쓴다. ⛔ 별도 `invalid` 상태를 두지 않는다 — 확인된 위반은
`missing` 의 사유 `validation_rejected`, 값 미확보를 확인한 것은 `no_value` 다.

판정 계약 `bank_v2_evidence/1`: 통화·필드·단위 근거를 **그 회차 응답에서** 확인해야 `valid` 다.
R1a 는 공식 경로의 근거 **후보 텍스트만** 남기고 판정은 `unknown` 으로 둔다(라벨 표기가 fixture 로
확인되기 전이다). MIBANK 내부와 writer 가드는 아직 계측하지 않는다(`not_instrumented`) — 덜 구현된
상태가 정책 생략이나 값 미확보로 둔갑하지 않게 한다.

⛔ 관측은 운영 판단을 대체하지 않는다. 루틴이 실제 추출에 쓴 요소·매칭 결과만 받고, 선택자를 다시
계산하지 않는다. 추가 DOM 탐색·문자열 변환은 `safely_report` 경계 안에서만 한다.
"""

import json
import math
from uuid import uuid4

# timeout·cancelled·abnormal 분류와 "관측 실패가 수집을 바꾸지 않는" 경계는 Investing 보고와 같은 규칙이다.
from app.crawlers.investing_report import _execution as _classify_execution
from app.crawlers.investing_report import safely_report

SCHEMA_VERSION = 1
VALIDITY_CONTRACT = "bank_v2_evidence/1"

OFFICIAL_PRIMARY = "official_primary"
OFFICIAL_SECONDARY = "official_secondary"
MIBANK = "mibank"
# R1a 에서 내부를 계측하는 경로. MIBANK 는 R1b 에서 더한다.
_INSTRUMENTED_PATHS = (OFFICIAL_PRIMARY, OFFICIAL_SECONDARY)

# 근거 후보 조각의 상한(C3 초안값). 잘리면 표시하고, 잘린 정보로 충돌·완결을 확정하지 않는다.
SNIPPET_MAX_CHARS = 160
SNIPPET_MAX_BYTES = 640
MAX_OBSERVATIONS_PER_PAIR = 8

# 회차 요약은 시도 간 증거를 이 순서로 고른다. ⚠️ Investing 회차 집계(valid → missing → unknown)와 다르다:
# 다른 시도의 유효 관측 확보 여부가 미확정(`unknown`)이면 회차 전체를 `missing`("필요한 유효 관측을 확보하지
# 못함", §7.2)으로 확정할 수 없다. Investing 의 같은 문제는 SOURCE_HEALTH_PLAN (D9) 에서 따로 본다.
_SUMMARY_ORDER = ("valid", "unknown", "missing", "not_attempted")


def _result(status, reason, **evidence):
    return {"status": status, "reason": reason, **evidence}


def _snippet(text):
    if text is None:
        return None
    normalized = " ".join(str(text).split())
    clipped = normalized[:SNIPPET_MAX_CHARS]
    encoded = clipped.encode("utf-8")
    if len(encoded) > SNIPPET_MAX_BYTES:
        clipped = encoded[:SNIPPET_MAX_BYTES].decode("utf-8", "ignore")
    return {"text": clipped, "truncated": clipped != normalized,
            "original_length": len(normalized)}


def _rate_evidence(rate):
    # JSON 표준(allow_nan=False)을 지키되 비유한 값이라는 사실은 보존한다.
    return rate if math.isfinite(rate) else str(rate)


def _label_candidates(element, item):
    """근거 **후보**만 남긴다 — 판정하지 않는다(라벨 표기는 fixture 로 확인한 뒤에 쓴다)."""
    labels = {}
    if item is not None:
        labels["item_text"] = _snippet(item.get_text(" ", strip=True))
    if element is not None:
        if element.name not in ("td", "th"):
            # 표 칸이 아닌 값 요소(citi 1차의 span)는 바로 위 묶음의 텍스트가 라벨 후보다.
            container = element.parent
            if container is not None and container is not item:
                labels["value_container_text"] = _snippet(container.get_text(" ", strip=True))
        row = element.find_parent("tr")
        if row is not None:
            labels["row_text"] = _snippet(row.get_text(" ", strip=True))
            cells = row.find_all(["td", "th"], recursive=False)
            owner = element if element.name in ("td", "th") else element.find_parent(["td", "th"])
            labels["cell_index"] = next(
                (index for index, cell in enumerate(cells) if cell is owner), None)
            table = row.find_parent("table")
            header = table.select_one("thead tr") if table is not None else None
            if header is not None:
                labels["header_text"] = _snippet(header.get_text(" ", strip=True))
                labels["header_has_span"] = any(
                    cell.get("colspan") or cell.get("rowspan")
                    for cell in header.find_all(["td", "th"], recursive=False))
    return labels


def start_report(logger, bank, pairs, paths):
    try:
        report = BankReport(logger, bank, pairs, paths)
    except Exception:
        # 보고 초기화 실패도 수집·저장에 영향을 주면 안 된다.
        return None
    safely_report(report, "emit", "bank_round_started")
    return report


def record_writer_call(observer, rates, call):
    """`call()` 을 그대로 부르고, 호출 지점에서 writer 호출·반환·예외를 기록한다.

    인자·반환값·예외는 바꾸지 않는다. 관측자가 없으면 기록 없이 부르기만 한다.
    """
    if observer is not None:
        observer.writer_started(rates)
    try:
        count = call()
    except BaseException as error:
        if observer is not None:
            observer.writer_finished(error=error)
        raise
    if observer is not None:
        observer.writer_finished(count=count)
    return count


class PathObserver:
    """한 경로의 관측자. 모든 호출이 `safely_report` 를 거친다 — 실패해도 수집은 그대로다."""

    __slots__ = ("report", "path")

    def __init__(self, report, path):
        self.report = report
        self.path = path

    def start(self):
        safely_report(self.report, "start_attempt", self.path)

    def finish(self, error=None):
        safely_report(self.report, "finish_attempt", self.path, error=error)

    def observed(self, pair, **evidence):
        safely_report(self.report, "observed", self.path, pair, **evidence)

    def missed(self, pair, reason, **evidence):
        safely_report(self.report, "missed", self.path, pair, reason, **evidence)

    def item_missed(self, item_key, reason, **evidence):
        safely_report(self.report, "item_missed", self.path, item_key, reason, **evidence)

    def loop_completed(self):
        safely_report(self.report, "loop_completed", self.path)

    def writer_started(self, rates):
        safely_report(self.report, "writer_started", self.path, rates)

    def writer_finished(self, count=None, error=None):
        safely_report(self.report, "writer_finished", self.path, count=count, error=error)


class BankReport:
    def __init__(self, logger, bank, pairs, paths):
        self.logger = logger
        self.bank = bank
        self.round_id = uuid4().hex
        self.pairs = tuple(pairs)
        self.telemetry_errors = []
        self.truncated = False
        self.execution = _result("running", "round_started")
        self.writer_calls = []
        self._sequence = 0
        self.attempts = {
            path: {
                "attempt_id": attempt_id,
                "path": path,
                "status": "not_reached",
                "reason": "not_started",
                "instrumented": path in _INSTRUMENTED_PATHS,
                "loop_completed": False,
                "telemetry_incomplete": False,
                "observations": [],
                "misses": [],
                "writer_call_ids": [],
                "execution": _result("not_attempted", "not_started"),
            }
            for attempt_id, path in enumerate(paths, start=1)
        }

    # ── 관측 ──────────────────────────────────────────────────────────────

    def telemetry_failed(self, method, *args, **kwargs):
        self.telemetry_errors.append(method)
        path = args[0] if args else kwargs.get("path")
        attempt = self.attempts.get(path)
        if attempt is not None:
            # 빠진 관측이 있을 수 있다 — 그 시도의 판정을 "확인 불가" 쪽으로만 내린다.
            attempt["telemetry_incomplete"] = True
            if method == "start_attempt" and attempt["status"] == "not_reached":
                # 시작 기록이 빠졌다고 "시도하지 않음" 으로 둔갑시키지 않는다.
                attempt.update(status="unknown", reason="telemetry_error")

    def _next_sequence(self):
        self._sequence += 1
        return self._sequence

    def start_attempt(self, path):
        attempt = self.attempts[path]
        attempt.update(status="attempted", reason="started")
        attempt["execution"] = _result("running", "attempt_started")

    def finish_attempt(self, path, error=None):
        attempt = self.attempts[path]
        attempt.update(status="failed" if error is not None else "succeeded",
                       reason="exception" if error is not None else "routine_returned")
        attempt["execution"] = _classify_execution(error)
        if error is not None:
            attempt["error_type"] = type(error).__name__

    def policy_skipped(self, path, reason):
        self.attempts[path].update(status="policy_skipped", reason=reason)

    def observed(self, path, pair, *, rate_text, rate, selector, element=None,
                 item_key=None, item=None, matched_code=None):
        attempt = self.attempts[path]
        if sum(1 for o in attempt["observations"] if o["pair"] == pair) >= MAX_OBSERVATIONS_PER_PAIR:
            # 상한을 넘은 관측은 버리되 사실을 남긴다 — 충돌·완결을 확정하지 못하게 된다.
            self.truncated = True
            attempt["observations_truncated"] = True
            return
        entry = {
            "sequence": self._next_sequence(),
            "pair": pair,
            "rate_text": _snippet(rate_text),
            "rate": _rate_evidence(rate),
            "finite": math.isfinite(rate),
            "selector": selector,
        }
        if item_key is not None:
            entry["item_key"] = item_key
        if matched_code is not None:
            entry["matched_code"] = matched_code
        # 핵심 추출 사실을 먼저 보존한다 — 라벨 후보 수집이 실패해도 관측 자체는 남는다.
        attempt["observations"].append(entry)
        try:
            entry["label_candidates"] = _label_candidates(element, item)
        except Exception as error:
            entry["label_candidates"] = None
            entry["label_candidates_error"] = type(error).__name__

    def missed(self, path, pair, reason, *, selector=None, rate_text=None, item_key=None):
        entry = {"sequence": self._next_sequence(), "pair": pair, "reason": reason,
                 "selector": selector}
        if rate_text is not None:
            entry["rate_text"] = _snippet(rate_text)
        if item_key is not None:
            entry["item_key"] = item_key
        self.attempts[path]["misses"].append(entry)

    def item_missed(self, path, item_key, reason, *, selector=None):
        self.attempts[path]["misses"].append(
            {"sequence": self._next_sequence(), "pair": None, "item_key": item_key,
             "reason": reason, "selector": selector})

    def loop_completed(self, path):
        self.attempts[path]["loop_completed"] = True

    def writer_started(self, path, rates):
        call_id = f"{self.round_id}:{len(self.writer_calls) + 1}"
        self.writer_calls.append({
            "writer_call_id": call_id,
            "path": path,
            "input_pairs": sorted(rates),
            "call_state": "called",
            # R1c 전에는 가드를 계측하지 않는다 — 결정 불명(unknown_telemetry)과도 구분한다.
            "guard_decision": "not_instrumented",
            "termination": "unknown",
            "returned_count": None,
            "error_type": None,
        })
        self.attempts[path]["writer_call_ids"].append(call_id)

    def writer_finished(self, path, count=None, error=None):
        calls = [c for c in self.writer_calls if c["path"] == path]
        if not calls:
            return
        call = calls[-1]
        call["termination"] = "raised" if error is not None else "returned"
        call["returned_count"] = count
        call["error_type"] = type(error).__name__ if error is not None else None

    # ── 판정 ──────────────────────────────────────────────────────────────

    def _conflicts(self, attempt):
        """같은 원천의 복수 귀속(한 항목 → 여러 통화)과 같은 통화의 복수 관측을 충돌로 본다.

        값이 같다는 사실은 귀속 오류의 증거가 아니다 — 원천(항목·관측 횟수)만 본다.
        """
        conflicted = set()
        by_item = {}
        for entry in attempt["observations"] + attempt["misses"]:
            if entry.get("item_key") is not None and entry.get("pair") is not None:
                by_item.setdefault(entry["item_key"], set()).add(entry["pair"])
        for pairs in by_item.values():
            if len(pairs) > 1:
                conflicted |= pairs
        counts = {}
        for entry in attempt["observations"]:
            counts[entry["pair"]] = counts.get(entry["pair"], 0) + 1
        conflicted |= {pair for pair, count in counts.items() if count > 1}
        return conflicted

    def _judge(self, attempt, pair, conflicted):
        """후보 내부 판정. 순서: 실행 안 됨 → 미계측 → 확인된 위반(비유한·충돌) → 계측 불완전·잘림
        → 관측(근거 미확인) → miss(no_value) → 루프 완료(not_matched) → 경로 실패(path_failed) → 미관측."""
        if attempt["status"] in ("not_reached", "policy_skipped", "unnecessary"):
            # 실행되지 않은 경로다 — 낮은 품질이 아니라 "시도하지 않았다" 는 별도 사실.
            return _result("not_attempted", attempt["reason"])
        if not attempt["instrumented"]:
            return _result("unknown", "not_instrumented")
        observations = [o for o in attempt["observations"] if o["pair"] == pair]
        references = [o["sequence"] for o in observations]
        if any(not o["finite"] for o in observations):
            return _result("missing", "validation_rejected", detail="non_finite",
                           observation_sequences=references)
        if pair in conflicted:
            return _result("missing", "validation_rejected", detail="attribution_conflict",
                           observation_sequences=references)
        # 확인된 위반은 위에서 이미 확정했다. 여기부터는 관측이 빠졌을 수 있으면 확정하지 않는다 —
        # 뒤 후보의 관측이 유실됐다면 앞 후보의 miss 로 "미확보" 를 단정하면 안 된다.
        if attempt["telemetry_incomplete"] or attempt.get("observations_truncated"):
            return _result("unknown", "evidence_incomplete", observation_sequences=references)
        if observations:
            # R1a: 통화·필드·단위 근거는 후보 텍스트만 있다 — 파싱 성공으로 올리지 않는다.
            return _result("unknown", "v2_evidence_unconfirmed", observation_sequences=references)
        misses = [m for m in attempt["misses"] if m["pair"] == pair]
        if misses:
            return _result("missing", "no_value", detail=misses[-1]["reason"],
                           miss_sequences=[m["sequence"] for m in misses])
        if attempt["loop_completed"]:
            # 루프를 끝까지 돌았는데 이 통화를 얻지 못했다(citi 1차의 매칭 없음 등).
            return _result("missing", "no_value", detail="not_matched")
        if attempt["status"] == "failed":
            # 경로가 예외로 중단됐다 — 이 경로에서 값을 얻지 못한 것은 확정이다.
            return _result("missing", "no_value", detail="path_failed",
                           error_type=attempt.get("error_type"))
        return _result("unknown", "unobserved")

    def _attempt_payloads(self):
        payloads = []
        for attempt in self.attempts.values():
            conflicted = self._conflicts(attempt)
            collection = {pair: self._judge(attempt, pair, conflicted) for pair in self.pairs}
            payloads.append({**{k: v for k, v in attempt.items()}, "collection": collection})
        return payloads

    def _summary(self, attempt_payloads):
        collection = {}
        for pair in self.pairs:
            candidates = [(payload, payload["collection"][pair]) for payload in attempt_payloads]
            for status in _SUMMARY_ORDER:
                chosen = [c for c in candidates if c[1]["status"] == status]
                if chosen:
                    payload, judged = chosen[-1]
                    collection[pair] = {**judged, "path": payload["path"],
                                        "attempt_id": payload["attempt_id"]}
                    break
        writing = {}
        writer_failed = "writer_started" in self.telemetry_errors
        for pair in self.pairs:
            call_ids = [c["writer_call_id"] for c in self.writer_calls if pair in c["input_pairs"]]
            if call_ids:
                # 가드·통화별 쓰기는 계측 전이다 — 호출됐다는 것만으로 쓰기를 확정하지 않는다.
                writing[pair] = _result("unknown", "per_currency_write_unverified",
                                        writer_call_ids=call_ids)
            elif writer_failed:
                writing[pair] = _result("unknown", "telemetry_error", writer_call_ids=[])
            else:
                writing[pair] = _result("not_attempted", "not_submitted_to_writer",
                                        writer_call_ids=[])
        return {"collection": collection, "writing": writing, "final_db": "not_checked"}

    def finish(self, error=None):
        self.execution = _classify_execution(error)
        self.execution["exception_propagated"] = error is not None
        for attempt in self.attempts.values():
            if attempt["status"] == "attempted":
                # 경로 종료 원인은 경로 경계(`except BaseException` 기록 후 재전파)에서 결속한다. 여기까지
                # `attempted` 로 남았다면 그 결속을 잃은 것이다 — 회차 예외를 그 시도에 추정해 붙이지 않는다
                # (종료 기록만 유실된, 이미 끝난 경로일 수 있다).
                attempt.update(status="unknown", reason="attempt_end_unrecorded")
                attempt["execution"] = _result("unknown", "attempt_end_unrecorded")
        succeeded = False
        for attempt in self.attempts.values():
            if attempt["status"] == "not_reached" and succeeded:
                attempt.update(status="unnecessary", reason="previous_attempt_succeeded")
            if attempt["status"] == "succeeded":
                succeeded = True
        self.emit("bank_round_finished")

    def emit(self, event):
        payload = {
            "event": event,
            "schema_version": SCHEMA_VERSION,
            "validity_contract": VALIDITY_CONTRACT,
            "source": self.bank,
            "round_id": self.round_id,
        }
        if event == "bank_round_started":
            payload["format"] = "lifecycle"
        else:
            attempts = self._attempt_payloads()
            payload.update(
                format="detail",
                attempts=attempts,
                summary=self._summary(attempts),
                writer_calls=self.writer_calls,
                execution=self.execution,
                truncated=self.truncated,
                telemetry_errors=self.telemetry_errors,
            )
        self.logger.info(json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                    separators=(",", ":")))
