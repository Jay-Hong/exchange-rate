#!/usr/bin/env python3
"""
ADR-034 Phase 2d Step 5 + PR A2 — Daily append orchestrator (--source all = bithumb + hana).

목적:
  - 기존 backfill writer를 subprocess로 호출 (재사용, 신규 write logic 0)
  - source별 daily append (yesterday KST 1 row) 일괄 실행
  - dry-run mode: validation + write command preview only (subprocess 실행 X)
  - write mode: subprocess + capture + structured summary (PASS/FAIL/rows/exit code)

A2 scope (--source all = bithumb + hana, 00:01 KST cron):
  - Bithumb: 24h candle close (backfill_bithumb writer 재사용)
  - Hana: observed_eod (bank_exchange_rates 내부 관측 read → backfill_hana_observed_eod writer)
  - KRX 미포함: close finalizer (CF 15:45 KST 종가 직후 trigger)는 별 PR 영역

source별 독립 (cross-source transaction 아님):
  - 각 source = 별도 subprocess + 각자 commit. Bithumb commit 후 Hana 실패 시
    Bithumb row는 유지되고 aggregate exit만 1 (부분 성공 = 의도된 정책).
  - 한 source 예외가 다른 source를 막지 않음 (main loop per-source except Exception 격리).
  - 재실행은 writer의 idempotent upsert(ON CONFLICT)로 안전.

후속 PR scope:
  - KRX close finalizer 통합 / Hana JPY·EUR 다각화 / retry·backoff·structured alert

사용법:
  # dry-run (default) — validation + write command preview only, subprocess 실행 X
  python scripts/daily_append_source_daily_rates.py
  python scripts/daily_append_source_daily_rates.py --date 2026-05-27

  # write mode — subprocess 실 실행 + structured summary
  python scripts/daily_append_source_daily_rates.py --write
  python scripts/daily_append_source_daily_rates.py --write --date 2026-05-27

  # 다중 source (bithumb + hana) — production cron 권장 형태
  python scripts/daily_append_source_daily_rates.py --source all --write --allow-production-write

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

# A2: --source 허용값 (bithumb / hana / all). KRX는 close finalizer 별 PR — 00:01 cron 미대상.
SUPPORTED_SOURCES = ["bithumb", "hana", "all"]
# "all" 확장 대상 — 순서 고정 (bithumb → hana). tuple로 불변 보장.
SOURCE_RUN_ALL = ("bithumb", "hana")

# source-aware verdict 정책 (Codex Blocker — global allowlist 대신 source별 잠금)
# - bithumb: 24/7 source라 skipped 불가 (항상 candle 존재) → written only
# - hana: 영업일 calendar라 skipped(주말/공휴일) 허용
SOURCE_POLICY = {
    "bithumb": {"asset": "usdt-krw", "allowed_statuses": frozenset({"written"})},
    "hana": {"asset": "usd-krw", "allowed_statuses": frozenset({"written", "skipped"})},
}

# dry-run preview 문구 (source별 — Bithumb candle fetch vs Hana 내부 DB read)
_DRYRUN_NOTE = {
    "bithumb": "Bithumb writer 전체 candle fetch + validation (DB write 0; "
               "--start-date/--end-date를 target-date 한정으로 사용하지 않음)",
    "hana": "Hana observed_eod writer — bank_exchange_rates 내부 관측 read 기반 "
            "단일일 dry-run (--date, DB write 0)",
}


def resolve_sources(source_arg: str) -> tuple[str, ...]:
    """--source 인자 → 실행 source tuple. "all" → SOURCE_RUN_ALL (순서 고정)."""
    if source_arg == "all":
        return SOURCE_RUN_ALL
    return (source_arg,)


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


def build_hana_command(d: date, *, write: bool, allow_production: bool) -> list[str]:
    """backfill_hana_observed_eod_source_daily_rates.py command 생성.

    **Bithumb과 CLI 형태 다름** (observed_eod writer):
    - write=True: --write --start-date X --end-date X --emit-daily-append-verdict (단일일)
    - write=False (dry-run): --date X (bank_exchange_rates 내부 read, start/end 미사용)
    """
    script_path = (
        Path(__file__).resolve().parent / "backfill_hana_observed_eod_source_daily_rates.py"
    ).resolve()
    if not write:
        # dry-run: observed_eod writer는 --date 기반 (start/end 아님)
        return [sys.executable, str(script_path), "--date", d.isoformat()]
    cmd = [
        sys.executable,
        str(script_path),
        "--write",
        "--start-date", d.isoformat(),
        "--end-date", d.isoformat(),
        "--emit-daily-append-verdict",  # orchestrator는 JSON verdict로 결과 판정
    ]
    if allow_production:
        cmd.append("--allow-production-write")
    return cmd


def build_command(source: str, d: date, *, write: bool, allow_production: bool) -> list[str]:
    """source별 command dispatch (A2 scope: bithumb, hana)."""
    if source == "bithumb":
        return build_bithumb_command(d, write=write, allow_production=allow_production)
    if source == "hana":
        return build_hana_command(d, write=write, allow_production=allow_production)
    raise ValueError(f"미지원 source: {source} (A2 scope: bithumb, hana)")


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


# result dict 계약 — main loop이 post-try에서 직접 indexing (record/print/summary).
# malformed(키 누락/non-dict) result는 main loop에서 synthetic FAIL로 변환 (격리 경계 보존).
_REQUIRED_RESULT_KEYS = frozenset({"exit", "status", "detail", "tail"})


def execute_source(source: str, d: date, *, allow_production: bool) -> dict:
    """source 1개 실행 단위: run_source(subprocess) + evaluate_source_result → result dict.

    계약: 반드시 `_REQUIRED_RESULT_KEYS` 4개 키를 가진 dict 반환.
    예외는 잡지 않음 — caller(main loop)가 per-source `except Exception`으로 격리하여
    한 source 실패가 다른 source 실행을 막지 않도록 한다.
    """
    rc, stdout, tail = run_source(source, d, allow_production=allow_production)
    verdict_status, detail = evaluate_source_result(source, d, rc, stdout)
    return {"exit": rc, "status": verdict_status, "detail": detail, "tail": tail}


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
    print(f"[DRY-RUN] writer dry-run command ({_DRYRUN_NOTE.get(source, source)}):")
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
            "ADR-034 Phase 2d Step 5 + PR A2 — Daily append orchestrator "
            "(--source all = bithumb + hana). 기존 backfill writer를 subprocess로 호출."
        )
    )
    parser.add_argument(
        "--source",
        choices=SUPPORTED_SOURCES,
        default="bithumb",
        help=(
            "적재 대상 source. 'all'=bithumb+hana (순서 고정). "
            "default 'bithumb' 유지 (후방 호환). KRX는 close finalizer 별 PR."
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
    sources = resolve_sources(args.source)  # "all" → ("bithumb", "hana")

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

    # write mode: source별 격리 실행 — 한 source 예외가 다른 source를 막지 않음.
    # execute_source(run_source + evaluate) + result shape 검증을 except Exception으로 감싼다
    # (BaseException 금지: KeyboardInterrupt / SystemExit 전파). 예외/malformed → synthetic FAIL.
    # 따라서 try 이후 result는 항상 _REQUIRED_RESULT_KEYS dict → record/print/summary indexing 안전.
    results: dict[str, dict] = {}
    for source in sources:
        print(f"--- {source} subprocess ---")
        try:
            result = execute_source(source, target_date, allow_production=args.allow_production_write)
            if not isinstance(result, dict) or not _REQUIRED_RESULT_KEYS <= result.keys():
                raise ValueError(f"execute_source malformed result (키 누락/non-dict): {result!r}")
        except Exception as e:
            result = {
                "exit": None,
                "status": "FAIL",
                "detail": f"orchestrator 예외: {type(e).__name__}: {e}",
                "tail": f"[EXCEPTION] {type(e).__name__}: {e}",
            }
        results[source] = result
        print(result["tail"])  # 위 검증으로 4-key dict 보장 → 안전
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
    print("[PASS] 모든 source 성공.")


if __name__ == "__main__":
    main()
