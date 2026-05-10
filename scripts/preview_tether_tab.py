#!/usr/bin/env python3
"""테더 탭 payload preview (PR Z-2b Stage 3 read-only 데이터 검증 도구, 2026-05-10).

`load_and_build_tether_tab_payload()` 결과를 운영 DB로 read-only 호출 후 stdout
출력. WebSocket publish 효과 0 — `TOPIC_DISPATCHER_ENABLED` 무관, env flag 해석
없음.

사용:
    # 컨테이너 내부에서
    docker compose exec -T fastapi python scripts/preview_tether_tab.py
    docker compose exec -T fastapi python scripts/preview_tether_tab.py --include-krx
    docker compose exec -T fastapi python scripts/preview_tether_tab.py --compact \\
        | jq '.data.usdt_krw'

    # JSONL append (cron / timeline 분석용)
    docker compose exec -T fastapi python scripts/preview_tether_tab.py --compact \\
        >> /tmp/tether_preview.jsonl

용도:
    5/11~5/17 baseline 윈도우 동안 운영 DB로 builder/helper 동작 검증.
    payload shape, 누락 source, timestamp, 정렬, display_name 등 정합성 확인.

설계 원칙:
    - read-only — DB SELECT만, INSERT/UPDATE/DELETE 0
    - publish 효과 0 — `publish_topic` 호출 없음
    - env flag 해석 없음 — `--include-krx`로 호출자가 명시
    - logger를 WARNING으로 격상 — stdout pure JSON 보장 (cron append 안전)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# 프로젝트 루트를 sys.path에 추가 (다른 scripts/와 동일 패턴)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app.usdt_topic_payload import load_and_build_tether_tab_payload  # noqa: E402


def main() -> int:
    # stdout pure JSON 보장 — INFO 로그가 stdout에 섞이면 jq/JSONL append 깨짐.
    # observe_kis_master.py는 fetch logger 1개만 격상하지만, preview는 builder/
    # helper가 여러 app.* logger를 거치므로 app 전체 root logger를 격상.
    logging.getLogger("app").setLevel(logging.WARNING)

    ap = argparse.ArgumentParser(
        description="테더 탭 payload preview (read-only)"
    )
    ap.add_argument(
        "--include-krx",
        action="store_true",
        help="KRX 미국달러선물 포함 (default: 제외)",
    )
    ap.add_argument(
        "--compact",
        action="store_true",
        help="single-line JSON 출력 (jq 파이프 / JSONL append용). "
             "default는 사람이 보기 좋은 indent JSON.",
    )
    args = ap.parse_args()

    db = SessionLocal()
    try:
        payload = load_and_build_tether_tab_payload(
            db,
            include_krx=args.include_krx,
        )
    finally:
        db.close()

    if args.compact:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
