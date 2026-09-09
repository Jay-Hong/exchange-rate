"""IBK 전용 경보 전달 — 접수(비차단)와 발송(별도 소비자 스레드)을 분리한다.

부모(`IbkParentRunner._notify`)는 **큐 접수가 성공하면 같은 문제의 반복 알림을 억제한다.**
그래서 "접수는 됐는데 실제 발송이 실패한" 회차는 부모가 다시 알려주지 않는다 — 유한 재시도와
최종 실패 기록은 이 모듈의 책임이다.

⛔ 경보 실패는 크롤러도 Chrome 도 다시 실행시키지 않는다. 이 모듈이 주입받는 것은 발송 콜러블
   하나뿐이라 그럴 수단 자체가 없다. 실패는 계수와 로그로만 남는다.
⛔ `sent` 는 **발송 콜러블이 성공을 보고했다**는 뜻뿐이다. 사용자가 그 알림을 봤다는 증거가
   아니다. 전달 계층은 열람을 관측할 수 없다.
⛔ 배선은 `start_ibk_result_path()` **한 곳**이 소유한다(`IBK_RESULT_PATH_ENABLED` 게이트 뒤).
   참조가 퍼지면 누가 `close()` 를 부르는지, 헬스체크 재시작이 같은 객체를 쓰는지가
   코드에서 안 보인다. 게이트가 꺼져 있으면 이 객체도 소비자 스레드도 만들지 않는다.
⛔ 이 모듈은 프로세스를 제어하지 않는다 — 그것을 지키는 시험이 **원문 문자열**로 훑으므로
   여기 산문에 소유자 모듈 이름을 적으면 가드가 산문에 걸린다(실측). 함수 이름으로 가리킨다.
⛔ 예외의 문자열·traceback 은 자격증명이 든 요청 URL 을 담을 수 있으므로 종류(type name)만 남긴다.
"""

import logging
import math
import queue
import threading
from collections import deque

logger = logging.getLogger("exchange_rate.ibk.notice_delivery")

DEFAULT_QUEUE_SIZE = 32
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_SECONDS = 5.0
DEFAULT_POLL_SECONDS = 0.2
DEFAULT_SHUTDOWN_TIMEOUT = 5.0
FINAL_FAILURE_HISTORY = 20

COUNTERS = (
    "accepted",              # 큐 접수 (발송 성공 아님)
    "rejected_inactive",     # 전달 비활성 — 부모의 반복 억제를 유발하지 않는다
    "rejected_saturated",    # 큐 포화
    "rejected_not_running",  # 소비자 미기동/종료됨
    "send_attempts",
    "sent",                  # 발송 콜러블이 성공 보고 (사용자 열람 아님)
    "send_failed",           # 개별 시도 실패
    "send_failed_final",     # 유한 재시도 소진 — 억제 이음매의 최종 실패
    "abandoned_at_shutdown", # 재시도 대기 중 종료 신호로 포기
    "dropped_at_shutdown",   # 종료 시 큐에 남아 발송하지 못함
)


def format_notice(notice) -> str:
    """경보 본문. 자격증명이나 원시 예외 문자열을 담지 않는다."""
    title = "✅ IBK 크롤러 복구" if notice.kind == "recovered" else "🚨 IBK 크롤러 경보"
    lines = [title, f"종류: {notice.kind}", f"run_id: {notice.run_id}",
             f"분류: {notice.classification}"]
    if notice.reason:
        lines.append(f"사유: {notice.reason}")
    block = notice.cleanup_block
    if block is not None:
        lines.append(f"정리 보류: {block.reason} (run_id={block.run_id}, since={block.since})")
    return "\n".join(lines)


class IbkNoticeDelivery:
    """유한 큐 접수 + 별도 소비자 스레드 발송.

    `event_sink` 는 부모의 비차단 포트다. 반환 True 는 **접수**만 뜻한다.
    비활성이거나 큐가 포화면 False 를 돌려 부모가 억제 상태로 넘어가지 않게 한다.
    """

    def __init__(self, *, send, enabled=True, queue_size=DEFAULT_QUEUE_SIZE,
                 max_attempts=DEFAULT_MAX_ATTEMPTS, retry_seconds=DEFAULT_RETRY_SECONDS,
                 poll_seconds=DEFAULT_POLL_SECONDS, formatter=format_notice):
        if not callable(send) or not callable(formatter):
            raise ValueError("INVALID_IBK_NOTICE_DEPENDENCY")
        if type(enabled) is not bool:
            raise ValueError("INVALID_IBK_NOTICE_ENABLED")
        if type(queue_size) is not int or queue_size < 1:
            raise ValueError("INVALID_IBK_NOTICE_QUEUE_SIZE")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("INVALID_IBK_NOTICE_MAX_ATTEMPTS")
        for name, value in (("RETRY", retry_seconds), ("POLL", poll_seconds)):
            # NaN 은 `value < 0` 을 통과하고 inf 는 poll 을 영원히 멈춰 종료를 못 끝낸다.
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"INVALID_IBK_NOTICE_{name}_SECONDS")
        self.enabled = enabled
        self._send, self._formatter = send, formatter
        self._max_attempts, self._retry_seconds = max_attempts, retry_seconds
        self._poll_seconds = poll_seconds
        self._queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._worker = None
        self._counts = dict.fromkeys(COUNTERS, 0)
        self._unsent = deque(maxlen=FINAL_FAILURE_HISTORY)

    # ---- 접수 (부모 스레드에서 호출, 절대 차단하지 않는다) ----

    def event_sink(self, notice) -> bool:
        if not self.enabled:
            self._bump("rejected_inactive")
            return False
        # 실행 여부 확인·enqueue·계수를 **종료 전환과 같은 임계구역**에서 직렬화한다.
        # 그러지 않으면 확인을 통과한 뒤 close() 가 드레인까지 완주해, 소비자가 없는 큐에
        # 넣고 True 를 돌려준다 — 부모는 억제 상태로 넘어가고 그 회차는 흔적 없이 사라진다.
        with self._lock:
            if not self.is_running():
                self._bump("rejected_not_running")
                return False
            try:
                self._queue.put_nowait(notice)
            except queue.Full:
                self._bump("rejected_saturated")
                return False
            self._bump("accepted")
            return True

    # ---- 수명주기 ----

    def start(self):
        with self._lock:
            if self._worker is not None:
                raise RuntimeError("IBK_NOTICE_DELIVERY_ALREADY_STARTED")
            self._stop.clear()
            self._worker = threading.Thread(target=self._run, name="ibk-notice-delivery",
                                            daemon=True)
            self._worker.start()
        return self

    def is_running(self) -> bool:
        worker = self._worker
        return worker is not None and worker.is_alive() and not self._stop.is_set()

    def close(self, timeout=DEFAULT_SHUTDOWN_TIMEOUT) -> bool:
        """유한 대기 종료. 반환값은 소비자가 시간 안에 끝났는지 여부다.

        종료 신호는 재시도 대기를 즉시 깨우므로 남는 작업은 **진행 중인 발송 1건**뿐이다.
        시간 안에 끝나지 않아도 daemon 스레드라 프로세스를 붙잡지 않는다.
        """
        with self._lock:
            worker = self._worker
            self._stop.set()  # 접수와 같은 락 안에서 전환한다
        if worker is None:
            return True
        # join 은 반드시 락 밖에서 — 소비자도 계수하려면 같은 락이 필요하다.
        worker.join(timeout)
        return not worker.is_alive()

    # ---- 소비자 ----

    def _run(self):
        while not self._stop.is_set():
            try:
                notice = self._queue.get(timeout=self._poll_seconds)
            except queue.Empty:
                continue
            try:
                self._deliver(notice)
            except Exception as exc:  # 소비자는 어떤 경우에도 죽지 않는다
                self._bump("send_failed_final")
                self._record_unsent(notice, 0, f"consumer_error:{type(exc).__name__}")
                logger.error("IBK_NOTICE_CONSUMER_ERROR",
                             extra={"bank": "ibk", "error_type": type(exc).__name__})
            finally:
                self._queue.task_done()
        self._drain_remaining()

    def _deliver(self, notice):
        text = self._formatter(notice)
        for attempt in range(1, self._max_attempts + 1):
            self._bump("send_attempts")
            try:
                ok = self._send(text) is True
            except Exception as exc:
                ok = False
                logger.warning("IBK_NOTICE_SEND_ERROR",
                               extra={"bank": "ibk", "run_id": notice.run_id,
                                      "attempt": attempt, "error_type": type(exc).__name__})
            if ok:
                self._bump("sent")
                return
            self._bump("send_failed")
            # 시도 횟수의 상한은 위 range 하나뿐이다(중복 상한을 두면 한쪽이 죽은 코드가 된다).
            # 여기 조건은 "마지막 시도 뒤에는 기다리지 않는다"만 뜻한다.
            if attempt < self._max_attempts and self._stop.wait(self._retry_seconds):
                self._bump("abandoned_at_shutdown")
                self._record_unsent(notice, attempt, "abandoned_at_shutdown")
                logger.error("IBK_NOTICE_ABANDONED_AT_SHUTDOWN",
                             extra={"bank": "ibk", "run_id": notice.run_id, "attempts": attempt})
                return
        self._bump("send_failed_final")
        self._record_unsent(notice, self._max_attempts, "send_failed_final")
        # 부모는 접수 성공으로 이미 억제 상태다 — 이 줄이 그 회차의 유일한 최종 기록이다.
        logger.error("IBK_NOTICE_SEND_FAILED_FINAL",
                     extra={"bank": "ibk", "run_id": notice.run_id,
                            "classification": notice.classification,
                            "attempts": self._max_attempts})

    def _drain_remaining(self):
        while True:
            try:
                notice = self._queue.get_nowait()
            except queue.Empty:
                return
            self._bump("dropped_at_shutdown")
            self._record_unsent(notice, 0, "dropped_at_shutdown")
            self._queue.task_done()

    # ---- 계측 ----

    def _bump(self, name):
        with self._lock:
            self._counts[name] += 1

    def _record_unsent(self, notice, attempts, outcome):
        with self._lock:
            self._unsent.append({"run_id": notice.run_id, "kind": notice.kind,
                                 "classification": notice.classification,
                                 "attempts": attempts, "outcome": outcome})

    def stats(self) -> dict:
        with self._lock:
            snapshot = dict(self._counts)
        snapshot["queued"] = self._queue.qsize()
        snapshot["running"] = self.is_running()
        # ⛔ `running` 만으로는 "꺼 놨다" 와 "떠 있어야 하는데 죽었다" 가 구분되지 않는다.
        snapshot["enabled"] = self.enabled
        return snapshot

    def unsent(self) -> tuple:
        """발송하지 못한 회차 기록(최근 것부터 유한 개). 부모는 이미 억제 상태라 여기만 남는다."""
        with self._lock:
            return tuple(self._unsent)


def build_default_delivery(**kwargs) -> IbkNoticeDelivery:
    """운영용 조립기 — `start_ibk_result_path()` 가 게이트 뒤에서 부른다.

    Telegram 설정을 여기서 읽어 클래스가 env 에 의존하지 않게 한다.
    """
    # 조건부 의존성: 테스트는 이 조립기를 쓰지 않으므로 notifications 를 임포트하지 않는다.
    from app.notifications.telegram import telegram_handler

    def send_plain(text: str) -> bool:
        # ⛔ Markdown 으로 보내면 안 된다. 본문에는 IbkReason(`SELENIUM_STRICT_REJECTED` 등)과
        #    `run_id:` 라벨 때문에 밑줄이 홀수 개 들어가고, 닫히지 않은 서식은 Telegram 에서
        #    파싱 오류가 된다. 같은 본문을 재시도해도 계속 실패하므로 경보가 영영 못 나간다.
        return telegram_handler.send_message(text, parse_mode="")

    kwargs.setdefault("enabled", telegram_handler.enabled)
    return IbkNoticeDelivery(send=send_plain, **kwargs)
