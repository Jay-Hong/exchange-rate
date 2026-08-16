"""S1a′ 배선 — **프로덕션 lifespan 이 실제로 scope 안에서 도는지**.

⛔ lane 단위 테스트가 전부 통과해도, `main.py` 에서 일부 호출만 scope 안에 들어가면
   late startup 실패가 그대로 샌다. 그래서 **첫 post-start 지점**과 **마지막 pre-yield
   지점**에 각각 실패를 주입한다 — 둘 다 예외와 실제 취소로.
"""
import ast
import asyncio
import inspect
import pathlib

import pytest

from app import auth_executor, main as app_main


def _lifespan_source() -> str:
    import textwrap
    return textwrap.dedent(inspect.getsource(app_main.lifespan.__wrapped__
                                             if hasattr(app_main.lifespan, "__wrapped__")
                                             else app_main.lifespan))


def test_scope_covers_from_lane_start_to_the_last_startup_step():
    """⛔ 범위를 **의미로** 잠근다 — 라인 번호는 한 번만 편집해도 낡는다.
    첫 post-start 호출과 마지막 startup 호출이 **같은 `async with` 블록 안**에 있어야 한다."""
    tree = ast.parse(_lifespan_source())
    scopes = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncWith)
              and "ws_auth_lane_startup_scope" in ast.unparse(n.items[0].context_expr)]
    assert len(scopes) == 1, f"startup scope 가 정확히 하나여야 한다: {len(scopes)}"
    body = ast.unparse(scopes[0])
    for must in ("start_default_executor_probe", "start_scheduler",
                 "start_krx_futures_client", "start_usdt_ws_gopax_client"):
        assert must in body, f"{must} 가 scope 밖이다 — 그 뒤 실패는 rollback 되지 않는다"
    # ⛔ yield 는 scope **밖**이어야 한다 — 안에 있으면 정상 운영·shutdown 예외가
    #    startup 실패로 오분류된다.
    assert not any(isinstance(n, ast.Expr) and isinstance(n.value, ast.Yield)
                   for n in ast.walk(scopes[0])), "yield 가 scope 안에 있다"


SUBPROC = r"""
import asyncio, os, pathlib, sys
sys.path.insert(0, os.environ["FXI_REPO"])
# ⛔ conftest 가 안 도는 프로세스다 — firebase / DB 스텁을 **import 전에** 직접 세운다.
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.environ["FXI_TMPDB"])
import tests.conftest  # noqa: F401,E402  (스텁 설치)
from app import auth_executor, main as app_main

target, kind = sys.argv[1], sys.argv[2]
owner = app_main.default_executor_probe if target.startswith("start_default") else app_main.scheduler
hit = None
hits = 0

def boom(*a, **k):
    global hits
    hits += 1
    hit.set()
    if kind == "error":
        raise RuntimeError("주입")

async def aboom(*a, **k):
    global hits
    hits += 1
    hit.set()
    if kind == "error":
        raise RuntimeError("주입")
    await asyncio.sleep(30)

setattr(owner, target, boom if target.startswith("start_default") else aboom)

async def scenario():
    global hit
    hit = asyncio.Event()

    async def run_lifespan():
        async with app_main.lifespan(app_main.app):
            pass

    task = asyncio.create_task(run_lifespan())
    if kind == "cancel":
        # 첫 경계는 sync 함수라 hit 뒤 다음 await 에서 취소되고, 마지막 경계는 aboom 안에서
        # 대기한다. 어느 쪽이든 수동 raise 가 아니라 실제 Task.cancel() 경로다.
        hit_waiter = asyncio.create_task(hit.wait())
        done, _ = await asyncio.wait(
            (task, hit_waiter), timeout=120, return_when=asyncio.FIRST_COMPLETED)
        if hit_waiter in done:
            task.cancel()
        elif task not in done:
            raise TimeoutError("주입 지점에 도달하지 못했다")
        hit_waiter.cancel()
        await asyncio.gather(hit_waiter, return_exceptions=True)

    caught = None
    try:
        await task
    except BaseException as exc:
        caught = type(exc).__name__
    return auth_executor.is_auth_executor_running(), caught, task.cancelled()

running, caught, cancelled = asyncio.run(scenario())
print("HITS=" + str(hits))
print("CAUGHT=" + str(caught))
print("CANCELLED=" + str(cancelled))
print("RUNNING=" + str(running))
"""


@pytest.mark.parametrize("target", [
    "start_default_executor_probe",      # 첫 post-start 지점
    "start_usdt_ws_gopax_client",        # 마지막 pre-yield 지점
])
@pytest.mark.parametrize("kind", ["error", "cancel"])
def test_failure_at_either_boundary_rolls_the_lane_back(tmp_path, target, kind):
    """⛔ **프로세스 격리**로 돌린다. 한 프로세스에서 실제 lifespan 을 여러 번 열면 앞 케이스가
    남긴 `AsyncIOScheduler` 가 **닫힌 loop 에 바인딩된 채** 남아 다음 `start_scheduler()` 의
    `add_job` 이 죽는다(실측 `app/scheduler.py:1407`). job 제거만으로는 부족하고 인스턴스
    자체가 낡는다. scope 가 scheduler 를 되돌리지 **않는 것이 계약**이므로 격리는 테스트가 진다.
    """
    import subprocess
    import sys as _sys

    script = tmp_path / "probe.py"
    script.write_text(SUBPROC)
    repo = str(pathlib.Path(__file__).resolve().parent.parent)
    import os
    env = dict(os.environ, FXI_REPO=repo, FXI_TMPDB=str(tmp_path / "probe.db"),
               PYTHONPATH=repo)
    r = subprocess.run([_sys.executable, str(script), target, kind],
                       capture_output=True, text=True, cwd=repo, timeout=180, env=env)
    expected = "RuntimeError" if kind == "error" else "CancelledError"
    expected_cancelled = "False" if kind == "error" else "True"
    expected_lines = {
        "HITS=1",
        f"CAUGHT={expected}",
        f"CANCELLED={expected_cancelled}",
        "RUNNING=False",
    }
    observed = set(r.stdout.splitlines())
    assert r.returncode == 0 and expected_lines <= observed, (
        f"{target}/{kind} 주입 도달·예외·rollback 계약 불일치 (rc={r.returncode})"
        f"\nmissing={sorted(expected_lines - observed)}"
        f"\n--- stdout ---\n{r.stdout[-1000:]}\n--- stderr ---\n{r.stderr[-1000:]}")


if __name__ == "__main__":
    pytest.main([__file__])
