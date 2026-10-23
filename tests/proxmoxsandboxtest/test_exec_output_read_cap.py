"""Exec-output reads stay under PVE's agent/file-read cap.

`sandbox_service()` wraps every request read in
`override_max_exec_output_size(SERVICE_REQUEST_READ_OUTPUT_LIMIT)` — 150 MiB,
nearly 10x PVE's 16 MiB cap on `count`. Passing that straight through makes PVE
400 on each poll, so no request is ever read, whatever its size. These tests
drive the read helpers with mocked QGA collaborators.
"""

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from inspect_ai.util import OutputLimitExceededError, SandboxEnvironmentLimits
from inspect_ai.util._sandbox.limits import override_max_exec_output_size
from inspect_ai.util._sandbox.service import SERVICE_REQUEST_READ_OUTPUT_LIMIT

from proxmoxsandbox._impl import guest_exec as mod
from proxmoxsandbox._impl.guest_exec import GuestExecutor


def _make_executor(*, truncated: bool = False) -> GuestExecutor:
    agent_commands = MagicMock()
    agent_commands.read_file_capped_or_blank = AsyncMock(return_value=(b"0", truncated))
    return GuestExecutor(agent_commands, vm_id=100, os_type="l26")


def _count(executor: GuestExecutor) -> int:
    read = cast(AsyncMock, executor.agent_commands.read_file_capped_or_blank)
    assert read.await_args is not None, "the read helper was never awaited"
    return int(read.await_args.kwargs["count"])


# The service's override is the trigger; the default limit is included so
# the clamp can't be "fixed" by hardcoding 16 MiB and losing a lower limit.
@pytest.mark.parametrize(
    "override",
    [None, SERVICE_REQUEST_READ_OUTPUT_LIMIT, mod._PVE_FILE_READ_MAX + 1],
    ids=["default", "service-request-limit", "one-over-cap"],
)
async def test_exec_output_read_count_never_exceeds_pve_cap(
    override: int | None,
) -> None:
    executor = _make_executor()
    if override is None:
        await executor._read_exec_output("/tmp/out")
    else:
        with override_max_exec_output_size(override):
            await executor._read_exec_output("/tmp/out")

    assert _count(executor) <= mod._PVE_FILE_READ_MAX


async def test_return_code_read_count_never_exceeds_pve_cap() -> None:
    executor = _make_executor()
    with override_max_exec_output_size(SERVICE_REQUEST_READ_OUTPUT_LIMIT):
        await executor._read_return_code("/tmp/")

    assert _count(executor) <= mod._PVE_FILE_READ_MAX


async def test_low_limit_is_still_honoured() -> None:
    """The clamp is a ceiling, not a floor — a limit under the cap wins."""
    executor = _make_executor()
    with override_max_exec_output_size(1024):
        await executor._read_exec_output("/tmp/out")

    assert _count(executor) == 1024


async def test_truncation_reports_whichever_limit_bit() -> None:
    executor = _make_executor(truncated=True)

    with override_max_exec_output_size(SERVICE_REQUEST_READ_OUTPUT_LIMIT):
        with pytest.raises(OutputLimitExceededError) as over_cap:
            await executor._read_exec_output("/tmp/out")
    assert mod._PVE_FILE_READ_MAX_STR in str(over_cap.value)

    with pytest.raises(OutputLimitExceededError) as under_cap:
        await executor._read_exec_output("/tmp/out")
    assert SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE_STR in str(under_cap.value)


async def test_missing_terminal_record_is_unknown_without_a_local_retry() -> None:
    executor = _make_executor()
    read = cast(AsyncMock, executor.agent_commands.read_file_capped_or_blank)
    read.return_value = (b"", False)

    assert await executor._read_return_code("/tmp/execution_") is None

    read.assert_awaited_once()
    assert read.await_args is not None
    assert read.await_args.kwargs["filepath"] == "/tmp/execution_script.returncode"


@pytest.mark.parametrize(
    "raw,truncated", [(b"0", True), (b"0\n1", False), (b"not-a-code", False)]
)
async def test_terminal_record_must_be_a_complete_integer(raw, truncated) -> None:
    executor = _make_executor()
    read = cast(AsyncMock, executor.agent_commands.read_file_capped_or_blank)
    read.return_value = (raw, truncated)

    with pytest.raises(ValueError, match="100"):
        await executor._read_return_code("/tmp/execution_")
