#!/usr/bin/env bash
# Install or verify the bounded 843462e topic-auth observation schedule.
#
#   sudo ops/install-topic-auth-capture.sh --install
#   sudo ops/install-topic-auth-capture.sh --check
#
# Installation performs one passive capture before enabling the timer. A
# capture failure leaves the timer disabled; it never opens a WebSocket or
# calls Firebase/RevenueCat.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
SYSTEMD_SOURCE="$HERE/systemd"
SERVICE=fxi-topic-auth-capture.service
TIMER=fxi-topic-auth-capture.timer
ENV_SOURCE="$SYSTEMD_SOURCE/fxi-topic-auth-capture.env"
SUM_SOURCE="$SYSTEMD_SOURCE/fxi-topic-auth-capture.sha256"
SERVICE_SOURCE="$SYSTEMD_SOURCE/$SERVICE"
TIMER_SOURCE="$SYSTEMD_SOURCE/$TIMER"
ENV_DEST=/etc/fxi/topic-auth-capture.env
SUM_DEST=/etc/fxi/topic-auth-capture.sha256
SERVICE_DEST="/etc/systemd/system/$SERVICE"
TIMER_DEST="/etc/systemd/system/$TIMER"
OUTPUT_DIR=/home/ubuntu/logs/topic-auth-rollout
CAPTURE_PROGRAM="$REPO_ROOT/ops/capture_topic_auth_rollout.py"
MANAGED_PATHS=(
  ops/capture_topic_auth_rollout.py
  ops/install-topic-auth-capture.sh
  ops/systemd/fxi-topic-auth-capture.env
  ops/systemd/fxi-topic-auth-capture.sha256
  ops/systemd/fxi-topic-auth-capture.service
  ops/systemd/fxi-topic-auth-capture.timer
)

die() {
  echo "중단: $*" >&2
  exit 1
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "root 권한이 필요하다 (sudo $0 $1)"
}

env_value() {
  local key="$1" matches
  matches=$(grep -c "^${key}=" "$ENV_SOURCE" || true)
  [ "$matches" -eq 1 ] || die "$ENV_SOURCE 의 $key 정의가 정확히 1개가 아니다"
  sed -n "s/^${key}=//p" "$ENV_SOURCE"
}

check_source() {
  local expected_path expected_sha actual_sha upstream head upstream_head
  local managed
  for managed in "${MANAGED_PATHS[@]}"; do
    git -C "$REPO_ROOT" ls-files --error-unmatch -- "$managed" >/dev/null 2>&1 || \
      die "설치 입력이 Git에 추적되지 않는다: $managed"
  done
  git -C "$REPO_ROOT" diff --quiet -- "${MANAGED_PATHS[@]}" || \
    die "설치 입력이 HEAD와 다르다 (unstaged drift)"
  git -C "$REPO_ROOT" diff --cached --quiet -- "${MANAGED_PATHS[@]}" || \
    die "설치 입력이 HEAD와 다르다 (staged drift)"

  upstream=$(git -C "$REPO_ROOT" rev-parse --symbolic-full-name '@{upstream}' 2>/dev/null) || \
    die "현재 branch에 upstream remote-tracking ref가 없다"
  case "$upstream" in
    refs/remotes/*) ;;
    *) die "upstream이 remote-tracking ref가 아니다: $upstream" ;;
  esac
  head=$(git -C "$REPO_ROOT" rev-parse HEAD)
  upstream_head=$(git -C "$REPO_ROOT" rev-parse "$upstream")
  [ "$head" = "$upstream_head" ] || \
    die "설치 HEAD가 local upstream ref와 다르다: HEAD=$head $upstream=$upstream_head"

  read -r expected_sha expected_path < "$SUM_SOURCE"
  [ "$expected_path" = "/home/ubuntu/exchange-rate/ops/capture_topic_auth_rollout.py" ] || \
    die "checksum 대상 경로가 운영 정본 경로와 다르다: $expected_path"
  actual_sha=$(sha256sum "$CAPTURE_PROGRAM" | awk '{print $1}')
  [ "$actual_sha" = "$expected_sha" ] || \
    die "capture program SHA drift: expected=$expected_sha actual=$actual_sha"

  [ "$(grep -c '^OnCalendar=' "$TIMER_SOURCE")" -eq 7 ] || \
    die "timer OnCalendar 항목은 정확히 7개여야 한다"
  ! grep -Eq '^OnCalendar=.*\*' "$TIMER_SOURCE" || \
    die "반복 wildcard OnCalendar 는 허용하지 않는다"
  grep -Fxq 'Persistent=true' "$TIMER_SOURCE" || die "timer 가 Persistent=true 가 아니다"
  grep -Fxq 'RandomizedDelaySec=0' "$TIMER_SOURCE" || die "timer 에 임의 지연이 있다"
  grep -Fq 'ExecStartPre=/usr/bin/sha256sum --check --status' "$SERVICE_SOURCE" || \
    die "service 가 capture program SHA 를 기동 전에 검증하지 않는다"

  env_value EXPECTED_ROLLOUT_STARTED_AT >/dev/null
  env_value EXPECTED_IMAGE_ID >/dev/null
  env_value EXPECTED_STAGE >/dev/null
  env_value EXPECTED_DISPATCHER_ENABLED >/dev/null
  env_value DEPLOYMENT_LABEL >/dev/null
}

check_no_dropins() {
  local root
  for root in /etc/systemd/system /run/systemd/system /usr/local/lib/systemd/system \
              /usr/lib/systemd/system; do
    if [ -d "$root/$SERVICE.d" ] && find "$root/$SERVICE.d" -type f -print -quit | grep -q .; then
      die "$root/$SERVICE.d 에 검토되지 않은 drop-in 이 있다"
    fi
    if [ -d "$root/$TIMER.d" ] && find "$root/$TIMER.d" -type f -print -quit | grep -q .; then
      die "$root/$TIMER.d 에 검토되지 않은 drop-in 이 있다"
    fi
  done
}

check_paths_are_not_symlinks() {
  local path
  for path in /etc/fxi "$ENV_DEST" "$SUM_DEST" "$SERVICE_DEST" "$TIMER_DEST" \
              "$OUTPUT_DIR"; do
    [ ! -L "$path" ] || die "관리 경로가 symlink 다: $path"
  done
}

last_schedule_epoch() {
  local last
  last=$(grep '^OnCalendar=' "$TIMER_SOURCE" | tail -1 | cut -d= -f2-)
  date -u -d "$last" +%s
}

check_install_window_open() {
  [ "$(date -u +%s)" -lt "$(last_schedule_epoch)" ] || \
    die "마지막 예약 시각이 이미 지났다 — 만료된 관찰 창을 설치하지 않는다"
}

check_installed() {
  check_source
  check_no_dropins
  check_paths_are_not_symlinks
  cmp -s "$SERVICE_SOURCE" "$SERVICE_DEST" || die "$SERVICE_DEST 가 정본과 다르다"
  cmp -s "$TIMER_SOURCE" "$TIMER_DEST" || die "$TIMER_DEST 가 정본과 다르다"
  cmp -s "$ENV_SOURCE" "$ENV_DEST" || die "$ENV_DEST 가 정본과 다르다"
  cmp -s "$SUM_SOURCE" "$SUM_DEST" || die "$SUM_DEST 가 정본과 다르다"
  [ "$(stat -c '%a' "$OUTPUT_DIR")" = 700 ] || die "$OUTPUT_DIR 권한이 0700이 아니다"
  [ "$(stat -c '%u:%g' "$OUTPUT_DIR")" = "$(id -u ubuntu):$(id -g ubuntu)" ] || \
    die "$OUTPUT_DIR 소유자가 ubuntu가 아니다"
  [ "$(stat -c '%a' "$ENV_DEST")" = 600 ] || die "$ENV_DEST 권한이 0600이 아니다"
  systemd-analyze verify "$SERVICE_DEST" "$TIMER_DEST"
  systemctl is-enabled --quiet "$TIMER" || die "$TIMER 가 enabled 상태가 아니다"
  systemctl is-active --quiet "$TIMER" || die "$TIMER 가 active 상태가 아니다"
  local next
  next=$(systemctl show "$TIMER" -p NextElapseUSecRealtime --value)
  if [ -n "$next" ] && [ "$next" != "n/a" ]; then
    echo "OK — $TIMER enabled/active, next=$next"
  elif [ "$(date -u +%s)" -ge "$(last_schedule_epoch)" ]; then
    echo "OK — $TIMER bounded schedule complete"
  else
    die "$TIMER 에 다음 실행 시각이 없다"
  fi
  local service_result
  service_result=$(systemctl show "$SERVICE" -p Result --value)
  [ "$service_result" = success ] || die "$SERVICE 마지막 결과가 success가 아니다: $service_result"
}

install_schedule() {
  check_source
  check_no_dropins
  check_paths_are_not_symlinks
  check_install_window_open

  # Reinstall is fail-closed: stop the old schedule before replacing any input.
  systemctl disable --now "$TIMER" 2>/dev/null || true
  install -d -m 0755 /etc/fxi
  install -d -o ubuntu -g ubuntu -m 0700 "$OUTPUT_DIR"
  install -m 0600 "$ENV_SOURCE" "$ENV_DEST"
  install -m 0644 "$SUM_SOURCE" "$SUM_DEST"
  install -m 0644 "$SERVICE_SOURCE" "$SERVICE_DEST"
  install -m 0644 "$TIMER_SOURCE" "$TIMER_DEST"
  systemctl daemon-reload
  systemd-analyze verify "$SERVICE_DEST" "$TIMER_DEST"

  # Exercise the exact installed unit, including its sandbox and environment,
  # before arming any future execution. Failure leaves the timer disabled.
  if ! systemctl start "$SERVICE"; then
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    die "$SERVICE 설치 검증 캡처가 실패했다 — timer 는 disabled 상태다"
  fi

  if ! systemctl enable --now "$TIMER"; then
    systemctl disable --now "$TIMER" 2>/dev/null || true
    die "$TIMER 활성화에 실패했다"
  fi
  if ! (check_installed); then
    systemctl disable --now "$TIMER" 2>/dev/null || true
    die "설치 후 검증에 실패해 timer 를 다시 disabled 했다"
  fi
}

main() {
  case "${1:-}" in
    --check)
      require_root --check
      check_installed
      ;;
    --install)
      require_root --install
      install_schedule
      ;;
    *)
      echo "usage: sudo $0 [--install | --check]" >&2
      exit 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
