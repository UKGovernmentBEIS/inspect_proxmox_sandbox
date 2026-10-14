"""Wrapper staging respects PVE's write cap and defers assembly to the claim."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._impl import exec_linux, exec_windows, guest_exec
from proxmoxsandbox._impl.guest_exec import GuestExecutor
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import VmConfig, VmSourceConfig


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


def _make_executor(os_type: str):
    agent = MagicMock(write_file=AsyncMock())
    return GuestExecutor(agent, 100, os_type), agent.write_file


_OS_PARAMS = [
    pytest.param("l26", "/tmp/proxmox_exec_test/", exec_linux, id="linux"),
    pytest.param(
        "win11", "C:\\Windows\\Temp\\proxmox_exec_test\\", exec_windows, id="windows"
    ),
]


@pytest.mark.parametrize(
    "size",
    [guest_exec._WRITE_CHUNK_SIZE - 100, guest_exec._WRITE_CHUNK_SIZE],
    ids=["under", "at-boundary"],
)
@pytest.mark.parametrize("os_type,tmp_start,platform", _OS_PARAMS)
async def test_small_script_single_write(os_type, tmp_start, platform, size):
    executor, write = _make_executor(os_type)
    script_path = f"{tmp_start}script{platform.SCRIPT_SUFFIX}"
    script = "a" * size

    parts, owned = await executor._stage_file(script_path, script.encode())

    write.assert_awaited_once_with(
        vm_id=100, filepath=script_path, content=script.encode()
    )
    assert parts == []
    assert owned == [script_path]


@pytest.mark.parametrize(
    "size",
    [guest_exec._WRITE_CHUNK_SIZE + 1, 130 * 1024, 11 * guest_exec._WRITE_CHUNK_SIZE],
    ids=["just-over", "multi-chunk", "two-digit-index"],
)
@pytest.mark.parametrize("os_type,tmp_start,platform", _OS_PARAMS)
async def test_large_script_chunked_and_reassembled(os_type, tmp_start, platform, size):
    executor, write = _make_executor(os_type)
    script_path = f"{tmp_start}script{platform.SCRIPT_SUFFIX}"
    data = ("a" * size).encode()
    expected_chunks = -(-len(data) // guest_exec._WRITE_CHUNK_SIZE)
    assert expected_chunks > 1
    width = len(str(expected_chunks - 1))
    expected_paths = [
        f"{script_path}.part{i:0{width}d}" for i in range(expected_chunks)
    ]

    parts, owned = await executor._stage_file(script_path, data)

    written_paths = [c.kwargs["filepath"] for c in write.await_args_list]
    assert written_paths == expected_paths
    assert b"".join(c.kwargs["content"] for c in write.await_args_list) == data
    assert all(c.kwargs["vm_id"] == 100 for c in write.await_args_list)
    assert all(
        len(c.kwargs["content"]) <= guest_exec._WRITE_CHUNK_SIZE
        for c in write.await_args_list
    )
    # The guarded platform launch owns assembly; staging cannot run a separate
    # assembly command before the atomic execution claim.
    assert parts == expected_paths
    assert owned == [script_path, *expected_paths]
    executor.agent_commands.exec_command.assert_not_called()
    executor.agent_commands.exec_command_once.assert_not_called()


@pytest.mark.parametrize("os_type,tmp_start,platform", _OS_PARAMS)
async def test_chunked_upload_does_not_re_enter_exec(os_type, tmp_start, platform):
    executor, write = _make_executor(os_type)
    executor.exec = AsyncMock()

    await executor._stage_file(
        f"{tmp_start}script{platform.SCRIPT_SUFFIX}", b"a" * (130 * 1024)
    )

    assert write.await_count > 1
    executor.exec.assert_not_called()


async def test_write_file_chunks_text_by_encoded_bytes() -> None:
    env = _make_sandbox()
    env.exec = AsyncMock()  # type: ignore[method-assign]
    env._write_file_only = AsyncMock()  # type: ignore[method-assign]
    env._try_iso_write = AsyncMock(return_value=False)  # type: ignore[method-assign]

    payload = "é" * (guest_exec._WRITE_CHUNK_SIZE // 2 + 1)
    data = payload.encode("utf-8")
    assert len(payload) <= guest_exec._WRITE_CHUNK_SIZE < len(data)

    await env.write_file("/tmp/out.txt", payload)

    chunk_writes = [
        call
        for call in env._write_file_only.await_args_list
        if "/chunk_" in call.args[0]
    ]
    assert [call.args[1] for call in chunk_writes] == [
        data[: guest_exec._WRITE_CHUNK_SIZE],
        data[guest_exec._WRITE_CHUNK_SIZE :],
    ]
