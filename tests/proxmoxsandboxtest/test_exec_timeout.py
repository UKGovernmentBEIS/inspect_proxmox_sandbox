"""exec() converts an exhausted poll into a clear TimeoutError, not a RetryError."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox import _proxmox_sandbox_environment as mod
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment


def _make_sandbox(agent_commands) -> ProxmoxSandboxEnvironment:
    return ProxmoxSandboxEnvironment(
        infra_commands=MagicMock(),
        agent_commands=agent_commands,
        ipam_mappings=(),
        vm_id=100,
        all_vm_ids=(100,),
        sdn_zone_id=None,
        instance=None,
        pool_id=None,
        os_type="l26",
    )


@pytest.mark.asyncio
async def test_exec_unresponsive_agent_raises_timeouterror(monkeypatch):
    """If exec-status never reports completion, exec raises TimeoutError."""
    # Drop the grace so the poll deadline is just `timeout`, keeping the test fast.
    monkeypatch.setattr(mod, "_EXEC_POLL_GRACE_SECONDS", 0)

    agent = MagicMock()
    agent.write_file = AsyncMock()
    agent.exec_command = AsyncMock(return_value={"pid": 42})
    # exited != 1 means "still running"; the agent never reports completion.
    agent.get_agent_exec_status = AsyncMock(return_value={"exited": 0})

    env = _make_sandbox(agent)

    with pytest.raises(TimeoutError) as exc_info:
        await env.exec(["sleep", "100"], timeout=1)

    message = str(exc_info.value)
    assert "guest" in message.lower()
    assert "RetryError" not in message
