#!/bin/bash
# ═════════════════════════════════════════════════════════════
# SQLite Database Backup Script
# ═════════════════════════════════════════════════════════════
# 용도: SQLite DB를 타임스탬프와 함께 백업
# 실행: ./scripts/backup-db.sh
# Cron: 0 3 * * * /path/to/scripts/backup-db.sh  # 매일 03:00
# ═════════════════════════════════════════════════════════════

set -e  # 에러 발생 시 스크립트 종료

# ─────────────────────────────────────
# 설정
# ─────────────────────────────────────
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${PROJECT_ROOT}/data/exchange_rates.db"
BACKUP_DIR="${PROJECT_ROOT}/volumes/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/exchange_rates_${TIMESTAMP}.db"

# 백업 보관 기간 (일)
RETENTION_DAYS=30

# ─────────────────────────────────────
# 백업 디렉토리 생성
# ─────────────────────────────────────
mkdir -p "${BACKUP_DIR}"

# ─────────────────────────────────────
# 백업 실행
# ─────────────────────────────────────
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting database backup..."

if [ ! -f "${DB_PATH}" ]; then
    echo "Error: Database file not found at ${DB_PATH}"
    exit 1
fi

# SQLite 백업 (안전한 방법)
sqlite3 "${DB_PATH}" ".backup '${BACKUP_FILE}'"

if [ -f "${BACKUP_FILE}" ]; then
    BACKUP_SIZE=$(du -h "${BACKUP_FILE}" | cut -f1)
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup completed: ${BACKUP_FILE} (${BACKUP_SIZE})"
else
    echo "Error: Backup failed"
    exit 1
fi

# ─────────────────────────────────────
# 오래된 백업 삭제
# ─────────────────────────────────────
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Removing backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_DIR}" -name "exchange_rates_*.db" -type f -mtime +${RETENTION_DAYS} -delete

# 남은 백업 개수
BACKUP_COUNT=$(find "${BACKUP_DIR}" -name "exchange_rates_*.db" -type f | wc -l)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Total backups: ${BACKUP_COUNT}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup process completed successfully"
