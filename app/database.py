# app/database.py

# 표준 라이브러리
import os
from contextlib import contextmanager

# 서드파티 라이브러리
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# 로컬 애플리케이션
# ⛔ app.config 가 아니다 — 그건 import 시 load_dotenv() 와 mkdir() 부작용이 있어서
#    이 모듈의 82개 importer 전부로 퍼진다. database_settings 는 부작용이 없다.
from app import database_settings

# 데이터베이스 URL (환경 변수 우선, 없으면 로컬 경로)
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # 로컬 개발 환경 (Docker 없이 실행 시)
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_PATH = os.path.join(BASE_DIR, "data", "exchange_rates.db")
    DATABASE_URL = f"sqlite:///{DB_PATH}"

# PostgreSQL 전환 시 (환경 변수로 설정)
# DATABASE_URL = "postgresql://user:password@localhost:5432/mydb"

# ⛔ profile 검증은 create_engine **이전**이다. 뒤로 밀면 잘못된 profile 로 engine 이 먼저
#    만들어진 뒤에야 예외가 난다 — 그 사이 커넥션 설정은 이미 정해진 상태다.
#    허용값 밖이면 여기서 기동이 실패한다. online 은 이 변수를 **부재**로 결정하므로(운영 .env
#    실측 0건) 이 경로는 누가 없어도 될 변수를 일부러 넣었을 때만 닿는다 — 근거는
#    app/database_settings.py docstring "왜 미지정 외에는 전부 실패인가".
DB_WORKLOAD_PROFILE = database_settings.resolve_profile_from_env()

# 풀 제한(RDS db.t4g.micro 메모리 절약)과 timeout 3종은 profile 이 정한다.
_engine_kwargs = database_settings.engine_kwargs(DATABASE_URL, DB_WORKLOAD_PROFILE)

# SQLAlchemy StatementError/DBAPIError 문자열은 기본적으로 SQL bind parameter를
# 포함한다. application engine에서 SQLAlchemy가 렌더링하는 `[parameters: ...]`는
# 일괄 숨긴다. 단, `exc.params` 자체를 지우거나 driver 원문·SQL literal을 정화하는
# 옵션은 아니므로 secret-bearing 실패 경로의 type-only logging은 계속 필요하다.
engine = create_engine(DATABASE_URL, hide_parameters=True, **_engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


# atomic_write_control(P1 control plane, CHECK-bearing)은 일반 create_all에서 제외 —
# scripts/migrate_atomic_write_control.py가 운영(non-test) 유일 생성 경로 (import/script 시점에 운영 PG로
# 신규 CHECK DDL을 emit하지 않음 = A1 behavior-change-0 + migration-first 코드 보장).
# 운영 진입점(main.py import / backfill_history.py 등)은 본 helper를 쓴다.
# (테스트는 control table이 필요하므로 Base.metadata.create_all을 직접 사용.)
# P1b C6-1: cutover-control tables(atomic_cutover_control/atomic_cutover_asset, CHECK-bearing)도
# 동일 — scripts/migrate_atomic_cutover.py가 운영 유일 생성 경로 (create_all 제외 = behavior-change-0).
# P1b C6-quiesce Q2a: quiesce evidence tables(atomic_quiesce_session/atomic_quiesce_app_ack, CHECK-bearing)도
# 동일 — scripts/migrate_atomic_quiesce.py가 운영 유일 생성 경로 (create_all 제외 = behavior-change-0).
CREATE_ALL_EXCLUDE_TABLES = frozenset({
    "atomic_write_control",
    "atomic_cutover_control",
    "atomic_cutover_asset",
    "atomic_quiesce_session",
    "atomic_quiesce_app_ack",
})


def create_all_app_tables(bind) -> None:
    """control-plane 테이블을 제외하고 ORM 테이블 생성 (checkfirst, idempotent)."""
    Base.metadata.create_all(
        bind=bind,
        tables=[t for t in Base.metadata.sorted_tables if t.name not in CREATE_ALL_EXCLUDE_TABLES],
    )


@contextmanager
def get_db_context():
    """
    DB 세션 Context Manager (연결 누수 방지)

    Usage:
        with get_db_context() as db:
            result = db.execute(query)

    Notes:
        - Phase 1A: 그래프 API에서 DB 연결 누수 방지용
        - 자동으로 close() 호출 보장
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
