"""한국 공휴일 calendar (PUBLIC ∪ BANK) — ADR-034 Phase 2d.

`holidays` 라이브러리 KR 구현 사용.

설계 근거 (실측 검증):
- **observed=True 명시 필수 (load-bearing)**: observed=False면 대체공휴일이 모두 빠짐.
  예) 2026-05-25 부처님오신날 대체(= KRX 운영 사고 날짜) / 2026-03-02 삼일절 대체
  → observed=True에서만 잡힘. default가 True이나 load-bearing이므로 명시 + fixture 잠금.
- **PUBLIC ∪ BANK 결합**: BANK 카테고리 단독은 근로자의날(5/1)만 반환 (공휴일 미포함).
  은행인 Hana는 공휴일 + 근로자의날 모두 휴무 → 두 카테고리 결합 필요.
- **categories 상수 사용**: 문자열 대신 `holidays.constants.PUBLIC / BANK` (typo/rename 안전).
"""

# 표준 라이브러리
from datetime import date
from functools import lru_cache
from typing import Optional

# 서드파티 라이브러리
import holidays
from holidays.constants import BANK, PUBLIC


@lru_cache(maxsize=None)
def _kr_holidays(year: int):
    """연도별 KR 공휴일 (PUBLIC ∪ BANK, observed=True) — lru_cache로 재사용.

    holidays 객체는 연도별로 안정적이므로 캐싱 안전.
    """
    return holidays.SouthKorea(years=year, categories=(PUBLIC, BANK), observed=True)


def is_kr_holiday(d: date) -> bool:
    """해당 날짜가 한국 공휴일(PUBLIC ∪ BANK)인지 여부."""
    return d in _kr_holidays(d.year)


def kr_holiday_name(d: date) -> Optional[str]:
    """공휴일이면 명칭 반환, 아니면 None (provenance/debug용)."""
    return _kr_holidays(d.year).get(d)
