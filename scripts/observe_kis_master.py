#!/usr/bin/env python3
"""KIS commodity master observer (PR6c-2d-3 follow-up, 2026-05-08).

5/18 만기 관찰 ad-hoc 스크립트. KIS 상품선물 master file 다운로드 →
USD futures 관찰 메타데이터 (short_code, name, contract_month, mmsc_cls_code) JSONL 1줄 출력.

cron 또는 manual 실행으로 시계열 누적 (>> /tmp/krx_master_obs.jsonl) 후 분석.

사용:
    python scripts/observe_kis_master.py
    python scripts/observe_kis_master.py --short-code A75605 --short-code A75606
    python scripts/observe_kis_master.py >> /tmp/krx_master_obs.jsonl

v1 범위: master file → JSONL snapshot. REST 응답 / cron 등록 / DB 저장은 제외.

Codex 합의 (2026-05-08):
- mmsc_cls_code offset 지식은 app/sources/kis_master.py의 helper 사용 (드리프트 방지)
- 운영 ContractInfo는 변경 X
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

# 프로젝트 루트를 sys.path에 추가 (다른 scripts/와 동일 패턴)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sources.kis_master import (  # noqa: E402
    extract_commodity_future_master_observation,
    fetch_commodity_future_master,
)

KST = timezone(timedelta(hours=9))
USD_NAME_PREFIX = "미국달러 F"
CONTRACT_MONTH_RE = re.compile(r"F\s+(\d{6})")


def collect_observations(
    raw: bytes,
    filter_codes: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """raw master 바이트 → USD futures 관찰 dict list.

    Args:
        raw: cp949 fixed-width 바이트 (fetch_commodity_future_master 결과)
        filter_codes: 필터할 short_code 집합. None이면 USD futures 전체.

    Returns:
        list of dict: {short_code, name, contract_month, mmsc_cls_code}.
    """
    observations: List[Dict[str, Any]] = []
    for row in raw.decode("cp949", errors="replace").splitlines():
        obs = extract_commodity_future_master_observation(row)
        if obs is None:
            continue
        if not obs["name"].startswith(USD_NAME_PREFIX):
            continue
        if filter_codes and obs["short_code"] not in filter_codes:
            continue
        m = CONTRACT_MONTH_RE.search(obs["name"])
        obs["contract_month"] = m.group(1) if m else None
        observations.append(obs)
    return observations


def main() -> int:
    # Codex 검토 fix (2026-05-08): stdout pure JSONL 보장.
    # `>> /tmp/*.jsonl` cron append 시 fetch INFO 로그가 첫 줄에 섞여 jq/json.tool
    # 파싱이 깨지는 문제 차단. fetch 실패는 stderr로 별도 경로 (return 1).
    logging.getLogger("app.sources.kis_master").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(
        description="KIS commodity master observer (PR6c-2d-3, v1)"
    )
    ap.add_argument(
        "--short-code",
        action="append",
        default=None,
        help="필터할 short_code (반복 허용, 미지정 시 USD futures 전체)",
    )
    args = ap.parse_args()

    try:
        raw = fetch_commodity_future_master(timeout=10)
    except Exception as e:
        print(f"fetch 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    filter_codes = set(args.short_code) if args.short_code else None
    observations = collect_observations(raw, filter_codes)

    snapshot = {
        "timestamp": datetime.now(KST).isoformat(),
        "contracts": observations,
    }
    print(json.dumps(snapshot, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
