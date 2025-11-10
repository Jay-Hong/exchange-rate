# app/admin/crawler_stats.py

"""
크롤러별 통계 수집 시스템

실시간으로 각 크롤러의 성공/실패/소요시간을 추적하여
관리자 페이지에서 모니터링 가능하도록 지원

메모리 사용량: ~1KB (10개 크롤러 × 100 bytes)
CPU 부담: 무시할 수준 (카운터 증가 O(1))
"""

import time
from datetime import datetime
from typing import Dict, Optional
from dataclasses import dataclass, field, asdict
import threading


@dataclass
class CrawlerStat:
    """개별 크롤러 통계"""
    bank: str
    success_count: int = 0
    fail_count: int = 0
    total_duration: float = 0.0  # 초
    last_success_at: Optional[str] = None  # ISO 8601 format
    last_fail_at: Optional[str] = None
    last_duration: float = 0.0  # 마지막 크롤링 소요 시간 (초)

    @property
    def avg_duration(self) -> float:
        """평균 소요 시간 (초)"""
        if self.success_count == 0:
            return 0.0
        return round(self.total_duration / self.success_count, 2)

    @property
    def success_rate(self) -> float:
        """성공률 (%)"""
        total = self.success_count + self.fail_count
        if total == 0:
            return 0.0
        return round((self.success_count / total) * 100, 1)

    def to_dict(self) -> dict:
        """딕셔너리로 변환 (JSON 직렬화용)"""
        data = asdict(self)
        data['avg_duration'] = self.avg_duration
        data['success_rate'] = self.success_rate
        return data


class CrawlerStatsCollector:
    """크롤러 통계 수집기 (싱글톤)"""

    def __init__(self):
        self.stats: Dict[str, CrawlerStat] = {}
        self._lock = threading.Lock()  # 멀티스레드 안전

    def record_success(self, bank: str, duration: float):
        """
        크롤링 성공 기록

        Args:
            bank: 은행 이름 (예: 'kb', 'hana', 'investing')
            duration: 소요 시간 (초)
        """
        with self._lock:
            if bank not in self.stats:
                self.stats[bank] = CrawlerStat(bank=bank)

            stat = self.stats[bank]
            stat.success_count += 1
            stat.total_duration += duration
            stat.last_duration = round(duration, 2)
            stat.last_success_at = datetime.now().isoformat()

    def record_failure(self, bank: str):
        """
        크롤링 실패 기록

        Args:
            bank: 은행 이름
        """
        with self._lock:
            if bank not in self.stats:
                self.stats[bank] = CrawlerStat(bank=bank)

            stat = self.stats[bank]
            stat.fail_count += 1
            stat.last_fail_at = datetime.now().isoformat()

    def get_stats(self) -> dict:
        """
        모든 크롤러 통계 반환 (JSON 직렬화 가능)

        Returns:
            {
                "investing": {...},
                "kb": {...},
                ...
            }
        """
        with self._lock:
            return {
                bank: stat.to_dict()
                for bank, stat in self.stats.items()
            }

    def get_stat(self, bank: str) -> Optional[dict]:
        """특정 크롤러 통계 반환"""
        with self._lock:
            stat = self.stats.get(bank)
            return stat.to_dict() if stat else None

    def reset(self):
        """모든 통계 초기화 (테스트/디버깅용)"""
        with self._lock:
            self.stats.clear()


# 싱글톤 인스턴스
crawler_stats = CrawlerStatsCollector()
