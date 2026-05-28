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
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
SUBPROCESS_TIMEOUT_SECONDS = 180
REPO_ROOT = Path(__file__).resolve().parent.parent

# 첫 PR scope — 향후 PR에서 hana / krx / all 확장
SUPPORTED_SOURCES = ["bithumb"]

# Source별 daily append expected row count (freshness 보장 검증)
# Codex Round 1 Blocker: row count mismatch는 returncode 0이라도 FAIL 처리해야 silent failure 차단
EXPECTED_ROWS_PER_SOURCE = {
    "bithumb": 1,  # 24h candle yesterday 1 row
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
        if allow_production:
            cmd.append("--allow-production-write")
    return cmd


def build_command(source: str, d: date, *, write: bool, allow_production: bool) -> list[str]:
    """source별 command dispatch (첫 PR scope: bithumb only)."""
    if source == "bithumb":
        return build_bithumb_command(d, write=write, allow_production=allow_production)
    raise ValueError(f"미지원 source: {source} (첫 PR scope: bithumb only)")


# ─────────────────────────────────────────────────────────────
# Subprocess execution + stdout parse
# ─────────────────────────────────────────────────────────────

_ROW_COUNT_RE = re.compile(r"(\d+) rows committed")


def parse_row_count(stdout: str) -> Optional[int]:
    """stdout에서 'N rows committed' regex parse. Bithumb writer 출력 패턴 의존.

    Codex Round 1 Blocker: regex 매칭 실패 시 None 반환 (0과 명확 구분 — silent failure 차단).
    """
    m = _ROW_COUNT_RE.search(stdout)
    return int(m.group(1)) if m else None


def run_source(source: str, d: date, *, allow_production: bool) -> tuple[int, Optional[int], str]:
    """source별 writer subprocess 실행 (cwd=REPO_ROOT, robustness).

    Returns: (returncode, row_count_or_None, stdout_tail_30_lines + stderr_tail_on_fail)
    """
    cmd = build_command(source, d, write=True, allow_production=allow_production)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            cwd=str(REPO_ROOT),  # Codex Round 1 Non-blocker 3: production cron robustness
        )
    except subprocess.TimeoutExpired:
        return 124, None, f"[TIMEOUT] {SUBPROCESS_TIMEOUT_SECONDS}s 초과 (source={source})"

    row_count = parse_row_count(result.stdout)
    stdout_lines = result.stdout.splitlines()
    tail = "\n".join(stdout_lines[-30:])
    if result.returncode != 0 and result.stderr:
        tail += "\n--- stderr (tail) ---\n" + "\n".join(result.stderr.splitlines()[-10:])
    return result.returncode, row_count, tail


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

    # write mode: subprocess 실행 + structured summary
    results: dict[str, dict] = {}
    for source in sources:
        print(f"--- {source} subprocess ---")
        rc, rows, tail = run_source(source, target_date, allow_production=args.allow_production_write)
        results[source] = {"exit": rc, "rows": rows, "tail": tail}
        print(tail)
        print()

    # structured summary
    # Codex Round 1 Blocker: returncode 0 AND rows == expected_rows 양방향 검증 (silent failure 차단)
    print("=" * 60)
    print(f"[Daily Append Summary] date={target_date.isoformat()} mode=write")
    print("=" * 60)
    any_fail = False
    for source, r in results.items():
        expected = EXPECTED_ROWS_PER_SOURCE[source]
        rows_actual = r["rows"]  # Optional[int] — None이면 stdout parse 실패
        rows_ok = rows_actual == expected
        exit_ok = r["exit"] == 0
        if exit_ok and rows_ok:
            status = "PASS"
        else:
            status = "FAIL"
            any_fail = True
        rows_display = "unparsed" if rows_actual is None else str(rows_actual)
        reason = ""
        if not exit_ok:
            reason = f", exit_fail"
        elif not rows_ok:
            if rows_actual is None:
                reason = f", row count parse 실패 (stdout regex mismatch — writer 출력 포맷 변경?)"
            else:
                reason = f", row count mismatch (silent failure — freshness 위반)"
        print(
            f"  {source}: {status} ({rows_display} rows committed, "
            f"expected={expected}, exit={r['exit']}{reason})"
        )
    print()
    if any_fail:
        print("[FAIL] 일부 source 실패 — 재실행 또는 rollback anchor 확인 필요")
        sys.exit(1)
    print("[PASS] 모든 source 성공. production cron 활성화 가능 (별 GO).")


if __name__ == "__main__":
    main()
