from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import OsType


@pytest.mark.parametrize("os_type", ["l26", "win11"])
@pytest.mark.parametrize("raw_code", [b"not-an-integer", b"\xff"])
async def test_invalid_returncode_fails_command_and_allows_next_exec(
    os_type: OsType, raw_code: bytes
) -> None:
    async def read_file(*, vm_id: int, filepath: str, count: int) -> tuple[bytes, bool]:
        if filepath.endswith("script.returncode"):
            return raw_code, False
        if filepath.endswith("script.stdout"):
            return b"command output", False
        return b"command stderr", False

    agent = MagicMock()
    agent.write_file = AsyncMock()
    agent.exec_command = AsyncMock(return_value={"pid": 42})
    agent.get_agent_exec_status = AsyncMock(return_value={"exited": 1, "exitcode": 0})
    agent.read_file_capped_or_blank = AsyncMock(side_effect=read_file)
    env = ProxmoxSandboxEnvironment(
        infra_commands=MagicMock(),
        agent_commands=agent,
        ipam_mappings=(),
        vm_id=100,
        all_vm_ids=(100,),
        sdn_zone_id=None,
        os_type=os_type,
    )

    result = await env.exec(["echo", "example"])

    assert not result.success
    assert result.returncode != 0
    assert result.stdout == "command output"
    assert result.stderr.startswith("command stderr\n")
    assert result.stderr.removeprefix("command stderr\n").strip()

    raw_code = b"0"
    next_result = await env.exec(["echo", "next"])

    assert next_result.success
    assert next_result.returncode == 0
    assert next_result.stdout == "command output"
    assert next_result.stderr == "command stderr"
