"""D7 Tier 1 첫 슬라이스: 축별 정규화와 단일 회차 기여만 계산한다.

운영 무접촉: 생산자나 다른 app 모듈을 import하지 않으며 운영 경로에 배선하지 않는다.
호출 등록·dedup·시간 버킷·어댑터·배선은 이 슬라이스의 범위 밖이다.
같은 보고 두 번 = 기여 두 번(누적은 다음 슬라이스). 상태나 식별 ledger를 보관하지 않는다.
"""

from __future__ import annotations

from collections import Counter


PAIRS = ("usd-krw", "jpy-krw", "eur-krw")
# 생산자와의 일치는 계약 시험이 검증한다. 생산자 의존성은 만들지 않는다.
REGISTRY = {
    "investing": (3, "investing_range_checked/2"),
    "bs": (1, "bank_v2_evidence/1"),
    "citi": (1, "bank_v2_evidence/1"),
}

_COLLECTION_BUCKETS = {
    "valid": "V",
    "missing": "M",
    "unknown": "U",
    "not_attempted": "N",
}
_INVESTING_WRITING = frozenset({"unknown", "not_attempted"})
_BANK_WRITING = frozenset({
    "performed", "no_change_needed", "policy_blocked", "not_attempted", "unknown",
})


def _normalize_axis(
    item: object, allowed: frozenset[str],
) -> tuple[dict[str, str], bool, bool]:
    """유효한 상태는 보존하고, 이 축의 상태·사유만 정규화한다."""
    status = item.get("status") if isinstance(item, dict) else None
    if not isinstance(status, str) or status not in allowed:
        return {"status": "unknown", "reason": "report_malformed"}, True, False
    reason = item.get("reason")
    unrecorded = not isinstance(reason, str) or not reason
    if unrecorded:
        reason = "reason_unrecorded"
    return {"status": status, "reason": reason}, False, unrecorded


def _sorted_unexpected(keys: set[object]) -> list[str]:
    """보고 결함은 진단으로 남긴다: 키 종류가 섞여도 정렬 예외 없이 문자열 목록을 만든다."""
    text_keys = sorted(key for key in keys if isinstance(key, str))
    other_keys = sorted(repr(key) for key in keys if not isinstance(key, str))
    return text_keys + other_keys


def normalize_round_axes(
    source: str,
    report_schema: int,
    validity_contract: str,
    selected_summary: dict,
    telemetry_error_present: bool,
) -> dict:
    """선택된 요약을 독립된 세 축과 한 회차의 통화별 기여로 반환한다."""
    if not isinstance(source, str):
        raise TypeError("source must be str")
    if not isinstance(report_schema, int) or isinstance(report_schema, bool):
        raise TypeError("report_schema must be int, not bool")
    if not isinstance(validity_contract, str):
        raise TypeError("validity_contract must be str")
    if not isinstance(selected_summary, dict):
        raise TypeError("selected_summary must be dict")
    if not isinstance(telemetry_error_present, bool):
        raise TypeError("telemetry_error_present must be bool")

    if source not in REGISTRY:
        return {"accepted": False, "reason": "unsupported_source"}
    if (report_schema, validity_contract) != REGISTRY[source]:
        return {"accepted": False, "reason": "unregistered_contract"}

    allowed = {
        "collection": frozenset(_COLLECTION_BUCKETS),
        "writing": _INVESTING_WRITING if source == "investing" else _BANK_WRITING,
    }
    axis_maps = {}
    unexpected_pairs = set()
    for axis in allowed:
        value = selected_summary.get(axis)
        axis_maps[axis] = value if isinstance(value, dict) else {}
        unexpected_pairs.update(key for key in axis_maps[axis] if key not in PAIRS)

    final_db = selected_summary.get("final_db")
    final_db_reason = (
        "not_checked"
        if isinstance(final_db, str) and final_db == "not_checked"
        else "report_malformed"
    )
    pairs = {}
    malformed = []
    reason_unrecorded = []
    collection_counts = dict.fromkeys(_COLLECTION_BUCKETS.values(), 0)
    unknown_reasons = Counter()
    not_attempted_reasons = Counter()
    writing_counts = Counter()
    final_db_counts = Counter()

    for pair in PAIRS:
        axes = {}
        for axis, statuses in allowed.items():
            item, is_malformed, is_unrecorded = _normalize_axis(
                axis_maps[axis].get(pair), statuses,
            )
            axes[axis] = item
            if is_malformed:
                malformed.append([pair, axis])
            elif is_unrecorded:
                reason_unrecorded.append([pair, axis])
        axes["final_db"] = {"status": "unknown", "reason": final_db_reason}
        if final_db_reason == "report_malformed":
            malformed.append([pair, "final_db"])
        pairs[pair] = axes

        collection = axes["collection"]
        status, reason = collection["status"], collection["reason"]
        collection_counts[_COLLECTION_BUCKETS[status]] += 1
        if status == "unknown":
            unknown_reasons[reason] += 1
        elif status == "not_attempted":
            not_attempted_reasons[reason] += 1
        writing_counts[axes["writing"]["status"]] += 1
        final_db_counts[axes["final_db"]["status"]] += 1

    return {
        "accepted": True,
        "source": source,
        "report_schema": report_schema,
        "validity_contract": validity_contract,
        "pairs": pairs,
        "contribution": {
            "collection": collection_counts,
            "collection_unknown_reasons": dict(unknown_reasons),
            "collection_not_attempted_reasons": dict(not_attempted_reasons),
            "writing": dict(writing_counts),
            "final_db": dict(final_db_counts),
        },
        "diagnostics": {
            "malformed_axis_items": len(malformed),
            "malformed": malformed,
            "reason_unrecorded": reason_unrecorded,
            "telemetry_error": telemetry_error_present,
            "unexpected_pairs": _sorted_unexpected(unexpected_pairs),
        },
    }
