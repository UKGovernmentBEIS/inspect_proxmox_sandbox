"""Unit tests for the compose-style guest healthcheck runner.

The runner gets `clock` and `sleep` injected so these tests run instantly and
can assert the exact retry schedule.
"""

import asyncio
from typing import Callable

import httpx
import pytest
from inspect_ai.util import ExecResult, OutputLimitExceededError

from proxmoxsandbox._impl.healthcheck import (
    _TRANSPORT_ERROR_LIMIT,
    HealthCheckFailed,
    HealthCheckRunner,
)
from proxmoxsandbox.schema import HealthCheck


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def _ok() -> ExecResult[str]:
    return ExecResult(success=True, returncode=0, stdout="", stderr="")


def _fail(code: int = 1) -> ExecResult[str]:
    return ExecResult(success=False, returncode=code, stdout="", stderr="oops")


def _transport_error() -> httpx.TransportError:
    return httpx.ConnectError("boom")


def _http_500() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://pve/x")
    return httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, request=request)
    )


def _scripted(*outcomes) -> tuple[Callable, list[HealthCheck]]:
    """execute() that replays `outcomes` (ExecResult or Exception), last one sticky."""
    calls: list[HealthCheck] = []

    async def execute(spec: HealthCheck) -> ExecResult[str]:
        calls.append(spec)
        item = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    return execute, calls


def _runner(spec: HealthCheck, execute, clock: FakeClock) -> HealthCheckRunner:
    return HealthCheckRunner(
        spec, execute, label="web (ID=100)", clock=clock, sleep=clock.sleep
    )


async def test_passes_on_first_success_without_sleeping():
    clock = FakeClock()
    execute, calls = _scripted(_ok())
    await _runner(HealthCheck(test=("true",)), execute, clock).run()
    assert len(calls) == 1
    assert clock.sleeps == []


async def test_fails_after_retries_consecutive_failures():
    clock = FakeClock()
    execute, calls = _scripted(_fail(3))
    spec = HealthCheck(test=("false",), interval=2, retries=3)
    with pytest.raises(HealthCheckFailed) as exc_info:
        await _runner(spec, execute, clock).run()
    assert len(calls) == 3
    assert clock.sleeps == [2, 2]
    message = str(exc_info.value)
    assert "web (ID=100)" in message
    assert "3 consecutive" in message
    assert "exit code 3" in message
    assert "oops" not in message  # guest output may contain secrets


async def test_success_after_failures_returns():
    clock = FakeClock()
    execute, calls = _scripted(_fail(), _fail(), _ok())
    spec = HealthCheck(test=("x",), interval=1, retries=3)
    await _runner(spec, execute, clock).run()
    assert len(calls) == 3
    assert clock.sleeps == [1, 1]


async def test_start_period_failures_do_not_count():
    clock = FakeClock()
    execute, calls = _scripted(_fail())
    spec = HealthCheck(test=("x",), interval=2, retries=1, start_period=10)
    with pytest.raises(HealthCheckFailed):
        await _runner(spec, execute, clock).run()
    # Attempts at t=0,2,4,6,8 are within the grace window; t=10 is the first
    # counted failure, and retries=1 means it is fatal.
    assert len(calls) == 6
    assert clock.sleeps == [2, 2, 2, 2, 2]


async def test_slow_probe_is_judged_by_when_it_started():
    """start_period applies to the attempt's start time, as in compose.

    A 25 s probe started at t=0 with start_period=20 must not count; the one
    started at t=26 must, and with retries=1 that is fatal.
    """
    clock = FakeClock()
    calls: list[float] = []

    async def slow_fail(spec: HealthCheck) -> ExecResult[str]:
        calls.append(clock.now)
        clock.now += 25
        return _fail()

    spec = HealthCheck(test=("x",), interval=1, retries=1, start_period=20)
    with pytest.raises(HealthCheckFailed, match="failed 1 consecutive"):
        await _runner(spec, slow_fail, clock).run()
    assert calls == [1000.0, 1026.0]


async def test_timeout_counts_as_failure():
    clock = FakeClock()
    execute, calls = _scripted(TimeoutError("Command timed out"))
    spec = HealthCheck(test=("x",), interval=1, retries=2)
    with pytest.raises(HealthCheckFailed, match="timed out"):
        await _runner(spec, execute, clock).run()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "exc, detail",
    [
        (PermissionError("Permission denied executing command"), "permission denied"),
        (OutputLimitExceededError("10 MiB", None), "output limit exceeded"),
    ],
)
async def test_exec_classification_errors_count_as_failures(exc, detail):
    """A non-executable test or oversized output consumes retries like exit 1."""
    clock = FakeClock()
    execute, calls = _scripted(exc)
    spec = HealthCheck(test=("/etc/hostname",), interval=2, retries=3)
    with pytest.raises(HealthCheckFailed, match=detail):
        await _runner(spec, execute, clock).run()
    assert len(calls) == 3
    assert clock.sleeps == [2, 2]


async def test_exec_classification_error_then_success_passes():
    clock = FakeClock()
    execute, calls = _scripted(PermissionError("nope"), _ok())
    await _runner(HealthCheck(test=("x",), interval=1), execute, clock).run()
    assert len(calls) == 2


async def test_transport_errors_do_not_count_toward_retries():
    clock = FakeClock()
    execute, calls = _scripted(
        _transport_error(), _transport_error(), _transport_error(), _ok()
    )
    spec = HealthCheck(test=("x",), interval=1, retries=1)
    await _runner(spec, execute, clock).run()
    assert len(calls) == 4
    assert clock.sleeps == [1, 1, 1]


async def test_http_status_errors_are_transport_errors():
    clock = FakeClock()
    request = httpx.Request("POST", "https://pve/x")
    err = httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, request=request)
    )
    execute, calls = _scripted(err, _ok())
    spec = HealthCheck(test=("x",), interval=1, retries=1)
    await _runner(spec, execute, clock).run()
    assert len(calls) == 2


@pytest.mark.parametrize("error", [_transport_error(), _http_500()])
async def test_transport_errors_are_bounded(error):
    """A dead agent costs _TRANSPORT_ERROR_LIMIT probes and `interval` sleeps only."""
    clock = FakeClock()
    execute, calls = _scripted(error)
    spec = HealthCheck(test=("x",), interval=1, retries=1)
    with pytest.raises(HealthCheckFailed, match="guest agent"):
        await _runner(spec, execute, clock).run()
    assert len(calls) == _TRANSPORT_ERROR_LIMIT
    assert clock.sleeps == [1] * (_TRANSPORT_ERROR_LIMIT - 1)


async def test_success_resets_transport_error_count():
    clock = FakeClock()
    outcomes = [_transport_error()] * (_TRANSPORT_ERROR_LIMIT - 1) + [
        _fail(),
        *[_transport_error()] * (_TRANSPORT_ERROR_LIMIT - 1),
        _ok(),
    ]
    execute, calls = _scripted(*outcomes)
    spec = HealthCheck(test=("x",), interval=1, retries=5)
    await _runner(spec, execute, clock).run()
    assert len(calls) == len(outcomes)


async def test_cancellation_propagates():
    clock = FakeClock()
    started = asyncio.Event()

    async def execute(spec: HealthCheck) -> ExecResult[str]:
        started.set()
        await asyncio.sleep(3600)
        return _ok()

    runner = HealthCheckRunner(
        HealthCheck(test=("x",)), execute, label="web", clock=clock, sleep=clock.sleep
    )
    task = asyncio.create_task(runner.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_execute_receives_the_spec():
    clock = FakeClock()
    execute, calls = _scripted(_ok())
    spec = HealthCheck(test=("systemctl", "is-active", "nginx"), timeout=7)
    await _runner(spec, execute, clock).run()
    assert calls == [spec]
