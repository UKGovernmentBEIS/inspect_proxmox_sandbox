"""Chunked wrapper staging and guest writes respect PVE's write cap."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from inspect_ai.util import ExecResult

from proxmoxsandbox._impl import exec_linux, exec_windows, guest_exec
from proxmoxsandbox._impl.guest_exec import GuestExecutor
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import OsType, VmConfig, VmSourceConfig


def _make_sandbox(os_type: OsType = "l26") -> ProxmoxSandboxEnvironment:
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
        os_type=os_type,
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

    owned = []
    parts = await executor._stage_file(script_path, script.encode(), owned)

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

    owned = []
    parts = await executor._stage_file(script_path, data, owned)

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
        f"{tmp_start}script{platform.SCRIPT_SUFFIX}", b"a" * (130 * 1024), []
    )

    assert write.await_count > 1
    executor.exec.assert_not_called()


@pytest.mark.parametrize("succeeded", [False, True])
async def test_iso_attempt_registers_exact_target_before_staging(succeeded):
    agent = MagicMock(write_file=AsyncMock())
    filepath = "/tmp/proxmox_exec_test/script.sh"
    data = b"x" * (guest_exec._WRITE_CHUNK_SIZE + 1)
    owned = []

    async def attempt(file, contents):
        assert file == filepath
        assert contents is data
        assert owned == [filepath]
        return succeeded

    attempt_iso = AsyncMock(side_effect=attempt)
    executor = GuestExecutor(agent, 100, try_iso_write=attempt_iso)
    executor.exec = AsyncMock()

    parts = await executor._stage_file(filepath, data, owned)

    attempt_iso.assert_awaited_once_with(filepath, data)
    if succeeded:
        assert parts == []
        assert owned == [filepath]
        agent.write_file.assert_not_awaited()
    else:
        assert parts == [f"{filepath}.part0", f"{filepath}.part1"]
        assert owned == [filepath, *parts]
        assert (
            b"".join(
                call.kwargs["content"] for call in agent.write_file.await_args_list
            )
            == data
        )
    executor.exec.assert_not_awaited()


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


_PAYLOAD = bytes(range(256)) * (guest_exec._WRITE_CHUNK_SIZE // 256 + 1)
assert len(_PAYLOAD) > guest_exec._WRITE_CHUNK_SIZE


async def _chunked_linux_write(target: Path) -> dict[str, str | bytes]:
    """Run write_file against mocks; return each guest file it would write, by path."""
    env = _make_sandbox()
    env.exec = AsyncMock()  # type: ignore[method-assign]
    env._write_file_only = AsyncMock()  # type: ignore[method-assign]
    env._try_iso_write = AsyncMock(return_value=False)  # type: ignore[method-assign]
    await env.write_file(str(target), _PAYLOAD)
    return {c.args[0]: c.args[1] for c in env._write_file_only.await_args_list}


def _replay_combine(
    written: dict[str, str | bytes], cwd: Path
) -> subprocess.CompletedProcess[bytes]:
    """Write the captured guest files locally and run combine.sh under sh.

    The chunk files land where the script expects them, so the only thing under
    test is the script itself.
    """
    combine = next(p for p in written if p.endswith("/combine.sh"))
    try:
        for path, content in written.items():
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(
                content.encode() if isinstance(content, str) else content
            )
        return subprocess.run(["sh", combine], cwd=cwd, capture_output=True)
    finally:
        shutil.rmtree(Path(combine).parent, ignore_errors=True)


# Two names that between them defeat every partial fix: `$(...)` still runs
# inside double quotes, and an apostrophe breaks naive single quotes.
@pytest.mark.parametrize(
    "name",
    ["out$(touch pwned).txt", "it's a file.txt"],
    ids=["command-substitution", "single-quote"],
)
async def test_write_file_combine_script_treats_target_as_data(
    tmp_path: Path, name: str
) -> None:
    target = tmp_path / name
    written = await _chunked_linux_write(target)

    result = _replay_combine(written, cwd=tmp_path)

    assert not (tmp_path / "pwned").exists(), "the file name ran as a command"
    assert result.returncode == 0, result.stderr.decode()
    assert target.read_bytes() == _PAYLOAD


async def test_write_file_combine_script_fails_on_a_missing_chunk(
    tmp_path: Path,
) -> None:
    target = tmp_path / "out.bin"
    written = await _chunked_linux_write(target)
    del written[next(p for p in written if p.endswith("/chunk_0"))]

    result = _replay_combine(written, cwd=tmp_path)

    assert result.returncode != 0


@pytest.mark.parametrize("os_type", ["l26", "win11"], ids=["linux", "windows"])
async def test_write_file_raises_when_combine_script_fails(os_type: OsType) -> None:
    # The combine script is the step that produces the destination file, so its
    # exit code is the write's outcome; mkdir and cleanup results are not.
    env = _make_sandbox(os_type)
    env._write_file_only = AsyncMock()  # type: ignore[method-assign]
    env._try_iso_write = AsyncMock(return_value=False)  # type: ignore[method-assign]

    async def fake_exec(cmd: list[str], **_: object) -> ExecResult[str]:
        if "combine" in cmd[-1]:
            return ExecResult(False, 1, "", "cat: write error: No space left on device")
        return ExecResult(True, 0, "", "")

    env.exec = AsyncMock(side_effect=fake_exec)  # type: ignore[method-assign]

    with pytest.raises(IOError, match="No space left on device"):
        await env.write_file("/tmp/out.bin", b"x" * (guest_exec._WRITE_CHUNK_SIZE + 1))

    # The temp dir is still removed when the combine fails.
    last_cmd = env.exec.await_args_list[-1].kwargs["cmd"]
    assert last_cmd[:2] == ["rm", "-rf"] or last_cmd[-1].startswith("rmdir /s /q")
    assert "split_out.bin" in last_cmd[-1]
