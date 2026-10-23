"""Task polling keeps its default cadence unless a caller opts into shorter waits."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI


@pytest.mark.parametrize("interval", [None, 0.1, 0.02])
async def test_task_poll_interval_preserves_default(monkeypatch, interval):
    api = AsyncProxmoxAPI("proxmox.test:8006", "test-user", "test-password")
    api.request = AsyncMock(
        side_effect=[{"status": "running"}, {"status": "stopped", "exitstatus": "OK"}]
    )
    sleeps = []

    async def record_sleep(seconds):
        sleeps.append(float(seconds))

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    kwargs = {} if interval is None else {"initial_poll_interval": interval}
    await api.wait_for_task(upid="UPID:test-node:1:1:1:test:100:test-user:", **kwargs)

    assert sleeps == [1.0 if interval is None else interval]
    assert api.request.await_count == 2
