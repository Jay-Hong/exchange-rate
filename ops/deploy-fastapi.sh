#!/usr/bin/env bash
#
# fastapi 이미지 전환 배포 — 태그 전환 + recreate + 검증 + 실패 복구를 **한 단위**로 수행한다.
#
# 왜 스크립트인가: 이 절차를 메모리의 복사 코드로 두는 동안 셸 제어흐름 결함을 **연속 3번** 냈다
# (① 실행 전 차단만 막고 runtime 원자성 없음 → ② signal trap 반환 후 본문이 계속돼 rc=0 오탐
#  → ③ set -u만 써서 실패 명령이 통과 + 성공 조건이 health뿐이라 recreate 미적용을 성공으로 오판).
# 복사 코드는 검증 대상이 아니므로 같은 부류가 계속 재발한다. tests/test_ops_deploy_fastapi.py가
# 실패 주입으로 이 파일의 8개 경로를 잠근다.
#
# 왜 원자성이 필요한가: 이 서버의 cron 5개가 전부 `docker compose run --rm fastapi ...`이고
# **`latest` 태그를 쓴다**. 태그만 바뀌고 recreate가 안 되면 **cron은 신 이미지 / 웹은 구 이미지**인
# split 상태가 되고, 그 조합은 아무도 검증한 적이 없다.
#
# ⛔ **이 스크립트가 막는 것은 *지속적인* split이다.** retag 직후부터 recreate 완료까지의
#    **일시적 창**은 막지 못한다 — 그 사이 cron이 발화하면 검증되지 않은 조합이 실제로 한 번 돈다.
#    이 창은 이 스크립트가 만든 것이 아니라 `latest`를 배포와 cron이 **공유하는 토폴로지**의
#    성질이고, 그동안의 수동 배포에도 있었다.
#    **근본 해법은 배포와 cron 5개가 공유하는 host lock**이고 그것이 **구현됐다**(아래).
#    cron은 `ops/cron-job.sh <job>` launcher를 통해 같은 lock을 잡고, 이 스크립트는 **준비부터
#    수렴까지** 그 lock을 쥔다. cron 충돌 가드는 그 위의 belt-and-braces다.
#
# ⛔ **준비(pull·build)도 lock 안이어야 한다.** compose의 fastapi 서비스는 `image:` 키가 없어
#    `docker compose build fastapi`가 **`latest`를 움직인다**(실측: build 직후 latest=신 /
#    running=구 = split). 그 순간 cron은 신 이미지, web은 구 이미지다. `git pull`도 host의
#    `ops/cron-job.sh`를 즉시 바꿔 **새 launcher가 구 이미지를 호출**할 수 있다.
#    그래서 `--prepare-from <ref>`가 pull·build를 lock 안에서 수행하고, build가 움직인 `latest`를
#    **즉시 복원**한 뒤 immutable 태그(`…:sha-<gitsha>`)로 고정한다 — cron은 그 전이를 못 본다.
#
# 사용:
#   ops/deploy-fastapi.sh --target exchange-rate-fastapi:pending-<sha> \
#                         --fallback exchange-rate-fastapi:rollback-<date>
#   (롤백은 두 인자를 스왑한다 — 방향만 다른 같은 절차다.)
#
# 테스트 seam (프로덕션에서는 건드리지 않는다):
#   DOCKER_BIN / HEALTH_RETRIES / HEALTH_SLEEP / COMPOSE_DIR
# ⚠️ `set -e`는 **defense-in-depth**다 — 실제 게이트는 아래 `converged_to`(running == target)이고,
#    변이 실측상 `set -u`로 되돌려도 테스트는 전부 green이다(수렴 검사가 tag 실패를 이미 잡는다).
#    그래도 두는 이유: 수렴 검사가 못 보는 실패(예: cd 실패)를 조용히 통과시키지 않기 위해서다.
set -Eeuo pipefail

DOCKER_BIN="${DOCKER_BIN:-docker}"
HEALTH_RETRIES="${HEALTH_RETRIES:-18}"
HEALTH_SLEEP="${HEALTH_SLEEP:-5}"
COMPOSE_DIR="${COMPOSE_DIR:-$HOME/exchange-rate}"
LATEST_TAG="${LATEST_TAG:-exchange-rate-fastapi:latest}"
SERVICE="${SERVICE:-fastapi}"
CONTAINER="${CONTAINER:-exchange-rate-app}"

CRON_MARGIN="${CRON_MARGIN:-4}"
ALLOW_CRON_WINDOW=no
PREPARE_REF=""
TARGET=""
FALLBACK=""
while [ $# -gt 0 ]; do
    case "$1" in
        --target)   TARGET="${2:?--target 값 필요}"; shift 2 ;;
        --fallback) FALLBACK="${2:?--fallback 값 필요}"; shift 2 ;;
        --service)  SERVICE="${2:?}"; shift 2 ;;
        --allow-cron-window) ALLOW_CRON_WINDOW=yes; shift ;;
        --prepare-from) PREPARE_REF="${2:?--prepare-from 값 필요}"; shift 2 ;;
        *) echo "알 수 없는 인자: $1" >&2; exit 2 ;;
    esac
done
# ⚠️ `--prepare-from`이면 target을 **우리가 만든다** — 그때는 `--target`을 받지 않는다
#    (받으면 "빌드한 것"과 "배포할 것"이 갈릴 수 있다).
if [ -n "$PREPARE_REF" ] && [ -n "$TARGET" ]; then
    echo "--prepare-from 과 --target 은 함께 쓸 수 없다 (target은 빌드 결과로 정해진다)" >&2
    exit 2
fi
[ -n "$TARGET" ] || [ -n "$PREPARE_REF" ] || { echo "--target 또는 --prepare-from 필수" >&2; exit 2; }
[ -n "$FALLBACK" ] || { echo "--fallback 필수" >&2; exit 2; }

cd "$COMPOSE_DIR"

# ── host lock (선행 조건) ────────────────────────────────────────────────────
# cron 5개와 **공유**하는 lock이 있어야 일시적 split 구간에 cron이 끼어드는 것을 실제로 막는다.
# ⛔ **비대칭 정책**: cron은 **대기**(`flock -w <초>`)하고 배포는 **즉시 거부**(`flock -w 0`)한다.
#    - cron을 `-w 0`으로 두면 daily append가 **조용히 건너뛰어 데이터 공백**이 된다.
#    - 배포를 대기시키면 긴 cron 뒤에 붙어 예산·타이밍 가정이 전부 깨진다.
# ⛔ **crontab이 lock을 잡도록 바뀌지 않으면 배포 쪽만 잡아도 무의미하다.** 그래서 산문 경고가
#    아니라 **preflight 게이트**로 확인한다 — 감싸이지 않은 cron 줄이 있으면 실행을 거부한다.
FLOCK_BIN="${FLOCK_BIN:-flock}"
CRONTAB_BIN="${CRONTAB_BIN:-crontab}"
# ── lock 경로: 단일 정본 (같은 디렉터리의 lock.conf) ─────────────────────────
# ⛔ **env 재정의 없음.** `FXI_LOCK_CONF` 같은 손잡이를 두면 결함이 한 단계 이동할 뿐이다 —
#    배포와 cron은 **별도 프로세스**이므로 한쪽 환경만 바꿀 수 있고, 그러면 배제가 사라진다
#    ("재정의하면 함께 움직인다"는 내 추론이 틀렸다: cron daemon의 환경에는 전파되지 않는다).
#    테스트는 스크립트와 정본을 임시 디렉터리에 **함께 복사**해 실행한다(설치 단위로 격리).
_HERE="$(cd "$(dirname "$0")" && pwd)"
LOCK_CONF="$_HERE/lock.conf"
[ -f "$LOCK_CONF" ] || { echo "lock 정본이 없다: $LOCK_CONF" >&2; exit 5; }
# shellcheck source=/dev/null
. "$LOCK_CONF"
: "${FXI_DEPLOY_LOCK:?lock 정본에 FXI_DEPLOY_LOCK 이 없다}"
ALLOW_UNLOCKED_CRON="${ALLOW_UNLOCKED_CRON:-no}"

# ⛔ 위치도 env로 바꿀 수 없다 — 설치 단위 밖의 정본을 가리키면 검증이 무의미해진다.
CRON_JOB_SCRIPT="$_HERE/cron-job.sh"

# ⛔ **substring으로 shell 의미를 추정하지 않는다.** 추정하던 동안 fail-open이 다섯 번 나왔고
#    마지막 셋이 원리적이었다(실측 — 전부 통과했다):
#      ① `… --policy wait -- true && docker compose run …`  (wrapper가 lock을 놓은 뒤 docker 실행)
#      ② `LOCK_FILE=/tmp/other.lock … -- docker compose run …` (배포와 다른 lock = 상호배제 아님)
#      ③ `--policy waiter` (substring 매치 → 통과, 런타임에만 실패)
#    줄이 임의 shell인 한 무엇을 확인해도 그 **옆에서** 다른 일을 할 수 있다.
#    그래서 crontab에서 임의성을 없앴다 — `ops/cron-job.sh <job>`이 정책과 docker 명령을 둘 다
#    소유하고, 여기서는 crontab이 그 정본 출력과 **문자열로 같은지**만 본다(추정 0).
if ! CRON_SNAPSHOT="$("$CRONTAB_BIN" -l 2>/dev/null)"; then
    echo "거부: crontab 조회 실패 — cron의 lock 적용 여부를 확인할 수 없다." >&2
    exit 5
fi
if ! CRON_EXPECTED="$("$CRON_JOB_SCRIPT" --print-crontab 2>/dev/null)"; then
    echo "거부: 정본 crontab을 생성할 수 없다 ($CRON_JOB_SCRIPT --print-crontab)" >&2
    exit 5
fi

# ⛔ **대상 줄을 고르지 않는다.** 고르던 동안 수집 자체를 회피하는 형태가 통과했다
#    (실측: `docker  compose`[공백 2개] / `docker-compose`). 무엇을 고르든 그 밖의 표기가 남는다.
#    그래서 **모든 비주석 줄**이 정본이거나 `ops/cron-allowlist.txt`에 선언돼 있어야 한다.
#    선언되지 않은 줄은 lock 밖에서 docker를 돌릴 수 있으므로 거부한다.
CRON_ALLOWLIST="$_HERE/cron-allowlist.txt"
if [ ! -f "$CRON_ALLOWLIST" ]; then
    echo "거부: cron allowlist 파일이 없다 ($CRON_ALLOWLIST)" >&2; exit 5
fi
# ⚠️ `|| true` 필수 — 비주석 줄이 0개면 `grep`이 1을 반환하고 `set -e`가 스크립트를 **조용히**
#    죽인다(rc=1, 메시지 없음). fail-closed이긴 하나 진단이 사라져 원인을 알 수 없다(실측).
_strip() { sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | grep -v '^#' | grep -v '^$' | sort || true; }
_actual="$(printf '%s\n' "$CRON_SNAPSHOT" | _strip)"
_declared="$( { printf '%s\n' "$CRON_EXPECTED"; cat "$CRON_ALLOWLIST"; } | _strip )"

if [ "$ALLOW_UNLOCKED_CRON" != yes ] && [ "$_actual" != "$_declared" ]; then
    echo "거부: crontab이 선언된 집합과 다르다." >&2
    echo "  정본: \`$CRON_JOB_SCRIPT --print-crontab\` / 그 외 허용: $CRON_ALLOWLIST" >&2
    echo "--- 선언되지 않은 줄 ---" >&2
    comm -23 <(printf '%s\n' "$_actual") <(printf '%s\n' "$_declared") >&2 || true
    echo "--- 선언됐는데 없는 줄 ---" >&2
    comm -13 <(printf '%s\n' "$_actual") <(printf '%s\n' "$_declared") >&2 || true
    echo "  ⚠️ 선언되지 않은 줄은 lock 밖에서 docker를 돌릴 수 있다 — 그래서 거부한다." >&2
    echo "  (테스트·긴급용 우회: ALLOW_UNLOCKED_CRON=yes)" >&2
    exit 5
fi
_TOTAL="$(printf '%s\n' "$_declared" | grep -c . || true)"
echo "[lock-preflight] crontab ${_TOTAL}줄 전부 선언됨 (정본 + $CRON_ALLOWLIST)"

exec 200>"$FXI_DEPLOY_LOCK"
if ! "$FLOCK_BIN" -w 0 200; then
    echo "거부: 다른 작업이 $FXI_DEPLOY_LOCK 을 쥐고 있다 (cron 실행 중일 가능성). 나중에 다시 시도." >&2
    exit 4
fi
echo "[lock] 획득 — 준비부터 수렴 완료까지 유지한다"

# ── cron 충돌 가드 (상태 변경 **전**) ────────────────────────────────────────
# 실측 crontab(UTC): 매시 :05 :07 :09 :11 + 매일 15:01. 배포 예산(recreate + health 최대 90s)이
# 그 발화에 겹치면 **일시적 split 구간에서 cron이 검증되지 않은 조합을 실행**한다.
# ⛔ **시각 산술만으로는 근본적으로 닫히지 않는다** — :11에 시작된 작업이 :12에 아직 돌고 있는지
#    시계로는 알 수 없다. 그래서 아래 **host lock이 선행 조건**이고, 이 가드는 그 위의 belt-and-braces다
#    (lock을 잡으려 대기하는 대신 애초에 겹칠 시각을 피한다).
minutes_to_next_cron() {
    local hh="$1" mm="$2" best=9999 m cand
    # ⛔ `-gt`가 아니라 `-ge`다 — **현재 분이 곧 발화 분**일 수 있다(실측: 00:11이 "54분"으로 통과했다).
    for m in 5 7 9 11; do
        if [ "$m" -ge "$mm" ]; then cand=$(( m - mm )); [ "$cand" -lt "$best" ] && best="$cand"; fi
    done
    cand=$(( 60 - mm + 5 )); [ "$cand" -lt "$best" ] && best="$cand"   # 다음 시각 최초 발화
    # ⛔ `-lt 1`이면 15:01 **정각**을 놓친다(실측: 15:01이 "4분"으로 통과했다).
    if [ "$hh" -eq 15 ] && [ "$mm" -le 1 ]; then
        cand=$(( 1 - mm )); [ "$cand" -lt "$best" ] && best="$cand"     # 같은 시각 15:01
    fi
    if [ "$hh" -eq 14 ]; then
        cand=$(( 60 - mm + 1 )); [ "$cand" -lt "$best" ] && best="$cand" # 다음 시각이 15:01
    fi
    echo "$best"
}
if [ "$ALLOW_CRON_WINDOW" = no ]; then
    NOW_HM="${NOW_UTC:-$(date -u +%H:%M)}"
    _HH=$(( 10#${NOW_HM%%:*} )); _MM=$(( 10#${NOW_HM##*:} ))
    LEFT="$(minutes_to_next_cron "$_HH" "$_MM")"
    # ⛔ `-lt`면 margin과 **정확히 같은** 시각이 통과한다(실측: LEFT=4, margin=4가 통과했다).
    if [ "$LEFT" -le "$CRON_MARGIN" ]; then
        echo "거부: ${LEFT}분 뒤 cron이 발화한다 (UTC ${NOW_HM}, margin=${CRON_MARGIN}분)." >&2
        echo "  일시적 split 구간에 cron이 겹치면 검증되지 않은 조합이 실행된다." >&2
        echo "  cron 발화 후 다시 시도하거나, 불가피하면 --allow-cron-window 로 명시 우회." >&2
        exit 3
    fi
    echo "[cron-guard] 다음 발화까지 ${LEFT}분 (margin ${CRON_MARGIN}) — 통과"
else
    echo "[cron-guard] ⚠️ --allow-cron-window — 일시적 split에 cron이 겹칠 수 있다" >&2
fi

# ⛔ `docker images --format '{{.ID}}'`를 쓰면 안 된다 — **축약 ID**(12자, prefix 없음)를 준다.
#    비교 대상인 `docker inspect <container> --format '{{.Image}}'`는 **`sha256:` 전체 ID**다.
#    실측(운영 서버): images → `48434889c5d6` / inspect → `sha256:48434889c5d6…a25563`.
#    두 형식을 그대로 비교하면 수렴 판정이 **영구 실패**하고, 그러면 recover가 성공한 배포를
#    자동 롤백한 뒤 그것도 검증 실패로 보고한다. `docker image inspect --format '{{.Id}}'`로
#    양쪽을 전체 ID로 통일한다(실측: inspect의 `{{.Image}}`와 문자열 동일).
image_id() {
    "$DOCKER_BIN" image inspect "$1" --format '{{.Id}}' </dev/null 2>/dev/null | head -1 || true
}
running_id() {
    "$DOCKER_BIN" inspect "$CONTAINER" --format '{{.Image}}' </dev/null 2>/dev/null | head -1
}
is_healthy() {
    # ⛔ `grep -q healthy`로 쓰면 안 된다 — **"unhealthy"에도 매치된다**(실패 주입 테스트가 잡았다).
    #    상태 필드를 그대로 대조한다. 공백 유무는 직렬화 설정에 따라 달라질 수 있어 허용한다.
    "$DOCKER_BIN" compose exec -T "$SERVICE" \
        curl -s --max-time 5 http://localhost:8000/health </dev/null 2>/dev/null \
        | grep -Eq '"status" *: *"healthy"'
}

# ── preflight: 어느 상태도 바꾸기 **전에** 두 태그가 실제로 존재하는지 확인한다.
#    없는 태그로 진행하면 tag가 실패하고 그 뒤 복구도 대상이 없어 손쓸 수 없다.
FALLBACK_ID="$(image_id "$FALLBACK")"
[ -n "$FALLBACK_ID" ] || { echo "PREFLIGHT 실패: fallback 태그 없음 ($FALLBACK) — 상태 무변화" >&2; exit 1; }
if [ -z "$PREPARE_REF" ]; then
    TARGET_ID="$(image_id "$TARGET")"
    [ -n "$TARGET_ID" ] || { echo "PREFLIGHT 실패: target 태그 없음 ($TARGET) — 상태 무변화" >&2; exit 1; }
    echo "[preflight] target=$TARGET($TARGET_ID) fallback=$FALLBACK($FALLBACK_ID)"
else
    echo "[preflight] fallback=$FALLBACK($FALLBACK_ID) / target은 lock 안에서 빌드한다"
fi

# ── 목표 도달 판정: health **그리고** running == 기대 ID **그리고** latest 태그 == 기대 ID.
#    ⛔ health만 보면 recreate가 조용히 미적용된 경우(구 컨테이너가 그대로 healthy)를 성공으로 오판한다.
#    ⛔ `latest`까지 봐야 하는 이유 — 이 서버의 cron 5개가 `docker compose run`으로 **latest를 쓴다**.
#       `latest`와 running이 갈리면 그게 곧 split(cron 한쪽 / 웹 다른쪽)이다. running만 보는 판정은
#       **복구 경로에서 특히 위험하다**: retag 실패 + 복구 recreate 실패면 latest=target·running=fallback인
#       split인데 "복구 완료"를 출력한다(선택적 실패 주입으로 재현 —
#       `test_recovery_without_retag_is_not_reported_as_success`).
converged_to() {
    local want="$1" i
    for i in $(seq 1 "$HEALTH_RETRIES"); do
        if is_healthy \
           && [ "$(running_id)" = "$want" ] \
           && [ "$(image_id "$LATEST_TAG")" = "$want" ]; then
            echo "  수렴 확인 (~$((i * HEALTH_SLEEP))s, image=$want, latest 일치)"
            return 0
        fi
        sleep "$HEALTH_SLEEP"
    done
    return 1
}

_BUILD_WT=""
cleanup_worktree() {
    [ -n "$_BUILD_WT" ] || return 0
    "${GIT_BIN:-git}" worktree remove --force "$_BUILD_WT" >/dev/null 2>&1 || true
    rm -rf "$_BUILD_WT" >/dev/null 2>&1 || true
    _BUILD_WT=""
}

RECOVERED=no
recover() {
    # ⛔ 진입 즉시 자기 trap을 해제한다 — 아래 명령들이 다시 trap을 발화시키면 무한 재귀가 된다.
    trap - EXIT HUP INT TERM
    # ⚠️ 임시 worktree 정리는 **복구보다 먼저**, 그리고 반드시 여기서도 한다 — build 중 종료되면
    #    /tmp 디렉터리와 git worktree metadata가 남는다(실측 지적).
    cleanup_worktree
    [ "$RECOVERED" = no ] || return 0
    RECOVERED=yes
    echo "!!! 미완 — FALLBACK($FALLBACK)으로 복구한다" >&2
    # ⚠️ 태그 복구가 실패해도 **recreate는 계속한다**(외부 검토의 "중단" 권고와 다른 선택).
    #    근거: 여기서 멈추면 latest=target·running=fallback인 **split이 남는다**. 계속하면 running이
    #    latest(=target)를 따라가 최소한 **일관된** 상태가 되고, 그때 아래 수렴 검사가 fallback에
    #    도달하지 못했음을 **loud하게** 알린다. 즉 "일관 + 시끄러운 실패" > "split + 조용한 성공".
    "$DOCKER_BIN" tag "$FALLBACK" "$LATEST_TAG" </dev/null \
        || echo "  ✗ 태그 복구 실패 — 수동 개입 필요" >&2
    "$DOCKER_BIN" compose up -d --force-recreate "$SERVICE" </dev/null >/dev/null 2>&1 \
        || echo "  ✗ 복구 recreate 실패 — 수동 개입 필요" >&2
    if converged_to "$FALLBACK_ID"; then
        echo "  복구 완료 — fallback 이미지로 수렴" >&2
    else
        echo "  ✗✗ 복구 후에도 미수렴 — 즉시 수동 개입 필요" >&2
    fi
}

# ⛔ signal과 EXIT은 **다른 handler**여야 한다. bash signal trap은 handler가 반환하면 **본문을 계속
#    실행**하므로(격리 실험 실측: 통합 handler → TERM → trap → 본문 계속 → rc=0 / 분리 → rc=1),
#    signal 쪽은 명시적으로 종료해야 한다.
#    ⚠️ 이 분리는 **테스트로 잠겨 있지 않다** — 오탐이 나는 창(수렴 확인 직후 `trap -` 직전)을
#    재현하려면 본 스크립트 PID로 signal을 보내야 하는데 가짜 docker에서는 명령 치환 서브셸이
#    가로막아 실패했다. 타이밍 의존 테스트를 CI에 넣지 않기로 하고 구조로만 닫았다.
#    ⛔ 그러므로 이 두 줄을 통합 handler로 "단순화"하지 말 것 — 테스트가 red를 내지 않는다.
trap 'recover; exit 1' HUP INT TERM
trap recover EXIT
# ★ 여기부터 상태를 바꾼다 — trap 무장 이후여야 한다.

if [ -n "$PREPARE_REF" ]; then
    # ⛔ **live worktree를 pull하지 않는다.** pull은 host의 `ops/cron-job.sh`·`lock.conf`를 즉시
    #    바꾸는데, 그 뒤 build나 health가 실패하면 recover는 **이미지만** 되돌려 launcher와 이미지
    #    세대가 갈린다(신 launcher + 구 이미지). 더 나쁘게, pull된 커밋이 `lock.conf` 경로를 바꾸면
    #    진행 중 배포는 **구 lock**을 쥔 채이고 새 cron은 **신 lock**을 잡아 즉시 동시 실행된다.
    #    그래서 **별 worktree**에서 빌드하고, live worktree 갱신은 **수렴 성공 뒤**에 한다
    #    (실패하면 live는 손대지 않은 상태로 남는다 — launcher·이미지가 함께 구 세대다).
    GIT_BIN="${GIT_BIN:-git}"
    "$GIT_BIN" fetch origin "$PREPARE_REF" >/dev/null 2>&1 \
        || { echo "거부: git fetch origin $PREPARE_REF 실패" >&2; exit 1; }
    _SHA="$("$GIT_BIN" rev-parse FETCH_HEAD)"        # ⛔ **전체 SHA** — 축약은 충돌 가능
    [ -n "$_SHA" ] || { echo "거부: FETCH_HEAD 를 해소할 수 없다" >&2; exit 1; }

    # ⛔ **runtime이 live worktree에서 읽는 파일이 바뀌면 거부한다.**
    #    이 스크립트는 **이미지만** 원자적으로 바꿀 수 있다. cron은 live worktree의
    #    `ops/*.sh`(launcher·정본·lock.conf)와 `docker-compose.yml`을 읽으므로, 대상 커밋이
    #    그것들을 바꾸면 "이미지는 신 세대 / 그 파일들은 구 세대"가 되고 이 스크립트로는 그 둘을
    #    한 트랜잭션에 넣을 수 없다(pull을 끼우면 pull 성공 직후 signal에서 세대가 갈린다 — 실측 지적).
    #    ⚠️ `app/`·`scripts/`는 bind-mount가 **아니라**(마운트는 data·logs·firebase json 3개, 실측)
    #      runtime에 영향이 없다. 그래서 그것들이 바뀌는 배포는 이미지 전환만으로 완결된다.
    _RUNTIME_PATHS="ops docker-compose.yml"
    if ! "$GIT_BIN" diff --quiet HEAD FETCH_HEAD -- $_RUNTIME_PATHS; then
        echo "거부: 이 ref가 runtime이 읽는 파일을 바꾼다 — 이미지 전환만으로 완결되지 않는다." >&2
        echo "  변경된 경로:" >&2
        # ⚠️ `|| true` 필수 — `git diff`는 변경이 있으면 **비영 종료**이고, 진단 출력용 파이프라인이
        #    `set -e`에 잡히면 남은 안내 메시지가 통째로 사라진다(같은 부류를 이미 한 번 겪었다).
        "$GIT_BIN" diff --name-only HEAD FETCH_HEAD -- $_RUNTIME_PATHS 2>/dev/null \
            | sed 's/^/    /' >&2 || true
        echo "  이 부류는 **host-config migration**이 필요하다(launcher·crontab·compose를 함께 옮기는" >&2
        echo "  별도 절차). versioned release directory + 원자적 symlink 전환이 근본 해법이고" >&2
        echo "  이 스크립트 범위 밖이다 — 지금은 거부가 정직한 답이다." >&2
        exit 1
    fi

    TARGET="exchange-rate-fastapi:sha-$_SHA"
    echo "[prepare] ref=$PREPARE_REF sha=$_SHA target=$TARGET"

    # ⛔ 같은 SHA를 재빌드해 기존 태그를 **덮어쓰지** 않는다 — immutable의 의미가 사라진다.
    _EXISTING="$(image_id "$TARGET")"

    _BUILD_WT="$(mktemp -d)"
    "$GIT_BIN" worktree add --detach "$_BUILD_WT" FETCH_HEAD >/dev/null 2>&1 \
        || { echo "거부: build worktree 생성 실패" >&2; exit 1; }

    # 별 compose 프로젝트명 → 빌드 결과가 `<project>-fastapi:latest`라 **live latest를 안 건드린다**
    # (compose 파일에 `name:`이 없어 프로젝트명이 이미지명을 정한다 — 실측).
    _BUILD_PROJECT="fxi-build"
    if ! "$DOCKER_BIN" compose -p "$_BUILD_PROJECT" -f "$_BUILD_WT/docker-compose.yml" \
            build "$SERVICE" </dev/null >/dev/null 2>&1; then
        # ⚠️ 여기서 inline 정리를 하지 **않는다** — 하면 trap 의 정리가 가려져(테스트가 구별 못 함)
        #    signal 경로에서 worktree 가 남는 결함이 회귀해도 red 가 안 난다(실측 생존).
        #    정리는 종료 처리(trap)가 단독으로 진다.
        echo "거부: build 실패 (live worktree·latest 무변화)" >&2; exit 1
    fi
    _BUILT_ID="$(image_id "$_BUILD_PROJECT-$SERVICE:latest")"
    cleanup_worktree
    [ -n "$_BUILT_ID" ] || { echo "거부: build 결과 이미지를 찾을 수 없다" >&2; exit 1; }

    if [ -n "$_EXISTING" ] && [ "$_EXISTING" != "$_BUILT_ID" ]; then
        echo "거부: $TARGET 가 이미 **다른** 이미지($_EXISTING)를 가리킨다 — immutable 위반." >&2
        echo "  같은 SHA가 다른 결과를 낸다는 뜻이다(dirty tree·base image 변동 등). 원인을 먼저 볼 것." >&2
        exit 1
    fi
    "$DOCKER_BIN" tag "$_BUILT_ID" "$TARGET" </dev/null \
        || { echo "거부: immutable 태그 실패" >&2; exit 1; }
    TARGET_ID="$(image_id "$TARGET")"
    [ -n "$TARGET_ID" ] || { echo "거부: immutable 태그가 해소되지 않는다" >&2; exit 1; }
    echo "[prepare] live worktree·latest 무변화 — 전환 성공 뒤에 pull한다"
fi
# ★ lock은 스크립트 종료 시 fd 200이 닫히며 자동 해제된다(retag 전부터 수렴 후까지 덮는다).

"$DOCKER_BIN" tag "$TARGET" "$LATEST_TAG" </dev/null
echo "[tag] $LATEST_TAG -> $TARGET"

"$DOCKER_BIN" compose up -d --force-recreate "$SERVICE" </dev/null >/dev/null 2>&1
echo "[recreate] 요청 완료"

if ! converged_to "$TARGET_ID"; then
    echo "!!! 목표 이미지로 수렴하지 못했다" >&2
    exit 1   # EXIT trap이 recover를 수행한다
fi

# ⛔ **live worktree를 pull하지 않는다.** 구 구현은 수렴 뒤 pull했는데, pull 성공 직후
#    HUP/TERM이 오면 recover가 **이미지만** 되돌려 신 launcher + 구 이미지가 남았다(실측 지적).
#    pull을 트랜잭션에 넣으면 그 창을 닫을 수 없고(소스 롤백은 실행 중 스크립트를 덮어쓴다),
#    **빼는 것**이 유일한 닫는 방법이다. 위 preflight가 runtime 파일 변경을 거부하므로
#    live worktree의 launcher·compose는 대상 커밋과 **의미상 동일**하고, 남는 차이(`app/` 등)는
#    bind-mount가 아니라 runtime에 영향이 없다.
#    ⚠️ 따라서 `git log`는 배포된 것을 반영하지 않는다 — 배포 기록은 **immutable 이미지 태그**
#      (`…:sha-<full>`)와 `docker inspect`가 진다. live 소스 갱신은 언제든 별도로 하면 되고,
#      실패해도 아무것도 불일치하지 않는다.
if [ -n "$PREPARE_REF" ]; then
    echo "[finalize] live worktree 는 그대로 둔다 (runtime 파일 동일 — 배포 기록은 이미지 태그)"
fi

# ★ 목표 수렴을 확인한 **뒤에만** 해제한다.
cleanup_worktree
trap - EXIT HUP INT TERM
echo "[완료] running == $TARGET ($TARGET_ID), health OK"
exit 0
