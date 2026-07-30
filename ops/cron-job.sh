#!/usr/bin/env bash
#
# cron 작업의 **유일한 진입점**. 이름만 받아 lock 정책과 docker 명령을 **둘 다 소유**한다.
#
# 왜 이렇게 하는가: crontab에 임의 shell을 두고 배포가 그 텍스트를 검사하는 동안 fail-open이
# **다섯 번** 나왔다. 마지막 셋이 결정적이다(실측 — 전부 통과했다):
#   ① `… cron-with-lock.sh --policy wait -- true && docker compose run …`
#      → wrapper가 `true`를 실행하고 **lock을 놓은 뒤** docker가 돈다. 검사는 두 토큰을 다 본다.
#   ② `LOCK_FILE=/tmp/other.lock cron-with-lock.sh … -- docker compose run …`
#      → 배포와 **다른 lock**이라 애초에 상호배제가 아니다.
#   ③ `--policy waiter` → substring `--policy wait`에 매치돼 통과하고 런타임에만 실패한다.
# 공통 원인: **줄이 임의 shell인 한 substring 검사로 의미를 추정할 수 없다.** 무엇을 확인해도
# 그 옆에서 다른 일을 할 수 있다. 그래서 crontab에서 임의성을 제거한다 —
# crontab은 아래 `--print-crontab` 출력과 **정확히 일치**해야 하고, 배포는 문자열 동일성만 본다.
#
# 사용:
#   ops/cron-job.sh --print-crontab     # 정본 crontab 출력(설치·검증 공용 단일 소스)
#   ops/cron-job.sh <job>               # cron이 부르는 형태
set -Eeuo pipefail

# ── lock 정본 (배포·wrapper·cron이 공유) ─────────────────────────────────────
# ── lock 경로: 단일 정본 (ops/lock.conf) ─────────────────────────────────────
# ⛔ 개별 fallback 금지. 재정의는 `FXI_LOCK_CONF`로 **정본 파일을 갈아끼우는 것**뿐이고,
#    그러면 배포·cron·wrapper가 함께 움직여 상호배제가 유지된다.
_HERE_LOCK="$(cd "$(dirname "$0")" && pwd)"
FXI_LOCK_CONF="${FXI_LOCK_CONF:-$_HERE_LOCK/lock.conf}"
[ -f "$FXI_LOCK_CONF" ] || { echo "lock 정본이 없다: $FXI_LOCK_CONF" >&2; exit 5; }
# shellcheck source=/dev/null
. "$FXI_LOCK_CONF"
: "${FXI_DEPLOY_LOCK:?lock 정본에 FXI_DEPLOY_LOCK 이 없다}"

HERE="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="${WRAPPER:-$HERE/cron-with-lock.sh}"
DOCKER_BIN="${DOCKER_BIN:-/usr/bin/docker}"

# ── job 표 (이름 → 정책 + 명령 + 로그) ──────────────────────────────────────
# 정책 근거: hourly append는 최근 2일을 idempotent re-roll하므로 한 회차 skip을 다음 회차가
# 회복한다(wait 허용). daily는 그 날 row를 한 번만 쓰므로 skip이 곧 데이터 공백이다(block).
# ⚠️ block은 stuck lock에서 hang이라는 대가가 있다 — 어느 실패를 받아들일지가 운영 결정이고
#    이 표가 그 결정을 **명시적으로 한 곳에** 담는다(전에는 crontab 5줄에 흩어져 있었다).
job_spec() {
    case "$1" in
        daily-all)        echo "block|1 15|daily_append.log|scripts/daily_append_source_daily_rates.py --source all --write --allow-production-write" ;;
        hourly-bithumb)   echo "wait|5 *|hourly_append.log|scripts/hourly_append_source_hourly_rates.py --write --allow-production-write" ;;
        hourly-investing) echo "wait|7 *|hourly_append_investing.log|scripts/hourly_append_investing_source_hourly_rates.py --currency all --write --allow-production-write" ;;
        hourly-hana)      echo "wait|9 *|hourly_append_hana.log|scripts/hourly_append_hana_source_hourly_rates.py --currency all --write --allow-production-write" ;;
        hourly-krx)       echo "wait|11 *|krx_hourly_append.log|scripts/hourly_append_krx_source_hourly_rates.py --write --allow-production-write" ;;
        *) return 1 ;;
    esac
}
JOB_NAMES="daily-all hourly-bithumb hourly-investing hourly-hana hourly-krx"

print_crontab() {
    local j spec policy sched log args
    for j in $JOB_NAMES; do
        spec="$(job_spec "$j")"
        policy="${spec%%|*}"; spec="${spec#*|}"
        sched="${spec%%|*}";  spec="${spec#*|}"
        log="${spec%%|*}";    args="${spec#*|}"
        : "$policy" "$args"   # 정본 줄에는 정책·명령이 들어가지 않는다(launcher가 소유)
        echo "$sched * * * cd ~/exchange-rate && ops/cron-job.sh $j >> ~/logs/$log 2>&1"
    done
}

if [ "${1:-}" = "--print-crontab" ]; then
    print_crontab; exit 0
fi

JOB="${1:?job 이름이 필요하다 (또는 --print-crontab)}"
SPEC="$(job_spec "$JOB")" || { echo "알 수 없는 job: $JOB" >&2; exit 2; }
POLICY="${SPEC%%|*}"; SPEC="${SPEC#*|}"
SPEC="${SPEC#*|}"          # schedule 버림
SPEC="${SPEC#*|}"          # log 버림
ARGS="$SPEC"

# shellcheck disable=SC2086  # ARGS는 이 파일이 소유하는 고정 문자열이다(외부 입력 아님)
exec "$WRAPPER" --policy "$POLICY" -- \
        "$DOCKER_BIN" compose run --rm fastapi python $ARGS
