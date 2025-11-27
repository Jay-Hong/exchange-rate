#!/usr/bin/env python3
"""
Selenium 크롤러 Subprocess Runner

각 크롤러를 독립 프로세스에서 실행하는 엔트리포인트입니다.
scheduler.py에서 asyncio.create_subprocess_exec()로 호출됩니다.

Usage:
    python -m app.crawlers.runner <bank_name>

Exit Codes:
    0: 성공
    1: 실패
    2: 잘못된 인자

Notes:
    - 타임아웃 시 프로세스 전체가 kill되므로 Chrome 프로세스도 함께 종료됨
    - driver.quit() 실행 여부와 무관하게 프로세스 정리 보장
    - 크롤러 함수는 내부에서 자체적으로 DB 연결 관리
    - 로깅은 subprocess에서 독립적으로 초기화
"""

import sys
import logging
from app.crawlers import (
    shinhan,
    ibk,
    nh,
    sc,
    hana,
    woori
)

# 크롤러 함수 매핑 (Selenium 기반만)
CRAWLER_MAP = {
    # 순수 Selenium 크롤러 (scheduler.py에서 enqueue_selenium_job으로 호출)
    'shinhan': shinhan.crawl_and_save_shinhan_bank_exchange_rates,
    'ibk': ibk.crawl_and_save_ibk_bank_exchange_rates,
    'nh': nh.crawl_and_save_nh_bank_exchange_rates,
    'sc': sc.crawl_and_save_sc_bank_exchange_rates,

    # Selenium 폴백 엔트리포인트 (현재 미사용, 향후 직접 호출용)
    # hana, woori는 scheduler.py에서 Request 기반으로 등록되고 내부에서 자체 폴백
    'hana_selenium': hana.crawl_and_save_hana_routine_selenium_entrypoint,
    'woori_selenium': woori.crawl_and_save_woori_routine_selenium_entrypoint,
}


def main():
    """메인 실행 함수"""
    # 로거 설정 (subprocess에서 독립 실행)
    logger = logging.getLogger("exchange_rate.crawler.runner")

    # 인자 검증
    if len(sys.argv) != 2:
        logger.error("❌ Usage: python -m app.crawlers.runner <bank_name>")
        sys.exit(2)

    bank_name = sys.argv[1]

    # 크롤러 함수 가져오기
    crawler_func = CRAWLER_MAP.get(bank_name)
    if not crawler_func:
        logger.error(f"❌ Unknown bank: {bank_name}")
        logger.error(f"Available banks: {list(CRAWLER_MAP.keys())}")
        sys.exit(2)

    try:
        logger.info(f"⏳ [{bank_name}] subprocess 크롤링 시작")

        # 크롤러 실행 (DB 연결은 크롤러 내부에서 자체 관리)
        crawler_func()

        logger.info(f"✅ [{bank_name}] subprocess 크롤링 성공")
        sys.exit(0)  # 성공

    except Exception as e:
        logger.exception(f"❌ [{bank_name}] subprocess 크롤링 실패")
        sys.exit(1)  # 실패


if __name__ == "__main__":
    main()
