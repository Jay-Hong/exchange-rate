#!/usr/bin/env python3
"""
atomic_cutover_control + atomic_cutover_asset 테이블 생성 + idempotent seed (PR D / P1b — C6-1).

배경:
  - P1_COMMON_BASE_DESIGN.md §15 cutover state machine(per-asset publish_state + readiness) +
    §18 control plane(global bootstrap session/generation/status + migration lease).
  - app/models.py의 AtomicCutoverControl / AtomicCutoverAsset ORM class 정의 기준.
  - A1 atomic_write_control와 **분리**된 cutover-state table (A1 row는 live-read라 무접촉).

⚠️ 이 script가 두 cutover table의 운영(non-test) 유일 생성 경로다 (C6-1 behavior-change-0 핵심):
  운영 진입점(main.py import / backfill_history.py 등)은 app/database.create_all_app_tables를
  쓰고, 이 helper는 두 table을 create_all에서 제외한다(database.CREATE_ALL_EXCLUDE_TABLES).
  따라서 import/script 시점에 운영 DB로 신규 CHECK DDL이 emit되지 않는다.
  → 배포 순서와 무관하게 언제 실행해도 무방. C6-1엔 이를 read/소비하는 live writer가 없어 무해.

사용법:
  python scripts/migrate_atomic_cutover.py [--dry-run] [--dialect postgresql|sqlite]

주의:
  - 신규 빈 테이블 2개 추가 + idempotent seed(control singleton 1-row + asset 3-row)라 lock 거의 없음.
  - **seed는 insert-if-missing 전용** (upsert-to-blocked 금지) — 이미 row가 있으면 값을 건드리지
    않는다. readiness/flip 후 이 migration을 재실행해도 publish_state/bootstrap_status를 리셋하지 않음.
  - CHECK(id=1 / status enum / generation>=0 / lease paired-null / publish_state enum / asset enum /
    membership / payload consistency)가 중복·오염 seed를 구조적으로 차단.
  - --dry-run은 DB 연결 없이 두 table의 DDL만 출력.
"""

# 표준 라이브러리
import argparse
import os
import sys

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# cutover per-asset domain (CHECK enum과 일치 — §15 first cutover all-at-once 3 asset)
_CUTOVER_ASSETS = ("usd-krw", "jpy-krw", "eur-krw")


def get_dry_run_ddl(dialect_name: str = "postgresql") -> str:
    """DB 연결 없이 dialect별 두 table CREATE DDL 생성 (--dry-run). CHECK 포함."""
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.models import AtomicCutoverAsset, AtomicCutoverControl

    if dialect_name == "postgresql":
        from sqlalchemy.dialects import postgresql
        dialect = postgresql.dialect()
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects import sqlite
        dialect = sqlite.dialect()
    else:
        raise ValueError(f"지원하지 않는 dialect: {dialect_name}")

    parts = []
    for model in (AtomicCutoverControl, AtomicCutoverAsset):
        table = model.__table__
        parts.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:  # 현재 Index 없음 — 미래 대비 루프 유지
            parts.append(str(CreateIndex(index).compile(dialect=dialect)))
    return "\n".join(parts)


def seed_cutover_control(db) -> str:
    """singleton row id=1 insert-if-missing (status=idle/gen=0/format=1). 이미 있으면 미변경.

    Returns: 'inserted' | 'exists'. (값 리셋 금지 — readiness 후 rerun 안전.)
    """
    from app.models import AtomicCutoverControl

    existing = db.query(AtomicCutoverControl).filter(AtomicCutoverControl.id == 1).one_or_none()
    if existing is not None:
        return "exists"
    db.add(AtomicCutoverControl(
        id=1, cutover_row_format_version=1, bootstrap_generation=0, bootstrap_status="idle",
    ))
    db.commit()
    return "inserted"


def seed_cutover_assets(db) -> dict:
    """3 asset row insert-if-missing (publish_state=blocked). 이미 있으면 미변경.

    Returns: {asset: 'inserted'|'exists'}. (state 리셋 금지.)
    """
    from app.models import AtomicCutoverAsset

    result = {}
    for asset in _CUTOVER_ASSETS:
        existing = db.query(AtomicCutoverAsset).filter(AtomicCutoverAsset.asset == asset).one_or_none()
        if existing is not None:
            result[asset] = "exists"
            continue
        db.add(AtomicCutoverAsset(asset=asset, publish_state="blocked"))
        result[asset] = "inserted"
    db.commit()
    return result


def _seed() -> None:
    """control singleton + 3 asset seed (명시 session + try/finally close)."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        control = seed_cutover_control(db)
        print(f"  [SEED] atomic_cutover_control id=1: {control}")
        assets = seed_cutover_assets(db)
        print(f"  [SEED] atomic_cutover_asset: {assets}")
    finally:
        db.close()


def _verify_check_constraints(inspector, table_name: str, expected: set, db_type: str) -> None:
    """기존 table CHECK 존재 검증 (control-plane drift 방어). PG는 누락 시 abort, sqlite는 warning."""
    try:
        present = {c.get("name") for c in inspector.get_check_constraints(table_name)}
    except Exception:
        present = set()
    missing = expected - present
    if not missing:
        print(f"  [OK] {table_name} CHECK {len(expected)}개 확인")
        return
    if db_type == "postgresql":
        raise SystemExit(f"[ABORT] {table_name} CHECK 누락 (무결성 위반): {sorted(missing)}")
    print(f"  [WARN] {table_name} CHECK introspection 누락 (sqlite 제약일 수 있음): {sorted(missing)}")


def apply_live() -> None:
    """실제 DB에 두 table 생성 (checkfirst) + idempotent seed."""
    from sqlalchemy import inspect

    from app.database import engine
    from app.models import AtomicCutoverAsset, AtomicCutoverControl

    inspector = inspect(engine)
    db_url = str(engine.url)
    db_type = "postgresql" if "postgresql" in db_url else "sqlite"
    print(f"DB 타입: {db_type}\n")

    checks = {
        "atomic_cutover_control": {
            "ck_atomic_cutover_control_singleton", "ck_atomic_cutover_control_status",
            "ck_atomic_cutover_control_generation", "ck_atomic_cutover_control_format_version",
            "ck_atomic_cutover_control_lease_paired",
        },
        "atomic_cutover_asset": {
            "ck_atomic_cutover_asset_publish_state", "ck_atomic_cutover_asset_asset",
            "ck_atomic_cutover_asset_membership", "ck_atomic_cutover_asset_payload_consistency",
        },
    }
    for model in (AtomicCutoverControl, AtomicCutoverAsset):
        table_name = model.__tablename__
        if table_name in inspector.get_table_names():
            print(f"[SKIP] '{table_name}' 이미 존재")
            _verify_check_constraints(inspector, table_name, checks[table_name], db_type)
        else:
            print(f"[CREATE] '{table_name}' 생성... (CHECK {len(checks[table_name])}개 포함)")
            model.__table__.create(bind=engine, checkfirst=True)
            print(f"  [OK] '{table_name}' 생성 완료")

    _seed()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="atomic_cutover_control + atomic_cutover_asset 생성 + seed (PR D / P1b C6-1)"
    )
    parser.add_argument("--dry-run", action="store_true", help="DB 연결 없이 DDL만 출력")
    parser.add_argument("--dialect", choices=["postgresql", "sqlite"], default="postgresql",
                        help="--dry-run DDL dialect (기본: postgresql)")
    args = parser.parse_args()

    if args.dry_run:
        print(f"모드: DRY-RUN ({args.dialect} dialect, DB 연결 없음)\n")
        print(get_dry_run_ddl(args.dialect))
        print("\n[DRY-RUN] 실제 실행 안 함.")
    else:
        print("모드: LIVE (두 table create checkfirst + idempotent seed)")
        apply_live()
    print("\n완료!")


if __name__ == "__main__":
    main()
