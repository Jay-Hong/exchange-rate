#!/usr/bin/env python3
"""Offline D21 Phase A0 PostgreSQL ordering harness.

This file lives under /private/tmp.  It mirrors the current SQL semantics and
exercises a candidate serialized desired-state reducer; it does not import or
modify either FXi repository.
"""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
import time
from dataclasses import dataclass, asdict


CONTAINER = sys.argv[1]


def psql(sql: str, *, timeout: float = 30.0) -> str:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            CONTAINER,
            "psql",
            "-X",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-A",
            "-t",
            "-F",
            "|",
        ],
        input=sql,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"psql rc={result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result.stdout.strip()


def q(sql: str, *, timeout: float = 30.0) -> str:
    return psql(f"SET search_path TO a0, public;\n{sql}\n", timeout=timeout)


SETUP = r"""
DROP SCHEMA IF EXISTS a0 CASCADE;
CREATE SCHEMA a0;
SET search_path TO a0, public;

-- Current production semantics, reduced to the columns relevant to ordering.
CREATE TABLE current_devices (
    id bigserial PRIMARY KEY,
    user_id text NOT NULL,
    device_token text NOT NULL UNIQUE,
    platform text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION current_register(
    p_uid text, p_token text, p_platform text, p_updated_at timestamptz DEFAULT clock_timestamp()
) RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO current_devices(user_id, device_token, platform, created_at, updated_at)
    VALUES (p_uid, p_token, p_platform, p_updated_at, p_updated_at)
    ON CONFLICT (device_token) DO UPDATE SET
        user_id = EXCLUDED.user_id,
        platform = EXCLUDED.platform,
        updated_at = EXCLUDED.updated_at;
    RETURN 'APPLIED';
END;
$$;

CREATE OR REPLACE FUNCTION current_delete(p_uid text, p_token text)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
    DELETE FROM current_devices WHERE user_id = p_uid AND device_token = p_token;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN CASE WHEN n > 0 THEN 'DELETED' ELSE 'NOT_FOUND' END;
END;
$$;

CREATE OR REPLACE FUNCTION current_purge(p_owner text, p_tokens text[])
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
    DELETE FROM current_devices
    WHERE user_id = p_owner AND device_token = ANY(p_tokens);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

CREATE OR REPLACE FUNCTION current_account_delete(p_uid text)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
    DELETE FROM current_devices WHERE user_id = p_uid;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

CREATE OR REPLACE FUNCTION current_retention(p_cutoff timestamptz)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
    DELETE FROM current_devices WHERE updated_at < p_cutoff;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END;
$$;

-- Deliberately unsafe barrier check, used only as a positive control.
CREATE TABLE naive_uid_barriers(uid_key text PRIMARY KEY);
CREATE TABLE naive_devices(device_token text PRIMARY KEY, user_id text NOT NULL);

CREATE OR REPLACE FUNCTION naive_register(
    p_uid text, p_token text, p_pause_after_check double precision DEFAULT 0
) RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM naive_uid_barriers WHERE uid_key = md5(p_uid)) THEN
        RETURN 'BARRIER_REJECTED';
    END IF;
    IF p_pause_after_check > 0 THEN
        PERFORM pg_sleep(p_pause_after_check);
    END IF;
    INSERT INTO naive_devices(device_token, user_id) VALUES (p_token, p_uid)
    ON CONFLICT (device_token) DO UPDATE SET user_id = EXCLUDED.user_id;
    RETURN 'APPLIED';
END;
$$;

CREATE OR REPLACE FUNCTION naive_account_delete(p_uid text)
RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO naive_uid_barriers(uid_key) VALUES (md5(p_uid)) ON CONFLICT DO NOTHING;
    DELETE FROM naive_devices WHERE user_id = p_uid;
    RETURN 'DELETED';
END;
$$;

-- Candidate v2 model. md5 stands in for a keyed HMAC only inside this synthetic harness.
CREATE TABLE v2_uid_barriers (
    uid_key text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reason text NOT NULL DEFAULT 'account_delete'
);

CREATE TABLE v2_token_claims (
    token_key text PRIMARY KEY,
    installation_id text NOT NULL,
    first_generation bigint NOT NULL,
    latest_generation bigint NOT NULL,
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE v2_install_heads (
    installation_id text PRIMARY KEY,
    max_generation bigint NOT NULL,
    last_op_id text NOT NULL,
    owner_uid_key text NOT NULL,
    desired_state text NOT NULL CHECK (
        desired_state IN ('ACTIVE','INACTIVE','TOKEN_RETIRED','RETENTION_EXPIRED','DELETION_BARRIER')
    ),
    token_key text,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE v2_active_devices (
    device_token text PRIMARY KEY,
    token_key text NOT NULL UNIQUE,
    installation_id text UNIQUE,
    user_id text NOT NULL,
    uid_key text NOT NULL,
    platform text NOT NULL,
    generation bigint,
    mode text NOT NULL CHECK (mode IN ('legacy','v2')),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE v2_receipts (
    op_id text PRIMARY KEY,
    installation_id text,
    generation bigint,
    outcome text NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- One serialization point is intentionally conservative for the spike. A production
-- reducer may shard it only after proving a canonical multi-key lock order.
CREATE OR REPLACE FUNCTION v2_apply(
    p_op_id text,
    p_installation text,
    p_generation bigint,
    p_uid text,
    p_token text,
    p_platform text,
    p_desired text,
    p_pause_after_checks double precision DEFAULT 0
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
    r_outcome text;
    h v2_install_heads%ROWTYPE;
    c v2_token_claims%ROWTYPE;
    token_hash text := md5(p_token);
    uid_hash text := md5(p_uid);
BEGIN
    IF p_desired NOT IN ('ACTIVE','INACTIVE') THEN
        RAISE EXCEPTION 'invalid desired state %', p_desired;
    END IF;
    PERFORM pg_advisory_xact_lock(20260904);

    SELECT outcome INTO r_outcome FROM v2_receipts WHERE op_id = p_op_id;
    IF FOUND THEN
        RETURN 'DUPLICATE:' || r_outcome;
    END IF;

    IF EXISTS (SELECT 1 FROM v2_uid_barriers WHERE uid_key = uid_hash) THEN
        INSERT INTO v2_receipts(op_id, installation_id, generation, outcome)
        VALUES (p_op_id, p_installation, p_generation, 'DELETION_BARRIER_REJECTED');
        RETURN 'DELETION_BARRIER_REJECTED';
    END IF;

    SELECT * INTO h FROM v2_install_heads WHERE installation_id = p_installation;
    IF FOUND AND p_generation <= h.max_generation THEN
        r_outcome := CASE
            WHEN p_generation = h.max_generation THEN 'GENERATION_CONFLICT_REJECTED'
            ELSE 'STALE_REJECTED'
        END;
        INSERT INTO v2_receipts(op_id, installation_id, generation, outcome)
        VALUES (p_op_id, p_installation, p_generation, r_outcome);
        RETURN r_outcome;
    END IF;

    SELECT * INTO c FROM v2_token_claims WHERE token_key = token_hash;
    IF FOUND AND c.installation_id <> p_installation THEN
        INSERT INTO v2_receipts(op_id, installation_id, generation, outcome)
        VALUES (p_op_id, p_installation, p_generation, 'TOKEN_CLAIM_CONFLICT_REJECTED');
        RETURN 'TOKEN_CLAIM_CONFLICT_REJECTED';
    END IF;

    IF p_pause_after_checks > 0 THEN
        PERFORM pg_sleep(p_pause_after_checks);
    END IF;

    INSERT INTO v2_token_claims(token_key, installation_id, first_generation, latest_generation)
    VALUES (token_hash, p_installation, p_generation, p_generation)
    ON CONFLICT (token_key) DO UPDATE SET
        latest_generation = GREATEST(v2_token_claims.latest_generation, EXCLUDED.latest_generation);

    IF p_desired = 'ACTIVE' THEN
        DELETE FROM v2_active_devices
        WHERE installation_id = p_installation AND device_token <> p_token;
        INSERT INTO v2_active_devices(
            device_token, token_key, installation_id, user_id, uid_key,
            platform, generation, mode, updated_at
        ) VALUES (
            p_token, token_hash, p_installation, p_uid, uid_hash,
            p_platform, p_generation, 'v2', clock_timestamp()
        )
        ON CONFLICT (device_token) DO UPDATE SET
            token_key = EXCLUDED.token_key,
            installation_id = EXCLUDED.installation_id,
            user_id = EXCLUDED.user_id,
            uid_key = EXCLUDED.uid_key,
            platform = EXCLUDED.platform,
            generation = EXCLUDED.generation,
            mode = 'v2',
            updated_at = clock_timestamp();
        r_outcome := 'APPLIED_ACTIVE';
    ELSE
        DELETE FROM v2_active_devices WHERE installation_id = p_installation;
        r_outcome := 'APPLIED_INACTIVE';
    END IF;

    INSERT INTO v2_install_heads(
        installation_id, max_generation, last_op_id, owner_uid_key,
        desired_state, token_key, updated_at
    ) VALUES (
        p_installation, p_generation, p_op_id, uid_hash,
        p_desired, token_hash, clock_timestamp()
    )
    ON CONFLICT (installation_id) DO UPDATE SET
        max_generation = EXCLUDED.max_generation,
        last_op_id = EXCLUDED.last_op_id,
        owner_uid_key = EXCLUDED.owner_uid_key,
        desired_state = EXCLUDED.desired_state,
        token_key = EXCLUDED.token_key,
        updated_at = clock_timestamp();

    INSERT INTO v2_receipts(op_id, installation_id, generation, outcome)
    VALUES (p_op_id, p_installation, p_generation, r_outcome);
    RETURN r_outcome;
END;
$$;

CREATE OR REPLACE FUNCTION v2_legacy_register(
    p_uid text, p_token text, p_platform text
) RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(20260904);
    IF EXISTS (SELECT 1 FROM v2_uid_barriers WHERE uid_key = md5(p_uid)) THEN
        RETURN 'DELETION_BARRIER_REJECTED';
    END IF;
    IF EXISTS (SELECT 1 FROM v2_token_claims WHERE token_key = md5(p_token)) THEN
        RETURN 'LEGACY_FENCED_REJECTED';
    END IF;
    INSERT INTO v2_active_devices(
        device_token, token_key, installation_id, user_id, uid_key,
        platform, generation, mode, updated_at
    ) VALUES (
        p_token, md5(p_token), NULL, p_uid, md5(p_uid),
        p_platform, NULL, 'legacy', clock_timestamp()
    )
    ON CONFLICT (device_token) DO UPDATE SET
        user_id = EXCLUDED.user_id,
        uid_key = EXCLUDED.uid_key,
        platform = EXCLUDED.platform,
        mode = 'legacy',
        updated_at = clock_timestamp();
    RETURN 'LEGACY_APPLIED';
END;
$$;

CREATE OR REPLACE FUNCTION v2_legacy_delete(p_uid text, p_token text)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
    PERFORM pg_advisory_xact_lock(20260904);
    IF EXISTS (SELECT 1 FROM v2_token_claims WHERE token_key = md5(p_token)) THEN
        RETURN 'LEGACY_FENCED_REJECTED';
    END IF;
    DELETE FROM v2_active_devices
    WHERE user_id = p_uid AND device_token = p_token AND mode = 'legacy';
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN CASE WHEN n > 0 THEN 'LEGACY_DELETED' ELSE 'LEGACY_NOT_FOUND' END;
END;
$$;

CREATE OR REPLACE FUNCTION v2_account_delete(
    p_uid text, p_pause_after_barrier double precision DEFAULT 0
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE uid_hash text := md5(p_uid);
BEGIN
    PERFORM pg_advisory_xact_lock(20260904);
    INSERT INTO v2_uid_barriers(uid_key) VALUES (uid_hash) ON CONFLICT DO NOTHING;
    UPDATE v2_install_heads SET
        desired_state = 'DELETION_BARRIER', updated_at = clock_timestamp()
    WHERE owner_uid_key = uid_hash;
    IF p_pause_after_barrier > 0 THEN
        PERFORM pg_sleep(p_pause_after_barrier);
    END IF;
    DELETE FROM v2_active_devices WHERE uid_key = uid_hash;
    RETURN 'ACCOUNT_DELETED';
END;
$$;

CREATE OR REPLACE FUNCTION v2_purge(p_owner text, p_tokens text[])
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    rec record;
    n integer := 0;
BEGIN
    PERFORM pg_advisory_xact_lock(20260904);
    FOR rec IN
        SELECT installation_id, device_token FROM v2_active_devices
        WHERE user_id = p_owner AND device_token = ANY(p_tokens)
    LOOP
        IF rec.installation_id IS NOT NULL THEN
            UPDATE v2_install_heads SET
                desired_state = 'TOKEN_RETIRED', updated_at = clock_timestamp()
            WHERE installation_id = rec.installation_id;
        END IF;
        DELETE FROM v2_active_devices WHERE device_token = rec.device_token;
        n := n + 1;
    END LOOP;
    RETURN n;
END;
$$;

CREATE OR REPLACE FUNCTION v2_retention(p_cutoff timestamptz)
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    rec record;
    n integer := 0;
BEGIN
    PERFORM pg_advisory_xact_lock(20260904);
    FOR rec IN
        SELECT installation_id, device_token FROM v2_active_devices
        WHERE updated_at < p_cutoff
    LOOP
        IF rec.installation_id IS NOT NULL THEN
            UPDATE v2_install_heads SET
                desired_state = 'RETENTION_EXPIRED', updated_at = clock_timestamp()
            WHERE installation_id = rec.installation_id;
        END IF;
        DELETE FROM v2_active_devices WHERE device_token = rec.device_token;
        n := n + 1;
    END LOOP;
    RETURN n;
END;
$$;
"""


@dataclass
class Result:
    scenario: str
    expected: str
    observed: str
    passed: bool
    kind: str


results: list[Result] = []


def add(scenario: str, expected: str, observed: str, passed: bool, kind: str) -> None:
    results.append(Result(scenario, expected, observed, passed, kind))


def reset_current() -> None:
    q("TRUNCATE current_devices RESTART IDENTITY;")


def reset_v2() -> None:
    q(
        "TRUNCATE v2_receipts, v2_active_devices, v2_install_heads, "
        "v2_token_claims, v2_uid_barriers;"
    )


def active_state() -> str:
    return q(
        "SELECT coalesce(string_agg(user_id || ':' || device_token || ':' || "
        "coalesce(installation_id,'legacy') || ':' || coalesce(generation::text,'-'), ',' "
        "ORDER BY device_token), '<absent>') FROM v2_active_devices;"
    )


def current_state() -> str:
    return q(
        "SELECT coalesce(string_agg(user_id || ':' || device_token, ',' ORDER BY device_token), "
        "'<absent>') FROM current_devices;"
    )


def invariant_violations() -> str:
    return q(
        r"""
WITH violations AS (
  SELECT 'active_head_mismatch:' || h.installation_id AS v
  FROM v2_install_heads h
  LEFT JOIN v2_active_devices d ON d.installation_id = h.installation_id
  WHERE h.desired_state = 'ACTIVE'
    AND (d.installation_id IS NULL OR d.token_key <> h.token_key
         OR d.uid_key <> h.owner_uid_key OR d.generation <> h.max_generation OR d.mode <> 'v2')
  UNION ALL
  SELECT 'inactive_head_has_row:' || h.installation_id
  FROM v2_install_heads h JOIN v2_active_devices d USING (installation_id)
  WHERE h.desired_state <> 'ACTIVE'
  UNION ALL
  SELECT 'barrier_has_row:' || d.user_id
  FROM v2_active_devices d JOIN v2_uid_barriers b ON b.uid_key = d.uid_key
  UNION ALL
  SELECT 'unclaimed_v2_row:' || d.device_token
  FROM v2_active_devices d LEFT JOIN v2_token_claims c ON c.token_key = d.token_key
  WHERE d.mode = 'v2' AND (c.token_key IS NULL OR c.installation_id <> d.installation_id)
)
SELECT coalesce(string_agg(v, ',' ORDER BY v), '<none>') FROM violations;
"""
    )


def call(sql: str) -> tuple[str, float]:
    started = time.monotonic()
    return q(sql, timeout=20.0), time.monotonic() - started


def run_current_matrix() -> None:
    reset_current()
    q("SELECT current_register('A','T','android'); SELECT current_register('B','T','android');")
    q("SELECT current_register('A','T','android');")
    state = current_state()
    add("current_stale_post_overwrites_new_owner", "B:T", state, state == "B:T", "hard_gate")

    reset_current()
    q("SELECT current_register('A','T','android'); SELECT current_register('A','T','android');")
    outcome = q("SELECT current_delete('A','T');")
    state = current_state()
    add(
        "current_stale_delete_removes_new_same_uid_registration",
        "A:T remains",
        f"{outcome};{state}",
        state == "A:T",
        "hard_gate",
    )

    reset_current()
    q("SELECT current_register('A','T','android'); SELECT current_account_delete('A');")
    q("SELECT current_register('A','T','android');")
    state = current_state()
    add("current_late_post_resurrects_after_account_delete", "<absent>", state, state == "<absent>", "hard_gate")

    reset_current()
    q("SELECT current_register('A','T','android'); SELECT current_register('B','T','android');")
    purged = q("SELECT current_purge('A', ARRAY['T']);")
    state = current_state()
    add("current_owner_fenced_purge_preserves_B", "purged=0;B:T", f"purged={purged};{state}", purged == "0" and state == "B:T", "existing_fence")

    reset_current()
    q("SELECT current_register('A','T','android'); SELECT current_purge('A', ARRAY['T']);")
    q("SELECT current_register('A','T','android');")
    state = current_state()
    add("current_late_post_resurrects_after_purge", "<absent>", state, state == "<absent>", "hard_gate")

    reset_current()
    q("SELECT current_register('A','T','android');")
    outcome = q("SELECT current_delete('B','T');")
    state = current_state()
    add("current_404_is_not_owner_absence_proof", "NOT_FOUND with A:T still present", f"{outcome};{state}", outcome == "NOT_FOUND" and state == "A:T", "characterization")

    reset_current()
    q("SELECT current_register('A','T','android');")
    state = current_state()
    add("current_response_loss_after_commit", "client unknown;A:T committed", f"simulated response loss;{state}", state == "A:T", "characterization")

    reset_current()
    q("SELECT current_register('A','NEW','android');")
    deleted_new = q("SELECT current_retention(clock_timestamp() - interval '7 days');")
    q("SELECT current_register('A','OLD','android', clock_timestamp() - interval '8 days');")
    deleted_old = q("SELECT current_retention(clock_timestamp() - interval '7 days');")
    state = current_state()
    ok = deleted_new == "0" and deleted_old == "1" and state == "A:NEW"
    add("current_retention_positive_control", "new kept;8d old deleted", f"new_deleted={deleted_new};old_deleted={deleted_old};{state}", ok, "positive_control")


def run_naive_barrier_positive_control() -> None:
    q("TRUNCATE naive_devices, naive_uid_barriers;")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        register_future = pool.submit(call, "SELECT naive_register('A','T',1.0);")
        time.sleep(0.2)
        delete_future = pool.submit(call, "SELECT naive_account_delete('A');")
        register_out, register_s = register_future.result()
        delete_out, delete_s = delete_future.result()
    state = q("SELECT coalesce(string_agg(user_id || ':' || device_token, ','), '<absent>') FROM naive_devices;")
    observed = f"register={register_out}@{register_s:.2f}s;delete={delete_out}@{delete_s:.2f}s;{state}"
    add("naive_barrier_check_then_upsert_TOCTOU", "race reproduces A:T resurrection", observed, state == "A:T", "positive_control")


def run_v2_sequential() -> None:
    reset_v2()
    q("SELECT v2_apply('a1','I',1,'A','T','android','ACTIVE');")
    q("SELECT v2_apply('b2','I',2,'B','T','android','ACTIVE');")
    stale = q("SELECT v2_apply('a-late','I',1,'A','T','android','ACTIVE');")
    state = active_state()
    inv = invariant_violations()
    add("v2_stale_A_post_after_B", "STALE_REJECTED;B:T:I:2", f"{stale};{state};inv={inv}", stale == "STALE_REJECTED" and state == "B:T:I:2" and inv == "<none>", "candidate")

    reset_v2()
    q("SELECT v2_apply('a1','I',1,'A','T','android','ACTIVE');")
    q("SELECT v2_apply('a2','I',2,'A','T','android','ACTIVE');")
    stale = q("SELECT v2_apply('old-delete','I',1,'A','T','android','INACTIVE');")
    state = active_state()
    add("v2_stale_delete_after_new_same_uid_registration", "STALE_REJECTED;A:T:I:2", f"{stale};{state}", stale == "STALE_REJECTED" and state == "A:T:I:2", "candidate")

    reset_v2()
    q("SELECT v2_apply('a1','I',1,'A','T','android','ACTIVE');")
    q("SELECT v2_account_delete('A');")
    late = q("SELECT v2_apply('a2-late','I',2,'A','T','android','ACTIVE');")
    state = active_state()
    inv = invariant_violations()
    add("v2_account_delete_blocks_late_v2_post", "DELETION_BARRIER_REJECTED;<absent>", f"{late};{state};inv={inv}", late == "DELETION_BARRIER_REJECTED" and state == "<absent>" and inv == "<none>", "candidate")

    late_legacy = q("SELECT v2_legacy_register('A','TL','android');")
    state = active_state()
    add("v2_account_delete_blocks_late_legacy_post", "DELETION_BARRIER_REJECTED;<absent>", f"{late_legacy};{state}", late_legacy == "DELETION_BARRIER_REJECTED" and state == "<absent>", "candidate")

    b = q("SELECT v2_apply('b2','I',2,'B','T','android','ACTIVE');")
    state = active_state()
    add("v2_new_uid_can_claim_after_A_delete", "APPLIED_ACTIVE;B:T:I:2", f"{b};{state}", b == "APPLIED_ACTIVE" and state == "B:T:I:2", "candidate")

    reset_v2()
    legacy = q("SELECT v2_legacy_register('A','T','android');")
    claim = q("SELECT v2_apply('b1','I',1,'B','T','android','ACTIVE');")
    stale_post = q("SELECT v2_legacy_register('A','T','android');")
    stale_delete = q("SELECT v2_legacy_delete('A','T');")
    state = active_state()
    ok = legacy == "LEGACY_APPLIED" and claim == "APPLIED_ACTIVE" and stale_post == "LEGACY_FENCED_REJECTED" and stale_delete == "LEGACY_FENCED_REJECTED" and state == "B:T:I:1"
    add("v2_claim_fences_legacy_android_on_same_token", "legacy allowed before claim;blocked after", f"{legacy};{claim};{stale_post};{stale_delete};{state}", ok, "candidate")

    ios = q("SELECT v2_legacy_register('IOS','TIOS','ios');")
    state = active_state()
    add("v2_unclaimed_ios_legacy_unchanged", "LEGACY_APPLIED;IOS row present", f"{ios};{state}", ios == "LEGACY_APPLIED" and "IOS:TIOS:legacy:-" in state, "candidate")

    reset_v2()
    q("SELECT v2_apply('r1','I',1,'A','T1','android','ACTIVE');")
    q("SELECT v2_apply('r2','I',2,'A','T2','android','ACTIVE');")
    late = q("SELECT v2_apply('r1-late','I',1,'A','T1','android','ACTIVE');")
    state = active_state()
    add("v2_rotation_rejects_old_token_post", "STALE_REJECTED;A:T2:I:2", f"{late};{state}", late == "STALE_REJECTED" and state == "A:T2:I:2", "candidate")

    reset_v2()
    q("SELECT v2_apply('p1','I',1,'A','T','android','ACTIVE');")
    purged = q("SELECT v2_purge('A', ARRAY['T']);")
    late = q("SELECT v2_apply('p1-late','I',1,'A','T','android','ACTIVE');")
    state = active_state()
    head = q("SELECT desired_state FROM v2_install_heads WHERE installation_id='I';")
    add("v2_purge_updates_head_and_rejects_late_same_generation", "purged=1;GENERATION_CONFLICT_REJECTED;<absent>;TOKEN_RETIRED", f"{purged};{late};{state};{head}", purged == "1" and late == "GENERATION_CONFLICT_REJECTED" and state == "<absent>" and head == "TOKEN_RETIRED", "candidate")

    # Adversarial case omitted by the first candidate's happy-path invariant:
    # an old send snapshot only carries owner+token, so its purge deletes a newer
    # same-owner/same-token registration and overwrites the user's ACTIVE intent.
    reset_v2()
    q("SELECT v2_apply('p-old','I',1,'A','T','android','ACTIVE');")
    q("SELECT v2_apply('p-new','I',2,'A','T','android','ACTIVE');")
    purged = q("SELECT v2_purge('A', ARRAY['T']);")
    state = active_state()
    head = q("SELECT desired_state || ':' || max_generation FROM v2_install_heads WHERE installation_id='I';")
    safe = purged == "0" and state == "A:T:I:2" and head == "ACTIVE:2"
    add(
        "v2_stale_purge_deletes_new_same_uid_generation",
        "purged=0;A:T:I:2;ACTIVE:2",
        f"purged={purged};{state};{head}",
        safe,
        "candidate_rejected",
    )

    reset_v2()
    q("SELECT v2_apply('t1','I',1,'A','T','android','ACTIVE');")
    deleted_new = q("SELECT v2_retention(clock_timestamp() - interval '7 days');")
    q("UPDATE v2_active_devices SET updated_at = clock_timestamp() - interval '8 days';")
    deleted_old = q("SELECT v2_retention(clock_timestamp() - interval '7 days');")
    state = active_state()
    head = q("SELECT desired_state FROM v2_install_heads WHERE installation_id='I';")
    ok = deleted_new == "0" and deleted_old == "1" and state == "<absent>" and head == "RETENTION_EXPIRED" and invariant_violations() == "<none>"
    add("v2_retention_preserves_new_and_transitions_old_head", "new kept;8d old expired atomically", f"new_deleted={deleted_new};old_deleted={deleted_old};{state};{head}", ok, "candidate")

    reset_v2()
    q("SELECT v2_account_delete('A'); DELETE FROM v2_uid_barriers WHERE uid_key=md5('A');")
    late = q("SELECT v2_legacy_register('A','T','android');")
    state = active_state()
    add("v2_finite_barrier_removal_positive_control", "removing barrier permits resurrection", f"{late};{state}", late == "LEGACY_APPLIED" and state == "A:T:legacy:-", "positive_control")


def run_v2_concurrency() -> None:
    reset_v2()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        old_f = pool.submit(call, "SELECT v2_apply('old','I',1,'A','T','android','ACTIVE',1.0);")
        time.sleep(0.2)
        new_f = pool.submit(call, "SELECT v2_apply('new','I',2,'B','T','android','ACTIVE',0);")
        old_out, old_s = old_f.result()
        new_out, new_s = new_f.result()
    state = active_state()
    add("v2_concurrent_old_locks_first", "final generation 2/B", f"old={old_out}@{old_s:.2f};new={new_out}@{new_s:.2f};{state}", state == "B:T:I:2" and invariant_violations() == "<none>", "candidate_concurrent")

    reset_v2()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        new_f = pool.submit(call, "SELECT v2_apply('new','I',2,'B','T','android','ACTIVE',1.0);")
        time.sleep(0.2)
        old_f = pool.submit(call, "SELECT v2_apply('old','I',1,'A','T','android','ACTIVE',0);")
        new_out, new_s = new_f.result()
        old_out, old_s = old_f.result()
    state = active_state()
    ok = new_out == "APPLIED_ACTIVE" and old_out == "STALE_REJECTED" and state == "B:T:I:2" and invariant_violations() == "<none>"
    add("v2_concurrent_new_locks_first", "old rejected;final generation 2/B", f"new={new_out}@{new_s:.2f};old={old_out}@{old_s:.2f};{state}", ok, "candidate_concurrent")

    reset_v2()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        register_f = pool.submit(call, "SELECT v2_apply('reg','I',1,'A','T','android','ACTIVE',1.0);")
        time.sleep(0.2)
        delete_f = pool.submit(call, "SELECT v2_account_delete('A',0);")
        register_out, register_s = register_f.result()
        delete_out, delete_s = delete_f.result()
    state = active_state()
    ok = register_out == "APPLIED_ACTIVE" and delete_out == "ACCOUNT_DELETED" and state == "<absent>" and invariant_violations() == "<none>"
    add("v2_barrier_stop_after_register_check_before_upsert", "register commits then delete removes it", f"register={register_out}@{register_s:.2f};delete={delete_out}@{delete_s:.2f};{state}", ok, "candidate_concurrent")

    reset_v2()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        delete_f = pool.submit(call, "SELECT v2_account_delete('A',1.0);")
        time.sleep(0.2)
        register_f = pool.submit(call, "SELECT v2_apply('reg','I',1,'A','T','android','ACTIVE',0);")
        delete_out, delete_s = delete_f.result()
        register_out, register_s = register_f.result()
    state = active_state()
    ok = delete_out == "ACCOUNT_DELETED" and register_out == "DELETION_BARRIER_REJECTED" and state == "<absent>" and invariant_violations() == "<none>"
    add("v2_barrier_stop_after_barrier_before_delete", "delete commits;register rejected", f"delete={delete_out}@{delete_s:.2f};register={register_out}@{register_s:.2f};{state}", ok, "candidate_concurrent")


def main() -> int:
    psql(SETUP, timeout=45.0)
    run_current_matrix()
    run_naive_barrier_positive_control()
    run_v2_sequential()
    run_v2_concurrency()

    for result in results:
        print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))

    counts: dict[str, int] = {}
    for result in results:
        counts[result.kind] = counts.get(result.kind, 0) + 1
    failed = [r.scenario for r in results if not r.passed]
    print(json.dumps({"summary": {"total": len(results), "by_kind": counts, "failed": failed}}, sort_keys=True))

    # Current hard-gate failures are expected characterizations. Candidate/positive controls
    # and the already-deployed owner fence must pass their asserted outcomes.
    unexpected = [
        r.scenario for r in results
        if not r.passed and r.kind not in {"hard_gate", "candidate_rejected"}
    ]
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
