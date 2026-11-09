"""VM start retries the transient failures a concurrent host can produce."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from proxmoxsandbox._impl.async_proxmox import ProxmoxTaskError
from proxmoxsandbox._impl.qemu_commands import QemuCommands

UPID = "UPID:pve1:0002D61D:000A6CF5:6AF1F71F:qmstart:106:root@pam:"


def _no_worker() -> httpx.HTTPStatusError:
    text = "got no worker upid - start worker failed"
    request = httpx.Request("POST", "https://pve/api2/json/x")
    return httpx.HTTPStatusError(
        text, request=request, response=httpx.Response(500, text=text, request=request)
    )


def _qemu(api) -> QemuCommands:
    return QemuCommands(api, "pve1", "local", MagicMock())


@pytest.mark.asyncio
async def test_failed_start_task_is_retried():
    api = AsyncMock()
    api.request = AsyncMock(return_value=UPID)
    api.wait_for_task = AsyncMock(
        side_effect=[
            ProxmoxTaskError(UPID, "start failed: QEMU exited with code 1"),
            None,
        ]
    )
    with patch("proxmoxsandbox._impl.qemu_commands.asyncio.sleep", AsyncMock()):
        await _qemu(api).start(vm_id=106)
    assert api.request.await_count == 2


@pytest.mark.asyncio
async def test_fork_failure_on_start_is_retried():
    api = AsyncMock()
    api.request = AsyncMock(side_effect=[_no_worker(), UPID])
    api.wait_for_task = AsyncMock()
    with patch("proxmoxsandbox._impl.qemu_commands.asyncio.sleep", AsyncMock()):
        await _qemu(api).start(vm_id=106)
    assert api.request.await_count == 2


@pytest.mark.asyncio
async def test_start_gives_up_after_bounded_attempts():
    api = AsyncMock()
    api.request = AsyncMock(return_value=UPID)
    api.wait_for_task = AsyncMock(side_effect=ProxmoxTaskError(UPID, "start failed"))
    with patch("proxmoxsandbox._impl.qemu_commands.asyncio.sleep", AsyncMock()):
        with pytest.raises(ProxmoxTaskError):
            await _qemu(api).start(vm_id=106)
    assert api.request.await_count == 3


@pytest.mark.asyncio
async def test_other_start_errors_are_not_retried():
    api = AsyncMock()
    request = httpx.Request("POST", "https://pve/api2/json/x")
    api.request = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "locked",
            request=request,
            response=httpx.Response(500, text="VM is locked (clone)", request=request),
        )
    )
    with pytest.raises(httpx.HTTPStatusError, match="locked"):
        await _qemu(api).start(vm_id=106)
    assert api.request.await_count == 1
