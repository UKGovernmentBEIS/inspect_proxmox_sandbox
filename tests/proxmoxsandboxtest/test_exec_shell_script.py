"""Run generated Linux wrappers to verify results and lost-response safety."""

import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from proxmoxsandbox._impl import agent_commands
from proxmoxsandbox._impl.agent_commands import AgentCommands, GuestExecIncompleteError
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import VmConfig, VmSourceConfig

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None,
    reason="needs sh on the test host",
)


def _make_sandbox() -> ProxmoxSandboxEnvironment:
    return ProxmoxSandboxEnvironment(
        infra_commands=MagicMock(),
        agent_commands=MagicMock(),
        ipam_mappings=(),
        vm_id=100,
        name="test-sandbox",
        all_vms={
            100: VmConfig(
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
                name="test-sandbox",
            )
        },
        sdn_zone_id=None,
        instance=None,
        pool_id=None,
        os_type="l26",
    )


def _write_script(
    tmp_path: Path, command_body: str, *, timeout: int | None = None
) -> str:
    """Generate the wrapper for `sh -c <command_body>`; return the tmp_start."""
    tmp_start = f"{tmp_path}/e_"
    script = _make_sandbox()._build_shell_script(
        tmp_start=tmp_start,
        command=["sh", "-c", command_body],
        stdin=None,
        cwd=None,
        env={},
        user=None,
        timeout=timeout,
    )
    Path(f"{tmp_start}script.sh").write_text(script)
    return tmp_start


async def _run(tmp_start: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        "sh",
        f"{tmp_start}script.sh",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait()


async def test_single_launch_still_works(tmp_path) -> None:
    marker = tmp_path / "marker"
    tmp_start = _write_script(tmp_path, f"echo side >> {marker}; echo out")

    assert await _run(tmp_start) == 0
    assert marker.read_text() == "side\n"
    assert Path(f"{tmp_start}script.stdout").read_text() == "out\n"
    assert Path(f"{tmp_start}script.returncode").read_text() == "0"


@pytest.mark.skipif(shutil.which("timeout") is None, reason="needs GNU timeout")
async def test_guest_timeout_records_terminal_result(tmp_path) -> None:
    marker = tmp_path / "marker"
    tmp_start = _write_script(tmp_path, f"sleep 60; echo side >> {marker}", timeout=1)

    assert await asyncio.wait_for(_run(tmp_start), timeout=10) == 0
    assert not marker.exists()
    assert Path(f"{tmp_start}script.returncode").read_text() == "124"


async def test_completed_linux_command_is_not_replayed_after_lost_response(
    tmp_path, monkeypatch
) -> None:
    marker = tmp_path / "marker"
    tmp_start = f"{tmp_path}/e_"
    local_script = Path(f"{tmp_start}script.sh")

    async def upload(script_path, script, *, is_windows):
        assert not is_windows
        # Relocate only the wrapper's result files into pytest's owned directory.
        local_script.write_text(
            script.replace(script_path.removesuffix("script.sh"), tmp_start)
        )
        return ["sh", str(local_script)]

    async def launch_then_drop(method, path, *, json):
        assert method == "POST" and path.endswith("/agent/exec")
        assert json["command"] == ["sh", str(local_script)]
        assert await _run(tmp_start) == 0
        # A retry here would acquire even an uncontended flock and run again.
        raise httpx.ReadTimeout("lost response after command completed")

    monkeypatch.setattr(agent_commands, "_QGA_RETRY_BASE_DELAY", 0)
    api = MagicMock(request=AsyncMock(side_effect=launch_then_drop))
    sandbox = _make_sandbox()
    sandbox.agent_commands = AgentCommands(api, "pve", qga_max_retries=2)
    monkeypatch.setattr(sandbox, "_upload_exec_script", AsyncMock(side_effect=upload))

    with pytest.raises(GuestExecIncompleteError) as error:
        await sandbox.exec(["sh", "-c", f"echo side >> {marker}; echo out"])

    api.request.assert_awaited_once()
    assert error.value.pid is None
    assert isinstance(error.value.__cause__, httpx.ReadTimeout)
    assert marker.read_text() == "side\n"
    assert Path(f"{tmp_start}script.stdout").read_text() == "out\n"
    assert Path(f"{tmp_start}script.returncode").read_text() == "0"
