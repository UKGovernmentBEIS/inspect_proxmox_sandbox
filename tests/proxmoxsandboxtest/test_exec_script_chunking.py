"""Chunked guest writes: _upload_exec_script and write_file above the cap.

A single agent/file-write caps base64 `content` at 61440 chars, so a large
command's wrapper script and any write_file payload over 40 KiB must be split
across multiple writes and reassembled inside the guest. These tests drive that
logic with mocked QGA collaborators — no live VM.
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from inspect_ai.util import ExecResult

from proxmoxsandbox import _proxmox_sandbox_environment as mod
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


_LINUX = (False, "/tmp/proxmox_script.sh")
_WINDOWS = (True, r"C:\Windows\Temp\proxmox_script.bat")
_OS_PARAMS = [pytest.param(*_LINUX, id="linux"), pytest.param(*_WINDOWS, id="windows")]


# `== _WRITE_CHUNK_SIZE` is the last size that still fits one write; the chunked
# branch starts at `+ 1`. Both boundaries are exercised so the `<=` can't drift.
@pytest.mark.parametrize(
    "size",
    [mod._WRITE_CHUNK_SIZE - 100, mod._WRITE_CHUNK_SIZE],
    ids=["under", "at-boundary"],
)
@pytest.mark.parametrize("is_windows,script_path", _OS_PARAMS)
async def test_small_script_single_write(
    is_windows: bool, script_path: str, size: int
) -> None:
    env = _make_sandbox()
    env._write_file_only = AsyncMock()  # type: ignore[method-assign]
    env._try_iso_write = AsyncMock(return_value=False)  # type: ignore[method-assign]

    script = "a" * size
    launch = await env._upload_exec_script(script_path, script, is_windows=is_windows)

    env._write_file_only.assert_awaited_once_with(script_path, script.encode("utf-8"))
    assert launch == (
        ["cmd.exe", "/c", script_path] if is_windows else ["sh", script_path]
    )


# `+ 1` is the smallest chunked script (2 parts); 11 chunks forces a two-digit
# part index (`.part10`), which only sorts after `.part09` because the names are
# zero-padded — the property the glob reassembly relies on.
@pytest.mark.parametrize(
    "size",
    [mod._WRITE_CHUNK_SIZE + 1, 130 * 1024, 11 * mod._WRITE_CHUNK_SIZE],
    ids=["just-over", "multi-chunk", "two-digit-index"],
)
@pytest.mark.parametrize("is_windows,script_path", _OS_PARAMS)
async def test_large_script_chunked_and_reassembled(
    is_windows: bool, script_path: str, size: int
) -> None:
    env = _make_sandbox()
    env._write_file_only = AsyncMock()  # type: ignore[method-assign]
    # Force the chunked path: above 128 KiB _upload_exec_script tries the ISO
    # fast path first, which with MagicMock collaborators would error (and flip
    # the disabled flag) instead of cleanly skipping.
    env._try_iso_write = AsyncMock(return_value=False)  # type: ignore[method-assign]

    data = ("a" * size).encode("utf-8")
    expected_chunks = -(-len(data) // mod._WRITE_CHUNK_SIZE)
    assert expected_chunks > 1
    width = len(str(expected_chunks - 1))

    launch = await env._upload_exec_script(
        script_path, data.decode("utf-8"), is_windows=is_windows
    )

    written_paths = [c.args[0] for c in env._write_file_only.await_args_list]
    expected_paths = [
        f"{script_path}.part{i:0{width}d}" for i in range(expected_chunks)
    ]
    assert written_paths == expected_paths
    # The invariant the zero-padding exists for: written (= numeric) order must
    # equal lexical order, so the shell glob reassembles chunks in sequence.
    # Unpadded names would put `.part10` before `.part2` and fail this.
    assert written_paths == sorted(written_paths)
    # Round-tripping the written chunks must reproduce the original script bytes.
    assert b"".join(c.args[1] for c in env._write_file_only.await_args_list) == data

    if is_windows:
        parts = "+".join(f'"{p}"' for p in expected_paths)
        assert launch == [
            "cmd.exe",
            "/c",
            f'copy /b {parts} "{script_path}" >nul && "{script_path}"',
        ]
    else:
        assert launch == [
            "sh",
            "-c",
            f"cat {script_path}.part* > {script_path} && exec sh {script_path}",
        ]


@pytest.mark.parametrize("is_windows,script_path", _OS_PARAMS)
async def test_chunked_upload_does_not_re_enter_exec(
    is_windows: bool, script_path: str
) -> None:
    # Structural guarantee made dynamic: the upload must use only leaf QGA
    # primitives. If anyone later routes it back through self.exec / self.write_file
    # (which re-enter the exec primitive), it recurses without bound — fail loudly.
    env = _make_sandbox()
    env._write_file_only = AsyncMock()  # type: ignore[method-assign]
    env._try_iso_write = AsyncMock(return_value=False)  # type: ignore[method-assign]
    env.exec = AsyncMock()  # type: ignore[method-assign]
    env.write_file = AsyncMock()  # type: ignore[method-assign]

    script = "a" * (130 * 1024)
    await env._upload_exec_script(script_path, script, is_windows=is_windows)

    env.exec.assert_not_called()
    env.write_file.assert_not_called()


@pytest.mark.parametrize("is_windows,script_path", _OS_PARAMS)
async def test_iso_fast_path_launches_single_file(
    is_windows: bool, script_path: str
) -> None:
    # When the ISO fast path writes the whole script, _upload_exec_script must
    # just launch it — no chunking, no per-file writes. The Windows variant
    # forces True only to exercise the branch; IsoWriter itself is Linux-only.
    env = _make_sandbox()
    env._write_file_only = AsyncMock()  # type: ignore[method-assign]
    env._try_iso_write = AsyncMock(return_value=True)  # type: ignore[method-assign]

    script = "a" * (130 * 1024)
    launch = await env._upload_exec_script(script_path, script, is_windows=is_windows)

    env._try_iso_write.assert_awaited_once_with(script_path, script.encode("utf-8"))
    env._write_file_only.assert_not_called()
    assert launch == (
        ["cmd.exe", "/c", script_path] if is_windows else ["sh", script_path]
    )


async def test_write_file_chunks_text_by_encoded_bytes() -> None:
    env = _make_sandbox()
    env.exec = AsyncMock()  # type: ignore[method-assign]
    env._write_file_only = AsyncMock()  # type: ignore[method-assign]
    env._try_iso_write = AsyncMock(return_value=False)  # type: ignore[method-assign]

    payload = "é" * (mod._WRITE_CHUNK_SIZE // 2 + 1)
    data = payload.encode("utf-8")
    assert len(payload) <= mod._WRITE_CHUNK_SIZE < len(data)

    await env.write_file("/tmp/out.txt", payload)

    chunk_writes = [
        call
        for call in env._write_file_only.await_args_list
        if "/chunk_" in call.args[0]
    ]
    assert [call.args[1] for call in chunk_writes] == [
        data[: mod._WRITE_CHUNK_SIZE],
        data[mod._WRITE_CHUNK_SIZE :],
    ]


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
    # write_file reassembles a chunked Linux write by running combine.sh in the
    # guest. Replay that script under the local sh, with the chunk files the
    # mock captured in the place the script expects, so the only thing under
    # test is whether the destination path is data or code to the shell.
    env = _make_sandbox()
    env.exec = AsyncMock()  # type: ignore[method-assign]
    env._write_file_only = AsyncMock()  # type: ignore[method-assign]
    env._try_iso_write = AsyncMock(return_value=False)  # type: ignore[method-assign]

    target = tmp_path / name
    data = bytes(range(256)) * (mod._WRITE_CHUNK_SIZE // 256 + 1)
    assert len(data) > mod._WRITE_CHUNK_SIZE

    await env.write_file(str(target), data)

    written = {c.args[0]: c.args[1] for c in env._write_file_only.await_args_list}
    combine = next(p for p in written if p.endswith("/combine.sh"))
    for path, content in written.items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(
            content.encode() if isinstance(content, str) else content
        )
    try:
        result = subprocess.run(["sh", combine], cwd=tmp_path, capture_output=True)
    finally:
        shutil.rmtree(Path(combine).parent)

    assert not (tmp_path / "pwned").exists(), "the file name ran as a command"
    assert result.returncode == 0, result.stderr.decode()
    assert target.read_bytes() == data


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
        await env.write_file("/tmp/out.bin", b"x" * (mod._WRITE_CHUNK_SIZE + 1))

    # The temp dir is still removed when the combine fails.
    last_cmd = env.exec.await_args_list[-1].kwargs["cmd"]
    assert last_cmd[0] in ("rm", "cmd.exe") and "split_out.bin" in last_cmd[-1]
