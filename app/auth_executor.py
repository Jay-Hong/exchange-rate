"""인증 전용 executor lane — **동거 `to_thread` 작업과 스레드를 나눈다**.

구조: 상태·동작은 `AuthExecutorLane` 이 소유하고, 모듈은 **WS singleton(`_ws_lane`) + 공개
함수 facade + startup 보상 scope** 를 노출한다. 내부 helper 에는 facade 를 두지 않는다 — 모듈
심볼을 patch 해도 lane 메서드에 닿지 않아 fault injection 이 공허해지고, `_metrics` 는
copy-and-swap 으로 재바인딩되므로 모듈 alias 는 첫 swap 직후 stale 이 된다. white-box 는
`_ws_lane` 을 직접 쓴다.
⚠️ 현재 live lane 은 WS 하나뿐이다(REST lane 은 S1b).

무엇을 고치나: `asyncio.to_thread` 는 loop 의 **기본** executor 를 쓴다. 그래서 재연결 폭주로
subscribe 인증이 몰리면 같은 pool 을 쓰는 다른 작업(atomic loader/store, DB selector 등)이 함께
밀린다. 측정: 동거 작업 p99 **3967ms → 31ms**(격리 적용 시).

⛔ **admission control 이 아니다.** 아무도 거절하지 않고 자원만 나눈다 — 새 wire 결과가 없다.
   (즉시거절 semaphore 는 별도로 **기각**됐다: 배포 재연결은 평균 유입이 낮아도 *동시 도착*이라
   1초면 빠질 큐를 대량 거절한다.)

설계 계약
- ⛔ **loop 의 default executor 를 교체하지 않는다**(`set_default_executor` 금지). 교체하면 격리가
  아니라 *전체 이주*가 되어, 인증이 아닌 작업까지 이 pool 의 W 에 갇힌다.
- ⛔ **미초기화·종료 상태에서 `asyncio.to_thread` 로 fallback 하지 않는다.** fallback 은 격리를
  **조용히 무효화**하는데, 하필 그게 필요한 순간(기동 직후·종료 중 폭주)에 그렇게 된다.
  fail-closed 로 `AuthExecutorNotReady` 를 던진다.
  ⚠️ **§8-C 로 분류되지 않는다** — `_classify_ws_subscribe_auth_failure` 는 이 예외를 모르므로
  `None` 을 돌려주고 호출부가 **그대로 재전파**해 연결이 정리된다(= fail-loud). 그게 맞다:
  이건 사용자 인증 실패가 아니라 **lifecycle·프로그래밍 결함**이라, 넓은 availability verdict
  (`temporarily_unavailable`)로 접으면 클라가 영구 재시도하고 운영자는 신호를 못 받는다.
  (한때 이 주석이 "호출자가 §8-C 로 분류하게 둔다"고 적었는데 **코드와 달랐다**.)
- ⚠️ **이것은 큐 상한이 아니다.** `ThreadPoolExecutor` 의 작업 큐는 `SimpleQueue` 라 **무제한**이다.
  격리는 *동거인* 을 지킬 뿐 인증 자신의 적체는 그대로다 — ingress 상한(nginx)과
  **연결 내부 subscribe 남용**은 여전히 **별도 열린 항목**이다. "자원 상한 완료" 라고 쓰지 말 것.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from app import config

logger = logging.getLogger("exchange_rate.auth_executor")

T = TypeVar("T")


class AuthExecutorNotReady(RuntimeError):
    """인증 전용 executor 가 없다(미기동·종료됨). ⛔ 여기서 fallback 하지 않는다."""


class AuthExecutorMetricsBusy(RuntimeError):
    """활성 작업(대기·실행 중)이 있는 동안의 reset 거부. gauge 를 0 으로 밀면 이후 감소가
    음수로 새어 장부가 깨진다(변이 ⑧)."""


class AuthExecutorLane:
    """인증 전용 executor **한 lane**. lane 별 인스턴스를 만들 수 있도록 상태를 캡슐화한다.

    ⚠️ **현재 live instance 는 WS 하나뿐이다** — REST lane 은 S1b 에서 붙는다. 이 슬라이스는
       그 자리를 만들 뿐이고, 두 lane 이 실제로 격리되는지는 아래 두-lane 행동 테스트가 잠근다.

    ⛔ **lane 고유값은 주입한다** — thread prefix·timing event 를 하드코딩하면 REST 기록이
       WS 로그로 섞여 두 폭주를 구분할 수 없다.
    ⛔ `log_timings` 는 **`Callable[[], bool]`** 이다. 생성 시점 bool 로 캡처하면 런타임
       monkeypatch 가 무력화된다. 잠그는 축이 **둘로 나뉜다**:
         · WS facade 경로 — `test_queued_cancellation_is_recorded_and_logged_with_nulls` 와
           `test_queue_heavy_and_execution_heavy_calls_are_not_swapped` 가 구조화 로그를
           **행동으로** 단언한다.
         · generic lane 경로 — `test_log_flag_is_read_at_call_time_per_lane` 이 생성 **뒤**
           flag 를 반전시켜 독립적으로 잠근다(변이 단독 실행에서 kill 실측).
       ⛔ "이 둘만이 잠근다" 식의 필요충분 서술을 쓰지 않는다 — 테스트가 늘면 그 순간 낡는다
          (실측: 위 generic 테스트를 추가하자마자 구 문장이 거짓이 됐다).
       ⛔ 라인 번호로도 적지 않는다 — 테스트를 한 번만 편집해도 낡는다.
    ⚠️ 상태(`_executor`/`_metrics`/`_metrics_lock`/`_configured_workers`)는 **인스턴스** 소유다.
       모듈 alias 를 만들지 말 것 — `_metrics` 는 copy-and-swap 으로 **재바인딩**되므로 alias 는
       첫 swap 직후 stale 객체를 가리킨다.
    """

    def __init__(self, *, thread_prefix: str, timing_event: str,
                 log_timings: Callable[[], bool]) -> None:
        self._thread_prefix = thread_prefix
        self._timing_event = timing_event
        self._log_timings = log_timings

        self._executor: ThreadPoolExecutor | None = None
        self._configured_workers: int = 0

        # ── W canary 계측 ───────────────────────────────────────────────────────────
        # ⛔ **총 wall time 만 보면 W 부족과 Firebase 지연을 구분할 수 없다** — 그게 W 를 정하려는 canary 의
        #    전부인데. 그래서 **큐 대기(queue_wait)** 와 **실행(execution)** 을 나눠 기록한다.
        # ⚠️ 토큰·UID 는 기록하지 않는다.
        self._metrics_lock = threading.Lock()
        self._metrics: dict[str, Any] = {
            "count": 0,
            "queue_wait_ms_sum": 0.0,
            "queue_wait_ms_max": 0.0,
            "execution_ms_sum": 0.0,
            "execution_ms_max": 0.0,
            "never_started": 0,      # 큐에서 취소돼 worker 에 닿지 못한 건수
            # ⛔ **execution 을 0 으로 확정하지 않는다.** wire deadline 이 발화해도 스레드는 계속 도는 것이
            #    이 설계의 핵심이라, 그 순간 execution 을 0 으로 적으면 **Firebase 실행시간을 통째로
            #    과소 측정**한다(실측: caller 취소 뒤에도, worker 실제 종료 뒤에도 0.0 이었다).
            "caller_cancelled_while_running": 0,
            "by_outcome": {},
            # ── S5: 제출/시작 장부 ──────────────────────────────────────────────────
            # ⛔ **`submitted_total` 은 `submit()` 앞에서 올린다.** 뒤로 옮기면 빠른 worker 가 먼저
            #    `started_total` 을 올려 `queued_now` 가 **음수**가 된다(변이 ①로 잠금).
            # ⛔ 그래서 `submit()` 이 던지면 되돌리는 대신 `submit_failed_total` 로 상계한다 — 장부가
            #    맞아야 `queued_now` 가 의미를 갖는다(변이 ②).
            # ⚠️ 형제 계측 `default_executor_probe` 는 `submitted_count`/`started_count` 를 쓴다 —
            #    접미사가 갈리므로 S7 노출에서 두 표면을 나란히 붙일 때 이름을 섞지 말 것.
            "submitted_total": 0,
            "submit_failed_total": 0,
            "started_total": 0,
            "in_flight": 0,            # gauge — worker 진입 +1 / `_record` 와 **같은 finally** 에서 -1 (변이 ③)
            "in_flight_max": 0,
            "queued_observed_max": 0,
            # ⚠️ 제출 이후 계측 실패는 **삼키되 드러낸다**. 이 값이 0 이 아니면 `queued_now`/`count` 는
            #    그만큼 어긋났을 수 있다 — 조용한 유실을 만들지 않기 위한 신호다.
            "ledger_errors_total": 0,
        }




    def _queued_locked(self) -> int:
        """⚠️ 호출자가 `_metrics_lock` 을 쥐고 있어야 한다."""
        return (self._metrics["submitted_total"] - self._metrics["submit_failed_total"]
                - self._metrics["started_total"] - self._metrics["never_started"])


    def auth_executor_metrics(self) -> dict[str, Any]:
        """읽기 전용 스냅샷(진단·테스트). ⚠️ 집계라 분포는 못 준다 — 분포가 필요하면 아래 per-call
        구조화 로그를 canary 기간에만 켠다(`WS_AUTH_EXECUTOR_LOG_TIMINGS`)."""
        with self._metrics_lock:
            snapshot = dict(self._metrics)
            snapshot["by_outcome"] = dict(self._metrics["by_outcome"])
            snapshot["queued_now"] = self._queued_locked()   # 파생 — 저장하지 않는다(두 벌 장부 금지)
        snapshot["max_workers"] = self._configured_workers
        return snapshot


    def reset_auth_executor_metrics(self) -> None:
        """⛔ 활성 작업 중에는 **거부**한다 — 카운터를 0 으로 밀면 아직 돌고 있는 worker 의 `-1` 이
        음수로 새어 `in_flight`/`queued_now` 가 영구히 어긋난다(변이 ⑧).

        ⛔ **장부 오류가 있어도 거부는 유지한다.** 한때 `ledger_errors_total > 0` 이면 queue 를
           무시했는데, start 장부 **직전**에 멈춰 있는 worker(in_flight=0 · queued=1)까지 무시해
           reset 후 그 worker 가 `started_total` 만 올려 **`queued_now = -1`** 이 됐다(codex 재현).
           영구 차단은 불편할 뿐이지만 음수 장부는 핵심 불변식을 깨뜨린다 — 나쁜 거래였다.
        ⛔ **강제 경로를 두지 않는다.** 한때 `force_after_shutdown=True`(조건: `_executor is None`)
           를 뒀는데, `begin_auth_executor_shutdown()` 이 **drain 전에** `_executor=None` 으로 만들고
           실제 drain 은 뒤의 `shutdown(wait=True)` 에서 끝난다 — 운영 lifespan 도 두 단계를 나눠
           쓴다(`main.py`). 그 사이에 강제 reset 하면 멈춰 있던 worker 가 재개해 `started_total` 만
           올려 **`queued_now = -1`** 이 됐다(codex 재현). drain 완료를 증명하는 토큰을 새로 만드는
           대신 **경로를 없앴다** — 이 계측은 in-memory 라 복구는 **프로세스 재시작**이면 충분하고,
           reset 에는 운영 route 가 없다(진단·테스트 전용).
        ⚠️ 정확한 계약은 **"불균형이 남아 있는 동안 열리지 않는다"** 이다 — "장부 오류가 있으면
           열리지 않는다"가 **아니다**. `ledger_errors_total > 0` 이어도 `in_flight == 0` 이고
           `queued_now == 0` 이면 reset 은 허용되고, 새 측정 창을 위해 오류 카운터도 0 으로 내려간다
           (오류가 균형을 깨지 않은 경우가 실제로 있다 — 예: 제출 성공 뒤 부기 실패).
           열리지 않는 것은 오직 **활성·불균형** 상태다."""
        with self._metrics_lock:
            # ⚠️ `!= 0` 이다 — 음수도 거부한다. 지켜지는 것은 **불균형** 신호이지 `ledger_errors_total`
            #    이 아니다(균형이 맞으면 그 카운터는 reset 과 함께 0 으로 내려간다).
            if self._metrics["in_flight"] > 0 or self._queued_locked() != 0:
                raise AuthExecutorMetricsBusy(
                    f"활성 작업 중 reset 거부 (in_flight={self._metrics['in_flight']}, "
                    f"queued={self._queued_locked()}, ledger_errors={self._metrics['ledger_errors_total']})")
            self._reset_locked()


    def _reset_locked(self) -> None:
        """⚠️ 호출자가 `_metrics_lock` 을 쥐고 있어야 한다.

        ⛔ **이 본체는 활성 큐에 안전하지 않다.** 대기 중인 제출이 있는 상태로 카운터를 0 으로 밀면
           그 작업들이 시작될 때 `started_total` 만 올라 `queued_now` 가 **음수**가 된다. 즉 공개
           함수의 가드는 편의가 아니라 **정확성의 조건**이다 — 완화 대비 방어라고 적었던 것은 틀렸다
           (codex 지적). 분리한 이유는 하나뿐: gauge 가 살아 있는 상태에서 `in_flight` 처리를
           단독으로 시험하기 위해서다."""
        new = dict(self._metrics)
        new.update(count=0, queue_wait_ms_sum=0.0, queue_wait_ms_max=0.0,
                   execution_ms_sum=0.0, execution_ms_max=0.0, never_started=0,
                   caller_cancelled_while_running=0,
                   submitted_total=0, submit_failed_total=0, started_total=0,
                   ledger_errors_total=0, by_outcome={})
        # ⚠️ max 는 0 이 아니라 **현재 gauge** 로 내린다 — `max < current` 는 불변식 위반이다.
        #    ⛔ `in_flight` 는 여기서 0 으로 밀지 않는다(살아 있는 worker 의 -1 이 음수로 샌다).
        new["in_flight_max"] = new["in_flight"]
        new["queued_observed_max"] = 0          # 카운터가 0 이 됐으므로 queued 도 0
        self._metrics = new                          # 단일 리바인딩 — 부분 reset 불가


    def _record(self, queue_wait_ms: float | None, execution_ms: float | None, outcome: str,
                *, worker_finished: bool = False) -> None:
        """⚠️ **worker 가 부른다**(또는 큐 취소 시 done callback 이). caller coroutine 이 아니다 —
        아래 `run_in_auth_executor` 주석 참조.

        ⛔ `worker_finished` 의 `in_flight` 감소는 **이 호출 안**에 있다. 별 `finally` 로 떼면
        기록은 남고 gauge 만 새는 경로가 생긴다(변이 ③)."""
        with self._metrics_lock:
            if worker_finished:
                if self._metrics["in_flight"] <= 0:   # ⛔ 음수면 reset 가드(`> 0`)가 영구히 무력해진다
                    raise RuntimeError("in_flight underflow — worker_finished 가 잘못 전달됐다")
                self._metrics["in_flight"] -= 1
            self._metrics["count"] += 1
            self._metrics["by_outcome"][outcome] = self._metrics["by_outcome"].get(outcome, 0) + 1
            if queue_wait_ms is None:
                self._metrics["never_started"] += 1
            else:
                self._metrics["queue_wait_ms_sum"] += queue_wait_ms
                self._metrics["queue_wait_ms_max"] = max(self._metrics["queue_wait_ms_max"], queue_wait_ms)
                if execution_ms is not None:
                    self._metrics["execution_ms_sum"] += execution_ms
                    self._metrics["execution_ms_max"] = max(self._metrics["execution_ms_max"], execution_ms)
        if self._log_timings():
            # ⚠️ canary 기간에만 켠다 — 재연결 폭주에서 subscribe 마다 한 줄이면 로그가 는다.
            # ⛔ `never_started` 도 **로그에 남긴다**(null 로). 한때 여기서 일찍 return 해 집계에만
            #    남고 로그에는 안 나왔는데, 그게 바로 과부하 신호라 canary 에서 못 보면 의미가 없다.
            logger.info(
                self._timing_event,
                extra={
                    "queue_wait_ms": round(queue_wait_ms, 1) if queue_wait_ms is not None else None,
                    "execution_ms": round(execution_ms, 1) if execution_ms is not None else None,
                    "outcome": outcome,
                    "max_workers": self._configured_workers,
                },
            )


    def _note_submitted(self) -> None:
        with self._metrics_lock:
            self._metrics["submitted_total"] += 1


    def _note_submit_failed(self) -> None:
        """`submit()` 이 던졌다 — 상계해서 `queued_now` 를 원위치시킨다.
        ⛔ 여기서 `queued_observed_max` 를 건드리지 않는다(변이 ⑦: 실패는 peak 을 남기지 않는다)."""
        with self._metrics_lock:
            self._metrics["submit_failed_total"] += 1


    def _note_submit_succeeded(self) -> None:
        """제출 성공 지점에서 큐 깊이를 **표본**한다.

        ⚠️ 이름 그대로 *observed* 다 — 상계도 하계도 아니다:
          · **놓친다**: worker 가 이 호출 전에 dequeue 하면 이미 `q=0` 이다.
          · **부풀 수 있다**: 다른 스레드가 `_note_submitted()` 만 마치고 아직 `submit()` 에
            머물러 있으면(그 제출이 나중에 실패해도) 그 예약이 이 표본에 섞인다(codex High).
        ⛔ 그러니 "실패한 제출은 peak 을 올리지 않는다"는 **단일 제출에서만** 참이다. 전역 동시성
          까지 보장하려면 예약과 확정을 분리한 두 벌 장부가 필요한데, 그러면 worker 가 예약보다
          먼저 시작해 `queued_now` 가 음수가 될 수 있다 — 음수 불가 쪽을 택했다."""
        with self._metrics_lock:
            q = self._queued_locked()
            if q > self._metrics["queued_observed_max"]:
                self._metrics["queued_observed_max"] = q


    def _safe_ledger(self, fn: Callable[[], None]) -> bool:
        """제출 **이후**의 장부 기록을 비-치명으로 만든다.

        ⛔ 계측이 서비스 경로를 결정하면 그건 관측이 아니라 정책이다 — worker 가 이미 돌고 있는데
           부기 예외가 caller 에게 반환되면 **계측이 업무 결과를 대체**한다(codex 재현).
        ⚠️ 삼키되 `ledger_errors_total` 로 드러낸다.
        ⛔ 반환값은 **"함수가 예외 없이 반환했는가"** 일 뿐 "커밋됐는가" 가 **아니다** — 한때 그렇게
           적었으나 커밋 후 예외·부분 커밋에서 무너진다(codex 재현). 커밋 여부가 필요한 곳은
           `_note_worker_started(receipt)` 처럼 lock 안에서 채우는 **receipt** 를 쓴다."""
        try:
            fn()
            return True
        except Exception:
            try:
                with self._metrics_lock:
                    self._metrics["ledger_errors_total"] += 1
            except Exception:                       # pragma: no cover - 최후 방어
                pass
            try:                                    # ⛔ 진단 로깅 실패가 업무 결과를 대체하면 안 된다
                logger.debug("auth_executor 장부 기록 실패", exc_info=True)
            except Exception:                       # pragma: no cover - 최후 방어
                pass
            return False


    def _note_worker_started(self, receipt: list[bool]) -> None:  # noqa: C901
        """⚠️ 커밋 사실을 **반환값이 아니라 `receipt` 로** 알린다.

        ⛔ 반환값은 함수가 **끝까지 정상 반환**해야 전달되므로 "커밋한 뒤 예외" 를 구분하지 못한다 —
           그 경우 호출자가 증가를 못 본 채 종료 감소를 생략해 `in_flight` 가 영구 잔존한다
           (codex 재현: in_flight=1 + reset 영구 차단).
        ⚠️ `receipt` 는 lock **안에서** 채워지므로 이후 어떤 예외도 커밋 사실을 지우지 못한다.
        ⚠️ 두 객체(receipt·`_metrics`)를 한 번에 커밋할 방법은 없다 — 리바인딩이 실패하는 창이 남고,
           그때는 `queued_now` 가 어긋난 채 남을 수 있다. 그 사실은 `ledger_errors_total` 로 드러난다.
           ⛔ 한때 reset 가드가 그 신호를 보고 완화하도록 했으나 **철회**했다 — 그 완화가 start 장부
           직전의 worker 를 놓쳐 `queued_now = -1` 을 만들었다. 불균형이 남으면 그 프로세스에서
           reset 은 열리지 않으며, 복구는 프로세스 재시작이다."""
        with self._metrics_lock:
            # ⛔ **부분 커밋 금지.** 순차 in-place 갱신은 중간 실패 시 `started_total`·`in_flight` 만
            #    오르고 receipt 가 안 남아 종료 감소가 생략된다(codex 재현: in_flight=1 영구 잔존).
            #    → 산술은 **사본**에서 끝내고 **단일 update** 로 커밋한다(S1 copy-and-swap 과 같은 규율).
            new = dict(self._metrics)
            new["started_total"] += 1
            new["in_flight"] += 1
            new["in_flight_max"] = max(new["in_flight_max"], new["in_flight"])
            # ⚠️ receipt 는 커밋 **직전**. 이 뒤 update 가 실패하면 카운터는 안 올랐는데 receipt 만
            #    남지만, 그 경우는 `_record` 의 underflow 가드가 무해하게 흡수한다(음수 방지).
            receipt.append(True)
            self._metrics = new              # ⛔ **단일 리바인딩**. `update()` 는 기존 dict 를 제자리
                                        #    갱신해 부분 실패가 가능하다 — copy-and-swap 이 아니다.


    def _record_caller_cancelled_while_running(self) -> None:
        """호출자 deadline 이 발화했지만 **worker 는 계속 돈다** — 이 설계의 핵심 성질이다.
        ⛔ 여기서 execution 을 확정하지 말 것. 실제 값은 worker 가 끝날 때 `_record` 가 남긴다."""
        with self._metrics_lock:
            self._metrics["caller_cancelled_while_running"] += 1


    def start_auth_executor(self, max_workers: int) -> None:
        """lifespan startup 에서 1회. ⚠️ **재진입 가능**해야 한다 — `TestClient` 는 lifespan 을
        여러 번 열고 닫으므로, 종료된 executor 를 재사용하면 두 번째 실행이 통째로 죽는다.
        """
        # ⚠️ 이 검사는 **중복이다** — `ThreadPoolExecutor(max_workers<=0)` 자체가 `ValueError` 를
        #    던진다(변이로 확인: 이 줄만 지워도 테스트가 통과한다). 메시지를 우리 어휘로 주려고
        #    남기지만, **판별력이 있는 가드로 착각하지 말 것**. 진짜 위험은 "양수지만 터무니없는 값"
        #    이고 그건 정책이 없어 코드로 못 막는다 — W 는 측정으로 정해 GO 기록에 남긴다.
        if max_workers <= 0:
            raise ValueError(f"인증 executor worker 수는 양수여야 한다: {max_workers}")
        if self._executor is not None:
            # 이미 살아 있으면 그대로 둔다(중복 startup 방어). 새로 만들면 구 pool 이 누수된다.
            return
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=self._thread_prefix)
        self._configured_workers = max_workers
        logger.info("✅ 인증 전용 executor 기동", extra={"max_workers": max_workers})


    def begin_auth_executor_shutdown(self) -> ThreadPoolExecutor | None:
        """**차단만 하고 기다리지 않는다** — 신규 submit 차단 + 큐 항목 취소.

        ⛔ `cancel_futures=True` — 큐에만 있는 작업은 **실행하지 않고** 취소한다(종료 중에 남은
        인증을 굳이 다 돌릴 이유가 없다). 실행 중인 것은 SDK 의 `httpTimeout` 이 끊는다.
        ⚠️ 반환한 executor 는 **반드시** `await_auth_executor_shutdown` 으로 마무리해야 한다 —
        안 그러면 다음 lifespan 이 구 스레드와 겹친다.
        """
        executor, self._executor = self._executor, None
        if executor is None:
            return None
        executor.shutdown(wait=False, cancel_futures=True)
        return executor


    async def await_auth_executor_shutdown(self, executor: ThreadPoolExecutor | None) -> None:
        """실행 중 worker 종료 대기 — **다른 async drain 이 끝난 뒤** 마지막에 await 한다.

        ⛔ **event loop 에서 동기로 `shutdown(wait=True)` 를 부르지 말 것.** 인증은 per-attempt
        `httpTimeout` **+ SDK 재시도/backoff** 라 총시간이 길 수 있는데, 그동안 loop 가 얼어 뒤따르는
        trigger drain · crawler · scheduler 종료가 **한 줄도 못 돈다**. 배포 중 container grace 가
        끝나면 그 정리가 통째로 날아간다. (한때 shutdown 맨 앞에서 동기로 불렀다 — 그 형태였다.)
        """
        if executor is None:
            return
        await asyncio.to_thread(executor.shutdown, wait=True)
        logger.info("🛑 인증 전용 executor 종료")


    def shutdown_auth_executor(self) -> None:
        """동기 편의형 — **테스트/단발 정리 전용**. 프로덕션 lifespan 은 위 2단계를 쓴다."""
        executor = self.begin_auth_executor_shutdown()
        if executor is None:
            return
        executor.shutdown(wait=True)
        logger.info("🛑 인증 전용 executor 종료")


    def is_auth_executor_running(self) -> bool:
        """관측용(진단 endpoint·테스트)."""
        return self._executor is not None


    async def run_in_auth_executor(self, func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """인증 호출을 **이 pool 에서만** 돌린다.

        ⛔ `loop.run_in_executor` 는 **키워드 인자를 받지 않는다.** 여기서는 `_timed` **클로저**가
        `*args, **kwargs` 를 그대로 나른다. 이 경로가 깨지면 `app=`/`check_revoked=` 가 조용히
        사라지고 — DEFAULT app 이 쓰여 낮춘 `httpTimeout` 이 통째로 no-op 이 되며 revoked 토큰이
        통과한다(둘 다 프레임·로그 어디에도 흔적이 안 남는다). 그래서 전용 테스트로 잠근다.

        ⛔ **계측의 소유자는 caller coroutine 이 아니라 worker 다.** 한때 caller 쪽에서 기록했는데,
        호출자가 deadline 으로 취소되면 그 순간 `finished` 가 없어 **execution 을 0ms 로 확정**하고
        worker 가 실제로 끝난 뒤에도 갱신하지 않았다(실측: 취소 직후 0.0, 종료 후에도 0.0).
        wire deadline 이 발화해도 스레드는 계속 도는 것이 **이 설계의 핵심**이라, 그 경우가 바로
        Firebase 실행시간을 재야 할 자리다 — 거기서 0 을 적으면 canary 전체가 무의미해진다.
        → worker 가 `finally` 에서 **실제** duration 을 남기고, 큐에서 취소돼 worker 에 닿지 못한
          건만 done callback 이 `never_started` 로 남긴다(`future.cancelled()` 로 정확히 갈린다).
        """
        executor = self._executor
        if executor is None:
            raise AuthExecutorNotReady("인증 전용 executor 가 없다")
        self._note_submitted()               # ⛔ submit **앞**(변이 ①) — 그리고 **시계보다도 앞**
        # ⚠️ 시계는 base(`b610e60`) 와 **같은 경계**에서 찍는다 — Event·클로저 생성이 포함되는 지점.
        #    한때 이 줄을 `_note_submitted()` 뒤·Event 생성 **뒤**로 옮겼는데, 그러면 lock 오염은
        #    사라져도 `queue_wait_ms` 의 **의미가 바뀐다**(codex: Event 생성에 50ms 주입 시
        #    base 110.24ms vs 이동 후 0.16ms). 장부만 시계 밖으로 빼고 경계는 보존한다.
        # ⛔ **`future.cancelled()` 로 "실행 중"을 판정하지 말 것.** 그건 *시작 전 취소* 에만 참이라,
        #    worker 가 **이미 정상 완료**했는데 event loop 가 결과 전달을 아직 못 한 창에서도
        #    `not cancelled()` 가 참이 되어 완료된 건을 "실행 중 취소"로 **오분류**한다(재현 완료).
        #    하필 부하가 클 때 — 즉 W 를 재려는 바로 그 상황에서 — 전달이 늦어 자주 생긴다.
        #    → worker 가 직접 올리는 두 이벤트로 판정한다.
        # ⛔ **예약 이후의 모든 실패**가 보상 범위다. 한때 `try` 가 `executor.submit()` 만 감쌌는데,
        #    `threading.Event()` 생성이 실패하면 `submitted_total=1 / submit_failed_total=0` 이 남아
        #    `queued_now=1` 이 **영구 잔존**하고 reset 도 영구 차단됐다(codex 재현).
        try:
            # ⚠️ 시계도 **보상 범위 안**이다 — 밖에 두면 clock 실패가 상계되지 않아 queued_now 가
            #    영구 잔존한다(codex 재현). 경계는 그대로: Event·클로저 생성 **앞**.
            submitted = time.monotonic()
            started_evt = threading.Event()
            finished_evt = threading.Event()

            def _timed() -> T:
                # ⛔ **base 순서를 바꾸지 말 것.** 한때 `_note_worker_started()` 를 여기 맨 앞에 뒀는데,
                #    그 lock 대기가 `queue_wait_ms` 에 섞이고(아래 `started` 가 lock 뒤라서) 그 창에서
                #    caller 가 취소되면 `started_evt` 가 아직 false 라 `caller_cancelled_while_running`
                #    까지 누락됐다 — 기존 7필드 "값·의미 불변" 위반이다(codex High).
                # ⚠️ 장부를 뒤에 둬도 불변식은 안전하다: 그 창에서는 started 가 **덜** 세어져
                #    `queued_now` 가 과대평가될 뿐 음수가 되지 않는다.
                dequeued = time.monotonic()     # 큐를 빠져나온 시각 = queue_wait 의 **끝**
                started_evt.set()
                start_receipt: list[bool] = []
                self._safe_ledger(lambda: self._note_worker_started(start_receipt))   # 실패해도 업무는 계속
                # ⚠️ 커밋 **여부**를 본다 — 함수가 정상 반환했는지가 아니다
                started_ok = bool(start_receipt)
                queue_wait_ms = (dequeued - submitted) * 1000.0
                # ⚠️ 실행 시각은 장부 lock **뒤**에 다시 찍는다 — dequeue 시각을 그대로 쓰면
                #    `_note_worker_started()` 의 lock 대기가 `execution_ms` 에 섞인다
                #    (codex 재현: 80ms 주입 → execution 82.07ms). 두 시계는 분리돼야 한다.
                exec_started = time.monotonic()
                outcome = "ok"
                try:
                    return func(*args, **kwargs)
                except BaseException as exc:
                    outcome = type(exc).__name__
                    raise
                finally:
                    # ⚠️ `worker_finished` 는 **실제로 올렸을 때만** True — 안 그러면 underflow 다
                    self._safe_ledger(lambda: self._record(queue_wait_ms,
                                                 (time.monotonic() - exec_started) * 1000.0,
                                                 outcome, worker_finished=started_ok))
                    finished_evt.set()          # ⚠️ 기록 **뒤에** 올린다 — "끝났다"가 "기록됐다"를 함의하게

            future = executor.submit(_timed)
        except BaseException:
            self._note_submit_failed()       # 장부 상계 후 그대로 재전파 (변이 ②)
            raise
        # 제출 성공 — 여기부터 계측 실패는 업무 결과를 대체하지 않는다
        self._safe_ledger(self._note_submit_succeeded)

        def _on_done(done: "Future[T]") -> None:
            # ⚠️ `cancelled()` 는 **시작 전에 취소된 경우만** 참이다 — 실행 중 취소는 concurrent
            #    future 를 취소하지 못하므로 여기서 정확히 갈린다(worker 기록과 중복되지 않는다).
            if done.cancelled():
                self._record(None, None, "never_started")

        future.add_done_callback(_on_done)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            # 시작 전 취소는 done callback 이 `never_started` 로 잡고, **이미 끝난** 건은 아무것도
            # 올리지 않는다(worker 가 제 값을 이미 남겼다). 남는 것이 진짜 "실행 중 취소"다.
            if started_evt.is_set() and not finished_evt.is_set():
                self._record_caller_cancelled_while_running()
            raise



# ── WS singleton + facade ──────────────────────────────────────────────────
# ⛔ **프로덕션 공개 함수만** 위임한다. 내부 helper(`_record`/`_note_*`/`_reset_locked`)에는
#    facade 를 만들지 않는다 — 모듈 심볼을 patch 해도 lane 메서드에 닿지 않아 fault injection 이
#    **공허**해지고, `_metrics` alias 는 copy-and-swap 재바인딩 때문에 즉시 stale 이 된다.
#    white-box 테스트는 `_ws_lane` 을 직접 patch 한다.
_ws_lane = AuthExecutorLane(
    thread_prefix="ws-auth",
    timing_event="ws_auth_timing",
    # ⚠️ 호출 시점 조회 — 생성 시 bool 캡처 금지(위 클래스 docstring 참조).
    log_timings=lambda: config.WS_AUTH_EXECUTOR_LOG_TIMINGS,
)


def auth_executor_metrics() -> dict[str, Any]:
    return _ws_lane.auth_executor_metrics()


def reset_auth_executor_metrics() -> None:
    _ws_lane.reset_auth_executor_metrics()


def start_auth_executor(max_workers: int) -> None:
    _ws_lane.start_auth_executor(max_workers)


def begin_auth_executor_shutdown() -> ThreadPoolExecutor | None:
    return _ws_lane.begin_auth_executor_shutdown()


async def await_auth_executor_shutdown(executor: ThreadPoolExecutor | None) -> None:
    await _ws_lane.await_auth_executor_shutdown(executor)


def shutdown_auth_executor() -> None:
    _ws_lane.shutdown_auth_executor()


def is_auth_executor_running() -> bool:
    return _ws_lane.is_auth_executor_running()


@asynccontextmanager
async def ws_auth_lane_startup_scope(max_workers: int):
    """S1a′ — **이번 startup 시도가 새로 만든** WS auth lane 에 한해 조건부 보상 종료.

    범위는 의미로 정의한다(라인 번호가 아니다): lane 생성부터 **마지막 startup 단계 완료
    직전**까지. FastAPI lifespan 의 `yield` 는 이 scope **밖**이다 — 안에 넣으면 정상 운영·
    shutdown 예외가 startup 실패로 오분류된다.

    ⛔ **lifespan 전체를 트랜잭션화하지 않는다.** Firebase·Redis·default-executor probe·
       scheduler·collector 의 startup side effect 는 되돌리지 않는다. 되돌리는 것은 이
       executor 하나뿐이다.
    ⛔ `created` 는 **startup 실패 보상 책임**일 뿐 일반 소유권이 아니다 — 정상 lifespan
       shutdown 은 여전히 lane 을 무조건 종료한다(`main.py`).
    ⚠️ **직렬화 전제**: auth executor lifecycle 호출은 단일 lifespan task 에서 직렬화된다.
       `is_auth_executor_running()` 과 `start_auth_executor()` 사이에 `await` 가 없어 같은
       task 끼리는 끼어들 수 없다. 별 thread·겹친 lifespan 이 동시에 부르면 check-and-start
       가 원자적이지 않으므로, 그 지원이 필요해지면 lane 내부에서 생성 토큰을 반환해야 한다.
    ⛔ `start_auth_executor` 호출도 **try 안**이다 — 생성 뒤 raise 하는 부분 성공에서
       rollback 이 안 돌면 pool 이 그대로 샌다(codex Blocker).
    ⛔ `BaseException` 이다 — **취소도 rollback 대상**이다.
    """
    created = not is_auth_executor_running()
    try:
        start_auth_executor(max_workers)
        yield
    except BaseException:
        if created:
            # ⛔ **rollback 실패가 startup 원인을 덮으면 안 된다.** 여기서 예외가 나가면 원래
            #    startup 예외가 사라져 운영자가 진짜 원인을 못 본다. 삼키되 드러낸다.
            try:
                executor = begin_auth_executor_shutdown()
                # ⚠️ `begin` 만으로는 `running` 이 false 가 되지만 worker 는 계속 돈다 — 합류까지 한다.
                await await_auth_executor_shutdown(executor)
            except BaseException:      # noqa: BLE001 — 원인 보존이 우선이다
                # ⛔ **진단 로깅도 보호한다.** `logger.exception` 이 던지면 아래 `raise` 에
                #    도달하지 못해 **로깅 예외가 원래 startup 원인을 덮는다**(codex).
                try:
                    logger.exception("auth lane rollback 실패 — startup 원인을 그대로 올린다")
                except BaseException:  # pragma: no cover - 최후 방어
                    pass
        raise


async def run_in_auth_executor(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    return await _ws_lane.run_in_auth_executor(func, *args, **kwargs)
