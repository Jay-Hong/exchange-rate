"""DB 오류의 **transient / 영구** 분류 — `app/main.py` 에서 이동(leaf 모듈).

## 왜 별 모듈인가

WS 인가 경로(`app/topic_authorization.py`)가 이 분류를 써야 하는데, 그 모듈은 firebase 를
import-time 에 끌고 오는 `app.main` 을 import 할 수 없다. 그렇다고 **복제하면 두 경로가
갈린다** — 이건 "재시도로 풀리는가"라는 **보안 인접 정책 지식**이라 단일 소스여야 한다.

⚠️ `app.main` 은 이 이름들을 그대로 re-export 하므로 기존 참조는 계속 동작한다.
"""
from sqlalchemy import exc as sqlalchemy_exc

# DB **인프라 transient**만 — 재시도가 의미 있는 것들. 503 변환 경계다.
# ⛔ `SQLAlchemyError` 전체를 쓰면 안 된다: `ProgrammingError`(잘못된 SQL/누락 테이블),
# `InvalidRequestError`·`ResourceClosedError`·`ArgumentError`·`CompileError`(ORM 사용 결함),
# `IntegrityError`·`DataError`(데이터 결함)가 전부 하위라 **영구 결함을 무한 재시도로 안내**하게 된다
# (codex Major 2026-07-26). 아래 4종은 connect 실패 / 네트워크 단절 / statement timeout /
# 풀 고갈 / failover를 커버하고 위 결함들은 걸리지 않는다(계층 실측 + 회귀 테스트로 잠금).
TRANSIENT_DB_ERRORS = (
    sqlalchemy_exc.OperationalError,
    sqlalchemy_exc.InterfaceError,
    sqlalchemy_exc.TimeoutError,        # 풀 고갈 (pool_size=3 + max_overflow=2)
    sqlalchemy_exc.DisconnectionError,
)

# 클래스만으로는 못 거르는 **명백한 영구 오류** — `OperationalError`로 도착하지만 재시도로 절대
# 안 풀리는 설정·권한 문제다 (codex Medium: PEP 249 OperationalError는 "data source name is not
# found" 같은 영구 케이스도 포함한다).
PERMANENT_DB_SQLSTATES = frozenset({
    "28000",   # invalid_authorization_specification
    "28P01",   # invalid_password
    "3D000",   # invalid_catalog_name (DB 없음)
    "42501",   # insufficient_privilege
})


def is_transient_db_error(exc: BaseException) -> bool:
    """이 DB 오류가 **재시도로 해소될 수 있는가** (503 vs 500).

    **폴라리티가 계약이다 — 모르면 transient로 본다(deny-list).**
    allow-list(아는 것만 transient, 나머지 re-raise)는 반대로 위험하다: 가장 중요한 transient인
    **connect 실패·failover가 SQLSTATE 없이 도착**하기 때문이다. psycopg 3.3 실측 — 도달 불가
    호스트 연결 시 `OperationalError`이고 `sqlstate is None`(libpq 클라이언트측 오류라 서버
    응답 코드가 없다). allow-list면 그 케이스가 500이 되어 **이 503의 존재 이유가 사라진다.**

    비용 비대칭도 같은 방향이다: 미지의 오류를 transient로 접으면 총체적 장애 동안 bounded 재시도가
    낭비될 뿐이지만(그 상황엔 어차피 전 endpoint가 죽는다), transient를 permanent로 접으면
    **회복 가능한 상태를 포기**한다.

    ⚠️ 남은 부정확: `InterfaceError`의 use-after-close 같은 코드 결함은 SQLSTATE가 없어 여기서
    transient로 분류된다. `logger.exception`이 실제 클래스·traceback을 남기므로 진단은 가능하다.
    flip 후 `topic_snapshot_db_unavailable` 실 telemetry가 쌓이면 그때 정밀화한다(추측으로 SQLSTATE
    allow-list를 짜면 위 connect 케이스를 깬다).
    """
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    return sqlstate not in PERMANENT_DB_SQLSTATES
