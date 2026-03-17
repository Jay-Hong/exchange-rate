# app/database.py

# 표준 라이브러리
import os
from contextlib import contextmanager

# 서드파티 라이브러리
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# 데이터베이스 URL (환경 변수 우선, 없으면 로컬 경로)
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    # 로컬 개발 환경 (Docker 없이 실행 시)
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DB_PATH = os.path.join(BASE_DIR, "data", "exchange_rates.db")
    DATABASE_URL = f"sqlite:///{DB_PATH}"

# PostgreSQL 전환 시 (환경 변수로 설정)
# DATABASE_URL = "postgresql://user:password@localhost:5432/mydb"

_is_sqlite = "sqlite" in DATABASE_URL

_engine_kwargs = {}
if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL: 연결 풀 제한 (RDS db.t4g.micro 메모리 절약)
    _engine_kwargs["pool_size"] = 3
    _engine_kwargs["max_overflow"] = 2

engine = create_engine(DATABASE_URL, **_engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


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
