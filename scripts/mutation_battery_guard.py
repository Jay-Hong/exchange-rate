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
import shutil
import subprocess
import tempfile
from typing import Iterator


class BatteryLockBusy(RuntimeError):
    """다른 변이 배터리가 이 worktree 를 잡고 있다."""


class NotIsolated(RuntimeError):
    """공유 worktree 의 production 파일을 변이하려 했다."""


#: 격리 worktree 표식. 이 파일이 조상 경로에 있으면 그 트리는 변이 전용이다.
ISOLATION_MARKER = ".fxi-mutation-isolated"


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

    현재 저장소의 production mutation runner 8개가 모두 이 락과 `isolated_worktree()`,
    `MutatedFile`을 함께 쓴다. 새 runner가 이 셋 중 하나를 빠뜨리면 영구 완결성 테스트가 거부한다.

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
        self.original_bytes = path.read_bytes()
        self.original = self.original_bytes.decode()
        self.original_sha = hashlib.sha256(self.original_bytes).hexdigest()
        self._last_written = self.original_bytes

    def write_mutant(self, text: str) -> None:
        """격리 트리인지 확인한 뒤, 디스크가 아직 내 것일 때만 변이본을 쓴다."""
        assert_isolated(self.path)
        self._assert_ours("변이 적용 전")
        encoded = text.encode()
        self.path.write_bytes(encoded)
        self._last_written = encoded

    def restore(self) -> None:
        """⛔ 무조건 덮어쓰지 않는다 — 남의 변경을 지우는 것이 이 사고의 핵심이었다."""
        assert_isolated(self.path)
        self._assert_ours("복원 전")
        self.path.write_bytes(self.original_bytes)
        self._last_written = self.original_bytes
        got = hashlib.sha256(self.path.read_bytes()).hexdigest()
        if got != self.original_sha:
            raise MutationConflict(f"복원 후 내용이 원본과 다르다: {self.path}")

    def _assert_ours(self, when: str) -> None:
        on_disk = self.path.read_bytes()
        if on_disk != self._last_written:
            raise MutationConflict(
                f"{when}: {self.path} 를 다른 프로세스가 바꿨다. "
                "덮어쓰면 그 변경이 사라진다 — 수동으로 확인할 것.\n"
                f"  기대 sha={hashlib.sha256(self._last_written).hexdigest()[:12]} "
                f"실제 sha={hashlib.sha256(on_disk).hexdigest()[:12]}"
            )


@contextlib.contextmanager
def isolated_worktree(repo: pathlib.Path) -> Iterator[pathlib.Path]:
    """변이 전용 **임시 git worktree** 를 만들고 그 안에서만 production 파일을 만지게 한다.

    ## 왜 락으로 부족한가

    `battery_lock()` 은 **배터리끼리만** 직렬화한다. 일반 `pytest`·`git`·`docker build` 같은
    **독자**는 락을 모르고, 변이본이 디스크에 있는 순간을 그대로 읽는다. 오늘 실제로 발화했다 —
    적대적 리뷰 에이전트가 내 배터리가 심어 둔 S2-6/S2-4 변이본을 읽고 "설명되지 않는 red 3건"
    을 보고했다.

    ⛔ **"배터리 도는지 확인 후 거절" 은 답이 아니다.** 확인과 실행 사이에 배터리가 시작되는
       TOCTOU 창이 남는다(codex). 격리는 그 창 자체를 없앤다 — 독자가 보는 트리에 변이본이
       **애초에 존재하지 않는다**.

    ## 미커밋 상태를 재현한다

    배터리는 보통 **아직 커밋하지 않은** 변경을 시험한다. 그래서 `HEAD` 로 worktree 를 만든 뒤
    `git diff HEAD` 를 적용하고 untracked 파일을 복사해 현재 작업 상태를 그대로 옮긴다.
    그러지 않으면 배터리가 **다른 코드**를 시험하면서 초록을 보고한다.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="fxi-mutation-"))
    wt = tmp / "wt"
    try:
        subprocess.run(["git", "worktree", "add", "--detach", "-q", str(wt), "HEAD"],
                       cwd=repo, check=True, capture_output=True)
        diff = subprocess.run(["git", "diff", "HEAD"], cwd=repo,
                              capture_output=True, text=True, check=True).stdout
        if diff.strip():
            subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=wt,
                           input=diff, text=True, check=True, capture_output=True)
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                                   cwd=repo, capture_output=True, text=True,
                                   check=True).stdout.split()
        for rel in untracked:
            src, dst = repo / rel, wt / rel
            if not src.is_file():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        (wt / ISOLATION_MARKER).write_text("mutation battery isolated worktree\n")
        yield wt
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                       cwd=repo, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def assert_isolated(path: pathlib.Path) -> None:
    """변이 대상이 격리 worktree 안인지 확인한다.

    세 production runner 모두 `isolated_worktree()` 안의 파일만 `MutatedFile`에 넘긴다.
    이 판정은 `write_mutant()`의 공용 경계에 걸려 있어 새 runner가 격리를 빠뜨려도 쓰기 전에
    거부한다. 락만으로는 일반 pytest·git·docker build 독자를 보호할 수 없다.
    """
    p = pathlib.Path(path).resolve()
    for parent in (p, *p.parents):
        if (parent / ISOLATION_MARKER).is_file():
            return
    raise NotIsolated(
        f"공유 worktree 의 파일을 변이하려 했다: {p}\n"
        "  변이는 isolated_worktree() 안에서만 한다 — 락은 일반 pytest 독자를 못 막는다."
    )
