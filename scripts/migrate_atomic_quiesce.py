#!/usr/bin/env python3
"""
atomic_quiesce_session + atomic_quiesce_app_ack 테이블 생성 (PR D / P1b — C6-quiesce Q2a).

배경:
  - P1_COMMON_BASE_DESIGN.md §9 step 6(개정) live-app quiesce handshake: drain = recreate +
    fresh-process halt ACK (구 in-process counter 폐기, §19 C6-quiesce). 그 cross-process bridge가
    durable evidence table 2개다 — halt 명령이 여는 quiesce session + recreate된 fresh app의 halt-관측 ACK.
  - app/models.py의 AtomicQuiesceSession / AtomicQuiesceAppAck ORM class 정의 기준.
  - A1 atomic_write_control / C6-1 atomic_cutover_* 와 **분리**된 quiesce-axis table
    (A1 row는 live-read라 무접촉 — halt_mode_generation은 value copy).

⚠️ 이 script가 두 quiesce table의 운영(non-test) 유일 생성 경로다 (Q2a behavior-change-0 핵심):
  운영 진입점(main.py import / backfill_history.py 등)은 app/database.create_all_app_tables를 쓰고,
  이 helper는 두 table을 create_all에서 제외한다(database.CREATE_ALL_EXCLUDE_TABLES). 따라서 import/script
  시점에 운영 DB로 신규 CHECK DDL이 emit되지 않는다. C6-quiesce엔 이를 read/소비하는 live writer가 없어 무해
  (CAS brick은 Q2b/dormant island, 실 caller는 Q4/C6-FLIP).

⚠️ **SEED 없음** — A1/C6-1 migrate와 의도적 divergence: quiesce table은 event/history surface다 (실제
  halt/quiesce가 일어나기 전까지 row 0개). singleton seed가 없다. create-only + CHECK 검증만.

사용법:
  python scripts/migrate_atomic_quiesce.py [--dry-run] [--dialect postgresql|sqlite]

주의:
  - 신규 빈 테이블 2개 추가 (CHECK + partial-unique index 포함)라 lock 거의 없음.
  - CHECK(format>=1 / state enum / halt_generation>=0 / observed_action enum / observed_generation>=0 /
    queue_size null-or->=0) + UNIQUE(session_id) + UNIQUE(session_id, boot_id) + partial-unique(state=open)가
    구조 무결성 보장.
  - --dry-run은 DB 연결 없이 두 table의 CREATE TABLE + CREATE INDEX DDL만 출력.
"""

# 표준 라이브러리
import argparse
import os
import sys

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_dry_run_ddl(dialect_name: str = "postgresql") -> str:
    """DB 연결 없이 dialect별 두 table CREATE DDL 생성 (--dry-run). CHECK + index 포함."""
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.models import AtomicQuiesceAppAck, AtomicQuiesceSession

    if dialect_name == "postgresql":
        from sqlalchemy.dialects import postgresql
        dialect = postgresql.dialect()
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects import sqlite
        dialect = sqlite.dialect()
    else:
        raise ValueError(f"지원하지 않는 dialect: {dialect_name}")

    parts = []
    for model in (AtomicQuiesceSession, AtomicQuiesceAppAck):
        table = model.__table__
        parts.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:  # partial-unique(state=open) + UNIQUE index 포함
            parts.append(str(CreateIndex(index).compile(dialect=dialect)))
    return "\n".join(parts)


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


def _verify_indexes(inspector, table_name: str, expected: set, db_type: str) -> None:
    """기존 table 의 expected index 존재 검증 (partial-unique single-open 등 drift 방어).

    partial-unique(state='open')는 동시 1개 open만 보장하는 load-bearing 무결성이라 rerun 시
    누락 검출 필요. PG는 누락 시 abort, sqlite는 warning (introspection 한계).
    """
    try:
        present = {ix.get("name") for ix in inspector.get_indexes(table_name)}
    except Exception:
        present = set()
    missing = expected - present
    if not missing:
        print(f"  [OK] {table_name} index {len(expected)}개 확인")
        return
    if db_type == "postgresql":
        raise SystemExit(f"[ABORT] {table_name} index 누락 (무결성 위반): {sorted(missing)}")
    print(f"  [WARN] {table_name} index introspection 누락 (sqlite 제약일 수 있음): {sorted(missing)}")


def apply_live() -> None:
    """실제 DB에 두 table 생성 (checkfirst). SEED 없음 (event/history table)."""
    from sqlalchemy import inspect

    from app.database import engine
    from app.models import AtomicQuiesceAppAck, AtomicQuiesceSession

    inspector = inspect(engine)
    db_url = str(engine.url)
    db_type = "postgresql" if "postgresql" in db_url else "sqlite"
    print(f"DB 타입: {db_type}\n")

    checks = {
        "atomic_quiesce_session": {
            "ck_atomic_quiesce_session_format_version", "ck_atomic_quiesce_session_state",
            "ck_atomic_quiesce_session_halt_generation",
        },
        "atomic_quiesce_app_ack": {
            "ck_atomic_quiesce_app_ack_observed_action", "ck_atomic_quiesce_app_ack_observed_generation",
            "ck_atomic_quiesce_app_ack_queue_size",
        },
    }
    indexes = {
        "atomic_quiesce_session": {
            "uq_atomic_quiesce_session_id", "uq_atomic_quiesce_session_single_open",
        },
        "atomic_quiesce_app_ack": {"uq_atomic_quiesce_app_ack_session_boot"},
    }
    for model in (AtomicQuiesceSession, AtomicQuiesceAppAck):
        table_name = model.__tablename__
        if table_name in inspector.get_table_names():
            print(f"[SKIP] '{table_name}' 이미 존재")
            _verify_check_constraints(inspector, table_name, checks[table_name], db_type)
            _verify_indexes(inspector, table_name, indexes[table_name], db_type)
        else:
            print(f"[CREATE] '{table_name}' 생성... (CHECK {len(checks[table_name])}개 + index {len(indexes[table_name])}개 포함)")
            model.__table__.create(bind=engine, checkfirst=True)
            print(f"  [OK] '{table_name}' 생성 완료")

    print("\n  [NO-SEED] quiesce table은 event/history surface — seed 없음 (A1/C6-1과 의도적 divergence).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="atomic_quiesce_session + atomic_quiesce_app_ack 생성 (PR D / P1b C6-quiesce Q2a, create-only)"
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
        print("모드: LIVE (두 table create checkfirst, seed 없음)")
        apply_live()
    print("\n완료!")


if __name__ == "__main__":
    main()
