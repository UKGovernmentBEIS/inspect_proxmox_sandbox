"""Default Windows exec must not replay a launch whose response was lost."""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from proxmoxsandbox._impl import agent_commands
from proxmoxsandbox._impl.agent_commands import AgentCommands, GuestExecIncompleteError
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment


class Guest:
    def __init__(self, *, launch_fault=None, upload_failures=0, status_failures=0):
        self.launch_fault = launch_fault
        self.upload_failures = upload_failures
        self.status_failures = status_failures
        self.writes = []
        self.launches = []
        self.cleanups = []
        self.status_reads = 0

    async def request(self, request):
        path = request.url.path
        if path.endswith("/file-write"):
            data = json.loads(request.content)
            self.writes.append(data)
            if len(self.writes) <= self.upload_failures:
                return httpx.Response(500, text="guest-file-close failed - got timeout")
            return httpx.Response(200, json={"data": None})
        if path.endswith("/exec"):
            command = json.loads(request.content)["command"]
            if command[-1].startswith(("del ", "rm -f ")):
                self.cleanups.append(command)
                return httpx.Response(200, json={"data": {"pid": 456}})
            # Record acceptance before dropping the first reply. A second
            # request would be accepted and return a PID, exposing replay.
            self.launches.append(command)
            if len(self.launches) == 1:
                if self.launch_fault == "transport":
                    raise httpx.ReadTimeout("lost start response", request=request)
                if self.launch_fault == "http":
                    return httpx.Response(500, text="guest-exec response lost")
            return httpx.Response(200, json={"data": {"pid": 123}})
        if path.endswith("/exec-status"):
            self.status_reads += 1
            if self.status_reads <= self.status_failures:
                raise httpx.ReadTimeout("lost status response", request=request)
            return httpx.Response(200, json={"data": {"exited": 1}})
        raise AssertionError((request.method, path))

    async def read(self, *, vm_id, filepath, count):
        if filepath.endswith("script.returncode"):
            return b"0", False
        if filepath.endswith("script.stdout"):
            return b"output\n", False
        return b"", False

    def sandbox(self, monkeypatch, *, os_type="win11"):
        client = httpx.AsyncClient
        monkeypatch.setattr(
            "proxmoxsandbox._impl.async_proxmox.httpx.AsyncClient",
            lambda **kwargs: client(
                transport=httpx.MockTransport(self.request), **kwargs
            ),
        )
        api = AsyncProxmoxAPI("pve.test", "test@pam", "unused")
        api.ticket = "test-ticket"
        api.csrf_token = "test-csrf"
        api.ticket_date = time.monotonic()
        # Exercise the normal policy, not healthchecks' qga_max_retries=1.
        agent = AgentCommands(api, "pve")
        agent.read_file_capped_or_blank = AsyncMock(side_effect=self.read)
        return ProxmoxSandboxEnvironment(
            infra_commands=MagicMock(),
            agent_commands=agent,
            ipam_mappings=(),
            vm_id=130,
            name="dc01",
            all_vms={},
            sdn_zone_id=None,
            os_type=os_type,
        )


@pytest.fixture(autouse=True)
def immediate_retries(monkeypatch):
    monkeypatch.setattr(agent_commands, "_QGA_RETRY_BASE_DELAY", 0)


@pytest.mark.parametrize("fault", ["transport", "http"])
@pytest.mark.parametrize("command", [["echo", "test"], ["echo", "x" * 100_000]])
async def test_lost_windows_start_response_is_not_replayed(monkeypatch, fault, command):
    guest = Guest(launch_fault=fault)
    with pytest.raises(
        GuestExecIncompleteError, match="completion is unknown"
    ) as error:
        await guest.sandbox(monkeypatch).exec(command)
    assert len(guest.launches) == 1
    assert guest.status_reads == 0
    assert guest.cleanups == []
    assert error.value.vm_id == 130
    assert error.value.pid is None
    assert error.value.script_path.endswith("script.bat")
    assert error.value.script_path in guest.launches[0][-1]
    assert error.value.script_path in str(error.value)


@pytest.mark.parametrize("upload_failures", [0, 2])
async def test_default_windows_exec_uploads_then_launches_once(
    monkeypatch, upload_failures
):
    guest = Guest(upload_failures=upload_failures)
    result = await guest.sandbox(monkeypatch).exec(["echo", "test"])
    assert result.success
    assert result.returncode == 0
    assert result.stdout == "output\n"
    assert result.stderr == ""
    assert len(guest.writes) == upload_failures + 1
    assert guest.writes == [guest.writes[0]] * len(guest.writes)
    assert len(guest.launches) == 1
    assert guest.launches[0][-1] == guest.writes[0]["file"]
    assert len(guest.cleanups) == 1


async def test_default_windows_exec_never_launches_after_exhausted_upload(monkeypatch):
    guest = Guest(upload_failures=agent_commands._QGA_MAX_RETRIES)
    with pytest.raises(httpx.HTTPStatusError, match="guest-file-close"):
        await guest.sandbox(monkeypatch).exec(["echo", "test"])
    assert len(guest.writes) == agent_commands._QGA_MAX_RETRIES
    assert guest.writes == [guest.writes[0]] * len(guest.writes)
    assert guest.launches == []


async def test_windows_status_retry_does_not_relaunch(monkeypatch):
    guest = Guest(status_failures=1)
    result = await guest.sandbox(monkeypatch).exec(["echo", "test"])
    assert result.success
    assert guest.status_reads == 2
    assert len(guest.launches) == 1


async def test_linux_keeps_retrying_the_same_guarded_script(monkeypatch):
    guest = Guest(launch_fault="transport")
    result = await guest.sandbox(monkeypatch, os_type="l26").exec(["echo", "test"])
    assert result.success
    assert len(guest.launches) == 2
    assert guest.launches[0] == guest.launches[1]
    assert guest.launches[0] == ["sh", guest.writes[0]["file"]]
