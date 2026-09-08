"""IBK Selenium 검증 모드 설정. **이 값의 단일 정의 위치**다.

`app/config.py` 가 아니라 여기 두는 이유는 두 가지다.
  · 전역 가이드가 요구하는 것은 "config **계층** 으로 분리" 이지 단일 파일이 아니다.
    리포에도 `app/database_settings.py` 처럼 소유 범위에 맞춰 분리한 선례가 있다.
  · `app/config.py` 는 topic-only C2 의 **인용 경로**라, 여기에 IBK 국소 설정을 넣으면
    이후 모든 IBK 슬라이스가 재결속을 끌고 다닌다. 이 파일은 인용 경로가 아니다.

⛔ enforce 를 추가할 때도 **같은 설정에 허용값만 늘린다.** 별도 env 나 제2의 설정 원천을
   만들지 않는다 — 두 원천이 생기면 어느 쪽이 실제로 먹는지 사후에 못 가린다.
"""

import os

ENV_VAR = "IBK_SELENIUM_VALIDATION_MODE"

MODE_LEGACY = "legacy"
MODE_SHADOW = "shadow"

#: 허용값. `enforce` 는 **아직 없다** — 값만 받고 동작이 없으면 설정과 실제가 어긋난다.
ALLOWED_MODES = (MODE_LEGACY, MODE_SHADOW)
DEFAULT_MODE = MODE_LEGACY


def resolve_mode(raw: str | None) -> str:
    """문자열을 모드로 판정한다. **순수 함수** — 환경을 읽지 않는다.

    ⛔ 미설정은 기본값이지만 **알 수 없는 값은 조용히 기본값으로 떨어지지 않는다.** 오타 하나로
       관측이 꺼진 채 "켜 뒀다" 고 믿게 되는 것이 이 설정의 가장 나쁜 실패다.
    """
    if raw is None or not raw.strip():
        return DEFAULT_MODE
    mode = raw.strip().lower()
    if mode not in ALLOWED_MODES:
        raise ValueError(f"{ENV_VAR} must be one of {ALLOWED_MODES} (got {raw!r}).")
    return mode


def resolve_mode_from_env(environ: dict | None = None) -> str:
    """환경에서 읽어 판정한다. 시험이 주입할 수 있게 인자를 연다."""
    env = os.environ if environ is None else environ
    return resolve_mode(env.get(ENV_VAR))


#: import 시점에 한 번 해석한다. 실행 중 모드가 흔들리면 관측과 강제가 같은 실행에서
#: 갈라질 수 있다. 변경은 프로세스 재생성이 필요하다(즉시 전환이 아니다).
VALIDATION_MODE = resolve_mode_from_env()
