from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._impl import agent_commands as agent_commands_module
from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.async_proxmox import ProxmoxTaskError

LOCK_TIMEOUT = "can't lock file '/var/lock/qemu-server/lock-101.conf' - got timeout"


def _agent_commands(
    resume_exitstatuses: list[str],
) -> tuple[AgentCommands, list[tuple[str, str]]]:
    """AgentCommands on a fake API whose resume tasks end with the given statuses."""
    calls: list[tuple[str, str]] = []
    statuses = iter(resume_exitstatuses)

    async def request(method: str, path: str, **kwargs):
        calls.append((method, path))
        if path.endswith("/rollback"):
            return "UPID:proxmox:1:2:3:qmrollback:101:root@pam:"
        if path.endswith("/status/resume"):
            return "UPID:proxmox:1:2:3:qmresume:101:root@pam:"
        raise AssertionError(f"unexpected request {method} {path}")

    async def wait_for_task(*, upid: str) -> None:
        if "qmresume" in upid and (status := next(statuses)) != "OK":
            raise ProxmoxTaskError(upid, status)

    api = MagicMock()
    api.request = AsyncMock(side_effect=request)
    api.wait_for_task = AsyncMock(side_effect=wait_for_task)
    return AgentCommands(api, "proxmox"), calls


def _resumes(calls: list[tuple[str, str]]) -> int:
    return sum(1 for _, path in calls if path.endswith("/status/resume"))


async def test_rollback_returns_once_the_config_lock_is_free() -> None:
    agent_commands, calls = _agent_commands([LOCK_TIMEOUT, LOCK_TIMEOUT, "OK"])

    await agent_commands.rollback_to_snapshot(101, "inspect1")

    assert _resumes(calls) == 3


async def test_rollback_raises_if_the_config_lock_stays_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_commands_module, "_ROLLBACK_LOCK_TIMEOUT", 0)
    agent_commands, calls = _agent_commands([LOCK_TIMEOUT] * 3)

    with pytest.raises(RuntimeError, match="config lock still held"):
        await agent_commands.rollback_to_snapshot(101, "inspect1")
    assert _resumes(calls) == 1


async def test_rollback_raises_other_resume_failures_at_once() -> None:
    agent_commands, calls = _agent_commands(["VM 101 is a template", "OK"])

    with pytest.raises(ProxmoxTaskError, match="VM 101 is a template"):
        await agent_commands.rollback_to_snapshot(101, "inspect1")
    assert _resumes(calls) == 1
