#!/usr/bin/env bash
# 호스트 설정(로그 rotation) 설치·검증. 호스트 교체·복구 시 이 스크립트가 정본이다.
#
#   sudo ops/install-host-config.sh            # 설치 + 검증
#   sudo ops/install-host-config.sh --verify   # 검증만
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

verify() {
  echo "--- logrotate 문법 ---"
  logrotate -d /etc/logrotate.d/fxi-nginx >/dev/null && echo "OK"
  echo "--- journald 상한 ---"
  systemd-analyze cat-config systemd/journald.conf | grep -E '^(SystemMaxUse|SystemKeepFree)='
  echo "--- USR1 재오픈 실증 (silent failure 검출) ---"
  logrotate -f /etc/logrotate.d/fxi-nginx
  sleep 2
  before=$(stat -c%s "$NGINX_LOG_DIR/access.log" 2>/dev/null || echo 0)
  curl -fsS -m 10 https://fxi.kr/api/rates -o /dev/null || true
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
  --verify) verify ;;
  *) install_config; verify ;;
esac
