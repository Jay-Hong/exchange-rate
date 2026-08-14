#!/usr/bin/env bash
# fxi-managed: ops/run_rc_canary.sh
#
# RC canary 의 검증된 host-side runner. 스크립트 provenance → 출력 선생성 → 최소권한 env →
# pinned image one-shot → process-level outer timeout 순서로만 실행한다.
#
# 왜 필요한가 (검토 실측 4건):
#  - 출력 디렉터리가 없으면 docker 가 root:root 0755 로 만들어 appuser(1000)가 못 쓴다.
#    호출자(ubuntu, uid 1000)가 **미리** 만들어야 한다.
#  - `--env-file .env` 는 RC 키 1개를 위해 관리자/DB/Telegram/KIS 등 전부를 전달한다.
#    **RC 키 한 줄만** 필터한 0600 임시 파일로 좁힌다. 키는 argv·stdout 에 싣지 않는다.
#  - canary 의 deadline 은 협력적이다 — 취소를 삼키는 callee 는 상한을 넘긴다.
#    `timeout` 뒤 EXIT trap의 named-container kill/remove가 실제 외부 호출 상한을 만든다.
#  - 산출물에 실행 이미지 provenance 가 없었다 — pinned image ID 를 env 로 주입한다.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/home/ubuntu/exchange-rate}"
OUTPUT_DIR="${OUTPUT_DIR:-/home/ubuntu/logs/rc-canary}"
CANARY_SCRIPT="scripts/rc_canary.py"
RUNNER_SCRIPT="ops/run_rc_canary.sh"
CONTAINER="exchange-rate-app"
OUTER_TIMEOUT_MARGIN_SECONDS=60
KILL_AFTER_SECONDS=15
env_file=""
container_name=""
source_head=""

die() {
  echo "중단: $*" >&2
  exit 1
}

require_clean_source() {
  # ⛔ 설치기(install-topic-auth-capture.sh)와 같은 3중 가드 — mount 될 바이트가
  #    검토·push 된 그 바이트임을 강제한다.
  local path
  for path in "$CANARY_SCRIPT" "$RUNNER_SCRIPT"; do
    git -C "$REPO_ROOT" ls-files --error-unmatch -- "$path" >/dev/null 2>&1 || \
      die "$path 가 git 에 추적되지 않는다"
    git -C "$REPO_ROOT" diff --quiet -- "$path" || \
      die "$path 가 HEAD와 다르다 (unstaged drift)"
    git -C "$REPO_ROOT" diff --cached --quiet -- "$path" || \
      die "$path 가 HEAD와 다르다 (staged drift)"
  done
  local upstream head upstream_head
  upstream=$(git -C "$REPO_ROOT" rev-parse --symbolic-full-name '@{upstream}' 2>/dev/null) || \
    die "현재 branch에 upstream remote-tracking ref가 없다"
  case "$upstream" in
    refs/remotes/*) ;;
    *) die "upstream이 remote-tracking ref가 아니다: $upstream" ;;
  esac
  head=$(git -C "$REPO_ROOT" rev-parse HEAD)
  upstream_head=$(git -C "$REPO_ROOT" rev-parse "$upstream")
  [ "$head" = "$upstream_head" ] || \
    die "실행 HEAD가 local upstream ref와 다르다: HEAD=$head $upstream=$upstream_head"
  source_head="$head"
}

cleanup() {
  local status=$?
  trap - EXIT
  if [ -n "$container_name" ] && docker inspect "$container_name" >/dev/null 2>&1; then
    docker kill "$container_name" >/dev/null 2>&1 || true
    docker container rm "$container_name" >/dev/null 2>&1 || true
  fi
  [ -z "$env_file" ] || unlink "$env_file" 2>/dev/null || true
  exit "$status"
}

trap cleanup EXIT

validated_outer_timeout() {
  local iterations="$1" cadence="$2" call_timeout="$3" max_runtime="$4"
  python3 - "$REPO_ROOT/$CANARY_SCRIPT" "$iterations" "$cadence" \
    "$call_timeout" "$max_runtime" "$OUTER_TIMEOUT_MARGIN_SECONDS" <<'PY'
import importlib.util
import math
import pathlib
import sys

path, raw_iterations, raw_cadence, raw_call_timeout, raw_runtime, raw_margin = sys.argv[1:]
spec = importlib.util.spec_from_file_location("rc_canary_runner_validation", pathlib.Path(path))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    iterations = int(raw_iterations)
    if str(iterations) != raw_iterations or not 1 <= iterations <= module.MAX_ITERATIONS:
        raise ValueError
    cadence = float(raw_cadence)
    call_timeout = float(raw_call_timeout)
    runtime = float(raw_runtime)
    module._require_finite("cadence_seconds", cadence, module.MIN_CADENCE_SECONDS, 3600.0)
    module._require_finite("call_timeout_seconds", call_timeout, 0.1, module.MAX_CALL_TIMEOUT_SECONDS)
    module._require_finite("max_runtime_seconds", runtime, 1.0, module.MAX_RUNTIME_SECONDS_CAP)
except (ValueError, module.CanaryContractError):
    raise SystemExit(2)
print(math.ceil(runtime) + int(raw_margin))
PY
}

run_canary() {
  local uid="$1"; shift
  local max_runtime="$1"; shift
  local iterations="$1"; shift
  local cadence="$1"; shift
  local call_timeout="$1"; shift

  require_clean_source
  [ "${#uid}" -le 128 ] || die "전용 UID는 128자를 넘을 수 없다"
  case "$uid" in *$'\n'*|*$'\r'*) die "전용 UID에 개행을 넣을 수 없다" ;; esac
  local outer_timeout
  outer_timeout=$(validated_outer_timeout "$iterations" "$cadence" "$call_timeout" "$max_runtime") || \
    die "canary 수치 인자가 계약 범위를 벗어났다"

  local rc_key_count rc_nonempty_count
  rc_key_count=$(grep -Ec '^REVENUECAT_API_KEY=' "$REPO_ROOT/.env" || true)
  rc_nonempty_count=$(grep -Ec '^REVENUECAT_API_KEY=.+' "$REPO_ROOT/.env" || true)
  [ "$rc_key_count" -eq 1 ] && [ "$rc_nonempty_count" -eq 1 ] || \
    die "REVENUECAT_API_KEY는 .env에 비어 있지 않은 한 줄로만 정의돼야 한다 — 호출 0회로 중단"

  # ⛔ docker 가 만들기 전에 호출자(uid 1000 == appuser)가 만들어야 컨테이너가 쓸 수 있다.
  mkdir -p "$OUTPUT_DIR"
  chmod 700 "$OUTPUT_DIR"
  command -v flock >/dev/null 2>&1 || die "flock 명령이 없다"
  exec 9>"$REPO_ROOT/.git/rc-canary.lock"
  chmod 600 "$REPO_ROOT/.git/rc-canary.lock"
  flock -n 9 || die "다른 RC canary runner가 이미 실행 중이다"

  local image
  image=$(docker inspect "$CONTAINER" --format '{{.Image}}') || \
    die "실행 중 컨테이너($CONTAINER)의 pinned image 를 읽지 못했다"
  [[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]] || die "pinned image ID 형식이 잘못됐다"

  # ⛔ RC 키 **한 줄만** — 값은 argv·stdout 에 싣지 않는다.
  # (env_file 은 global — `local` 이면 EXIT trap 시점에 unbound 가 돼 set -u 가 죽인다. 실측)
  env_file=$(umask 077 && mktemp)
  grep -E '^REVENUECAT_API_KEY=' "$REPO_ROOT/.env" > "$env_file"
  printf 'CANARY_UID=%s\n' "$uid" >> "$env_file"
  printf 'CANARY_IMAGE_ID=%s\n' "$image" >> "$env_file"
  printf 'CANARY_RUNNER_SOURCE_HEAD=%s\n' "$source_head" >> "$env_file"
  printf 'CANARY_RUNNER_SHA256=%s\n' \
    "$(sha256sum "$REPO_ROOT/$RUNNER_SCRIPT" | awk '{print $1}')" >> "$env_file"
  printf 'CANARY_OUTER_TIMEOUT_SECONDS=%s\n' "$outer_timeout" >> "$env_file"

  # timeout은 docker CLI만 죽일 수 있으므로 이름을 고정하고 EXIT trap에서 남은 container도
  # 반드시 kill/remove한다. 그렇지 않으면 CLI 종료 뒤 RC 프로세스가 계속 돌 수 있다(실측).
  container_name="fxi-rc-canary-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  timeout --kill-after="$KILL_AFTER_SECONDS" "$outer_timeout" \
    docker run --rm --name "$container_name" \
      --user "$(id -u):$(id -g)" \
      --env-file "$env_file" \
      -v "$REPO_ROOT/$CANARY_SCRIPT:/app/$CANARY_SCRIPT:ro" \
      -v "$OUTPUT_DIR:/out" \
      "$image" \
      python "$CANARY_SCRIPT" \
        --max-runtime-seconds "$max_runtime" \
        --iterations "$iterations" \
        --cadence-seconds "$cadence" \
        --call-timeout-seconds "$call_timeout" \
        --output-dir /out
  container_name=""
}

main() {
  local uid="" max_runtime="600" iterations="10" cadence="5" call_timeout="10"
  while [ $# -gt 0 ]; do
    case "$1" in
      --uid|--max-runtime-seconds|--iterations|--cadence-seconds|--call-timeout-seconds)
        [ $# -ge 2 ] || die "$1 값이 필요하다"
        case "$1" in
          --uid) uid="$2" ;;
          --max-runtime-seconds) max_runtime="$2" ;;
          --iterations) iterations="$2" ;;
          --cadence-seconds) cadence="$2" ;;
          --call-timeout-seconds) call_timeout="$2" ;;
        esac
        shift 2
        ;;
      *) die "허용하지 않는 인자: $1" ;;
    esac
  done
  [ -n "$uid" ] || die "--uid <전용 canary UID> 가 필요하다 (운영 사용자 금지)"
  run_canary "$uid" "$max_runtime" "$iterations" "$cadence" "$call_timeout"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
