"""tests 공통 setup — collection 시작 시(모든 test import 전) 환경 격리.

main.py top-level은 firebase_admin import + DB engine 연결을 한다
(memory: project_main_py_helper_placement). 어떤 test file이든 app.database를 import하기
전에 DATABASE_URL을 sqlite로 override해야 RDS 연결 시도(timeout) 없이 동작한다.

conftest.py는 pytest가 test file들보다 먼저 import하므로, 여기서 설정하면
collection 순서(알파벳)와 무관하게 app.database.engine이 sqlite로 생성된다.

기존 in-memory SQLite 테스트(writer/orchestrator 등)는 각자 engine을 만들고
app.database.SessionLocal/engine을 patch하므로 본 설정에 영향받지 않는다.
"""
import atexit
import os
import sys
import tempfile
from unittest.mock import MagicMock

# 1. firebase_admin chain stub (main.py import 시 실제 init/creds 회피) — force(설치 여부 무관)
for _m in ("firebase_admin", "firebase_admin.credentials", "firebase_admin.messaging",
           "firebase_admin.auth", "firebase_admin.exceptions"):
    sys.modules[_m] = MagicMock()

# 2. DATABASE_URL → file-backed sqlite tempfile 강제 (RDS 연결 회피).
#    sqlite:///:memory:는 connection/thread별 분리 DB → endpoint(asyncio.to_thread 새 connection)에서
#    create_all(engine) 테이블이 안 보임("no such table") → 항상 file sqlite로 강제 (in-memory도 override).
_fd, _path = tempfile.mkstemp(suffix="_pytest.db")
os.close(_fd)  # mkstemp fd 즉시 닫기 (path만 사용)
os.environ["DATABASE_URL"] = f"sqlite:///{_path}"
atexit.register(lambda: os.path.exists(_path) and os.remove(_path))
