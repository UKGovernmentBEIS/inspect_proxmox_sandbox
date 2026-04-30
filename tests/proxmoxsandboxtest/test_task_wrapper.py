from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._impl.task_wrapper import TaskWrapper


def _make_task_wrapper(cluster_tasks: list[dict]) -> TaskWrapper:
    api = MagicMock()
    api.request = AsyncMock(return_value=cluster_tasks)
    return TaskWrapper(api)


@pytest.mark.asyncio
async def test_running_task_treated_as_incomplete() -> None:
    # No endtime → still running.
    tasks = [
        {
            "upid": "UPID:proxmox:running:1",
            "starttime": 1777541620,
            "type": "qmstart",
        },
    ]
    wrapper = _make_task_wrapper(tasks)
    incomplete = await wrapper.new_incomplete_tasks(pre_existing_incomplete_tasks=[])
    assert len(incomplete) == 1
    assert incomplete[0]["upid"] == "UPID:proxmox:running:1"


@pytest.mark.asyncio
async def test_failed_task_treated_as_complete() -> None:
    # Task finished but failed: endtime is set, status is the error message.
    # Regression: old code treated `status != "OK"` as still running and would
    # wait forever on background failures like a routine apt-get update.
    tasks = [
        {
            "upid": "UPID:proxmox:aptupdate:1",
            "starttime": 1777541624,
            "endtime": 1777541625,
            "status": "command 'apt-get update' failed: exit code 100",
            "type": "aptupdate",
        },
    ]
    wrapper = _make_task_wrapper(tasks)
    incomplete = await wrapper.new_incomplete_tasks(pre_existing_incomplete_tasks=[])
    assert incomplete == []


@pytest.mark.asyncio
async def test_succeeded_task_treated_as_complete() -> None:
    tasks = [
        {
            "upid": "UPID:proxmox:reload:1",
            "starttime": 1777541622,
            "endtime": 1777541623,
            "status": "OK",
            "type": "reloadnetworkall",
        },
    ]
    wrapper = _make_task_wrapper(tasks)
    incomplete = await wrapper.new_incomplete_tasks(pre_existing_incomplete_tasks=[])
    assert incomplete == []


@pytest.mark.asyncio
async def test_pre_existing_incomplete_filtered_out() -> None:
    tasks = [
        {"upid": "UPID:already-running", "starttime": 1, "type": "x"},
        {"upid": "UPID:newly-running", "starttime": 2, "type": "y"},
    ]
    wrapper = _make_task_wrapper(tasks)
    incomplete = await wrapper.new_incomplete_tasks(
        pre_existing_incomplete_tasks=[{"upid": "UPID:already-running"}],
    )
    assert [t["upid"] for t in incomplete] == ["UPID:newly-running"]
