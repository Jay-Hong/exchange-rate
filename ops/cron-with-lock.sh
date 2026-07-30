#!/usr/bin/env bash
#
# cron 작업을 **배포와 공유하는 lock** 아래에서 실행한다.
#
# 왜 이 파일이 있는가: 배포 스크립트가 crontab의 `flock` 표기를 직접 파싱해 대기 정책을 검증하던
# 동안 **fail-open이 세 번 연속** 나왔다 — `flock -n` / `flock --timeout 0` / `flock -w0`가 전부
# "무한 대기"로 통과했다(실측). shell 옵션 문법은 whitelist하기 어렵고, 놓친 표기는 전부
# **안전한 쪽이 아니라 위험한 쪽으로** 통과한다. 그래서 정책을 **이름**으로 만들고 검증을
# 토큰 확인으로 축소한다 — 배포 스크립트는 이제 shell 행을 파싱하지 않는다.
#
# 사용 (crontab):
#   5 * * * * cd ~/exchange-rate && ops/cron-with-lock.sh --policy wait  -- /usr/bin/docker compose run --rm fastapi python scripts/hourly_...
#   1 15 * * * cd ~/exchange-rate && ops/cron-with-lock.sh --policy block -- /usr/bin/docker compose run --rm fastapi python scripts/daily_...
#
# 정책 (⚠️ 어느 쪽을 쓸지는 **운영 결정**이다 — 이 파일은 둘 다 제공만 한다):
#   wait  — `flock -w N`. N초 안에 못 잡으면 **이 회차를 건너뛴다**(exit 75 = EX_TEMPFAIL).
#           hourly append에 적합: 최근 2일을 idempotent re-roll하므로 **다음 회차가 회복**한다.
#   block — `flock`(무한 대기). **건너뛰지 않는다.**
#           daily append에 적합: 그 날 row를 한 번만 쓰므로 skip이 곧 **데이터 공백**이다.
#           ⛔ 대가: lock이 stuck이면 이 작업이 조용히 매달린다(skip 대신 hang). 어느 실패를
#              받아들일지가 정책의 본질이고, 그 선택은 이 파일이 하지 않는다.
set -Eeuo pipefail

FLOCK_BIN="${FLOCK_BIN:-flock}"
LOCK_FILE="${LOCK_FILE:-/var/lock/fxi-deploy.lock}"
WAIT_SECONDS="${CRON_LOCK_WAIT:-600}"
POLICY=""

while [ $# -gt 0 ]; do
    case "$1" in
        --policy) POLICY="${2:?--policy 값 필요}"; shift 2 ;;
        --)       shift; break ;;
        *) echo "알 수 없는 인자: $1" >&2; exit 2 ;;
    esac
done
[ $# -gt 0 ] || { echo "실행할 명령이 없다 (`--` 뒤에 명령을 둘 것)" >&2; exit 2; }

exec 200>"$LOCK_FILE"
case "$POLICY" in
    wait)
        if ! "$FLOCK_BIN" -w "$WAIT_SECONDS" 200; then
            echo "lock 대기 timeout(${WAIT_SECONDS}s) — 이 회차를 건너뛴다(다음 회차가 회복)." >&2
            exit 75   # EX_TEMPFAIL — cron 로그에 실패로 남는다(조용한 skip 금지)
        fi ;;
    block)
        "$FLOCK_BIN" 200 ;;   # 무한 대기 — 건너뛰지 않는다
    *)
        echo "정책이 필요하다: --policy wait|block" >&2; exit 2 ;;
esac

exec "$@"
