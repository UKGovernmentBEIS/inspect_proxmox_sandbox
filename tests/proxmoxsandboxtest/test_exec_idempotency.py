"""The exec wrapper script must be idempotent under a double launch.

agent/exec is retried on transient errors, so the same script can be launched
more than once for one exec (if the agent received the request but the pid
response was lost). The flock guard in _build_shell_script must ensure the
command then runs exactly once and every launch only finishes once the result
is ready. We test that directly by running the generated script concurrently -
the same condition a lost-response double-launch produces - rather than trying
to reproduce the rare timing.

Runs the generated sh on the test host, so it needs `sh` + `flock` (util-linux).
"""

import asyncio
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None or shutil.which("sh") is None,
    reason="needs sh + flock on the test host",
)


def _make_sandbox() -> ProxmoxSandboxEnvironment:
    return ProxmoxSandboxEnvironment(
        infra_commands=MagicMock(),
        agent_commands=MagicMock(),
        ipam_mappings=(),
        vm_id=100,
        all_vm_ids=(100,),
        sdn_zone_id=None,
        instance=None,
        pool_id=None,
        os_type="l26",
    )


def _write_script(tmp_path: Path, command_body: str) -> str:
    """Generate the wrapper for `sh -c <command_body>`; return the tmp_start."""
    tmp_start = f"{tmp_path}/e_"
    script, cmd_file = _make_sandbox()._build_shell_script(
        tmp_start=tmp_start,
        command=["sh", "-c", command_body],
        stdin=None,
        cwd=None,
        env={},
        user=None,
        timeout=None,
    )
    # The wrapper runs `sh {tmp_start}cmd`, so both files must be on disk.
    Path(f"{tmp_start}cmd").write_text(cmd_file)
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


async def test_concurrent_double_launch_runs_command_once(tmp_path):
    marker = tmp_path / "marker"
    # `echo side >> marker` is an observable side effect, separate from the
    # command's stdout (which the wrapper redirects to script.stdout).
    tmp_start = _write_script(tmp_path, f"echo side >> {marker}; echo out; sleep 0.5")

    rcs = await asyncio.gather(_run(tmp_start), _run(tmp_start))

    assert rcs == [0, 0]
    # Command body ran exactly once despite two concurrent launches.
    assert marker.read_text() == "side\n"
    # Output and return code are intact (not interleaved / duplicated).
    assert Path(f"{tmp_start}script.stdout").read_text() == "out\n"
    assert Path(f"{tmp_start}script.returncode").read_text() == "0"


async def test_single_launch_still_works(tmp_path):
    marker = tmp_path / "marker"
    tmp_start = _write_script(tmp_path, f"echo side >> {marker}; echo out")

    assert await _run(tmp_start) == 0
    assert marker.read_text() == "side\n"
    assert Path(f"{tmp_start}script.stdout").read_text() == "out\n"
    assert Path(f"{tmp_start}script.returncode").read_text() == "0"
