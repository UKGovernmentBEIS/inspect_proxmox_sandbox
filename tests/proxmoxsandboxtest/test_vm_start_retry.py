"""VM start retries the transient failures a concurrent host can produce."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from proxmoxsandbox._impl.async_proxmox import ProxmoxTaskError
from proxmoxsandbox._impl.qemu_commands import QemuCommands

from .proxmox_sandbox_utils import http_500

UPID = "UPID:pve1:0002D61D:000A6CF5:6AF1F71F:qmstart:106:root@pam:"


def _qemu(api) -> QemuCommands:
    return QemuCommands(api, "pve1", "local", MagicMock())


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


async def test_failed_start_task_is_retried():
    api = AsyncMock()
    api.request = AsyncMock(return_value=UPID)
    api.wait_for_task = AsyncMock(
        side_effect=[
            ProxmoxTaskError(UPID, "start failed: QEMU exited with code 1"),
            None,
        ]
    )
    await _qemu(api).start(vm_id=106)
    assert api.request.await_count == 2


async def test_fork_failure_on_start_is_retried():
    api = AsyncMock()
    api.request = AsyncMock(
        side_effect=[http_500("got no worker upid - start worker failed"), UPID]
    )
    api.wait_for_task = AsyncMock()
    await _qemu(api).start(vm_id=106)
    assert api.request.await_count == 2


async def test_start_gives_up_after_bounded_attempts():
    api = AsyncMock()
    api.request = AsyncMock(return_value=UPID)
    api.wait_for_task = AsyncMock(side_effect=ProxmoxTaskError(UPID, "start failed"))
    with pytest.raises(ProxmoxTaskError):
        await _qemu(api).start(vm_id=106)
    assert api.request.await_count == 3


async def test_other_start_errors_are_not_retried():
    api = AsyncMock()
    api.request = AsyncMock(side_effect=http_500("VM is locked (clone)"))
    with pytest.raises(httpx.HTTPStatusError, match="locked"):
        await _qemu(api).start(vm_id=106)
    assert api.request.await_count == 1
