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


def _authorize_production(db, args):
    """b2 prod override authorize — static TTL ceiling + full drain proof + lease acquire.

    Returns: ProductionAuthToken(인가 성공) | int(exit code, BLOCK 시 2). caller(main)는 int면 즉시 return.
    flag는 필요조건일 뿐 — 충분조건은 (drain proof[confirm_quiesce_drained] ∧ lease acquire). args는
    --allow-production-migration True 전제(combo guard 통과 후 호출).
    """
    import math
    import uuid

    from app.atomic_cutover_durable import CasResult, cas_acquire_migration_lease
    from app.atomic_migration import ProductionAuthToken
    from app.atomic_quiesce_durable import confirm_quiesce_drained
    from app.crud import BANK_DISPLAY_ORDER, SUPPORTED_CURRENCY_PAIRS
    from app.models import get_utc_now

    # static TTL ceiling (DB-independent max keys by scope) — _check_apply_target는 single front-gate라
    # mid-run lease 만료를 못 잡음 → TTL이 전체 worst-case(max_keys × per-key timeout)를 덮어야(codex#4).
    n_pairs = len(SUPPORTED_CURRENCY_PAIRS)
    n_banks = len(BANK_DISPLAY_ORDER)
    max_keys = {"bank": n_banks * n_pairs, "investing": n_pairs,
                "all": (n_banks + 1) * n_pairs}[args.scope]
    required_ttl = math.ceil(max_keys * args.timeout_sec) + 120  # ceil(codex) + 120s margin (timeout=soft bound)
    if args.lease_ttl_seconds < required_ttl:
        print(f"[BLOCKED] --lease-ttl-seconds={args.lease_ttl_seconds} < 필요 {required_ttl} "
              f"(max_keys={max_keys} × timeout={args.timeout_sec}s + margin 120)")
        return 2

    # full drain proof — "control이 HALT"만으론 부족(stale legacy-cached writer가 v1 덮음). open quiesce
    # session + qualifying ACK(recreate된 fresh process의 halt 관측) + control HALT @ expected gen 전부 요구.
    if not confirm_quiesce_drained(db, expected_generation=args.expected_writer_generation):
        print("[BLOCKED] quiesce drain 미확인 — halt + recreate-drain(ACK) 선행 필요 (fail-closed). "
              "control HALT @ --expected-writer-generation / open quiesce session / qualifying ACK 미충족.")
        return 2

    # migration lease acquire (owner = fresh per-run token; stable hostname 금지 — idempotency가 우발 동시성 가림)
    owner = f"migrate-{uuid.uuid4()}"
    try:
        r = cas_acquire_migration_lease(db, owner=owner, now=get_utc_now(), ttl_seconds=args.lease_ttl_seconds)
    except Exception as e:
        db.rollback()
        print(f"[BLOCKED] migration lease acquire DB 예외 — fail-closed: {type(e).__name__}")
        return 2
    if r is not CasResult.APPLIED:
        db.rollback()
        print("[BLOCKED] migration lease 미획득 (타 holder non-expired / cutover row 부재 / format mismatch)")
        return 2
    db.commit()  # cross-session durability — staged lease는 다른 host를 배제 못 함 (codex C9)
    print(f"[OK] production authorized — drain proof + lease (owner={owner}, ttl={args.lease_ttl_seconds}s)")
    return ProductionAuthToken(owner=owner, expected_generation=args.expected_writer_generation)


def _release_production_lease(db, token) -> None:
    """b2 prod lease 해제 (best-effort, finally). **rollback-first**(run_migration 실패로 깨진 tx state
    정리) → release → commit. release 예외/non-APPLIED는 원본 migration 에러를 mask하지 않음(raise/return X) —
    TTL 만료가 최종 backstop."""
    from app.atomic_cutover_durable import CasResult, cas_release_migration_lease
    try:
        db.rollback()  # 실패한 migration tx state가 release를 막지 않게 (codex)
        rr = cas_release_migration_lease(db, owner=token.owner)
        if rr is CasResult.APPLIED:
            db.commit()
        else:
            db.rollback()
            print(f"[WARN] migration lease release non-APPLIED ({rr.value}) — TTL 만료가 backstop")
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print(f"[WARN] migration lease release 예외 (무시, TTL backstop): {type(e).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description="A3-3b v1→v2 latest migration (dry-run default)")
    parser.add_argument("--apply", action="store_true",
                        help="실제 write (생략 시 dry-run). prod에서는 hard-fail(override 없음).")
    parser.add_argument("--scope", choices=["bank", "investing", "all"], default="all")
    parser.add_argument("--max-retries", type=int, default=3, help="migrate_cas changed 재시도 상한")
    parser.add_argument("--timeout-sec", type=float, default=60.0, help="per-key retry 시간 상한")
    # b2 — prod migration override (dormant, operator-run only). 단순 flag 아님: drain proof + lease 동반.
    parser.add_argument("--allow-production-migration", action="store_true",
                        help="prod apply override (C6-PRE). halt drain proof + migration lease 충족 시만. "
                             "--apply + --expected-writer-generation 필수.")
    parser.add_argument("--expected-writer-generation", type=int, default=None,
                        help="prod override 시 halt mode_generation pin (operator 확인값, 0 유효).")
    parser.add_argument("--lease-ttl-seconds", type=int, default=3600,
                        help="prod override migration lease TTL (런타임 강제: >= max_keys×timeout+margin).")
    args = parser.parse_args()

    if args.max_retries < 0:
        print("[BLOCKED] --max-retries는 0 이상")
        return 1
    if args.timeout_sec <= 0:
        print("[BLOCKED] --timeout-sec는 양수")
        return 1

    # b2 combo fail-close — flag는 필요조건일 뿐(절대 충분조건 아님). I/O 전.
    if args.allow_production_migration:
        if not args.apply:
            print("[BLOCKED] --allow-production-migration은 --apply 필요")
            return 1
        if args.expected_writer_generation is None:  # is None (gen 0은 유효 pin — falsy 체크 금지)
            print("[BLOCKED] --allow-production-migration은 --expected-writer-generation 필요")
            return 1
        if args.expected_writer_generation < 0:
            print("[BLOCKED] --expected-writer-generation은 0 이상")
            return 1
        if args.lease_ttl_seconds <= 0:
            print("[BLOCKED] --lease-ttl-seconds는 양수")
            return 1

    # apply 안전 가드 — prod hard-fail (override 없음). main 진입 직후, DB/Redis I/O 전.
    # b2: override 경로는 check_apply_safety를 우회(authorize[drain proof + lease] + _check_apply_target
    # backstop이 대체) — no-flag 경로는 byte-identical(기존 prod hard-block 유지).
    if args.apply and not args.allow_production_migration:
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
        production_authorized = None
        try:
            if args.allow_production_migration:
                rc = _authorize_production(db, args)   # ProductionAuthToken | int(exit code)
                if isinstance(rc, int):
                    return rc                          # BLOCK (exit 2) — lease 미획득이라 release 불요
                production_authorized = rc
            result = run_migration(
                db, client, apply=args.apply, scope=args.scope,
                max_retries=args.max_retries, timeout_sec=args.timeout_sec,
                production_authorized=production_authorized,
            )
        finally:
            if production_authorized is not None:
                _release_production_lease(db, production_authorized)
            db.close()
    finally:
        if lock_fd is not None:
            lock_fd.close()

    _print_report(result)
    return 1 if result.error_count else 0


if __name__ == "__main__":
    sys.exit(main())
