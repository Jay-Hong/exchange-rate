# app/database.py

# 표준 라이브러리
import os

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

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()
