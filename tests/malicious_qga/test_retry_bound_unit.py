"""Unit test for the QGA retry envelope being subordinate to a caller deadline.

No Proxmox needed: drives `_retry_on_qga_error` with a coroutine that always
raises a transient error and asserts the loop stops at the deadline instead of
burning all 25 attempts (~460s), which a guest returning transient-looking 5xx
on every poll could otherwise force regardless of the caller's timeout.
"""

import time

import httpx
import pytest

from proxmoxsandbox._impl.agent_commands import AgentCommands


def _make() -> AgentCommands:
    return AgentCommands(async_proxmox=object(), node="proxmox")  # type: ignore[arg-type]


async def test_deadline_stops_retries_early():
    ac = _make()
    attempts = 0

    async def always_transient():
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("boom")

    start = time.monotonic()
    with pytest.raises(httpx.ConnectError):
        await ac._retry_on_qga_error(
            "test", always_transient, deadline=time.monotonic() + 0.5
        )
    elapsed = time.monotonic() - start
    # Bounded by the ~0.5s deadline, nowhere near the 25-attempt envelope.
    assert elapsed < 5
    assert attempts < 10


async def test_no_deadline_preserves_default_behaviour():
    """Without a deadline a non-transient error still raises immediately."""
    ac = _make()

    async def not_transient():
        raise httpx.HTTPStatusError(
            "no such file",
            request=httpx.Request("GET", "http://x"),
            response=httpx.Response(500, text="No such file"),
        )

    with pytest.raises(httpx.HTTPStatusError):
        await ac._retry_on_qga_error("test", not_transient)
