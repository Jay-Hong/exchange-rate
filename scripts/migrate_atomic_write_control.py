#!/usr/bin/env python3
"""
atomic_write_control 테이블 생성 + singleton seed 마이그레이션 (PR D / P1b — A1).

배경:
  - P1_COMMON_BASE_DESIGN.md §4 control table (legacy/atomic/halt 3-state mode).
  - app/models.py의 AtomicWriteControl ORM class 정의 기준.
  - 리포 첫 DB CHECK(id=1 / requested_mode enum / activation_epoch non-negative)을
    포함하는 테이블 — 이 script가 CHECK DDL을 명시·audit 경로로 적용.

⚠️ 이 script가 atomic_write_control의 운영(non-test) 유일 생성 경로다 (A1 behavior-change-0 핵심):
  운영 진입점(main.py import / backfill_history.py 등)은 app/database.create_all_app_tables를
  쓰고, 이 helper는 atomic_write_control을 create_all에서 제외한다. 따라서 import/script
  시점에 운영 DB로 신규 CHECK DDL이 emit되지 않는다 (리포 첫 DB CHECK라도 startup 위험 0).
  → 배포 순서와 무관하게 이 script를 배포 전/후 언제 실행해도 무방. 미실행 구간엔
  status endpoint가 control_read_error='row_missing'을 fail-closed surface로 보고하며,
  A1엔 이를 consume하는 writer가 없어 무해.

사용법:
  python scripts/migrate_atomic_write_control.py [--dry-run] [--dialect postgresql|sqlite]

주의:
  - 신규 빈 테이블 추가 + singleton 1-row idempotent seed라 lock 거의 없음, ALTER 없음.
  - production-write guard 불요: migrate_bithumb/krx의 guard는 기존 row UPDATE 사고
    방지용. 여기는 net-new 격리 테이블 CREATE + idempotent single-row INSERT이고,
    CHECK(id=1)이 중복/오염 seed를 구조적으로 차단한다 (data-safety risk 없음).
  - 이미 atomic_write_control 테이블이 있으면 안전 skip, seed도 idempotent.
  - --dry-run은 DB 연결 없이 DDL만 출력 (PostgreSQL dialect 기본).
"""

# 표준 라이브러리
import argparse
import os
import sys

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_dry_run_ddl(dialect_name: str = "postgresql") -> str:
    """DB 연결 없이 dialect별 CREATE TABLE DDL 생성 (--dry-run 용도).

    CHECK 제약 3개가 DDL에 포함되어 출력됨 (리포 첫 CHECK — 적용 전 검토용).
    """
    # 함수 내부 import — DB 연결 회피 (--dry-run 시 engine import도 회피)
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.models import AtomicWriteControl

    if dialect_name == "postgresql":
        from sqlalchemy.dialects import postgresql
        dialect = postgresql.dialect()
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects import sqlite
        dialect = sqlite.dialect()
    else:
        raise ValueError(f"지원하지 않는 dialect: {dialect_name}")

    table = AtomicWriteControl.__table__
    parts = [str(CreateTable(table).compile(dialect=dialect))]

    # control table엔 Index 없음(CHECK만) — 미래 Index 추가 대비 루프 유지
    for index in table.indexes:
        parts.append(str(CreateIndex(index).compile(dialect=dialect)))

    return "\n".join(parts)


def _seed_singleton() -> None:
    """singleton row id=1 idempotent seed (명시 session + try/finally close).

    bootstrap 내부 commit. 운영 audit을 위해 seed 결과 값 출력.
    """
    from app.atomic_write_control import bootstrap_atomic_write_control
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        row = bootstrap_atomic_write_control(db)
        print(
            "  [SEED] singleton row id=1: "
            f"requested_mode={row.requested_mode} / "
            f"activation_epoch={row.activation_epoch} / "
            f"mode_generation={row.mode_generation} / "
            f"required_writer_protocol={row.required_writer_protocol} / "
            f"control_row_format_version={row.control_row_format_version}"
        )
    finally:
        db.close()


def _verify_check_constraints(inspector, table_name: str, db_type: str) -> None:
    """기존 table의 CHECK 3개 존재 검증 (control-plane drift 방어).

    PG는 named CHECK introspection을 신뢰 → 누락 시 abort (SystemExit). SQLite는
    introspection 제약이 있어 누락을 warning만 (운영은 PG). 신규 생성 경로는 항상
    CHECK와 함께 atomic하게 만들어지므로, 이 검증은 외부 변조/부분 생성 방어용.
    """
    expected = {
        "ck_atomic_write_control_singleton",
        "ck_atomic_write_control_requested_mode",
        "ck_atomic_write_control_activation_epoch",
    }
    try:
        present = {c.get("name") for c in inspector.get_check_constraints(table_name)}
    except Exception:
        present = set()
    missing = expected - present
    if not missing:
        print(f"  [OK] CHECK 3개 확인: {sorted(expected)}")
        return
    if db_type == "postgresql":
        raise SystemExit(
            f"[ABORT] control table CHECK 제약 누락 (무결성 위반): {sorted(missing)}. "
            "table 점검/재생성 후 재실행하세요."
        )
    print(f"  [WARN] CHECK introspection 누락 (sqlite 제약일 수 있음): {sorted(missing)}")


def apply_live() -> None:
    """실제 DB에 테이블 생성 (checkfirst) + singleton seed."""
    # 함수 내부 import — --dry-run에서는 import 자체도 회피
    from sqlalchemy import inspect

    from app.database import engine
    from app.models import AtomicWriteControl

    inspector = inspect(engine)
    table_name = AtomicWriteControl.__tablename__

    db_url = str(engine.url)
    db_type = "postgresql" if "postgresql" in db_url else "sqlite"
    print(f"DB 타입: {db_type}")
    print()

    if table_name in inspector.get_table_names():
        print(f"[SKIP] '{table_name}' 테이블이 이미 존재함")
        columns = [c["name"] for c in inspector.get_columns(table_name)]
        print(f"  Columns ({len(columns)}): {columns}")
        _verify_check_constraints(inspector, table_name, db_type)
    else:
        print(f"[CREATE] '{table_name}' 테이블 생성... (CHECK 3개 포함 — 리포 첫 DB CHECK)")
        # idempotent create (checkfirst=True — 이미 있으면 skip)
        AtomicWriteControl.__table__.create(bind=engine, checkfirst=True)
        print(f"  [OK] '{table_name}' 테이블 생성 완료")
        inspector_after = inspect(engine)
        columns = [c["name"] for c in inspector_after.get_columns(table_name)]
        print(f"  Columns ({len(columns)}): {columns}")

    # 테이블 존재 여부와 무관하게 singleton seed (idempotent)
    _seed_singleton()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="atomic_write_control 테이블 생성 + singleton seed (PR D / P1b A1)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB 연결 없이 DDL만 출력 (PostgreSQL dialect 기본)",
    )
    parser.add_argument(
        "--dialect",
        choices=["postgresql", "sqlite"],
        default="postgresql",
        help="--dry-run 시 DDL 생성 dialect (기본: postgresql)",
    )
    args = parser.parse_args()

    if args.dry_run:
        print(f"모드: DRY-RUN ({args.dialect} dialect, DB 연결 없음)")
        print()
        ddl = get_dry_run_ddl(args.dialect)
        print(ddl)
        print()
        print("[DRY-RUN] 실제 실행 안 함. --dry-run 없이 다시 실행하면 적용됨.")
    else:
        print("모드: LIVE (DB 연결 + create checkfirst + singleton seed)")
        apply_live()

    print("\n완료!")


if __name__ == "__main__":
    main()
