"""Unit tests for QGA transient-error retry logic (no Proxmox infra required).

Covers the failure classes seen killing real eval samples (httpx transport
errors and Proxmox 596/597 "Broken pipe") and the per-call retry policy:

                   connect-phase   5xx       read-phase
    IDEMPOTENT         yes          yes          yes
    PROCESS_START      yes          no           no

IDEMPOTENT covers file read/write and exec-status (made idempotent by the
PID-gone -> disk fallback). PROCESS_START is exec-start, the one call a retry
could run twice. read-phase = ReadTimeout/ReadError/RemoteProtocolError
(request sent, response lost).
"""

import httpx
import pytest

from proxmoxsandbox._impl.agent_commands import (
    _QGA_MAX_RETRIES,
    AgentCommands,
    _is_pid_gone,
    _QgaRetry,
)


def _http_error(status_code: int, message: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://proxmox:8006/api2/json/x")
    response = httpx.Response(status_code, request=request, text=message)
    return httpx.HTTPStatusError(
        message or f"HTTP {status_code}", request=request, response=response
    )


# The real message for a finished+already-read PID (the id is a stray %ld).
_PID_GONE = _http_error(500, "Agent error: PID ld does not exist")

# Request provably never reached the server -> always safe to retry.
CONNECT_PHASE_ERRORS = [
    httpx.ConnectError("connection refused"),
    httpx.ConnectTimeout(""),
    httpx.PoolTimeout(""),
]

# Server returned an error, not a result: nothing consumed, but a process
# start may have run -> retry for idempotent calls, not for process-start.
SERVER_ERRORS = [
    _http_error(500, "500 QEMU guest agent is not running"),
    _http_error(500, "guest-exec-status failed - got timeout"),
    _http_error(596, "Server error '596 Broken pipe'"),
    _http_error(597, "Server error '597 Broken pipe'"),
]

# Request sent, response lost: side effect may have happened -> retry only when
# freely repeatable.
LOST_RESPONSE_ERRORS = [
    httpx.ReadTimeout(""),
    httpx.ReadError(""),
    httpx.WriteError(""),
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
]

# Never retryable, regardless of call.
PERMANENT_ERRORS = [
    _http_error(400, "value may only be 61440 characters long"),
    _http_error(401, "auth required"),
    _http_error(404, "not found"),
    _http_error(
        500,
        "Agent error: failed to open file "
        "'/tmp/x.returncode': No such file or directory",
    ),
    _PID_GONE,  # surfaced immediately; get_agent_exec_status turns it into a disk read
]

ALL_POLICIES = [_QgaRetry.IDEMPOTENT, _QgaRetry.PROCESS_START]


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
    # _retry_on_qga_error / _is_transient_qga_error don't touch these.
    return AgentCommands(async_proxmox=None, node="proxmox")  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(_delay):
        return None

    monkeypatch.setattr("proxmoxsandbox._impl.agent_commands.asyncio.sleep", _noop)


def _retryable(agent_commands, exc, policy) -> bool:
    return agent_commands._is_transient_qga_error(exc, policy=policy)


# --- classification matrix -------------------------------------------------


@pytest.mark.parametrize("policy", ALL_POLICIES)
@pytest.mark.parametrize("exc", CONNECT_PHASE_ERRORS)
def test_connect_phase_always_retryable(agent_commands, exc, policy):
    assert _retryable(agent_commands, exc, policy) is True


@pytest.mark.parametrize("policy", ALL_POLICIES)
@pytest.mark.parametrize("exc", PERMANENT_ERRORS)
def test_permanent_errors_never_retryable(agent_commands, exc, policy):
    assert _retryable(agent_commands, exc, policy) is False


@pytest.mark.parametrize("exc", SERVER_ERRORS)
def test_server_errors_retried_except_for_process_start(agent_commands, exc):
    assert _retryable(agent_commands, exc, _QgaRetry.IDEMPOTENT) is True
    assert _retryable(agent_commands, exc, _QgaRetry.PROCESS_START) is False


@pytest.mark.parametrize("exc", LOST_RESPONSE_ERRORS)
def test_lost_response_retried_only_when_idempotent(agent_commands, exc):
    assert _retryable(agent_commands, exc, _QgaRetry.IDEMPOTENT) is True
    assert _retryable(agent_commands, exc, _QgaRetry.PROCESS_START) is False


def test_is_pid_gone():
    assert _is_pid_gone(_PID_GONE) is True
    assert _is_pid_gone(_http_error(500, "got timeout")) is False


# --- retry loop behaviour --------------------------------------------------


async def test_idempotent_recovers_after_transport_errors(agent_commands):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("")
        return "ok"

    result = await agent_commands._retry_on_qga_error(
        "read", flaky, policy=_QgaRetry.IDEMPOTENT
    )
    assert result == "ok"
    assert calls["n"] == 3


async def test_idempotent_recovers_after_5xx(agent_commands):
    # The exec-status "got timeout" 500 that the e2e pause test hit.
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(500, "guest-exec-status failed - got timeout")
        return "ok"

    result = await agent_commands._retry_on_qga_error(
        "exec-status", flaky, policy=_QgaRetry.IDEMPOTENT
    )
    assert result == "ok"
    assert calls["n"] == 3


async def test_process_start_recovers_after_connect_error(agent_commands):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("down")
        return "ok"

    result = await agent_commands._retry_on_qga_error(
        "exec", flaky, policy=_QgaRetry.PROCESS_START
    )
    assert result == "ok"
    assert calls["n"] == 3


async def test_process_start_does_not_retry_read_timeout(agent_commands):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        raise httpx.ReadTimeout("")

    with pytest.raises(httpx.ReadTimeout):
        await agent_commands._retry_on_qga_error(
            "exec", flaky, policy=_QgaRetry.PROCESS_START
        )
    assert calls["n"] == 1


async def test_process_start_does_not_retry_5xx(agent_commands):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        raise _http_error(500, "500 QEMU guest agent is not running")

    with pytest.raises(httpx.HTTPStatusError):
        await agent_commands._retry_on_qga_error(
            "exec", flaky, policy=_QgaRetry.PROCESS_START
        )
    assert calls["n"] == 1


async def test_retry_exhausts_then_raises(agent_commands):
    calls = {"n": 0}

    async def always_down():
        calls["n"] += 1
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        await agent_commands._retry_on_qga_error(
            "read", always_down, policy=_QgaRetry.IDEMPOTENT
        )
    assert calls["n"] == _QGA_MAX_RETRIES


async def test_too_large_write_not_retried(agent_commands):
    calls = {"n": 0}

    async def too_large():
        calls["n"] += 1
        raise _http_error(400, "value may only be 61440 characters long")

    with pytest.raises(httpx.HTTPStatusError):
        await agent_commands._retry_on_qga_error(
            "write", too_large, policy=_QgaRetry.IDEMPOTENT
        )
    assert calls["n"] == 1


async def test_missing_file_not_retried(agent_commands):
    calls = {"n": 0}

    async def missing():
        calls["n"] += 1
        raise _http_error(
            500, "failed to open file '/tmp/x': No such file or directory"
        )

    with pytest.raises(httpx.HTTPStatusError):
        await agent_commands._retry_on_qga_error(
            "read", missing, policy=_QgaRetry.IDEMPOTENT
        )
    assert calls["n"] == 1


# --- get_agent_exec_status PID-gone disk fallback --------------------------


async def test_exec_status_pid_gone_reports_finished():
    # PID already consumed: surfaced immediately (not retried), then converted
    # to a synthetic completed status so exec() reads results from disk.
    api = _FakeApi([_PID_GONE])
    ac = AgentCommands(async_proxmox=api, node="proxmox")  # type: ignore[arg-type]
    result = await ac.get_agent_exec_status(vm_id=101, pid=868)
    assert result == {"exited": 1}
    assert api.calls == 1


async def test_exec_status_recovers_consumed_after_lost_response():
    # First attempt's response is lost (agent consumed the result); the retry
    # finds the PID gone -> fall back to "finished".
    api = _FakeApi([httpx.ReadTimeout(""), _PID_GONE])
    ac = AgentCommands(async_proxmox=api, node="proxmox")  # type: ignore[arg-type]
    result = await ac.get_agent_exec_status(vm_id=101, pid=868)
    assert result == {"exited": 1}
    assert api.calls == 2


async def test_exec_status_returns_status_when_present():
    api = _FakeApi([{"exited": 1, "exitcode": 0}])
    ac = AgentCommands(async_proxmox=api, node="proxmox")  # type: ignore[arg-type]
    result = await ac.get_agent_exec_status(vm_id=101, pid=868)
    assert result == {"exited": 1, "exitcode": 0}
    assert api.calls == 1
