#!/usr/bin/env bash
# 호스트 설정(로그 rotation) 설치·점검. 호스트 교체·복구 시 이 스크립트가 정본이다.
#
#   sudo ops/install-host-config.sh                  # 설치 + 비파괴 점검 + USR1 실증(최초 1회)
#   sudo ops/install-host-config.sh --check          # 비파괴 점검만 (동기화·문법·실효값). 언제든 안전
#   sudo ops/install-host-config.sh --install        # 설치/복구 + 점검 (강제 rotation 없음)
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
  # 구 이름(limits.conf)이 남아 있으면 정의가 둘이 되고 정렬상 먼저 와 혼란을 준다.
  # ⚠️ 다만 `limits.conf`는 **흔한 이름**이라 관리자가 만든 다른 파일일 수 있다 —
  #    FXi 소유 마커가 있을 때만 지우고, 없으면 **중단**한다(남의 설정을 말없이 지우지 않는다).
  legacy=/etc/systemd/journald.conf.d/limits.conf
  if [ -e "$legacy" ]; then
    if grep -q "fxi-managed:" "$legacy"; then
      rm -f "$legacy"
      echo "구 FXi 파일 제거: $legacy"
    else
      echo "중단: $legacy 가 존재하지만 FXi 소유 마커가 없다 — 다른 주체의 설정일 수 있다." >&2
      echo "  내용을 확인하고 직접 처리한 뒤 다시 실행할 것: sudo cat $legacy" >&2
      exit 1
    fi
  fi
  install -m 0644 "$HERE/systemd/zz-fxi-limits.conf" /etc/systemd/journald.conf.d/zz-fxi-limits.conf
  systemctl restart systemd-journald
  echo "설치 완료: /etc/logrotate.d/fxi-nginx, /etc/systemd/journald.conf.d/zz-fxi-limits.conf"
}

check() {   # 비파괴 — 로그를 건드리지 않는다
  local file_drift=0 override_drift=0

  # (1) 정본 ↔ 설치본 **동일성**. 문법만 보면 호스트에서 USR1이나 rotate 상한을 손으로 지워도
  #     통과한다 — 그러면 "정본" 계약이 이름뿐이다.
  echo "--- 정본 ↔ 설치본 ---"
  for pair in "logrotate/fxi-nginx:/etc/logrotate.d/fxi-nginx" \
              "systemd/zz-fxi-limits.conf:/etc/systemd/journald.conf.d/zz-fxi-limits.conf"; do
    src="$HERE/${pair%%:*}"; dst="${pair##*:}"
    if cmp -s "$src" "$dst"; then
      echo "OK   $dst"
    else
      echo "DRIFT $dst 가 정본과 다르다:" >&2
      diff -u "$src" "$dst" 2>&1 | head -20 >&2 || true
      file_drift=1
    fi
  done

  echo "--- logrotate 문법 (dry-run) ---"
  logrotate -d /etc/logrotate.d/fxi-nginx >/dev/null && echo "OK"

  # (2) journald는 drop-in 병합이라 **파일이 맞아도 후순위가 덮을 수 있다**
  #     (실측: 우리 drop-in 뒤에 /usr/lib/systemd/journald.conf.d/syslog.conf가 온다).
  #     systemd는 뒤에 오는 정의가 이기므로 **마지막 값 = 실효값**으로 비교한다.
  echo "--- journald 실효값 (후순위 drop-in 재정의 검출) ---"
  for key in SystemMaxUse SystemKeepFree; do
    want=$(grep -E "^${key}=" "$HERE/systemd/zz-fxi-limits.conf" | tail -1)
    got=$(systemd-analyze cat-config systemd/journald.conf | grep -E "^${key}=" | tail -1)
    if [ "$want" = "$got" ]; then
      echo "OK   $got"
    else
      # ⚠️ 이 경우 **재설치로는 해결되지 않는다** — 같은 파일명을 다시 복사해도 정렬 순서는 그대로다.
      echo "DRIFT 실효값 '$got' ≠ 정본 '$want'" >&2
      echo "  → 우리 파일보다 **뒤에 정렬되는** drop-in이 같은 키를 정의했다. 재설치로는 안 고쳐진다." >&2
      echo "  → 범인 확인: systemd-analyze cat-config systemd/journald.conf | grep -n '^# /\|^${key}='" >&2
      echo "  → 그 파일을 고치거나, 정본 이름을 더 뒤로 정렬되게 바꿔야 한다(현재 zz- 접두사)." >&2
      override_drift=1
    fi
  done

  # ⚠️ 안내는 drift **종류별**로 갈린다. override에 "재설치"를 안내하면 (a) 문제가 안 고쳐지고
  #    (b) 무기명 실행이 `--verify-reopen`까지 돌려 **강제 rotation**까지 일으킨다.
  if [ "$file_drift" -ne 0 ]; then
    echo "→ 파일 drift는 재설치로 복구된다: sudo $0 --install" >&2
    echo "   (무기명 실행 \`sudo $0\`은 강제 rotation까지 하므로 복구 목적이면 --install을 쓸 것)" >&2
  fi
  if [ "$override_drift" -ne 0 ]; then
    echo "→ override drift는 **재설치로 안 고쳐진다** — 위 안내대로 범인 drop-in을 처리할 것." >&2
  fi
  [ "$file_drift" -eq 0 ] && [ "$override_drift" -eq 0 ] || exit 1
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
  --install)       install_config; check ;;          # 복구용 — 강제 rotation 없음
  --verify-reopen) verify_reopen ;;
  "")              install_config; check; verify_reopen ;;
  *) echo "usage: $0 [--check | --install | --verify-reopen]" >&2; exit 2 ;;
esac
