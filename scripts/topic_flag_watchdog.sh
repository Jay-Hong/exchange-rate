#!/bin/bash
# topic_flag bounded-rehearsal watchdog (v6).
#
# ⛔ 목적: 리허설의 상한을 **약속이 아니라 장치**로 만든다. main watchdog 이 살아 있는 동안에는
#    대화 세션이나 사람의 응답 없이 OFF 로 수렴한다.
#
# 인자: $1 = FIRE_AT epoch(수렴 시작)  $2 = CONTRACT_AT epoch(계약 상한)  $3 = 로그 파일
# 종료: 0 계약 내 OFF 확정·유지 / 3 OFF 확정했으나 상한 초과 / 1 give-up / 2 무장 실패 / 4 중복 무장
#
# ── 실측으로 확인된 결함 이력 (같은 실수를 되풀이하지 않기 위해 이름을 남긴다)
#  v1 (a) sleep 10 폴링 감지 지연  (b) 고정 횟수 재시도 → 실효 상한이 계약보다 김
#      (c) 10회 실패 후 포기        (d) stdout 만 써서 로그가 tmux 와 함께 소실
#  v2 (e) FIRE_AT 과거면 무장 거부 → 창 안 재무장 불가(가장 필요한 순간에 장치 소멸)
#      (f) epoch 상한 부재 → ms 오타면 영원히 대기하는데 모든 신호가 "무장 성공"
#      (g) 무장 실패가 무흔적(검증이 exec 앞) → 기동 명령은 0 을 반환
#      (h) 무제한 파괴적 재시도 → 6h 동안 운영 컨테이너 recreate. 포기하지 않을 대상은
#          **목표**이지 **행위**가 아니다
#  v3 (i) **계약 판정이 stale** — watcher 가 fresh status 가 아니라 최대 15초 전 파일을 읽어,
#          직전에 다시 ON 이 돼도 breach 를 놓쳤다. 계약 판정은 그 시각의 관측이어야 한다.
#      (j) **READY 가 watcher 보다 먼저** — READY 직후 죽으면 무장돼 보이지만 계약 감시자가 없다.
#      (k) **singleton 부재** — 재무장 시 두 인스턴스가 서로 다른 시각에 OFF 를 강제한다.
#  v4 (l) **observer 가 singleton FD 를 상속** — main 이 SIGKILL 되면 OFF 수렴 주체는 사라지지만
#         observer 가 flock 을 계속 보유해 새 watchdog 재무장까지 막았다. observer 는 fork 직후
#         FD 9 를 닫고, singleton 소유권은 수렴 main 에만 둔다.
#      (m) **retry sleep budget clamp 유실** — 계약까지 15초 남았는데 20초를 자며 마지막 시도
#         기회를 버렸다. 계약 전 sleep 은 남은 초를 넘지 않는다.
#  v5 (n) **장수 자식이 singleton FD 를 상속** — main SIGKILL 뒤 고아 `off`(최대 420s)나
#         `sleep`(최대 300s)이 락을 붙들어 재무장을 막았다. 모든 장수 자식은 FD 9 를 닫는다.
#  v6 (o) **죽은 metadata pid 를 근거로 flock 실패를 우회** — 같은 stale lock 을 본 watchdog 이
#         모두 lock 없이 무장할 수 있어 singleton 자체가 사라졌다. 실제 flock 이 유일한 판정이며,
#         metadata 는 진단 정보일 뿐 우회 근거로 쓰지 않는다.
#      (p) **status pipeline 밖 command-substitution shell 이 FD 를 보유** — 내부 명령만 닫아도
#         `state="$(probe_status)"` 의 대기 셸이 최대 90s 락을 붙들었다. 캡처 subshell 자체가 먼저
#         FD 9 를 영구 폐쇄한 뒤 probe 를 실행한다.
#      (q) **짧은 helper 치환도 FD 를 보유** — 평소 1ms 인 `date` 가 지연되면 같은 실패가 재현됐다.
#         실행 시간이 아니라 소유권 규칙으로 막는다: lock 획득 뒤 모든 command-substitution 은
#         첫 명령으로 `exec 9>&-` 를 실행한다.
#
# ⚠️ 알림 경로가 없다(운영 `TELEGRAM_ENABLED=false`). breach 로그도 sentinel 도 **기록일 뿐
#    아무도 부르지 않는다**. 계약 위반 시의 실질 보호는 계속되는 (유계) 재시도이지 경보가 아니다.
#  v6 (q) **자동 재무장자 부재** — FD 상속을 끊어 "재무장이 가능"해졌지만 그걸 할 주체가 없었다.
#         main 이 SIGKILL/OOM 으로 죽으면 observer 는 기록만 하고 OFF 를 강제하지 않아,
#         상한이 다시 "사람이 알아채면" 조건부가 됐다. → observer 가 main 생존을 함께 보고
#         (zombie·pid 재사용까지 구별) 죽으면 같은 계약을 `exec` 로 인계한다.
#
# ⚠️ **EC2 재부팅은 여전히 미커버다.** 재부팅되면 이 프로세스 계보 전체가 사라지고 컨테이너는
#    `.env` 그대로 살아난다. 이 공백은 명시적으로 수용한다 — 재부팅은 앱 끊김으로 즉시
#    드러나는 반면 main 사망은 조용하기 때문에, 조용한 쪽을 장치로 닫고 시끄러운 쪽을 남긴다.
set -u
umask 077

FALLBACK_EVIDENCE="$HOME/WATCHDOG_ARM_FAILED"
arm_fail() {
  { echo "[$(exec 9>&-; TZ=Asia/Seoul date '+%F %H:%M:%S' 2>/dev/null)] ARM FAILED: $*"; } >>"$FALLBACK_EVIDENCE" 2>/dev/null
  echo "ARM FAILED: $*" >&2
  exit 2
}

case "$#" in
  3) HANDOFF=0 ;;
  4) [ "$4" = "--handoff" ] || arm_fail "알 수 없는 4번째 인자: $4"; HANDOFF=1 ;;
  *) arm_fail "인자 3~4개 필요(받음 $#) — usage: $0 <fire_epoch> <contract_epoch> <logfile> [--handoff]" ;;
esac
FIRE_AT="$1"; CONTRACT_AT="$2"; LOG="$3"

# ⚠️ `cd` 전에 절대경로로 굳힌다 — 인계(exec)가 상대경로 `$0` 를 쓰면 작업 디렉터리가 바뀐 뒤 깨진다.
case "$0" in /*) SELF="$0" ;; *) SELF="$PWD/$0" ;; esac

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
: >>"$LOG" || arm_fail "로그 파일 열기 실패: $LOG"
chmod 600 "$LOG" 2>/dev/null || true
exec >>"$LOG" 2>&1
# ↑ 여기서부터 모든 출력·실패가 영속 파일에 남는다 (v2 결함 g)

log() { echo "[$(exec 9>&-; TZ=Asia/Seoul date '+%F %H:%M:%S')] $*"; }
kst() { TZ=Asia/Seoul date -d "@$1" '+%F %H:%M:%S' 9>&- 2>/dev/null || echo "?($1)"; }
epoch_now() { date +%s 9>&-; }
sleep_without_singleton() { sleep "$1" 9>&-; }
die() { log "⛔ ARM FAILED: $*"; arm_fail "$*"; }

# ⛔ `kill -0` 만으로는 부족하다 — **zombie 도 성공**하고, pid 는 재사용된다. `/proc/<pid>/stat` 의
#    state 와 starttime 까지 봐야 "그때 그 프로세스가 아직 돈다"가 된다.
#    전역에 기록해 command substitution(fork) 을 피한다 — fork 는 곧 FD 상속 문제다.
PROC_STATE=""; PROC_START=""
read_proc() {                        # $1=pid → PROC_STATE / PROC_START, 못 읽으면 1
  local raw
  PROC_STATE=""; PROC_START=""
  read -r raw < "/proc/$1/stat" 2>/dev/null || return 1
  raw="${raw##*) }"                  # comm 은 괄호 안이고 공백을 포함할 수 있다
  # shellcheck disable=SC2086
  set -- $raw                        # 이제 $1=state … $20=starttime (전체 필드 기준 3번·22번)
  PROC_STATE="$1"; PROC_START="${20:-}"
  [ -n "$PROC_START" ]
}

POLL_SECONDS=2            # 발화·계약 시각 감지 지연 상한
POLL_HOLD=15              # OFF 확정 후 유지 확인 간격
RETRY_FAST=20             # 계약 상한 이전 재시도 간격
RETRY_SLOW=300            # 상한 이후 — 이미 실패를 선언했으니 파괴적 시도 빈도를 낮춘다
OFF_BUDGET=420            # `off` 1회 상한 (canary_monitor RECREATE_TIMEOUT_SECONDS=180 이 recreate·health 에 각각)
STATUS_BUDGET=90
WATCHER_STATUS_BUDGET=60  # 계약 판정용 — 선언이 너무 늦지 않게 본 루프보다 좁게 잡는다
INEFFECTIVE_LIMIT=3       # off 가 ok 인데 status 가 OFF 가 아닌 연속 횟수 → 재생성 중단 (v2 결함 h)
HOLD_GRACE=120
GIVE_UP_AFTER=21600       # 상한 + 6h — 무한 루프가 나중의 의도적 ON 과 싸우지 않도록 종료점

cd "$HOME/exchange-rate" || die "cd 실패: $HOME/exchange-rate"

# ── 1. 인자 위생 — 거부는 **장치가 없는 편이 나은 경우**로만 좁힌다
case "${FIRE_AT}${CONTRACT_AT}" in
  ""|*[!0-9]*) die "epoch 이 양의 정수가 아니다 (fire=$FIRE_AT contract=$CONTRACT_AT)" ;;
esac
NOW="$(exec 9>&-; epoch_now)"
[ "$FIRE_AT" -lt "$CONTRACT_AT" ] || die "FIRE_AT >= CONTRACT_AT (fire=$FIRE_AT contract=$CONTRACT_AT)"
[ "$(( FIRE_AT - NOW ))" -le 86400 ] \
  || die "FIRE_AT 이 24h 초과 미래 (delta=$(( FIRE_AT - NOW ))s) — epoch 단위(ms?) 오류 의심"   # v2 결함 f
[ "$(( CONTRACT_AT - FIRE_AT ))" -le 21600 ] \
  || die "수렴 창이 6h 초과 (window=$(( CONTRACT_AT - FIRE_AT ))s) — epoch 단위 오류 의심"
# ⛔ stale 거부는 **사람이 붙여넣은 옛 명령**을 막기 위한 것이다. observer 인계는 방금 죽은
#    main 의 계약을 그대로 잇는 것이라 성격이 다르다 — 여기서 거부하면 계약 경계에서 main 이
#    죽었을 때 재무장 경로가 곧 무장 거부가 되어 OFF 를 아무도 강제하지 않는다(v7 조건 3).
if [ "$HANDOFF" -eq 1 ]; then
  [ "$(( NOW - CONTRACT_AT ))" -le "$GIVE_UP_AFTER" ] \
    || die "handoff 인데 계약이 ${GIVE_UP_AFTER}s 초과 과거다 ($(exec 9>&-; kst "$CONTRACT_AT"))"
else
  [ "$CONTRACT_AT" -gt "$NOW" ] || die "CONTRACT_AT 이 이미 지났다 ($(exec 9>&-; kst "$CONTRACT_AT")) — stale 명령"
fi
# ⚠️ FIRE_AT 만 과거인 것은 **거부하지 않는다** (v2 결함 e) — 창 안 재무장이 정확히 그 모양이다.
if [ "$FIRE_AT" -le "$NOW" ]; then
  log "late-arm — FIRE_AT($(exec 9>&-; kst "$FIRE_AT")) 이 이미 지났다. 즉시 수렴 시작"
  FIRE_AT="$NOW"
fi

# ── 2. singleton (v3 결함 k) — 두 인스턴스가 서로 다른 시각에 OFF 를 강제하면 어느 계약이
#      지배하는지 알 수 없다. 실제 flock 이 유일한 판정이다. 파일에 적힌 pid 는 진단용이며,
#      죽었거나 재사용됐다는 이유로 lock 실패를 우회하지 않는다(v6 결함 o).
WD_LOCK="$HOME/.topic_flag_watchdog.lock"
exec 9>>"$WD_LOCK" || die "watchdog lock 열기 실패: $WD_LOCK"
chmod 600 "$WD_LOCK" 2>/dev/null || true
if ! flock -n 9; then
  holder="$(cat "$WD_LOCK" 2>/dev/null | tr -d '\n')"
  log "⛔ 중복 무장 거부 — singleton lock 이 이미 잡혀 있다 [metadata=${holder:-<empty>}]"
  log "   metadata pid 생존 여부는 lock 소유권 증명이 아니다. 기존 holder 종료 후 다시 무장할 것."
  exit 4
fi
: >"$WD_LOCK"
printf 'pid=%s fire=%s contract=%s log=%s\n' "$$" "$FIRE_AT" "$CONTRACT_AT" "$LOG" >&9 \
  || die "watchdog lock metadata 기록 실패: $WD_LOCK"

GIVE_UP_AT=$(( CONTRACT_AT + GIVE_UP_AFTER ))
# ⚠️ 실행별 이름 — 이전 창의 breach 증거를 지우지 않는다.
BREACH_SENTINEL="$HOME/logs/WATCHDOG_CONTRACT_BREACH.$CONTRACT_AT"

TMP="$(exec 9>&-; mktemp 2>/dev/null)" || TMP=/dev/null   # 발화 이후엔 어떤 이유로도 죽지 않는다
WATCHER_PID=""

# ── 3. main 사망 시 인계 (v6 결함 p) — observer 는 기록만 하고 OFF 를 강제하지 않았다.
#      main 이 SIGKILL/OOM 으로 죽으면 강제할 주체가 0 이 되는데 자동 재무장자가 없었다.
MAIN_PID="$$"
REARM=1
if ! read_proc "$MAIN_PID"; then
  REARM=0
  log "⚠️ /proc/$MAIN_PID/stat 을 못 읽는다 — main 사망 시 자동 인계 없이 진행한다"
elif [ ! -r "$SELF" ]; then
  REARM=0
  log "⚠️ 스크립트 경로를 못 읽는다($SELF) — main 사망 시 자동 인계 없이 진행한다"
fi
MAIN_START="$PROC_START"
# ⛔ 정상 종료 표식 — observer 가 "죽었으니 인계"와 "끝나서 사라짐"을 가르는 유일한 근거다.
#    cleanup 이 **kill 보다 먼저** 쓴다. SIGKILL 은 cleanup 을 안 태우므로 표식이 없고,
#    그래서 그때만 인계가 일어난다(v7 조건 4).
DONE_MARKER="${LOG}.done"
rm -f "$DONE_MARKER" 2>/dev/null || true

cleanup() {
  : >"$DONE_MARKER" 2>/dev/null || true
  [ -n "$WATCHER_PID" ] && kill "$WATCHER_PID" 2>/dev/null
  rm -f "$TMP" 9>&- 2>/dev/null
}
trap cleanup EXIT

# ⛔ 계약 판정은 **그 시각의 관측**이어야 한다 (v3 결함 i). 캐시된 상태를 읽으면
#    직전에 다시 ON 이 돼도 breach 를 놓친다. 확인 못 한 것(AMBIGUOUS/unreadable)도 breach 다
#    — 계약은 "상한까지 OFF 를 **확인**한다"이지 "OFF 였을 것이다"가 아니다.
probe_status() {
  local budget="${1:-$STATUS_BUDGET}"
  # ⚠️ 호출부도 `$(exec 9>&-; probe_status ...)` 여야 한다. 내부 pipeline 만 닫으면 이를 기다리는
  #    command-substitution shell 이 FD 를 계속 보유한다(v6 결함 p, Linux 회귀로 실증).
  { timeout -k 5 "$budget" python3 scripts/topic_flag.py status 2>/dev/null \
    | python3 -c 'import json,sys;print(json.load(sys.stdin).get("state"))' 2>/dev/null ; } 9>&-
}

sleep_before_retry() {
  local now remaining nap
  now="$(exec 9>&-; epoch_now)"
  if [ "$now" -lt "$CONTRACT_AT" ]; then
    remaining=$(( CONTRACT_AT - now ))
    nap="$RETRY_FAST"
    [ "$remaining" -lt "$nap" ] && nap="$remaining"
  else
    nap="$RETRY_SLOW"
  fi
  [ "$nap" -gt 0 ] && sleep_without_singleton "$nap"
}

(
  # ⛔ singleton 은 OFF 로 수렴시키는 main 의 생존을 뜻해야 한다. 관측 전용 child 가 이 FD 를
  #    물려받으면 main SIGKILL 뒤에도 lock 만 살아 새 watchdog 이 재무장되지 못한다(v4 결함 l).
  exec 9>&-
  # ⛔ 계약 시각을 기다리는 동안 **main 의 생존도 함께 본다**. main 이 죽으면 OFF 를 강제할
  #    주체가 0 이 되므로, observer 가 같은 계약을 그대로 인계한다.
  #    spawn 이 아니라 `exec` 인 이유: 프로세스가 늘지 않고, 인계 실패가 조용히 남지 않으며,
  #    새 인스턴스가 자기 observer 를 다시 만들어 이 성질이 재귀적으로 유지된다.
  while [ "$(exec 9>&-; epoch_now)" -lt "$CONTRACT_AT" ]; do
    sleep_without_singleton "$POLL_SECONDS"
    [ "$REARM" -eq 1 ] || continue
    if ! read_proc "$MAIN_PID" || [ "$PROC_STATE" = "Z" ] || [ "$PROC_START" != "$MAIN_START" ]; then
      # ⚠️ 정상 종료였다면 인계하지 않는다 — cleanup 이 kill 보다 먼저 표식을 쓴다.
      if [ -e "$DONE_MARKER" ]; then
        log "main($MAIN_PID) 정상 종료 — 인계하지 않는다"
        exit 0
      fi
      log "⛔ main($MAIN_PID) 사망 감지 (state=${PROC_STATE:-<gone>}) — 같은 계약으로 인계한다"
      exec bash "$SELF" "$FIRE_AT" "$CONTRACT_AT" "$LOG" --handoff
      log "⛔ 인계 exec 실패 — 이 observer 는 기록만 계속한다"   # exec 성공 시 도달 불가
    fi
  done
  t0="$(exec 9>&-; epoch_now)"
  cur="$(exec 9>&-; probe_status "$WATCHER_STATUS_BUDGET")"
  took=$(( $(exec 9>&-; epoch_now) - t0 ))
  if [ "$cur" = "OFF" ]; then
    log "계약 상한 도달 — fresh 관측 OFF 확인 (breach 아님, probe=${took}s)"
  else
    log "⛔ CONTRACT BREACH — 계약 상한까지 OFF 미확인 (state=${cur:-<unreadable>}, probe=${took}s)."
    log "   ⚠️ 알림 경로 없음 — 이 줄과 sentinel 은 기록일 뿐 아무도 부르지 않는다. 수동 확인 필요."
    printf 'contract=%s breached_at=%s state=%s\n' \
      "$(exec 9>&-; kst "$CONTRACT_AT")" "$(exec 9>&-; kst "$(epoch_now)")" "${cur:-unreadable}" >"$BREACH_SENTINEL" 2>/dev/null \
      || log "   ⛔ sentinel 기록마저 실패: $BREACH_SENTINEL"
  fi
  # ⚠️ 계약 **이후**에도 감시를 멈추지 않는다 — breach 상태로 수렴 중이거나 hold 중에 main 이
  #    죽으면 거기서도 강제 주체가 0 이 된다. give-up 까지는 인계 대상이다.
  [ "$REARM" -eq 1 ] || exit 0
  while [ "$(exec 9>&-; epoch_now)" -lt "$GIVE_UP_AT" ]; do
    sleep_without_singleton "$POLL_SECONDS"   # /proc 읽기뿐이라 촘촘해도 싸다
    if ! read_proc "$MAIN_PID" || [ "$PROC_STATE" = "Z" ] || [ "$PROC_START" != "$MAIN_START" ]; then
      if [ -e "$DONE_MARKER" ]; then
        log "main($MAIN_PID) 정상 종료 — 인계하지 않는다"
        exit 0
      fi
      log "⛔ main($MAIN_PID) 사망 감지 (계약 이후, state=${PROC_STATE:-<gone>}) — 같은 계약으로 인계한다"
      exec bash "$SELF" "$FIRE_AT" "$CONTRACT_AT" "$LOG" --handoff
    fi
  done
) &
WATCHER_PID=$!

# ⛔ READY 는 **무장을 구성하는 모든 것이 존재한다**는 핸드셰이크다 (v3 결함 j).
#    watcher 생존을 확인한 뒤에만 찍는다 — 무장 절차는 이 줄을 근거로 완료를 판정한다.
sleep_without_singleton 1
kill -0 "$WATCHER_PID" 2>/dev/null || die "계약 watcher 기동 실패 — 무장하지 않는다"
log "READY pid=$$ watcher=$WATCHER_PID fire=$(exec 9>&-; kst "$FIRE_AT") contract=$(exec 9>&-; kst "$CONTRACT_AT") giveup=$(exec 9>&-; kst "$GIVE_UP_AT")"
log "ARMED fire_in=$(( FIRE_AT - $(exec 9>&-; epoch_now) ))s contract_in=$(( CONTRACT_AT - $(exec 9>&-; epoch_now) ))s" \
    "(fire_epoch=$FIRE_AT contract_epoch=$CONTRACT_AT off_budget=${OFF_BUDGET}s sentinel=$BREACH_SENTINEL)"

while [ "$(exec 9>&-; epoch_now)" -lt "$FIRE_AT" ]; do sleep_without_singleton "$POLL_SECONDS"; done
log "FIRE — 수렴 시작 (계약까지 $(( CONTRACT_AT - $(exec 9>&-; epoch_now) ))s)"

attempt=0
ineffective=0
recreate_stopped=0
confirmed_at=""
phase="converge"

while :; do
  now="$(exec 9>&-; epoch_now)"
  if [ "$now" -ge "$GIVE_UP_AT" ]; then
    log "⛔ GIVE UP — $(( GIVE_UP_AFTER / 3600 ))h 초과. flag 가 OFF 로 확정되지 않았다"
    exit 1
  fi

  if [ "$phase" = "converge" ]; then
    if [ "$recreate_stopped" -eq 0 ]; then
      attempt=$(( attempt + 1 ))
      # `9>&-` — 최장 수명 자식이다. singleton FD 를 물려주면 안 된다(v5 결함 m).
      timeout -k 15 "$OFF_BUDGET" python3 scripts/topic_flag.py off >"$TMP" 2>&1 9>&-; rc=$?
      log "off attempt=$attempt exit=$rc $(exec 9>&-; tail -1 "$TMP" 2>/dev/null | cut -c1-200)"
      [ "$rc" -eq 124 ] && log "   ⚠️ off 가 ${OFF_BUDGET}s 예산을 초과해 강제 종료됐다"
    else
      rc=-1   # 재생성 중단 모드 — 관측만 계속한다
    fi

    # ⛔ 성공 판정은 off 의 exit code 가 아니라 status 다. 쓰기만 되고 재생성이 남으면
    #    classify() 가 PENDING_RECREATE 를 주고, 그때 런타임은 여전히 dispatch 중이다.
    state="$(exec 9>&-; probe_status)"
    now="$(exec 9>&-; epoch_now)"
    log "status=${state:-<unreadable>} vs_contract=$(( now - CONTRACT_AT ))s ineffective=$ineffective"

    if [ "$state" = "OFF" ]; then
      confirmed_at="$now"
      if [ "$now" -le "$CONTRACT_AT" ]; then
        log "CONFIRMED OFF — 계약 상한 $(( CONTRACT_AT - now ))s 전 확정. 상한까지 유지 확인"
      else
        log "CONFIRMED OFF — 계약 상한 $(( now - CONTRACT_AT ))s **초과** 후 확정"
      fi
      phase="hold"; continue
    fi

    # ── 파괴적 행위의 유계화 (v2 결함 h)
    #    off 가 ok 인데도 status 가 OFF 가 아니면 재생성으로 고쳐지지 않는 유형이다.
    #    rc≠0(락 경합·abort)은 재생성을 하지 않았으므로 계속 시도한다.
    if [ "$rc" -eq 0 ]; then
      ineffective=$(( ineffective + 1 ))
      if [ "$ineffective" -ge "$INEFFECTIVE_LIMIT" ] && [ "$recreate_stopped" -eq 0 ]; then
        recreate_stopped=1
        log "⛔ off 가 ${INEFFECTIVE_LIMIT}회 연속 ok 인데 status 가 OFF 가 아니다 — 재생성 중단."
        log "   관측 축 고장 또는 구조적 원인 의심. 이후는 status 관측만 계속한다(수동 확인 필요)."
      fi
    else
      ineffective=0
    fi

    sleep_before_retry
    continue
  fi

  # ── hold: 첫 확정에서 종료하지 않는다 (v2 결함). 상한(+grace)까지 OFF 유지를 확인한다.
  hold_until=$(( CONTRACT_AT + HOLD_GRACE ))
  if [ "$now" -ge "$hold_until" ]; then
    if [ "$confirmed_at" -le "$CONTRACT_AT" ]; then
      log "DONE — 계약 상한 내 OFF 확정 후 +${HOLD_GRACE}s 유지 확인"
      exit 0
    fi
    log "DONE(late) — OFF 로 수렴했으나 계약 상한을 초과했다"
    exit 3
  fi
  sleep_without_singleton "$POLL_HOLD"
  state="$(exec 9>&-; probe_status)"
  if [ "$state" != "OFF" ]; then
    log "⚠️ hold 중 OFF 이탈 (state=${state:-<unreadable>}) — 수렴 루프로 복귀"
    ineffective=0; recreate_stopped=0; phase="converge"
  fi
done
