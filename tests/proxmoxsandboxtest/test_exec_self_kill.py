"""An exec command must not be able to kill its own sandbox wrapper (issue #75).

The agent's command used to be spliced inline into the wrapper, so it landed in
the argv of the `timeout` scaffolding process. An in-command `pkill -f <pat>` /
`pgrep -f <pat> | kill` then matched and killed the wrapper, corrupting the
result or leaving no return code for the provider to read.

The fix puts the command in a separate file the wrapper runs as `sh {tmp}cmd`
(contents never appear in any argv), and records a `script.started` sentinel so a
genuinely-killed wrapper (e.g. a broad `pkill -f sh`) is reported to the agent as
a clear, non-fatal error instead of a misleading timeout.

Tiers:
  * pure unit  - assert the command text stays out of the wrapper (no shell).
  * host-level - render and run the wrapper on the test host with a real
                 `pkill`; no Proxmox VM needed (gated on sh/flock/timeout/pkill).
  * mocked     - drive exec() with a mocked guest agent to check the
                 killed-wrapper path returns a clear result rather than raising.
The full QGA-into-a-real-VM path is covered by the opt-in e2e suite.
"""

import asyncio
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment

SENTINEL = "z9_issue75_marker_z9"

_HOST_TOOLS = ["sh", "flock", "timeout", "pkill", "pgrep"]
hostlevel = pytest.mark.skipif(
    any(shutil.which(t) is None for t in _HOST_TOOLS),
    reason=f"needs {', '.join(_HOST_TOOLS)} on the test host",
)


def _make_sandbox(agent_commands=None) -> ProxmoxSandboxEnvironment:
    return ProxmoxSandboxEnvironment(
        infra_commands=MagicMock(),
        agent_commands=agent_commands if agent_commands is not None else MagicMock(),
        ipam_mappings=(),
        vm_id=100,
        all_vm_ids=(100,),
        sdn_zone_id=None,
        instance=None,
        pool_id=None,
        os_type="l26",
    )


# --------------------------------------------------------------------------- #
# Tier 1: pure unit — the command text must stay out of the wrapper argv.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        ["pkill", "-f", SENTINEL],
        ["sh", "-c", f"pkill -f {SENTINEL}"],
        ["bash", "-c", f"kill $(pgrep -f {SENTINEL})"],
    ],
)
def test_command_text_kept_out_of_wrapper(command):
    wrapper, cmd_file = _make_sandbox()._build_shell_script(
        tmp_start="/tmp/t_",
        command=command,
        stdin=None,
        cwd=None,
        env={},
        user=None,
        timeout=30,
    )
    # The regression guard for #75: the agent's pattern is not in the wrapper
    # (so `pkill -f <pat>` can't match the wrapper / timeout scaffolding)...
    assert SENTINEL not in wrapper
    # ...it lives only in the opaque command file.
    assert SENTINEL in cmd_file
    # The wrapper reaches the command only via that file, under timeout.
    assert "timeout -k 5s 30s sh /tmp/t_cmd" in wrapper


def test_started_sentinel_written_before_command():
    wrapper, _ = _make_sandbox()._build_shell_script(
        tmp_start="/tmp/t_",
        command=["true"],
        stdin=None,
        cwd=None,
        env={},
        user=None,
        timeout=None,
    )
    assert "echo -n R > /tmp/t_script.started" in wrapper
    # The sentinel must be written *before* the command, else a killed wrapper
    # can't be told apart from a command that never started.
    assert wrapper.index("echo -n R > /tmp/t_script.started") < wrapper.index(
        "sh /tmp/t_cmd"
    )


def test_simple_command_is_exec_in_cmd_file():
    # No stdin pipe -> exec, so the wrapper's `timeout` keeps the command as its
    # direct child (timeout/SIGKILL semantics unchanged from before the fix).
    _, cmd_file = _make_sandbox()._build_shell_script(
        tmp_start="/tmp/t_",
        command=["cat", "foo"],
        stdin=None,
        cwd=None,
        env={},
        user=None,
        timeout=10,
    )
    assert cmd_file.strip() == "exec cat foo"


# --------------------------------------------------------------------------- #
# Tier 2: host-level — render and run the wrapper with a real pkill.
# --------------------------------------------------------------------------- #


def _write_files(tmp_path: Path, command, timeout=None) -> str:
    tmp_start = f"{tmp_path}/k_"
    wrapper, cmd_file = _make_sandbox()._build_shell_script(
        tmp_start=tmp_start,
        command=command,
        stdin=None,
        cwd=None,
        env={},
        user=None,
        timeout=timeout,
    )
    Path(f"{tmp_start}cmd").write_text(cmd_file)
    Path(f"{tmp_start}script.sh").write_text(wrapper)
    return tmp_start


def _pgrep(pattern: str) -> bool:
    """True if any process matches `pgrep -f <pattern>`."""
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


@hostlevel
async def test_self_pkill_kills_only_target_not_wrapper(tmp_path):
    """`pkill -f <tag>` kills the agent's own tagged process, not the wrapper.

    Post-fix the wrapper survives and records the real return code (0). A
    leftover return code file proves the wrapper ran to completion.
    """
    tag = f"sentinel{uuid.uuid4().hex}"
    # A long-lived process whose own argv contains the unique tag: a copy of
    # `sleep` named after the tag (so `pkill -f <tag>` hits it directly).
    sentinel_bin = tmp_path / tag
    shutil.copy(shutil.which("sleep"), sentinel_bin)
    sentinel = await asyncio.create_subprocess_exec(
        str(sentinel_bin),
        "300",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(60):  # wait until visible to pgrep
            if _pgrep(tag):
                break
            await asyncio.sleep(0.05)
        assert _pgrep(tag), "sentinel did not start"

        tmp_start = _write_files(tmp_path, ["pkill", "-f", tag], timeout=30)
        proc = await asyncio.create_subprocess_exec(
            "sh",
            f"{tmp_start}script.sh",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        # Wrapper completed and recorded the real exit code (0 = pkill matched).
        # Had the wrapper been killed, this file would be absent.
        assert Path(f"{tmp_start}script.returncode").read_text() == "0"
        # The agent's actual target is dead.
        assert not _pgrep(tag)
        # No wrapper / timeout scaffolding was orphaned.
        assert not _pgrep(f"{tmp_start}cmd")
    finally:
        if sentinel.returncode is None:
            sentinel.terminate()
            await sentinel.wait()
        subprocess.run(["pkill", "-f", tag], capture_output=True)


@hostlevel
async def test_killed_wrapper_leaves_started_without_returncode(tmp_path):
    """A wrapper killed mid-command leaves script.started but no returncode.

    That is the invariant exec()'s #75 killed-wrapper detection relies on.
    """
    tmp_start = _write_files(tmp_path, ["sleep", "5"], timeout=None)
    proc = await asyncio.create_subprocess_exec(
        "sh",
        f"{tmp_start}script.sh",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):  # wait until the wrapper has started the command
            if Path(f"{tmp_start}script.started").exists():
                break
            await asyncio.sleep(0.05)
        assert Path(f"{tmp_start}script.started").exists()

        # Simulate a broad pkill hitting the wrapper: kill the outer sh before it
        # can record a return code.
        proc.kill()
        await proc.wait()
        await asyncio.sleep(0.2)

        assert not Path(f"{tmp_start}script.returncode").exists()
        assert Path(f"{tmp_start}script.started").read_text() == "R"
    finally:
        subprocess.run(["pkill", "-f", f"{tmp_start}cmd"], capture_output=True)


# --------------------------------------------------------------------------- #
# Mocked: exec() reports a killed wrapper to the agent (no raise, no fake
# timeout). Validates the #75/3a interpretation without a VM.
# --------------------------------------------------------------------------- #


async def test_exec_reports_killed_wrapper_to_agent():
    agent = AsyncMock()
    agent.exec_command.return_value = {"pid": 123}
    agent.get_agent_exec_status.return_value = {"exited": 1}

    async def fake_read(vm_id, filepath, max_size):
        if filepath.endswith("script.returncode"):
            return {"content": ""}  # never written -> wrapper was killed
        if filepath.endswith("script.started"):
            return {"content": "R"}  # but it had reached the command
        if filepath.endswith("script.stdout"):
            return {"content": "partial output\n"}
        return {"content": ""}

    agent.read_file_or_blank.side_effect = fake_read

    sandbox = _make_sandbox(agent_commands=agent)
    result = await sandbox.exec(["sh", "-c", "pkill -f sh"], timeout=30)

    # Clear, non-fatal result for the agent rather than a raise or a fake 124.
    assert result.success is False
    assert result.returncode == 137
    assert "check the state" in result.stderr.casefold()
    assert "may or may not have executed" in result.stderr.casefold()
    # The agent-facing message must not leak any sandbox / infra detail.
    assert "issue #" not in result.stderr
    assert "wrapper" not in result.stderr.casefold()
    assert "proxmox" not in result.stderr.casefold()
    # Any partial output is still surfaced.
    assert "partial output" in result.stdout
