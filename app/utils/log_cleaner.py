# app/utils/log_cleaner.py

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from app.config import LOG_DIR

logger = logging.getLogger("exchange_rate.utils.log_cleaner")

def cleanup_old_log_files(days: int = 7):
    """
    오래된 로그 백업 파일 삭제

    Args:
        days: 보관 기간 (일)
    """
    cutoff_time = datetime.now() - timedelta(days=days)
    deleted_count = 0
    freed_space = 0

    try:
        # .log.1, .log.2 등 백업 파일 검색
        for log_file in LOG_DIR.glob("*.log.*"):
            try:
                # 파일 수정 시간 확인
                file_mtime = datetime.fromtimestamp(log_file.stat().st_mtime)

                if file_mtime < cutoff_time:
                    file_size = log_file.stat().st_size
                    log_file.unlink()
                    deleted_count += 1
                    freed_space += file_size
                    logger.info(
                        f"🗑️ 오래된 로그 파일 삭제: {log_file.name}",
                        extra={
                            "file": log_file.name,
                            "size_mb": round(file_size / 1024 / 1024, 2),
                            "age_days": (datetime.now() - file_mtime).days
                        }
                    )
            except Exception as e:
                logger.error(f"파일 삭제 실패: {log_file.name}", exc_info=True)

        if deleted_count > 0:
            logger.info(
                f"✅ 로그 정리 완료: {deleted_count}개 파일, {round(freed_space / 1024 / 1024, 2)}MB 확보",
                extra={
                    "deleted_count": deleted_count,
                    "freed_mb": round(freed_space / 1024 / 1024, 2)
                }
            )
        else:
            logger.debug("📂 삭제할 오래된 로그 파일 없음")

    except Exception as e:
        logger.error("로그 정리 중 오류 발생", exc_info=True)


def get_log_disk_usage():
    """
    로그 디렉토리 디스크 사용량 조회

    Returns:
        dict: {total_mb, file_count, oldest_file, newest_file}
    """
    try:
        total_size = 0
        file_count = 0
        oldest_file = None
        newest_file = None
        oldest_time = datetime.now()
        newest_time = datetime(2000, 1, 1)

        for log_file in LOG_DIR.glob("*.log*"):
            total_size += log_file.stat().st_size
            file_count += 1

            file_mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
            if file_mtime < oldest_time:
                oldest_time = file_mtime
                oldest_file = log_file.name
            if file_mtime > newest_time:
                newest_time = file_mtime
                newest_file = log_file.name

        return {
            "total_mb": round(total_size / 1024 / 1024, 2),
            "file_count": file_count,
            "oldest_file": oldest_file,
            "oldest_days": (datetime.now() - oldest_time).days if oldest_file else 0,
            "newest_file": newest_file
        }
    except Exception as e:
        logger.error("디스크 사용량 조회 실패", exc_info=True)
        return {}
