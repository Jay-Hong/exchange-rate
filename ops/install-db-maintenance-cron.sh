#!/usr/bin/env bash
# FXi DB maintenance cron 설치·점검. `ops/cron/fxi-db-maintenance.crontab` 이 desired state 다.
#
#   ops/install-db-maintenance-cron.sh --check     # 비파괴 점검만 (언제든 안전)
#   ops/install-db-maintenance-cron.sh --install   # managed 5줄 일괄 전환 + 점검
#   ops/install-db-maintenance-cron.sh --restore <backup> <sha256>   # SHA 검증 후 복원
#
# ⛔ **기존 `ops/install-host-config.sh` 에 흡수하지 않는다.** 그쪽은 root 로
#    `/etc/logrotate.d`·`/etc/systemd` 를 만지고 journald 를 restart 하며 무인자 실행이
#    **파괴적**(`logrotate -f`)이다. 사용자 crontab 병합은 권한·복구·파괴성이 전혀 다르다.
# ⛔ 무인자 실행은 아무것도 하지 않는다 — 모드를 반드시 명시한다.
# ⛔ `sudo` 로 돌리지 말 것. root crontab 을 보게 되어 **다른 사람의 crontab 을 점검**한다.
#
# ⛔ 전체 crontab 교체 금지. managed 5줄만 바꾸고 나머지(무관 job·주석·빈 줄)는
#    **바이트 그대로** 보존한다.
# ⚠️ `crontab` 에는 CAS API 가 없다. write 직전 재-read 로 스냅샷과 대조하고 installer 끼리는
#    `flock` 으로 직렬화하지만, 사람이 동시에 `crontab -e` 를 돌리는 경쟁은 **제거할 수 없다** —
#    운영 잔여 한계다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# ⛔ 안내 명령에는 **절대 경로**를 쓴다. `${BASH_SOURCE[0]}` 는 `ops/install-…` 같은 상대
#    경로일 수 있어, 다른 디렉터리에서 복사해 실행하면 exit 127 이 난다(실측).
SELF="$HERE/$(basename "${BASH_SOURCE[0]}")"
MANIFEST="$HERE/cron/fxi-db-maintenance.crontab"
TOKEN=" -e DB_WORKLOAD_PROFILE=maintenance"
# ⚠️ 공용 `/tmp` 고정 경로는 **다른 사용자와 충돌**한다 — UID 를 넣어 사용자별로 가른다.
LOCK="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/fxi-db-maintenance-cron.$(id -u).lock"
BACKUP_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/fxi-cron-backups"

usage() { sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2; }

managed_lines() { grep -vE '^\s*(#|$)' "$MANIFEST"; }
legacy_lines()  { managed_lines | sed "s| -e DB_WORKLOAD_PROFILE=maintenance||"; }

# 현재 crontab 을 **파일로** 뜬다. ⛔ command substitution 은 **trailing LF 를 먹는다** —
#   실측: "a\nb\n\n\n" 이 "a\nb\n" 이 됐다. 바이트 보존을 주장하려면 파일을 그대로 다뤄야 한다.
# ⛔ `crontab -l` 의 **모든** 실패를 빈 crontab 으로 접지 않는다. 실측: exit 42 인 crontab 이
#   len=0 으로 둔갑해 managed=0 legacy=0 오진이 됐다. "job 없음"(exit 1 + 빈 출력)만 빈 것이다.
acquire_write_lock() {
  # ⛔ **쓰기 경로는 전부** 이 락을 탄다. 한때 `--restore` 가 락 밖이라 `--install` 과
  #    같은 사용자 상태를 동시에 덮을 수 있었다(실측: fake flock 을 항상 실패시켜도 복원이
  #    성공했다).
  if ! command -v flock >/dev/null 2>&1; then
    echo "❌ 이 플랫폼에 flock(1) 이 없어 installer 간 직렬화를 보장할 수 없다 — 쓰지 않는다"
    echo "   (read-only 점검은 --check 로 가능하다. 운영 호스트는 Linux 라 flock 이 있다.)"
    return 1
  fi
  exec 9>"$LOCK"
  flock -n 9 || { echo "❌ 다른 installer 가 이 호스트에서 실행 중이다"; return 1; }
}

snapshot_crontab() {  # $1 = 출력 파일
  local rc
  set +e; crontab -l >"$1" 2>"$1.err"; rc=$?; set -e
  # ⛔ **모든 nonzero 를 fail-closed** 한다. 한때 `exit 1` + `no crontab` 부분문자열이면 빈
  #    것으로 봤는데, `backend unavailable: no crontab service` + exit 1 도 통과했다(실측).
  #    대상 호스트는 기존 5줄이 **필수**라 "빈 crontab" 은 어차피 정상 상태가 아니다 —
  #    구별하려 애쓰기보다 전부 막고 사람이 보게 하는 쪽이 정확하다.
  if [ "$rc" -ne 0 ]; then
    echo "❌ crontab -l 실패 (exit $rc): $(head -c 200 "$1.err")" >&2
    return 1
  fi
  rm -f "$1.err"
}

# 상태 판정: MANAGED_COMPLETE / LEGACY_COMPLETE / 그 밖 전부 거부.
# ⛔ 부분 전환·중복·누락·혼합은 **전부 거부**다. "대충 맞으면 진행" 이 이 절차의 실패 모드다.
classify() {
  local current="$1" managed=0 legacy=0 dup=0 line n
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    n=$(grep -Fxc -- "$line" <<<"$current" || true)
    [ "$n" -gt 1 ] && dup=1
    [ "$n" -ge 1 ] && managed=$((managed + 1))
  done < <(managed_lines)
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    n=$(grep -Fxc -- "$line" <<<"$current" || true)
    [ "$n" -gt 1 ] && dup=1
    [ "$n" -ge 1 ] && legacy=$((legacy + 1))
  done < <(legacy_lines)

  if [ "$dup" -eq 1 ]; then echo "REJECT:중복 줄이 있다"; return; fi
  if [ "$managed" -eq 5 ] && [ "$legacy" -eq 0 ]; then echo "MANAGED_COMPLETE"; return; fi
  if [ "$legacy" -eq 5 ] && [ "$managed" -eq 0 ]; then echo "LEGACY_COMPLETE"; return; fi
  echo "REJECT:부분 상태 (managed=$managed legacy=$legacy) — 일괄 전환만 허용한다"
}

do_check() {
  local current state snap
  snap="$(mktemp)"; trap 'rm -f "$snap" "$snap.err"' RETURN
  snapshot_crontab "$snap" || return 1
  current="$(cat "$snap")"
  state="$(classify "$current")"
  echo "state: $state"
  # ⛔ `--check` 는 **MANAGED_COMPLETE 만** 성공이다. LEGACY_COMPLETE 를 통과시키면
  #    아직 cutover 되지 않은 호스트가 green 이 된다(codex).
  case "$state" in
    MANAGED_COMPLETE) echo "✅ managed 5줄이 정확히 설치돼 있다"; return 0 ;;
    LEGACY_COMPLETE)  echo "❌ 아직 cutover 전이다 — --install 필요"; return 1 ;;
    *)                echo "❌ $state"; return 1 ;;
  esac
}

do_install() {
  local current state before after snap tmp backup
  snap="$(mktemp)"; tmp="$(mktemp)"
  trap 'rm -f "$snap" "$snap.err" "$tmp"' RETURN
  snapshot_crontab "$snap" || return 1
  before="$(shasum -a 256 <"$snap" | cut -d' ' -f1)"
  current="$(cat "$snap")"
  state="$(classify "$current")"
  case "$state" in
    MANAGED_COMPLETE) echo "state: $state — 이미 설치됨(idempotent no-op)"; return 0 ;;
    LEGACY_COMPLETE)  : ;;
    *) echo "❌ $state — 설치하지 않는다"; return 1 ;;
  esac

  # ⛔ 락은 **쓰기 직전**에만 필요하다. 판정은 read-only 라 락 없이 해야 어느 플랫폼에서든
  #    거부 사유가 관측된다 — 락을 앞에 두면 부분 상태·중복 같은 진짜 사유가 가려진다(실측).
  acquire_write_lock || return 1

  # ⛔ **스냅샷 파일 자체를 변환**한다 — 메모리를 거치면 trailing LF 가 사라진다(실측).
  cp "$snap" "$tmp"
  local legacy managed
  while IFS= read -r legacy && IFS= read -r managed <&3; do
    [ -z "$legacy" ] && continue
    python3 - "$tmp" "$legacy" "$managed" <<'PY'
import sys, pathlib
p, old, new = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = p.read_text().split("\n")
hits = [i for i, l in enumerate(lines) if l == old]
assert len(hits) == 1, f"치환 대상이 {len(hits)}회 — 중단"
lines[hits[0]] = new
p.write_text("\n".join(lines))
PY
  done < <(legacy_lines) 3< <(managed_lines)

  # ⛔ write 직전 재-read — 스냅샷과 다르면 그 사이 남이 바꿨다는 뜻이다. 덮지 않고 중단한다.
  snapshot_crontab "$snap" || return 1
  after="$(shasum -a 256 <"$snap" | cut -d' ' -f1)"
  if [ "$before" != "$after" ]; then
    echo "❌ 설치 직전 crontab 이 바뀌었다 — 덮어쓰지 않고 중단한다"; return 1
  fi

  # ⛔ 백업은 **최종 CAS 를 통과한 뒤**다. 앞에 두면 중단된 실행이 **stale 백업**과 그 "복원"
  #    명령을 남긴다 — 현재 상태와 다른 것을 되돌리라고 안내하는 셈이다(실측).
  # ⛔ 이름은 **충돌 불가능**해야 한다. 초 단위 timestamp 는 같은 초에 두 번 돌면 앞 백업을
  #    덮는다(실측: 백업 1개만 남고 첫 원본이 사라졌다). `mktemp` 로 유일성을 커널에 맡긴다.
  mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
  # ⚠️ `XXXXXX` 는 템플릿 **끝**이어야 한다 — macOS `mktemp` 는 중간에 있으면 거부한다(실측).
  backup="$(mktemp "$BACKUP_DIR/crontab.$(date +%Y%m%dT%H%M%S).bak.XXXXXX")"
  cp "$snap" "$backup"; chmod 600 "$backup"
  echo "backup: $backup  sha256=$after"
  # ⛔ 복원은 **이 스크립트가 소유**한다. `crontab <file>` 을 그냥 안내하면 SHA 가 검증되지
  #    않고, 파일이 잘려 있어도 그대로 설치된다. `--restore` 가 SHA 를 확인한 뒤 설치한다.
  # ⛔ 경로를 `printf %q` 로 인용한다 — 공백·따옴표가 든 경로에서 안내 명령이 깨진다.
  printf '  복원: %s --restore %s %s\n' \
    "$(printf '%q' "$SELF")" "$(printf '%q' "$backup")" "$after"

  crontab "$tmp"
  do_check
}

[ $# -ge 1 ] || usage
[ -r "$MANIFEST" ] || { echo "❌ manifest 없음: $MANIFEST"; exit 1; }

# ⛔ 락은 **변경 경로에만** 건다. `--check` 는 read-only 라 락이 필요 없다.
# ⚠️ macOS 엔 `flock(1)` 이 없다 — 로컬 스모크에서 "다른 installer 가 실행 중" 이라는 **거짓
#    사유**가 나왔다. 없으면 그렇게 둘러대지 말고 **정확한 이유로 거부**한다(운영 호스트는
#    Linux 라 존재한다). 거짓 진단은 없는 것보다 나쁘다.
do_restore() {   # $1 = backup 파일, $2 = 기대 SHA
  local got snap pre
  acquire_write_lock || return 1
  [ -r "$1" ] || { echo "❌ 백업을 읽을 수 없다: $1"; return 1; }

  # ⛔ **검증한 그 파일을 설치한다.** 백업 경로에서 SHA 를 재고 같은 경로를 다시 `crontab` 에
  #    넘기면 그 사이 파일이 바뀔 수 있다(TOCTOU). 스냅샷으로 떠서 그것만 다룬다.
  snap="$(mktemp)"; pre="$(mktemp)"
  trap 'rm -f "$snap" "$pre" "$pre.err"' RETURN
  cp "$1" "$snap"
  got="$(shasum -a 256 <"$snap" | cut -d' ' -f1)"
  if [ "$got" != "$2" ]; then
    echo "❌ 백업 SHA 불일치 — 복원하지 않는다"; echo "   기대=$2"; echo "   실제=$got"; return 1
  fi

  # ⛔ 복원 **전에 현재 상태도 백업**한다. cutover 이후 무관 cron 이 바뀌었다면 이 복원이
  #    그것까지 되돌린다 — 되돌릴 길을 남긴다.
  snapshot_crontab "$pre" || return 1
  mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
  local pre_backup pre_sha
  pre_backup="$(mktemp "$BACKUP_DIR/crontab.$(date +%Y%m%dT%H%M%S).pre-restore.XXXXXX")"
  cp "$pre" "$pre_backup"; chmod 600 "$pre_backup"
  pre_sha="$(shasum -a 256 <"$pre_backup" | cut -d' ' -f1)"
  echo "pre-restore backup: $pre_backup  sha256=$pre_sha"
  printf '  되돌리기: %s --restore %s %s\n' \
    "$(printf '%q' "$SELF")" "$(printf '%q' "$pre_backup")" "$pre_sha"

  crontab "$snap"
  echo "✅ 복원 완료: $1"
}


case "$1" in
  --check)
    [ $# -eq 1 ] || usage
    do_check
    ;;
  --install)
    [ $# -eq 1 ] || usage
    do_install
    ;;
  --restore)
    [ $# -eq 3 ] || usage
    do_restore "$2" "$3"
    ;;
  *) usage ;;
esac
