"""운영 `.env` 의 `ENV=development` → `ENV=production` **1행 플립**.

목적은 로그 형식이 아니라 **안전장치 복원**이다. `ENV != production` 이면
(1) 기동 시 `ADMIN_PASSWORD` 필수 검사가 발동하지 않고 (2) `verify_admin` 이 값 부재 시
`admin1234` 로 폴백한다(`app/main.py` 의 `verify_admin` / startup 가드). 지금은 값이 있어
**잠재 노출**이지만 fail-closed 가 아니다.

## 왜 스크립트인가 — 수동 절차가 이미 닫은 결함을 되살렸다

한때 이 작업을 `sed -i 's/^ENV=development$/ENV=production/'` + 임의 이름 backup 으로
제안했다. **셋 다 틀렸다**:

⛔ `sed` 는 (1) fsync 가 없어 크래시 시 rename 후 내용이 날아갈 수 있고 (2) **0-match 를
   성공처럼 통과**시키며 (3) 키가 중복되면 전부 바꾼다. 이 리포엔 이미
   `atomic_write_bytes`(mkstemp+fsync+`os.replace`+디렉터리 fsync)가 있다.
⛔ `.env.pre-*` 같은 **임의 이름 backup 은 `.gitignore` 에 걸리지 않는다** — `.gitignore` 의
   `.env` 는 정확히 그 이름만 잡는다. 운영 리포엔 이미 unignored `.env.bak*` 가 쌓여 있다.
   여기서는 ignore 되는 `.env.canary-backup`(`*.canary-backup*`) 하나만 쓴다.
⛔ 수동 절차엔 **signal 처리가 없다**. SSH 가 끊기면(SIGHUP) 기본 동작이 프로세스를 죽여
   복원이 한 줄도 돌지 않는다 — canary 에서 이미 겪은 계열이다.

## 순서가 계약이다

1. **signal handler 를 backup 보다 먼저 설치한다.** 그래야 backup 이 생긴 뒤의 어떤 중단도
   정리 경로를 갖는다. 첫 signal 만 취소로 바꾸고 **반복 signal 은 무시**한다 — 정리 중에
   다시 취소가 오면 복원의 `await` 가 끊긴다(`make_signal_canceller` 재사용).
2. **순수 read 검증을 backup 보다 먼저 한다.** `EnvRestorer.restore()` 에는 no-op 분기가 없어
   **무조건 컨테이너를 재생성**한다. 읽기 전용 실패가 운영 재생성을 유발하면 안 된다.
3. **정리는 상태를 안다.** write 전이면 backup 만 지운다(재생성 없음). write 후면 복원한다.
4. **`finally: restore()` 를 두지 않는다.** 성공 경로는 변경을 **유지**하고 backup 만 지운다.
   그 뒤 `restore()` 가 불리면 backup 이 없어 예외가 되어 **성공한 절차가 실패로** 끝난다.
5. **성공 commit point = 전 smoke 통과 + backup unlink + 디렉터리 fsync 까지**. unlink 만
   하고 fsync 전에 죽으면 backup 이 되살아나 "미적용"과 구분되지 않는다.

## 컨테이너의 ENV 는 `env_file` 이 아니라 `environment:` 에서 온다

`docker-compose.yml` 의 fastapi 는 `env_file: [.env]` 와 함께
`environment: - ENV=${ENV:-production}` 을 갖는다. `environment:` 가 `env_file` 을 이기고,
`${...}` interpolation 은 **셸 환경변수가 `--env-file` 을 이긴다**. 파일 검증이 전부 green 인데
컨테이너는 안 바뀌는 경로가 여기다 → ambient 변수를 **fail-closed 로 거부**한다.
거부 목록은 하드코딩하지 않고 **compose 파일에서 실제로 뽑는다**(추가되는 변수를 놓치지 않는다).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.canary_monitor import (  # noqa: E402  ⛔ 기존 primitive 재사용 — 수정 0
    CANCEL_ON_SIGNALS,
    PRODUCTION_TARGET,
    CollectorError,
    EnvRestorer,
    Target,
    admin_fetch_command,
    atomic_write_bytes,
    backup_path_for,
    check_target_identity,
    container_env,
    create_env_backup,
    make_signal_canceller,
    parse_curl_response,
    read_env_values,
    recreate_and_verify_health,
    run_command,
    validate_canary_start_env,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

FROM_LINE = b"ENV=development"
TO_LINE = b"ENV=production"
FLAG_KEYS = ("TOPIC_DISPATCHER_ENABLED", "KRX_CLIENT_DISTRIBUTION_ENABLED")

#: compose/docker 자체의 **대상 선택** 변수. `PRODUCTION_TARGET` 은 `-p`·`-f` 를 붙이지 않으므로
#: (`Target.project`·`compose_file` 이 `None`) 이 변수들이 그대로 대상을 바꾼다.
TARGET_SELECTORS = (
    "COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES",
    "DOCKER_HOST", "DOCKER_CONTEXT",
)

#: ⛔ 값을 출력하지 않는다 — 일치할 때만 리터럴 `MATCH` 를 낸다.
#: (`{{json .Config.Env}}` 는 ADMIN_PASSWORD·KIS_APP_SECRET·DATABASE_URL 을 전량 덤프한다.)
MATCH_FMT = '{{range .Config.Env}}{{if eq . "ENV=%s"}}MATCH{{end}}{{end}}'

#: ⛔ 상태코드만 낸다. `parse_curl_response` 는 **non-2xx 를 전부** `CollectorError` 로 접어
#: 401(정상 거부)과 503(ADMIN_PASSWORD 미전달 = 이 절차의 최악)을 구분하지 못한다.
WRONG_PW_PY = (
    "import base64,sys,urllib.request,urllib.error\n"
    "c=base64.b64encode(b'admin:this-is-deliberately-wrong').decode()\n"
    "r=urllib.request.Request('http://localhost:8000'+sys.argv[1],"
    "headers={'Authorization':'Basic '+c})\n"
    "try:\n"
    "    with urllib.request.urlopen(r,timeout=5) as x: code=x.status\n"
    "except urllib.error.HTTPError as e: code=e.code\n"
    "sys.stdout.write(str(code))\n"
)

#: ⚠️ fastapi 는 `expose` 만 하고 host port 를 publish 하지 않는다 — 호스트에서는 닿지 않는다.
#: 컨테이너 안에서 연결해야 **앱을 실제로 통과**했음이 증명된다.
WS_PY = (
    "import asyncio,json,sys,websockets\n"
    "async def m():\n"
    "    ws=await asyncio.wait_for(websockets.connect('ws://localhost:8000/ws'),timeout=10)\n"
    "    try: msg=json.loads(await asyncio.wait_for(ws.recv(),timeout=20))\n"
    "    finally: await ws.close()\n"
    "    rates=(msg.get('data') or {}).get('rates') or []\n"
    "    sys.stdout.write(str(msg.get('type'))+' '+str(len(rates)))\n"
    "asyncio.run(m())\n"
)


class Abort(Exception):
    """중단 사유. ⚠️ write 이전인지 이후인지는 **호출부의 상태**가 안다."""


def say(step: str, detail: str = "") -> None:
    """⛔ `.env` 본문 라인·secret 값은 절대 여기로 나가지 않는다."""
    print(f"[{step}] {detail}", flush=True)


# ── 순수 함수 (테스트가 직접 부른다) ────────────────────────────────────────


def compose_interpolated_names(text: str) -> set:
    """compose 파일이 interpolation 에 쓰는 변수 이름 전부.

    ⛔ 하드코딩하지 않는 이유: `environment:` 에 변수가 추가될 때마다 거부 목록이 조용히
    낡는다. 파일에서 뽑으면 **추가와 동시에 보호된다**(테스트가 이 동치를 잠근다).
    """
    names = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", text))
    names |= set(re.findall(r"\$(?!\{)([A-Za-z_][A-Za-z0-9_]*)", text))
    return names


def ambient_problems(environ: dict, interpolated: set) -> list:
    """⛔ **fail-closed.** 셸에 있는 값이 `--env-file` 을 이기므로, 하나라도 있으면 거부한다.

    ⚠️ 값은 담지 않는다 — `DATABASE_URL`·`REDIS_PASSWORD` 도 이 목록에 들어온다.
    """
    problems = []
    for name in sorted(interpolated & set(environ)):
        problems.append(f"셸에 {name} 이 설정돼 있다 — compose interpolation 에서 "
                        "--env-file 을 이긴다(값 미표시)")
    for name in TARGET_SELECTORS:
        if name in environ:
            problems.append(f"셸에 {name} 이 설정돼 있다 — compose 대상 스택이 바뀔 수 있다")
    return problems


def plan_new_bytes(data: bytes) -> bytes:
    """⛔ `ENV=` 로 시작하는 행이 **정확히 1개**이고 그것이 `development` 여야 한다.
    0개(이미 바뀜/키 없음)·2개 이상(중복)은 **바꾸기 전에** 중단한다.
    ⚠️ CRLF·마지막 개행 부재를 포함해 **나머지 전 bytes 를 보존**한다.
    """
    lines = data.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.rstrip(b"\r\n").startswith(b"ENV=")]
    if len(hits) != 1:
        raise Abort(f"`ENV=` 행이 {len(hits)}개다(정확히 1개여야 한다) — 변경하지 않았다")
    index = hits[0]
    core = lines[index].rstrip(b"\r\n")
    if core != FROM_LINE:
        raise Abort(f"`ENV=` 행이 기대와 다르다(길이 {len(core)}) — 변경하지 않았다")
    ending = lines[index][len(core):]                    # b"\r\n" | b"\n" | b""
    lines[index] = TO_LINE + ending
    new = b"".join(lines)
    expected = len(data) - len(FROM_LINE) + len(TO_LINE)
    if len(new) != expected:
        raise Abort("치환 결과 길이가 기대와 다르다 — 변경하지 않았다")
    return new


def check_file_invariants(text: str, stage: str) -> None:
    """flag 2종 false + `ADMIN_PASSWORD` non-empty. ⛔ 값은 출력하지 않는다.

    ⚠️ `ADMIN_PASSWORD` 가 비면 **production 전환이 곧 admin endpoint 503** 이다 —
    바로 그 fail-closed 를 켜는 작업이므로 여기서 먼저 막는다.
    """
    values = read_env_values(text, list(FLAG_KEYS) + ["ADMIN_PASSWORD"])
    problems = validate_canary_start_env(values)         # flag 값은 secret 이 아니다
    if problems:
        raise Abort(f"{stage}: {problems}")
    admin = values.get("ADMIN_PASSWORD")
    if admin is None or not admin.strip():
        raise Abort(f"{stage}: ADMIN_PASSWORD 가 없거나 비어 있다 "
                    "— production 에서 admin endpoint 가 503 이 된다")


def check_env_file_mode(path: Path) -> None:
    """운영 secret 파일은 소유자 외 읽기·쓰기를 허용하지 않는다."""
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise Abort(
            f"{path.name} 권한이 {mode:03o}다 — ENV 전환 전에 `chmod 600 {path.name}` 필요"
        )


def check_legacy_backup_modes(env_path: Path) -> None:
    """과거 수동 backup도 같은 secret 전체를 담으므로 느슨한 권한을 허용하지 않는다."""
    insecure = []
    for path in env_path.parent.glob(f"{env_path.name}.bak*"):
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            insecure.append(f"{path.name}({mode:03o})")
    if insecure:
        raise Abort(
            "owner-only가 아닌 과거 env backup이 있다: " + ", ".join(sorted(insecure))
            + " — ENV 전환 전에 `chmod 600 .env.bak*` 필요"
        )


def classify_stdout(logs: str) -> dict:
    """⚠️ **"모든 줄이 JSON"을 요구하면 false red 다.** `Dockerfile` 의 CMD 에 `--log-config`
    가 없어 uvicorn 자체 로거는 평문을 그대로 낸다 — 앱 로그만 JSON 이 된다.
    ⚠️ python-json-logger 는 `ensure_ascii` 기본값이라 한국어·이모지가 `\\uXXXX` 로
    escape 된다 — **원문 grep 은 뒤집힌다**. 줄 단위 JSON 파싱으로 판정한다.
    """
    app_json = 0
    startup_env = None
    errors = 0
    for line in logs.splitlines():
        if "ERROR" in line or "Traceback" in line:
            errors += 1
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        if {"timestamp", "level", "logger"} <= parsed.keys():   # CustomJsonFormatter 고정 필드
            app_json += 1
            if "env" in parsed and startup_env is None:
                startup_env = parsed["env"]                     # 기동 로그의 extra
    return {"app_json_lines": app_json, "startup_env": startup_env, "error_lines": errors}


def log_baseline(path: Path) -> dict:
    """⚠️ size 만으로는 **회전을 못 본다**(10MB, backupCount 3). inode 를 함께 잡는다."""
    if not path.exists():
        return {"exists": False, "inode": None, "size": 0}
    stat = path.stat()
    return {"exists": True, "inode": stat.st_ino, "size": stat.st_size}


def count_new_errors(path: Path, baseline: dict) -> int:
    """baseline 이후 구간의 ERROR/Traceback 수. ⛔ 내용은 반환하지 않는다(UID·경로가 섞인다).

    ⛔ **회전이 감지되면 fail-closed 로 중단한다.** 새 파일만 읽으면 회전 직전 구간을
    통째로 놓쳐 "ERROR 0" 이라는 **틀린 안심**을 만든다. 3분 창에서 10MB 회전이 일어난 것
    자체가 이상 신호이므로 사람이 봐야 한다.
    """
    if not path.exists():
        if baseline.get("exists"):
            raise Abort("error.log 가 사라졌다 — 회전/삭제 가능성, 자동 판정하지 않는다")
        return 0
    stat = path.stat()
    if baseline.get("exists") and stat.st_ino != baseline.get("inode"):
        raise Abort("error.log 의 inode 가 바뀌었다(회전) — 자동으로 ERROR 0 이라고 하지 않는다")
    if stat.st_size < baseline.get("size", 0):
        raise Abort("error.log 크기가 줄었다(회전/절단) — 자동으로 ERROR 0 이라고 하지 않는다")
    with path.open("rb") as handle:
        handle.seek(baseline.get("size", 0))
        tail = handle.read()
    return sum(1 for line in tail.splitlines()
               if b"ERROR" in line or b"Traceback" in line)


def unlink_durably(path: Path) -> None:
    """⛔ unlink 만 하고 죽으면 backup 이 되살아나 '미적용'과 구분되지 않는다.
    디렉터리 fsync 까지가 **성공 commit point** 다."""
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


# ── 관측 (전부 판정/개수만 반환) ────────────────────────────────────────────


async def inspect_value(target: Target, fmt: str) -> str:
    return (await run_command(target.inspect(fmt))).strip()


async def observe(what, label: str, attempts: int = 3, delay: float = 2.0):
    """⚠️ **관측 실패와 서비스 이상을 섞지 않는다.** `CollectorError` 는 "못 읽었다"는 뜻이라
    곧바로 rollback 사유로 쓰면 멀쩡한 서비스를 되돌린다."""
    last = None
    for attempt in range(attempts):
        try:
            return await what()
        except CollectorError as exc:
            last = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(delay)
    raise CollectorError(f"{label}: {attempts}회 모두 관측 실패 — {last}")


async def project_label(target: Target) -> str:
    raw = await run_command(target.inspect("{{json .Config.Labels}}"))
    labels = json.loads(raw)
    return labels.get("com.docker.compose.project") or ""


async def preflight(target: Target, env_path: Path, error_log: Path) -> dict:
    problems = ambient_problems(dict(os.environ), compose_interpolated_names(
        COMPOSE_FILE.read_text(encoding="utf-8")))
    if problems:
        raise Abort("; ".join(problems))

    identity = await check_target_identity(target)
    if identity:
        raise Abort(f"target identity: {identity}")

    oneoff = (await run_command(
        ["docker", "ps", "--filter", "label=com.docker.compose.oneoff=True", "-q"])).strip()
    if oneoff:
        raise Abort("cron one-shot 컨테이너가 실행 중이다 — 끝난 뒤에 다시 실행할 것")

    backup = backup_path_for(env_path)
    if backup.exists():
        raise Abort(
            f"이전 창의 backup 이 남아 있다: {backup.name} — ⛔ `--recover` 를 그냥 실행하지 말 것. "
            "성공 직후 중단이면 그것이 **완료된 보안 수정을 되돌린다**. "
            "컨테이너 ENV 를 먼저 보고 어느 상태인지 판정할 것")

    check_env_file_mode(env_path)
    check_legacy_backup_modes(env_path)
    check_file_invariants(env_path.read_text(encoding="utf-8"), "preflight")

    live = await observe(lambda: container_env(["ENV", *FLAG_KEYS], target), "preflight env")
    if live.get("ENV") != "development":
        raise Abort(f"컨테이너 현재 ENV={live.get('ENV')!r} — development 가 아니다")

    return {
        "container_id": await inspect_value(target, "{{.Id}}"),
        "project": await project_label(target),
        "error_log": log_baseline(error_log),
    }


async def smoke(target: Target, baseline: dict, error_log: Path) -> dict:
    """각 검사가 **무엇을 증명하는지**가 주석이다. 실패는 예외."""
    result: dict = {}

    # 1. 재생성이 실제로 일어났는가 — env 는 재생성 시에만 다시 읽힌다.
    container_id = await inspect_value(target, "{{.Id}}")
    if container_id == baseline["container_id"]:
        raise Abort("컨테이너 Id 가 그대로다 — 재생성되지 않았다(구 env 로 돌고 있다)")
    result["recreated"] = True

    # 2. 재생성 전후로 **같은 스택**인가(ambient 로 대상이 바뀌지 않았는가).
    identity = await check_target_identity(target)
    if identity:
        raise Abort(f"재생성 후 target identity: {identity}")
    project = await project_label(target)
    if project != baseline["project"]:
        raise Abort(f"compose project 가 바뀌었다: {baseline['project']!r} → {project!r}")

    # 3. 기동 중 crash 로 재시작을 반복하지 않았는가.
    restarts = await inspect_value(target, "{{.RestartCount}}")
    if restarts != "0":
        raise Abort(f"RestartCount={restarts} — 기동 중 crash 가 있었다")

    # 4. **생성 시점** env (authoritative). ⛔ 값 미출력 — 일치 여부만.
    if await inspect_value(target, MATCH_FMT % "production") != "MATCH":
        raise Abort("컨테이너 생성 시점 env 에 ENV=production 이 없다")
    result["config_env"] = "MATCH"

    # 5. exec 프로세스가 보는 env (독립 경로 교차 확인). `ENV`·flag 는 secret 이 아니다.
    live = await observe(lambda: container_env(["ENV", *FLAG_KEYS], target), "smoke env")
    if live.get("ENV") != "production":
        raise Abort(f"컨테이너 ENV={live.get('ENV')!r}")
    for key in FLAG_KEYS:
        if (live.get(key) or "false").strip().lower() != "false":
            raise Abort(f"{key} 가 false 가 아니다: {live.get(key)!r}")
    result["in_process_env"] = "production"

    # 6. **포맷터 전환 + 앱 프로세스가 읽은 ENV** 를 한 검사로 증명한다.
    #    JSON 파싱이 되면 포맷터가 바뀐 것이고, 그 안의 `env` 필드는 **앱이 실제로 읽은 값**이다.
    logs = await run_command(target.compose("logs", "--no-log-prefix", "fastapi"), timeout=90)
    classified = classify_stdout(logs)
    if classified["app_json_lines"] < 1:
        raise Abort("stdout 에 앱 JSON 로그가 한 줄도 없다 — 포맷터가 바뀌지 않았다")
    if classified["startup_env"] != "production":
        raise Abort(f"기동 로그의 env={classified['startup_env']!r} — 앱이 읽은 값이 다르다")
    result["stdout_json_lines"] = classified["app_json_lines"]
    result["startup_env"] = classified["startup_env"]

    # 7. admin 정상 인증. ⚠️ **이건 ENV 를 증명하지 않는다** — `verify_admin` 은
    #    ADMIN_PASSWORD 가 있으면 ENV 를 아예 읽지 않는다. **회귀 검사**일 뿐이다.
    await observe(lambda: _admin_ok(target, "/admin/api/redis-status"), "admin 200")
    result["admin_regression"] = "200"

    # 8. 잘못된 비밀번호는 **401**. ⛔ 503 이면 ADMIN_PASSWORD 가 컨테이너에 전달되지 않은 것이고
    #    production 에서 그건 admin endpoint 전체 불능이다(= 이 전환의 최악 결과).
    code = (await run_command(target.compose(
        "exec", "-T", "fastapi", "python", "-c", WRONG_PW_PY, "/admin/api/redis-status"))).strip()
    if code != "401":
        raise Abort(f"잘못된 비밀번호에 {code} 응답 — 401 이어야 한다"
                    + (" (503 = ADMIN_PASSWORD 미전달)" if code == "503" else ""))
    result["wrong_password_status"] = 401

    # 9. legacy WS 가 초기 payload 를 실제로 보내는가(앱을 통과했다는 증거).
    ws_out = (await run_command(target.compose(
        "exec", "-T", "fastapi", "python", "-c", WS_PY), timeout=60)).strip()
    kind, _, count = ws_out.partition(" ")
    if kind != "rates":
        raise Abort(f"legacy WS 초기 프레임 type={kind!r}")
    try:
        rate_count = int(count)
    except ValueError as exc:
        raise Abort(f"legacy WS rates 개수가 정수가 아니다: {count!r}") from exc
    if rate_count <= 0:
        raise Abort(f"legacy WS 초기 rates 가 비었다: {rate_count}")
    result["ws_initial"] = {"type": kind, "rate_count": rate_count}

    # 10. 신규 ERROR 0 — 컨테이너 stdout(재생성 후 전량) + error.log 의 baseline 이후.
    new_errors = classified["error_lines"] + count_new_errors(error_log, baseline["error_log"])
    if new_errors:
        raise Abort(f"신규 ERROR/Traceback {new_errors}건")
    result["new_errors"] = 0

    return result


async def _admin_ok(target: Target, path: str) -> str:
    parsed = parse_curl_response(await run_command(admin_fetch_command(path, target)))
    return "200" if isinstance(parsed, dict) else "?"


# ── orchestrator ───────────────────────────────────────────────────────────


async def run_flip(target: Target = PRODUCTION_TARGET, *,
                   env_path: Optional[Path] = None,
                   error_log: Optional[Path] = None) -> dict:
    env_path = Path(env_path or target.env_file).resolve()
    error_log = Path(error_log or (REPO_ROOT / "volumes" / "logs" / "app" / "error.log"))
    summary: dict = {"ok": False, "wrote": False, "rolled_back": False}

    try:
        say("S0", "preflight (읽기 전용 — 실패해도 잔재 0)")
        baseline = await preflight(target, env_path, error_log)
        say("S0", "통과")

        say("S1", "치환 계획 (순수 read — 실패해도 잔재 0)")
        data = env_path.read_bytes()
        new_data = plan_new_bytes(data)
        say("S1", f"통과 — 대상 1행, {len(data)} → {len(new_data)} bytes")
    except (Abort, CollectorError, OSError) as exc:
        # ⚠️ `OSError` 도 여기서 접는다 — preflight 는 `.env` 와 과거 backup 을 `stat`/`read`
        #    하므로 깨진 symlink·권한 문제가 **Abort 가 아닌 형태로** 올라올 수 있다. 그대로
        #    두면 summary 없이 traceback 만 남아 "어디까지 갔는지"가 안 보인다. 이 구간은
        #    **아직 아무것도 만들지 않았으므로** 그냥 요약을 남기고 끝내는 것이 맞다.
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary

    # ⛔ **backup 보다 먼저 만들 것이 없다** — 여기부터는 정리 경로가 필요하다.
    #    (signal handler 는 이 함수 **밖**에서 이미 설치돼 있다: `amain_with_signals`)
    #
    # ⚠️ **감수하는 잔재 하나**: `create_env_backup` 이 link 게시 **뒤**(디렉터리 fsync)에서
    #    실패하면 `backup` 지역변수는 아직 None 이라 아래 정리가 파일을 지우지 못한다.
    #    ⛔ 그렇다고 `backup_path_for(...).exists()` 로 바꾸면 **더 나쁘다** — backup 이 이미
    #    있어서(FileExistsError) 실패한 경우까지 지워 **남의 backup 을 파괴**한다.
    #    남는 것은 원본 사본 하나뿐이고 `.env` 는 그대로이므로 **안전한 잔재**이며, 다음 실행의
    #    preflight 가 그것을 발견해 무엇을 확인해야 하는지 안내한다. 그래서 고치지 않는다.
    say("S2", "durable backup")
    backup: Optional[Path] = None
    try:
        backup = create_env_backup(env_path)
        # S1 뒤 다른 프로세스가 env 를 바꿨다면 stale 계획으로 덮어쓰지 않는다. backup 은
        # 이 실행의 소유권 표식이기도 하므로, 비교가 끝난 뒤에만 S3 로 진행한다.
        if backup.read_bytes() != data or env_path.read_bytes() != data:
            raise Abort("S1 이후 .env 가 변경됐다 — stale 계획을 적용하지 않는다")
    except (Abort, CollectorError, OSError) as exc:
        if backup is not None:
            try:
                unlink_durably(backup)
            except OSError as unlink_exc:
                summary["backup_remove_error"] = str(unlink_exc)
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary
    say("S2", f"생성 {backup.name} (0600, gitignore 대상)")

    restorer = EnvRestorer(env_path, target)
    write_attempted = False
    try:
        say("S3", "원자적 write")
        write_attempted = True
        atomic_write_bytes(env_path, new_data)
        summary["wrote"] = True

        say("S4", "재생성 전 검증")
        after = env_path.read_bytes()
        if after != new_data:
            raise Abort("write 결과가 계획한 bytes 와 다르다")
        check_file_invariants(after.decode("utf-8"), "post-write")
        say("S4", "통과 — 계획 bytes 와 완전 일치, flag/ADMIN_PASSWORD 불변")

        say("S5", "force-recreate + health 대기")
        await recreate_and_verify_health(target)

        say("S6", "smoke")
        summary["smoke"] = await smoke(target, baseline, error_log)
        say("S6", "전 항목 통과")
    except BaseException as exc:                          # noqa: BLE001  취소·signal 도 정리한다
        summary["error"] = f"{type(exc).__name__}: {exc}"
        if write_attempted and not summary["wrote"]:
            # atomic_write_bytes 는 os.replace 뒤 디렉터리 fsync 에서 실패할 수 있다. 함수가
            # 예외를 냈다는 사실만으로 "write 전 실패"라고 분류하면 바뀐 env 를 남긴 채
            # backup 을 삭제한다. 현재 bytes 를 모르면 변경 가능성으로 보고 복원한다.
            try:
                summary["wrote"] = env_path.read_bytes() != data
            except OSError:
                summary["wrote"] = True
        if summary["wrote"]:
            say("ROLLBACK", "write 이후 실패 — backup 전체 복원")
            try:
                await restorer.restore()                  # 복원 → recreate → health → byte 동일 → unlink
                summary["rolled_back"] = True
                back = await inspect_value(target, MATCH_FMT % "development")
                summary["rollback_env_match"] = back
                if back != "MATCH":
                    summary["rollback_warning"] = "복원 후 컨테이너 ENV 가 development 가 아니다"
            except BaseException as restore_exc:          # noqa: BLE001
                summary["rollback_error"] = f"{type(restore_exc).__name__}: {restore_exc}"
                summary["manual"] = f"{backup_path_for(env_path)} 가 원본이다 — 수동 복원 필요"
        else:
            # ⚠️ **아무것도 바꾸지 않았다** — 복원하면 운영 컨테이너를 헛되이 재생성한다
            #    (`restore()` 에는 no-op 분기가 없다).
            say("ROLLBACK", "write 전 실패 — backup 만 제거(재생성 없음)")
            try:
                unlink_durably(backup)
                summary["backup_removed"] = True
            except OSError as unlink_exc:
                summary["backup_remove_error"] = str(unlink_exc)
        return summary

    # ⛔ 성공 경로는 **되돌리지 않는다**. 전 smoke 통과 뒤 unlink + 디렉터리 fsync 까지가
    #    성공 commit point 다.
    unlink_durably(backup)
    summary["ok"] = True
    summary["backup_removed"] = True
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="운영 .env 의 ENV 를 production 으로 1행 플립", allow_abbrev=False)
    parser.add_argument("--require-tmux", action="store_true",
                        help="tmux 밖 실행을 거부한다(운영 권장). signal 처리와 **직교**하다 — "
                             "handler 는 항상 설치된다")
    return parser


async def amain(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.require_tmux and not os.environ.get("TMUX"):
        print(json.dumps({"ok": False, "error": "tmux 세션 밖이다"}, ensure_ascii=False))
        return 1
    summary = await run_flip()
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary.get("ok") else 1


async def amain_with_signals(argv: Optional[Sequence[str]] = None) -> int:
    """⛔ handler 는 **backup 생성 전에** 설치된다(이 함수가 `run_flip` 을 감싼다).
    첫 SIGHUP/SIGTERM 만 취소로 바꾸고 **반복 signal 은 무시**한다 — 정리 중 재취소는
    복원의 `await` 를 끊는다(`make_signal_canceller` 재사용, canary 와 같은 계약).
    """
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
        print(f"[SIGNAL] {received['signum']} — 정리 완료 후 종료", file=sys.stderr)
        return 128 + received["signum"]
    finally:
        for signum in installed:
            try:
                loop.remove_signal_handler(signum)
            except Exception:                             # noqa: BLE001 — 종료 경로를 막지 않는다
                pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain_with_signals()))
