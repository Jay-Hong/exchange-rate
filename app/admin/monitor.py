# app/admin/monitor.py

"""
시스템 모니터링 모듈

- 메모리/CPU 사용량 추적
- Chrome 프로세스 모니터링
- WebSocket 연결 수 추적
- 임계값 초과 시 알림
"""

# 표준 라이브러리
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any
from collections import deque

# 로거 설정
logger = logging.getLogger("exchange_rate.monitor")


class SystemMonitor:
    """시스템 리소스 모니터링 클래스"""

    def __init__(self):
        # 최근 60개 데이터 포인트 저장 (5분 간격 × 60 = 5시간)
        self.memory_history = deque(maxlen=60)
        self.cpu_history = deque(maxlen=60)
        self.chrome_process_history = deque(maxlen=60)

        # 임계값 설정
        self.MEMORY_WARNING_PERCENT = 70  # 메모리 70% 이상 경고
        self.MEMORY_CRITICAL_PERCENT = 85  # 메모리 85% 이상 위험
        self.CHROME_MAX_PROCESSES = 4  # Chrome 프로세스 최대 개수
        self.CHROME_WARNING_PROCESSES = 3  # Chrome 프로세스 경고 개수

    def get_memory_stats(self) -> Dict[str, Any]:
        """메모리 사용량 통계"""
        try:
            import psutil

            memory = psutil.virtual_memory()
            return {
                "used_mb": round(memory.used / (1024 * 1024), 1),
                "available_mb": round(memory.available / (1024 * 1024), 1),
                "percent": round(memory.percent, 1),
                "total_mb": round(memory.total / (1024 * 1024), 1),
            }
        except Exception as e:
            logger.error("메모리 통계 조회 실패", exc_info=True)
            return {"error": str(e)}

    def get_cpu_stats(self) -> Dict[str, Any]:
        """CPU 사용량 통계"""
        try:
            import psutil

            cpu_percent = psutil.cpu_percent(interval=1)
            cpu_count = psutil.cpu_count()

            return {
                "percent": round(cpu_percent, 1),
                "count": cpu_count,
                "per_cpu": [round(p, 1) for p in psutil.cpu_percent(percpu=True)],
            }
        except Exception as e:
            logger.error("CPU 통계 조회 실패", exc_info=True)
            return {"error": str(e)}

    def get_chrome_processes(self) -> Dict[str, Any]:
        """Chrome 프로세스 모니터링"""
        try:
            import psutil
            import time

            chrome_processes = []
            total_memory_mb = 0

            for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'create_time']):
                try:
                    proc_name = proc.info['name'].lower()
                    if any(name in proc_name for name in ['chrome', 'chromedriver']):
                        memory_mb = proc.info['memory_info'].rss / (1024 * 1024)
                        age_seconds = int(time.time() - proc.info['create_time'])

                        chrome_processes.append({
                            "pid": proc.info['pid'],
                            "name": proc.info['name'],
                            "memory_mb": round(memory_mb, 1),
                            "age_seconds": age_seconds,
                        })
                        total_memory_mb += memory_mb
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            return {
                "count": len(chrome_processes),
                "processes": chrome_processes,
                "total_memory_mb": round(total_memory_mb, 1),
            }
        except Exception as e:
            logger.error("Chrome 프로세스 조회 실패", exc_info=True)
            return {"error": str(e)}

    def record_stats(self):
        """통계 기록 (5분마다 호출)"""
        try:
            memory_stats = self.get_memory_stats()
            cpu_stats = self.get_cpu_stats()
            chrome_stats = self.get_chrome_processes()

            # 타임스탬프 추가
            timestamp = datetime.now().isoformat()

            # 히스토리에 추가
            if "error" not in memory_stats:
                self.memory_history.append({
                    "timestamp": timestamp,
                    "percent": memory_stats["percent"],
                    "used_mb": memory_stats["used_mb"],
                })

            if "error" not in cpu_stats:
                self.cpu_history.append({
                    "timestamp": timestamp,
                    "percent": cpu_stats["percent"],
                })

            if "error" not in chrome_stats:
                self.chrome_process_history.append({
                    "timestamp": timestamp,
                    "count": chrome_stats["count"],
                    "total_memory_mb": chrome_stats["total_memory_mb"],
                })

            # 임계값 체크
            self._check_thresholds(memory_stats, chrome_stats)

            # [2025-11-08 이전] DEBUG 레벨 로깅 (관리자 페이지 히스토리 미표시)
            # logger.debug(
            #     "📊 모니터링 통계 기록",
            #     extra={
            #         "memory_percent": memory_stats.get("percent"),
            #         "cpu_percent": cpu_stats.get("percent"),
            #         "chrome_count": chrome_stats.get("count"),
            #     }
            # )
            # [2025-11-08] INFO 레벨로 변경 (관리자 페이지 Chart.js 히스토리 활성화)
            logger.info(
                "📊 모니터링 통계 기록",
                extra={
                    "memory_percent": memory_stats.get("percent"),
                    "cpu_percent": cpu_stats.get("percent"),
                    "chrome_count": chrome_stats.get("count"),
                }
            )

        except Exception as e:
            logger.error("통계 기록 실패", exc_info=True)

    def _check_thresholds(self, memory_stats: Dict, chrome_stats: Dict):
        """임계값 체크 및 경고"""
        # 메모리 임계값 체크
        if "percent" in memory_stats:
            if memory_stats["percent"] >= self.MEMORY_CRITICAL_PERCENT:
                logger.error(
                    "🚨 메모리 사용량 위험",
                    extra={
                        "memory_percent": memory_stats["percent"],
                        "threshold": self.MEMORY_CRITICAL_PERCENT,
                    }
                )
                self._send_alert(
                    f"🚨 메모리 사용량 위험: {memory_stats['percent']}% (임계값: {self.MEMORY_CRITICAL_PERCENT}%)"
                )
            elif memory_stats["percent"] >= self.MEMORY_WARNING_PERCENT:
                logger.warning(
                    "⚠️ 메모리 사용량 경고",
                    extra={
                        "memory_percent": memory_stats["percent"],
                        "threshold": self.MEMORY_WARNING_PERCENT,
                    }
                )

        # Chrome 프로세스 임계값 체크
        if "count" in chrome_stats:
            if chrome_stats["count"] >= self.CHROME_MAX_PROCESSES:
                logger.error(
                    "🚨 Chrome 프로세스 과다",
                    extra={
                        "chrome_count": chrome_stats["count"],
                        "threshold": self.CHROME_MAX_PROCESSES,
                    }
                )
                self._send_alert(
                    f"🚨 Chrome 프로세스 과다: {chrome_stats['count']}개 (최대: {self.CHROME_MAX_PROCESSES}개)"
                )
            elif chrome_stats["count"] >= self.CHROME_WARNING_PROCESSES:
                logger.warning(
                    "⚠️ Chrome 프로세스 경고",
                    extra={
                        "chrome_count": chrome_stats["count"],
                        "threshold": self.CHROME_WARNING_PROCESSES,
                    }
                )

    def _send_alert(self, message: str):
        """알림 전송 (텔레그램 등)"""
        try:
            from app.config import TELEGRAM_ENABLED

            if TELEGRAM_ENABLED:
                from app.notifications.telegram import send_telegram_message

                send_telegram_message(message)
                logger.info("📨 텔레그램 알림 전송", extra={"message": message})
        except Exception as e:
            logger.error("알림 전송 실패", exc_info=True)

    def get_history(self, hours: int = 1) -> Dict[str, List]:
        """최근 N시간 히스토리 조회"""
        cutoff_time = datetime.now() - timedelta(hours=hours)

        def filter_recent(history):
            return [
                item for item in history
                if datetime.fromisoformat(item["timestamp"]) > cutoff_time
            ]

        return {
            "memory": filter_recent(self.memory_history),
            "cpu": filter_recent(self.cpu_history),
            "chrome_processes": filter_recent(self.chrome_process_history),
        }

    def get_current_stats(self) -> Dict[str, Any]:
        """현재 시스템 상태 조회 (API용)"""
        memory_stats = self.get_memory_stats()
        cpu_stats = self.get_cpu_stats()
        chrome_stats = self.get_chrome_processes()

        return {
            "timestamp": datetime.now().isoformat(),
            "memory": memory_stats,
            "cpu": cpu_stats,
            "chrome": chrome_stats,
            "health": {
                "memory_ok": memory_stats.get("percent", 100) < self.MEMORY_WARNING_PERCENT,
                "chrome_ok": chrome_stats.get("count", 99) < self.CHROME_WARNING_PROCESSES,
            }
        }


# 글로벌 모니터 인스턴스
system_monitor = SystemMonitor()
