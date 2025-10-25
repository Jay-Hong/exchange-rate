# app/admin/stats.py

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger("exchange_rate.utils.broadcast_stats")


class BroadcastStats:
    """
    WebSocket 브로드캐스트 통계 수집 및 관리

    확장 가능한 설계:
    - 히스토리 저장으로 그래프 추가 용이
    - 에러 로그 관리로 알림 시스템 연동 가능
    - 통계 메서드 분리로 새로운 지표 추가 용이
    """

    def __init__(self, max_history: int = 100, max_errors: int = 10):
        """
        Args:
            max_history: 저장할 최대 히스토리 개수 (그래프용)
            max_errors: 저장할 최대 에러 개수
        """
        # 기본 카운터
        self.total_success = 0
        self.total_failure = 0

        # 시간 추적
        self.last_broadcast_time: Optional[datetime] = None
        self.first_broadcast_time: Optional[datetime] = None

        # 히스토리 (그래프 및 평균 계산용)
        self.broadcast_history = deque(maxlen=max_history)
        self.error_history = deque(maxlen=max_errors)

        # 최근 데이터 크기 (KB)
        self.recent_sizes = deque(maxlen=20)

    def record_success(self, data_size_bytes: int, rate_count: int):
        """
        성공적인 브로드캐스트 기록

        Args:
            data_size_bytes: 전송된 데이터 크기 (bytes)
            rate_count: 전송된 환율 개수
        """
        now = datetime.now()

        # 첫 브로드캐스트 시간 기록
        if self.first_broadcast_time is None:
            self.first_broadcast_time = now

        # 간격 계산 (이전 브로드캐스트와의 시간차)
        interval = None
        if self.last_broadcast_time:
            interval = (now - self.last_broadcast_time).total_seconds()

        # 히스토리 저장 (그래프용)
        self.broadcast_history.append({
            "timestamp": now.isoformat(),
            "status": "success",
            "interval": interval,
            "data_size_kb": round(data_size_bytes / 1024, 2),
            "rate_count": rate_count
        })

        # 통계 업데이트
        self.total_success += 1
        self.last_broadcast_time = now
        self.recent_sizes.append(data_size_bytes)

        logger.debug(
            f"📡 브로드캐스트 성공 기록",
            extra={
                "size_kb": round(data_size_bytes / 1024, 2),
                "interval": interval,
                "total_success": self.total_success
            }
        )

    def record_failure(self, error_message: str):
        """
        실패한 브로드캐스트 기록

        Args:
            error_message: 에러 메시지
        """
        now = datetime.now()

        # 에러 히스토리 저장 (알림 시스템 연동용)
        self.error_history.append({
            "timestamp": now.isoformat(),
            "error": error_message
        })

        # 히스토리 저장
        self.broadcast_history.append({
            "timestamp": now.isoformat(),
            "status": "failure",
            "error": error_message
        })

        self.total_failure += 1

        logger.warning(
            f"❌ 브로드캐스트 실패 기록",
            extra={
                "error": error_message,
                "total_failure": self.total_failure
            }
        )

    def record_skip(self, reason: str = "no_changes"):
        """
        브로드캐스트 스킵 기록 (변경사항 없음 등)

        Args:
            reason: 스킵 이유
        """
        now = datetime.now()

        self.broadcast_history.append({
            "timestamp": now.isoformat(),
            "status": "skipped",
            "reason": reason
        })

    def get_stats(self) -> Dict:
        """
        현재 통계 반환 (관리자 페이지용)

        Returns:
            {
                "last_broadcast": "3초 전",
                "last_broadcast_time": "2025-10-07T15:30:00",
                "success_count": 1245,
                "failure_count": 2,
                "success_rate": 99.84,
                "avg_interval": 10.2,
                "avg_data_size_kb": 12.4,
                "uptime_hours": 24.5,
                "broadcasts_per_hour": 360
            }
        """
        now = datetime.now()

        # 마지막 브로드캐스트 시간 계산
        last_broadcast_ago = None
        last_broadcast_time_str = None
        if self.last_broadcast_time:
            delta = now - self.last_broadcast_time
            last_broadcast_ago = self._format_time_ago(delta)
            last_broadcast_time_str = self.last_broadcast_time.isoformat()

        # 성공률 계산
        total = self.total_success + self.total_failure
        success_rate = (self.total_success / total * 100) if total > 0 else 0

        # 평균 간격 계산 (최근 20개)
        recent_intervals = [
            h["interval"] for h in list(self.broadcast_history)[-20:]
            if h.get("status") == "success" and h.get("interval") is not None
        ]
        avg_interval = sum(recent_intervals) / len(recent_intervals) if recent_intervals else 0

        # 평균 데이터 크기 계산
        avg_size_kb = (sum(self.recent_sizes) / len(self.recent_sizes) / 1024) if self.recent_sizes else 0

        # 가동시간 및 시간당 브로드캐스트 수
        uptime_hours = 0
        broadcasts_per_hour = 0
        if self.first_broadcast_time:
            uptime = now - self.first_broadcast_time
            uptime_hours = uptime.total_seconds() / 3600
            if uptime_hours > 0:
                broadcasts_per_hour = self.total_success / uptime_hours

        return {
            "last_broadcast": last_broadcast_ago or "없음",
            "last_broadcast_time": last_broadcast_time_str,
            "success_count": self.total_success,
            "failure_count": self.total_failure,
            "success_rate": round(success_rate, 2),
            "avg_interval": round(avg_interval, 1) if avg_interval else 0,
            "avg_data_size_kb": round(avg_size_kb, 1),
            "uptime_hours": round(uptime_hours, 1),
            "broadcasts_per_hour": round(broadcasts_per_hour, 1),
            "total_broadcasts": total,
            "status": self._get_health_status()
        }

    def get_recent_history(self, limit: int = 50) -> List[Dict]:
        """
        최근 브로드캐스트 히스토리 반환 (그래프용)

        Args:
            limit: 반환할 최대 개수

        Returns:
            최근 히스토리 리스트
        """
        return list(self.broadcast_history)[-limit:]

    def get_recent_errors(self) -> List[Dict]:
        """
        최근 에러 목록 반환 (알림 시스템용)

        Returns:
            최근 에러 리스트
        """
        return list(self.error_history)

    def _format_time_ago(self, delta: timedelta) -> str:
        """
        시간차를 사람이 읽기 쉬운 형식으로 변환

        Args:
            delta: 시간차

        Returns:
            "3초 전", "2분 전" 등
        """
        seconds = int(delta.total_seconds())

        if seconds < 60:
            return f"{seconds}초 전"
        elif seconds < 3600:
            minutes = seconds // 60
            return f"{minutes}분 전"
        else:
            hours = seconds // 3600
            return f"{hours}시간 전"

    def _get_health_status(self) -> str:
        """
        브로드캐스트 건강 상태 판단

        Returns:
            "healthy", "warning", "critical"
        """
        # 최근 브로드캐스트 시간 확인
        if not self.last_broadcast_time:
            return "unknown"

        now = datetime.now()
        delta = now - self.last_broadcast_time

        # 30초 이상 브로드캐스트 없으면 warning
        if delta.total_seconds() > 30:
            return "warning"

        # 60초 이상 없으면 critical
        if delta.total_seconds() > 60:
            return "critical"

        # 최근 1시간 성공률 확인
        recent_hour = [
            h for h in self.broadcast_history
            if datetime.fromisoformat(h["timestamp"]) > now - timedelta(hours=1)
        ]

        if recent_hour:
            recent_success = len([h for h in recent_hour if h.get("status") == "success"])
            recent_total = len([h for h in recent_hour if h.get("status") in ["success", "failure"]])

            if recent_total > 0:
                success_rate = recent_success / recent_total
                if success_rate < 0.9:  # 90% 미만
                    return "warning"

        return "healthy"

    def reset_stats(self):
        """통계 초기화 (테스트 또는 관리자 수동 리셋용)"""
        self.total_success = 0
        self.total_failure = 0
        self.last_broadcast_time = None
        self.first_broadcast_time = None
        self.broadcast_history.clear()
        self.error_history.clear()
        self.recent_sizes.clear()

        logger.info("🔄 브로드캐스트 통계 초기화")


# 전역 인스턴스 (main.py에서 사용)
broadcast_stats = BroadcastStats()
