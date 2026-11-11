"""wait_for_task reports a failed task instead of returning as if it succeeded."""

from unittest.mock import AsyncMock

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI, ProxmoxTaskError

UPID = "UPID:pve1:0001B52B:00076FF9:6AF1EF78:qmstart:104:root@pam:"


def _api(statuses: list[dict]) -> AsyncProxmoxAPI:
    api = AsyncProxmoxAPI.__new__(AsyncProxmoxAPI)
    api.request = AsyncMock(side_effect=statuses)  # type: ignore[method-assign]
    return api


async def test_failed_task_raises_with_its_exitstatus():
    api = _api([{}, {"exitstatus": "start failed: QEMU exited with code 1"}])
    with pytest.raises(ProxmoxTaskError, match="QEMU exited with code 1") as info:
        await api.wait_for_task(upid=UPID)
    assert info.value.upid == UPID


async def test_ok_and_warnings_tasks_return():
    await _api([{"exitstatus": "OK"}]).wait_for_task(upid=UPID)
    await _api([{"exitstatus": "WARNINGS: 2"}]).wait_for_task(upid=UPID)
