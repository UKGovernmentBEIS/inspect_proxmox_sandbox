"""Healthchecks recover one execution within one transport budget."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from proxmoxsandbox._impl import agent_commands, guest_exec
from proxmoxsandbox._impl.agent_commands import AgentCommands, GuestExecIncompleteError
from proxmoxsandbox._impl.deadline import within_budget
from proxmoxsandbox._impl.guest_exec import GuestExecResultError
from proxmoxsandbox._impl.healthcheck import HealthCheckFailed, HealthCheckRunner
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox.schema import HealthCheck, VmConfig, VmSourceConfig

from .guest_agent_fixture import execution_stage


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
        launch_failures=0,
        launch_error=None,
        fault=None,
        returncodes=(0,),
        status_failures=0,
        status_error=None,
        missing_results=0,
    ):
        self.upload_failures = upload_failures
        self.launch_failures = launch_failures
        self.launch_error = launch_error or httpx.ReadTimeout("lost launch response")
        self.fault = fault
        self.returncodes = returncodes
        self.writes = []
        self.prepares = []
        self.launches = []
        self.script_paths = []
        self.status_failures = status_failures
        self.status_error = status_error
        self.status_reads = 0
        self.result_reads = []
        self.missing_results = missing_results
        self.cleanups = []

    async def request(self, method, path, *, json=None):
        if path.endswith("/file-write"):
            self.writes.append(dict(json))
            if self.fault == "upload-wait":
                await asyncio.sleep(60)
            if len(self.writes) <= self.upload_failures:
                raise http_failure()
            if json["file"] not in self.script_paths:
                self.script_paths.append(json["file"])
            return None
        if path.endswith("/exec"):
            command = json["command"]
            stage = execution_stage(command)
            if stage == "prepare":
                self.prepares.append(command)
                return {"pid": 321}
            if stage == "cleanup":
                self.cleanups.append(command)
                if self.fault == "cleanup-wait":
                    await asyncio.sleep(60)
                return {"pid": 456}
            self.launches.append(command)
            if len(self.launches) <= self.launch_failures:
                raise self.launch_error
            return {"pid": 123}
        if "/exec-status?" in path:
            if path.endswith("pid=321"):
                return {"exited": 1, "exitcode": 0}
            self.status_reads += 1
            if self.status_reads <= self.status_failures:
                raise self.status_error
            if self.fault == "status-wait":
                await asyncio.sleep(60)
            return {"exited": 1}
        raise AssertionError((method, path, json))

    async def read(self, *, vm_id, filepath, count):
        if self.fault == f"{filepath.rsplit('.', 1)[-1]}-wait":
            await asyncio.sleep(60)
        if filepath.endswith("script.returncode"):
            self.result_reads.append(filepath)
            if len(self.result_reads) <= self.missing_results:
                return b"", False
            code = self.returncodes[
                min(len(self.script_paths) - 1, len(self.returncodes) - 1)
            ]
            return str(code).encode(), False
        return b"", False

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
def immediate_normal_retries(monkeypatch):
    monkeypatch.setattr(agent_commands, "_QGA_RETRY_BASE_DELAY", 0)


def runner(execute, *, retries=2):
    return HealthCheckRunner(
        HealthCheck(test=("echo", "test"), timeout=1, interval=0.001, retries=retries),
        execute,
        label="dc01 (ID=130)",
    )


def shorten_budget(monkeypatch):
    async def short_budget(operation, seconds, **kwargs):
        return await within_budget(operation, min(seconds, 0.02), **kwargs)

    monkeypatch.setattr(guest_exec, "within_budget", short_budget)


@pytest.mark.parametrize("os_type", ["win11", "l26"])
async def test_upload_retries_identical_path_and_bytes_before_one_launch(
    monkeypatch, os_type
):
    guest = Guest(upload_failures=2)
    await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert len(guest.writes) == 3
    assert guest.writes == [guest.writes[0]] * 3
    assert len(guest.launches) == 1
    assert guest.writes[0]["file"] in " ".join(guest.launches[0])


@pytest.mark.parametrize("os_type", ["win11", "l26"])
async def test_exhausted_upload_cannot_launch_or_start_a_new_healthcheck(
    monkeypatch, os_type
):
    guest = Guest(upload_failures=agent_commands._QGA_MAX_RETRIES)
    with pytest.raises(HealthCheckFailed, match="could not reach"):
        await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert len(guest.writes) == agent_commands._QGA_MAX_RETRIES
    assert guest.writes == [guest.writes[0]] * agent_commands._QGA_MAX_RETRIES
    assert guest.launches == []


@pytest.mark.parametrize("os_type", ["win11", "l26"])
@pytest.mark.parametrize("fault", ["upload-wait"])
async def test_communication_deadline_cannot_start_a_new_healthcheck(
    monkeypatch, os_type, fault
):
    shorten_budget(monkeypatch)
    guest = Guest(fault=fault)
    with pytest.raises(HealthCheckFailed, match="could not reach"):
        await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert len(guest.writes) == 1
    assert len(guest.launches) == (0 if fault == "upload-wait" else 1)


@pytest.mark.parametrize("os_type", ["win11", "l26"])
@pytest.mark.parametrize("fault", ["stdout-wait", "stderr-wait"])
async def test_output_collection_failure_preserves_terminal_identity(
    monkeypatch, os_type, fault
):
    shorten_budget(monkeypatch)
    guest = Guest(fault=fault)
    with pytest.raises(GuestExecResultError) as error:
        await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert error.value.vm_id == 130
    assert error.value.pid == 123
    assert error.value.returncode == 0
    assert error.value.script_path == guest.writes[0]["file"]
    assert len(guest.launches) == 1
    assert guest.cleanups == []


@pytest.mark.parametrize("os_type", ["win11", "l26"])
@pytest.mark.parametrize("fault", ["status-wait", "returncode-wait"])
async def test_unresolved_execution_deadline_retains_identity(
    monkeypatch, os_type, fault
):
    shorten_budget(monkeypatch)
    guest = Guest(fault=fault)
    with pytest.raises(GuestExecIncompleteError) as error:
        await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert len(guest.launches) == 1
    assert error.value.vm_id == 130
    assert error.value.pid == 123
    assert error.value.script_path == guest.writes[0]["file"]
    assert error.value.script_path in str(error.value)


@pytest.mark.parametrize("os_type", ["win11", "l26"])
async def test_cleanup_deadline_preserves_confirmed_success(monkeypatch, os_type):
    shorten_budget(monkeypatch)
    guest = Guest(fault="cleanup-wait")
    await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert len(guest.launches) == 1
    assert len(guest.cleanups) == 1
    assert len(guest.result_reads) == 1


@pytest.mark.parametrize("os_type", ["win11", "l26"])
@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("lost launch response"),
        http_failure("guest-exec response lost"),
        httpx.ConnectError("request not sent"),
        httpx.ConnectTimeout("connection not established"),
        httpx.PoolTimeout("connection not acquired"),
    ],
)
async def test_guarded_launch_recovery_keeps_one_execution_identity(
    monkeypatch, os_type, error
):
    guest = Guest(launch_failures=1, launch_error=error)
    await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert len(guest.writes) == 1
    assert len(guest.launches) == 2
    assert guest.launches[0] == guest.launches[1]
    assert len(guest.result_reads) == 1


@pytest.mark.parametrize("os_type", ["win11", "l26"])
async def test_exhausted_launch_recovery_cannot_create_a_new_execution(
    monkeypatch, os_type
):
    guest = Guest(launch_failures=agent_commands._QGA_MAX_RETRIES)
    with pytest.raises(GuestExecIncompleteError) as error:
        await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert len(guest.writes) == 1
    assert len(guest.launches) == agent_commands._QGA_MAX_RETRIES
    assert guest.launches == [guest.launches[0]] * agent_commands._QGA_MAX_RETRIES
    assert error.value.pid is None
    assert error.value.script_path == guest.writes[0]["file"]


@pytest.mark.parametrize("returncode", [1, 124])
@pytest.mark.parametrize("os_type", ["win11", "l26"])
async def test_confirmed_terminal_failure_remains_retryable(
    monkeypatch, os_type, returncode
):
    guest = Guest(returncodes=(returncode, 0))
    await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert len(guest.launches) == 2
    assert guest.launches[0] != guest.launches[1]
    assert len(guest.script_paths) == 2


@pytest.mark.parametrize("os_type", ["win11", "l26"])
@pytest.mark.parametrize("returncode", [1, 124])
async def test_terminal_failure_consumes_service_retry(
    monkeypatch, os_type, returncode
):
    guest = Guest(returncodes=(returncode,))
    with pytest.raises(HealthCheckFailed, match="failed 1 consecutive"):
        await runner(
            guest.production_executor(monkeypatch, os_type=os_type), retries=1
        ).run()
    assert len(guest.launches) == 1


@pytest.mark.parametrize("os_type", ["win11", "l26"])
@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize(
    "status_error",
    [httpx.ReadTimeout("lost status response"), http_failure("lost status response")],
)
async def test_status_recovery_keeps_known_pid_without_relaunch(
    monkeypatch, os_type, exhausted, status_error
):
    guest = Guest(
        status_failures=agent_commands._QGA_MAX_RETRIES if exhausted else 1,
        status_error=status_error,
    )
    check = runner(guest.production_executor(monkeypatch, os_type=os_type))
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


@pytest.mark.parametrize("os_type", ["win11", "l26"])
async def test_exited_duplicate_wrapper_waits_for_original_result(monkeypatch, os_type):
    guest = Guest(launch_failures=1, missing_results=1)
    await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert guest.launches == [guest.launches[0]] * 2
    assert guest.status_reads == 1
    assert len(guest.result_reads) == 2
    assert guest.result_reads[0] == guest.result_reads[1]
    assert len(guest.writes) == 1


@pytest.mark.parametrize("os_type", ["win11", "l26"])
async def test_exited_wrapper_without_terminal_record_is_not_a_command_failure(
    monkeypatch, os_type
):
    shorten_budget(monkeypatch)
    guest = Guest(missing_results=100)
    with pytest.raises(GuestExecIncompleteError) as error:
        await runner(guest.production_executor(monkeypatch, os_type=os_type)).run()

    assert error.value.pid == 123
    assert guest.status_reads == 1
    assert len(guest.launches) == 1
    assert guest.cleanups == []
