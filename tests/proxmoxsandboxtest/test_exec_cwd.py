"""The working directory of an exec must not depend on which user it runs as.

`su -l` starts a login shell, so without care a command run as one user starts
in that user's home directory while a command run as another starts somewhere
else. Callers that create a directory in one exec and write into it with a
relative path in the next then fail, which is how Inspect's human agent
installs itself.

The su cases are asserted structurally because switching user needs root, but
the directory change itself is executed.
"""

import asyncio
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None, reason="needs sh on the test host"
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


def _script(tmp_start: str, body: str, cwd: str | None, user: str | None) -> str:
    return _make_sandbox()._build_shell_script(
        tmp_start=tmp_start,
        command=["sh", "-c", body],
        stdin=None,
        cwd=cwd,
        env={},
        user=user,
        timeout=None,
    )


async def _run(tmp_start: str, script: str) -> int:
    Path(f"{tmp_start}script.sh").write_text(script)
    proc = await asyncio.create_subprocess_exec(
        "sh",
        f"{tmp_start}script.sh",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait()


async def test_explicit_cwd_is_where_the_command_runs(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    tmp_start = f"{tmp_path}/e_"

    assert await _run(tmp_start, _script(tmp_start, "pwd", str(workdir), None)) == 0

    assert Path(f"{tmp_start}script.stdout").read_text().strip() == str(workdir)


async def test_without_a_user_the_launch_directory_is_kept(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    tmp_start = f"{tmp_path}/e_"
    Path(f"{tmp_start}script.sh").write_text(_script(tmp_start, "pwd", None, None))

    proc = await asyncio.create_subprocess_exec(
        "sh",
        f"{tmp_start}script.sh",
        cwd=str(workdir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await proc.wait() == 0

    assert Path(f"{tmp_start}script.stdout").read_text().strip() == str(workdir)


def test_su_without_a_cwd_restores_the_launch_directory(tmp_path):
    tmp_start = f"{tmp_path}/e_"

    script = _script(tmp_start, "pwd", None, "someone")

    assert f"pwd > {tmp_start}script.cwd\n" in script
    assert script.index(f"pwd > {tmp_start}script.cwd") < script.index("su -l")
    assert f'cd "$(cat {tmp_start}script.cwd)" || exit $?' in script


def test_su_with_a_cwd_uses_it_rather_than_the_launch_directory(tmp_path):
    tmp_start = f"{tmp_path}/e_"

    script = _script(tmp_start, "pwd", "/srv/target", "someone")

    assert "script.cwd" not in script
    assert "cd /srv/target || exit $?" in script


def test_no_user_needs_no_directory_bookkeeping(tmp_path):
    tmp_start = f"{tmp_path}/e_"

    script = _script(tmp_start, "pwd", None, None)

    assert "su -l" not in script
    assert "script.cwd" not in script
