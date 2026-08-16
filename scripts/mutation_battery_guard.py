#!/usr/bin/env python3
"""변이 배터리 공용 안전장치 — **커널 락**과 **충돌 감지 복원**.

## 왜 있는가 (실제 사고)

감사 workflow 의 병렬 에이전트들에게 "변이를 적용해 pytest 로 확인하고 원복하라" 고 지시했고,
그것들이 **같은 worktree** 에서 동시에 production 파일을 쓰고 읽었다. 결과:

- 무관한 테스트 25건이 red 가 되어 원인 추적에 오래 걸렸다
- 동시 쓰기로 **복원이 덮여** `init_rest_auth_app()` 이 `pass` 로 남았다

⛔ 근본 기전은 **작업 격리**다(감사 workflow 는 `isolation: "worktree"` 로 띄운다). 이 모듈은
   그 위의 **방어층**이다 — 방어층을 근본 대책이라고 부르지 않는다.

## 두 장치

### 1. `battery_lock()` — 커널 flock

`ps` 로 다른 pytest 를 찾는 방식은 셋 다 틀렸다: `exists()` 후 `write()` 는 **원자적이지 않고**,
확인 직후 새 프로세스가 뜰 수 있으며, `ps` 는 cwd 를 몰라 **다른 worktree 의 안전한 실행까지**
거부한다. 소유권은 커널이 판정해야 한다(`scripts/env_operation_lock.py` 의 선례).

락 파일은 `git rev-parse --git-path` 로 **worktree 별 git 디렉터리**에 둔다 — working tree 를
더럽히지 않고, `.gitignore` 도 필요 없으며, 격리된 worktree 끼리는 **병렬 실행이 가능**하다.
⛔ 락 파일을 커밋하면 안 된다: 존재는 활성 소유권의 증거가 아니다(stale 이 정상처럼 보인다).

### 2. `MutatedFile` — 충돌 감지 복원

"복원 후 sha == 내 스냅샷" 은 **충돌 부재를 증명하지 못한다.** 남이 그 사이 정상 변경을 했어도
내가 스냅샷으로 덮어쓰면 sha 는 일치한다 — 위 사고에서 실제로 남의 편집이 그렇게 사라졌다.
그래서 **복원 직전에 디스크 내용이 "내가 마지막으로 쓴 그 변이본" 인지** 확인한다. 다르면
**덮어쓰지 않고 소리 내어 멈춘다**.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import pathlib
import subprocess
from typing import Iterator


class BatteryLockBusy(RuntimeError):
    """다른 변이 배터리가 이 worktree 를 잡고 있다."""


class MutationConflict(RuntimeError):
    """복원 대상 파일을 **다른 프로세스가 바꿨다**. 덮어쓰지 않고 멈춘다."""


def lock_path(repo: pathlib.Path) -> pathlib.Path:
    """worktree 별 git 디렉터리 안의 락 경로. working tree 를 더럽히지 않는다."""
    out = subprocess.run(["git", "rev-parse", "--git-path", "mutation-battery.lock"],
                         cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    return (repo / out).resolve() if not os.path.isabs(out) else pathlib.Path(out)


@contextlib.contextmanager
def battery_lock(repo: pathlib.Path) -> Iterator[pathlib.Path]:
    """이 worktree 에서 **이 가드를 채택한** 배터리들이 공유하는 배타 락.

    ⚠️ "모든 배터리" 가 아니다 — 현재 `mutation_c3_ios_pin_gate` · `mutation_s0_queue_wait` ·
       `mutation_s6_arrival` · `mutation_s7_exposure` · `mutation_subscribe_load_metrics` 5개는
       아직 직접 쓰기 방식이라 이 락을 우회한다. 이관은 별도 hardening 슬라이스다 —
       복원 구조(다중 파일·bytes)가 서로 달라 한 슬라이스에 끌어들이면 범위가 과도해진다.

    ⛔ 비차단(`LOCK_NB`)이다 — 기다리지 않고 즉시 거절한다. 배터리는 몇 분씩 도는데 대기하면
       두 번째 실행이 조용히 줄을 서다가 첫 실행이 끝난 뒤 시작해 원인 추적을 흐린다.
    """
    path = lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BatteryLockBusy(
                f"다른 변이 배터리가 실행 중이다 ({path}). 동시 변이는 서로의 복원을 덮는다."
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode())
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class MutatedFile:
    """변이 대상 파일 하나의 write/restore 를 **충돌 감지와 함께** 소유한다."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.original = path.read_text()
        self.original_sha = hashlib.sha256(self.original.encode()).hexdigest()
        self._last_written = self.original

    def write_mutant(self, text: str) -> None:
        """변이본을 쓰기 **전에** 디스크가 아직 내 것인지 확인한다."""
        self._assert_ours("변이 적용 전")
        self.path.write_text(text)
        self._last_written = text

    def restore(self) -> None:
        """⛔ 무조건 덮어쓰지 않는다 — 남의 변경을 지우는 것이 이 사고의 핵심이었다."""
        self._assert_ours("복원 전")
        self.path.write_text(self.original)
        self._last_written = self.original
        got = hashlib.sha256(self.path.read_text().encode()).hexdigest()
        if got != self.original_sha:
            raise MutationConflict(f"복원 후 내용이 원본과 다르다: {self.path}")

    def _assert_ours(self, when: str) -> None:
        on_disk = self.path.read_text()
        if on_disk != self._last_written:
            raise MutationConflict(
                f"{when}: {self.path} 를 다른 프로세스가 바꿨다. "
                "덮어쓰면 그 변경이 사라진다 — 수동으로 확인할 것.\n"
                f"  기대 sha={hashlib.sha256(self._last_written.encode()).hexdigest()[:12]} "
                f"실제 sha={hashlib.sha256(on_disk.encode()).hexdigest()[:12]}"
            )
