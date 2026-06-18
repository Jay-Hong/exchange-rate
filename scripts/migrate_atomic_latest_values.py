#!/usr/bin/env python3
"""P1b A3-3b — controlled v1→v2 latest value migration command (§17, dormant).

bank/investing latest:* 값을 v1(revision 없음)에서 v2(schema_version/revision_key/rate_key)로
이행하는 controlled command. **dry-run 기본** — write는 `--apply` 명시 시만. **prod hard-fail
(override 없음)**: apply는 C6 lease 전까지 local 전용(sqlite DB + localhost Redis). lockfile은
same-host 중복 실행 방지용 보조일 뿐 prod safety 아님(prod는 위 가드가 차단).

dormant — live writer path가 본 스크립트/runner를 호출하지 않음. 운영 cutover 실행은 C6.

usage:
  python scripts/migrate_atomic_latest_values.py                 # dry-run (전체)
  python scripts/migrate_atomic_latest_values.py --scope bank    # bank만 dry-run
  python scripts/migrate_atomic_latest_values.py --apply         # local apply (sqlite+localhost만)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가 (python scripts/... 직접 실행 시 app import — 기존 스크립트 패턴)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _print_report(result) -> None:
    mode = "DRY-RUN" if result.dry_run else "APPLY"
    print(f"\n=== atomic latest migration [{mode}] ===")
    for r in result.results:
        flag = "✗" if r.error else ("→W" if r.wrote else "  ")
        line = f"  {flag} [{r.kind}] {r.key} action={r.action} retries={r.retries}"
        if r.error:
            line += f" ERROR={r.error}"
        elif r.detail:
            line += f" — {r.detail}"
        print(line)
    # 전 action histogram — 일부만 표기하면 skipped_newer/redis_ahead/db_absent/retry_exhausted/
    # timeout/fetch_error/exception 등 non-error action이 요약에서 비가시(holistic 검토 Low).
    from collections import Counter
    hist = Counter(r.action for r in result.results)
    hist_str = " ".join(f"{a}={n}" for a, n in sorted(hist.items()))
    print(
        f"--- total={len(result.results)} wrote={result.wrote_count} "
        f"errors={result.error_count} | {hist_str}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="A3-3b v1→v2 latest migration (dry-run default)")
    parser.add_argument("--apply", action="store_true",
                        help="실제 write (생략 시 dry-run). prod에서는 hard-fail(override 없음).")
    parser.add_argument("--scope", choices=["bank", "investing", "all"], default="all")
    parser.add_argument("--max-retries", type=int, default=3, help="migrate_cas changed 재시도 상한")
    parser.add_argument("--timeout-sec", type=float, default=60.0, help="per-key retry 시간 상한")
    args = parser.parse_args()

    if args.max_retries < 0:
        print("[BLOCKED] --max-retries는 0 이상")
        return 1
    if args.timeout_sec <= 0:
        print("[BLOCKED] --timeout-sec는 양수")
        return 1

    # apply 안전 가드 — prod hard-fail (override 없음). main 진입 직후, DB/Redis I/O 전.
    if args.apply:
        from app.atomic_migration import check_apply_safety
        block = check_apply_safety()
        if block:
            print(f"[BLOCKED] {block}")
            return 1

    # lockfile — same-host 중복 실행 방지(보조, prod safety 아님). apply일 때만.
    lock_fd = None
    if args.apply:
        import fcntl
        import os
        import tempfile
        lock_path = os.path.join(tempfile.gettempdir(), "fxi_atomic_migration.lock")
        lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"[BLOCKED] 다른 migration 인스턴스 실행 중 (lock: {lock_path})")
            lock_fd.close()
            return 1

    try:
        from app.atomic_migration import run_migration
        from app.database import SessionLocal
        from app.latest_rates_cache import _get_sync_client

        client = _get_sync_client()
        if client is None:
            print("[ERROR] Redis sync client 연결 실패")
            return 1

        db = SessionLocal()
        try:
            result = run_migration(
                db, client, apply=args.apply, scope=args.scope,
                max_retries=args.max_retries, timeout_sec=args.timeout_sec,
            )
        finally:
            db.close()
    finally:
        if lock_fd is not None:
            lock_fd.close()

    _print_report(result)
    return 1 if result.error_count else 0


if __name__ == "__main__":
    sys.exit(main())
