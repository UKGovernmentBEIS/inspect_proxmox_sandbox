"""Unit tests for the VM readiness preconditions in QemuCommands.

`await_vm` is split into `await_running` (Proxmox status poll) and
`await_agent` (guest-agent ping) so the startup scheduler can wait for exactly
what a VM needs: agentless appliances only need to be running.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import tenacity

from proxmoxsandbox._impl.qemu_commands import (
    _POLL_MAX_WAIT,
    _POLL_WAIT,
    GuestAgentUnavailableError,
    QemuCommands,
    VmNotRunningError,
)
from proxmoxsandbox.schema import HealthCheck, VmConfig, VmSourceConfig

# Real-time tenacity budgets. The poll backoff starts at 1 s (multiplier 1; the
# min=0.1 floor never bites) and stop_before_delay refuses a sleep that would
# overrun the budget, so _TINY allows exactly one attempt and _TWO_ATTEMPTS a
# second one after the first 1 s sleep.
_TINY = 0.3
_TWO_ATTEMPTS = 1.5


def _qemu() -> QemuCommands:
    return QemuCommands(
        async_proxmox=MagicMock(),
        node="pve",
        image_storage="local-lvm",
        task_wrapper=MagicMock(),
        storage_commands=MagicMock(),
    )


def _http_500() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://pve/api2/json/x")
    response = httpx.Response(
        500, request=request, text="QEMU guest agent is not running"
    )
    return httpx.HTTPStatusError("500", request=request, response=response)


async def test_await_running_returns_once_running():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(
        side_effect=[{"status": "stopped"}, {"status": "running"}]
    )
    await qemu.await_running(100, label="vm-100 (ID=100)", timeout=_TWO_ATTEMPTS)
    assert qemu.async_proxmox.request.await_count == 2
    method, path = qemu.async_proxmox.request.await_args_list[0].args
    assert (method, path) == ("GET", "/nodes/pve/qemu/100/status/current")


async def test_await_running_raises_domain_error_on_timeout():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(return_value={"status": "stopped"})
    with pytest.raises(VmNotRunningError, match=r"VM vm-100 \(ID=100\)") as exc_info:
        await qemu.await_running(100, label="vm-100 (ID=100)", timeout=_TINY)
    assert isinstance(exc_info.value, TimeoutError)


async def test_await_running_honours_status_for_wait():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(return_value={"status": "stopped"})
    await qemu.await_running(
        100, label="vm-100 (ID=100)", status_for_wait="stopped", timeout=_TINY
    )


async def test_await_agent_returns_on_first_successful_ping():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(side_effect=[_http_500(), None])
    await qemu.await_agent(100, label="vm-100 (ID=100)", timeout=_TWO_ATTEMPTS)
    assert qemu.async_proxmox.request.await_count == 2
    method, path = qemu.async_proxmox.request.await_args_list[-1].args
    assert (method, path) == ("POST", "/nodes/pve/qemu/100/agent/ping")


def test_poll_backoff_is_capped():
    """Uncapped, attempt 14 already waits ~30 s; slow guests need short gaps."""
    waits = []
    for attempt in range(1, 100):
        state = tenacity.RetryCallState(None, None, (), {})
        state.attempt_number = attempt
        waits.append(_POLL_WAIT(state))
    assert waits[:3] == pytest.approx([1.0, 1.3, 1.69])
    assert waits == sorted(waits)
    assert max(waits) == _POLL_MAX_WAIT


async def test_await_agent_sleeps_follow_the_poll_schedule(monkeypatch):
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(side_effect=_http_500)
    with pytest.raises(GuestAgentUnavailableError):
        await qemu.await_agent(100, label="vm-100 (ID=100)", timeout=_TWO_ATTEMPTS)
    assert sleeps == [1.0]
    assert max(sleeps) <= _POLL_MAX_WAIT


async def test_await_agent_raises_actionable_error_on_timeout():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(side_effect=_http_500)
    with pytest.raises(
        GuestAgentUnavailableError, match="qemu-guest-agent"
    ) as exc_info:
        await qemu.await_agent(100, label="vm-100 (ID=100)", timeout=_TINY)
    assert "VM vm-100 (ID=100)" in str(exc_info.value)
    assert isinstance(exc_info.value, TimeoutError)


async def test_await_vm_with_agent_runs_both_preconditions():
    qemu = _qemu()
    qemu.await_running = AsyncMock()  # type: ignore[method-assign]
    qemu.await_agent = AsyncMock()  # type: ignore[method-assign]
    await qemu.await_vm(100, label="vm-100 (ID=100)", requires_guest_agent=True)
    qemu.await_running.assert_awaited_once()
    qemu.await_agent.assert_awaited_once()


async def test_await_vm_without_agent_skips_ping():
    qemu = _qemu()
    qemu.await_running = AsyncMock()  # type: ignore[method-assign]
    qemu.await_agent = AsyncMock()  # type: ignore[method-assign]
    await qemu.await_vm(100, label="vm-100 (ID=100)", requires_guest_agent=False)
    qemu.await_running.assert_awaited_once()
    qemu.await_agent.assert_not_awaited()


async def test_await_vm_stopped_never_pings_agent():
    """built_in_vm waits for 'stopped' on a sandbox template; no agent then."""
    qemu = _qemu()
    qemu.await_running = AsyncMock()  # type: ignore[method-assign]
    qemu.await_agent = AsyncMock()  # type: ignore[method-assign]
    await qemu.await_vm(
        100,
        label="vm-100 (ID=100)",
        requires_guest_agent=True,
        status_for_wait="stopped",
    )
    qemu.await_running.assert_awaited_once_with(
        100, status_for_wait="stopped", label="vm-100 (ID=100)"
    )
    qemu.await_agent.assert_not_awaited()


_SOURCE = VmSourceConfig(built_in="ubuntu24.04")


def test_agent_enabled_for_sandbox():
    json: dict = {}
    _qemu().other_config_json(VmConfig(vm_source_config=_SOURCE), json)
    assert json["agent"] == "enabled=1"
    assert json["sata5"] == "none,media=cdrom"


def test_agent_disabled_for_plain_non_sandbox():
    json: dict = {}
    _qemu().other_config_json(
        VmConfig(vm_source_config=_SOURCE, is_sandbox=False), json
    )
    assert json["agent"] == "enabled=0"
    assert "sata5" not in json


def test_agent_enabled_for_non_sandbox_with_healthcheck():
    """A healthcheck needs QGA; the ISO fast-path CD-ROM stays sandbox-only."""
    json: dict = {}
    _qemu().other_config_json(
        VmConfig(
            vm_source_config=_SOURCE,
            is_sandbox=False,
            healthcheck=HealthCheck(test=("true",)),
        ),
        json,
    )
    assert json["agent"] == "enabled=1"
    assert "sata5" not in json


async def test_vm_bridges_reads_every_nic_regardless_of_option_order():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(
        return_value={
            "net0": "virtio=BC:24:11:3E:C3:BA,bridge=tcc919v0",
            "net1": "bridge=tcc919v1,virtio=BC:24:11:3E:C3:BB,firewall=1",
            "scsi0": "local:100/vm-100-disk-0.qcow2,size=32G",
            "netmask": "unrelated",
        }
    )
    assert await qemu.vm_bridges(100) == {"tcc919v0", "tcc919v1"}
