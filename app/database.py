# app/database.py

# 표준 라이브러리
import os

# 서드파티 라이브러리
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# SQLite 사용
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "exchange_rates.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"   # "sqlite:///./exchange_rates.db"

# PostgreSQL 전환 시 (나중에 바꾸면 됨)
# DATABASE_URL = "postgresql://user:password@localhost:5432/mydb"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()
