#!/usr/bin/env python3
"""
ADR-034 Phase 2d Step 5 — Daily append orchestrator (minimum viable, first PR Bithumb only).

목적:
  - 기존 backfill writer를 subprocess로 호출 (재사용, 신규 write logic 0)
  - source별 daily append (yesterday KST 1 row) 일괄 실행
  - dry-run mode: validation + write command preview only (subprocess 실행 X)
  - write mode: subprocess + capture + structured summary (PASS/FAIL/rows/exit code)

첫 PR scope:
  - Bithumb only (24h candle close, 적재 시각: 다음날 00:01 KST cron 권장)
  - Hana observed_eod (별 PR): bank_exchange_rates 내부 관측 DB read → hana_observed_eod 신 writer 필요
  - KRX close finalizer 통합 (별 PR): CF 15:45 KST 종가 직후 trigger

후속 PR scope 확장:
  - --source choices=["bithumb"] → ["bithumb", "hana", "krx", "all"]
  - production cron 등록 절차 (Bithumb 00:01 KST + Hana 00:01 + KRX close finalizer 직후)
  - retry/backoff/structured error/alert (운영 데이터 기반 단계적 강화)

사용법:
  # dry-run (default) — validation + write command preview only, subprocess 실행 X
  python scripts/daily_append_source_daily_rates.py
  python scripts/daily_append_source_daily_rates.py --date 2026-05-27

  # write mode — subprocess 실 실행 + structured summary
  python scripts/daily_append_source_daily_rates.py --write
  python scripts/daily_append_source_daily_rates.py --write --date 2026-05-27

  # production execution — --allow-production-write writer subprocess에 forward
  python scripts/daily_append_source_daily_rates.py --write --allow-production-write

주의:
  - default --date: yesterday KST (datetime.now(tz=KST).date() - 1)
  - subprocess timeout: 180초/source
  - writer 자체 production guard (dialect 검사) — --allow-production-write 없으면 non-SQLite 차단
  - dry-run preview는 string only (subprocess 실행 X) — operator가 production cron 진입 전 차이 확인 가능
"""

# 표준 라이브러리
import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# 로컬 애플리케이션 (sys.path 설정 후) — daily append verdict 공유 계약
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.daily_append_verdict import (  # noqa: E402
    VALID_SKIPPED_REASONS,
    VALID_STATUSES,
    VERDICT_VERSION,
    extract_verdicts,
)


KST = ZoneInfo("Asia/Seoul")
SUBPROCESS_TIMEOUT_SECONDS = 180
REPO_ROOT = Path(__file__).resolve().parent.parent

# 첫 PR scope — 향후 PR에서 hana / krx / all 확장
SUPPORTED_SOURCES = ["bithumb"]

# source-aware verdict 정책 (Codex Blocker — global allowlist 대신 source별 잠금)
# - bithumb: 24/7 source라 skipped 불가 (항상 candle 존재) → written only
# - hana: 영업일 calendar라 skipped(주말/공휴일) 허용
SOURCE_POLICY = {
    "bithumb": {"asset": "usdt-krw", "allowed_statuses": frozenset({"written"})},
    "hana": {"asset": "usd-krw", "allowed_statuses": frozenset({"written", "skipped"})},
}


# ─────────────────────────────────────────────────────────────
# argparse helpers
# ─────────────────────────────────────────────────────────────

def _date_arg(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"--date format은 YYYY-MM-DD (입력: {s!r})")


def yesterday_kst() -> date:
    """KST 기준 yesterday (default --date)."""
    return datetime.now(tz=KST).date() - timedelta(days=1)


# ─────────────────────────────────────────────────────────────
# Source별 command builder
# ─────────────────────────────────────────────────────────────

def build_bithumb_command(d: date, *, write: bool, allow_production: bool) -> list[str]:
    """기존 backfill_bithumb_source_daily_rates.py command 생성.

    write=False: validation only (--write 없음, fetch + 9 validation, DB write X)
    write=True: 실 DB 적재 (--write [--allow-production-write])
    """
    script_path = (Path(__file__).resolve().parent / "backfill_bithumb_source_daily_rates.py").resolve()
    cmd = [
        sys.executable,
        str(script_path),
        "--start-date", d.isoformat(),
        "--end-date", d.isoformat(),
    ]
    if write:
        cmd.append("--write")
        cmd.append("--emit-daily-append-verdict")  # orchestrator는 JSON verdict로 결과 판정
        if allow_production:
            cmd.append("--allow-production-write")
    return cmd


def build_command(source: str, d: date, *, write: bool, allow_production: bool) -> list[str]:
    """source별 command dispatch (첫 PR scope: bithumb only)."""
    if source == "bithumb":
        return build_bithumb_command(d, write=write, allow_production=allow_production)
    raise ValueError(f"미지원 source: {source} (첫 PR scope: bithumb only)")


# ─────────────────────────────────────────────────────────────
# Subprocess execution + JSON verdict 판정 (fail-closed)
# ─────────────────────────────────────────────────────────────

def run_source(source: str, d: date, *, allow_production: bool) -> tuple[int, str, str]:
    """source별 writer subprocess 실행 (cwd=REPO_ROOT, robustness).

    Returns: (returncode, stdout_full, stdout_tail_30_lines + stderr_tail_on_fail)
    """
    cmd = build_command(source, d, write=True, allow_production=allow_production)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            cwd=str(REPO_ROOT),  # production cron robustness
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"[TIMEOUT] {SUBPROCESS_TIMEOUT_SECONDS}s 초과 (source={source})"

    stdout_lines = result.stdout.splitlines()
    tail = "\n".join(stdout_lines[-30:])
    if result.returncode != 0 and result.stderr:
        tail += "\n--- stderr (tail) ---\n" + "\n".join(result.stderr.splitlines()[-10:])
    return result.returncode, result.stdout, tail


def _is_strict_int(x) -> bool:
    """bool 제외 정수 (Python에서 True==1, type(True) is bool이라 엄격 검사)."""
    return type(x) is int


def _validate_verdict_schema(v: dict, source: str, expected_date: date) -> list[str]:
    """verdict JSON schema + source-aware 정책 검증.

    검증: version / source / asset(source별) / date_kst / status(source별 허용).
    """
    policy = SOURCE_POLICY.get(source)
    if policy is None:
        return [f"미지원 source={source!r} (SOURCE_POLICY 없음)"]

    issues = []
    if v.get("version") != VERDICT_VERSION:
        issues.append(f"version={v.get('version')!r} != {VERDICT_VERSION}")
    if v.get("source") != source:
        issues.append(f"source={v.get('source')!r} != {source!r}")
    if v.get("asset") != policy["asset"]:  # Codex Blocker 2: asset 검증
        issues.append(f"asset={v.get('asset')!r} != {policy['asset']!r}")
    if v.get("date_kst") != expected_date.isoformat():
        issues.append(f"date_kst={v.get('date_kst')!r} != {expected_date.isoformat()!r}")
    status = v.get("status")
    if status not in VALID_STATUSES:
        issues.append(f"status={status!r} invalid")
    elif status not in policy["allowed_statuses"]:  # Codex Blocker 3: source-aware
        issues.append(
            f"status={status!r}는 source={source}에 비허용 (허용: {sorted(policy['allowed_statuses'])})"
        )
    return issues


def evaluate_source_result(
    source: str, expected_date: date, returncode: int, stdout: str,
) -> tuple[str, str]:
    """fail-closed verdict 판정.

    - exit nonzero → 항상 FAIL (sentinel은 선택적 진단)
    - exit 0 → sentinel 정확히 1개 + JSON object + source-aware schema + status×rows 검증
      written → rows==1(int) + reason None / skipped → rows==0(int) + 허용 reason / error → FAIL

    Returns: (status: "PASS"|"FAIL", detail).
    """
    try:
        verdicts = extract_verdicts(stdout)
    except (ValueError, json.JSONDecodeError):
        return "FAIL", "malformed verdict JSON (sentinel parse 실패)"

    if returncode != 0:
        # nonzero exit → 항상 FAIL. sentinel이 dict면 진단 정보만 추가 (Non-blocker).
        diag = ""
        if len(verdicts) == 1 and isinstance(verdicts[0], dict):
            v = verdicts[0]
            diag = f" verdict.status={v.get('status')!r} reason={v.get('reason')!r}"
        return "FAIL", f"exit={returncode}{diag} (sentinel={len(verdicts)})"

    # exit 0 → sentinel 정확히 1개 필수
    if len(verdicts) != 1:
        return "FAIL", f"exit 0이나 sentinel {len(verdicts)}개 (정확히 1개 필수)"

    v = verdicts[0]
    if not isinstance(v, dict):  # Codex Blocker 1: non-object JSON (list/null/str) → crash 방지
        return "FAIL", f"verdict가 JSON object 아님 (type={type(v).__name__})"

    schema_issues = _validate_verdict_schema(v, source, expected_date)
    if schema_issues:
        return "FAIL", "schema: " + "; ".join(schema_issues)

    status = v["status"]
    rows = v.get("rows")
    if not _is_strict_int(rows):  # Codex Blocker 4: bool/str rows 거부 (True==1 회피)
        return "FAIL", f"rows type 부적합 ({type(rows).__name__}, int 필요)"

    if status == "written":
        if rows != 1:
            return "FAIL", f"written이나 rows={rows} (1 기대 — freshness 위반)"
        if v.get("reason") is not None:  # Non-blocker: written은 reason None
            return "FAIL", f"written이나 reason={v.get('reason')!r} (None 기대)"
        return "PASS", "written rows=1"
    if status == "skipped":
        if rows != 0:
            return "FAIL", f"skipped이나 rows={rows} (0 기대)"
        if v.get("reason") not in VALID_SKIPPED_REASONS:
            return "FAIL", f"skipped이나 reason={v.get('reason')!r} 비허용"
        return "PASS", f"skipped ({v.get('reason')})"
    # status == "error" — allowed_statuses에서 이미 걸러지나 방어
    return "FAIL", f"error verdict (reason={v.get('reason')!r})"


# ─────────────────────────────────────────────────────────────
# Dry-run preview (subprocess 실행 X)
# ─────────────────────────────────────────────────────────────

def print_dry_run_preview(source: str, d: date) -> None:
    """dry-run mode: validation + write command preview (operator UX 측면).

    string only — subprocess 실행 X. operator가 production cron 진입 전 차이 확인.
    """
    val_cmd = build_command(source, d, write=False, allow_production=False)
    write_cmd = build_command(source, d, write=True, allow_production=False)
    write_cmd_prod = build_command(source, d, write=True, allow_production=True)

    print(f"--- {source} ---")
    print(f"[DRY-RUN] writer full dry-run command (Bithumb writer 전체 candle fetch + 9 validation, DB write 0;")
    print(f"          주의: writer dry-run은 --start-date/--end-date를 target-date 한정으로 사용하지 않음):")
    print(f"  {' '.join(val_cmd)}")
    print()
    print(f"[DRY-RUN] write command preview (local SQLite, --allow-production-write 없음):")
    print(f"  {' '.join(write_cmd)}")
    print()
    print(f"[DRY-RUN] write command preview (production, --allow-production-write 명시):")
    print(f"  {' '.join(write_cmd_prod)}")
    print()


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "ADR-034 Phase 2d Step 5 — Daily append orchestrator (minimum viable, "
            "first PR Bithumb only). 기존 backfill writer를 subprocess로 호출."
        )
    )
    parser.add_argument(
        "--source",
        choices=SUPPORTED_SOURCES,
        default="bithumb",
        help=(
            "적재 대상 source (첫 PR scope: bithumb only). "
            "Hana observed_eod / KRX close finalizer 통합은 별 PR에서 enum 확장."
        ),
    )
    parser.add_argument(
        "--date",
        type=_date_arg,
        default=None,
        help="YYYY-MM-DD (default: yesterday KST). 적재 대상 date_kst.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "[Write mode] subprocess로 writer 실 실행. default: dry-run "
            "(validation command + write command preview only, subprocess 실행 X)."
        ),
    )
    parser.add_argument(
        "--allow-production-write",
        action="store_true",
        help=(
            "writer subprocess에 --allow-production-write forward. production execution "
            "(non-SQLite DB) 시 필수. default off — local SQLite smoke만 허용 + writer 자체 "
            "production guard로 dialect 검사."
        ),
    )
    args = parser.parse_args()

    target_date = args.date or yesterday_kst()
    # 첫 PR은 단일 source. 향후 --source all 옵션 추가 시 list 분기.
    sources = [args.source]

    print(f"모드: {'WRITE (subprocess 실 실행)' if args.write else 'DRY-RUN (command preview only)'}")
    print(f"대상 date: {target_date.isoformat()} (KST)")
    print(f"sources: {sources}")
    if args.write:
        print(f"allow_production_write: {args.allow_production_write}")
        print(f"subprocess timeout: {SUBPROCESS_TIMEOUT_SECONDS}s / source")
    print()

    if not args.write:
        # dry-run mode: command preview only (subprocess 실행 X)
        for source in sources:
            print_dry_run_preview(source, target_date)
        print("[DRY-RUN 완료] subprocess 실행 안 됨. --write 명시 시 실 적재 진입.")
        return

    # write mode: subprocess 실행 + JSON verdict 기반 fail-closed 판정
    results: dict[str, dict] = {}
    for source in sources:
        print(f"--- {source} subprocess ---")
        rc, stdout, tail = run_source(source, target_date, allow_production=args.allow_production_write)
        verdict_status, detail = evaluate_source_result(source, target_date, rc, stdout)
        results[source] = {"exit": rc, "status": verdict_status, "detail": detail, "tail": tail}
        print(tail)
        print()

    # structured summary (fail-closed verdict 판정)
    print("=" * 60)
    print(f"[Daily Append Summary] date={target_date.isoformat()} mode=write")
    print("=" * 60)
    any_fail = False
    for source, r in results.items():
        if r["status"] != "PASS":
            any_fail = True
        print(f"  {source}: {r['status']} (exit={r['exit']}, {r['detail']})")
    print()
    if any_fail:
        print("[FAIL] 일부 source 실패 — 재실행 또는 rollback anchor 확인 필요")
        sys.exit(1)
    print("[PASS] 모든 source 성공. production cron 활성화 가능 (별 GO).")


if __name__ == "__main__":
    main()
