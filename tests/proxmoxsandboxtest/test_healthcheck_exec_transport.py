"""Healthchecks retry uploads, but never replay an unresolved command."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import tenacity

from proxmoxsandbox import _proxmox_sandbox_environment as environment
from proxmoxsandbox._impl import agent_commands
from proxmoxsandbox._impl.agent_commands import AgentCommands, GuestExecIncompleteError
from proxmoxsandbox._impl.deadline import within_budget
from proxmoxsandbox._impl.healthcheck import HealthCheckFailed, HealthCheckRunner
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import HealthCheck, VmConfig, VmSourceConfig


def http_failure(message="guest-file-close failed - got timeout"):
    request = httpx.Request("POST", "https://pve.test/agent/file-write")
    return httpx.HTTPStatusError(
        message, request=request, response=httpx.Response(500, request=request)
    )


class Guest:
    def __init__(
        self,
        *,
        upload_failures=0,
        fault=None,
        returncodes=(0,),
        status_failures=0,
        status_error=None,
    ):
        self.upload_failures = upload_failures
        self.fault = fault
        self.returncodes = returncodes
        self.writes = []
        self.launches = []
        self.status_failures = status_failures
        self.status_error = status_error
        self.status_reads = 0

    async def request(self, method, path, *, json=None):
        if path.endswith("/file-write"):
            self.writes.append(dict(json))
            if self.fault == "upload-wait":
                await asyncio.sleep(60)
            if len(self.writes) <= self.upload_failures:
                raise http_failure()
            return None
        if path.endswith("/exec"):
            command = json["command"]
            if command[:2] in (["cmd.exe", "/c"], ["sh", "-c"]) and command[
                2
            ].startswith(("del ", "rm -f ")):
                if self.fault == "cleanup-wait":
                    await asyncio.sleep(60)
                return {"pid": 456}
            self.launches.append(command)
            if self.fault == "launch-transport":
                raise httpx.ReadTimeout("lost start response")
            if self.fault == "launch-http":
                raise http_failure("guest-exec response lost")
            return {"pid": 123}
        if "/exec-status?" in path:
            self.status_reads += 1
            if self.status_reads <= self.status_failures:
                raise self.status_error
            if self.fault == "status-transport":
                raise httpx.ReadTimeout("lost status response")
            if self.fault == "outer-timeout":
                await asyncio.sleep(60)
            return {"exited": 0 if self.fault == "poll-timeout" else 1}
        raise AssertionError((method, path, json))

    async def read(self, *, vm_id, filepath, count):
        if self.fault == f"{filepath.rsplit('.', 1)[-1]}-wait":
            await asyncio.sleep(60)
        if filepath.endswith("script.returncode"):
            code = self.returncodes[
                min(len(self.launches) - 1, len(self.returncodes) - 1)
            ]
            return str(code).encode(), False
        return b"", False

    def sandbox(self, *, os_type="win11"):
        api = MagicMock(request=AsyncMock(side_effect=self.request))
        agent = AgentCommands(api, "pve", qga_max_retries=1)
        agent.read_file_capped_or_blank = AsyncMock(side_effect=self.read)
        return ProxmoxSandboxEnvironment(
            infra_commands=MagicMock(),
            agent_commands=agent,
            ipam_mappings=(),
            vm_id=130,
            name="dc01",
            all_vms={
                130: VmConfig(
                    name="dc01",
                    vm_source_config=VmSourceConfig(existing_vm_template_tag="dc01"),
                )
            },
            sdn_zone_id=None,
            os_type=os_type,
        )

    def production_executor(self, monkeypatch, *, os_type="win11"):
        monkeypatch.setattr(
            AgentCommands, "read_file_capped_or_blank", AsyncMock(side_effect=self.read)
        )
        infra = MagicMock(
            async_proxmox=MagicMock(request=AsyncMock(side_effect=self.request)),
            node="pve",
        )
        return InfraCommands._healthcheck_executor(
            infra,
            130,
            VmConfig(
                name="dc01",
                vm_source_config=VmSourceConfig(existing_vm_template_tag="dc01"),
                os_type=os_type,
            ),
        )


@pytest.fixture(autouse=True)
def immediate_write_retries(monkeypatch):
    monkeypatch.setattr(agent_commands, "_QGA_RETRY_BASE_DELAY", 0)


def runner(sandbox):
    async def execute(spec):
        return await sandbox.exec(list(spec.test), timeout=spec.timeout)

    return HealthCheckRunner(
        HealthCheck(
            test=("powershell.exe", "-EncodedCommand", "ZQB4AGkAdAAgADAA"),
            timeout=1,
            interval=0.01,
            retries=2,
        ),
        execute,
        label="dc01 (ID=130)",
    )


async def test_upload_retries_identical_path_and_bytes_before_one_launch(monkeypatch):
    guest = Guest(upload_failures=2)
    execute = guest.production_executor(monkeypatch)
    result = await execute(HealthCheck(test=("echo", "test"), timeout=1))
    assert result.success
    assert len(guest.writes) == 3
    assert guest.writes == [guest.writes[0]] * 3
    assert len(guest.launches) == 1
    assert guest.launches[0][-1] == guest.writes[0]["file"]


async def test_exhausted_upload_cannot_launch_partial_script(monkeypatch):
    guest = Guest(upload_failures=agent_commands._QGA_MAX_RETRIES)
    execute = guest.production_executor(monkeypatch)
    with pytest.raises(httpx.HTTPStatusError, match="guest-file-close"):
        await execute(HealthCheck(test=("echo", "test"), timeout=1))
    assert len(guest.writes) == agent_commands._QGA_MAX_RETRIES
    assert guest.writes == [guest.writes[0]] * agent_commands._QGA_MAX_RETRIES
    assert guest.launches == []


@pytest.mark.parametrize("os_type", ["win11", "l26"])
@pytest.mark.parametrize(
    "fault",
    ["upload-wait", "upload-retries", "stdout-wait", "returncode-wait", "cleanup-wait"],
)
async def test_production_healthcheck_communication_wait_is_unreachable(
    monkeypatch, os_type, fault
):
    async def short_budget(operation, seconds):
        return await within_budget(operation, 0.01)

    monkeypatch.setattr(environment, "within_budget", short_budget)
    monkeypatch.setattr(agent_commands, "_QGA_RETRY_BASE_DELAY", 1)
    guest = Guest(
        fault=fault,
        upload_failures=agent_commands._QGA_MAX_RETRIES
        if fault == "upload-retries"
        else 0,
    )
    check = HealthCheckRunner(
        HealthCheck(test=("echo", "test"), timeout=1, retries=1),
        guest.production_executor(monkeypatch, os_type=os_type),
        label="dc01 (ID=130)",
    )

    probe = await check._probe()

    assert len(guest.writes) == 1
    expected_launches = 0 if fault.startswith("upload") else 1
    assert (probe.outcome, len(guest.launches)) == ("unreachable", expected_launches)
    assert guest.status_reads == expected_launches


@pytest.mark.parametrize(
    "fault",
    [
        "launch-transport",
        "launch-http",
        "status-transport",
        "poll-timeout",
        "outer-timeout",
    ],
)
@pytest.mark.parametrize("os_type", ["win11", "l26"])
async def test_indeterminate_execution_aborts_healthcheck_without_relaunch(
    monkeypatch, fault, os_type
):
    guest = Guest(fault=fault)
    if fault == "poll-timeout":
        monkeypatch.setattr(
            environment.tenacity,
            "stop_after_delay",
            lambda _: tenacity.stop_after_attempt(1),
        )
    if fault == "outer-timeout":

        async def short_budget(operation, seconds):
            return await within_budget(operation, 0.02)

        monkeypatch.setattr(environment, "within_budget", short_budget)
    with pytest.raises(GuestExecIncompleteError) as error:
        await runner(guest.sandbox(os_type=os_type)).run()
    assert len(guest.launches) == 1
    assert error.value.vm_id == 130
    assert error.value.pid == (None if fault.startswith("launch") else 123)
    assert error.value.script_path == guest.writes[0]["file"]
    assert error.value.script_path in str(error.value)


@pytest.mark.parametrize("returncode", [1, 124])
async def test_confirmed_terminal_failure_remains_retryable(returncode):
    guest = Guest(returncodes=(returncode, 0))
    await runner(guest.sandbox()).run()
    assert len(guest.launches) == 2


@pytest.mark.parametrize("os_type", ["win11", "l26"])
@pytest.mark.parametrize("returncode", [1, 124])
async def test_production_healthcheck_terminal_failure_consumes_service_retry(
    monkeypatch, os_type, returncode
):
    guest = Guest(returncodes=(returncode,))
    check = HealthCheckRunner(
        HealthCheck(test=("echo", "test"), timeout=1, retries=1),
        guest.production_executor(monkeypatch, os_type=os_type),
        label="dc01 (ID=130)",
    )

    with pytest.raises(HealthCheckFailed, match="failed 1 consecutive"):
        await check.run()
    assert len(guest.launches) == 1


@pytest.mark.parametrize("os_type", ["win11", "l26"])
@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize(
    "status_error",
    [httpx.ReadTimeout("lost status response"), http_failure("lost status response")],
)
async def test_production_healthcheck_retries_known_pid_without_relaunch(
    monkeypatch, os_type, exhausted, status_error
):
    guest = Guest(
        status_failures=agent_commands._QGA_MAX_RETRIES if exhausted else 1,
        status_error=status_error,
    )
    execute = guest.production_executor(monkeypatch, os_type=os_type)
    check = HealthCheckRunner(
        HealthCheck(test=("echo", "test"), timeout=1, interval=0.01, retries=2),
        execute,
        label="dc01 (ID=130)",
    )
    if exhausted:
        with pytest.raises(GuestExecIncompleteError) as error:
            await check.run()
        assert error.value.vm_id == 130
        assert error.value.pid == 123
        assert error.value.script_path == guest.writes[0]["file"]
    else:
        await check.run()
    assert guest.status_reads == (agent_commands._QGA_MAX_RETRIES if exhausted else 2)
    assert len(guest.launches) == 1
