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


def _lifespan_ast():
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/main.py").read_text())
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "lifespan")


def test_shutdown_covers_both_lanes_and_isolates_their_failures():
    """⛔ S1b — **한 lane 의 종료 실패가 다른 lane 을 건너뛰면 안 된다**.

    순차로 부르면 첫 `begin` 이 던지는 순간 나머지 lane 은 종료조차 시도되지 않고, 다음
    lifespan 이 구 worker 스레드와 겹친다(`TestClient` 는 lifespan 을 여러 번 연다).

    ⚠️ **행동이 아니라 구조로 잠근다.** 이 경로를 행동으로 재현하려면 프로세스를 격리해 실패를
       주입해야 하는데(위 `test_failure_at_either_boundary_rolls_the_lane_back` 이 그렇게 한다),
       종료 실패까지 그 비용을 치를 만큼의 추가 이득이 없다. "두 lane 이 각각 try 안에 있다" 가
       무너지는 순간이 곧 계약 위반이다.
    ⚠️ 한때 이 계약을 `tests/test_two_lane_lifecycle.py` 가 본다고 적었는데 **거짓**이었다 —
       그쪽은 lane 표면에서 패턴만 재현하고 `app/main.py` 배선에는 닿지 않는다.
    """
    import ast

    fn = _lifespan_ast()
    names = {
        "begin_auth_executor_shutdown",
        "begin_rest_auth_executor_shutdown",
        "await_auth_executor_shutdown",
        "await_rest_auth_executor_shutdown",
    }
    seen = {n for node in ast.walk(fn) if isinstance(node, ast.Attribute) and node.attr in names
            for n in [node.attr]}
    missing = sorted(names - seen)
    assert not missing, f"lifespan 이 종료하지 않는 lane 표면: {missing}"

    # ⛔ **호출이 아니라 loop 를 본다.** 실제 코드는 `(name, fn)` 쌍을 순회하며 지역 변수로
    #    부르므로, 심볼 자체는 loop **헤더**(try 밖)에 있고 호출은 `Name` 이다. "Attribute 가
    #    try 안에 있나" 로 물으면 올바른 코드에도 red 가 난다(실측). 계약은 "각 lane 을 순회하는
    #    loop 의 **본문**이 try 로 감싸여 있다" 이다.
    def _loop_over(group: set[str]) -> ast.For | None:
        for node in ast.walk(fn):
            if not isinstance(node, ast.For):
                continue
            attrs = {a.attr for a in ast.walk(node.iter) if isinstance(a, ast.Attribute)}
            if group <= attrs:
                return node
        return None

    for label, group in (("begin", {"begin_auth_executor_shutdown",
                                    "begin_rest_auth_executor_shutdown"}),
                         ("await", {"await_auth_executor_shutdown",
                                    "await_rest_auth_executor_shutdown"})):
        loop = _loop_over(group)
        assert loop is not None, f"{label}: 두 lane 을 함께 순회하는 loop 가 없다 — 순차 호출이면 첫 실패가 나머지를 건너뛴다"
        assert any(isinstance(n, ast.Try) for n in ast.walk(loop)), \
            f"{label}: loop 본문에 try 가 없다 — 한 lane 실패가 다른 lane 종료를 건너뛴다"


def test_startup_opens_a_separate_scope_per_lane():
    """⛔ 조합 scope 는 `created` 판정을 결합해 **남의 lane 을 내릴** 수 있다.

    그 회귀는 정상 경로에서 전혀 드러나지 않는다 — 실패 경로에서만 발화한다.
    """
    import ast

    fn = _lifespan_ast()
    scopes = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr.endswith("_lane_startup_scope")
    }
    assert scopes == {"ws_auth_lane_startup_scope", "rest_auth_lane_startup_scope"}, scopes

    # wrapper 두 개의 **존재**만 보면 순차 scope 도 통과한다. 한때 REST scope 를 `pass` 로
    # 먼저 닫은 뒤 WS/startup body 를 실행해 late failure 가 REST lane 을 되돌리지 못했다.
    protected_at_last_step: set[str] = set()

    def walk(node, active: frozenset[str] = frozenset()):
        nonlocal protected_at_last_step
        if isinstance(node, ast.AsyncWith):
            active = active | {
                item.context_expr.func.attr
                for item in node.items
                if isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and item.context_expr.func.attr.endswith("_lane_startup_scope")
            }
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "start_usdt_ws_gopax_client"):
            protected_at_last_step |= set(active)
        for child in ast.iter_child_nodes(node):
            walk(child, active)

    walk(fn)
    assert protected_at_last_step == scopes, (
        "마지막 startup 단계가 두 lane scope 모두의 보호를 받지 않는다: "
        f"{sorted(protected_at_last_step)}")


if __name__ == "__main__":
    pytest.main([__file__])
