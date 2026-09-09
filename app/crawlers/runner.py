#!/usr/bin/env python3
"""
Selenium 크롤러 Subprocess Runner

각 크롤러를 독립 프로세스에서 실행하는 엔트리포인트입니다.
scheduler.py에서 asyncio.create_subprocess_exec()로 호출됩니다.

Usage:
    python -m app.crawlers.runner <bank_name>
    python scripts/ibk_subprocess_bootstrap.py ibk --run-id <id> \
        --reference-time <aware ISO> --work-deadline <monotonic float>
    두 번째 경로는 결과 adapter 미연결 상태이므로 현재는 수집 전에 종료한다.

Exit Codes:
    0: legacy 성공 / IBK 결과 경로에서는 종료·전달 (부모 결과 검증 필요)
    1: 실패
    2: 잘못된 인자

Notes:
    - legacy 부모는 직접 자식 kill, 새 capture는 소유 프로세스 그룹 kill을 수행
    - 별도 그룹/세션으로 이탈한 자손의 정리는 보장하지 않음
    - 크롤러 함수는 내부에서 자체적으로 DB 연결 관리
    - 로깅은 subprocess에서 독립적으로 초기화
"""

import logging
import os
import sys
from datetime import datetime, timezone

from app.ibk_result_protocol import IbkProtocolError, decode_ibk_result, encode_ibk_result
from app.ibk_run_context import IbkRunContext
from app import config
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


# No implicit LegacyResult/None -> OBSERVED adapter. Bind only a separately verified
# final-result crawler here. Until then the new bootstrap path stops BEFORE DB refresh.
#
# ⛔ 자식은 **자기 프로세스에서** 게이트를 읽는다. 부모가 켜져 있어도 자식이 꺼져 있으면
#    (배포 중 env 불일치) 자식은 결과를 만들지 않고 어댑터 부재로 끝난다 — 조용히 legacy
#    결과를 지어내지 않는다.
IBK_RESULT_CRAWLER = (
    ibk.run_ibk_dated_result if config.IBK_RESULT_PATH_ENABLED else None
)


def _ibk_result_main(args, result_fd, crawler):
    logger = logging.getLogger("exchange_rate.crawler.runner")
    try:
        context = IbkRunContext.from_arguments(args)
        if type(result_fd) is not int or result_fd < 3:
            raise IbkProtocolError("RESULT_CHANNEL_REQUIRED")
    except IbkProtocolError:
        logger.error("IBK_RESULT_ARGUMENTS_REJECTED")
        return 2
    if not callable(crawler):
        logger.error("IBK_RESULT_ADAPTER_UNAVAILABLE")
        return 2
    try:
        # Conditional import: legacy main keeps its original refresh path, and the
        # unbound protocol path must not connect to the DB at all.
        from app.atomic_write_refresh import refresh_write_mode_cache
        refresh_write_mode_cache()
        result = crawler(context)
    except Exception:
        logger.error("IBK_RESULT_CRAWLER_ERROR")
        return 1
    try:
        frame = encode_ibk_result(result)
        checked = decode_ibk_result(frame, context.run_id)
        context.validate_result(checked, received_at=datetime.now(timezone.utc))
    except (IbkProtocolError, AttributeError):
        # The process completed but the result is invalid. Emit no fabricated
        # semantic outcome: IbkParentRunner classifies exit0/no-frame as protocol
        # error without retry. This channel must NEVER use an exit-code-only parent.
        logger.error("IBK_RESULT_REJECTED")
        return 0
    try:
        remaining = memoryview(frame)
        while remaining:
            written = os.write(result_fd, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
    except OSError:
        logger.error("IBK_RESULT_EMIT_ERROR")
        return 1
    logger.info("IBK_RESULT_EMITTED", extra={"ibk_status": checked.status.value})
    return 0  # delivery/completion, NOT a blanket crawler success


def main(*, argv=None, result_fd=None, ibk_result_crawler=None):
    """메인 실행 함수"""
    args = tuple(sys.argv[1:] if argv is None else argv)
    if result_fd is not None:
        crawler = IBK_RESULT_CRAWLER if ibk_result_crawler is None else ibk_result_crawler
        return _ibk_result_main(args, result_fd, crawler)
    # 로거 설정 (subprocess에서 독립 실행)
    logger = logging.getLogger("exchange_rate.crawler.runner")

    # 인자 검증
    if len(args) != 1:
        logger.error("❌ Usage: python -m app.crawlers.runner <bank_name>")
        sys.exit(2)

    bank_name = args[0]

    # P1b A2-2: subprocess는 별 프로세스라 main process의 write-mode cache poll을 공유하지 않음
    # → 진입 시 1회 refresh로 control row를 읽어 cache 세팅 (no-throw). bank 크롤러가 이 subprocess
    # 안에서 insert_bank_rates_into_db를 호출하므로 필요. A2(pre-activation)엔 legacy → 동작 동일.
    from app.atomic_write_refresh import refresh_write_mode_cache
    refresh_write_mode_cache()

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
    sys.exit(main())
