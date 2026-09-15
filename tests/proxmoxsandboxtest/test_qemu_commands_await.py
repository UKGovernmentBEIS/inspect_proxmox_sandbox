"""Unit tests for the VM readiness preconditions in QemuCommands.

`await_vm` is split into `await_running` (Proxmox status poll) and
`await_agent` (guest-agent ping) so the startup scheduler can wait for exactly
what a VM needs: agentless appliances only need to be running.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from proxmoxsandbox._impl.qemu_commands import (
    GuestAgentUnavailableError,
    QemuCommands,
    VmNotRunningError,
)
from proxmoxsandbox.schema import HealthCheck, VmConfig, VmSourceConfig

# Real-time tenacity budget for the "gives up" tests; short but enough for
# several attempts at wait_exponential(min=0.1).
_TINY = 0.3


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


# --- await_running ---------------------------------------------------------------


async def test_await_running_returns_once_running():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(
        side_effect=[{"status": "stopped"}, {"status": "running"}]
    )
    await qemu.await_running(100, timeout=_TINY)
    assert qemu.async_proxmox.request.await_count == 2
    method, path = qemu.async_proxmox.request.await_args_list[0].args
    assert (method, path) == ("GET", "/nodes/pve/qemu/100/status/current")


async def test_await_running_raises_domain_error_on_timeout():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(return_value={"status": "stopped"})
    with pytest.raises(VmNotRunningError, match="VM 100") as exc_info:
        await qemu.await_running(100, timeout=_TINY)
    assert isinstance(exc_info.value, TimeoutError)


async def test_await_running_honours_status_for_wait():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(return_value={"status": "stopped"})
    await qemu.await_running(100, status_for_wait="stopped", timeout=_TINY)


# --- await_agent -----------------------------------------------------------------


async def test_await_agent_returns_on_first_successful_ping():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(side_effect=[_http_500(), None])
    await qemu.await_agent(100, timeout=_TINY)
    assert qemu.async_proxmox.request.await_count == 2
    method, path = qemu.async_proxmox.request.await_args_list[-1].args
    assert (method, path) == ("POST", "/nodes/pve/qemu/100/agent/ping")


async def test_await_agent_raises_actionable_error_on_timeout():
    qemu = _qemu()
    qemu.async_proxmox.request = AsyncMock(side_effect=_http_500)
    with pytest.raises(
        GuestAgentUnavailableError, match="qemu-guest-agent"
    ) as exc_info:
        await qemu.await_agent(100, timeout=_TINY)
    assert "VM 100" in str(exc_info.value)
    assert isinstance(exc_info.value, TimeoutError)


# --- await_vm façade -------------------------------------------------------------


async def test_await_vm_sandbox_runs_both_preconditions():
    qemu = _qemu()
    qemu.await_running = AsyncMock()  # type: ignore[method-assign]
    qemu.await_agent = AsyncMock()  # type: ignore[method-assign]
    await qemu.await_vm(100, is_sandbox=True)
    qemu.await_running.assert_awaited_once()
    qemu.await_agent.assert_awaited_once()


async def test_await_vm_non_sandbox_skips_agent():
    qemu = _qemu()
    qemu.await_running = AsyncMock()  # type: ignore[method-assign]
    qemu.await_agent = AsyncMock()  # type: ignore[method-assign]
    await qemu.await_vm(100, is_sandbox=False)
    qemu.await_running.assert_awaited_once()
    qemu.await_agent.assert_not_awaited()


async def test_await_vm_stopped_never_pings_agent():
    """built_in_vm waits for 'stopped' on a sandbox template; no agent then."""
    qemu = _qemu()
    qemu.await_running = AsyncMock()  # type: ignore[method-assign]
    qemu.await_agent = AsyncMock()  # type: ignore[method-assign]
    await qemu.await_vm(100, is_sandbox=True, status_for_wait="stopped")
    qemu.await_running.assert_awaited_once_with(100, status_for_wait="stopped")
    qemu.await_agent.assert_not_awaited()


# --- other_config_json: agent enablement -----------------------------------------

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
