#!/usr/bin/env bash
# 호스트 설정(로그 rotation) 설치·점검. 호스트 교체·복구 시 이 스크립트가 정본이다.
#
#   sudo ops/install-host-config.sh                  # 설치 + 비파괴 점검 + USR1 실증(최초 1회)
#   sudo ops/install-host-config.sh --check          # 비파괴 점검만 (문법·상한). 언제든 안전
#   sudo ops/install-host-config.sh --verify-reopen  # ⚠️ 파괴적: 강제 rotation으로 USR1 실증
#
# ⚠️ `--verify-reopen`은 `logrotate -f`로 **실제 로그를 강제 회전**한다. 반복 실행하면 작은
#    아카이브가 쌓여 `rotate 7`을 밀어내고 **과거 로그를 조기 삭제**한다. 그래서 비파괴 `--check`와
#    분리했다 — 상시 점검은 `--check`, USR1 실증은 설치 직후나 문제 조사 때만.
#    (구 `--verify`는 이름과 달리 파괴적이었다. 그 이름은 제거했다.)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NGINX_LOG_DIR=/home/ubuntu/exchange-rate/volumes/logs/nginx

install_config() {
  install -m 0644 "$HERE/logrotate/fxi-nginx" /etc/logrotate.d/fxi-nginx
  install -d /etc/systemd/journald.conf.d
  install -m 0644 "$HERE/systemd/journald-limits.conf" /etc/systemd/journald.conf.d/limits.conf
  systemctl restart systemd-journald
  echo "설치 완료: /etc/logrotate.d/fxi-nginx, /etc/systemd/journald.conf.d/limits.conf"
}

check() {   # 비파괴 — 로그를 건드리지 않는다
  echo "--- logrotate 문법 (dry-run) ---"
  logrotate -d /etc/logrotate.d/fxi-nginx >/dev/null && echo "OK"
  echo "--- journald 상한 ---"
  systemd-analyze cat-config systemd/journald.conf | grep -E '^(SystemMaxUse|SystemKeepFree)='
}

verify_reopen() {   # ⚠️ 파괴적 — 강제 rotation 발생
  echo "--- USR1 재오픈 실증 (⚠️ 강제 rotation) ---"
  logrotate -f /etc/logrotate.d/fxi-nginx
  sleep 2
  before=$(stat -c%s "$NGINX_LOG_DIR/access.log" 2>/dev/null || echo 0)
  # ⚠️ 공인 DNS(https://fxi.kr)로 쏘면 안 된다 — 호스트 교체 중 DNS 전환 **전**에는 구 서버로 가
  #    이 호스트의 로그가 안 자라 오판한다. --resolve로 **로컬 nginx를 확정적으로** 때린다
  #    (Host/SNI는 fxi.kr 그대로라 TLS·vhost 매칭은 정상).
  curl -fsS -m 10 --resolve fxi.kr:443:127.0.0.1 https://fxi.kr/api/rates -o /dev/null || true
  sleep 2
  after=$(stat -c%s "$NGINX_LOG_DIR/access.log" 2>/dev/null || echo 0)
  if [ "$after" -gt "$before" ]; then
    echo "OK — rotation 후 새 access.log가 자란다 ($before → $after bytes)"
  else
    echo "FAIL — 새 파일이 자라지 않는다. nginx가 rename된 inode에 쓰고 있다(USR1 미전달)." >&2
    exit 1
  fi
}

case "${1:-}" in
  --check)         check ;;
  --verify-reopen) verify_reopen ;;
  "")              install_config; check; verify_reopen ;;
  *) echo "usage: $0 [--check | --verify-reopen]" >&2; exit 2 ;;
esac
