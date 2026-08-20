"""Exec-output reads stay under PVE's agent/file-read cap.

Inspect's sandbox service wraps its request read in
`override_max_exec_output_size(SERVICE_REQUEST_READ_OUTPUT_LIMIT)` — 150 MiB,
nearly 10x PVE's 16 MiB cap on `count`. Passing that straight through makes PVE
400 every poll, which stalls the service (and so `sandbox_agent_bridge`) with no
useful error. These tests drive the read helpers with mocked QGA collaborators.
"""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from inspect_ai.util import OutputLimitExceededError, SandboxEnvironmentLimits
from inspect_ai.util._sandbox.limits import override_max_exec_output_size
from inspect_ai.util._sandbox.service import SERVICE_REQUEST_READ_OUTPUT_LIMIT

from proxmoxsandbox import _proxmox_sandbox_environment as mod
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment


def _make_sandbox(*, truncated: bool = False) -> ProxmoxSandboxEnvironment:
    agent_commands = MagicMock()
    agent_commands.read_file_capped_or_blank = AsyncMock(return_value=(b"0", truncated))
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


def _count(env: ProxmoxSandboxEnvironment) -> int:
    read = cast(AsyncMock, env.agent_commands.read_file_capped_or_blank)
    assert read.await_args is not None, "the read helper was never awaited"
    return int(read.await_args.kwargs["count"])


# The service's override is the real trigger; the default limit is included so
# the clamp can't be "fixed" by hardcoding 16 MiB and losing a lower limit.
@pytest.mark.parametrize(
    "override",
    [None, SERVICE_REQUEST_READ_OUTPUT_LIMIT, mod._PVE_AGENT_FILE_READ_MAX + 1],
    ids=["default", "service-request-limit", "one-over-cap"],
)
async def test_exec_output_read_count_never_exceeds_pve_cap(
    override: int | None,
) -> None:
    env = _make_sandbox()
    if override is None:
        await env._read_exec_output("/tmp/out")
    else:
        with override_max_exec_output_size(override):
            await env._read_exec_output("/tmp/out")

    assert _count(env) <= mod._PVE_AGENT_FILE_READ_MAX


async def test_return_code_read_count_never_exceeds_pve_cap() -> None:
    env = _make_sandbox()
    with override_max_exec_output_size(SERVICE_REQUEST_READ_OUTPUT_LIMIT):
        await env._read_return_code("/tmp/")

    assert _count(env) <= mod._PVE_AGENT_FILE_READ_MAX


async def test_low_limit_is_still_honoured() -> None:
    """The clamp is a ceiling, not a floor — a limit under the cap wins."""
    env = _make_sandbox()
    with override_max_exec_output_size(1024):
        await env._read_exec_output("/tmp/out")

    assert _count(env) == 1024


async def test_truncation_reports_whichever_limit_bit() -> None:
    env = _make_sandbox(truncated=True)

    with override_max_exec_output_size(SERVICE_REQUEST_READ_OUTPUT_LIMIT):
        with pytest.raises(OutputLimitExceededError) as over_cap:
            await env._read_exec_output("/tmp/out")
    assert mod._PVE_AGENT_FILE_READ_MAX_STR in str(over_cap.value)

    with pytest.raises(OutputLimitExceededError) as under_cap:
        await env._read_exec_output("/tmp/out")
    assert SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE_STR in str(under_cap.value)
