"""비교 알림 evaluator (ADR-037 S2) — 두 소스 간 spread = left − right 조건 평가.

설계 (ADR-037 Decision 3 + S2 구현 확정 2026-07-03):
- **단일 event loop marshal**: 모든 hook(USDT/KRX async tick + bank/investing sync thread)이
  `emit_comparison_observation()`으로 진입 — sync thread는 `topic_trigger_bridge.schedule_on_loop`
  마샬링(FX shadow 선례). 모든 평가가 단일 `ComparisonAlertEvaluator` 인스턴스의 main loop에서
  실행되어 `_in_flight_settings`(setting_id claim, add → try → finally discard) 하나로 dual-trigger
  중복 발송 차단 — threading.Lock 불요. marshal은 best-effort(실패 시 tick 1회 소실 → 크롤러 주기
  재평가 자기 치유).
- **후보 캐시**: (source, asset) → 해당 키가 left **또는** right인 활성 알림 목록. TTL 10s
  (기존 AlertSettingsCache와 동일 그레인). tick당 DB scan 금지(ADR codex B2).
- **반대편 leg 조회**: `get_latest_rate_unified` — Redis latest 우선(latest:source/bank/investing:*)
  + DB fallback. (rate, observed_at, origin) 반환 — origin(redis|db)은 구조화 로그까지만
  (DB 로그 스키마 미저장, ADR B1 amendment). bank/investing Redis helper는 stale 시 None을
  반환하므로(latest_rates_cache is_stale) 그 경우 DB fallback이 마지막 관측값을 제공 —
  stale gate 미도입(ADR Open 3) semantics 유지, observed_at으로 설명 가능(codex B3).
- **3-session 분리**(기존 evaluator 패턴): load / refetch+gate / persist 각각 별도 session,
  FCM 발사 중 DB session 미보유.
- **flag**: config.COMPARISON_ALERT_ENABLED (default false) — emit 진입점 first-line gate
  (KRX_ALERT_EVALUATOR_ENABLED 패턴).

재사용:
- delivery_allowed(ADR-036 B2 once/repeat gate) — FreshComparisonSnapshot이 동일 4필드
  (enabled/triggered/repeat_interval_sec/last_notified_at)를 갖춰 duck-type 호환.
- send_fcm_multicast_sync(to_thread) / models.get_utc_now(naive UTC).
"""

# 표준 라이브러리
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger("exchange_rate.comparison_evaluator")

# 후보 캐시 TTL (기존 ALERT_CACHE_TTL_SEC과 동일 그레인)
COMPARISON_CACHE_TTL_SEC = 10.0

# bank 세계 source 식별자 (bank_exchange_rates 조회 대상 — legacy_policy allowlist와 정합).
# investing은 별도 테이블(investing_exchange_rates), 그 외(usdt 거래소 5 + krx)는 source_rates.
_BANK_SOURCES = frozenset({"kb", "hana", "shinhan", "woori", "ibk", "nh", "sc", "bs", "citi"})


# ---------------------------------------------------------------------------
# Snapshot dataclasses (ORM-free — session detach 위험 차단, 기존 패턴)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ComparisonCandidate:
    """load 시점 캐시 스냅샷 (cache TTL 10s 동안 사용). refetch가 최종 방어선."""
    setting_id: int
    user_id: str
    tab: str
    left_source: str
    left_asset: str
    right_source: str
    right_asset: str
    diff_type: str          # 'signed' | 'absolute'
    operator: str           # 'gte' | 'lte'
    threshold: float
    device_tokens: Tuple[str, ...]


@dataclass(frozen=True)
class FreshComparisonSnapshot:
    """refetch 결과 (session close 후 gate/발사에 사용).

    delivery_allowed(alert_evaluator)가 요구하는 4필드(enabled/triggered/repeat_interval_sec/
    last_notified_at)를 동일 이름으로 보유 — duck-type 재사용 (FreshSettingSnapshot 미상속:
    source/asset/condition 등 단일 알림 필드가 비교엔 무의미).
    조건 필드(diff_type/operator/threshold)도 refetch 포함 — cache TTL 10s 동안 사용자 변경 시
    stale 발송 차단 (기존 evaluator Codex Finding 선례).
    """
    setting_id: int
    enabled: bool
    triggered: bool
    tab: str
    left_source: str
    left_asset: str
    right_source: str
    right_asset: str
    diff_type: str
    operator: str
    threshold: float
    repeat_interval_sec: Optional[int] = None
    last_notified_at: Optional[datetime] = None


@dataclass(frozen=True)
class UnifiedRate:
    """get_latest_rate_unified 결과. origin은 구조화 로그 전용(DB 미저장 — ADR B1 amendment)."""
    rate: float
    observed_at: Optional[datetime]   # naive UTC (DB timestamp 기준) — 로그/FCM 동봉 (codex B3)
    origin: str                       # 'redis' | 'db'


# ---------------------------------------------------------------------------
# 순수 함수 — 조건 평가 (Decision D: diff_type × operator 4조합)
# ---------------------------------------------------------------------------

def spread_matches(diff_type: str, operator: str, threshold: float, spread: float) -> bool:
    """spread(= left − right, signed raw)가 조건을 충족하는지.

    - signed: spread 그대로 비교 (김프 gte / 역프 lte)
    - absolute: |spread| 비교 (괴리 gte / 수렴 lte)
    미지의 diff_type/operator는 False (fail-closed — API validation이 1차 방어).
    """
    value = abs(spread) if diff_type == "absolute" else spread
    if diff_type not in ("signed", "absolute"):
        return False
    if operator == "gte":
        return value >= threshold
    if operator == "lte":
        return value <= threshold
    return False


# ---------------------------------------------------------------------------
# Unified rate lookup (sync — to_thread에서 호출)
# ---------------------------------------------------------------------------

def _parse_iso_to_naive_utc(value) -> Optional[datetime]:
    """Redis helper가 주는 timestamp(ISO str 또는 datetime)를 naive UTC로 정규화."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is not None:
        from datetime import timezone as _tz
        dt = dt.astimezone(_tz.utc).replace(tzinfo=None)
    return dt


def get_latest_rate_unified(source: str, asset: str) -> Optional[UnifiedRate]:
    """source+asset의 최신 rate — Redis latest 우선 + DB fallback (ADR-037 Decision 3).

    sync 함수 (Redis helper들이 sync client) — async 경로에서는 to_thread로 호출.
    반환 None = Redis/DB 모두 값 없음 (해당 tick 평가 skip — graceful).

    분기: investing → latest:investing:* / bank(_BANK_SOURCES) → latest:bank:* (stale 시 None →
    DB fallback) / 그 외(usdt 거래소 + krx) → latest:source:*. DB fallback은 각 세계의 최신 1건.
    """
    from app import latest_rates_cache

    # 1) Redis latest 우선
    try:
        if source == "investing":
            entry = latest_rates_cache.get_latest_investing_rate_from_sync_job(asset)
        elif source in _BANK_SOURCES:
            entry = latest_rates_cache.get_latest_bank_rate_from_sync_job(source, asset)
        elif source == "krx":
            entry = latest_rates_cache.get_latest_krx_rate_from_sync_job(asset)
        else:
            entry = latest_rates_cache.get_latest_usdt_rate_from_sync_job(source, asset)
    except Exception:
        logger.warning("[comparison] Redis unified lookup 실패 (%s:%s) — DB fallback", source, asset,
                       exc_info=True)
        entry = None

    if entry is not None and entry.get("rate") is not None:
        observed = _parse_iso_to_naive_utc(entry.get("rate_changed_at") or entry.get("timestamp"))
        return UnifiedRate(rate=float(entry["rate"]), observed_at=observed, origin="redis")

    # 2) DB fallback (마지막 관측값 — stale gate 미도입 semantics, ADR Open 3)
    try:
        from app import models
        from app.database import get_db_context

        with get_db_context() as db:
            if source == "investing":
                row = (db.query(models.InvestingExchangeRate)
                       .filter(models.InvestingExchangeRate.currency == asset)
                       .order_by(models.InvestingExchangeRate.timestamp.desc()).first())
            elif source in _BANK_SOURCES:
                row = (db.query(models.BankExchangeRate)
                       .filter(models.BankExchangeRate.bank == source,
                               models.BankExchangeRate.currency == asset)
                       .order_by(models.BankExchangeRate.timestamp.desc()).first())
            else:
                row = (db.query(models.SourceRate)
                       .filter(models.SourceRate.source == source,
                               models.SourceRate.asset == asset)
                       .order_by(models.SourceRate.timestamp.desc()).first())
            if row is None:
                return None
            return UnifiedRate(rate=float(row.rate), observed_at=row.timestamp, origin="db")
    except Exception:
        logger.exception("[comparison] DB unified lookup 실패 (%s:%s)", source, asset)
        return None


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

class ComparisonAlertEvaluator:
    """비교 알림 평가기 — 단일 인스턴스, main loop 전용 (marshal 계약은 모듈 진입 helper가 담당).

    흐름: schedule(source, asset) → 후보 캐시(TTL 10s, left∪right 매칭) → setting_id claim →
    양쪽 leg unified lookup → spread_matches → refetch+delivery_allowed gate → FCM → persist(log).
    """

    def __init__(self, sender=None) -> None:
        # (source, asset) → (tuple[ComparisonCandidate], populated_at epoch)
        self._cache: dict = {}
        self._loading: set = set()
        self._in_flight_settings: set = set()
        self._tasks: set = set()   # strong-ref (GC 방지 — FX shadow 선례)
        self._sender = sender      # 테스트 주입용 (기본 send_fcm_multicast_sync)

    # -- 진입점 (main loop 컨텍스트 전제) --------------------------------

    def schedule(self, source: str, asset: str) -> None:
        """비동기 평가 task 발사 — non-blocking (tick 경로에서 호출)."""
        task = asyncio.create_task(self._evaluate_async(source, asset))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- 후보 로드 (TTL cache) -------------------------------------------

    async def _get_candidates(self, source: str, asset: str) -> tuple:
        key = (source, asset)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and (now - cached[1]) < COMPARISON_CACHE_TTL_SEC:
            return cached[0]
        if key in self._loading:            # thundering-herd 차단 (기존 패턴)
            return cached[0] if cached else ()
        self._loading.add(key)
        try:
            candidates = await asyncio.to_thread(self._load_candidates_sync, source, asset)
            self._cache[key] = (candidates, time.monotonic())
            return candidates
        finally:
            self._loading.discard(key)

    @staticmethod
    def _load_candidates_sync(source: str, asset: str) -> tuple:
        """(source,asset)이 left 또는 right인 활성 알림 + device_tokens (empty 제외)."""
        from sqlalchemy import and_, or_

        from app import models
        from app.database import get_db_context

        with get_db_context() as db:
            rows = (db.query(models.ComparisonAlert)
                    .filter(models.ComparisonAlert.enabled == True,  # noqa: E712
                            or_(and_(models.ComparisonAlert.left_source == source,
                                     models.ComparisonAlert.left_asset == asset),
                                and_(models.ComparisonAlert.right_source == source,
                                     models.ComparisonAlert.right_asset == asset)))
                    .all())
            out = []
            for r in rows:
                tokens = tuple(
                    t[0] for t in db.query(models.UserDevice.device_token)
                    .filter(models.UserDevice.user_id == r.user_id).all()
                )
                if not tokens:
                    continue   # FCM 대상 없음 — 평가 무의미 (기존 패턴)
                out.append(ComparisonCandidate(
                    setting_id=r.id, user_id=r.user_id, tab=r.tab,
                    left_source=r.left_source, left_asset=r.left_asset,
                    right_source=r.right_source, right_asset=r.right_asset,
                    diff_type=r.diff_type, operator=r.operator, threshold=r.threshold,
                    device_tokens=tokens,
                ))
            return tuple(out)

    # -- 평가 -------------------------------------------------------------

    async def _evaluate_async(self, source: str, asset: str) -> None:
        try:
            candidates = await self._get_candidates(source, asset)
            for candidate in candidates:
                if candidate.setting_id in self._in_flight_settings:
                    logger.debug("[comparison] setting %d in-flight, skip", candidate.setting_id)
                    continue
                self._in_flight_settings.add(candidate.setting_id)
                try:
                    await self._send_one(candidate)
                finally:
                    self._in_flight_settings.discard(candidate.setting_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[comparison] evaluator 실패 (격리 — tick 경로 영향 X)")

    async def _send_one(self, candidate: ComparisonCandidate) -> None:
        # 1) 양쪽 leg 조회 (sync unified lookup → to_thread)
        left = await asyncio.to_thread(get_latest_rate_unified,
                                       candidate.left_source, candidate.left_asset)
        right = await asyncio.to_thread(get_latest_rate_unified,
                                        candidate.right_source, candidate.right_asset)
        if left is None or right is None:
            logger.debug("[comparison] leg 값 없음 (setting %d) — skip", candidate.setting_id)
            return

        spread = left.rate - right.rate
        if not spread_matches(candidate.diff_type, candidate.operator, candidate.threshold, spread):
            return

        # 2) refetch + gate (새 session — cache TTL 동안의 변경/중복 최종 방어)
        fresh = await asyncio.to_thread(self._refetch_snapshot_sync, candidate.setting_id)
        if fresh is None:
            return
        from app.notifications.alert_evaluator import delivery_allowed
        from app.models import get_utc_now
        if not delivery_allowed(fresh, get_utc_now()):   # duck-type 재사용 (4필드 동일)
            return
        # refetch된 pair/tab stale 검증 (codex B2) — TTL 10s 창 내 사용자가 left/right/tab을
        # 바꿨으면 이번 tick은 skip (old pair 값으로 new 설정에 발송/log 방지). 다음 tick이
        # fresh candidate로 재평가 (S3 CRUD invalidate_cache가 즉시 반영 경로).
        if (fresh.tab != candidate.tab
                or fresh.left_source != candidate.left_source
                or fresh.left_asset != candidate.left_asset
                or fresh.right_source != candidate.right_source
                or fresh.right_asset != candidate.right_asset):
            logger.debug("[comparison] setting %d pair/tab 변경 감지 — skip (stale cache)",
                         candidate.setting_id)
            return
        # refetch된 조건으로 재검증 (TTL 창 내 사용자 변경 반영)
        if not spread_matches(fresh.diff_type, fresh.operator, fresh.threshold, spread):
            return

        # 3) FCM (session 미보유)
        title, body, data = self._build_payload(fresh, candidate, left, right, spread)
        sender = self._sender
        if sender is None:
            from app.notifications.fcm import send_fcm_multicast_sync as sender  # lazy
        result = await asyncio.to_thread(sender, list(candidate.device_tokens), title, body, data)

        # 4) persist (새 session): mark + log + 무효 토큰 정리
        await asyncio.to_thread(self._persist_result_sync, candidate, fresh, left, right,
                                spread, result)
        logger.info("⚡️ 비교알림 발송", extra={
            "setting_id": candidate.setting_id, "tab": candidate.tab,
            "left": f"{candidate.left_source}:{candidate.left_asset}",
            "right": f"{candidate.right_source}:{candidate.right_asset}",
            "spread": round(spread, 4),
            "left_origin": left.origin, "right_origin": right.origin,   # origin은 로그 전용
            "is_repeat": fresh.repeat_interval_sec is not None,
        })

    # -- storage (sync, to_thread) ----------------------------------------

    @staticmethod
    def _refetch_snapshot_sync(setting_id: int) -> Optional[FreshComparisonSnapshot]:
        from app import models
        from app.database import get_db_context

        with get_db_context() as db:
            r = db.query(models.ComparisonAlert).filter(
                models.ComparisonAlert.id == setting_id).first()
            if r is None:
                return None
            return FreshComparisonSnapshot(
                setting_id=r.id, enabled=r.enabled, triggered=r.triggered, tab=r.tab,
                left_source=r.left_source, left_asset=r.left_asset,
                right_source=r.right_source, right_asset=r.right_asset,
                diff_type=r.diff_type, operator=r.operator, threshold=r.threshold,
                repeat_interval_sec=r.repeat_interval_sec, last_notified_at=r.last_notified_at,
            )

    @staticmethod
    def _build_payload(fresh: FreshComparisonSnapshot, candidate: ComparisonCandidate,
                       left: UnifiedRate, right: UnifiedRate, spread: float):
        """FCM title/body/data — type=comparison_alert (기존 앱은 unknown type 무시)."""
        # ADR-036 payload-flag 계약: is_repeat = **repeat 모드 여부**(repeat_interval_sec 존재) —
        # 첫 발화도 repeat면 true (클라 "repeat면 로컬 비활성화 금지" 판단 기준, codex B1).
        is_repeat = fresh.repeat_interval_sec is not None
        # 푸시 문구 (사용자 2026-07-09): 소스명은 title로, 현재값은 title 마지막,
        # body는 목표/조건만. 비교(absolute)=↔·차이·N원 / 김프(signed)=-·김프·부호값(%).
        from app import source_registry
        from app.crud import BANK_NAMES_KR, WIDE_GAP, format_threshold

        def _disp(src: str, asset: str) -> str:
            # registry(거래소·usd 은행) → BANK_NAMES_KR(FX 은행 jpy/eur·shinhan 등) → upper.
            # FX 비교(usd/jpy/eur) 소스 일부가 registry 미등록이라 대문자 코드 노출 방지 (codex 019f49d2).
            d = source_registry.get_source_definition(src, asset)
            name = d.display_name if d else BANK_NAMES_KR.get(src, src.upper())
            return name.removesuffix("은행")   # title 컴팩트 (하나은행→하나, 국민은행→국민)

        left_disp = _disp(candidate.left_source, candidate.left_asset)
        right_disp = _disp(candidate.right_source, candidate.right_asset)
        arrow = "↑" if fresh.operator == "gte" else "↓"
        direction = "이상" if fresh.operator == "gte" else "이하"
        threshold_str = format_threshold(fresh.threshold)

        if fresh.diff_type == "absolute":   # 비교 (거래소간 차이 — '원' 미표시, 사용자 2026-07-09)
            title = f"📊 {left_disp} ↔ {right_disp}{WIDE_GAP}차이{WIDE_GAP}{format_threshold(abs(spread))}"
            body = f"[ {threshold_str} {arrow}{direction} 도달]"
        else:                               # signed (김프/역프)
            spread_str = format_threshold(spread)   # 부호 유지 (-24.7)
            rr = float(right.rate)
            if rr:
                current = f"{spread_str} ({spread / rr * 100:.2f}%)"
            else:
                current = spread_str
            title = f"📊 {left_disp} - {right_disp}{WIDE_GAP}김프{WIDE_GAP}{current}"
            body = f"[ {threshold_str} {arrow}{direction} 도달]"
        data = {
            "type": "comparison_alert",
            "setting_id": str(fresh.setting_id),
            "tab": fresh.tab,
            "left_source": candidate.left_source, "left_asset": candidate.left_asset,
            "right_source": candidate.right_source, "right_asset": candidate.right_asset,
            "left_rate": str(left.rate), "right_rate": str(right.rate),
            "spread": str(round(spread, 6)),
            "diff_type": fresh.diff_type, "operator": fresh.operator,
            "threshold": str(fresh.threshold),
            "left_observed_at": left.observed_at.isoformat() if left.observed_at else "",
            "right_observed_at": right.observed_at.isoformat() if right.observed_at else "",
            "is_repeat": "true" if is_repeat else "false",
        }
        return title, body, data

    @staticmethod
    def _persist_result_sync(candidate: ComparisonCandidate, fresh: FreshComparisonSnapshot,
                             left: UnifiedRate, right: UnifiedRate, spread: float,
                             fcm_result: dict) -> None:
        """mark(once/repeat 분기) + log 기록 + 무효 토큰 삭제 — 단일 session (기존 persist 패턴)."""
        from app import crud, models
        from app.database import get_db_context

        success = bool(fcm_result.get("success_count", 0) > 0)
        is_repeat = fresh.repeat_interval_sec is not None   # repeat 모드 여부 (ADR-036 계약, codex B1)
        with get_db_context() as db:
            if success:
                crud.mark_comparison_alert_triggered(db, fresh.setting_id, spread)
            db.add(models.ComparisonNotificationLog(
                user_id=candidate.user_id, setting_id=fresh.setting_id, tab=fresh.tab,
                left_source=candidate.left_source, left_asset=candidate.left_asset,
                right_source=candidate.right_source, right_asset=candidate.right_asset,
                diff_type=fresh.diff_type, operator=fresh.operator, threshold=fresh.threshold,
                left_rate=left.rate, right_rate=right.rate, spread=spread,
                left_observed_at=left.observed_at, right_observed_at=right.observed_at,
                is_repeat=is_repeat, success=success,
                error_message=None if success else "all tokens failed",
            ))
            failed = fcm_result.get("failed_tokens") or []
            if failed:
                db.query(models.UserDevice).filter(
                    models.UserDevice.device_token.in_(failed)).delete(synchronize_session=False)
            db.commit()

    # -- cache invalidation (CRUD API에서 호출 — S3) -----------------------

    def invalidate_cache(self) -> None:
        """설정 생성/수정/삭제 시 전체 무효화 (per-key 정밀 무효화는 left/right 4키라 과설계)."""
        self._cache.clear()


# ---------------------------------------------------------------------------
# Module singleton + sync-safe 진입 helper (3 hook 공용)
# ---------------------------------------------------------------------------

_evaluator: Optional[ComparisonAlertEvaluator] = None


def get_comparison_evaluator() -> ComparisonAlertEvaluator:
    global _evaluator
    if _evaluator is None:
        _evaluator = ComparisonAlertEvaluator()
    return _evaluator


def _run_comparison_schedule(source: str, asset: str) -> None:
    """schedule_on_loop 대상 sync wrapper — main loop 위에서 실행됨."""
    get_comparison_evaluator().schedule(source, asset)


def emit_comparison_observation(source: str, asset: str) -> None:
    """3 hook 공용 진입점 (ADR-037 dual-trigger) — 어느 실행 컨텍스트에서든 안전.

    - flag off → zero-overhead return (KRX_ALERT_EVALUATOR_ENABLED 패턴).
    - main loop 위(async tick 경로) → 직접 schedule.
    - sync thread(bank/investing 크롤러) → schedule_on_loop 마샬링 (best-effort —
      실패 시 tick 1회 소실, 크롤러 주기 재평가로 자기 치유. ADR B1 amendment).
    """
    from app import config
    if not config.COMPARISON_ALERT_ENABLED:
        return
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if in_loop:
        _run_comparison_schedule(source, asset)
        return
    from app.topic_trigger_bridge import schedule_on_loop
    if not schedule_on_loop(_run_comparison_schedule, source, asset):
        logger.debug("[comparison] marshal 실패 (loop 미등록/shutdown) — tick skip (best-effort)")
