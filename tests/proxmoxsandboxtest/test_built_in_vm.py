"""Rebuilds the built-in templates from their upstream images.

The module removes only the templates it is about to rebuild and leaves the
results in place, so the rest of the suite (which calls ensure_exists on
first use) runs against them instead of building its own. The rebuilds run
concurrently: each is mostly waiting on the guest's apt run, so the module
costs about as much as its slowest build.

Kali's kali-linux-everything install dwarfs the other two, so it is only
rebuilt here under --runslow. Without it, a host that has no Kali template
still builds one (through the same code) the first time a test asks for it.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox.schema import _load_single_instance_from_env

pytestmark = pytest.mark.req_proxmox


def _built_ins(config: pytest.Config) -> list[str]:
    fast = ["ubuntu24.04", "debian13"]
    return fast + (["kali2025.4"] if config.getoption("--runslow") else [])


@dataclass(frozen=True)
class Rebuild:
    built_ins: list[str]
    new_vms: list[dict[str, Any]]
    """VMs present after the rebuild that were not present before it."""


@pytest.fixture(scope="module")
async def rebuild(request: pytest.FixtureRequest) -> Rebuild:
    built_ins = _built_ins(request.config)
    instance_config = _load_single_instance_from_env()
    async_proxmox = AsyncProxmoxAPI.from_instance_config(instance_config)
    infra = InfraCommands.build(
        async_proxmox, instance_config.node, instance_config.image_storage
    )
    built_in_vm = infra.built_in_vm

    for built_in in built_ins:
        await built_in_vm.clear_builtin(built_in)
    known_builtins = await built_in_vm.known_builtins()
    assert not set(built_ins) & set(known_builtins)

    existing_vm_ids = {vm["vmid"] for vm in await infra.qemu_commands.list_vms()}

    await asyncio.gather(*(built_in_vm.ensure_exists(name) for name in built_ins))

    all_vms = await infra.qemu_commands.list_vms()
    return Rebuild(
        built_ins=built_ins,
        new_vms=[vm for vm in all_vms if vm["vmid"] not in existing_vm_ids],
    )


async def test_one_vm_per_built_in(rebuild: Rebuild) -> None:
    assert len(rebuild.new_vms) == len(rebuild.built_ins), rebuild.new_vms


async def test_ubuntu(rebuild: Rebuild) -> None:
    _check_template(rebuild, "ubuntu24.04")


async def test_debian(rebuild: Rebuild) -> None:
    _check_template(rebuild, "debian13")


@pytest.mark.slow
async def test_kali(rebuild: Rebuild) -> None:
    _check_template(rebuild, "kali2025.4")


def _check_template(rebuild: Rebuild, built_in: str) -> None:
    tagged = [
        vm
        for vm in rebuild.new_vms
        if f"builtin-{built_in}" in (vm.get("tags") or "").split(";")
    ]
    assert len(tagged) == 1, tagged
    template = tagged[0]
    assert template["template"] == 1
    assert "inspect" in template["tags"].split(";")
    assert template["name"] == f"inspect-{built_in}"
