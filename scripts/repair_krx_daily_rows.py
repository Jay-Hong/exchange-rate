"""KRX 일봉 2일자 repair — 만기 휴장 보정으로 드러난 잘못된/누락 행 교정.

## 무엇을 고치는가 (정확히 2건, allowlist 고정)

만기 계산에 휴장 보정이 들어가면서 `[prev.expiry, this.expiry)` segment 경계가
이동했다. 주중 기준 영향 날짜는 **2일뿐**이다:

  - `2026-02-13` — 기존 행이 **A75602**(2월물). 보정 후 그날은 A75602의 만기일이라
    `boundary=next` 정책상 **A75603**(3월물) 구간이다. → **UPDATE**
  - `2026-08-14` — 행 **부재**. 그날은 A75608 만기일이므로 **A75609** 구간인데,
    구 코드가 8/17을 만기로 알고 있어 close finalizer가 A75608 identity로 gate에
    걸려 아무 행도 남기지 못했다. → **INSERT**

## 왜 기존 백필로 못 하는가

`backfill_krx_openapi_source_daily_rates`의 write core는 **insert-only**이고
(`write_krx_gap_rows` — "기존 matched row update/delete 0"), comparator는 contract
불일치를 `hard_issues`로 올려 실행 전체를 중단시킨다.

## 안전 계약 (전부 codex 감사에서 도출)

  - **allowlist 2일자만**, CLI로 확장 불가.
  - **한 transaction, 2일자 원자 처리**. 2/13만 성공하고 8/14가 실패하면 반쪽
    교정 상태가 남고, 그 상태는 사전 조건을 더 이상 만족하지 않아 재실행도 막힌다.
  - **`FOR UPDATE` 먼저, 그 다음 preimage**. 순서가 반대면 잠금 전에 읽은 값과
    실제 덮어쓸 값이 다를 수 있다.
  - **full preimage** — `SourceDailyRate` 전 컬럼(`id` 포함). 범용 `row_to_dict`는
    `id`를 돌려주지 않으므로 **전용 reader**를 쓴다.
  - UPDATE는 **조건부 + rowcount == 1**, INSERT는 **plain INSERT**(공용 `upsert`는
    `ON CONFLICT DO UPDATE`라 경합 행을 덮는다 — 여기서는 unique 충돌이 abort여야 한다).
  - write 후 `flush()` + `expire_all()` → **transaction 내부** post-verify.
  - production guard는 **실제 세션이 쓰는 URL**로 판정한다. 별도 `os.getenv`로
    보면 `.env`에만 URL이 있는 흔한 실행에서 guard가 빈 문자열을 보고 통과한다.

실행은 별도 데이터 정정 GO 대상이다.
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

SOURCE = "krx"
ASSET = "usd-krw-futures"
SOURCE_METHOD = "krx_openapi_daily"
CLOSE_BASIS = "krx_cf_close_1545"
OHLC_QUALITY = "source_ohlc"

# 실행 단계. 예외가 났을 때 **DB가 어떤 상태인지**를 이 값으로 가른다.
#
#   PRE_COMMIT  — commit 호출 전. rollback이 유효하고 write는 없다.
#   COMMITTING  — `commit()` 호출 중. ⚠️ 예외가 나도 **미commit이 아니다**:
#                 서버가 COMMIT을 적용한 직후 연결이 끊겨 ACK만 못 받았을 수
#                 있다(in-doubt). 이때 rollback은 되돌리지 못하고, "중단"으로
#                 적으면 거짓 보고가 된다 (codex 감사 지적).
#   COMMITTED   — `commit()`이 정상 반환. 데이터는 바뀌었다.
PHASE_PRE_COMMIT = "pre_commit"
PHASE_COMMITTING = "committing"
PHASE_COMMITTED = "committed"

# 중단이 아닌 상태들 — 데이터가 바뀌었거나(또는 바뀌었을 수 있고), rollback으로
# 되돌아가지 않는다. exit code 2로 구분한다 (1 = 중단, write 없음).
COMMITTED_RECEIPT_UNVERIFIED = "COMMITTED_BUT_RECEIPT_UNVERIFIED"
COMMIT_OUTCOME_UNVERIFIED = "COMMIT_OUTCOME_UNVERIFIED"


@dataclass(frozen=True)
class RepairTarget:
    """교정 대상 1건. **코드에 고정**되며 CLI로 확장할 수 없다."""
    date_kst: date
    action: str                                  # "update" | "insert"
    expected_existing_contract: Optional[str]    # update 시 사전 조건
    target_contract: str
    target_contract_month: str


REPAIR_ALLOWLIST: tuple[RepairTarget, ...] = (
    RepairTarget(date(2026, 2, 13), "update", "A75602", "A75603", "202603"),
    RepairTarget(date(2026, 8, 14), "insert", None, "A75609", "202609"),
)

_ALLOWED_DATES = frozenset(t.date_kst for t in REPAIR_ALLOWLIST)

# preimage에 담는 컬럼 — `SourceDailyRate` **전 컬럼**. 일부만 담으면 "무엇이
# 있었는가"를 재구성할 수 없다. `id`는 어떤 물리 행이었는지를 고정한다.
PREIMAGE_COLUMNS = (
    "id", "source", "asset", "date_kst", "rate", "high", "low", "close",
    "ohlc_quality", "close_basis", "source_method", "contract_code",
    "basis_date", "published_at", "captured_at", "metadata_json",
)


class RepairAbort(RuntimeError):
    """교정 중단 — 호출자는 transaction을 rollback한다."""


# ---------------------------------------------------------------------------
# 순수 계약
# ---------------------------------------------------------------------------

def resolve_target(target_date: date) -> RepairTarget:
    if target_date not in _ALLOWED_DATES:
        raise RepairAbort(
            f"allowlist 밖 날짜: {target_date} — 허용 {sorted(_ALLOWED_DATES)}")
    return next(t for t in REPAIR_ALLOWLIST if t.date_kst == target_date)


def check_precondition(target: RepairTarget, existing: Optional[dict]) -> None:
    """어긋나면 **조용히 건너뛰지 않고** abort.

    "이미 고쳐져 있으면 skip"으로 만들면 재실행이 조용해지지만, 다른 무언가가
    그 행을 바꿔 놓은 경우와 구분되지 않는다.
    """
    if target.action == "update":
        if existing is None:
            raise RepairAbort(f"{target.date_kst}: UPDATE 대상 행이 없다")
        got = existing.get("contract_code")
        if got != target.expected_existing_contract:
            raise RepairAbort(
                f"{target.date_kst}: 기존 contract_code={got!r} != "
                f"기대 {target.expected_existing_contract!r} — 이미 교정됐거나 예상 밖 상태")
    elif target.action == "insert":
        if existing is not None:
            raise RepairAbort(
                f"{target.date_kst}: INSERT 대상인데 행이 이미 있다 "
                f"(contract_code={existing.get('contract_code')!r})")
    else:  # pragma: no cover — dataclass가 코드에 고정돼 도달 불가
        raise RepairAbort(f"unknown action: {target.action!r}")


def validate_fetched_row(target: RepairTarget, row: dict) -> None:
    """공식 API 변환 결과가 교정 대상과 정합한가 (write 전 fail-closed)."""
    if row.get("source") != SOURCE or row.get("asset") != ASSET:
        raise RepairAbort(f"source/asset 불일치: {row.get('source')}/{row.get('asset')}")
    if row.get("date_kst") != target.date_kst:
        raise RepairAbort(f"fetch date_kst={row.get('date_kst')} != {target.date_kst}")
    if row.get("contract_code") != target.target_contract:
        raise RepairAbort(
            f"fetch contract_code={row.get('contract_code')!r} != "
            f"{target.target_contract!r}")
    for field in ("close", "high", "low"):
        value = row.get(field)
        if value is None:
            raise RepairAbort(f"{field} 누락 — 불완전 OHLC는 write하지 않는다")
        if Decimal(str(value)) <= 0:
            raise RepairAbort(f"{field}={value} 비양수")
    close, high, low = (Decimal(str(row[k])) for k in ("close", "high", "low"))
    if not (low <= close <= high):
        raise RepairAbort(f"OHLC 순서 위반: low={low} close={close} high={high}")
    if row.get("rate") is None or Decimal(str(row["rate"])) != close:
        raise RepairAbort("rate != close (invariant 위반)")
    if row.get("basis_date") is not None or row.get("published_at") is not None:
        raise RepairAbort("KRX 방향은 basis_date·published_at이 None이어야 한다")


def verify_post_state(target: RepairTarget, row: Optional[dict],
                      fetched: dict) -> None:
    """**transaction 내부** post-verify. 실패 시 caller가 rollback."""
    if row is None:
        raise RepairAbort(f"{target.date_kst}: write 후 행이 없다")
    for field, want in (("contract_code", target.target_contract),
                        ("source_method", SOURCE_METHOD),
                        ("close_basis", CLOSE_BASIS),
                        ("ohlc_quality", OHLC_QUALITY)):
        if row.get(field) != want:
            raise RepairAbort(f"{target.date_kst}: post {field}={row.get(field)!r} != {want!r}")
    for nullable in ("basis_date", "published_at"):
        if row.get(nullable) is not None:
            raise RepairAbort(f"post {nullable}가 None이 아니다 (KRX 방향 위반)")
    rate, close = row.get("rate"), row.get("close")
    if rate is None or close is None or Decimal(str(rate)) != Decimal(str(close)):
        raise RepairAbort(f"post rate({rate}) != close({close})")
    # 가져온 값이 **실제로 반영**됐는가. contract·provenance만 보면 기존 행도
    # rate==close이므로 contract만 갈아끼운 write가 통과한다.
    for field in ("close", "high", "low"):
        want, got = fetched.get(field), row.get(field)
        if got is None or Decimal(str(got)) != Decimal(str(want)):
            raise RepairAbort(
                f"post {field}={got} != fetched {want} — write가 값을 반영하지 않았다")
    # provenance metadata도 **교체**됐는가. 가격만 보면 구 계약월의 metadata가
    # 남은 채로 통과한다 (codex 감사 지적).
    if row.get("metadata_json") != fetched.get("metadata_json"):
        raise RepairAbort(
            f"post metadata_json={row.get('metadata_json')!r} != "
            f"fetched {fetched.get('metadata_json')!r} — provenance 미교체")


def preimage_fingerprint(rows: dict[date, Optional[dict]]) -> str:
    """교정 전 상태의 논리 지문 — **전 컬럼**. 되돌릴 때 대조 기준."""
    payload = {
        str(d): (None if r is None else {
            k: (None if r.get(k) is None else
                (r[k] if k == "metadata_json" else str(r[k])))
            for k in PREIMAGE_COLUMNS
        })
        for d, r in sorted(rows.items())
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def preimage_sha(fingerprint: str) -> str:
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def check_production_write_guard(database_url: str, *, write: bool,
                                 allow_production: bool) -> None:
    """production DB에 `--write`를 실수로 겨눴을 때 **1줄 + 중단**.

    ⚠️ 판정 대상은 **실제 세션이 bind된 URL**이어야 한다. 별도로 `os.getenv`를
    읽으면 URL이 `.env`에만 있는 흔한 실행에서 빈 문자열을 보고 통과하는데,
    그 뒤 `app.database` import가 dotenv를 로드해 세션은 production에 붙는다
    (codex 감사 지적). dialect/host는 출력하지 않는다(보안 원칙).
    """
    if not write:
        return
    if database_url.startswith("postgresql") and not allow_production:
        raise RepairAbort(
            "production DB 대상 write는 --allow-production-write 필요 (중단)")


# ---------------------------------------------------------------------------
# transaction core — DB 어댑터 주입 (테스트는 실제 PostgreSQL로 검증)
# ---------------------------------------------------------------------------

def repair_all(
    *,
    lock_and_read: Callable[[date], Optional[dict]],
    fetch_official: Callable[[RepairTarget], dict],
    apply_update: Callable[[RepairTarget, dict], int],
    apply_insert: Callable[[RepairTarget, dict], None],
    reread: Callable[[date], Optional[dict]],
    persist_preimage: Callable[[str, str], None],
    write: bool,
) -> dict:
    """2일자를 **한 단위**로 처리. 예외는 그대로 올려 caller가 rollback한다.

    순서가 계약이다:
      1. **잠금 후** 전 대상 read → full preimage 고정
      2. 전 대상 사전 조건 확인 (하나라도 위반이면 write 0)
      3. 전 대상 fresh fetch + 입력 검증 (아직 write 없음)
      4. **preimage를 디스크에 내구 기록** (write 전 — 아래 참조)
      5. write (UPDATE는 rowcount==1 강제, INSERT는 unique 충돌 시 abort)
      6. 전 대상 post-verify (같은 transaction 안)

    4가 5보다 앞서야 하는 이유: preimage를 메모리에 들고 있다가 commit 뒤에
    stdout으로만 내보내면, commit 성공 직후 프로세스가 죽거나 pipe가 끊겼을 때
    **데이터는 바뀌었는데 되돌릴 기준이 없다** (codex 감사 지적). 기록이
    실패하면 애초에 write를 하지 않는다.
    """
    preimage = {t.date_kst: lock_and_read(t.date_kst) for t in REPAIR_ALLOWLIST}
    fingerprint = preimage_fingerprint(preimage)

    for target in REPAIR_ALLOWLIST:
        check_precondition(target, preimage[target.date_kst])

    fetched: dict[date, dict] = {}
    for target in REPAIR_ALLOWLIST:
        row = fetch_official(target)
        validate_fetched_row(target, row)
        fetched[target.date_kst] = row

    plan = [{"date_kst": str(t.date_kst), "action": t.action,
             "contract_code": t.target_contract,
             "close": str(fetched[t.date_kst]["close"])}
            for t in REPAIR_ALLOWLIST]

    if not write:
        return {"mode": "dry-run", "preimage_sha": preimage_sha(fingerprint),
                "preimage": fingerprint, "planned": plan}

    # write 전에 되돌림 기준을 디스크에 못박는다. 실패하면 여기서 멈춘다.
    persist_preimage(fingerprint, preimage_sha(fingerprint))

    for target in REPAIR_ALLOWLIST:
        row = fetched[target.date_kst]
        if target.action == "update":
            rowcount = apply_update(target, row)
            if rowcount != 1:
                raise RepairAbort(
                    f"{target.date_kst}: 조건부 UPDATE rowcount={rowcount} != 1 "
                    "— 사전 조건 확인 이후 행이 바뀌었다")
        else:
            apply_insert(target, row)

    for target in REPAIR_ALLOWLIST:
        verify_post_state(target, reread(target.date_kst), fetched[target.date_kst])

    return {"mode": "write", "preimage_sha": preimage_sha(fingerprint),
            "preimage": fingerprint, "applied": plan}


def build_db_adapters(db, fetch_fn):
    """SQLAlchemy 세션 + KRX 공식 API 어댑터. 순수 계약은 위에 있고 여기는 배선만.

    Args:
        db: SQLAlchemy `Session` (caller가 commit/rollback 소유).
        fetch_fn: `date -> list[raw OutBlock_1 rows]` (KRX fut_bydd_trd).
    """
    from sqlalchemy import func, insert as sa_insert, select, update

    from app.models import SourceDailyRate

    import backfill_krx_openapi_source_daily_rates as krx_api

    def _row_to_full_dict(row) -> dict:
        # ⚠️ 범용 `source_daily_rates.row_to_dict`는 `id`를 돌려주지 않아
        #    full preimage가 성립하지 않는다 (codex 감사 지적). 전용 reader를 쓴다.
        return {c: getattr(row, c) for c in PREIMAGE_COLUMNS}

    def lock_and_read(d: date) -> Optional[dict]:
        """**잠금 먼저, 그 다음 읽기.** 순서가 반대면 잠금 전 값과 덮어쓸 값이 갈린다.

        행이 없을 때도 `FOR UPDATE`는 잠글 대상이 없다(gap lock 아님) — 그래서
        INSERT 경합은 잠금이 아니라 **unique 충돌 abort**로 막는다.
        """
        # ORM select + `with_for_update()`를 쓴다. 손으로 쓴 SQL은 테이블 이름을
        # 문자열로 박아 넣어 schema 라우팅(테스트 격리)을 우회한다.
        db.execute(
            select(SourceDailyRate.id).where(
                SourceDailyRate.source == SOURCE,
                SourceDailyRate.asset == ASSET,
                SourceDailyRate.date_kst == d
            ).with_for_update())
        row = db.execute(
            select(SourceDailyRate).where(
                SourceDailyRate.source == SOURCE,
                SourceDailyRate.asset == ASSET,
                SourceDailyRate.date_kst == d)
        ).scalar_one_or_none()
        return None if row is None else _row_to_full_dict(row)

    def fetch_official(target: RepairTarget) -> dict:
        """실제 primitive 경로 — fetch → exact select → 변환. 중간 생략 없음."""
        raw_rows = fetch_fn(target.date_kst)
        picked = krx_api.select_usd_front_month_row(
            raw_rows, target.target_contract_month)
        if picked is None:
            raise RepairAbort(
                f"{target.date_kst}: {target.target_contract_month} front-month row 없음")
        if picked.get("BAS_DD") != target.date_kst.strftime("%Y%m%d"):
            # 2026-05-25 사고와 동형 — 요청일과 다른 날짜의 값을 쓰지 않는다.
            raise RepairAbort(
                f"{target.date_kst}: BAS_DD={picked.get('BAS_DD')} != 요청일 (stale 응답)")
        contract = _ContractStub(target.target_contract, target.target_contract_month,
                                 _expiry_of(target.target_contract_month))
        return krx_api.krx_row_to_source_daily(picked, contract)

    def apply_update(target: RepairTarget, row: dict) -> int:
        """**조건부** UPDATE — 기존 contract_code가 기대값일 때만. rowcount로 확인.

        `captured_at`은 공용 upsert 정책([app/source_daily_rates.py] `func.now()`)과
        같이 갱신한다 — 그 컬럼의 계약이 "row 마지막 update 시각"이므로, 여기만
        빼면 정정된 행이 정정 전 시각을 달고 남는다 (codex 감사 지적).
        원래 값은 preimage에 보존된다.
        """
        result = db.execute(
            update(SourceDailyRate)
            .where(SourceDailyRate.source == SOURCE,
                   SourceDailyRate.asset == ASSET,
                   SourceDailyRate.date_kst == target.date_kst,
                   SourceDailyRate.contract_code == target.expected_existing_contract)
            .values(rate=row["rate"], close=row["close"], high=row["high"],
                    low=row["low"], ohlc_quality=OHLC_QUALITY,
                    close_basis=CLOSE_BASIS, source_method=SOURCE_METHOD,
                    contract_code=target.target_contract,
                    basis_date=None, published_at=None,
                    metadata_json=row["metadata_json"],
                    captured_at=func.now())
        )
        db.flush()
        db.expire_all()
        return result.rowcount

    def apply_insert(target: RepairTarget, row: dict) -> None:
        """**plain INSERT** — 공용 `upsert`(ON CONFLICT DO UPDATE)를 쓰지 않는다.

        경합 writer가 그 사이 행을 넣었으면 여기서 unique 충돌로 **abort**해야
        하는데, upsert는 그 행을 조용히 덮는다 (codex 감사 지적).
        """
        from sqlalchemy.exc import IntegrityError

        try:
            db.execute(sa_insert(SourceDailyRate).values(
                source=SOURCE, asset=ASSET, date_kst=target.date_kst,
                rate=row["rate"], close=row["close"], high=row["high"],
                low=row["low"], ohlc_quality=OHLC_QUALITY,
                close_basis=CLOSE_BASIS, source_method=SOURCE_METHOD,
                contract_code=target.target_contract,
                basis_date=None, published_at=None,
                metadata_json=row["metadata_json"]))
            db.flush()
        except IntegrityError as exc:
            raise RepairAbort(
                f"{target.date_kst}: INSERT unique 충돌 — 경합 writer가 행을 넣었다") from exc
        db.expire_all()

    def reread(d: date) -> Optional[dict]:
        db.expire_all()
        row = db.execute(
            select(SourceDailyRate).where(
                SourceDailyRate.source == SOURCE,
                SourceDailyRate.asset == ASSET,
                SourceDailyRate.date_kst == d)
        ).scalar_one_or_none()
        return None if row is None else _row_to_full_dict(row)

    return dict(lock_and_read=lock_and_read, fetch_official=fetch_official,
                apply_update=apply_update, apply_insert=apply_insert, reread=reread)


@dataclass(frozen=True)
class _ContractStub:
    """`krx_row_to_source_daily`가 duck typing으로 요구하는 최소 형태."""
    short_code: str
    contract_month: str
    expiry_date: date


def _expiry_of(contract_month: str) -> date:
    from app.sources.kis_master import _compute_expiry_date

    expiry = _compute_expiry_date(contract_month)
    if expiry is None:
        raise RepairAbort(f"만기 계산 실패: {contract_month}")
    return expiry


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="KRX 일봉 2일자 repair (2026-02-13 UPDATE + 2026-08-14 INSERT, 원자)")
    p.add_argument("--write", action="store_true",
                   help="미지정 시 dry-run (잠금·사전조건·fetch·검증만, write 0)")
    p.add_argument("--allow-production-write", action="store_true")
    p.add_argument("--preimage-out", required=True,
                   help="교정 전 상태를 **write 전에** 기록할 경로 (기존 파일이면 거부)")
    return p


def write_preimage_artifact(path: pathlib.Path, fingerprint: str, sha: str) -> None:
    """**배타 생성 + fsync**. 이게 되돌림의 유일한 기준이므로 덮어쓰지 않는다."""
    payload = json.dumps({"preimage_sha256": sha, "preimage": fingerprint},
                         ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RepairAbort(f"preimage 파일이 이미 있다: {path}") from exc
    except OSError as exc:
        raise RepairAbort(f"preimage 생성 실패 {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise RepairAbort(f"preimage 기록 실패 {path}: {exc}") from exc
    _fsync_dir(path.parent)


def _fsync_dir(directory: pathlib.Path) -> None:
    """디렉터리 엔트리까지 내려야 **파일 존재 자체**가 crash를 견딘다."""
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_commit_receipt(preimage_path: pathlib.Path, result: dict) -> None:
    """commit이 **났다**는 양성 증거. 내구 기록한다.

    ⚠️ **부재는 미commit의 증거가 아니다.** `commit()` 반환 직후 receipt 기록
    전에 죽으면 파일 상태가 "write 전 사망"과 똑같다 (codex 감사 지적).
    receipt는 있으면 commit 확정, 없으면 **미확정**이고, 그때의 판정은
    DB를 preimage와 대조하는 read-only 확인으로 한다.
    """
    path = preimage_path.with_suffix(preimage_path.suffix + ".committed")
    payload = json.dumps(
        {"committed_at": datetime.now(timezone.utc).isoformat(),
         "preimage_sha256": result["preimage_sha"],
         "applied": result["applied"]},
        ensure_ascii=False, indent=2, default=str) + "\n"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RepairAbort(f"receipt가 이미 있다: {path}") from exc
    except OSError as exc:
        raise RepairAbort(f"receipt 생성 실패 {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        raise RepairAbort(f"receipt 기록 실패 {path}: {exc}") from exc
    _fsync_dir(path.parent)


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # ⚠️ guard는 **실제 세션이 bind된 URL**로 판정한다. import가 dotenv를 로드하므로
    #    app.database를 먼저 들여온 뒤 그 모듈의 DATABASE_URL을 본다.
    from app.database import DATABASE_URL, SessionLocal

    try:
        check_production_write_guard(DATABASE_URL, write=args.write,
                                     allow_production=args.allow_production_write)
    except RepairAbort as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1

    auth_key = os.getenv("KRX_AUTH_KEY", "")
    if not auth_key:
        print("[abort] KRX_AUTH_KEY 미설정", file=sys.stderr)
        return 1

    import backfill_krx_openapi_source_daily_rates as krx_api

    preimage_path = pathlib.Path(args.preimage_out)
    db = SessionLocal()
    phase = PHASE_PRE_COMMIT
    try:
        adapters = build_db_adapters(db, krx_api.make_krx_fetch_fn(auth_key))
        result = repair_all(
            write=args.write,
            persist_preimage=lambda fp, sha: write_preimage_artifact(
                preimage_path, fp, sha),
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
        # ⚠️ 예외를 일괄 "aborted"로 보고하면 안 된다. 단계에 따라 DB 상태가
        #    다르고, 운영자의 다음 행동이 갈린다 (codex 감사 지적).
        if phase == PHASE_COMMITTED:
            print(json.dumps(
                {"status": COMMITTED_RECEIPT_UNVERIFIED, "error": str(exc),
                 "preimage": str(preimage_path),
                 "note": "DB write는 **완료**됐다. 영수증/출력만 실패. "
                         "실제 상태는 DB를 preimage와 대조해 확인할 것."},
                ensure_ascii=False, default=str), file=sys.stderr)
            return 2
        if phase == PHASE_COMMITTING:
            # in-doubt: 서버가 적용한 뒤 ACK만 유실됐을 수 있다. rollback은
            # 되돌리지 못하므로 부르지 않고, 판정을 DB 대조로 넘긴다.
            print(json.dumps(
                {"status": COMMIT_OUTCOME_UNVERIFIED, "error": str(exc),
                 "preimage": str(preimage_path),
                 "note": "commit 결과 **미확정**이다 (적용됐을 수 있다). "
                         "rollback하지 않았다. DB를 preimage와 대조해 확인할 것."},
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
