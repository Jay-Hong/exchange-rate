"""크롤러 모듈 - 모든 크롤러 관련 기능"""

from app.crawlers.investing import crawl_and_save_investing_exchange_rates
from app.crawlers.kb import crawl_and_save_kb_bank_exchange_rates
from app.crawlers.hana import crawl_and_save_hana_bank_exchange_rates
from app.crawlers.shinhan import crawl_and_save_shinhan_bank_exchange_rates
from app.crawlers.woori import crawl_and_save_woori_bank_exchange_rates
from app.crawlers.ibk import crawl_and_save_ibk_bank_exchange_rates
from app.crawlers.nh import crawl_and_save_nh_bank_exchange_rates
from app.crawlers.sc import crawl_and_save_sc_bank_exchange_rates
from app.crawlers.bs import crawl_and_save_bs_bank_exchange_rates
from app.crawlers.citi import crawl_and_save_citi_bank_exchange_rates
from app.crawlers.dxy import crawl_and_save_dxy

__all__ = [
    "crawl_and_save_investing_exchange_rates",
    "crawl_and_save_kb_bank_exchange_rates",
    "crawl_and_save_hana_bank_exchange_rates",
    "crawl_and_save_shinhan_bank_exchange_rates",
    "crawl_and_save_woori_bank_exchange_rates",
    "crawl_and_save_ibk_bank_exchange_rates",
    "crawl_and_save_nh_bank_exchange_rates",
    "crawl_and_save_sc_bank_exchange_rates",
    "crawl_and_save_bs_bank_exchange_rates",
    "crawl_and_save_citi_bank_exchange_rates",
    "crawl_and_save_dxy",
]
