"""배포 2 전 최소 계측 계약.

KB/Hana/Woori가 모든 수집 경로 실패나 hard-fail 저장 보류를 정상 반환하면
request wrapper가 거짓 성공을 기록한다. 최종 실패 전파와 연속 실패 통계를
함께 잠근다.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from app.admin.crawler_stats import CrawlerStatsCollector
from app.crawlers import hana, kb, woori
from app import scheduler


def _evaluation(*, hard_fail: bool = False, soft_fail: bool = False) -> dict:
    return {"hard_fail": hard_fail, "soft_fail": soft_fail, "details": {}}


def test_kb_all_three_paths_failed_propagates_failure():
    db = MagicMock()
    with (
        patch.object(kb, "SessionLocal", return_value=db),
        patch.object(
            kb, "crawl_and_save_routine", side_effect=[RuntimeError("primary"), RuntimeError("secondary")]
        ),
        patch.object(kb, "_crawl_mibank_kb", side_effect=RuntimeError("mibank")),
    ):
        with pytest.raises(RuntimeError, match="모든 URL 실패"):
            kb.crawl_and_save_kb_bank_exchange_rates()

    db.close.assert_called_once()


@pytest.mark.parametrize(
    ("module", "mibank_name"),
    ((hana, "_crawl_mibank_hana"), (woori, "_crawl_mibank_woori")),
)
def test_hybrid_bank_all_three_paths_failed_propagates_failure(module, mibank_name):
    db = MagicMock()
    patches = [
        patch.object(module, "SessionLocal", return_value=db),
        patch.object(module, "crawl_and_save_routine", side_effect=RuntimeError("primary")),
        patch.object(module, "_run_selenium_subprocess_fallback", side_effect=RuntimeError("selenium")),
        patch.object(module, mibank_name, side_effect=RuntimeError("mibank")),
    ]
    if module is woori:
        patches.append(patch.object(woori, "is_mibank_rate_reliable", return_value=True))

    with patches[0], patches[1], patches[2], patches[3]:
        if len(patches) == 5:
            with patches[4]:
                with pytest.raises(RuntimeError, match="모든 URL 실패"):
                    module.__dict__[f"crawl_and_save_{module.BANK_NAME}_bank_exchange_rates"]()
        else:
            with pytest.raises(RuntimeError, match="모든 URL 실패"):
                module.__dict__[f"crawl_and_save_{module.BANK_NAME}_bank_exchange_rates"]()

    db.close.assert_called_once()


def test_woori_unreliable_mibank_skip_after_two_failures_is_failure():
    db = MagicMock()
    with (
        patch.object(woori, "SessionLocal", return_value=db),
        patch.object(woori, "crawl_and_save_routine", side_effect=RuntimeError("primary")),
        patch.object(
            woori, "_run_selenium_subprocess_fallback", side_effect=RuntimeError("selenium")
        ),
        patch.object(woori, "is_mibank_rate_reliable", return_value=False),
    ):
        with pytest.raises(RuntimeError, match="신뢰 불가 시간대"):
            woori.crawl_and_save_woori_bank_exchange_rates()

    db.close.assert_called_once()


@pytest.mark.parametrize(
    ("module", "mibank_name"),
    (
        (kb, "_crawl_mibank_kb"),
        (hana, "_crawl_mibank_hana"),
        (woori, "_crawl_mibank_woori"),
    ),
)
def test_hard_fail_that_withholds_storage_propagates_failure(module, mibank_name):
    db = MagicMock()
    crawl_function = {
        kb: kb.crawl_and_save_kb_bank_exchange_rates,
        hana: hana.crawl_and_save_hana_bank_exchange_rates,
        woori: woori.crawl_and_save_woori_bank_exchange_rates,
    }[module]

    with ExitStack() as stack:
        stack.enter_context(patch.object(module, "SessionLocal", return_value=db))
        stack.enter_context(
            patch.object(module, "crawl_and_save_routine", side_effect=RuntimeError("primary"))
        )
        stack.enter_context(
            patch.object(
                module,
                mibank_name,
                return_value=({"usd-krw": 1400.0}, _evaluation(hard_fail=True)),
            )
        )
        insert = stack.enter_context(
            patch.object(module.crud, "insert_bank_rates_into_db")
        )
        if module in (hana, woori):
            stack.enter_context(
                patch.object(
                    module,
                    "_run_selenium_subprocess_fallback",
                    side_effect=RuntimeError("selenium"),
                )
            )
        if module is woori:
            stack.enter_context(
                patch.object(woori, "is_mibank_rate_reliable", return_value=True)
            )

        with pytest.raises(RuntimeError, match="hard_fail"):
            crawl_function()

        insert.assert_not_called()

    db.close.assert_called_once()


def test_unchanged_valid_mibank_result_remains_success():
    db = MagicMock()
    with (
        patch.object(kb, "SessionLocal", return_value=db),
        patch.object(
            kb, "crawl_and_save_routine", side_effect=[RuntimeError("primary"), RuntimeError("secondary")]
        ),
        patch.object(
            kb,
            "_crawl_mibank_kb",
            return_value=({"usd-krw": 1400.0}, _evaluation()),
        ),
        patch.object(kb.crud, "insert_bank_rates_into_db", return_value=0) as insert,
    ):
        kb.crawl_and_save_kb_bank_exchange_rates()

    insert.assert_called_once()
    db.close.assert_called_once()


@pytest.mark.parametrize(
    ("module", "crawl_function", "mibank_name"),
    (
        (kb, kb.crawl_and_save_kb_bank_exchange_rates, "_crawl_mibank_kb"),
        (hana, hana.crawl_and_save_hana_bank_exchange_rates, "_crawl_mibank_hana"),
        (woori, woori.crawl_and_save_woori_bank_exchange_rates, "_crawl_mibank_woori"),
    ),
)
def test_primary_request_success_stops_before_fallbacks(module, crawl_function, mibank_name):
    """계측 수리가 정상 1차 성공을 실패 또는 폴백으로 바꾸면 안 된다."""
    db = MagicMock()
    with ExitStack() as stack:
        stack.enter_context(patch.object(module, "SessionLocal", return_value=db))
        primary = stack.enter_context(
            patch.object(module, "crawl_and_save_routine", return_value=0)
        )
        mibank = stack.enter_context(patch.object(module, mibank_name))
        selenium = None
        if module in (hana, woori):
            selenium = stack.enter_context(
                patch.object(module, "_run_selenium_subprocess_fallback")
            )

        crawl_function()

    primary.assert_called_once()
    mibank.assert_not_called()
    if selenium is not None:
        selenium.assert_not_called()
    db.close.assert_called_once()


@pytest.mark.parametrize(
    ("module", "crawl_function", "mibank_name"),
    (
        (hana, hana.crawl_and_save_hana_bank_exchange_rates, "_crawl_mibank_hana"),
        (woori, woori.crawl_and_save_woori_bank_exchange_rates, "_crawl_mibank_woori"),
    ),
)
def test_hybrid_selenium_success_stops_before_mibank(module, crawl_function, mibank_name):
    """하나·우리의 성공한 중간 Selenium 폴백은 계속 성공으로 끝나야 한다."""
    db = MagicMock()
    with (
        patch.object(module, "SessionLocal", return_value=db),
        patch.object(module, "crawl_and_save_routine", side_effect=RuntimeError("primary")),
        patch.object(module, "_run_selenium_subprocess_fallback", return_value=None) as selenium,
        patch.object(module, mibank_name) as mibank,
    ):
        crawl_function()

    selenium.assert_called_once()
    mibank.assert_not_called()
    db.close.assert_called_once()


@pytest.mark.parametrize(
    ("module", "crawl_function", "mibank_name"),
    (
        (hana, hana.crawl_and_save_hana_bank_exchange_rates, "_crawl_mibank_hana"),
        (woori, woori.crawl_and_save_woori_bank_exchange_rates, "_crawl_mibank_woori"),
    ),
)
def test_hybrid_valid_mibank_soft_fail_is_saved(module, crawl_function, mibank_name):
    """마지막 폴백의 유효 soft-fail은 기존 계약대로 저장하고 정상 반환한다."""
    db = MagicMock()
    rates = {"usd-krw": 1400.0}
    with ExitStack() as stack:
        stack.enter_context(patch.object(module, "SessionLocal", return_value=db))
        stack.enter_context(
            patch.object(module, "crawl_and_save_routine", side_effect=RuntimeError("primary"))
        )
        stack.enter_context(
            patch.object(
                module,
                "_run_selenium_subprocess_fallback",
                side_effect=RuntimeError("selenium"),
            )
        )
        stack.enter_context(
            patch.object(
                module,
                mibank_name,
                return_value=(rates, _evaluation(soft_fail=True)),
            )
        )
        if module is woori:
            stack.enter_context(
                patch.object(woori, "is_mibank_rate_reliable", return_value=True)
            )
        insert = stack.enter_context(
            patch.object(module.crud, "insert_bank_rates_into_db", return_value=0)
        )

        crawl_function()

    insert.assert_called_once_with(db=db, current_rates=rates, bank_name=module.BANK_NAME)
    db.close.assert_called_once()


def test_wrapper_failure_increments_and_success_resets_consecutive_failures():
    stats = CrawlerStatsCollector()

    with patch.object(scheduler, "crawler_stats", stats):
        scheduler.make_request_crawler_wrapper(
            "hana", MagicMock(side_effect=RuntimeError("all paths failed"))
        )()
        scheduler.make_request_crawler_wrapper(
            "hana", MagicMock(side_effect=RuntimeError("all paths failed again"))
        )()

        failed = stats.get_stat("hana")
        assert failed["success_count"] == 0
        assert failed["fail_count"] == 2
        assert failed["consecutive_failures"] == 2

        scheduler.make_request_crawler_wrapper("hana", MagicMock(return_value=None))()

    recovered = stats.get_stat("hana")
    assert recovered["success_count"] == 1
    assert recovered["fail_count"] == 2
    assert recovered["consecutive_failures"] == 0
