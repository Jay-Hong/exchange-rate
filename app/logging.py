# app/logging.py

import logging
import logging.handlers
import sys
from datetime import datetime
from pythonjsonlogger import jsonlogger
from pytz import timezone
from app.config import ENV, LOG_LEVEL, LOG_FILES, LOG_ROTATION

# 한국 시간대 설정
KST = timezone('Asia/Seoul')

class ColoredFormatter(logging.Formatter):
    """개발 환경용 컬러 포맷터"""
    COLORS = {
        'DEBUG': '\033[36m',    # Cyan
        'INFO': '\033[32m',     # Green
        'WARNING': '\033[33m',  # Yellow
        'ERROR': '\033[31m',    # Red
        'CRITICAL': '\033[35m', # Magenta
    }
    RESET = '\033[0m'

    def format(self, record):
        # 원본 levelname 백업 (다른 핸들러에 영향 방지)
        original_levelname = record.levelname
        log_color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{log_color}{record.levelname}{self.RESET}"
        result = super().format(record)
        # 복원
        record.levelname = original_levelname
        return result

class CustomJsonFormatter(jsonlogger.JsonFormatter):
    """프로덕션용 JSON 포맷터 (타임존 인식)"""
    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        # 관리자 페이지 log_reader에서 필요한 필드들
        log_record['timestamp'] = datetime.fromtimestamp(record.created, tz=KST).isoformat()
        log_record['level'] = record.levelname
        log_record['logger'] = record.name
        log_record['function'] = record.funcName
        log_record['line'] = record.lineno

def setup_logging():
    """
    로깅 시스템 초기화

    LOG_LEVEL 환경 변수로 전체 로그 레벨 제어:
    - DEBUG: 모든 로그 출력 (개발 중 상세 디버깅)
    - INFO: 일반 정보 이상 (기본값, 운영 권장)
    - WARNING: 경고 이상만 (운영 환경 최적화)
    - ERROR: 에러만 (긴급 상황만 추적)
    """

    # 루트 로거 설정 (모든 로거의 기준 레벨)
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    # 기존 핸들러 제거 (중복 방지)
    root_logger.handlers.clear()

    # === 콘솔 핸들러 (터미널 출력) ===
    console_handler = logging.StreamHandler(sys.stdout)
    # LOG_LEVEL 환경 변수를 따름 (DEBUG/INFO/WARNING/ERROR)
    console_handler.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    # 개발 환경: 컬러 출력 (가독성 좋음)
    # 운영 환경: JSON 출력 (로그 수집 도구에 적합)
    if ENV == "development":
        console_formatter = ColoredFormatter(
            '%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    else:
        console_formatter = CustomJsonFormatter(
            '%(timestamp)s %(level)s %(name)s %(message)s'
        )

    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # === 파일 핸들러들 (모두 JSON 형식으로 저장) ===
    # Option 1 (단순화): app.log + error.log 만 사용
    # - 중복 제거 (디스크 66% 절약)
    # - 관리 단순화 (2개만 확인)
    # - 관리자 페이지 필터링으로 크롤러 로그 확인 가능

    # 1. app.log - 모든 운영 로그 (LOG_LEVEL 환경 변수를 따름)
    # 용도: 일반 동작, 환율 변경, 브로드캐스트, 크롤러 활동 등
    # 필터: 없음 (모든 모듈의 LOG_LEVEL 이상 기록)
    app_handler = logging.handlers.RotatingFileHandler(
        LOG_FILES["app"], **LOG_ROTATION
    )
    app_handler.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    app_handler.setFormatter(CustomJsonFormatter())
    root_logger.addHandler(app_handler)

    # 2. error.log - 에러/경고 전용 (WARNING 이상)
    # 용도: 문제 발생 시 빠른 확인, 알림 트리거
    # 필터: 없음 (모든 모듈의 WARNING 이상 기록)
    error_handler = logging.handlers.RotatingFileHandler(
        LOG_FILES["error"], **LOG_ROTATION
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(CustomJsonFormatter())
    root_logger.addHandler(error_handler)

    # PR2: broadcast cron='*' (매초 wake-up) 변경으로 apscheduler.executors.default가
    # 매초 "Running job" + "executed successfully" INFO 2줄 출력 → 하루 ~172,800 lines.
    # 분석 노이즈 + 디스크 폭증 방지 위해 WARNING으로 낮춘다.
    # ERROR/WARNING (실제 misfire / job 실패)는 그대로 기록됨.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

    return logging.getLogger("exchange_rate")

# 로거 인스턴스 생성
logger = setup_logging()


def get_logger(name: str):
    return logging.getLogger(name)
