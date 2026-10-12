"""Exercise real healthcheck scheduling and exec retries with a virtual clock."""

import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest

from proxmoxsandbox._impl.agent_commands import (
    GuestExecIncompleteError,
    HealthcheckTransportExhaustedError,
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
        read_failures=0,
        upload_delay=0,
        runtime=0,
        returncodes=(0,),
        stall=False,
    ):
        self.upload_failures = upload_failures
        self.read_failures = read_failures
        self.upload_delay = upload_delay
        self.runtime = runtime
        self.returncodes = returncodes
        self.stall = stall
        self.uploads = []
        self.upload_paths = {}
        self.reads = []
        self.launches = []
        self.current_path = None
        self.launched_path = None
        self.started = asyncio.Event()
        self.cancelled = False

    async def request(self, method, path, *, json=None):
        now = asyncio.get_running_loop().time()
        if path.endswith("/file-write"):
            self.uploads.append(now)
            self.current_path = json["file"]
            attempt = self.upload_paths.setdefault(self.current_path, 0) + 1
            self.upload_paths[self.current_path] = attempt
            self.started.set()
            if self.stall:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            if attempt <= self.upload_failures:
                raise http_failure()
            await asyncio.sleep(self.upload_delay)
            return None
        if path.endswith("/exec"):
            if self.current_path != self.launched_path:
                self.launches.append(now)
                self.launched_path = self.current_path
            return {"pid": 123}
        if "/exec-status?" in path:
            return {"exited": int(now >= self.launches[-1] + self.runtime)}
        raise AssertionError((method, path))

    async def read_file_capped(self, node, vm_id, filepath, count):
        self.reads.append(filepath)
        if len(self.reads) <= self.read_failures:
            raise http_failure("guest-file-read failed - got timeout")
        if filepath.endswith("script.returncode"):
            if asyncio.get_running_loop().time() < self.launches[-1] + self.runtime:
                raise http_failure("failed to open file - No such file")
            index = min(len(self.launches) - 1, len(self.returncodes) - 1)
            return str(self.returncodes[index]).encode(), False
        return b"", False

    def runner(self, loop, *, os_type, spec=None):
        infra = MagicMock(async_proxmox=self, node="pve")
        config = VmConfig(
            name="dc",
            vm_source_config=VmSourceConfig(existing_vm_template_tag="dc"),
            os_type=os_type,
        )
        execute = InfraCommands._healthcheck_executor(infra, 100, config)
        return HealthCheckRunner(
            spec or HealthCheck(test=("echo", "ready")),
            execute,
            label="dc",
            clock=loop.time,
        )


@pytest.mark.parametrize("os_type", ["l26", "win11"])
@pytest.mark.parametrize("timeout", [30, 300])
def test_persistent_upload_failure_has_one_25_request_budget(os_type, timeout):
    guest = Guest(upload_failures=1000)
    with virtual_clock() as loop:
        check = guest.runner(
            loop,
            os_type=os_type,
            spec=HealthCheck(test=("echo", "ready"), timeout=timeout),
        )
        start = loop.time()
        with pytest.raises(HealthCheckFailed) as exc_info:
            loop.run_until_complete(check.run())
        assert loop.time() - start == 120

    assert isinstance(exc_info.value.__cause__, HealthcheckTransportExhaustedError)
    assert guest.uploads == [start + 5 * attempt for attempt in range(25)]
    assert len(guest.upload_paths) == 1
    assert guest.launches == []


@pytest.mark.parametrize("os_type", ["l26", "win11"])
def test_transient_uploads_recover_in_place_at_configured_cadence(os_type):
    guest = Guest(upload_failures=2)
    with virtual_clock() as loop:
        start = loop.time()
        loop.run_until_complete(guest.runner(loop, os_type=os_type).run())
        assert loop.time() - start == 10

    assert guest.uploads == [start, start + 5, start + 10]
    assert len(guest.upload_paths) == 1
    assert guest.launches == [start + 10]


@pytest.mark.parametrize("os_type", ["l26", "win11"])
def test_successful_upload_does_not_reset_result_recovery_budget(os_type):
    guest = Guest(upload_failures=15, read_failures=10)
    with virtual_clock() as loop:
        start = loop.time()
        with pytest.raises((HealthCheckFailed, GuestExecIncompleteError)) as exc_info:
            loop.run_until_complete(guest.runner(loop, os_type=os_type).run())
        assert loop.time() - start == 120

    error = exc_info.value
    while error is not None and not isinstance(
        error, HealthcheckTransportExhaustedError
    ):
        error = error.__cause__
    assert isinstance(error, HealthcheckTransportExhaustedError)
    assert error.failures == 25
    assert len(guest.uploads) == 16
    assert len(guest.reads) == 10
    assert len(guest.launches) == 1
    assert len(guest.upload_paths) == 1


@pytest.mark.parametrize("os_type", ["l26", "win11"])
def test_confirmed_unhealthy_has_fresh_transport_budget_for_next_probe(os_type):
    guest = Guest(upload_failures=22, returncodes=(1, 0))
    with virtual_clock() as loop:
        start = loop.time()
        loop.run_until_complete(guest.runner(loop, os_type=os_type).run())
        assert loop.time() - start == 225

    assert len(guest.uploads) == 46
    assert len(guest.upload_paths) == 2
    assert guest.launches == [start + 110, start + 225]


@pytest.mark.parametrize("os_type", ["l26", "win11"])
def test_late_upload_recovery_leaves_time_for_command(os_type):
    guest = Guest(upload_delay=179, runtime=10)
    with virtual_clock() as loop:
        start = loop.time()
        loop.run_until_complete(guest.runner(loop, os_type=os_type).run())
        assert 189 <= loop.time() - start < 218

    assert guest.launches == [start + 179]


@pytest.mark.parametrize("os_type", ["l26", "win11"])
def test_cancellation_stops_transport_without_launch_or_retry(os_type):
    guest = Guest(stall=True)

    async def cancel(check):
        task = asyncio.create_task(check.run())
        await guest.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with virtual_clock() as loop:
        start = loop.time()
        loop.run_until_complete(cancel(guest.runner(loop, os_type=os_type)))
        assert loop.time() == start

    assert len(guest.uploads) == 1
    assert guest.cancelled
    assert guest.launches == []
