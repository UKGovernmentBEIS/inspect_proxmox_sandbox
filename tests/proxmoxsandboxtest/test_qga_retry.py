"""Unit tests for QGA transient-error retry logic (no Proxmox infra required).

Covers the failure classes seen killing real eval samples: httpx transport
errors and Proxmox 596/597 "Broken pipe", which must be retried, while
permanent errors (400 too-large, missing-file 500) must not be.
"""

import httpx
import pytest

from proxmoxsandbox._impl.agent_commands import _QGA_MAX_RETRIES, AgentCommands


def _http_error(status_code: int, message: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://proxmox:8006/api2/json/x")
    response = httpx.Response(status_code, request=request, text=message)
    return httpx.HTTPStatusError(
        message or f"HTTP {status_code}", request=request, response=response
    )


@pytest.fixture
def agent_commands() -> AgentCommands:
    # _retry_on_qga_error / _is_transient_qga_error don't touch these.
    return AgentCommands(async_proxmox=None, node="proxmox")  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(_delay):
        return None

    monkeypatch.setattr("proxmoxsandbox._impl.agent_commands.asyncio.sleep", _noop)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout(""),
        httpx.ReadTimeout(""),
        httpx.ReadError(""),
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        _http_error(500, "500 QEMU guest agent is not running"),
        _http_error(596, "Server error '596 Broken pipe'"),
        _http_error(597, "Server error '597 Broken pipe'"),
    ],
)
def test_transient_errors_are_retryable(agent_commands, exc):
    assert agent_commands._is_transient_qga_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        _http_error(400, "value may only be 61440 characters long"),
        _http_error(401, "auth required"),
        _http_error(404, "not found"),
        _http_error(
            500,
            "Agent error: failed to open file "
            "'/tmp/x.returncode': No such file or directory",
        ),
    ],
)
def test_permanent_errors_are_not_retryable(agent_commands, exc):
    assert agent_commands._is_transient_qga_error(exc) is False


async def test_retry_recovers_after_transport_errors(agent_commands):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectTimeout("")
        return "ok"

    assert await agent_commands._retry_on_qga_error("read", flaky) == "ok"
    assert calls["n"] == 3


async def test_retry_recovers_after_broken_pipe(agent_commands):
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
