#!/usr/bin/env python3
"""Second D21 A0 reducer candidate: desired/effective split + revision-CAS cleanup."""

from __future__ import annotations

import concurrent.futures as futures
import json
import subprocess
import sys
import time
from dataclasses import dataclass, asdict


CONTAINER = sys.argv[1]


def psql(sql: str, timeout: float = 30.0) -> str:
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
         "-U", "postgres", "-d", "postgres", "-A", "-t", "-F", "|"],
        input=sql,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"psql rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


def q(sql: str, timeout: float = 30.0) -> str:
    return psql(f"SET search_path TO a0v3, public;\n{sql}\n", timeout)


SCHEMA = r"""
DROP SCHEMA IF EXISTS a0v3 CASCADE;
CREATE SCHEMA a0v3;
SET search_path TO a0v3, public;

-- md5 is a deterministic synthetic stand-in. A production design needs a keyed,
-- versioned HMAC and an explicit key-rotation policy.
CREATE TABLE uid_deletion_barriers (
    uid_hmac text PRIMARY KEY,
    hmac_key_version integer NOT NULL DEFAULT 1,
    deletion_id text NOT NULL,
    deleted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz
);

CREATE TABLE token_order_heads (
    token_hmac text PRIMARY KEY,
    installation_id text NOT NULL,
    latest_generation bigint NOT NULL,
    claim_state text NOT NULL CHECK (claim_state IN ('CLAIMED','RETIRED')),
    server_revision bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE registration_lineage_heads (
    installation_id text PRIMARY KEY,
    max_client_generation bigint NOT NULL,
    last_operation_id text NOT NULL,
    last_payload_hash text NOT NULL,
    owner_uid_hmac text NOT NULL,
    desired_state text NOT NULL CHECK (desired_state IN ('ACTIVE','INACTIVE')),
    effective_state text NOT NULL CHECK (
        effective_state IN ('PRESENT','INACTIVE','FCM_UNREGISTERED','RETENTION_EXPIRED','ACCOUNT_DELETED')
    ),
    token_hmac text,
    server_revision bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE active_delivery_rows (
    device_token text PRIMARY KEY,
    token_hmac text NOT NULL UNIQUE,
    installation_id text UNIQUE,
    user_id text NOT NULL,
    uid_hmac text NOT NULL,
    platform text NOT NULL,
    client_generation bigint,
    server_revision bigint,
    protocol text NOT NULL CHECK (protocol IN ('legacy','v3')),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE operation_receipts (
    operation_id text PRIMARY KEY,
    installation_id text,
    client_generation bigint,
    payload_hash text,
    outcome text NOT NULL,
    server_revision bigint,
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION apply_transition(
    p_operation_id text,
    p_installation text,
    p_generation bigint,
    p_uid text,
    p_token text,
    p_platform text,
    p_desired text,
    p_pause_after_checks double precision DEFAULT 0
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    h registration_lineage_heads%ROWTYPE;
    c token_order_heads%ROWTYPE;
    prior operation_receipts%ROWTYPE;
    uid_key text := md5(p_uid);
    token_key text := md5(p_token);
    payload_key text := md5(p_installation || '|' || p_generation || '|' || p_uid || '|' || p_token || '|' || p_platform || '|' || p_desired);
    next_revision bigint;
    outcome text;
BEGIN
    IF p_desired NOT IN ('ACTIVE','INACTIVE') THEN
        RAISE EXCEPTION 'invalid desired state %', p_desired;
    END IF;

    -- Conservative A0 serialization point shared by every mutation path.
    PERFORM pg_advisory_xact_lock(2026090401);

    SELECT * INTO prior FROM operation_receipts WHERE operation_id = p_operation_id;
    IF FOUND THEN
        IF prior.payload_hash <> payload_key THEN
            RETURN 'OPERATION_ID_PAYLOAD_CONFLICT';
        END IF;
        RETURN 'IDEMPOTENT_REPLAY:' || prior.outcome || ':r' || coalesce(prior.server_revision::text, '-');
    END IF;

    IF EXISTS (SELECT 1 FROM uid_deletion_barriers WHERE uid_hmac = uid_key) THEN
        INSERT INTO operation_receipts(operation_id, installation_id, client_generation, payload_hash, outcome)
        VALUES (p_operation_id, p_installation, p_generation, payload_key, 'DELETION_BARRIER_REJECTED');
        RETURN 'DELETION_BARRIER_REJECTED';
    END IF;

    SELECT * INTO h FROM registration_lineage_heads WHERE installation_id = p_installation;
    IF FOUND AND p_generation <= h.max_client_generation THEN
        outcome := CASE WHEN p_generation = h.max_client_generation
            THEN 'GENERATION_CONFLICT_REJECTED' ELSE 'STALE_REJECTED' END;
        INSERT INTO operation_receipts(operation_id, installation_id, client_generation, payload_hash, outcome, server_revision)
        VALUES (p_operation_id, p_installation, p_generation, payload_key, outcome, h.server_revision);
        RETURN outcome;
    END IF;

    SELECT * INTO c FROM token_order_heads WHERE token_hmac = token_key;
    IF FOUND AND c.installation_id <> p_installation THEN
        INSERT INTO operation_receipts(operation_id, installation_id, client_generation, payload_hash, outcome, server_revision)
        VALUES (p_operation_id, p_installation, p_generation, payload_key, 'TOKEN_CLAIM_CONFLICT_REJECTED', c.server_revision);
        RETURN 'TOKEN_CLAIM_CONFLICT_REJECTED';
    END IF;

    IF p_pause_after_checks > 0 THEN
        PERFORM pg_sleep(p_pause_after_checks);
    END IF;

    next_revision := coalesce(h.server_revision, 0) + 1;

    INSERT INTO token_order_heads(token_hmac, installation_id, latest_generation, claim_state, server_revision)
    VALUES (token_key, p_installation, p_generation,
            CASE WHEN p_desired='ACTIVE' THEN 'CLAIMED' ELSE 'RETIRED' END,
            next_revision)
    ON CONFLICT (token_hmac) DO UPDATE SET
        latest_generation = EXCLUDED.latest_generation,
        claim_state = EXCLUDED.claim_state,
        server_revision = EXCLUDED.server_revision,
        updated_at = clock_timestamp();

    IF h.installation_id IS NOT NULL AND h.token_hmac IS NOT NULL AND h.token_hmac <> token_key THEN
        UPDATE token_order_heads SET
            claim_state='RETIRED', server_revision=next_revision, updated_at=clock_timestamp()
        WHERE token_hmac = h.token_hmac AND installation_id = p_installation;
    END IF;

    IF p_desired = 'ACTIVE' THEN
        DELETE FROM active_delivery_rows WHERE installation_id = p_installation AND device_token <> p_token;
        INSERT INTO active_delivery_rows(
            device_token, token_hmac, installation_id, user_id, uid_hmac,
            platform, client_generation, server_revision, protocol, updated_at
        ) VALUES (
            p_token, token_key, p_installation, p_uid, uid_key,
            p_platform, p_generation, next_revision, 'v3', clock_timestamp()
        )
        ON CONFLICT (device_token) DO UPDATE SET
            token_hmac = EXCLUDED.token_hmac,
            installation_id = EXCLUDED.installation_id,
            user_id = EXCLUDED.user_id,
            uid_hmac = EXCLUDED.uid_hmac,
            platform = EXCLUDED.platform,
            client_generation = EXCLUDED.client_generation,
            server_revision = EXCLUDED.server_revision,
            protocol = 'v3',
            updated_at = clock_timestamp();
        outcome := 'APPLIED_ACTIVE';
    ELSE
        DELETE FROM active_delivery_rows WHERE installation_id = p_installation;
        outcome := 'APPLIED_INACTIVE';
    END IF;

    INSERT INTO registration_lineage_heads(
        installation_id, max_client_generation, last_operation_id, last_payload_hash,
        owner_uid_hmac, desired_state, effective_state, token_hmac, server_revision, updated_at
    ) VALUES (
        p_installation, p_generation, p_operation_id, payload_key, uid_key,
        p_desired, CASE WHEN p_desired='ACTIVE' THEN 'PRESENT' ELSE 'INACTIVE' END,
        token_key, next_revision, clock_timestamp()
    )
    ON CONFLICT (installation_id) DO UPDATE SET
        max_client_generation = EXCLUDED.max_client_generation,
        last_operation_id = EXCLUDED.last_operation_id,
        last_payload_hash = EXCLUDED.last_payload_hash,
        owner_uid_hmac = EXCLUDED.owner_uid_hmac,
        desired_state = EXCLUDED.desired_state,
        effective_state = EXCLUDED.effective_state,
        token_hmac = EXCLUDED.token_hmac,
        server_revision = EXCLUDED.server_revision,
        updated_at = clock_timestamp();

    INSERT INTO operation_receipts(operation_id, installation_id, client_generation, payload_hash, outcome, server_revision)
    VALUES (p_operation_id, p_installation, p_generation, payload_key, outcome, next_revision);
    RETURN outcome || ':r' || next_revision;
END;
$$;

CREATE OR REPLACE FUNCTION purge_snapshot(
    p_owner text,
    p_installation text,
    p_generation bigint,
    p_revision bigint,
    p_token text,
    p_pause_after_lock double precision DEFAULT 0
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    uid_key text := md5(p_owner);
    token_key text := md5(p_token);
    next_revision bigint;
    n integer;
BEGIN
    PERFORM pg_advisory_xact_lock(2026090401);
    IF p_pause_after_lock > 0 THEN PERFORM pg_sleep(p_pause_after_lock); END IF;

    DELETE FROM active_delivery_rows
    WHERE user_id = p_owner
      AND installation_id = p_installation
      AND device_token = p_token
      AND client_generation = p_generation
      AND server_revision = p_revision
      AND protocol = 'v3';
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n = 0 THEN RETURN 'STALE_PURGE_NOOP'; END IF;

    next_revision := p_revision + 1;
    UPDATE registration_lineage_heads SET
        effective_state = 'FCM_UNREGISTERED',
        server_revision = next_revision,
        updated_at = clock_timestamp()
    WHERE installation_id = p_installation
      AND owner_uid_hmac = uid_key
      AND token_hmac = token_key
      AND max_client_generation = p_generation
      AND server_revision = p_revision
      AND effective_state = 'PRESENT';
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n <> 1 THEN RAISE EXCEPTION 'active row/head CAS diverged'; END IF;

    UPDATE token_order_heads SET
        claim_state='RETIRED', server_revision=next_revision, updated_at=clock_timestamp()
    WHERE token_hmac=token_key AND installation_id=p_installation;
    RETURN 'PURGED:r' || next_revision;
END;
$$;

CREATE OR REPLACE FUNCTION retention_sweep(p_cutoff timestamptz)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE rec record; n integer := 0; next_revision bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(2026090401);
    FOR rec IN
        SELECT * FROM active_delivery_rows WHERE updated_at < p_cutoff ORDER BY device_token
    LOOP
        IF rec.protocol = 'v3' THEN
            next_revision := rec.server_revision + 1;
            UPDATE registration_lineage_heads SET
                effective_state='RETENTION_EXPIRED', server_revision=next_revision,
                updated_at=clock_timestamp()
            WHERE installation_id=rec.installation_id
              AND max_client_generation=rec.client_generation
              AND server_revision=rec.server_revision
              AND effective_state='PRESENT';
            IF NOT FOUND THEN RAISE EXCEPTION 'retention active row/head CAS diverged'; END IF;
            UPDATE token_order_heads SET
                claim_state='RETIRED', server_revision=next_revision, updated_at=clock_timestamp()
            WHERE token_hmac=rec.token_hmac AND installation_id=rec.installation_id;
        END IF;
        DELETE FROM active_delivery_rows WHERE device_token=rec.device_token;
        n := n + 1;
    END LOOP;
    RETURN n;
END;
$$;

CREATE OR REPLACE FUNCTION account_delete(
    p_uid text,
    p_deletion_id text,
    p_pause_after_barrier double precision DEFAULT 0
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE uid_key text := md5(p_uid);
BEGIN
    PERFORM pg_advisory_xact_lock(2026090401);
    INSERT INTO uid_deletion_barriers(uid_hmac, deletion_id)
    VALUES (uid_key, p_deletion_id)
    ON CONFLICT (uid_hmac) DO NOTHING;
    UPDATE token_order_heads t SET
        claim_state='RETIRED', server_revision=h.server_revision+1,
        updated_at=clock_timestamp()
    FROM registration_lineage_heads h
    WHERE h.owner_uid_hmac=uid_key
      AND h.token_hmac=t.token_hmac
      AND h.installation_id=t.installation_id;
    UPDATE registration_lineage_heads SET
        desired_state='INACTIVE', effective_state='ACCOUNT_DELETED',
        server_revision=server_revision+1, updated_at=clock_timestamp()
    WHERE owner_uid_hmac=uid_key;
    IF p_pause_after_barrier > 0 THEN PERFORM pg_sleep(p_pause_after_barrier); END IF;
    DELETE FROM active_delivery_rows WHERE uid_hmac=uid_key;
    RETURN 'ACCOUNT_DELETED';
END;
$$;

CREATE OR REPLACE FUNCTION legacy_register(p_uid text, p_token text, p_platform text)
RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(2026090401);
    IF EXISTS (SELECT 1 FROM uid_deletion_barriers WHERE uid_hmac=md5(p_uid)) THEN
        RETURN 'DELETION_BARRIER_REJECTED';
    END IF;
    IF EXISTS (SELECT 1 FROM token_order_heads WHERE token_hmac=md5(p_token)) THEN
        RETURN 'LEGACY_FENCED_REJECTED';
    END IF;
    INSERT INTO active_delivery_rows(
        device_token, token_hmac, installation_id, user_id, uid_hmac,
        platform, client_generation, server_revision, protocol
    ) VALUES (p_token, md5(p_token), NULL, p_uid, md5(p_uid), p_platform, NULL, NULL, 'legacy')
    ON CONFLICT (device_token) DO UPDATE SET
        user_id=EXCLUDED.user_id, uid_hmac=EXCLUDED.uid_hmac,
        platform=EXCLUDED.platform, protocol='legacy', updated_at=clock_timestamp();
    RETURN 'LEGACY_APPLIED';
END;
$$;

CREATE OR REPLACE FUNCTION legacy_delete(p_uid text, p_token text)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
    PERFORM pg_advisory_xact_lock(2026090401);
    IF EXISTS (SELECT 1 FROM token_order_heads WHERE token_hmac=md5(p_token)) THEN
        RETURN 'LEGACY_FENCED_REJECTED';
    END IF;
    DELETE FROM active_delivery_rows
    WHERE user_id=p_uid AND device_token=p_token AND protocol='legacy';
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN CASE WHEN n=1 THEN 'LEGACY_DELETED' ELSE 'LEGACY_NOT_FOUND' END;
END;
$$;
"""


@dataclass
class TestResult:
    name: str
    expected: str
    observed: str
    passed: bool


results: list[TestResult] = []


def add(name: str, expected: str, observed: str, passed: bool) -> None:
    results.append(TestResult(name, expected, observed, passed))


def reset() -> None:
    q("TRUNCATE operation_receipts, active_delivery_rows, registration_lineage_heads, token_order_heads, uid_deletion_barriers;")


def active() -> str:
    return q("SELECT coalesce(string_agg(user_id||':'||device_token||':'||coalesce(installation_id,'legacy')||':'||coalesce(client_generation::text,'-')||':r'||coalesce(server_revision::text,'-'), ',' ORDER BY device_token), '<absent>') FROM active_delivery_rows;")


def head(installation: str = "I") -> str:
    return q(f"SELECT desired_state||'/'||effective_state||'/g'||max_client_generation||'/r'||server_revision FROM registration_lineage_heads WHERE installation_id='{installation}';")


def violations() -> str:
    return q(r"""
WITH v AS (
  SELECT 'present_without_exact_row:'||h.installation_id AS x
  FROM registration_lineage_heads h
  LEFT JOIN active_delivery_rows d ON d.installation_id=h.installation_id
  WHERE h.effective_state='PRESENT'
    AND (d.installation_id IS NULL OR d.token_hmac<>h.token_hmac
         OR d.uid_hmac<>h.owner_uid_hmac
         OR d.client_generation<>h.max_client_generation
         OR d.server_revision<>h.server_revision OR d.protocol<>'v3')
  UNION ALL
  SELECT 'absent_state_has_row:'||h.installation_id
  FROM registration_lineage_heads h JOIN active_delivery_rows d USING(installation_id)
  WHERE h.effective_state<>'PRESENT'
  UNION ALL
  SELECT 'barrier_has_row:'||d.user_id
  FROM active_delivery_rows d JOIN uid_deletion_barriers b ON b.uid_hmac=d.uid_hmac
  UNION ALL
  SELECT 'v3_row_without_claim:'||d.device_token
  FROM active_delivery_rows d LEFT JOIN token_order_heads t ON t.token_hmac=d.token_hmac
  WHERE d.protocol='v3' AND (t.token_hmac IS NULL OR t.installation_id<>d.installation_id)
)
SELECT coalesce(string_agg(x, ',' ORDER BY x), '<none>') FROM v;
""")


def call(sql: str) -> tuple[str, float]:
    start = time.monotonic()
    return q(sql, 20.0), time.monotonic() - start


def sequential() -> None:
    reset()
    q("SELECT apply_transition('a1','I',1,'A','T','android','ACTIVE');")
    q("SELECT apply_transition('b2','I',2,'B','T','android','ACTIVE');")
    stale = q("SELECT apply_transition('a-late','I',1,'A','T','android','ACTIVE');")
    state = active()
    add("stale_post_cannot_overwrite_new_owner", "STALE_REJECTED,B/g2", f"{stale};{state};{violations()}", stale=="STALE_REJECTED" and state=="B:T:I:2:r2" and violations()=="<none>")

    reset()
    q("SELECT apply_transition('a1','I',1,'A','T','android','ACTIVE');")
    q("SELECT apply_transition('a2','I',2,'A','T','android','ACTIVE');")
    stale = q("SELECT apply_transition('old-del','I',1,'A','T','android','INACTIVE');")
    add("stale_delete_cannot_remove_new_same_uid_registration", "STALE_REJECTED,A/g2", f"{stale};{active()}", stale=="STALE_REJECTED" and active()=="A:T:I:2:r2")

    reset()
    first = q("SELECT apply_transition('dup','I',1,'A','T','android','ACTIVE');")
    before = head()
    duplicate = q("SELECT apply_transition('dup','I',1,'A','T','android','ACTIVE');")
    after = head()
    conflict = q("SELECT apply_transition('other','I',1,'A','T2','android','ACTIVE');")
    add("idempotent_replay_and_generation_conflict", "same receipt/no revision bump/conflict rejected", f"{first};{duplicate};{conflict};{before}->{after}", duplicate.startswith("IDEMPOTENT_REPLAY:APPLIED_ACTIVE:r1") and conflict=="GENERATION_CONFLICT_REJECTED" and before==after)

    reset()
    q("SELECT apply_transition('g1','I',1,'A','T','android','ACTIVE');")
    old_revision = q("SELECT server_revision FROM active_delivery_rows WHERE device_token='T';")
    q("SELECT apply_transition('g2','I',2,'A','T','android','ACTIVE');")
    purge = q(f"SELECT purge_snapshot('A','I',1,{old_revision},'T');")
    add("stale_purge_snapshot_cannot_delete_new_generation", "STALE_PURGE_NOOP,A/g2", f"{purge};{active()};{head()}", purge=="STALE_PURGE_NOOP" and active()=="A:T:I:2:r2" and head()=="ACTIVE/PRESENT/g2/r2")

    reset()
    q("SELECT apply_transition('g1','I',1,'A','T','android','ACTIVE');")
    purge = q("SELECT purge_snapshot('A','I',1,1,'T');")
    after_purge = f"{active()};{head()}"
    restore = q("SELECT apply_transition('g2','I',2,'A','T2','android','ACTIVE');")
    add("purge_changes_effective_not_desired_then_new_intent_restores", "ACTIVE/FCM_UNREGISTERED then ACTIVE/PRESENT", f"{purge};{after_purge};{restore};{active()};{head()}", purge=="PURGED:r2" and after_purge=="<absent>;ACTIVE/FCM_UNREGISTERED/g1/r2" and restore=="APPLIED_ACTIVE:r3" and active()=="A:T2:I:2:r3" and head()=="ACTIVE/PRESENT/g2/r3" and violations()=="<none>")

    # A replay of the operation that preceded the external purge must not resurrect the row.
    replay = q("SELECT apply_transition('g1','I',1,'A','T','android','ACTIVE');")
    add("old_operation_replay_after_external_state_change_is_noop", "receipt replay/no mutation", f"{replay};{active()};{head()}", replay.startswith("IDEMPOTENT_REPLAY:APPLIED_ACTIVE:r1") and active()=="A:T2:I:2:r3" and head()=="ACTIVE/PRESENT/g2/r3")

    reset()
    q("SELECT apply_transition('r1','I',1,'A','T','android','ACTIVE');")
    new_deleted = q("SELECT retention_sweep(clock_timestamp()-interval '7 days');")
    q("UPDATE active_delivery_rows SET updated_at=clock_timestamp()-interval '8 days';")
    old_deleted = q("SELECT retention_sweep(clock_timestamp()-interval '7 days');")
    add("retention_positive_control_and_effective_transition", "new 0,old 1,desired ACTIVE/effective RETENTION_EXPIRED", f"{new_deleted};{old_deleted};{active()};{head()}", new_deleted=="0" and old_deleted=="1" and active()=="<absent>" and head()=="ACTIVE/RETENTION_EXPIRED/g1/r2" and violations()=="<none>")

    reset()
    q("SELECT apply_transition('a1','I',1,'A','T','android','ACTIVE'); SELECT account_delete('A','del-A');")
    late_v3 = q("SELECT apply_transition('a2','I',2,'A','T','android','ACTIVE');")
    late_new_token = q("SELECT apply_transition('a3','I2',1,'A','NEVER-SEEN','android','ACTIVE');")
    late_legacy = q("SELECT legacy_register('A','LEGACY-NEW','android');")
    add("uid_barrier_blocks_all_late_A_posts_including_new_tokens", "all barrier rejected;absent", f"{late_v3};{late_new_token};{late_legacy};{active()};{violations()}", late_v3==late_new_token==late_legacy=="DELETION_BARRIER_REJECTED" and active()=="<absent>" and violations()=="<none>")

    b = q("SELECT apply_transition('b2','I',2,'B','T','android','ACTIVE');")
    add("A_deletion_barrier_does_not_block_B", "B active", f"{b};{active()};{head()}", b=="APPLIED_ACTIVE:r3" and active()=="B:T:I:2:r3" and head()=="ACTIVE/PRESENT/g2/r3")

    reset()
    legacy = q("SELECT legacy_register('A','T','android');")
    claim = q("SELECT apply_transition('b1','I',1,'B','T','android','ACTIVE');")
    late_post = q("SELECT legacy_register('A','T','android');")
    late_delete = q("SELECT legacy_delete('A','T');")
    ios = q("SELECT legacy_register('IOS','TIOS','ios');")
    ok = legacy=="LEGACY_APPLIED" and claim=="APPLIED_ACTIVE:r1" and late_post==late_delete=="LEGACY_FENCED_REJECTED" and ios=="LEGACY_APPLIED" and "B:T:I:1:r1" in active() and "IOS:TIOS:legacy:-:r-" in active()
    add("legacy_fence_is_token_scoped_and_unclaimed_ios_remains", "claimed T fenced;unclaimed iOS token allowed", f"{legacy};{claim};{late_post};{late_delete};{ios};{active()}", ok)

    reset()
    q("SELECT apply_transition('i1','I1',1,'A','T','android','ACTIVE');")
    other = q("SELECT apply_transition('i2','I2',1,'B','T','android','ACTIVE');")
    add("different_lineage_same_token_conflicts_safely", "TOKEN_CLAIM_CONFLICT_REJECTED", f"{other};{active()}", other=="TOKEN_CLAIM_CONFLICT_REJECTED" and active()=="A:T:I1:1:r1")


def concurrent_cases() -> None:
    reset()
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        old_f = pool.submit(call, "SELECT apply_transition('old','I',1,'A','T','android','ACTIVE',1.0);")
        time.sleep(0.2)
        new_f = pool.submit(call, "SELECT apply_transition('new','I',2,'B','T','android','ACTIVE',0);")
        old_out, old_s = old_f.result(); new_out, new_s = new_f.result()
    add("concurrent_old_first_then_new", "B/g2", f"{old_out}@{old_s:.2f};{new_out}@{new_s:.2f};{active()}", active()=="B:T:I:2:r2" and violations()=="<none>")

    reset()
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        new_f = pool.submit(call, "SELECT apply_transition('new','I',2,'B','T','android','ACTIVE',1.0);")
        time.sleep(0.2)
        old_f = pool.submit(call, "SELECT apply_transition('old','I',1,'A','T','android','ACTIVE',0);")
        new_out, new_s = new_f.result(); old_out, old_s = old_f.result()
    add("concurrent_new_first_then_old", "old stale,B/g2", f"{new_out}@{new_s:.2f};{old_out}@{old_s:.2f};{active()}", old_out=="STALE_REJECTED" and active()=="B:T:I:2:r1" and violations()=="<none>")

    reset()
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        reg_f = pool.submit(call, "SELECT apply_transition('reg','I',1,'A','T','android','ACTIVE',1.0);")
        time.sleep(0.2)
        del_f = pool.submit(call, "SELECT account_delete('A','del-A',0);")
        reg_out, reg_s = reg_f.result(); del_out, del_s = del_f.result()
    add("barrier_stop_register_after_check_before_upsert", "register then deletion;absent", f"{reg_out}@{reg_s:.2f};{del_out}@{del_s:.2f};{active()};{head()}", active()=="<absent>" and head()=="INACTIVE/ACCOUNT_DELETED/g1/r2" and violations()=="<none>")

    reset()
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        del_f = pool.submit(call, "SELECT account_delete('A','del-A',1.0);")
        time.sleep(0.2)
        reg_f = pool.submit(call, "SELECT apply_transition('reg','I',1,'A','T','android','ACTIVE',0);")
        del_out, del_s = del_f.result(); reg_out, reg_s = reg_f.result()
    add("barrier_stop_delete_after_barrier_before_row_delete", "delete then barrier rejection;absent", f"{del_out}@{del_s:.2f};{reg_out}@{reg_s:.2f};{active()}", reg_out=="DELETION_BARRIER_REJECTED" and active()=="<absent>" and violations()=="<none>")

    reset()
    q("SELECT apply_transition('g1','I',1,'A','T','android','ACTIVE');")
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        purge_f = pool.submit(call, "SELECT purge_snapshot('A','I',1,1,'T',1.0);")
        time.sleep(0.2)
        new_f = pool.submit(call, "SELECT apply_transition('g2','I',2,'A','T','android','ACTIVE',0);")
        purge_out, purge_s = purge_f.result(); new_out, new_s = new_f.result()
    add("concurrent_purge_first_then_new_registration", "new registration present", f"{purge_out}@{purge_s:.2f};{new_out}@{new_s:.2f};{active()};{head()}", active()=="A:T:I:2:r3" and head()=="ACTIVE/PRESENT/g2/r3" and violations()=="<none>")

    reset()
    q("SELECT apply_transition('g1','I',1,'A','T','android','ACTIVE');")
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        new_f = pool.submit(call, "SELECT apply_transition('g2','I',2,'A','T','android','ACTIVE',1.0);")
        time.sleep(0.2)
        purge_f = pool.submit(call, "SELECT purge_snapshot('A','I',1,1,'T',0);")
        new_out, new_s = new_f.result(); purge_out, purge_s = purge_f.result()
    add("concurrent_new_registration_first_then_stale_purge", "stale purge noop,new present", f"{new_out}@{new_s:.2f};{purge_out}@{purge_s:.2f};{active()};{head()}", purge_out=="STALE_PURGE_NOOP" and active()=="A:T:I:2:r2" and head()=="ACTIVE/PRESENT/g2/r2" and violations()=="<none>")

    reset()
    q("SELECT apply_transition('a1','I1',1,'A','T1','android','ACTIVE'); SELECT apply_transition('b1','I2',1,'B','T2','android','ACTIVE');")
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
        a_f = pool.submit(call, "SELECT apply_transition('a2','I1',2,'A','T2','android','ACTIVE',0.5);")
        b_f = pool.submit(call, "SELECT apply_transition('b2','I2',2,'B','T1','android','ACTIVE',0.5);")
        a_out, a_s = a_f.result(); b_out, b_s = b_f.result()
    state = active()
    add("concurrent_two_lineage_token_swap", "both conflict,no deadlock,original rows", f"{a_out}@{a_s:.2f};{b_out}@{b_s:.2f};{state}", a_out==b_out=="TOKEN_CLAIM_CONFLICT_REJECTED" and state=="A:T1:I1:1:r1,B:T2:I2:1:r1" and violations()=="<none>")


def main() -> int:
    psql(SCHEMA, 45.0)
    sequential()
    concurrent_cases()
    for result in results:
        print(json.dumps(asdict(result), sort_keys=True))
    failed = [r.name for r in results if not r.passed]
    print(json.dumps({"summary": {"total": len(results), "passed": len(results)-len(failed), "failed": failed}}, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
