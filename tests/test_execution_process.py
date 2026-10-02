from __future__ import annotations

import asyncio
import sys

import pytest

from codeagent.execution.models import ExecutionLimits, SandboxError
from codeagent.execution.process import run_bounded
from codeagent.infra.cancellation import CancellationToken, CancelledByUser


@pytest.mark.parametrize("values", [
    {"memory_mib": 0}, {"pids": -1}, {"output_bytes": True},
    {"cpus": float("nan")}, {"timeout_seconds": float("inf")},
])
def test_resource_limits_reject_invalid_values(values):
    with pytest.raises(ValueError):
        ExecutionLimits(**values)


async def test_bounded_control_process_does_not_interpret_shell():
    marker = "a; echo not-a-shell"
    result = await run_bounded(
        [sys.executable, "-I", "-c", "import sys; print(sys.argv[1])", marker],
    )
    assert result.returncode == 0
    assert result.stdout.decode().strip() == marker


async def test_control_process_does_not_inherit_secrets(monkeypatch):
    monkeypatch.setenv("MC_PRIVATE_TEST_VALUE", "synthetic")
    result = await run_bounded([
        sys.executable, "-I", "-c",
        "import os; print('MC_PRIVATE_TEST_VALUE' in os.environ)",
    ])
    assert result.stdout.strip() == b"False"


async def test_stdout_and_stderr_share_a_byte_budget():
    with pytest.raises(SandboxError, match="字节上限"):
        await run_bounded([
            sys.executable, "-I", "-c",
            "import sys; sys.stdout.write('x'*700); sys.stderr.write('y'*700)",
        ], max_bytes=1024)


async def test_silent_process_timeout_is_bounded():
    with pytest.raises(TimeoutError):
        await run_bounded([sys.executable, "-I", "-c", "import time; time.sleep(10)"],
                          timeout_seconds=.15)


async def test_cancellation_wakes_silent_process():
    token = CancellationToken()
    task = asyncio.create_task(run_bounded(
        [sys.executable, "-I", "-c", "import time; time.sleep(10)"], cancellation=token,
    ))
    await asyncio.sleep(.15)
    token.cancel()
    with pytest.raises(CancelledByUser):
        await asyncio.wait_for(task, 5)


async def test_pre_cancelled_command_never_starts():
    token = CancellationToken()
    token.cancel()
    with pytest.raises(CancelledByUser):
        await run_bounded(["does-not-exist"], cancellation=token)


async def test_input_and_nonzero_exit_preserved():
    result = await run_bounded([
        sys.executable, "-I", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); "
        "sys.exit(7)",
    ], data=b"payload")
    assert result.returncode == 7 and result.stdout == b"payload"
