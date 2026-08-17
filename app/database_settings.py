"""DB workload profile 과 engine kwargs 를 **부작용 없이** 도출한다.

## 왜 별 모듈인가

⚠️ **"부작용 전파를 막으려고" 가 아니다.** 처음엔 그렇게 적었는데 틀렸다 — 실측으로
   `app/__init__.py` 가 `from app.logging import logger` 를 하고 `app/logging.py` 가
   `app/config.py` 를 끌어오므로, `app.database` 는 **이 변경 전에도 이미** `load_dotenv()` 와
   `LOG_DIR.mkdir()` 를 거쳤다. `app.*` 안에 있는 한 그 배는 이미 떠났다.

진짜 이유는 둘이다:

1. **순수 함수라 남김없이 시험된다.** 판정(`resolve_profile`)과 도출(`engine_kwargs`)이
   engine 생성과 분리돼 있어 `create_engine` 부작용 없이 전 경우를 돌릴 수 있다.
2. **변이를 정확히 겨눌 수 있다.** 관용 허용(`.lower()`)·기본값 폴백·값 교차배선 같은 결함을
   한 함수 안에서 재현할 수 있어야 그 커버리지가 어느 테스트에 귀속되는지 말할 수 있다.

그래서 이 모듈 **자체**는 `os.getenv` 와 SQLAlchemy URL 파서 외에 아무것도 쓰지 않는다 —
`app.*` 를 하나도 import 하지 않아 파일 하나만 떼어 단독으로 로드된다.

## profile 이 무엇을 가르는가

같은 코드가 두 성격의 프로세스에서 돈다:

- **online** — 사용자 대면 FastAPI. 풀이 마르면 **빨리 실패**해야 503 으로 접힌다.
- **maintenance** — `ops/cron/fxi-db-maintenance.crontab` 의 backfill/rollup one-shot 컨테이너.
  긴 집계가 **정상**이라 online 상한을 그대로 쓰면 중간에 잘린다.

선택자는 `DB_WORKLOAD_PROFILE` env 하나다(M0a manifest 가 cron 5줄에 붙인다).

## ⛔ 왜 미지정 외에는 전부 실패인가

⚠️ 처음엔 "조용히 online 으로 떨어지면 maintenance 가 중간까지 쓰고 잘려 **부분 작업이 남는다**"
   고 적었다. 그 근거는 **과장이었다**(외부 검토 지적 → 확인). manifest 의 5개 writer 는
   `db.commit()` / 실패 시 `db.rollback()` 트랜잭션이라 57014 가 나면 롤백된다 — 부분 쓰기는
   남지 않고 job 이 실패할 뿐이다. 트랜잭션 밖에서 항목별로 커밋하는 스크립트에만 해당한다.

fail-closed 를 유지하는 **실제** 근거는 셋이다:

1. **online 은 이 변수를 아예 설정하지 않는다**(운영 `.env` 실측: 0건). online 은 *부재*로
   결정되므로, 여기서 기동이 죽는 경로는 "없어도 될 변수를 누군가 일부러 추가했을 때" 뿐이다.
   그건 조용히 넘길 상황이 아니라 멈춰야 할 상황이다.
2. **안전한 fallback 이라고 쓸 수가 없다.** 리포 원칙은 fallback 을 넣으려면 왜 안전한지 한 줄로
   설명 가능해야 한다고 요구한다 — maintenance 에 online 상한을 물리는 것은 안전하지 않다.
3. **한 변수에 두 의미를 주지 않는다.** "online 은 관용, maintenance 는 엄격" 으로 가르면 같은
   오타가 어디에 쓰였느냐에 따라 다르게 동작한다 — 추론이 더 어려워진다.

대신 죽을 때 **진단 가능해야** 한다: 예외 메시지가 변수명·틀린 값·허용값을 모두 담는다.

그래서 `"Online"` · `"ONLINE"` · `" online"` · `""` · 오타 전부 **기동 실패**다. 빈 문자열을
미지정으로 보지 않는 이유: manifest 는 빈 값을 만들지 않는다 — 빈 값은 누군가 절반만 고친 흔적이다.

## ⚠️ 이 모듈이 보장하지 않는 것

- **작업 전체 수명의 상한이 아니다.** `statement_timeout` 은 statement 마다 다시 적용된다.
  짧은 문장 100개는 각각 통과하면서 전체로는 얼마든지 길어질 수 있다.
- **"nginx 상한보다 작다" 는 불변식이 아니다.** nginx 예산은 경로마다 다르고(`/api` 30s,
  `/admin` 60s, `/ws` 600s), maintenance cron 은 nginx 를 아예 거치지 않는다. 계층을 가로지르는
  부등식을 계약으로 적으면 한쪽만 바뀌었을 때 거짓이 된다.
- **M0a manifest 의 5개 scheduled 경로만** maintenance 로 자동 전환된다. 손으로 돌리는 다른
  스크립트는 online 상한을 받는다.
"""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy.engine import make_url

ENV_VAR = "DB_WORKLOAD_PROFILE"

ONLINE = "online"
MAINTENANCE = "maintenance"
PROFILES = (ONLINE, MAINTENANCE)

#: ⛔ 이 슬라이스는 풀 **용량**을 바꾸지 않는다 — 상한과 용량을 같이 움직이면 개선의 귀속이
#:    불가능해진다(용량 재산정은 별 슬라이스). 기존 값 그대로 고정 전제로 둔다.
POOL_SIZE = 3
MAX_OVERFLOW = 2

# ── 값의 근거: 운영 RDS 실측 (2026-08-17) ──────────────────────────────────────
#
# ⛔ 이 값들은 **추정이 아니라 측정**에서 나왔다. 처음엔 online statement 상한을 10s 로 잡았는데,
#    운영에서 재보니 기존 경로 둘이 그 예산의 2/3 를 이미 쓰고 있었다:
#
#      source_rates                     587,239 행 / 215 MB   (statement_timeout 은 현재 0=무제한)
#      retention DELETE 스캔                    331 ms        ← 안전
#      1d precompute 24h fetch                   12 ms        ← 안전
#      legacy_format window (mirror 주기)     6,993 ms        ← 10s 의 70%
#      legacy 1y investing fetch              6,589 ms        ← 10s 의 66%, 725,959 행
#
#    뒤의 둘은 **이 슬라이스가 만든 문제가 아니라 이미 있던 느린 경로**다. 10s 를 걸면 내가
#    그것들을 끊는다. 그래서 이 슬라이스의 목표를 정확히 좁힌다:
#    **무제한(0)을 유한하게 만드는 것**이지, 공격적인 SLO 를 강제하는 것이 아니다.
#
# ⚠️ 알려진 부채(별 슬라이스, 측정 후 처리):
#    - `crud.get_source_rates_as_legacy_format(db)` 는 WHERE 없이 전 테이블 window 집계를 돌고
#      legacy policy 필터 뒤 **6행**만 남긴다. mirror 주기마다 반복된다.
#    - `investing_exchange_rates` 는 retention 이 없어 무한 증식한다(현재 725,959 행/1년).
#    이 둘을 고치기 전에는 online statement 상한을 15s 아래로 내리지 않는다 — 내리려면
#    먼저 재측정해서 근거를 갱신할 것.

#: 풀에서 커넥션을 기다리는 상한(초).
#: ⚠️ SQLAlchemy 기본은 30s 다. 7s 짜리 질의가 커넥션을 물고 있는 동안(위 실측) 뒤에 선 요청이
#:    한 번은 통과할 수 있어야 하므로 online 을 7s 아래로 내리지 않는다.
POOL_TIMEOUT_SECONDS = {ONLINE: 10, MAINTENANCE: 30}
#: libpq TCP 연결 상한(초). 같은 VPC 안이라 정상값은 1~3ms — 5s 도 매우 넉넉하다.
CONNECT_TIMEOUT_SECONDS = {ONLINE: 5, MAINTENANCE: 10}
#: 서버 측 statement 상한(ms). ⛔ 0 은 "빠름" 이 아니라 **무제한**이다 — 0 을 두면 안 된다.
#: online 60s = 실측 최악(6,993ms)의 약 8.6배 여유.
STATEMENT_TIMEOUT_MS = {ONLINE: 60_000, MAINTENANCE: 900_000}


class InvalidDbWorkloadProfile(RuntimeError):
    """`DB_WORKLOAD_PROFILE` 값이 허용된 profile 이 아니다 — 조용히 넘기지 않는다."""


class UnvalidatedDbWorkloadProfile(InvalidDbWorkloadProfile):
    """⛔ **검증을 거치지 않은 profile 이 `engine_kwargs` 까지 왔다.**

    정상 경로에서는 도달하지 않는다(호출자는 언제나 `resolve_profile*` 을 통과한 값을 준다).
    별 클래스인 이유는 진단이 아니라 **판정**이다: 검증을 우회한 결함을 심었을 때 방어층이
    같은 예외를 던지면, 테스트가 "검증이 죽였다" 와 "방어층이 죽였다" 를 구분하지 못해
    변이가 **엉뚱한 이유로** KILLED 로 보인다(실측: 7개 중 6개가 그랬다).

    `InvalidDbWorkloadProfile` 을 상속하므로 기존 `except` 는 그대로 동작한다.
    """


def resolve_profile(raw: str | None) -> str:
    """env 원문을 profile 로 판정한다. **미지정만** 기본값이고 나머지는 전부 실패다.

    ⛔ `.lower()`/`.strip()` 관용을 넣지 않는다. 관용은 "설정이 먹었는지" 를 흐리게 만들고,
       실제로 틀린 값을 정상처럼 통과시킨다.
    """
    if raw is None:
        return ONLINE
    if raw in PROFILES:
        return raw
    raise InvalidDbWorkloadProfile(
        f"{ENV_VAR}={raw!r} 은 허용되지 않는다. 허용값: {', '.join(PROFILES)} "
        f"(미지정이면 {ONLINE}). 대소문자·공백·빈 문자열도 거절한다 — "
        "조용히 online 으로 떨어지면 maintenance 작업이 중간까지만 쓰고 잘린다."
    )


def resolve_profile_from_env(environ: dict[str, str] | None = None) -> str:
    """`os.environ` 에서 읽어 판정한다(테스트가 주입할 수 있게 인자를 연다)."""
    env = os.environ if environ is None else environ
    return resolve_profile(env.get(ENV_VAR))


def is_sqlite_url(url: str) -> bool:
    """⛔ `"sqlite" in url` 로 판정하지 않는다.

    그 방식은 `postgresql://u@h/sqlite_backup` 처럼 **이름에 sqlite 가 든 PostgreSQL URL** 을
    SQLite 로 오판해 PG 전용 인자를 통째로 떨어뜨린다. backend 는 URL 파서가 판정한다.
    """
    return make_url(url).get_backend_name() == "sqlite"


def engine_kwargs(url: str, profile: str) -> dict[str, Any]:
    """`create_engine(url, **kwargs)` 에 그대로 넘길 kwargs.

    ⛔ SQLite 경로에 PG 전용 인자가 새면 **연결하는 순간** `TypeError` 가 난다(로컬 전멸).
       그래서 backend 분기가 먼저다.
    """
    if profile not in PROFILES:
        # 방어층 — 정상 경로는 resolve_profile 을 이미 통과했다. **하위클래스**로 던져
        # "검증이 막았다" 와 구분 가능하게 한다(그 구분이 없으면 검증 우회 변이가 방어층
        # 덕분에 죽어 놓고 검증이 잡은 것처럼 보인다).
        raise UnvalidatedDbWorkloadProfile(
            f"검증을 거치지 않은 profile 이 engine_kwargs 에 도달했다: {profile!r}")

    if is_sqlite_url(url):
        # SQLite 는 statement_timeout / connect_timeout 개념이 없다. profile 은 검증만 되고
        # kwargs 에는 반영되지 않는다(로컬에서도 오타는 잡히되 동작은 그대로).
        return {"connect_args": {"check_same_thread": False}}

    return {
        "pool_size": POOL_SIZE,
        "max_overflow": MAX_OVERFLOW,
        "pool_timeout": POOL_TIMEOUT_SECONDS[profile],
        "connect_args": {
            "connect_timeout": CONNECT_TIMEOUT_SECONDS[profile],
            "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS[profile]}",
        },
    }
