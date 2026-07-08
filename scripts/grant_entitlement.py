#!/usr/bin/env python3
"""
entitlement 부여/회수/조회 운영 도구 (ADR-038 G1 — 운영자 수동 부여).

앱 내 입력 UI 없음(Apple 2.3.1 리젝 리스크로 이스터에그 방식 기각 — ADR-038 Decision 1).
운영자가 이 스크립트로 user_entitlements를 직접 관리한다.

사용법:
  # 조회 (읽기 전용 — guard 불요)
  python scripts/grant_entitlement.py list [--user-id UID]

  # 부여 (기본 dry-run — --write로 실제 적용)
  python scripts/grant_entitlement.py grant --user-id UID [--key krx_futures] --write [--allow-production-write]

  # 회수 (기본 dry-run) — 해당 사용자의 KRX 알림(단일 + 김프 counter)도 함께 disable
  #   (evaluator는 entitlement 재검사를 하지 않으므로[hot path 비용 — A1 결정과 동일 축]
  #    회수 시점에 알림을 꺼야 계속 발화하지 않음 — codex Q4 (i) 합의 2026-07-08)
  python scripts/grant_entitlement.py revoke --user-id UID [--key krx_futures] --write [--allow-production-write]

주의:
  - production(비-SQLite)에서 --write는 --allow-production-write 필수 (backfill writer guard 계약).
  - revoke 후 운영 프로세스의 entitlement 판정은 즉시 반영 (무캐시 DB 조회 — codex Q6).
"""

# 표준 라이브러리
import argparse
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_KEY = "krx_futures"
KRX_SOURCE, KRX_ASSET = "krx", "usd-krw-futures"


def check_production_write_guard(allow_production: bool) -> Optional[str]:
    """write 진입 전 production DB guard (backfill writer 패턴 재사용).

    dialect 검사 (sqlite 안전, 그 외 default reject). host redacted (보안 원칙).
    """
    from app.database import engine
    dialect_name = engine.url.get_dialect().name
    if dialect_name == "sqlite":
        return None
    if not allow_production:
        host = engine.url.host or "(unknown)"
        redacted = "***" if host and host != "(unknown)" else "(unknown)"
        return (
            f"non-SQLite DB detected (dialect={dialect_name} host={redacted}). "
            "--allow-production-write 명시 안 됨 — production write 차단."
        )
    return None


def cmd_list(user_id: Optional[str]) -> None:
    from app import models
    from app.database import get_db_context

    with get_db_context() as db:
        q = db.query(models.UserEntitlement)
        if user_id:
            q = q.filter(models.UserEntitlement.user_id == user_id)
        rows = q.order_by(models.UserEntitlement.granted_at.desc()).all()
        print(f"user_entitlements: {len(rows)}건")
        for r in rows:
            print(f"  id={r.id} user={r.user_id} key={r.key} granted_at={r.granted_at}")


def cmd_grant(user_id: str, key: str, write: bool) -> None:
    from app import models
    from app.database import get_db_context

    with get_db_context() as db:
        existing = (db.query(models.UserEntitlement)
                      .filter(models.UserEntitlement.user_id == user_id,
                              models.UserEntitlement.key == key).first())
        if existing:
            print(f"[SKIP] 이미 부여됨: user={user_id} key={key} (granted_at={existing.granted_at})")
            return
        if not write:
            print(f"[DRY-RUN] 부여 예정: user={user_id} key={key} — --write로 적용")
            return
        db.add(models.UserEntitlement(user_id=user_id, key=key))
        db.commit()
        print(f"[OK] 부여 완료: user={user_id} key={key}")


def cmd_revoke(user_id: str, key: str, write: bool) -> None:
    """회수 + (key=krx_futures면) 해당 사용자의 KRX 알림 자동 disable (codex Q4-(i)).

    disable 대상:
      - source_notification_settings: (krx, usd-krw-futures) AND enabled=True
      - comparison_alerts: diff_type='signed' AND right=(krx, usd-krw-futures) AND enabled=True
    삭제가 아니라 disable — 재부여 시 사용자가 직접 재활성 가능 (그 시점 gate 통과 필요).
    """
    from app import models
    from app.database import get_db_context

    with get_db_context() as db:
        ent = (db.query(models.UserEntitlement)
                 .filter(models.UserEntitlement.user_id == user_id,
                         models.UserEntitlement.key == key).first())

        src_alerts = []
        cmp_alerts = []
        if key == DEFAULT_KEY:
            src_alerts = (db.query(models.SourceNotificationSetting)
                            .filter(models.SourceNotificationSetting.user_id == user_id,
                                    models.SourceNotificationSetting.source == KRX_SOURCE,
                                    models.SourceNotificationSetting.asset == KRX_ASSET,
                                    models.SourceNotificationSetting.enabled == True)  # noqa: E712
                            .all())
            cmp_alerts = (db.query(models.ComparisonAlert)
                            .filter(models.ComparisonAlert.user_id == user_id,
                                    models.ComparisonAlert.diff_type == "signed",
                                    models.ComparisonAlert.right_source == KRX_SOURCE,
                                    models.ComparisonAlert.right_asset == KRX_ASSET,
                                    models.ComparisonAlert.enabled == True)  # noqa: E712
                            .all())

        print(f"대상: entitlement={'있음' if ent else '없음(멱등)'} / "
              f"KRX 단일알림 disable {len(src_alerts)}건 / 김프 counter disable {len(cmp_alerts)}건")
        for a in src_alerts:
            print(f"  source setting id={a.id} threshold={a.threshold} condition={a.condition}")
        for a in cmp_alerts:
            print(f"  comparison alert id={a.id} {a.left_source}-{a.right_source} threshold={a.threshold}")

        if not write:
            print("[DRY-RUN] 변경 안 함 — --write로 적용")
            return

        if ent:
            db.delete(ent)
        for a in src_alerts:
            a.enabled = False
        for a in cmp_alerts:
            a.enabled = False
        db.commit()
        print(f"[OK] 회수 완료: entitlement 삭제={'1' if ent else '0'}, "
              f"단일알림 disable={len(src_alerts)}, 김프 disable={len(cmp_alerts)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="entitlement 부여/회수/조회 (ADR-038 G1)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="entitlement 조회 (읽기 전용)")
    p_list.add_argument("--user-id", default=None)

    for name in ("grant", "revoke"):
        sp = sub.add_parser(name)
        sp.add_argument("--user-id", required=True)
        sp.add_argument("--key", default=DEFAULT_KEY)
        sp.add_argument("--write", action="store_true", help="실제 적용 (기본 dry-run)")
        sp.add_argument("--allow-production-write", action="store_true")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args.user_id)
        return

    if args.write:
        guard = check_production_write_guard(args.allow_production_write)
        if guard:
            print(f"[차단] {guard}")
            sys.exit(1)

    if args.command == "grant":
        cmd_grant(args.user_id, args.key, args.write)
    else:
        cmd_revoke(args.user_id, args.key, args.write)


if __name__ == "__main__":
    main()
