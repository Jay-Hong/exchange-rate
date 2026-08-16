"""KRX 일봉 2일자 repair — 만기 휴장 보정으로 드러난 잘못된/누락 행 교정.

## 무엇을 고치는가 (정확히 2건, allowlist 고정)

만기 계산에 휴장 보정이 들어가면서 `[prev.expiry, this.expiry)` segment 경계가
이동했다. 주중 기준 영향 날짜는 **2일뿐**이다:

  - `2026-02-13` — 기존 행이 **A75602**(2월물). 보정 후 그날은 A75602의 만기일이라
    `boundary=next` 정책상 **A75603**(3월물) 구간이다. → **UPDATE**
  - `2026-08-14` — 행 **부재**. 그날은 A75608 만기일이므로 **A75609** 구간인데,
    구 코드가 8/17을 만기로 알고 있어 close finalizer가 A75608 identity로 gate에
    걸려 아무 행도 남기지 못했다. → **INSERT**

(달력 날짜로는 2/13~15·8/14~16 6일의 계약 배정이 바뀌지만, 주말은 KRX 거래일이
아니라 애초에 행이 없다.)

## 왜 기존 백필로 못 하는가

`scripts/backfill_krx_openapi_source_daily_rates.py`의 write core는 **insert-only**
이고(`write_krx_gap_rows` — "기존 matched row update/delete 0"), comparator는
contract 불일치를 `hard_issues`로 올려 실행 전체를 중단시킨다. 즉 2/13이 A75602로
남아 있는 한 그 창의 dry-run이 통과할 수 없고, 8/14 삽입도 같은 실행에서 막힌다.

## 안전 계약

  - **allowlist 2일자만**. 그 밖의 날짜는 인자로도 지정할 수 없다.
  - **fresh refetch** — dry-run 산출물을 재사용하지 않는다. stale manifest로
    쓰는 것이 2026-05-25 사고(휴장일 stale 종가를 정상 응답으로 기록)의 형태다.
  - **preimage fingerprint** → `FOR UPDATE` → 조건부 write → **transaction 내부
    post-verify** → commit. 하나라도 어긋나면 rollback.
  - 2/13은 **기존이 정확히 A75602일 때만** UPDATE (아니면 abort).
    8/14는 **부재일 때만** INSERT (unique 충돌은 abort).
  - production write는 명시 flag 2개(`--write` + `--allow-production-write`)를
    모두 요구하고, guard는 **외부 API 호출 전에** 통과해야 한다.

실행 자체는 별도 데이터 정정 GO 대상이다. 이 파일은 도구일 뿐이다.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Optional

SOURCE = "krx"
ASSET = "usd-krw-futures"
SOURCE_METHOD = "krx_openapi_daily"
CLOSE_BASIS = "krx_cf_close_1545"
OHLC_QUALITY = "source_ohlc"


@dataclass(frozen=True)
class RepairTarget:
    """교정 대상 1건. **코드에 고정**되며 CLI로 확장할 수 없다."""
    date_kst: date
    action: str                    # "update" | "insert"
    expected_existing_contract: Optional[str]   # update 시 사전 조건
    target_contract: str


REPAIR_ALLOWLIST: tuple[RepairTarget, ...] = (
    RepairTarget(date(2026, 2, 13), "update", "A75602", "A75603"),
    RepairTarget(date(2026, 8, 14), "insert", None, "A75609"),
)

_ALLOWED_DATES = frozenset(t.date_kst for t in REPAIR_ALLOWLIST)


class RepairAbort(RuntimeError):
    """교정 중단 — 호출자는 transaction을 rollback한다."""


# ---------------------------------------------------------------------------
# 사전 조건 (순수)
# ---------------------------------------------------------------------------

def resolve_target(target_date: date) -> RepairTarget:
    if target_date not in _ALLOWED_DATES:
        raise RepairAbort(
            f"allowlist 밖 날짜: {target_date} — 허용 {sorted(_ALLOWED_DATES)}"
        )
    return next(t for t in REPAIR_ALLOWLIST if t.date_kst == target_date)


def check_precondition(target: RepairTarget, existing: Optional[dict]) -> None:
    """write 직전 사전 조건. 어긋나면 **조용히 건너뛰지 않고** abort한다.

    "이미 고쳐져 있으면 skip"으로 만들면 재실행이 조용해지지만, **다른 무언가가
    그 행을 바꿔 놓은 경우와 구분되지 않는다**. 교정은 일회성이므로 상태가
    예상과 다르면 사람이 봐야 한다.
    """
    if target.action == "update":
        if existing is None:
            raise RepairAbort(f"{target.date_kst}: UPDATE 대상 행이 없다")
        got = existing.get("contract_code")
        if got != target.expected_existing_contract:
            raise RepairAbort(
                f"{target.date_kst}: 기존 contract_code={got!r} != "
                f"기대 {target.expected_existing_contract!r} — 이미 교정됐거나 예상 밖 상태"
            )
    elif target.action == "insert":
        if existing is not None:
            raise RepairAbort(
                f"{target.date_kst}: INSERT 대상인데 행이 이미 있다 "
                f"(contract_code={existing.get('contract_code')!r})"
            )
    else:  # pragma: no cover — dataclass가 코드에 고정돼 도달 불가
        raise RepairAbort(f"unknown action: {target.action!r}")


def validate_fetched_row(target: RepairTarget, row: dict) -> None:
    """공식 API에서 받은 행이 교정 대상과 정합한가 (write 전 fail-closed)."""
    if row.get("date_kst") != target.date_kst:
        raise RepairAbort(
            f"fetch date_kst={row.get('date_kst')} != 대상 {target.date_kst}"
        )
    if row.get("contract_code") != target.target_contract:
        raise RepairAbort(
            f"fetch contract_code={row.get('contract_code')!r} != "
            f"대상 {target.target_contract!r}"
        )
    for field in ("close", "high", "low"):
        value = row.get(field)
        if value is None:
            raise RepairAbort(f"{field} 누락 — 불완전 OHLC는 write하지 않는다")
        if Decimal(str(value)) <= 0:
            raise RepairAbort(f"{field}={value} 비양수")
    close = Decimal(str(row["close"]))
    high = Decimal(str(row["high"]))
    low = Decimal(str(row["low"]))
    if not (low <= close <= high):
        raise RepairAbort(f"OHLC 순서 위반: low={low} close={close} high={high}")
    if row.get("rate") is not None and Decimal(str(row["rate"])) != close:
        raise RepairAbort("rate != close (invariant 위반)")


def verify_post_state(target: RepairTarget, row: Optional[dict],
                      fetched: Optional[dict] = None) -> None:
    """**transaction 내부** post-verify. 실패 시 rollback.

    `fetched`를 주면 **가져온 값이 실제로 반영됐는지**까지 본다. contract와
    provenance만 검사하면, write가 contract만 바꾸고 기존 가격을 남겨도
    (기존 행도 `rate == close`이므로) 통과한다 — codex 리뷰 지적.
    """
    if row is None:
        raise RepairAbort(f"{target.date_kst}: write 후 행이 없다")
    if row.get("contract_code") != target.target_contract:
        raise RepairAbort(
            f"{target.date_kst}: post contract_code={row.get('contract_code')!r} "
            f"!= {target.target_contract!r}"
        )
    if row.get("source_method") != SOURCE_METHOD:
        raise RepairAbort(f"post source_method={row.get('source_method')!r}")
    if row.get("close_basis") != CLOSE_BASIS:
        raise RepairAbort(f"post close_basis={row.get('close_basis')!r}")
    if row.get("ohlc_quality") != OHLC_QUALITY:
        raise RepairAbort(f"post ohlc_quality={row.get('ohlc_quality')!r}")
    for nullable in ("basis_date", "published_at"):
        if row.get(nullable) is not None:
            raise RepairAbort(f"post {nullable}가 None이 아니다 (KRX 방향 위반)")
    rate, close = row.get("rate"), row.get("close")
    if rate is None or close is None or Decimal(str(rate)) != Decimal(str(close)):
        raise RepairAbort(f"post rate({rate}) != close({close})")
    if fetched is not None:
        for field in ("close", "high", "low"):
            want, got = fetched.get(field), row.get(field)
            if got is None or Decimal(str(got)) != Decimal(str(want)):
                raise RepairAbort(
                    f"post {field}={got} != fetched {want} — write가 값을 반영하지 않았다"
                )


def preimage_fingerprint(rows: dict[date, Optional[dict]]) -> str:
    """교정 전 상태의 논리 지문 — 되돌릴 때 대조 기준.

    snapshot(DB 전체 복구 앵커)과 별개 축이다. snapshot은 복구 수단이고 이건
    "무엇이 있었는가"의 기록이라, 사후에 rollback 필요성을 판단할 때 쓴다.
    """
    payload = {
        str(d): (None if r is None else {
            k: (str(r[k]) if r.get(k) is not None else None)
            for k in ("contract_code", "rate", "close", "high", "low",
                      "close_basis", "source_method", "ohlc_quality")
        })
        for d, r in sorted(rows.items())
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# production guard — 외부 API 호출 **전에** 통과해야 한다
# ---------------------------------------------------------------------------

def check_production_write_guard(database_url: str, *, write: bool,
                                 allow_production: bool) -> None:
    """production DB에 `--write`를 실수로 겨눴을 때 **1줄 + 중단**.

    guard가 늦게 걸리면 이미 외부 API를 호출한 뒤라 token rotation 위험이 있다.
    dialect/host는 출력하지 않는다(보안 원칙).
    """
    if not write:
        return
    is_production = database_url.startswith("postgresql")
    if is_production and not allow_production:
        raise RepairAbort(
            "production DB 대상 write는 --allow-production-write 필요 (중단)"
        )


# ---------------------------------------------------------------------------
# transaction core (주입 가능 — 테스트는 in-memory)
# ---------------------------------------------------------------------------

def repair_one(
    target: RepairTarget,
    *,
    read_existing: Callable[[date], Optional[dict]],
    fetch_official: Callable[[RepairTarget], dict],
    write_row: Callable[[RepairTarget, dict], None],
) -> dict:
    """단일 대상 교정. 예외는 그대로 올려 caller가 rollback하게 한다.

    순서가 계약이다: 사전 조건 → **fresh fetch** → 입력 검증 → write →
    post-verify. fetch를 사전 조건보다 앞에 두면 abort할 상태에서도 외부 API를
    때리고, post-verify를 commit 뒤로 미루면 되돌릴 수 없다.
    """
    existing = read_existing(target.date_kst)
    check_precondition(target, existing)

    fetched = fetch_official(target)
    validate_fetched_row(target, fetched)

    write_row(target, fetched)

    verify_post_state(target, read_existing(target.date_kst), fetched)
    return {"date_kst": str(target.date_kst), "action": target.action,
            "contract_code": target.target_contract,
            "close": str(fetched["close"])}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="KRX 일봉 2일자 repair (2026-02-13 UPDATE / 2026-08-14 INSERT)")
    p.add_argument("--date", required=True,
                   help=f"교정 날짜 — allowlist: {sorted(str(d) for d in _ALLOWED_DATES)}")
    p.add_argument("--write", action="store_true",
                   help="미지정 시 dry-run (사전 조건·fetch·검증만, write 0)")
    p.add_argument("--allow-production-write", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover — CLI 배선
    args = build_parser().parse_args(argv)
    try:
        target = resolve_target(date.fromisoformat(args.date))
    except (RepairAbort, ValueError) as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"target": str(target.date_kst), "action": target.action,
                      "contract": target.target_contract, "write": args.write},
                     ensure_ascii=False))
    print("실행 경로(DB 세션·공식 API fetch)는 데이터 정정 GO 시점에 배선한다 — "
          "순수 계약과 transaction core는 이 모듈에 구현·테스트돼 있다.", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
