# app/utils/telegram_handler.py

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger("exchange_rate.utils.telegram")

class TelegramHandler:
    """
    텔레그램 알림 핸들러 (Phase 2)

    용도:
    - 크롤러 연속 실패 알림
    - 시스템 에러 알림
    - 환율 급변동 알림
    """

    def __init__(self):
        self.enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

        if self.enabled and (not self.bot_token or not self.chat_id):
            logger.warning("⚠️ 텔레그램 활성화 상태이지만 BOT_TOKEN 또는 CHAT_ID가 설정되지 않았습니다")
            self.enabled = False

    def send_message(self, message: str, parse_mode: str = "Markdown") -> bool:
        """
        텔레그램 메시지 전송

        Args:
            message: 전송할 메시지
            parse_mode: 메시지 형식 (Markdown, HTML)

        Returns:
            전송 성공 여부
        """
        if not self.enabled:
            logger.debug("텔레그램 알림 비활성화 상태 - 메시지 전송 스킵")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode
        }

        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("✅ 텔레그램 메시지 전송 성공", extra={"message_length": len(message)})
            return True

        except requests.exceptions.RequestException as e:
            logger.error("❌ 텔레그램 메시지 전송 실패", exc_info=True, extra={"error": str(e)})
            return False

    def send_error_alert(self, crawler_name: str, error_message: str, error_count: int = 1) -> bool:
        """
        크롤러 에러 알림

        Args:
            crawler_name: 크롤러 이름
            error_message: 에러 메시지
            error_count: 연속 실패 횟수
        """
        message = f"""
🚨 *크롤러 에러 알림*

은행: `{crawler_name}`
연속 실패: {error_count}회
에러: {error_message[:200]}
"""
        return self.send_message(message)

    def send_rate_spike_alert(self, currency: str, old_rate: float, new_rate: float, change_percent: float) -> bool:
        """
        환율 급변동 알림

        Args:
            currency: 통화쌍
            old_rate: 이전 환율
            new_rate: 새 환율
            change_percent: 변동률 (%)
        """
        direction = "📈" if new_rate > old_rate else "📉"
        message = f"""
{direction} *환율 급변동 알림*

통화: `{currency.upper()}`
변동: {old_rate:.2f} → {new_rate:.2f}
변동률: {change_percent:+.2f}%
"""
        return self.send_message(message)

    def send_system_alert(self, alert_type: str, details: str) -> bool:
        """
        시스템 알림

        Args:
            alert_type: 알림 유형 (메모리, DB, 네트워크 등)
            details: 상세 정보
        """
        message = f"""
⚠️ *시스템 알림*

유형: {alert_type}
상세: {details}
"""
        return self.send_message(message)


# 싱글톤 인스턴스
telegram_handler = TelegramHandler()
