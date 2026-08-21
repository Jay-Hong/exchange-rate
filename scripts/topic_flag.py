"""운영 `TOPIC_DISPATCHER_ENABLED` 를 켜고 끄는 **대칭 짧은 트랜잭션** 실행기.

## 왜 별도 도구인가

⛔ `scripts/env_flip.py` 는 재사용할 수 없다 — 두 곳에서 **구조적으로** 막힌다:
   (1) `preflight` 가 런타임 `ENV=development` 를 요구한다(운영은 이제 production).
   (2) `check_file_invariants` → `canary_monitor.validate_canary_start_env` 가
       **`TOPIC_DISPATCHER_ENABLED` 가 false/미설정이 아니면 거부**한다(preflight·post-write 양쪽).
       즉 그 도구의 **안전 불변식이 "release flag 를 켜지 않는다"** 라서 이 목적과 정면 충돌한다.
⛔ `canary_monitor` 도 안 된다 — `finally: await restorer.restore()` 로 **항상 되돌린다**.

## ⛔ 원본 backup 은 만들지 않는다

`env_flip` 은 원본이 **무엇이든 될 수 있어서** whole-file backup 이 필요했다. 여기는 다르다:
**불리언 한 키**이고 **두 목표 상태가 이미 알려져 있다**. 그래서 되돌리기는 "원본 복원" 이 아니라
**반대 값을 쓰는 또 하나의 key-scope 쓰기**다. 따라서 원본 env 사본은 필요 없다.

그 결과 다음이 **전부 사라진다**(패치가 아니라 전제 제거):
· backup + owner sidecar 의 게시 경합 — 두 파일을 따로 게시하는 이상 원자성이 불가능했다.
· `canary_monitor --recover` 가 공유 backup 을 owner 확인 없이 복원·삭제해 저널을 가로채는 문제.
· 세 도구 공통 transaction 소유권 규약(= 검증된 도구 2개 수정).
· `EnvRestorer` 의 무조건 덮어쓰기에 CAS 를 다는 문제 — 복원 자체가 없고, key-scope 쓰기는
  write 직전 바이트 재확인으로 CAS 가 내장된다.

다만 **복원 자료**와 **미완료 활성화의 증거**는 다른 문제다. ON write 직후 SIGKILL되고 나중에
무관한 배포가 컨테이너를 재생성하면 `.env`와 런타임이 모두 true가 되어, 비교만으로는 smoke를
통과하지 않은 활성화를 구분할 수 없다. 그래서 secret-free write-ahead marker 하나를 ON write
전에 게시하고, 전체 검증 또는 OFF 수렴 뒤에만 지운다. marker는 원본 값도 secret도 담지 않는다.

## 두 명령 모두 멱등이고 수렴한다

| 죽은 지점 | 남는 상태 | 복구 |
| --- | --- | --- |
| write 전 | 무변경 | 아무 명령 |
| write 후 / recreate 전 | 파일 true · 런타임 false | `on` 재실행(완료) **또는** `off`(되돌림) |
| recreate 후 / 검증 전 | 둘 다 true | `on` 재실행(검증만) 또는 `off` |

## 세 env 변경 도구의 상호 배제

`topic_flag`·`canary_monitor`·`env_flip` 의 **CLI 전체 트랜잭션**은 같은 `.env`별 non-blocking
`flock` 을 잡는다. lock 파일은 저널이 아니며 존재 여부로 상태를 판단하지 않는다. 프로세스가
SIGKILL 로 죽어도 커널이 lock 을 해제하고, 기존 canary backup/recover 계약은 그대로 유지된다.
`status` 는 lock 점유를 별도 관측해 진행 중인 트랜잭션을 안정 ON/OFF 로 접지 않는다.

## `off` 는 `on` 보다 **약한 전제**를 갖는다

`off` 는 **장애 대응 coarse kill** 이다(iOS `TOPIC_V2_RELEASE_RUNBOOK.md` Rollback 2차).
health 정상·안전 시간대·파일 권한·one-shot 부재를 요구하면 **정작 필요한 순간에 막힌다**.
그래서 OFF 방향은 **엉뚱한 스택을 건드리는 것만** 막는다(identity + ambient 대상 선택 변수).
⚠️ Apple phased Pause 와도 **독립**이다 — Pause 실패가 이 명령을 막지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.canary_monitor import (  # noqa: E402  ⛔ 검증된 primitive 재사용 — 수정 0
    CANCEL_ON_SIGNALS,
    PRODUCTION_TARGET,
    CollectorError,
    Target,
    admin_fetch_command,
    atomic_write_bytes,
    backup_path_for,
    check_target_identity,
    container_env,
    make_signal_canceller,
    parse_curl_response,
    read_env_values,
    recreate_and_verify_health,
    run_command,
    wait_until_healthy,
)
from scripts.env_flip import (  # noqa: E402
    COMPOSE_FILE,
    ambient_problems,
    check_env_file_mode,
    check_legacy_backup_modes,
    compose_interpolated_names,
    inspect_value,
    observe,
    say,
    unlink_durably,
)
from scripts.env_operation_lock import (  # noqa: E402
    EnvOperationLocked,
    TOPIC_FLAG_PENDING_SUFFIX,
    env_operation_is_locked,
    env_operation_lock,
    topic_flag_pending_path,
)

TARGET_KEY = b"TOPIC_DISPATCHER_ENABLED"
AUTH_STAGE_KEY = "WS_TOPIC_AUTH_STAGE"
REQUIRED_AUTH_STAGE = "enforce_authenticated_premium"
TRUE = b"true"
FALSE = b"false"
PENDING_SUFFIX = TOPIC_FLAG_PENDING_SUFFIX

FX_TOPICS = ("fx:usd-krw", "fx:jpy-krw", "fx:eur-krw")

#: ⛔ 값을 출력하지 않는다 — 일치할 때만 리터럴을 낸다.
MATCH_FMT = '{{range .Config.Env}}{{if eq . "TOPIC_DISPATCHER_ENABLED=%s"}}MATCH{{end}}{{end}}'

# ── 상태 ───────────────────────────────────────────────────────────────────
STATE_ON = "ON"
STATE_OFF = "OFF"
STATE_PENDING_RECREATE = "PENDING_RECREATE"   # 파일과 런타임이 다르다(재생성 미완료)
STATE_INTERRUPTED = "INTERRUPTED"               # write-ahead marker가 남은 미완료 ON
STATE_DRIFTED = "DRIFTED"                     # 런타임 신호끼리 모순
STATE_AMBIGUOUS = "AMBIGUOUS"                 # 다른 도구의 트랜잭션 진행 중 등
STATE_UNKNOWN = "UNKNOWN"                     # 필수 관측을 못 읽었다


class Abort(Exception):
    """중단 사유."""


def resolve_env_path(raw) -> Path:
    """⛔ `.resolve()` **전에** `lstat` 으로 본다 — resolve 는 symlink 를 따라가므로 그 뒤에
    권한/종류를 검사하면 **링크가 아니라 대상**을 보게 되어 symlink 검사가 무력화된다.
    ⚠️ 긴급 OFF 도 예외가 아니다 — 엉뚱한 대상 파일에 쓰는 것은 차단이 아니라 새 사고다.
    """
    path = Path(raw)
    try:
        info = path.lstat()
    except OSError as exc:
        raise Abort(f"env 경로를 확인할 수 없다: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise Abort(f"{path.name} 이 symlink 다 — 대상이 바뀔 수 있어 쓰지 않는다")
    if not stat.S_ISREG(info.st_mode):
        raise Abort(f"{path.name} 이 일반 파일이 아니다")
    return path.resolve()


def pending_marker_path(env_path: Path) -> Path:
    return topic_flag_pending_path(env_path)


def activation_marker_exists(env_path: Path) -> bool:
    """Return whether a valid, owner-only pending activation marker exists."""
    marker = pending_marker_path(env_path)
    try:
        info = marker.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise Abort(f"activation marker를 확인할 수 없다: {type(exc).__name__}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise Abort("activation marker가 일반 파일이 아니다")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise Abort("activation marker의 소유자/권한이 안전하지 않다")
    return True


def ensure_activation_marker(env_path: Path) -> Path:
    """Publish the write-ahead marker before an ON write can occur.

    The marker intentionally has no payload.  Existence means only "an ON
    transaction has not committed", so even a crash immediately after O_EXCL
    creation leaves sufficient recovery evidence and no secret material.
    """
    marker = pending_marker_path(env_path)
    if activation_marker_exists(env_path):
        return marker
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(marker, flags, 0o600)
    except FileExistsError:
        if activation_marker_exists(env_path):
            return marker
        raise Abort("activation marker 게시 경합을 판정할 수 없다")
    except OSError as exc:
        raise Abort(f"activation marker를 만들 수 없다: {type(exc).__name__}") from exc
    try:
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(marker.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return marker


def clear_activation_marker(env_path: Path) -> bool:
    """Durably clear a valid marker; return whether one was removed."""
    if not activation_marker_exists(env_path):
        return False
    unlink_durably(pending_marker_path(env_path))
    return True


# ── 순수 함수 (테스트가 직접 부른다) ────────────────────────────────────────


def key_line_indexes(data: bytes, key: bytes = TARGET_KEY) -> list:
    """`KEY=` 로 시작하는 행의 인덱스 전부. ⚠️ `KEY_OTHER=` 는 매칭되지 않는다."""
    lines = data.splitlines(keepends=True)
    prefix = key + b"="
    return [i for i, line in enumerate(lines) if line.rstrip(b"\r\n").startswith(prefix)]


def read_key_value(data: bytes, key: bytes = TARGET_KEY) -> Optional[bytes]:
    """단일 행일 때만 **raw 값**(공백 미제거)을 준다. 0개/중복이면 None."""
    hits = key_line_indexes(data, key)
    if len(hits) != 1:
        return None
    lines = data.splitlines(keepends=True)
    return lines[hits[0]].rstrip(b"\r\n")[len(key) + 1:]


def is_true(raw) -> bool:
    """⛔ **`app/config.py` 와 같은 술어여야 한다** — `os.getenv(...).lower() == "true"`.
    거기엔 `.strip()` 이 **없다**. 여기서 strip 하면 값에 공백이 섞였을 때 실행기는 ON 을
    선언하고 앱은 False 로 동작한다 — 사람이 몇 시간 기기 smoke 를 하고 전부 실패한다.
    """
    if raw is None:
        return False
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return raw.lower() == "true"


def _rewrite(data: bytes, indexes, value: bytes) -> bytes:
    """지정한 행들만 `KEY=value` 로 바꾼다. ⚠️ CRLF·마지막 개행 부재를 포함해 나머지 전 bytes 보존.

    ⛔ `canary_monitor.apply_env_values` 를 쓰지 않는 이유: 그 함수는 `"\\n".join(out) + "\\n"` 로
    **파일 전체를 재작성**하며 **후행 개행을 강제**한다 — 대상 키 밖 bytes 가 조용히 바뀐다.
    """
    lines = data.splitlines(keepends=True)
    for index in indexes:
        core = lines[index].rstrip(b"\r\n")
        ending = lines[index][len(core):]
        lines[index] = TARGET_KEY + b"=" + value + ending
    return b"".join(lines)


def plan_enable(data: bytes) -> bytes:
    """⛔ ON 방향은 **정확히 1행 + 명확한 값**을 요구한다(fail-closed). 이미 true 면 무변경.

    ⚠️ 기존 값이 `garbage` 여도 앱 술어상 false 이므로 "고쳐서 true 로" 쓸 수는 있다. 그러나 그건
    **누군가 예상 밖 편집을 했다는 신호**다 — fail-closed 방향은 교정이 아니라 중단이다.
    (OFF 는 반대로 fail-open 이라 무엇이 적혀 있든 false 로 정규화한다.)
    """
    hits = key_line_indexes(data)
    if len(hits) != 1:
        raise Abort(f"`{TARGET_KEY.decode()}=` 행이 {len(hits)}개다 — ON 은 정확히 1개를 요구한다")
    raw = read_key_value(data)
    if (raw or b"").decode("utf-8", "replace").lower() not in ("true", "false"):
        raise Abort(f"`{TARGET_KEY.decode()}` 값이 true/false 가 아니다(길이 {len(raw or b'')}) "
                    "— 예상 밖 편집이므로 교정하지 않고 중단한다")
    if is_true(raw):
        return data                                   # 멱등
    return _rewrite(data, hits, TRUE)


def plan_disable(data: bytes) -> bytes:
    """⚠️ OFF 방향은 **fail-open** 이다 — 0개(코드 default false)·1개·중복 전부 수용해 **모두 false 로**
    정규화한다. 목표 상태가 모호하지 않으므로 여기서 중단하면 **긴급 차단이 막힌다**.
    """
    hits = key_line_indexes(data)
    if not hits:
        return data                                   # 키 부재 = 코드 default false
    return _rewrite(data, hits, FALSE)


def classify(observation: dict) -> str:
    """관측 묶음 → 상태. ⛔ 못 읽은 신호를 False 로 접지 않는다."""
    if observation.get("operation_locked") is True:
        return STATE_AMBIGUOUS
    if "operation_locked" in observation and observation.get("operation_locked") is None:
        return STATE_UNKNOWN
    if observation.get("activation_pending") is True:
        return STATE_INTERRUPTED
    if "activation_pending" in observation and observation.get("activation_pending") is None:
        return STATE_UNKNOWN
    if observation.get("other_tool_backup"):
        return STATE_AMBIGUOUS
    runtime = [observation.get("container"), observation.get("inspect"),
               observation.get("dispatcher_endpoint")]
    if any(signal is None for signal in runtime) or observation.get("file_readable") is False:
        return STATE_UNKNOWN
    # ⚠️ **FX 를 판정에서 빼면 안 된다** — 전역 3축이 true 여도 FX endpoint 가 불능이거나
    #    `FX_TOPIC_ENABLED=false` 면 이 릴리스의 목적(FX topic)이 서지 않는데 `ON` 으로 보고된다.
    if observation.get("fx") is None:
        return STATE_UNKNOWN
    # ⚠️ 키가 **중복**이면 파일이 어느 값인지 단정할 수 없다 — `read_key_value` 가 None 을 주고
    #    `is_true(None)` 은 False 라 **조용히 "꺼짐"으로 접힌다**. 그 접힘을 여기서 막는다.
    #    (0개는 코드 default false 라 정상 — 중복만 모호하다.)
    if (observation.get("file_line_count") or 0) > 1:
        return STATE_AMBIGUOUS
    file_value = observation.get("file_value")        # bytes | None(키 부재 = default false)
    # ⚠️ 단일 행이어도 값이 **앱 술어상 명확하지 않으면** 안정 상태로 부르지 않는다.
    #    `=garbage` 는 `is_true` 상 False 라 런타임 false 와 함께 `OFF`(exit 0)로 접히는데,
    #    그건 예상 밖 편집을 "정상"으로 축복하는 것이다. `plan_enable` 은 이미 거부한다.
    if file_value is not None and file_value.decode("utf-8", "replace").lower() not in (
            "true", "false"):
        return STATE_AMBIGUOUS
    if len(set(bool(signal) for signal in runtime)) != 1:
        return STATE_DRIFTED                          # 런타임 신호끼리 모순
    runtime_true = bool(runtime[0])
    file_true = is_true(file_value)
    if file_true != runtime_true:
        return STATE_PENDING_RECREATE                 # 쓰기는 됐고 재생성이 남았다(양방향)
    if runtime_true:
        file_auth_stage = observation.get("file_auth_stage")
        file_auth_stage_line_count = observation.get("file_auth_stage_line_count")
        if file_auth_stage_line_count is None:
            return STATE_UNKNOWN
        if file_auth_stage_line_count > 1:
            return STATE_AMBIGUOUS
        if file_auth_stage != REQUIRED_AUTH_STAGE:
            # 현재 런타임만 final이어도 파일이 compatibility면 다음 recreate에서 인증이 퇴행한다.
            return STATE_DRIFTED
        auth_stage = observation.get("auth_stage")
        if auth_stage is None:
            return STATE_UNKNOWN
        if auth_stage != REQUIRED_AUTH_STAGE:
            # Release dispatcher ON은 익명 FX/USDT를 허용하는 compatibility stage와 양립할 수 없다.
            return STATE_DRIFTED
    fx_signals = [(observation.get("fx") or {}).get(topic) for topic in FX_TOPICS]
    if any(signal is None for signal in fx_signals):
        return STATE_UNKNOWN
    fx_values = [bool(signal) for signal in fx_signals]
    if runtime_true and not all(fx_values):
        # dispatcher 는 켜졌는데 FX topic 이 안 서는 상태 — `FX_TOPIC_ENABLED` 축이 다르다.
        return STATE_DRIFTED
    if not runtime_true and any(fx_values):
        # 전역 coarse kill 이 false 인데 개별 FX endpoint 가 true 면 OFF 가 성립하지 않는다.
        return STATE_DRIFTED
    return STATE_ON if runtime_true else STATE_OFF


#: ⛔ **안정 상태만 exit 0** — `UNKNOWN`/`DRIFTED`/`AMBIGUOUS`/`PENDING_RECREATE` 를 0 으로 주면
#: 자동화가 "괜찮다"로 읽는다. 그건 관측 실패를 정상으로 접는 것과 같다.
STABLE_STATES = (STATE_ON, STATE_OFF)


# ── 관측 (판정/개수만, 값 미노출) ───────────────────────────────────────────


async def admin_json(target: Target, path: str) -> dict:
    return parse_curl_response(await run_command(admin_fetch_command(path, target)))


def explicit_bool(mapping: object, key: str) -> Optional[bool]:
    """Return only an explicit JSON boolean; missing/coerced values are unknown."""
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(key)
    return value if isinstance(value, bool) else None


async def measure(target: Target, env_path: Path, data: Optional[bytes] = None) -> dict:
    """모든 명령이 공유한다. **부작용 0**, 실패한 축은 `None`(= 못 읽음)으로 남긴다.

    ⚠️ `data` 를 넘기면 **그 스냅샷**으로 판정한다 — 한 트랜잭션이 파일을 여러 번 읽으면
    각 읽기 사이가 전부 TOCTOU 창이 된다.
    """
    result: dict = {"other_tool_backup": backup_path_for(env_path).exists()}
    try:
        data = env_path.read_bytes() if data is None else data
        result["file_readable"] = True
        result["file_value"] = read_key_value(data)
        result["file_line_count"] = len(key_line_indexes(data))
        auth_key = AUTH_STAGE_KEY.encode()
        raw_file_auth_stage = read_key_value(data, auth_key)
        result["file_auth_stage"] = (
            raw_file_auth_stage.decode("utf-8", "replace")
            if raw_file_auth_stage is not None
            else None
        )
        result["file_auth_stage_line_count"] = len(key_line_indexes(data, auth_key))
    except OSError as exc:
        result["file_readable"] = False
        result["file_error"] = str(exc)

    try:
        live = await observe(
            lambda: container_env([TARGET_KEY.decode(), AUTH_STAGE_KEY], target),
            "container_env",
        )
        result["container"] = is_true(live.get(TARGET_KEY.decode()))
        result["auth_stage"] = live.get(AUTH_STAGE_KEY)
    except CollectorError:
        result["container"] = None
        result["auth_stage"] = None
    try:
        result["inspect"] = (await inspect_value(target, MATCH_FMT % "true")) == "MATCH"
    except CollectorError:
        result["inspect"] = None
    try:
        result["dispatcher_endpoint"] = explicit_bool(
            await admin_json(target, "/admin/api/topic-status"), "enabled")
    except CollectorError:
        result["dispatcher_endpoint"] = None

    # ⚠️ **FX 성공은 전역 dispatcher 하나로 증명되지 않는다** — FX topic 은 `FX_TOPIC_ENABLED` 와
    #    `TOPIC_DISPATCHER_ENABLED` 의 **AND** 다(`app/fx_topic_publisher.py`). 별도 endpoint 로 본다.
    try:
        payload = await admin_json(target, "/admin/api/topic-status/fx")
        result["fx"] = {
            topic: explicit_bool(payload.get(topic), "enabled") for topic in FX_TOPICS
        }
        first = payload.get(FX_TOPICS[0]) or {}
        result["fx_topic_enabled"] = explicit_bool(first, "fx_topic_enabled")
    except CollectorError:
        result["fx"] = None
        result["fx_topic_enabled"] = None
    return result


# ── preflight (비대칭) ─────────────────────────────────────────────────────


async def preflight_common(target: Target) -> None:
    """⛔ OFF 도 이것만은 지킨다 — **엉뚱한 스택을 재생성하는 것은 차단이 아니라 새 사고**다."""
    # ⚠️ interpolated 를 비워 넘기면 `TARGET_SELECTORS`(COMPOSE_FILE·DOCKER_HOST 등)만 검사한다 —
    #    OFF 가 상속해도 되는 **최소** 가드다.
    problems = ambient_problems(dict(os.environ), set())
    if problems:
        raise Abort("; ".join(problems))
    identity = await check_target_identity(target)
    if identity:
        raise Abort(f"target identity: {identity}")


async def preflight_on(target: Target, env_path: Path) -> bytes:
    """ON 은 fail-closed. ⚠️ OFF 는 이걸 **상속하지 않는다**.

    ⛔ **검증한 bytes 를 그대로 돌려준다.** 호출부가 파일을 다시 읽어 계획을 세우면
    *사전검사 → 계획* 사이가 TOCTOU 창이 되어, 그 사이 `.env` 에 들어온
    `KRX_CLIENT_DISTRIBUTION_ENABLED=true` 같은 값이 **정상 계획으로 실려 함께 배포**된다.
    기존 CAS 는 *계획 → write* 만 덮어 이 창을 막지 못했다.
    """
    await preflight_common(target)
    interpolated = compose_interpolated_names(COMPOSE_FILE.read_text(encoding="utf-8"))
    problems = ambient_problems(dict(os.environ), interpolated)
    if problems:
        raise Abort("; ".join(problems))
    if backup_path_for(env_path).exists():
        raise Abort("다른 도구(canary/env_flip)의 트랜잭션이 진행 중이다 — 그쪽을 먼저 끝낼 것")
    oneoff = (await run_command(
        ["docker", "ps", "--filter", "label=com.docker.compose.oneoff=True", "-q"])).strip()
    if oneoff:
        raise Abort("cron one-shot 컨테이너가 실행 중이다 — 끝난 뒤에 다시 실행할 것")

    # ⛔ secret 파일 권한 — 우리가 이 파일에 쓰기 때문에 확인한다(OFF 는 요구하지 않는다).
    check_env_file_mode(env_path)
    check_legacy_backup_modes(env_path)

    # ⛔ **파일과 런타임을 둘 다 본다.** recreate 는 런타임이 아니라 **`.env` 를 적용**하므로,
    #    현재 컨테이너만 검사하면 `.env` 에 **대기 중인** 값이 함께 켜진다 — 예컨대
    #    `KRX_CLIENT_DISTRIBUTION_ENABLED=true` 가 파일에 있으면 topic 을 켜면서 **KRX 배포까지
    #    함께 켜진다**. 이건 합의된 노출 범위를 조용히 넓히는 경로다.
    data = env_path.read_bytes()                       # ⛔ 이 스냅샷이 계획·CAS 의 기준이 된다
    pending = read_env_values(
        data.decode("utf-8"),
        ["ENV", "KRX_CLIENT_DISTRIBUTION_ENABLED", "FX_TOPIC_ENABLED", AUTH_STAGE_KEY,
         "ADMIN_PASSWORD"])
    live = await observe(
        lambda: container_env(
            ["ENV", "KRX_CLIENT_DISTRIBUTION_ENABLED", "FX_TOPIC_ENABLED", AUTH_STAGE_KEY], target),
        "preflight env")

    for label, value in (("파일", pending.get("ENV")), ("런타임", live.get("ENV"))):
        if value != "production":
            raise Abort(f"{label} ENV={value!r} — production 이 아니다")
    for label, value in (("파일", pending.get("KRX_CLIENT_DISTRIBUTION_ENABLED")),
                         ("런타임", live.get("KRX_CLIENT_DISTRIBUTION_ENABLED"))):
        if is_true(value):
            raise Abort(f"{label} KRX_CLIENT_DISTRIBUTION_ENABLED 가 true — 이 창에서는 OFF 여야 한다")
    # ⚠️ FX topic 은 `FX_TOPIC_ENABLED ∧ TOPIC_DISPATCHER_ENABLED` 다. 전자가 false 면 dispatcher 를
    #    켜도 **FX 가 서지 않아** 이 릴리스의 목적이 성립하지 않는다.
    for label, value in (("파일", pending.get("FX_TOPIC_ENABLED")),
                         ("런타임", live.get("FX_TOPIC_ENABLED"))):
        if not is_true(value):
            raise Abort(f"{label} FX_TOPIC_ENABLED 가 true 가 아니다 — FX topic 이 서지 않는다")
    # ⛔ compatibility에서 dispatcher를 켜면 익명 FX/USDT subscribe가 최신 snapshot을 받는다.
    #    파일만 확인하면 recreate 전 런타임과 계획이 갈릴 수 있으므로 두 축 모두 정확히 일치시킨다.
    # ⛔ **파일 쪽은 `read_env_values` 를 쓰지 않는다.** 그건 행 전체를 `.strip()` 하는 **관대한** 파서라
    #    `WS_TOPIC_AUTH_STAGE=enforce_authenticated_premium ` (후행 공백)을 통과시킨다. 그런데
    #    `app/config.parse_topic_auth_stage` 는 `.strip().lower()` 없이 **정확한 문자열만** 받고 아니면
    #    ValueError 를 던진다 — config 는 모듈 레벨이라 **컨테이너가 import 에서 죽는다**.
    #    관대한 파서로 통과시키면 사전검사는 OK 인데 재생성이 운영을 내려버린다(그 뒤 판정은 raw 비교라
    #    영구 DRIFTED — 재실행이 재생성을 반복한다). 그래서 **앱과 같은 strict 규칙**으로 여기서 거른다.
    #    ⚠️ 반대로 판정부를 strip 으로 "맞추면" 앱이 거부하는 값을 도구가 승인하게 된다 — `is_true` 의
    #    docstring 과 같은 이유로 방향은 항상 strict 쪽이다.
    raw_file_stage = read_key_value(data, AUTH_STAGE_KEY.encode())
    file_stage = raw_file_stage.decode("utf-8", "replace") if raw_file_stage is not None else None
    for label, value in (("파일", file_stage), ("런타임", live.get(AUTH_STAGE_KEY))):
        if value != REQUIRED_AUTH_STAGE:
            raise Abort(
                f"{label} {AUTH_STAGE_KEY}={value!r} — Release ON은 "
                f"{REQUIRED_AUTH_STAGE!r}만 허용한다(공백 포함 정확 일치)"
            )
    # ⚠️ 사후 검증이 admin endpoint 를 쓴다 — 값이 비면 재생성 뒤 검증 자체가 불가능해진다.
    admin = pending.get("ADMIN_PASSWORD")
    if admin is None or not admin.strip():
        raise Abort("ADMIN_PASSWORD 가 비어 있다 — 재생성 뒤 교차 검증을 할 수 없다")

    # ⛔ baseline health — 이미 아픈 스택을 켜서 원인을 섞지 않는다(OFF 는 요구하지 않는다).
    await wait_until_healthy(target, timeout=30.0)
    return data


# ── 트랜잭션 ───────────────────────────────────────────────────────────────


async def _apply(target: Target, env_path: Path, planned_from: bytes, planned: bytes,
                 expect_true: bool, summary: dict) -> None:
    """계획 bytes 적용 → 재생성 → 런타임 검증.

    ⛔ `planned_from` 은 **계획을 계산한 그 bytes** 다. write 직전에 현재 파일이 그것과 같은지
    확인한다(CAS) — 같지 않으면 계획을 세운 뒤 누군가 바꾼 것이므로 **stale 계획을 덮어쓰지 않는다**.
    ⚠️ 한때 여기서 파일을 연달아 두 번 읽어 비교했는데, 그건 **항상 같아서 아무것도 막지 못했다**.
    """
    # A CAS is required even when the target row already has the requested
    # value.  That path can still recreate a pending runtime, which would also
    # apply any unrelated env edit made after preflight.
    current = env_path.read_bytes()
    if current != planned_from:
        raise Abort("사전검사/계획 이후 .env 가 변경됐다 — stale 계획을 적용하지 않는다")
    if planned != planned_from:
        atomic_write_bytes(env_path, planned)
        summary["wrote"] = True
        written = env_path.read_bytes()
        if written != planned:
            raise Abort("write 결과가 계획한 bytes 와 다르다")
    else:
        summary["wrote"] = False
        say("WRITE", "파일이 이미 목표 상태 — 쓰기 생략")

    await recreate_and_verify_health(target)
    summary["recreated"] = True

    final_before_measure = env_path.read_bytes()
    observation = await measure(target, env_path, final_before_measure)
    final_after_measure = env_path.read_bytes()
    summary["state"] = classify(observation)
    summary["env_matches_plan"] = (
        final_before_measure == planned and final_after_measure == planned
    )
    ok = (summary["env_matches_plan"]
          and observation.get("container") is expect_true
          and observation.get("inspect") is expect_true
          and observation.get("dispatcher_endpoint") is expect_true)
    # ⚠️ FX 는 AND 게이트라 전역 신호만으로 증명되지 않는다. ON 은 전부 true,
    # OFF 는 전부 false 여야 하며 관측 불능도 성공으로 접지 않는다.
    fx = observation.get("fx")
    if fx is None:
        ok = False
    else:
        expected_fx = expect_true
        ok = ok and all(fx.get(topic) is expected_fx for topic in FX_TOPICS)
        summary["fx"] = fx
    if expect_true:
        summary["fx_topic_enabled"] = observation.get("fx_topic_enabled")
        summary["auth_stage"] = observation.get("auth_stage")
        ok = ok and observation.get("auth_stage") == REQUIRED_AUTH_STAGE
    if not ok:
        raise Abort(f"런타임 검증 실패 — state={summary['state']} "
                    f"env_matches_plan={summary['env_matches_plan']} "
                    f"container={observation.get('container')} "
                    f"inspect={observation.get('inspect')} "
                    f"dispatcher={observation.get('dispatcher_endpoint')} "
                    f"auth_stage={observation.get('auth_stage')!r}")


async def _converge_off(target: Target, env_path: Path) -> dict:
    """활성화가 시작된 뒤의 실패는 **무조건** 멱등 OFF 를 시도한다.

    ⛔ **관측 결과로 생략하지 않는다.** 한때 "켜진 흔적이 없으면 건너뛴다" 로 두었는데,
    파일 읽기 실패 + container/inspect 관측 실패가 겹치면 **실제 파일이 true 여도**
    "흔적 없음" 이 되어 켜진 채로 끝났다(그리고 `dispatcher_endpoint` 는 그 판정에서 아예
    빠져 있었다). OFF 는 멱등이라 불필요한 시도의 비용은 **최대 재생성 1회**지만,
    생략의 비용은 **flag 가 켜진 채 남는 것**이다 — 비대칭이 명백하다.
    """
    say("CONVERGE", "활성화 시작 이후 실패 — 멱등 off 로 수렴 시도(관측과 무관)")
    try:
        result = await turn_off(target, env_path=env_path)
        return {
            "attempted": True,
            "ok": result.get("ok", False),
            "state": result.get("state"),
            "error": result.get("error"),
        }
    except BaseException as exc:                       # noqa: BLE001
        return {"attempted": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def turn_on(target: Target = PRODUCTION_TARGET, *, env_path: Optional[Path] = None) -> dict:
    summary: dict = {"command": "on", "ok": False, "wrote": False, "recreated": False}
    activation_started = False
    try:
        env_path = resolve_env_path(env_path or target.env_file)
        say("S0", "preflight (fail-closed)")
        data = await preflight_on(target, env_path)     # 사전검사가 검증한 **그** bytes
        observation = await measure(target, env_path, data)
        planned = plan_enable(data)
        pending = activation_marker_exists(env_path)
        if planned == data and classify(observation) == STATE_ON and not pending:
            # ⚠️ 멱등 — 교차 검증상 이미 ON 이면 **운영 컨테이너를 헛되이 재생성하지 않는다**
            #    (`off` 와 대칭). `_apply` 는 무조건 recreate 하므로 여기서 갈라야 한다.
            if env_path.read_bytes() != data:
                raise Abort("사전검사 이후 .env 가 변경됐다 — ON no-op 으로 접지 않는다")
            say("ON", "교차 검증상 이미 ON — no-op(운영 컨테이너 무접촉)")
            summary["ok"] = True
            summary["state"] = STATE_ON
            return summary
        say("S1", "계획 수립 통과")
        ensure_activation_marker(env_path)              # ⛔ write-ahead: ON write보다 먼저
        summary["activation_marker"] = True
        activation_started = True
        await _apply(target, env_path, data, planned, True, summary)
        clear_activation_marker(env_path)               # commit point: 전 검증 통과 뒤
        summary["activation_marker_removed"] = True
    except BaseException as exc:                       # noqa: BLE001  취소·signal 도 되돌린다
        summary["error"] = f"{type(exc).__name__}: {exc}"
        # ⛔ **`wrote` 에 종속시키지 않는다.** `atomic_write_bytes` 는 `os.replace` **뒤**
        #    디렉터리 fsync 에서 실패할 수 있고, 그때 파일은 true 인데 `wrote` 는 False 다.
        #    이미 true 인 재진입에서 **검증만** 실패한 경우도 `wrote=False` 다.
        #    → preflight 를 통과한 뒤의 실패는 관측으로 생략하지 않고 OFF 수렴을 시도한다.
        #    (`env_flip` 에서 똑같은 결함을 고쳐 놓고 여기서 재현했다 — 같은 실수를 두 번 했다.)
        if activation_started:
            summary["converge"] = await _converge_off(target, env_path)
        return summary
    summary["ok"] = True
    return summary


async def turn_off(target: Target = PRODUCTION_TARGET, *, env_path: Optional[Path] = None,
                   _retry_cancel: bool = True) -> dict:
    """⛔ **긴급 차단 경로** — 전제를 최소로 둔다. health·안전창·파일권한·one-shot 을 요구하지 않는다."""
    summary: dict = {"command": "off", "ok": False, "wrote": False, "recreated": False}
    try:
        env_path = resolve_env_path(env_path or target.env_file)   # symlink/비정규 파일 거부
        await preflight_common(target)                # 엉뚱한 스택만 막는다
        data = env_path.read_bytes()
        observation = await measure(target, env_path, data)
        planned = plan_disable(data)
        # ⛔ no-op 판정은 **교차 검증을 통과한 `OFF`** 일 때만이다. 파일·container·dispatcher 만
        #    보면 `inspect=true`·FX 관측 실패·중복 행이 있어도 "성공"으로 반환된다 —
        #    긴급 차단 명령이 아무 것도 안 하고 ok 를 주는 최악의 false green 이다.
        if planned == data and classify(observation) == STATE_OFF:
            if env_path.read_bytes() != data:
                raise Abort("OFF 관측 이후 .env 가 변경됐다 — no-op 으로 접지 않는다")
            say("OFF", "교차 검증상 이미 OFF — no-op(운영 컨테이너 무접촉)")
            summary["activation_marker_removed"] = clear_activation_marker(env_path)
            summary["ok"] = True
            summary["state"] = STATE_OFF
            return summary
        await _apply(target, env_path, data, planned, False, summary)
        summary["activation_marker_removed"] = clear_activation_marker(env_path)
    except BaseException as exc:                       # noqa: BLE001
        summary["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, asyncio.CancelledError) and _retry_cancel:
            # The first signal cancels the active await.  Repeated signals are
            # ignored by make_signal_canceller, so one fresh OFF attempt can
            # finish the coarse kill instead of leaving file/runtime split.
            say("CONVERGE", "OFF 중 취소 — 멱등 off 를 한 번 더 완료한다")
            retry = await turn_off(target, env_path=env_path, _retry_cancel=False)
            summary["converge"] = retry
        return summary
    summary["ok"] = True
    return summary


async def report_status(target: Target = PRODUCTION_TARGET, *,
                        env_path: Optional[Path] = None) -> dict:
    env_path = resolve_env_path(env_path or target.env_file)
    # Read-only does not mean target-agnostic.  Without this check an ambient
    # COMPOSE_PROJECT_NAME/DOCKER_HOST can combine the local env file with a
    # different stack and produce a stable-looking but false ON/OFF result.
    await preflight_common(target)
    observation = await measure(target, env_path)
    try:
        observation["operation_locked"] = env_operation_is_locked(env_path)
    except EnvOperationLocked:
        observation["operation_locked"] = None
    try:
        observation["activation_pending"] = activation_marker_exists(env_path)
    except Abort:
        observation["activation_pending"] = None
    file_value = observation.get("file_value")
    return {
        "command": "status",
        "state": classify(observation),
        "file_is_true": is_true(file_value),
        "file_line_count": observation.get("file_line_count"),
        "container": observation.get("container"),
        "inspect_match": observation.get("inspect"),
        "dispatcher_endpoint": observation.get("dispatcher_endpoint"),
        "file_auth_stage": observation.get("file_auth_stage"),
        "auth_stage": observation.get("auth_stage"),
        "fx": observation.get("fx"),
        "fx_topic_enabled": observation.get("fx_topic_enabled"),
        "other_tool_backup": observation.get("other_tool_backup"),
        "operation_locked": observation.get("operation_locked"),
        "activation_pending": observation.get("activation_pending"),
    }


# ── CLI ────────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="운영 TOPIC_DISPATCHER_ENABLED on/off/status", allow_abbrev=False)
    parser.add_argument("command", choices=["on", "off", "status"])
    return parser


async def amain(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "status":
        try:
            summary = await report_status()
        except Exception as exc:                       # cancellation은 BaseException이라 그대로 전파
            summary = {
                "command": "status", "state": STATE_UNKNOWN,
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        try:
            # The lock covers the complete CLI transaction, including ON
            # failure convergence.  Acquiring inside turn_off would deadlock
            # when turn_on calls it while already holding this same lock.
            with env_operation_lock(PRODUCTION_TARGET.env_file, f"topic_flag:{args.command}"):
                summary = await (turn_on() if args.command == "on" else turn_off())
        except EnvOperationLocked as exc:
            summary = {"command": args.command, "ok": False, "error": str(exc)}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if args.command == "status":
        # ⛔ 안정 상태만 0 — UNKNOWN/DRIFTED/AMBIGUOUS/PENDING_RECREATE 를 0 으로 주면
        #    자동화가 "괜찮다"로 읽고, 그건 관측 실패를 정상으로 접는 것과 같다.
        return 0 if summary.get("state") in STABLE_STATES else 2
    return 0 if summary.get("ok") else 1


async def amain_with_signals(argv: Optional[Sequence[str]] = None) -> int:
    """첫 SIGHUP/SIGTERM 만 취소로 바꾸고 **반복은 무시**한다(canary 와 같은 계약)."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(amain(argv))
    received: dict = {"signum": None}
    handler = make_signal_canceller(task, received)
    installed = []
    for signum in CANCEL_ON_SIGNALS:
        try:
            loop.add_signal_handler(signum, handler, signum)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        result = await task
        if received["signum"] is not None:
            print(f"[SIGNAL] {received['signum']} — 정리 완료 후 종료", file=sys.stderr)
            return 128 + received["signum"]
        return result
    except asyncio.CancelledError:
        if received["signum"] is None:
            raise
        return 128 + received["signum"]
    finally:
        for signum in installed:
            try:
                loop.remove_signal_handler(signum)
            except Exception:                          # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain_with_signals()))
