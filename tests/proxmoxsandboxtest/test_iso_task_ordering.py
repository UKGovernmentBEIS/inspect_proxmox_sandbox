"""ISO dependencies follow task completion, not HTTP acknowledgement."""

import asyncio
from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._impl import iso_write
from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.deadline import remaining_budget, within_budget
from proxmoxsandbox._impl.iso_write import IsoWriter
from proxmoxsandbox._impl.qga_responses import ExecStatus


class TaskApi(AsyncProxmoxAPI):
    """A task finishes independently of the request that created it."""

    def __init__(self, *, delayed=None, failed=None):
        super().__init__("proxmox.test:8006", "test-user", "test-password")
        self.events = []
        self.tasks = {}
        self.counts = Counter()
        self.waits = Counter()
        self.ready = {
            stage: asyncio.Event() for stage in ("upload", "attach", "detach")
        }
        self.started = {stage: asyncio.Event() for stage in self.ready}
        self.resumed = {stage: asyncio.Event() for stage in self.ready}
        self.cancelled = {stage: asyncio.Event() for stage in self.ready}
        self.budgets = []
        self.failed = failed
        for stage, ready in self.ready.items():
            if stage != delayed:
                ready.set()

    def submit(self, stage):
        self.events.append(stage)
        self.budgets.append((stage, remaining_budget()))
        self.counts[stage] += 1
        upid = f"UPID:test-node:{self.counts[stage]}:1:1:{stage}:100:test-user:"
        self.tasks[upid] = stage
        return upid

    async def upload_file(self, node, storage, file, content_type, filename=None):
        assert Path(file).is_file()
        self.volid = f"{storage}:iso/{filename}"
        return self.submit("upload")

    async def request(self, method, path, **kwargs):
        if method == "POST" and path.endswith("/qemu/100/config"):
            stage = (
                "detach"
                if kwargs["json"] == {"sata5": "none,media=cdrom"}
                else "attach"
            )
            return self.submit(stage)
        if method == "GET" and path.endswith("/status"):
            upid = path.split("/tasks/", 1)[1].removesuffix("/status")
            stage = self.tasks[upid]
            self.events.append(f"status:{stage}")
            return {"exitstatus": f"{stage} failed" if self.failed == stage else "OK"}
        if method == "DELETE" and path.endswith(f"/content/{self.volid}"):
            self.events.append("delete")
            return None
        raise AssertionError(f"Unexpected request: {method} {path}")

    async def wait_for_task(self, *, upid, initial_poll_interval=1.0):
        assert initial_poll_interval == 0.1
        stage = self.tasks[upid]
        self.events.append(f"wait:{stage}")
        self.budgets.append((f"wait:{stage}", remaining_budget()))
        self.waits[upid] += 1
        self.started[stage].set()
        if self.waits[upid] > 1:
            self.resumed[stage].set()
        try:
            await self.ready[stage].wait()
        except asyncio.CancelledError:
            self.events.append(f"cancel:{stage}")
            self.cancelled[stage].set()
            raise
        self.events.append(f"done:{stage}")
        # The shared API waits for termination without rejecting task failure.


def make_writer(api):
    writer = IsoWriter(api, MagicMock(), MagicMock(), "test-node")

    async def copy(vm_id, filepath):
        api.events.append("copy")

    writer._copy_in_guest = AsyncMock(side_effect=copy)
    return writer


async def test_guest_copy_uses_short_initial_poll(monkeypatch):
    agent = MagicMock()
    agent.exec_command = AsyncMock(return_value=123)
    agent.get_agent_exec_status = AsyncMock(
        side_effect=[ExecStatus(exited=0), ExecStatus(exited=1, exitcode=0)]
    )
    writer = IsoWriter(MagicMock(), agent, MagicMock(), "test-node")
    sleeps = []

    async def record_sleep(seconds):
        sleeps.append(float(seconds))

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    await writer._copy_in_guest(100, "/target")

    assert sleeps == [0.1]
    assert agent.get_agent_exec_status.await_count == 2


@pytest.mark.parametrize(
    "stage,dependent", [("upload", "attach"), ("attach", "copy"), ("detach", "delete")]
)
async def test_dependent_operation_waits_for_successful_task(stage, dependent):
    api = TaskApi(delayed=stage)
    writer = make_writer(api)
    transfer = asyncio.create_task(writer.write_file(100, "/target", b"payload"))
    try:
        await asyncio.wait_for(api.started[stage].wait(), 2)
        assert dependent not in api.events
    finally:
        api.ready[stage].set()
        await asyncio.wait_for(transfer, 2)

    assert api.events == [
        "upload",
        "wait:upload",
        "done:upload",
        "status:upload",
        "attach",
        "wait:attach",
        "done:attach",
        "status:attach",
        "copy",
        "detach",
        "wait:detach",
        "done:detach",
        "status:detach",
        "delete",
    ]
    assert writer._pending_task is None


@pytest.mark.parametrize("stage", ["upload", "attach", "detach"])
async def test_failed_task_is_not_retried_or_treated_as_success(stage, caplog):
    api = TaskApi(failed=stage)
    writer = make_writer(api)
    with pytest.raises(IOError):
        await asyncio.wait_for(writer.write_file(100, "/target", b"payload"), 2)

    assert api.counts[stage] == 1
    if stage == "upload":
        assert api.counts["attach"] == 0
        assert api.counts["detach"] == 0
    if stage != "detach":
        assert "copy" not in api.events
        assert api.events[-1] == "delete"
    else:
        assert "delete" not in api.events
        assert f"retaining {api.volid}" in caplog.text


@pytest.mark.parametrize("stage", ["upload", "attach"])
async def test_cancellation_settles_accepted_task_before_cleanup(stage):
    api = TaskApi(delayed=stage)
    writer = make_writer(api)
    transfer = asyncio.create_task(writer.write_file(100, "/target", b"payload"))
    await asyncio.wait_for(api.started[stage].wait(), 2)
    transfer.cancel()
    try:
        await asyncio.wait_for(api.resumed[stage].wait(), 2)
        assert "detach" not in api.events
        assert "delete" not in api.events
        assert "copy" not in api.events
        assert not transfer.done()
    finally:
        api.ready[stage].set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(transfer, 2)

    assert api.counts[stage] == 1
    assert api.events[-1] == "delete"
    if stage == "upload":
        assert "attach" not in api.events
    else:
        assert api.events.index("done:attach") < api.events.index("detach")
    assert writer._pending_task is None


async def test_cancellation_during_final_detach_does_not_delete_iso():
    api = TaskApi(delayed="detach")
    writer = make_writer(api)
    transfer = asyncio.create_task(writer.write_file(100, "/target", b"payload"))
    await asyncio.wait_for(api.started["detach"].wait(), 2)
    transfer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(transfer, 2)

    assert "delete" not in api.events
    assert api.tasks[writer._pending_task] == "detach"


@pytest.mark.parametrize("stage", ["upload", "attach"])
async def test_unresolved_cancelled_task_retains_iso_with_bounded_cleanup(
    stage, monkeypatch, caplog
):
    monkeypatch.setattr(iso_write, "CLEANUP_WAIT_SECONDS", 0.02)
    api = TaskApi(delayed=stage)
    writer = make_writer(api)
    transfer = asyncio.create_task(writer.write_file(100, "/target", b"payload"))
    await asyncio.wait_for(api.started[stage].wait(), 2)
    transfer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(transfer, 2)

    assert api.tasks[writer._pending_task] == stage
    assert api.counts[stage] == 1
    assert "detach" not in api.events
    assert "delete" not in api.events
    assert f"retaining {api.volid}" in caplog.text
    assert writer._pending_task in caplog.text


async def test_final_detach_timeout_retains_iso(monkeypatch, caplog):
    monkeypatch.setattr(iso_write, "CLEANUP_WAIT_SECONDS", 0.02)
    api = TaskApi(delayed="detach")
    writer = make_writer(api)
    with pytest.raises(IOError, match="cleanup was incomplete"):
        await asyncio.wait_for(writer.write_file(100, "/target", b"payload"), 2)

    assert api.counts["detach"] == 1
    assert "delete" not in api.events
    assert api.tasks[writer._pending_task] == "detach"
    assert f"retaining {api.volid}" in caplog.text


@pytest.mark.parametrize("stage", ["upload", "attach"])
async def test_task_wait_inherits_deadline_but_cleanup_gets_own_allowance(stage):
    api = TaskApi(delayed=stage)
    writer = make_writer(api)
    transfer = asyncio.create_task(
        within_budget(writer.write_file(100, "/target", b"payload"), 0.05)
    )
    try:
        await asyncio.wait_for(api.resumed[stage].wait(), 2)
        assert "delete" not in api.events
        budgets = [budget for name, budget in api.budgets if name == f"wait:{stage}"]
        assert 0 < budgets[0] <= 0.05
        assert 100 < budgets[1] <= iso_write.CLEANUP_WAIT_SECONDS
    finally:
        api.ready[stage].set()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(transfer, 2)

    assert api.events[-1] == "delete"


async def test_upload_and_attachment_submission_share_their_task_wait_budget():
    api = TaskApi()
    await make_writer(api).write_file(100, "/target", b"payload")

    budgets = dict(api.budgets)
    assert 0 < budgets["wait:upload"] <= budgets["upload"] <= 600
    assert 0 < budgets["wait:attach"] <= budgets["attach"] <= 30
    assert (
        0
        < budgets["wait:detach"]
        <= budgets["detach"]
        <= iso_write.CLEANUP_WAIT_SECONDS
    )


async def test_guest_copy_retry_waits_for_detach_and_reattach():
    api = TaskApi()
    writer = make_writer(api)
    writer._copy_in_guest.side_effect = [IOError("cannot open cdrom"), None]
    await writer.write_file(100, "/target", b"payload")

    assert api.events == [
        "upload",
        "wait:upload",
        "done:upload",
        "status:upload",
        "attach",
        "wait:attach",
        "done:attach",
        "status:attach",
        "detach",
        "wait:detach",
        "done:detach",
        "status:detach",
        "attach",
        "wait:attach",
        "done:attach",
        "status:attach",
        "detach",
        "wait:detach",
        "done:detach",
        "status:detach",
        "delete",
    ]
    assert writer._copy_in_guest.await_count == 2
