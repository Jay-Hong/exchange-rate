#!/usr/bin/env python3
"""Adversarial counterexamples against the 18/18 A0 reducer candidate."""

from __future__ import annotations

import json
import subprocess
import sys


CONTAINER = sys.argv[1]


def q(sql: str) -> str:
    proc = subprocess.run(
        [
            "docker", "exec", "-i", CONTAINER,
            "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
            "-U", "postgres", "-d", "postgres", "-A", "-t", "-F", "|",
        ],
        input="SET search_path TO a0v3, public;\n" + sql + "\n",
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"psql rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


def reset() -> None:
    q(
        "TRUNCATE operation_receipts, active_delivery_rows, "
        "registration_lineage_heads, token_order_heads, uid_deletion_barriers;"
    )


def active() -> str:
    return q(
        "SELECT coalesce(string_agg(user_id||':'||device_token||':'||"
        "coalesce(installation_id,'legacy'), ',' ORDER BY device_token), '<absent>') "
        "FROM active_delivery_rows;"
    )


def emit(name: str, hard_gate: str, observed: str, passed: bool) -> None:
    print(json.dumps({
        "name": name,
        "hard_gate": hard_gate,
        "observed": observed,
        "passed": passed,
    }, sort_keys=True))


def main() -> int:
    failures = 0
    total = 0

    # INACTIVE must not claim an arbitrary token that the lineage never owned.
    reset()
    q("SELECT apply_transition('a1','I1',1,'A','T1','android','ACTIVE');")
    inactive = q("SELECT apply_transition('a2','I1',2,'A','T2','android','INACTIVE');")
    b = q("SELECT apply_transition('b1','I2',1,'B','T2','android','ACTIVE');")
    token_heads = q(
        "SELECT string_agg(token_hmac||':'||installation_id||':'||claim_state, ',' "
        "ORDER BY installation_id) FROM token_order_heads;"
    )
    passed = b == "APPLIED_ACTIVE:r1" and active() == "B:T2:I2"
    emit(
        "inactive_arbitrary_token_must_not_poison_future_claim",
        "an INACTIVE transition may retire only the lineage's exact current token",
        f"inactive={inactive};future_B={b};active={active()};heads={token_heads}",
        passed,
    )
    total += 1
    failures += 0 if passed else 1

    # A receipt is historical evidence, not the current desired/effective state.
    reset()
    q("SELECT apply_transition('op-active','I',1,'A','T','android','ACTIVE');")
    q("SELECT account_delete('A','delete-A');")
    replay = q("SELECT apply_transition('op-active','I',1,'A','T','android','ACTIVE');")
    state = q(
        "SELECT desired_state||'/'||effective_state FROM registration_lineage_heads "
        "WHERE installation_id='I';"
    )
    passed = replay.startswith("IDEMPOTENT_REPLAY:") and "ACCOUNT_DELETED" in replay
    emit(
        "receipt_replay_must_not_report_stale_active_as_current_after_account_delete",
        "a replay may expose the historical result only if current effective state is explicit",
        f"replay={replay};active={active()};current={state}",
        passed,
    )
    total += 1
    failures += 0 if passed else 1

    # During cutover, an unclaimed v3 operation has no ordering relation to a
    # newer legacy commit. It must not silently take ownership.
    reset()
    legacy = q("SELECT legacy_register('B','T','android');")
    delayed_v3 = q("SELECT apply_transition('old-a','I',1,'A','T','android','ACTIVE');")
    passed = active() == "B:T:legacy"
    emit(
        "legacy_to_v3_cutover_needs_an_ordering_barrier",
        "a delayed pre-cutover v3 request must not overwrite a newer legacy owner",
        f"legacy={legacy};delayed_v3={delayed_v3};active={active()}",
        passed,
    )
    total += 1
    failures += 0 if passed else 1

    # A permanent token claim tied to the old installation may block a valid
    # post-delete owner on a new installation identity.
    reset()
    q("SELECT apply_transition('a1','I1',1,'A','T','android','ACTIVE');")
    q("SELECT account_delete('A','delete-A');")
    b = q("SELECT apply_transition('b1','I2',1,'B','T','android','ACTIVE');")
    passed = b == "APPLIED_ACTIVE:r1" and active() == "B:T:I2"
    emit(
        "retired_token_claim_needs_bounded_safe_reclaim_semantics",
        "a valid new installation must have a defined recovery path",
        f"new_installation={b};active={active()}",
        passed,
    )
    total += 1
    failures += 0 if passed else 1

    # The same stale-receipt ambiguity exists after an external FCM purge.
    reset()
    q("SELECT apply_transition('op-active','I',1,'A','T','android','ACTIVE');")
    q("SELECT purge_snapshot('A','I',1,1,'T');")
    replay = q("SELECT apply_transition('op-active','I',1,'A','T','android','ACTIVE');")
    state = q(
        "SELECT desired_state||'/'||effective_state FROM registration_lineage_heads "
        "WHERE installation_id='I';"
    )
    passed = replay.startswith("IDEMPOTENT_REPLAY:") and "FCM_UNREGISTERED" in replay
    emit(
        "receipt_replay_must_not_hide_external_purge",
        "historical operation receipt and current effective state must be distinguishable",
        f"replay={replay};active={active()};current={state}",
        passed,
    )
    total += 1
    failures += 0 if passed else 1

    # Purge must fail atomically if its ordering head is missing/corrupt.
    reset()
    q("SELECT apply_transition('a1','I',1,'A','T','android','ACTIVE');")
    q("DELETE FROM token_order_heads WHERE token_hmac=md5('T');")
    purge = q("SELECT purge_snapshot('A','I',1,1,'T');")
    legacy = q("SELECT legacy_register('A','T','android');")
    passed = purge.startswith("INVARIANT_") and legacy == "LEGACY_FENCED_REJECTED"
    emit(
        "purge_must_cas_token_head_or_rollback",
        "missing/mismatched token head must make the purge transaction fail closed",
        f"purge={purge};legacy_after={legacy};active={active()}",
        passed,
    )
    total += 1
    failures += 0 if passed else 1

    # Client-controlled generation needs a bounded, recoverable domain.
    reset()
    maxed = q(
        "SELECT apply_transition('max','I',9223372036854775807,'A','T','android','ACTIVE');"
    )
    future = q(
        "SELECT apply_transition('future','I',9223372036854775807,'A','T2','android','ACTIVE');"
    )
    passed = maxed != "APPLIED_ACTIVE:r1"
    emit(
        "client_generation_must_not_allow_bigint_max_lineage_lockout",
        "untrusted generation input must have validation and a recovery protocol",
        f"max={maxed};next_attempt={future};active={active()}",
        passed,
    )
    total += 1
    failures += 0 if passed else 1

    reset()
    zero = q("SELECT apply_transition('zero','I',0,'A','T','android','ACTIVE');")
    passed = zero.startswith("INVALID_GENERATION")
    emit(
        "client_generation_domain_must_reject_zero_or_negative",
        "generation must be positive and validated before it becomes durable high-water",
        f"generation_zero={zero};active={active()}",
        passed,
    )
    total += 1
    failures += 0 if passed else 1

    print(json.dumps({"summary": {"total": total, "passed": total - failures, "failed": failures}}))
    # Counterexamples are expected evidence; keep the harness process successful.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
