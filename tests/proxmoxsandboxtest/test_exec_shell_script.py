"""Run the shared executor against actual Linux scripts and lossy QGA replies."""

import asyncio
import base64
import shutil
from pathlib import Path

import httpx
import pytest

from proxmoxsandbox._impl import agent_commands, exec_linux
from proxmoxsandbox._impl.agent_commands import AgentCommands, GuestExecIncompleteError
from proxmoxsandbox._impl.guest_exec import GuestExecResultError, GuestExecutor

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")


def _failure(message: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://pve.test/agent")
    return httpx.HTTPStatusError(
        message, request=request, response=httpx.Response(500, request=request)
    )


class LocalGuest:
    def __init__(self, *, lost_launches=0, lose_after_exit=False, output_failure=False):
        self.lost_launches = lost_launches
        self.lose_after_exit = lose_after_exit
        self.output_failure = output_failure
        self.processes = {}
        self.launches = []
        self.cleanups = []
        self.reads = []
        self.uploads = []
        self.launch_started = asyncio.Event()

    async def request(self, method, path, *, json=None):
        if path.endswith("/file-write"):
            destination = Path(json["file"])
            destination.write_bytes(base64.b64decode(json["content"]))
            self.uploads.append(destination)
            return None
        if path.endswith("/exec"):
            command = json["command"]
            cleanup = command[:2] == ["rm", "-f"]
            (self.cleanups if cleanup else self.launches).append(command)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.processes[process.pid] = process
            if not cleanup:
                self.launch_started.set()
                if len(self.launches) <= self.lost_launches:
                    if self.lose_after_exit:
                        await process.wait()
                    raise httpx.ReadTimeout("lost launch acknowledgement")
            return {"pid": process.pid}
        if "/exec-status?pid=" in path:
            pid = int(path.rsplit("=", 1)[-1])
            process = self.processes[pid]
            if process.returncode is None:
                return {"exited": 0}
            if process.returncode < 0:
                return {"exited": 1, "signal": -process.returncode}
            return {"exited": 1, "exitcode": process.returncode}
        raise AssertionError((method, path))

    async def read_file_capped(self, node, vm_id, filepath, count):
        self.reads.append(filepath)
        if filepath.endswith("script.stdout") and self.output_failure:
            raise _failure("guest-file-read got timeout")
        path = Path(filepath)
        if not path.exists():
            raise _failure("failed to open file - No such file")
        data = path.read_bytes()
        return data[:count], len(data) > count

    def executor(self, monkeypatch, tmp_path, *, retries=3):
        monkeypatch.setattr(exec_linux, "TMP_DIR", str(tmp_path))
        monkeypatch.setattr(agent_commands, "_QGA_RETRY_BASE_DELAY", 0)
        return GuestExecutor(AgentCommands(self, "pve", qga_max_retries=retries), 100)

    async def settle(self):
        await asyncio.gather(*(process.wait() for process in self.processes.values()))


@pytest.mark.parametrize("after_exit", [False, True])
@pytest.mark.parametrize("large", [False, True])
async def test_lost_acknowledgement_recovers_actual_command_once(
    monkeypatch, tmp_path, after_exit, large
):
    guest = LocalGuest(lost_launches=1, lose_after_exit=after_exit)
    marker = tmp_path / "side-effects"
    command = ["sh", "-c", f"sleep .3; echo side >> {marker}; echo output"]
    result = await guest.executor(monkeypatch, tmp_path).exec(
        command, env={"PAYLOAD": "x" * 100_000} if large else None, timeout=5
    )
    await guest.settle()
    assert result.stdout == "output\n"
    assert result.returncode == 0
    assert marker.read_text() == "side\n"
    assert len(guest.launches) == 2
    assert guest.launches[0] == guest.launches[1]
    assert len(guest.cleanups) == 1
    assert any(str(path).endswith(".part0") for path in guest.uploads) is large
    claim = next(tmp_path.glob("*claimed"))
    terminal = next(tmp_path.glob("*script.returncode"))
    assert claim.is_dir()
    assert terminal.read_text() == "0"
    assert not list(tmp_path.glob("*script.stdout"))
    assert not list(tmp_path.glob("*script.sh*"))
    # A launch arriving after result delivery and cleanup still cannot replay.
    late = await asyncio.create_subprocess_exec(*guest.launches[0])
    assert await late.wait() == 0
    assert marker.read_text() == "side\n"


async def test_result_recovers_after_all_launch_acknowledgements_are_lost(
    monkeypatch, tmp_path
):
    guest = LocalGuest(lost_launches=2, lose_after_exit=True)
    marker = tmp_path / "side-effects"
    result = await guest.executor(monkeypatch, tmp_path, retries=2).exec(
        ["sh", "-c", f"echo side >> {marker}"], timeout=5
    )
    await guest.settle()
    assert result.success
    assert len(guest.launches) == 2
    assert marker.read_text() == "side\n"


async def test_lost_output_retains_terminal_identity_and_evidence(
    monkeypatch, tmp_path
):
    guest = LocalGuest(output_failure=True)
    marker = tmp_path / "side-effects"
    with pytest.raises(GuestExecResultError) as raised:
        await guest.executor(monkeypatch, tmp_path, retries=2).exec(
            ["sh", "-c", f"echo side >> {marker}; echo output"], timeout=5
        )
    await guest.settle()
    assert raised.value.returncode == 0
    assert raised.value.pid in guest.processes
    assert raised.value.vm_id == 100
    assert Path(raised.value.script_path).exists()
    assert marker.read_text() == "side\n"
    assert next(tmp_path.glob("*script.stdout")).read_text() == "output\n"
    assert next(tmp_path.glob("*script.returncode")).read_text() == "0"
    assert guest.cleanups == []
    assert len(guest.launches) == 1


async def test_wrapper_death_is_incomplete_not_a_fabricated_exit_code(
    monkeypatch, tmp_path
):
    guest = LocalGuest()
    task = asyncio.create_task(
        guest.executor(monkeypatch, tmp_path).exec(["sleep", ".1"], timeout=1)
    )
    await guest.launch_started.wait()
    while not list(tmp_path.glob("*claimed")):
        await asyncio.sleep(0.001)
    next(iter(guest.processes.values())).kill()
    with pytest.raises(GuestExecIncompleteError) as raised:
        await task
    await guest.settle()
    assert raised.value.vm_id == 100
    assert guest.cleanups == []
    assert next(tmp_path.glob("*claimed")).is_dir()
    assert not list(tmp_path.glob("*script.returncode"))
    await asyncio.sleep(0.1)


async def test_cancellation_retains_execution_without_replaying(monkeypatch, tmp_path):
    guest = LocalGuest()
    marker = tmp_path / "side-effects"
    task = asyncio.create_task(
        guest.executor(monkeypatch, tmp_path).exec(
            ["sh", "-c", f"sleep .3; echo side >> {marker}"], timeout=5
        )
    )
    await guest.launch_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await guest.settle()
    assert len(guest.launches) == 1
    assert marker.read_text() == "side\n"
    assert next(tmp_path.glob("*claimed")).is_dir()
    assert next(tmp_path.glob("*script.returncode")).read_text() == "0"
    assert guest.cleanups == []
