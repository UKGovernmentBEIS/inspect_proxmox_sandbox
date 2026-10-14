"""Unit tests for QGA transient-error retry logic (no Proxmox infra required).

Safe QGA operations retry httpx transport errors and 5xx, including Proxmox
596/597 "Broken pipe". Permanent errors (4xx, missing file, gone PID) are not
retried. Arbitrary launches are single-shot; exec-status retries use the
PID-gone disk fallback when a completed status response was lost.
"""

import asyncio

import httpx
import pytest

from proxmoxsandbox._impl.agent_commands import (
    _QGA_MAX_RETRIES,
    AgentCommands,
    HealthcheckTransportExhaustedError,
    _is_pid_gone,
    healthcheck_transport_budget,
    is_transient_qga_error,
)
from proxmoxsandbox._impl.qga_responses import ExecStatus


def _http_error(status_code: int, message: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://proxmox:8006/api2/json/x")
    response = httpx.Response(status_code, request=request, text=message)
    return httpx.HTTPStatusError(
        message or f"HTTP {status_code}", request=request, response=response
    )


# The real message for a finished+already-read PID (the id is a stray %ld).
_PID_GONE = _http_error(500, "Agent error: PID ld does not exist")

TRANSIENT_ERRORS = [
    httpx.ConnectError("connection refused"),
    httpx.ConnectTimeout(""),
    httpx.PoolTimeout(""),
    httpx.ReadTimeout(""),
    httpx.ReadError(""),
    httpx.WriteError(""),
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
    _http_error(500, "500 QEMU guest agent is not running"),
    _http_error(500, "guest-exec-status failed - got timeout"),
    _http_error(596, "Server error '596 Broken pipe'"),
    _http_error(597, "Server error '597 Broken pipe'"),
]

PERMANENT_ERRORS = [
    _http_error(400, "value may only be 61440 characters long"),
    _http_error(401, "auth required"),
    _http_error(404, "not found"),
    _http_error(
        500,
        "Agent error: failed to open file "
        "'/tmp/x.returncode': No such file or directory",
    ),
    _http_error(500, "Agent error: failed to read file: Is a directory"),
    _PID_GONE,  # surfaced immediately; get_agent_exec_status turns it into a disk read
]


class _FakeApi:
    """Minimal stand-in for AsyncProxmoxAPI whose request() replays a script."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def request(self, method, path, **kwargs):
        self.calls += 1
        item = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def agent_commands() -> AgentCommands:
    # _retry_on_qga_error doesn't touch these.
    return AgentCommands(async_proxmox=None, node="proxmox")  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(_delay):
        return None

    monkeypatch.setattr("proxmoxsandbox._impl.agent_commands.asyncio.sleep", _noop)


# --- classification --------------------------------------------------------


@pytest.mark.parametrize("exc", TRANSIENT_ERRORS)
def test_transient_errors_are_retryable(exc):
    assert is_transient_qga_error(exc) is True


@pytest.mark.parametrize("exc", PERMANENT_ERRORS)
def test_permanent_errors_are_not_retryable(exc):
    assert is_transient_qga_error(exc) is False


def test_is_pid_gone():
    assert _is_pid_gone(_PID_GONE) is True
    assert _is_pid_gone(_http_error(500, "got timeout")) is False


# --- retry loop ------------------------------------------------------------


async def test_recovers_after_transient_errors(agent_commands):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("")
        return "ok"

    assert await agent_commands._retry_on_qga_error("read", flaky) == "ok"
    assert calls["n"] == 3


async def test_recovers_after_broken_pipe(agent_commands):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(596, "Server error '596 Broken pipe'")
        return "ok"

    assert await agent_commands._retry_on_qga_error("read", flaky) == "ok"


async def test_retry_exhausts_then_raises(agent_commands):
    calls = {"n": 0}

    async def always_down():
        calls["n"] += 1
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        await agent_commands._retry_on_qga_error("read", always_down)
    assert calls["n"] == _QGA_MAX_RETRIES


async def test_retry_budget_is_configurable():
    calls = {"n": 0}

    async def always_down():
        calls["n"] += 1
        raise _http_error(500, "500 QEMU guest agent is not running")

    single_shot = AgentCommands(
        async_proxmox=None,  # type: ignore[arg-type]
        node="proxmox",
        qga_max_retries=1,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await single_shot._retry_on_qga_error("read", always_down)
    assert calls["n"] == 1


async def test_healthcheck_recovery_budget_spans_operations(
    agent_commands, monkeypatch
):
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("proxmoxsandbox._impl.agent_commands.asyncio.sleep", sleep)
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 21:
            return "uploaded"
        raise _http_error(500)

    with healthcheck_transport_budget(interval=5):
        assert await agent_commands._retry_on_qga_error("upload", flaky) == "uploaded"
        with pytest.raises(HealthcheckTransportExhaustedError) as exc_info:
            await agent_commands._retry_on_qga_error("read", flaky)
        assert exc_info.value.failures == 25
        assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)
        with pytest.raises(HealthcheckTransportExhaustedError):
            await agent_commands._retry_on_qga_error("read", flaky)

    assert calls == 26  # 25 failures and one successful upload, never a fresh budget.
    assert sleeps == [5] * 24


async def test_nested_healthcheck_scope_does_not_reset_recovery(agent_commands):
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 21:
            return "uploaded"
        raise httpx.ReadTimeout("lost response")

    with healthcheck_transport_budget(interval=1):
        await agent_commands._retry_on_qga_error("upload", flaky)
        with healthcheck_transport_budget(interval=999):
            with pytest.raises(HealthcheckTransportExhaustedError):
                await agent_commands._retry_on_qga_error("read", flaky)
    assert calls == 26


async def test_healthcheck_scope_preserves_explicit_operation_cap():
    api = _FakeApi([_http_error(500)])
    agent = AgentCommands(api, "pve", qga_max_retries=1)  # type: ignore[arg-type]
    with healthcheck_transport_budget(interval=1):
        with pytest.raises(HealthcheckTransportExhaustedError) as exc_info:
            await agent.write_file(100, b"script", "/tmp/script")
    assert exc_info.value.failures == 1
    assert api.calls == 1


async def test_normal_operations_retain_independent_retry_budgets(agent_commands):
    for _ in range(2):
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 25:
                raise httpx.ReadTimeout("lost response")
            return "ok"

        assert await agent_commands._retry_on_qga_error("read", flaky) == "ok"
        assert calls == 25


async def test_cancelled_scope_does_not_leak_into_later_operations(agent_commands):
    async def cancel():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        with healthcheck_transport_budget(interval=1):
            await agent_commands._retry_on_qga_error("read", cancel)

    async def fail():
        raise httpx.ReadTimeout("lost response")

    # An unscoped operation retains its original exception, not healthcheck exhaustion.
    with pytest.raises(httpx.ReadTimeout):
        await agent_commands._retry_on_qga_error("read", fail)


async def test_too_large_write_not_retried(agent_commands):
    calls = {"n": 0}

    async def too_large():
        calls["n"] += 1
        raise _http_error(400, "value may only be 61440 characters long")

    with pytest.raises(httpx.HTTPStatusError):
        await agent_commands._retry_on_qga_error("write", too_large)
    assert calls["n"] == 1


async def test_missing_file_not_retried(agent_commands):
    calls = {"n": 0}

    async def missing():
        calls["n"] += 1
        raise _http_error(
            500, "failed to open file '/tmp/x': No such file or directory"
        )

    with pytest.raises(httpx.HTTPStatusError):
        await agent_commands._retry_on_qga_error("read", missing)
    assert calls["n"] == 1


# --- get_agent_exec_status PID-gone disk fallback --------------------------


async def test_exec_status_pid_gone_reports_finished():
    # PID already consumed: surfaced immediately (not retried), then converted
    # to a synthetic completed status so exec() reads results from disk.
    api = _FakeApi([_PID_GONE])
    ac = AgentCommands(async_proxmox=api, node="proxmox")  # type: ignore[arg-type]
    assert await ac.get_agent_exec_status(vm_id=101, pid=868) == ExecStatus(exited=1)
    assert api.calls == 1


async def test_exec_status_recovers_consumed_after_lost_response():
    # First attempt's response is lost (agent consumed the result); the retry
    # finds the PID gone -> fall back to "finished".
    api = _FakeApi([httpx.ReadTimeout(""), _PID_GONE])
    ac = AgentCommands(async_proxmox=api, node="proxmox")  # type: ignore[arg-type]
    assert await ac.get_agent_exec_status(vm_id=101, pid=868) == ExecStatus(exited=1)
    assert api.calls == 2


async def test_exec_status_returns_status_when_present():
    api = _FakeApi([{"exited": 1, "exitcode": 0}])
    ac = AgentCommands(async_proxmox=api, node="proxmox")  # type: ignore[arg-type]
    assert await ac.get_agent_exec_status(vm_id=101, pid=868) == ExecStatus(
        exited=1, exitcode=0
    )
    assert api.calls == 1
