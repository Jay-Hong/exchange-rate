"""FX topic membership contract (PR D / P1b B2a — success watermark membership 기준).

§5.1 line 65: "기준 source 집합 = source_registry **또는 별도 고정 계약**". FX는 source_registry가
9은행 전체·jpy/eur를 모델링하지 않으므로(§15 line 195 'source_registry 파생' 문구는 FX에 부정확 —
source_registry엔 usd-krw 기준 investing/kb/hana만) **별도 고정 계약**으로 명시한다. fx:<asset>
snapshot의 source 집합 = BANK_DISPLAY_ORDER(9은행) + investing reference (§12 line 57 '9은행+investing').

membership_version은 **명시 int 상수** — set 변경(은행 추가/제거)은 §5.1 "명시적 membership revision
변경"이라 FX_MEMBERSHIP_VERSION을 함께 bump해야 한다(lock-test가 강제). derived hash 대신 명시 상수로
둬 의도적 변경만 허용. B2a BuildResult / B2b coordinator / B1 watermark 모두 이 단일 source를 공유.
"""
from __future__ import annotations

from app.crud import BANK_DISPLAY_ORDER

# fx:<asset> snapshot membership = 9 banks(BANK_DISPLAY_ORDER) + investing reference.
FX_MEMBERSHIP_SOURCES = frozenset(BANK_DISPLAY_ORDER) | {"investing"}

# 명시 membership revision. FX_MEMBERSHIP_SOURCES 변경 시 반드시 함께 bump (lock-test 강제 — §5.1).
FX_MEMBERSHIP_VERSION = 1


def fx_membership_version() -> int:
    """현 FX membership contract version (B2a BuildResult / B2b / B1 watermark 공유 single source)."""
    return FX_MEMBERSHIP_VERSION
