"""ISO staging preserves the private execution and recovery contract."""

import asyncio
import hashlib
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._impl.qga_responses import GuestAgentTamperError

from .guest_agent_fixture import Scenario, exercise
from .test_exec_shell_script import LocalGuest
from .test_iso_write import _make_env


@pytest.fixture
def iso_sandbox(monkeypatch, tmp_path):
    guest = LocalGuest()
    sandbox = _make_env()
    sandbox.agent_commands = guest.executor(monkeypatch, tmp_path).agent_commands

    async def copy(vm_id, filepath, contents):
        assert vm_id == sandbox.vm_id
        assert sandbox._iso_write_lock.locked()
        target = Path(filepath)
        assert target.parent.stat().st_mode & 0o7777 == 0o700
        target.write_bytes(contents)

    writer = MagicMock(write_file=AsyncMock(side_effect=copy))
    monkeypatch.setattr(
        "proxmoxsandbox._proxmox_sandbox_environment.IsoWriter",
        MagicMock(return_value=writer),
    )
    return sandbox, guest, writer


@pytest.mark.parametrize("payloads", ["script", "stdin", "both"])
async def test_iso_payloads_preserve_bytes_and_exact_cleanup(iso_sandbox, payloads):
    sandbox, guest, writer = iso_sandbox
    environment = "é" * 25_000 if payloads in ("script", "both") else ""
    data = bytes(range(256)) * 512 if payloads in ("stdin", "both") else b""
    result = await sandbox.exec(
        ["sh", "-c", "printf '%s' \"$PAYLOAD\" | sha256sum; sha256sum"],
        input=data,
        env={"PAYLOAD": environment},
        timeout=5,
    )
    await guest.settle()

    assert result.success
    assert result.stdout == "".join(
        f"{hashlib.sha256(contents).hexdigest()}  -\n"
        for contents in (environment.encode(), data)
    )
    transfers = {
        Path(call.args[1]).name: call.args[2]
        for call in writer.write_file.await_args_list
    }
    assert set(transfers) == {
        name
        for name, needed in (
            ("stdin", payloads in ("stdin", "both")),
            ("script.sh", payloads in ("script", "both")),
        )
        if needed
    }
    if "stdin" in transfers:
        assert transfers["stdin"] is data
    if "script.sh" in transfers:
        assert (
            f"export PAYLOAD={shlex.quote(environment)}"
            in transfers["script.sh"].decode()
        )
    assert len(guest.prepares) == len(guest.launches) == len(guest.cleanups) == 1
    assert all(".part" not in str(path) for path in guest.uploads)
    workspace = Path(guest.reads[0]).parent
    assert {entry.name for entry in workspace.iterdir()} == {
        "claimed",
        "script.returncode",
    }
    expected_files = [workspace / "stdin"] if data else []
    expected_files.extend(
        workspace / name
        for name in (
            "script.sh",
            "script.stdout",
            "script.stderr",
            "script.returncode.tmp",
        )
    )
    assert guest.cleanups == [["rm", "-f", "--", *map(str, expected_files)]]


@pytest.mark.parametrize(
    "data,use_iso",
    [
        (b"x" * (40 * 1024), False),
        (b"x" * (40 * 1024 + 1), True),
        ("é" * (20 * 1024), False),
        ("é" * (20 * 1024 + 1), True),
    ],
    ids=["bytes-below", "bytes-equal", "utf8-below", "utf8-above"],
)
async def test_stdin_uses_the_existing_encoded_threshold(iso_sandbox, data, use_iso):
    sandbox, guest, writer = iso_sandbox
    result = await sandbox.exec(["sha256sum"], input=data, timeout=5)
    await guest.settle()

    encoded = data if isinstance(data, bytes) else data.encode()
    assert result.stdout == f"{hashlib.sha256(encoded).hexdigest()}  -\n"
    if use_iso:
        writer.write_file.assert_awaited_once()
        assert Path(writer.write_file.await_args.args[1]).name == "stdin"
        assert writer.write_file.await_args.args[2] == encoded
    else:
        writer.write_file.assert_not_awaited()
        assert any(path.name == "stdin" for path in guest.uploads)


@pytest.mark.parametrize("copied_bytes", [17, None], ids=["partial", "complete"])
async def test_failed_iso_copy_falls_back_without_replaying(iso_sandbox, copied_bytes):
    sandbox, guest, writer = iso_sandbox
    copy = writer.write_file.side_effect

    async def fail(vm_id, filepath, contents):
        await copy(vm_id, filepath, contents[:copied_bytes])
        raise IOError("copy acknowledgement lost")

    writer.write_file.side_effect = fail
    data = bytes(range(256)) * 512
    result = await sandbox.exec(["sha256sum"], input=data, timeout=5)
    later = await sandbox.exec(["sha256sum"], input=data, timeout=5)
    await guest.settle()

    assert result.stdout == later.stdout == f"{hashlib.sha256(data).hexdigest()}  -\n"
    assert sandbox._iso_fast_path_disabled
    writer.write_file.assert_awaited_once()
    assert len(guest.launches) == len(guest.cleanups) == 2
    assert len([path for path in guest.uploads if ".part" in path.name]) == 8
    assert all(not path.exists() for path in guest.uploads)
    assert not Path(writer.write_file.await_args.args[1]).exists()


@pytest.mark.parametrize("fault", ["cancellation", "malformed_reply"])
async def test_interrupted_iso_staging_cleans_only_its_target(iso_sandbox, fault):
    sandbox, guest, writer = iso_sandbox
    copy = writer.write_file.side_effect
    copying = asyncio.Event()

    async def interrupt(vm_id, filepath, contents):
        await copy(vm_id, filepath, contents[:17])
        copying.set()
        if fault == "malformed_reply":
            raise GuestAgentTamperError(vm_id, "exec", "invalid copy reply")
        await asyncio.Future()

    writer.write_file.side_effect = interrupt
    operation = asyncio.create_task(
        sandbox.exec(["sha256sum"], input=b"x" * 100_000, timeout=5)
    )
    await asyncio.wait_for(copying.wait(), 5)
    if fault == "cancellation":
        operation.cancel()
        error = asyncio.CancelledError
    else:
        error = GuestAgentTamperError
    with pytest.raises(error):
        await operation
    await guest.settle()

    target = Path(writer.write_file.await_args.args[1])
    assert sandbox._iso_fast_path_disabled
    assert guest.uploads == guest.launches == []
    assert guest.cleanups == [["rm", "-f", "--", str(target)]]
    assert list(target.parent.iterdir()) == []


@pytest.mark.parametrize(
    "reply",
    [{"exec_reply": {"pid": "invalid"}}, {"status_reply": {"exited": "invalid"}}],
)
async def test_invalid_iso_reply_prevents_the_payload_launch(reply):
    observed = await exercise(
        Scenario(
            operation="exec",
            command=["echo", "x" * 100_000],
            iso_uploads=True,
            **reply,
        )
    )

    assert observed.kind == "rejected"
    assert observed.launched is False


async def test_exec_does_not_reenter_public_write_file(iso_sandbox):
    sandbox, guest, writer = iso_sandbox
    sandbox.write_file = AsyncMock(side_effect=AssertionError("recursive transfer"))
    result = await sandbox.exec(["sha256sum"], input=b"x" * 100_000, timeout=5)
    await guest.settle()

    assert result.success
    writer.write_file.assert_awaited_once()
    sandbox.write_file.assert_not_awaited()


async def test_exec_and_file_transfer_share_the_iso_lock(iso_sandbox, tmp_path):
    sandbox, guest, writer = iso_sandbox
    copy = writer.write_file.side_effect
    copying = asyncio.Event()
    release = asyncio.Event()

    async def wait_then_copy(vm_id, filepath, contents):
        copying.set()
        await release.wait()
        await copy(vm_id, filepath, contents)

    writer.write_file.side_effect = wait_then_copy
    data = b"x" * 100_000
    execution = asyncio.create_task(sandbox.exec(["sha256sum"], input=data, timeout=5))
    await asyncio.wait_for(copying.wait(), 5)
    destination = tmp_path / "ordinary"
    transfer = asyncio.create_task(sandbox.write_file(str(destination), data))
    try:
        await asyncio.sleep(0)
        writer.write_file.assert_awaited_once()
    finally:
        release.set()
        result, _ = await asyncio.wait_for(asyncio.gather(execution, transfer), 5)
        await guest.settle()

    assert result.success
    assert destination.read_bytes() == data
    assert writer.write_file.await_count == 2


@pytest.mark.parametrize("lost_launches,after_exit", [(1, False), (1, True), (3, True)])
async def test_iso_execution_keeps_its_claim_after_lost_launch_replies(
    iso_sandbox, tmp_path, lost_launches, after_exit
):
    sandbox, guest, writer = iso_sandbox
    guest.lost_launches = lost_launches
    guest.lose_after_exit = after_exit
    marker = tmp_path / "effects"
    result = await sandbox.exec(
        ["sh", "-c", f"sleep .1; echo once >> {shlex.quote(str(marker))}; echo output"],
        env={"PAYLOAD": "x" * 100_000},
        timeout=5,
    )
    await guest.settle()

    assert result.stdout == "output\n"
    assert marker.read_text() == "once\n"
    writer.write_file.assert_awaited_once()
    assert len(guest.launches) == min(lost_launches + 1, 3)
    assert all(command == guest.launches[0] for command in guest.launches)
    assert len(guest.cleanups) == 1
    late = await asyncio.create_subprocess_exec(*guest.launches[0])
    assert await late.wait() == 0
    assert marker.read_text() == "once\n"


async def test_cancelled_iso_execution_retains_recovery_files(iso_sandbox, tmp_path):
    sandbox, guest, writer = iso_sandbox
    marker = tmp_path / "effects"
    operation = asyncio.create_task(
        sandbox.exec(
            ["sh", "-c", f"sleep .1; echo once >> {shlex.quote(str(marker))}"],
            env={"PAYLOAD": "x" * 100_000},
            timeout=5,
        )
    )
    await asyncio.wait_for(guest.launch_started.wait(), 5)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    await guest.settle()

    script = Path(writer.write_file.await_args.args[1])
    assert script.is_file()
    assert (script.parent / "claimed").is_dir()
    assert (script.parent / "script.returncode").read_text() == "0"
    assert marker.read_text() == "once\n"
    assert len(guest.launches) == 1
    assert guest.cleanups == []
