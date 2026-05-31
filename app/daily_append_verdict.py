"""Daily append verdict 계약 (ADR-034 Phase 2d) — writer ↔ orchestrator 공유 단일 소스.

writer가 `--emit-daily-append-verdict` 시 stdout에 sentinel 1줄 출력, orchestrator가 파싱.

fail-closed 계약:
- exit 0 → sentinel 정확히 1개 필수 (없거나 2개 이상이면 FAIL)
- status ∈ {written, skipped, error}
- written → rows == 1 / skipped → rows == 0 + 허용 reason / error → FAIL
- exit nonzero → 항상 FAIL (sentinel은 선택적 진단 정보)

writer 내부 action → 외부 status 매핑: write→written / skip_ok→skipped / skip_error→error.
"""

# 표준 라이브러리
import json
from datetime import date
from typing import Optional

SENTINEL_PREFIX = "DAILY_APPEND_VERDICT_JSON="
VERDICT_VERSION = 1

# 허용 status / skipped reason (orchestrator fail-closed 검증용)
VALID_STATUSES = frozenset({"written", "skipped", "error"})
VALID_SKIPPED_REASONS = frozenset({"weekend_no_changes", "holiday_no_changes"})


def emit_verdict(
    source: str,
    asset: str,
    date_kst: date,
    status: str,
    reason: Optional[str],
    rows: int,
) -> None:
    """verdict sentinel 1줄을 stdout에 출력."""
    payload = {
        "version": VERDICT_VERSION,
        "source": source,
        "asset": asset,
        "date_kst": date_kst.isoformat(),
        "status": status,
        "reason": reason,
        "rows": rows,
    }
    print(SENTINEL_PREFIX + json.dumps(payload, ensure_ascii=False))


def extract_verdicts(stdout: str) -> list[object]:
    """stdout에서 sentinel JSON 전부 추출 (개수/schema 검증은 caller 책임).

    반환 타입 list[object]: json.loads는 dict 외(list/null/str/number)도 반환 가능.
    consumer가 isinstance(v, dict) 가드로 fail-closed 처리 (Codex Blocker 1).
    malformed JSON 라인은 ValueError로 전파 (fail-closed — caller가 FAIL 처리).
    """
    results: list[object] = []
    for line in stdout.splitlines():
        if line.startswith(SENTINEL_PREFIX):
            results.append(json.loads(line[len(SENTINEL_PREFIX):]))
    return results
