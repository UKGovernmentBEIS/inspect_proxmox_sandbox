"""Keep private staging behind confirmed guest-side permission setup."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._impl import guest_exec
from proxmoxsandbox._impl.agent_commands import AgentCommands
from proxmoxsandbox._impl.guest_exec import GuestExecutor
from proxmoxsandbox._impl.qga_responses import ExecStatus


@pytest.mark.parametrize("os_type", ["l26", "win11"])
@pytest.mark.parametrize(
    "status", [ExecStatus(exited=1, exitcode=1), ExecStatus(exited=1, signal=9)]
)
async def test_failed_private_directory_setup_never_uploads_payload(os_type, status):
    agent = MagicMock(spec=AgentCommands)
    agent.exec_command = AsyncMock(return_value=123)
    agent.get_agent_exec_status = AsyncMock(return_value=status)
    agent.write_file = AsyncMock()
    executor = GuestExecutor(agent, 100, os_type)

    with pytest.raises(RuntimeError, match="private execution directory"):
        await executor.exec(["echo", "payload"], input="private input", timeout=1)

    agent.write_file.assert_not_awaited()
    assert agent.exec_command.await_count == 1
    command = agent.exec_command.await_args.args[1]
    assert "private input" not in " ".join(command)
    assert "payload" not in " ".join(command)


@pytest.mark.parametrize("os_type", ["l26", "win11"])
async def test_unknown_preparation_status_requires_fresh_confirmation(
    monkeypatch, os_type
):
    agent = MagicMock(spec=AgentCommands)
    agent.exec_command = AsyncMock(side_effect=[123, 124])
    agent.get_agent_exec_status = AsyncMock(
        side_effect=[ExecStatus(exited=1), ExecStatus(exited=1, exitcode=0)]
    )
    sleeper = AsyncMock()
    monkeypatch.setattr(guest_exec.asyncio, "sleep", sleeper)
    executor = GuestExecutor(agent, 100, os_type)
    directory = (
        r"C:\Windows\Temp\proxmox_exec_123"
        if os_type == "win11"
        else "/tmp/proxmox_exec_123"
    )

    await executor._prepare_workspace(directory)

    assert agent.exec_command.await_count == 2
    first, second = agent.exec_command.await_args_list
    assert first == second
    assert agent.get_agent_exec_status.await_args_list[0].args == (100, 123)
    assert agent.get_agent_exec_status.await_args_list[1].args == (100, 124)
    sleeper.assert_awaited_once_with(0.1)


async def test_pending_preparation_cannot_be_treated_as_success(monkeypatch):
    agent = MagicMock(spec=AgentCommands)
    agent.exec_command = AsyncMock(return_value=123)
    agent.get_agent_exec_status = AsyncMock(
        side_effect=[ExecStatus(exited=0), ExecStatus(exited=1, exitcode=0)]
    )
    sleeper = AsyncMock()
    monkeypatch.setattr(guest_exec.asyncio, "sleep", sleeper)

    await GuestExecutor(agent, 100)._prepare_workspace("/tmp/proxmox_exec_123")

    assert agent.exec_command.await_count == 1
    assert agent.get_agent_exec_status.await_count == 2
    sleeper.assert_awaited_once_with(0.1)
