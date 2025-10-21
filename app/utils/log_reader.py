# app/utils/log_reader.py

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from pytz import timezone
from app.config import LOG_FILES

# 한국 시간대
KST = timezone('Asia/Seoul')

logger = logging.getLogger("exchange_rate.utils.log_reader")

def read_logs(
    log_type: str = "app",
    level: Optional[str] = None,
    limit: int = 300,
    hours: int = 24,
    bank: Optional[str] = None
) -> List[Dict]:
    """
    로그 파일 읽기 (JSON 파싱) - 관리자 페이지용

    Args:
        log_type: "app", "error" (단순화: crawler, debug 제거)
        level: 로그 레벨 필터 (INFO, WARNING, ERROR 등)
        limit: 최대 반환 개수 (기본 300개 = 약 5-10분치 로그)
        hours: 조회 시간 범위 (최근 N시간)
        bank: 은행/크롤러 필터 (investing, kb, hana 등) - 효율적인 백엔드 필터링

    Returns:
        로그 엔트리 리스트 (최신순)

    Note:
        bank 파라미터 사용 시 효율적 필터링:
        - 파일을 읽으면서 즉시 필터링
        - limit에 도달하면 즉시 중단
        - 메모리/CPU/I/O 모두 최적화
    """
    log_file = LOG_FILES.get(log_type)
    if not log_file or not log_file.exists():
        return []

    cutoff_time = datetime.now(KST) - timedelta(hours=hours)
    logs = []

    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    log_entry = json.loads(line.strip())

                    # 시간 필터 (타임존 호환성: naive/aware datetime 모두 처리)
                    timestamp_str = log_entry.get('timestamp', '')
                    log_time = datetime.fromisoformat(timestamp_str)

                    # naive datetime이면 KST로 변환 (기존 로그 호환)
                    if log_time.tzinfo is None:
                        log_time = KST.localize(log_time)

                    if log_time < cutoff_time:
                        continue

                    # 레벨 필터
                    if level and log_entry.get('level') != level:
                        continue

                    # 은행/크롤러 필터 (logger 패턴 OR bank 필드)
                    if bank:
                        logger_name = log_entry.get('logger', '')
                        # 정확한 크롤러 이름 매칭: exchange_rate.crawler.{bank}
                        logger_match = logger_name == f"exchange_rate.crawler.{bank}"
                        bank_match = log_entry.get('bank') == bank
                        if not (logger_match or bank_match):
                            continue

                    logs.append(log_entry)

                except (json.JSONDecodeError, ValueError):
                    continue

        # 최신순 정렬 및 limit 적용
        logs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        return logs[:limit]

    except Exception as e:
        logger.error(f"로그 읽기 실패: {log_type}", exc_info=True)
        return []

def get_log_stats(hours: int = 24) -> Dict:
    """
    로그 통계 조회 (관리자 대시보드용)

    Returns:
        {
            "total": 전체 로그 수,
            "by_level": {레벨별 개수},
            "by_logger": {로거별 개수},
            "errors_last_hour": 최근 1시간 에러 수
        }
    """
    logs = read_logs(log_type="app", hours=hours)

    stats = {
        "total": len(logs),
        "by_level": {},
        "by_logger": {},
        "errors_last_hour": 0
    }

    one_hour_ago = datetime.now(KST) - timedelta(hours=1)

    for log in logs:
        level = log.get('level', 'UNKNOWN')
        logger_name = log.get('logger', 'UNKNOWN')

        stats["by_level"][level] = stats["by_level"].get(level, 0) + 1
        stats["by_logger"][logger_name] = stats["by_logger"].get(logger_name, 0) + 1

        if level in ['ERROR', 'CRITICAL']:
            log_time = datetime.fromisoformat(log.get('timestamp', ''))
            # naive datetime이면 KST로 변환 (기존 로그 호환)
            if log_time.tzinfo is None:
                log_time = KST.localize(log_time)
            if log_time > one_hour_ago:
                stats["errors_last_hour"] += 1

    return stats
