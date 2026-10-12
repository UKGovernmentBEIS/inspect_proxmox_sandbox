"""Verify composed healthcheck retry counts and deadlines without real waits."""

import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest

from proxmoxsandbox._impl.agent_commands import (
    GuestAgentCommunicationTimeoutError,
    GuestExecIncompleteError,
)
from proxmoxsandbox._impl.healthcheck import HealthCheckFailed, HealthCheckRunner
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox.schema import HealthCheck, VmConfig, VmSourceConfig


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        self.now = 1000.0
        super().__init__()
        select = self._selector.select
        self._selector.select = lambda timeout=None: select(0)

    def time(self):
        return self.now

    def _run_once(self):
        if not self._ready and self._scheduled:
            self.now = max(self.now, self._scheduled[0]._when)
        super()._run_once()


@contextmanager
def virtual_clock():
    loop = VirtualLoop()
    try:
        with patch("time.monotonic", loop.time):
            yield loop
    finally:
        loop.close()


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
        upload_delay=0,
        presend_failures=0,
        presend_error=httpx.ConnectError,
        launch_error=None,
        status_error=None,
        returncodes=(0,),
    ):
        self.upload_failures = upload_failures
        self.upload_delay = upload_delay
        self.presend_failures = presend_failures
        self.presend_error = presend_error
        self.launch_error = launch_error
        self.status_error = status_error
        self.returncodes = returncodes
        self.uploads = []
        self.upload_paths = {}
        self.launch_requests = []
        self.launches = []
        self.status_reads = 0
        self.failed_requests = 0
        self.cancelled = False
        self.started = asyncio.Event()

    async def request(self, method, path, *, json=None):
        if path.endswith("/file-write"):
            self.uploads.append(dict(json))
            attempt = self.upload_paths.setdefault(json["file"], 0) + 1
            self.upload_paths[json["file"]] = attempt
            self.started.set()
            try:
                await asyncio.sleep(self.upload_delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            if attempt <= self.upload_failures:
                self.failed_requests += 1
                raise http_failure()
            return None
        if path.endswith("/exec"):
            command = json["command"]
            if command[-1].startswith(("del ", "rm -f ")):
                return {"pid": 456}
            self.launch_requests.append(command)
            if len(self.launch_requests) <= self.presend_failures:
                self.failed_requests += 1
                raise self.presend_error("request not sent")
            self.launches.append(command)
            if self.launch_error is not None:
                self.failed_requests += 1
                raise self.launch_error
            return {"pid": 123}
        if "/exec-status?" in path:
            self.status_reads += 1
            if self.status_error is not None:
                self.failed_requests += 1
                raise self.status_error
            return {"exited": 1}
        raise AssertionError((method, path))

    async def read_file_capped(self, node, vm_id, filepath, count):
        if filepath.endswith("script.returncode"):
            index = min(len(self.launches) - 1, len(self.returncodes) - 1)
            return str(self.returncodes[index]).encode(), False
        return b"", False

    def runner(self, loop, os_type, *, timeout=30, command=("echo", "ready")):
        infra = MagicMock(async_proxmox=self, node="pve")
        config = VmConfig(
            name="dc",
            vm_source_config=VmSourceConfig(existing_vm_template_tag="dc"),
            os_type=os_type,
        )
        execute = InfraCommands._healthcheck_executor(infra, 100, config)
        return HealthCheckRunner(
            HealthCheck(test=command, timeout=timeout),
            execute,
            label="dc",
            clock=loop.time,
        )


@pytest.mark.parametrize("os_type", ["l26", "win11"])
@pytest.mark.parametrize("timeout", [30, 300])
def test_persistent_upload_failure_keeps_25_request_120_second_bound(os_type, timeout):
    guest = Guest(upload_failures=1000)
    with virtual_clock() as loop:
        start = loop.time()
        with pytest.raises(HealthCheckFailed):
            loop.run_until_complete(guest.runner(loop, os_type, timeout=timeout).run())
        assert loop.time() - start == 120

    assert len(guest.uploads) == guest.failed_requests == 25
    assert guest.uploads == [guest.uploads[0]] * 25
    assert guest.launch_requests == []


@pytest.mark.parametrize("os_type", ["l26", "win11"])
@pytest.mark.parametrize("upload_failures", [2, 24])
def test_upload_can_recover_in_place_through_the_last_allowed_attempt(
    os_type, upload_failures
):
    guest = Guest(upload_failures=upload_failures)
    with virtual_clock() as loop:
        start = loop.time()
        loop.run_until_complete(guest.runner(loop, os_type).run())
        assert loop.time() - start == upload_failures * 5

    assert len(guest.uploads) == upload_failures + 1
    assert guest.uploads == [guest.uploads[0]] * (upload_failures + 1)
    assert len(guest.launch_requests) == len(guest.launches) == 1


@pytest.mark.parametrize("os_type", ["l26", "win11"])
def test_successful_chunks_do_not_replenish_upload_failure_budget(os_type):
    guest = Guest(upload_failures=10)
    with virtual_clock() as loop:
        start = loop.time()
        with pytest.raises(HealthCheckFailed):
            loop.run_until_complete(
                guest.runner(loop, os_type, command=("echo", "x" * 100_000)).run()
            )
        assert loop.time() - start == 120

    assert all(".part" in path for path in guest.upload_paths)
    assert list(guest.upload_paths.values()) == [11, 11, 5]
    assert len(guest.uploads) == 27
    assert guest.failed_requests == 25
    assert guest.launch_requests == []


@pytest.mark.parametrize("os_type", ["l26", "win11"])
@pytest.mark.parametrize("presend_failures", [4, 5])
def test_upload_and_unsent_launch_failures_share_one_budget(os_type, presend_failures):
    guest = Guest(upload_failures=4, presend_failures=presend_failures)
    with virtual_clock() as loop:
        start = loop.time()
        if presend_failures == 5:
            with pytest.raises(HealthCheckFailed):
                loop.run_until_complete(guest.runner(loop, os_type).run())
        else:
            loop.run_until_complete(guest.runner(loop, os_type).run())
        assert loop.time() - start == 120

    assert len(guest.uploads) == 25
    assert len(guest.upload_paths) == len(guest.launch_requests) == 5
    assert guest.failed_requests == 20 + presend_failures
    assert len(guest.launches) == (0 if presend_failures == 5 else 1)


@pytest.mark.parametrize("os_type", ["l26", "win11"])
@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
)
def test_definitely_unsent_launch_remains_retryable(os_type, error_type):
    guest = Guest(presend_failures=2, presend_error=error_type)
    with virtual_clock() as loop:
        start = loop.time()
        loop.run_until_complete(guest.runner(loop, os_type).run())
        assert loop.time() - start == 10

    assert len(guest.launch_requests) == len(guest.upload_paths) == 3
    assert len(guest.launches) == 1


@pytest.mark.parametrize("os_type", ["l26", "win11"])
@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("lost response"), http_failure("guest-exec timeout")]
)
def test_uncertain_launch_is_not_retried_on_either_platform(os_type, error):
    guest = Guest(launch_error=error)
    with virtual_clock() as loop:
        with pytest.raises(GuestExecIncompleteError) as exc_info:
            loop.run_until_complete(guest.runner(loop, os_type).run())

    assert len(guest.launch_requests) == len(guest.upload_paths) == 1
    assert exc_info.value.__cause__ is error


@pytest.mark.parametrize("os_type", ["l26", "win11"])
def test_healthcheck_status_reads_do_not_gain_retries(os_type):
    guest = Guest(status_error=httpx.ReadTimeout("lost status response"))
    with virtual_clock() as loop:
        with pytest.raises(GuestExecIncompleteError):
            loop.run_until_complete(guest.runner(loop, os_type).run())

    assert len(guest.launch_requests) == guest.status_reads == 1


@pytest.mark.parametrize("os_type", ["l26", "win11"])
def test_confirmed_unhealthy_resets_the_transport_streak(os_type):
    guest = Guest(upload_failures=22, returncodes=(1, 0))
    with virtual_clock() as loop:
        start = loop.time()
        loop.run_until_complete(guest.runner(loop, os_type).run())
        assert loop.time() - start == 225

    assert guest.failed_requests == 44
    assert len(guest.uploads) == 46
    assert len(guest.launches) == len(guest.upload_paths) == 2


@pytest.mark.parametrize("os_type", ["l26", "win11"])
def test_slow_errors_exhaust_communication_allowance_without_new_probe(os_type):
    guest = Guest(upload_failures=1000, upload_delay=60)
    with virtual_clock() as loop:
        start = loop.time()
        with pytest.raises(HealthCheckFailed) as exc_info:
            loop.run_until_complete(guest.runner(loop, os_type).run())
        assert loop.time() - start == 218

    assert isinstance(exc_info.value.__cause__, GuestAgentCommunicationTimeoutError)
    assert len(guest.uploads) == 4
    assert len(guest.upload_paths) == 1
    assert guest.failed_requests == 3
    assert guest.cancelled
    assert guest.launch_requests == []


@pytest.mark.parametrize("os_type", ["l26", "win11"])
def test_cancellation_does_not_retry_or_launch(os_type):
    guest = Guest(upload_delay=60)

    async def cancel(check):
        task = asyncio.create_task(check.run())
        await guest.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with virtual_clock() as loop:
        start = loop.time()
        loop.run_until_complete(cancel(guest.runner(loop, os_type)))
        assert loop.time() == start

    assert len(guest.uploads) == 1
    assert guest.cancelled
    assert guest.launch_requests == []
