"""KRX 시간봉 4버킷 삭제 — 만기 휴장 보정으로 드러난 잘못된 월물 행 제거.

## 무엇을 지우는가 (정확히 4건, allowlist 고정)

구 코드가 8월물 만기를 8/17(무보정 셋째 월요일)로 알고 있어, 실제 만기일인
**2026-08-14 종일 A75608(만기 당일 월물)을 붙들고** 수집했다. 보정 후 정책상
그날 07:00 KST부터는 **A75609**가 맞다. 그 결과:

  - `08:00`~`11:00` KST 4버킷 — **A75608 가격**. 표시되면 안 되는 월물이다. → **삭제**
  - `12:00`~`15:00` — A75609가 거래 중이었으나 **수집 자체가 없다**. 원천 tick이
    없으므로 **합성하지 않는다**. 빈 구간이 잘못된 가격보다 정확하다.
  - `00:00`~`06:00` — 야간장(8/13 18:00 시작). 보정 후에도 A75608이 맞다. → **보존**

`source_hourly_rates`는 `contract_code`를 저장하지 않으므로(D6-① 재설계) 행 자체로는
월물을 알 수 없다. 귀속 근거는 **당시 구독 상태**이고, `point_count`가 11시에 급감한
것(57→269→111→**29**)이 A75608의 최종거래일 11:30 조기 종료와 일치한다.

주의: 원본 `source_rates` tick은 30일간 남고 월물을 저장하지 않는다. 정상 cron의
기본 2일 재집계 창은 이미 8/14를 벗어났지만, 원본이 소멸하는 2026-09-14 전까지
`hourly_append_krx_source_hourly_rates.py --window-days 14` 같은 수동 장기 재집계를
실행하면 이 4버킷이 다시 생길 수 있다. 그 기간에는 해당 창의 수동 재집계를 금지한다.

## 왜 기존 helper를 못 쓰는가

`app/source_hourly_rates.delete_range()`는 **내부에서 바로 commit**하고 preimage가
없다. 되돌릴 기준 없이 지우는 셈이라 이 작업에는 부적합하다.

## 안전 계약 (일봉 교정 도구와 동형)

  - **allowlist 4버킷만**, CLI로 확장 불가.
  - **`FOR UPDATE` 먼저, 그 다음 preimage** (전 컬럼).
  - `--expect-preimage-sha`로 dry-run이 검토한 상태와 삭제 대상을 **결속**.
  - preimage를 **삭제 전에** `O_EXCL`+fsync로 내구 기록 — 기록 실패면 삭제 없음.
  - DELETE **rowcount == 4** 강제.
  - **같은 transaction 안에서** 4행 부재 + 보존 창 지문 불변 + 총 행 수 −4 확인.
  - 실패 단계 3분류(PRE_COMMIT / COMMITTING / COMMITTED) — exit 1 vs 2.
  - `--revert-from`으로 preimage에서 **4행 원자 복원**.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

# 프로젝트 루트를 sys.path에 추가 — 리포 관용구. 이게 없으면 문서에 적힌 실행
# 형태가 `ModuleNotFoundError: app`으로 죽는다(일봉 도구에서 실측된 결함).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

SOURCE = "krx"
ASSET = "usd-krw-futures"

# 삭제 대상 — naive KST 시 정각. **코드에 고정**되며 CLI로 확장할 수 없다.
DELETE_ALLOWLIST: tuple[datetime, ...] = (
    datetime(2026, 8, 14, 8, 0, 0),
    datetime(2026, 8, 14, 9, 0, 0),
    datetime(2026, 8, 14, 10, 0, 0),
    datetime(2026, 8, 14, 11, 0, 0),
)

# 보존을 **증명**할 창 (야간장). 삭제 전후 지문이 같아야 한다.
KEEP_WINDOW: tuple[datetime, ...] = tuple(
    datetime(2026, 8, 14, h, 0, 0) for h in range(0, 7)
)

PREIMAGE_COLUMNS = (
    "id", "source", "asset", "bucket_ts_kst", "rate", "high", "low", "close",
    "ohlc_quality", "close_basis", "source_method", "contract_code",
    "basis_date", "published_at", "captured_at", "metadata_json",
)

PHASE_PRE_COMMIT = "pre_commit"
PHASE_COMMITTING = "committing"
PHASE_COMMITTED = "committed"

COMMITTED_RECEIPT_UNVERIFIED = "COMMITTED_BUT_RECEIPT_UNVERIFIED"
COMMIT_OUTCOME_UNVERIFIED = "COMMIT_OUTCOME_UNVERIFIED"

_NO_RECEIPT_SENTINEL = "__no_receipt__"


class RepairAbort(RuntimeError):
    """중단 — 호출자는 transaction을 rollback한다."""


# ---------------------------------------------------------------------------
# 순수 계약
# ---------------------------------------------------------------------------

def fingerprint(rows: dict) -> str:
    """전 컬럼 논리 지문. key는 문자열화한 bucket_ts_kst."""
    payload = {
        str(k): (None if r is None else {
            c: (None if r.get(c) is None else
                (r[c] if c == "metadata_json" else str(r[c])))
            for c in PREIMAGE_COLUMNS
        })
        for k, r in sorted(rows.items(), key=lambda kv: str(kv[0]))
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def sha(text_: str) -> str:
    return hashlib.sha256(text_.encode("utf-8")).hexdigest()


def check_precondition(rows: dict) -> None:
    """정확히 4행이 있고, **우리가 아는 모양**인가.

    "없으면 조용히 skip"으로 만들면 재실행이 조용해지지만, 다른 무언가가 그 행을
    바꿔 놓은 경우와 구분되지 않는다.
    """
    missing = [ts for ts in DELETE_ALLOWLIST if rows.get(ts) is None]
    if missing:
        raise RepairAbort(
            f"삭제 대상 버킷이 없다: {[str(m) for m in missing]} — "
            "이미 지워졌거나 예상 밖 상태")
    if len(rows) != len(DELETE_ALLOWLIST):
        raise RepairAbort(f"대상 수 {len(rows)} != {len(DELETE_ALLOWLIST)}")
    for ts, row in sorted(rows.items(), key=lambda kv: str(kv[0])):
        if row.get("source") != SOURCE or row.get("asset") != ASSET:
            raise RepairAbort(f"{ts}: source/asset 불일치")
        # KRX 시간봉은 월물을 저장하지 않는다(D6-①). 값이 있으면 우리가 아는
        # 행이 아니다 — 다른 경로가 쓴 것일 수 있으므로 멈춘다.
        if row.get("contract_code") is not None:
            raise RepairAbort(
                f"{ts}: contract_code={row['contract_code']!r} — KRX 시간봉은 "
                "월물을 저장하지 않는다. 예상 밖 행이므로 지우지 않는다")


def verify_post_delete(remaining: dict, keep_before: str, keep_after: str,
                       total_before: int, total_after: int) -> None:
    """**transaction 내부** 사후 검증. 실패 시 caller가 rollback."""
    still = [str(ts) for ts, row in remaining.items() if row is not None]
    if still:
        raise RepairAbort(f"삭제 후에도 남아 있다: {still}")
    if keep_before != keep_after:
        raise RepairAbort(
            "보존 창(00~06시) 지문이 달라졌다 — 삭제가 옆으로 샜다")
    delta = total_after - total_before
    if delta != -len(DELETE_ALLOWLIST):
        raise RepairAbort(
            f"KRX 시간봉 총 행 수 delta={delta} != -{len(DELETE_ALLOWLIST)}")


def build_restore_plan(preimage_rows: dict) -> list[dict]:
    """preimage → 역연산(= 4행 재삽입) 계획."""
    allowed = {str(ts) for ts in DELETE_ALLOWLIST}
    unknown = sorted(set(preimage_rows) - allowed)
    if unknown:
        raise RepairAbort(f"preimage에 allowlist 밖 버킷: {unknown}")
    missing = sorted(allowed - set(preimage_rows))
    if missing:
        raise RepairAbort(f"preimage에 빠진 버킷: {missing} — 부분 복원은 하지 않는다")
    plan = []
    for key in sorted(preimage_rows):
        row = preimage_rows[key]
        if row is None:
            raise RepairAbort(f"{key}: preimage가 None — 복원할 값이 없다")
        absent = [c for c in PREIMAGE_COLUMNS if c not in row]
        if absent:
            raise RepairAbort(f"{key}: preimage에 없는 컬럼 {absent}")
        plan.append({"bucket_ts_kst": key,
                     "values": {c: row[c] for c in PREIMAGE_COLUMNS}})
    return plan


def check_production_write_guard(database_url: str, *, mutates: bool,
                                 allow_production: bool) -> None:
    """production DB를 실수로 겨눴을 때 **1줄 + 중단**.

    판정 대상은 **실제 세션이 bind된 URL**이다. dotenv는 패키지 import 체인이
    로드한다(`app/__init__` → `app.logging` → `app.config`). dialect/host는
    출력하지 않는다(보안 원칙).
    """
    if not mutates:
        return
    if database_url.startswith("postgresql") and not allow_production:
        raise RepairAbort(
            "production DB 대상 변경은 --allow-production-write 필요 (중단)")


# ---------------------------------------------------------------------------
# transaction core
# ---------------------------------------------------------------------------

def delete_all(
    *,
    lock_and_read: Callable[[], dict],
    keep_snapshot: Callable[[], str],
    total_count: Callable[[], int],
    apply_delete: Callable[[], int],
    reread: Callable[[], dict],
    persist_preimage: Callable[[str, str], None],
    write: bool,
    expected_preimage_sha: Optional[str] = None,
) -> dict:
    """4버킷을 **한 단위**로 삭제. 예외는 그대로 올려 caller가 rollback한다.

    순서가 계약이다:
      1. **잠금 후** 전 대상 read → preimage 고정
      2. 검토 상태와 결속(`expected_preimage_sha`)
      3. 사전 조건 (정확히 4행, 우리가 아는 모양)
      4. 보존 창 지문 + 총 행 수 기록
      5. preimage **내구 기록** (삭제 전)
      6. DELETE (rowcount == 4 강제)
      7. 같은 transaction 안에서 사후 검증
    """
    rows = lock_and_read()
    fp = fingerprint(rows)
    actual_sha = sha(fp)

    if expected_preimage_sha is not None and expected_preimage_sha != actual_sha:
        raise RepairAbort(
            f"preimage sha 불일치: 현재 {actual_sha[:16]}… != 기대 "
            f"{expected_preimage_sha[:16]}… — dry-run 이후 대상이 바뀌었다")

    check_precondition(rows)

    keep_before = keep_snapshot()
    total_before = total_count()

    plan = [{"bucket_ts_kst": str(ts)} for ts in DELETE_ALLOWLIST]
    if not write:
        return {"mode": "dry-run", "preimage_sha": actual_sha, "preimage": fp,
                "planned_delete": plan, "keep_fingerprint": sha(keep_before),
                "total_before": total_before}

    persist_preimage(fp, actual_sha)

    rowcount = apply_delete()
    if rowcount != len(DELETE_ALLOWLIST):
        raise RepairAbort(
            f"DELETE rowcount={rowcount} != {len(DELETE_ALLOWLIST)} — "
            "사전 조건 확인 이후 대상이 바뀌었다")

    remaining = reread()
    keep_after = keep_snapshot()
    total_after = total_count()
    verify_post_delete(remaining, keep_before, keep_after,
                       total_before, total_after)
    postimage = fingerprint(remaining)

    return {"mode": "write", "preimage_sha": actual_sha, "preimage": fp,
            "deleted": plan, "keep_fingerprint": sha(keep_before),
            "total_before": total_before, "total_after": total_after,
            "postimage": postimage, "postimage_sha": sha(postimage)}


def restore_all(
    *,
    plan: list[dict],
    expected_sha: str,
    expected_postimage_sha: Optional[str],
    lock_and_read: Callable[[], dict],
    apply_insert: Callable[[list[dict]], int],
    reread: Callable[[], dict],
) -> dict:
    """삭제를 되돌린다. 사후조건 = **지문이 원래 preimage sha를 재현**."""
    current = lock_and_read()
    if expected_postimage_sha is None:
        raise RepairAbort(
            "postimage sha가 없다 — 무엇을 되돌리는지 확인할 수 없다. "
            "삭제 영수증(.committed)이 있어야 하고, 없으면 "
            "--revert-without-receipt로 명시해야 한다")
    if expected_postimage_sha != _NO_RECEIPT_SENTINEL:
        current_sha = sha(fingerprint(current))
        if current_sha != expected_postimage_sha:
            raise RepairAbort(
                f"현재 상태가 우리 postimage가 아니다: {current_sha[:16]}… != "
                f"{expected_postimage_sha[:16]}… — 삭제 이후 다른 변경이 있었다")
    present = [str(ts) for ts, row in current.items() if row is not None]
    if present:
        raise RepairAbort(
            f"복원 대상이 이미 존재한다: {present} — 우리 상태가 아니다")

    rowcount = apply_insert(plan)
    if rowcount != len(DELETE_ALLOWLIST):
        raise RepairAbort(f"복원 INSERT rowcount={rowcount} != {len(DELETE_ALLOWLIST)}")

    got = sha(fingerprint(reread()))
    if got != expected_sha:
        raise RepairAbort(
            f"복원 후 지문 {got[:16]}… != 원래 {expected_sha[:16]}… — "
            "되돌림이 원 상태를 재현하지 못했다")
    return {"mode": "restore", "restored_preimage_sha": got,
            "restored": [p["bucket_ts_kst"] for p in plan]}


# ---------------------------------------------------------------------------
# DB 배선
# ---------------------------------------------------------------------------

def build_adapters(db):
    from sqlalchemy import delete as sa_delete, func, insert as sa_insert, select

    from app.models import SourceHourlyRate as M

    def _scope(*extra):
        return (M.source == SOURCE, M.asset == ASSET, *extra)

    def _to_dict(row) -> dict:
        return {c: getattr(row, c) for c in PREIMAGE_COLUMNS}

    def lock_and_read() -> dict:
        # ORM select + with_for_update — 손으로 쓴 SQL은 테이블 이름을 문자열로
        # 박아 schema 라우팅(테스트 격리)을 우회한다.
        db.execute(select(M.id).where(
            *_scope(M.bucket_ts_kst.in_(DELETE_ALLOWLIST))).with_for_update())
        rows = db.execute(select(M).where(
            *_scope(M.bucket_ts_kst.in_(DELETE_ALLOWLIST)))).scalars().all()
        out = {ts: None for ts in DELETE_ALLOWLIST}
        for row in rows:
            out[row.bucket_ts_kst] = _to_dict(row)
        return out

    def keep_snapshot() -> str:
        db.expire_all()
        rows = db.execute(select(M).where(
            *_scope(M.bucket_ts_kst.in_(KEEP_WINDOW)))).scalars().all()
        return fingerprint({r.bucket_ts_kst: _to_dict(r) for r in rows})

    def total_count() -> int:
        db.expire_all()
        return db.execute(select(func.count()).select_from(M)
                          .where(*_scope())).scalar_one()

    def apply_delete() -> int:
        result = db.execute(sa_delete(M).where(
            *_scope(M.bucket_ts_kst.in_(DELETE_ALLOWLIST))))
        db.flush()
        db.expire_all()
        return result.rowcount

    def apply_insert(plan: list[dict]) -> int:
        # ⛔ INSERT 의 `rowcount` 를 믿지 않는다 — 이 드라이버는 RETURNING 없는
        #    INSERT 에 **-1** 을 돌려준다(실측: 4건 넣고 합계 -4). executemany 는
        #    아예 `IteratorResult` 라 rowcount 속성조차 없다.
        #    그래서 넣은 뒤 **실제로 존재하는 대상 행 수를 읽어** 반환한다 —
        #    드라이버 산출물이 아니라 관측이다.
        for item in plan:
            v = item["values"]
            db.execute(sa_insert(M).values(
                **{c: _deserialize(c, v[c]) for c in PREIMAGE_COLUMNS}))
        db.flush()
        db.expire_all()
        return db.execute(
            select(func.count()).select_from(M)
            .where(*_scope(M.bucket_ts_kst.in_(DELETE_ALLOWLIST)))).scalar_one()

    def reread() -> dict:
        db.expire_all()
        return lock_and_read()

    return dict(lock_and_read=lock_and_read, keep_snapshot=keep_snapshot,
                total_count=total_count, apply_delete=apply_delete,
                apply_insert=apply_insert, reread=reread)


def _deserialize(column: str, raw):
    """지문은 전부 문자열로 직렬화돼 있다 — 컬럼 타입으로 되돌린다."""
    if raw is None:
        return None
    if column == "metadata_json":
        return raw
    if column == "id":
        return int(raw)
    if column in ("source", "asset", "ohlc_quality", "close_basis",
                  "source_method", "contract_code"):
        return str(raw)
    if column == "basis_date":
        return date.fromisoformat(str(raw)[:10])
    if column in ("bucket_ts_kst", "published_at", "captured_at"):
        return datetime.fromisoformat(str(raw))
    return Decimal(str(raw))


# ---------------------------------------------------------------------------
# 산출물
# ---------------------------------------------------------------------------

def _fsync_dir(directory: pathlib.Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exclusive(path: pathlib.Path, payload: str) -> None:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RepairAbort(f"파일이 이미 있다: {path}") from exc
    except OSError as exc:
        raise RepairAbort(f"생성 실패 {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise RepairAbort(f"기록 실패 {path}: {exc}") from exc
    _fsync_dir(path.parent)


def write_preimage_artifact(path: pathlib.Path, fp: str, sha_: str) -> None:
    """되돌림의 유일한 기준 — 삭제 **전에** 내구 기록한다."""
    _write_exclusive(path, json.dumps(
        {"preimage_sha256": sha_, "preimage": fp},
        ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def write_commit_receipt(preimage_path: pathlib.Path, result: dict) -> None:
    """commit이 **났다**는 양성 증거.

    ⚠️ **부재는 미commit의 증거가 아니다** — commit 반환 직후 죽으면 파일 상태가
    "삭제 전 사망"과 같다. 그때의 판정은 DB를 preimage와 대조해 한다.
    """
    _write_exclusive(
        preimage_path.with_suffix(preimage_path.suffix + ".committed"),
        json.dumps({"committed_at": datetime.now(timezone.utc).isoformat(),
                    "preimage_sha256": result["preimage_sha"],
                    "postimage_sha256": result["postimage_sha"],
                    "postimage": result["postimage"],
                    "deleted": result["deleted"],
                    "total_before": result["total_before"],
                    "total_after": result["total_after"]},
                   ensure_ascii=False, indent=2, default=str) + "\n")


def load_preimage_artifact(path: pathlib.Path) -> tuple[dict, str]:
    """preimage 산출물 → `(rows, sha)`. **sha를 재계산해 대조**한다."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepairAbort(f"preimage 읽기 실패 {path}: {exc}") from exc
    fp, sha_ = payload.get("preimage"), payload.get("preimage_sha256")
    if not isinstance(fp, str) or not isinstance(sha_, str):
        raise RepairAbort("preimage 산출물 형식이 아니다")
    if sha(fp) != sha_:
        raise RepairAbort("preimage 파일의 sha가 본문과 맞지 않는다 — 손상/변조")
    rows = json.loads(fp)
    if not isinstance(rows, dict):
        raise RepairAbort("preimage 본문이 object가 아니다")
    return rows, sha_


def load_postimage_sha(preimage_path: pathlib.Path, *,
                       expected_preimage_sha: str,
                       allow_missing: bool) -> str:
    """커밋 영수증에서 삭제 직후 상태 지문을 검증해 읽는다."""
    path = preimage_path.with_suffix(preimage_path.suffix + ".committed")
    if not path.is_file():
        if allow_missing:
            return _NO_RECEIPT_SENTINEL
        raise RepairAbort(
            f"삭제 영수증이 없다: {path} — 무엇을 되돌리는지 확인할 수 없다. "
            "그래도 진행하려면 --revert-without-receipt")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepairAbort(f"영수증 읽기 실패 {path}: {exc}") from exc
    if payload.get("preimage_sha256") != expected_preimage_sha:
        raise RepairAbort("영수증이 이 preimage와 연결되지 않는다")
    postimage = payload.get("postimage")
    postimage_sha = payload.get("postimage_sha256")
    if not isinstance(postimage, str) or not isinstance(postimage_sha, str):
        if allow_missing:
            return _NO_RECEIPT_SENTINEL
        raise RepairAbort(
            "영수증에 postimage가 없다 — --revert-without-receipt 필요")
    if sha(postimage) != postimage_sha:
        raise RepairAbort("영수증의 postimage sha가 본문과 맞지 않는다 — 손상/변조")
    return postimage_sha


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="KRX 시간봉 4버킷 삭제 (2026-08-14 08~11시 KST, 원자)")
    p.add_argument("--write", action="store_true",
                   help="미지정 시 dry-run (잠금·사전조건·지문만, 변경 0)")
    p.add_argument("--allow-production-write", action="store_true")
    p.add_argument("--preimage-out",
                   help="삭제 전 상태를 기록할 경로 (기존 파일이면 거부)")
    p.add_argument("--expect-preimage-sha",
                   help="dry-run이 출력한 preimage_sha. --write 시 필수")
    p.add_argument("--revert-from",
                   help="preimage 산출물 경로. 지정 시 **복원 모드**")
    p.add_argument("--revert-without-receipt", action="store_true",
                   help="커밋 영수증 없이 복원 (약한 수동 비상 경로)")
    return p


def _validate_cli(args) -> None:
    if args.revert_from:
        if args.write:
            raise RepairAbort("--revert-from 과 --write 는 함께 쓸 수 없다")
        if args.expect_preimage_sha:
            raise RepairAbort("--revert-from 은 파일 안의 sha 를 쓴다")
        return
    if args.revert_without_receipt:
        raise RepairAbort("--revert-without-receipt 는 --revert-from 과 함께만 쓴다")
    if not args.preimage_out:
        raise RepairAbort("삭제/dry-run에는 --preimage-out 경로가 필요하다")
    if not args.write:
        if args.expect_preimage_sha:
            raise RepairAbort("--expect-preimage-sha 는 --write 와 함께만 의미가 있다")
        return
    if not args.expect_preimage_sha:
        raise RepairAbort(
            "--write 에는 --expect-preimage-sha 가 필요하다 — dry-run 이 검토한 "
            "상태와 삭제 대상을 결속해야 한다")
    if len(args.expect_preimage_sha) != 64:
        raise RepairAbort("--expect-preimage-sha 는 sha256 64자여야 한다")


def _run_revert(args, SessionLocal) -> int:
    preimage_path = pathlib.Path(args.revert_from)
    rows, sha_ = load_preimage_artifact(preimage_path)
    postimage_sha = load_postimage_sha(
        preimage_path, expected_preimage_sha=sha_,
        allow_missing=args.revert_without_receipt)
    plan = build_restore_plan(rows)
    print(json.dumps({"mode": "restore-plan", "preimage_sha256": sha_,
                      "buckets": [p["bucket_ts_kst"] for p in plan]},
                     ensure_ascii=False, indent=2, default=str))
    db = SessionLocal()
    phase = PHASE_PRE_COMMIT
    try:
        adapters = build_adapters(db)
        result = restore_all(
            plan=plan, expected_sha=sha_,
            expected_postimage_sha=postimage_sha,
            lock_and_read=adapters["lock_and_read"],
            apply_insert=adapters["apply_insert"],
            reread=adapters["reread"])
        phase = PHASE_COMMITTING
        db.commit()
        phase = PHASE_COMMITTED
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        if phase == PHASE_COMMITTED:
            print(json.dumps({"status": COMMITTED_RECEIPT_UNVERIFIED,
                              "error": str(exc)},
                             ensure_ascii=False, default=str), file=sys.stderr)
            return 2
        if phase == PHASE_COMMITTING:
            print(json.dumps({"status": COMMIT_OUTCOME_UNVERIFIED,
                              "error": str(exc),
                              "note": "복원 commit 결과 **미확정** — rollback 하지 않았다"},
                             ensure_ascii=False, default=str), file=sys.stderr)
            return 2
        db.rollback()
        print(json.dumps({"aborted": str(exc)}, ensure_ascii=False, default=str),
              file=sys.stderr)
        return 1
    finally:
        db.close()


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        _validate_cli(args)
    except RepairAbort as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1

    from app.database import DATABASE_URL, SessionLocal

    mutates = bool(args.write or args.revert_from)
    try:
        check_production_write_guard(DATABASE_URL, mutates=mutates,
                                     allow_production=args.allow_production_write)
    except RepairAbort as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1

    if args.revert_from:
        try:
            return _run_revert(args, SessionLocal)
        except RepairAbort as exc:
            print(f"[abort] revert: {exc}", file=sys.stderr)
            return 1

    preimage_path = pathlib.Path(args.preimage_out)
    db = SessionLocal()
    phase = PHASE_PRE_COMMIT
    try:
        adapters = build_adapters(db)
        result = delete_all(
            write=args.write,
            expected_preimage_sha=args.expect_preimage_sha,
            persist_preimage=lambda fp, s: write_preimage_artifact(
                preimage_path, fp, s),
            **adapters)
        if args.write:
            phase = PHASE_COMMITTING
            db.commit()
            phase = PHASE_COMMITTED
            write_commit_receipt(preimage_path, result)
        else:
            db.rollback()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        if phase == PHASE_COMMITTED:
            print(json.dumps(
                {"status": COMMITTED_RECEIPT_UNVERIFIED, "error": str(exc),
                 "preimage": str(preimage_path),
                 "note": "삭제는 **완료**됐다. 영수증/출력만 실패."},
                ensure_ascii=False, default=str), file=sys.stderr)
            return 2
        if phase == PHASE_COMMITTING:
            print(json.dumps(
                {"status": COMMIT_OUTCOME_UNVERIFIED, "error": str(exc),
                 "preimage": str(preimage_path),
                 "note": "commit 결과 **미확정**이다. rollback 하지 않았다."},
                ensure_ascii=False, default=str), file=sys.stderr)
            return 2
        db.rollback()
        print(json.dumps({"aborted": str(exc)}, ensure_ascii=False, default=str),
              file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
