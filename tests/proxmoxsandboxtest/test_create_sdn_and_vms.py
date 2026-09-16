"""Interleaving tests for dependency-ordered VM creation in create_sdn_and_vms.

Collaborators are mocked; readiness is controlled by per-VM asyncio.Events so
each test decides exactly when a VM becomes ready and asserts what was created
before/after. Cloning stays serial; only readiness waits overlap.
"""

import asyncio
import itertools
from logging import getLogger
from typing import Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from inspect_ai.util import ExecResult

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._impl.qemu_commands import VmNotRunningError
from proxmoxsandbox.schema import (
    HealthCheck,
    ProxmoxSandboxEnvironmentConfig,
    VmConfig,
    VmSourceConfig,
)

Event = Tuple[str, str]
_SOURCE = VmSourceConfig(built_in="ubuntu24.04")


def _vm(name: str, **kwargs) -> VmConfig:
    return VmConfig(vm_source_config=_SOURCE, name=name, **kwargs)


async def _settle() -> None:
    """Let every runnable task make progress until they all block."""
    for _ in range(50):
        await asyncio.sleep(0)


class Harness:
    """Mocked InfraCommands whose readiness is driven by the test."""

    def __init__(self, vms: Tuple[VmConfig, ...], fail_running: Dict[str, Exception]):
        self.vms = vms
        self.events: List[Event] = []
        self.ready = {vm.name: asyncio.Event() for vm in vms}
        self.healthy = {vm.name: asyncio.Event() for vm in vms}
        self.cancelled: List[str] = []
        self._names: Dict[int, str] = {}
        ids = itertools.count(100)

        infra = MagicMock()
        infra.logger = getLogger("test")
        infra.TRACE_NAME = "test"
        infra.sdn_commands.create_sdn = AsyncMock(return_value=(None, {}))
        infra.built_in_vm.known_builtins = AsyncMock(return_value={})

        async def ipam(vnet_aliases, vm_config, sdn_zone_id):
            self.events.append(("ipam", vm_config.name))
            return []

        infra.create_ipam_mappings = AsyncMock(side_effect=ipam)

        async def create(
            sdn_vnet_aliases, vm_config, built_in_vm_ids, wait_until_ready
        ):
            assert wait_until_ready is False
            vm_id = next(ids)
            self._names[vm_id] = vm_config.name
            self.events.append(("create", vm_config.name))
            return vm_id

        infra.qemu_commands.create_and_start_vm = AsyncMock(side_effect=create)
        infra.qemu_commands.register_vm = MagicMock()

        async def await_running(vm_id, **kwargs):
            name = self._names[vm_id]
            try:
                await self.ready[name].wait()
            except asyncio.CancelledError:
                self.cancelled.append(name)
                raise
            if name in fail_running:
                raise fail_running[name]
            self.events.append(("running", name))

        async def await_agent(vm_id, **kwargs):
            self.events.append(("agent", self._names[vm_id]))

        infra.qemu_commands.await_running = AsyncMock(side_effect=await_running)
        infra.qemu_commands.await_agent = AsyncMock(side_effect=await_agent)

        def executor(vm_id, vm_config):
            async def execute(spec: HealthCheck) -> ExecResult[str]:
                await self.healthy[vm_config.name].wait()
                self.events.append(("healthy", vm_config.name))
                return ExecResult(success=True, returncode=0, stdout="", stderr="")

            return execute

        infra._healthcheck_executor = MagicMock(side_effect=executor)

        infra.create_sdn_and_vms = InfraCommands.create_sdn_and_vms.__get__(infra)
        infra._start_vms_in_dependency_order = (
            InfraCommands._start_vms_in_dependency_order.__get__(infra)
        )
        infra._create_vm = InfraCommands._create_vm.__get__(infra)
        infra._await_vm_ready = InfraCommands._await_vm_ready.__get__(infra)
        self.infra = infra

    def start(self) -> "asyncio.Task":
        edges = ProxmoxSandboxEnvironmentConfig(vms_config=self.vms).dependency_edges()
        return asyncio.create_task(
            self.infra.create_sdn_and_vms(
                "abc", sdn_config=None, vms_config=self.vms, dependency_edges=edges
            )
        )

    def created(self) -> List[str]:
        return [name for kind, name in self.events if kind == "create"]

    def index(self, event: Event) -> int:
        return self.events.index(event)


def _harness(*vms: VmConfig, fail_running: Dict[str, Exception] | None = None):
    return Harness(vms, fail_running or {})


# --- legacy parity ----------------------------------------------------------------


async def test_no_dependencies_creates_all_before_any_readiness():
    h = _harness(_vm("a"), _vm("b"), _vm("c"))
    task = h.start()
    await _settle()
    assert h.created() == ["a", "b", "c"]
    for name in ("a", "b", "c"):
        h.ready[name].set()
    result, zone, ipam = await task
    assert [cfg.name for _, cfg in result] == ["a", "b", "c"]
    assert [vm_id for vm_id, _ in result] == [100, 101, 102]
    assert [c.args[0] for c in h.infra.qemu_commands.register_vm.call_args_list] == [
        100,
        101,
        102,
    ]


async def test_ipam_mappings_created_for_all_vms_before_first_create():
    h = _harness(_vm("a"), _vm("b"))
    task = h.start()
    await _settle()
    assert h.events[:2] == [("ipam", "a"), ("ipam", "b")]
    assert h.events[2] == ("create", "a")
    for name in ("a", "b"):
        h.ready[name].set()
    await task


async def test_await_before_next_vm_blocks_later_creates():
    h = _harness(_vm("a", await_before_next_vm=True), _vm("b"), _vm("c"))
    task = h.start()
    await _settle()
    assert h.created() == ["a"]
    h.ready["a"].set()
    await _settle()
    assert h.created() == ["a", "b", "c"]
    h.ready["b"].set()
    h.ready["c"].set()
    await task
    assert h.index(("running", "a")) < h.index(("create", "b"))


# --- depends_on -------------------------------------------------------------------


async def test_forward_reference_creates_dependency_first():
    h = _harness(_vm("a", depends_on=("c",)), _vm("b"), _vm("c"))
    task = h.start()
    await _settle()
    assert h.created() == ["b", "c"]
    h.ready["c"].set()
    await _settle()
    assert h.created() == ["b", "c", "a"]
    h.ready["a"].set()
    h.ready["b"].set()
    result, _, _ = await task
    # Declaration order is preserved in the result regardless of creation order.
    assert [cfg.name for _, cfg in result] == ["a", "b", "c"]


async def test_diamond_waits_for_both_dependencies():
    h = _harness(_vm("dns"), _vm("db"), _vm("app", depends_on=("dns", "db")))
    task = h.start()
    await _settle()
    assert h.created() == ["dns", "db"]
    h.ready["dns"].set()
    await _settle()
    assert h.created() == ["dns", "db"]
    h.ready["db"].set()
    await _settle()
    assert h.created() == ["dns", "db", "app"]
    h.ready["app"].set()
    await task


async def test_dependency_failure_fails_startup_and_cancels_others():
    h = _harness(
        _vm("a"),
        _vm("b", depends_on=("a",)),
        _vm("c"),
        fail_running={"a": VmNotRunningError("VM 100 did not reach running")},
    )
    task = h.start()
    await _settle()
    assert h.created() == ["a", "c"]
    h.ready["a"].set()
    with pytest.raises(VmNotRunningError):
        await task
    assert "b" not in h.created()
    assert h.cancelled == ["c"]


# --- readiness preconditions and healthchecks ----------------------------------------


async def test_agentless_non_sandbox_dependency_only_needs_running():
    h = _harness(_vm("router", is_sandbox=False), _vm("web", depends_on=("router",)))
    task = h.start()
    await _settle()
    h.ready["router"].set()
    await _settle()
    assert h.created() == ["router", "web"]
    h.ready["web"].set()
    await task
    assert ("agent", "router") not in h.events
    assert ("agent", "web") in h.events


async def test_non_sandbox_with_healthcheck_waits_for_agent_then_check():
    h = _harness(
        _vm("svc", is_sandbox=False, healthcheck=HealthCheck(test=("true",))),
    )
    task = h.start()
    await _settle()
    h.ready["svc"].set()
    h.healthy["svc"].set()
    await task
    assert h.index(("running", "svc")) < h.index(("agent", "svc"))
    assert h.index(("agent", "svc")) < h.index(("healthy", "svc"))


async def test_healthcheck_gates_dependants():
    spec = HealthCheck(test=("systemctl", "is-active", "nginx"))
    h = _harness(_vm("a", healthcheck=spec), _vm("b", depends_on=("a",)))
    task = h.start()
    await _settle()
    h.ready["a"].set()
    await _settle()
    assert h.created() == ["a"], "running+agent is not enough when a healthcheck exists"
    h.healthy["a"].set()
    await _settle()
    assert h.created() == ["a", "b"]
    h.ready["b"].set()
    await task
    h.infra._healthcheck_executor.assert_called_once()
    vm_id, vm_config = h.infra._healthcheck_executor.call_args.args
    assert (vm_id, vm_config.healthcheck) == (100, spec)


# --- _healthcheck_executor ---------------------------------------------------------


async def test_healthcheck_executor_runs_test_through_sandbox_exec():
    infra = MagicMock()
    infra._healthcheck_executor = InfraCommands._healthcheck_executor.__get__(infra)
    spec = HealthCheck(test=("systemctl", "is-active", "nginx"), timeout=7)
    vm_config = _vm("svc", os_type="win11", healthcheck=spec)

    env = MagicMock()
    env.exec = AsyncMock(
        return_value=ExecResult(success=True, returncode=0, stdout="", stderr="")
    )
    with (
        patch(
            "proxmoxsandbox._proxmox_sandbox_environment.ProxmoxSandboxEnvironment",
            return_value=env,
        ) as env_cls,
        patch("proxmoxsandbox._impl.agent_commands.AgentCommands"),
    ):
        execute = infra._healthcheck_executor(100, vm_config)
        result = await execute(spec)

    assert result.success
    env.exec.assert_awaited_once_with(["systemctl", "is-active", "nginx"], timeout=7)
    kwargs = env_cls.call_args.kwargs
    assert kwargs["vm_id"] == 100
    assert kwargs["os_type"] == "win11"
