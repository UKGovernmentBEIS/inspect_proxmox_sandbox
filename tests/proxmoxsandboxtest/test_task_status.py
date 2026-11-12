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


async def test_ok_task_returns_without_logging(caplog: pytest.LogCaptureFixture):
    await _api([{"exitstatus": "OK"}]).wait_for_task(upid=UPID)
    assert not caplog.records


async def test_task_with_warnings_returns_and_is_logged(
    caplog: pytest.LogCaptureFixture,
):
    await _api([{"exitstatus": "WARNINGS: 2"}]).wait_for_task(upid=UPID)
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("WARNING", f"Proxmox task {UPID} finished with WARNINGS: 2")
    ]
