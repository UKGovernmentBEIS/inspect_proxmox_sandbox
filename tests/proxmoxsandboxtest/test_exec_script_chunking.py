"""_upload_exec_script chunks oversize wrapper scripts under the file-write cap.

A single agent/file-write caps base64 `content` at 61440 chars, so a large
command (its wrapper script > ~45 KiB raw) must be split across multiple writes
and reassembled inside the launched command. These tests drive that logic with
mocked QGA collaborators — no live VM.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox import _proxmox_sandbox_environment as mod
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment


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
    env.exec = AsyncMock()  # type: ignore[method-assign]
    env.write_file = AsyncMock()  # type: ignore[method-assign]

    script = "a" * (130 * 1024)
    await env._upload_exec_script(script_path, script, is_windows=is_windows)

    env.exec.assert_not_called()
    env.write_file.assert_not_called()
