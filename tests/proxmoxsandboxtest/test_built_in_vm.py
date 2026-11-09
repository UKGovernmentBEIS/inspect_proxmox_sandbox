"""Rebuilds the built-in templates from their upstream images.

The module removes only the templates it is about to rebuild and leaves the
results in place, so the rest of the suite (which calls ensure_exists on
first use) runs against them instead of building its own. The rebuilds run
concurrently: each is mostly waiting on the guest's apt run, so the module
costs about as much as its slowest build.

Kali's kali-linux-everything install dwarfs the other two, so it is only
rebuilt here when PROXMOX_TEST_REBUILD_KALI is set. Without it, a host that
has no Kali template still builds one (through the same code) the first time
test_proxmox_sandbox_environment_e2e.py::test_built_in asks for it.
"""

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import pytest

from proxmoxsandbox._impl.async_proxmox import AsyncProxmoxAPI
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox.schema import _load_single_instance_from_env

pytestmark = pytest.mark.req_proxmox

REBUILD_KALI = bool(os.getenv("PROXMOX_TEST_REBUILD_KALI"))

BUILT_INS = ["ubuntu24.04", "debian13"] + (["kali2025.4"] if REBUILD_KALI else [])


@dataclass(frozen=True)
class Rebuild:
    built_ins: list[str]
    new_vms: list[dict[str, Any]]
    """VMs present after the rebuild that were not present before it."""


@pytest.fixture(scope="module")
async def rebuild() -> Rebuild:
    instance_config = _load_single_instance_from_env()
    async_proxmox = AsyncProxmoxAPI.from_instance_config(instance_config)
    infra = InfraCommands.build(
        async_proxmox, instance_config.node, instance_config.image_storage
    )
    built_in_vm = infra.built_in_vm

    for built_in in BUILT_INS:
        await built_in_vm.clear_builtin(built_in)
    known_builtins = await built_in_vm.known_builtins()
    assert not set(BUILT_INS) & set(known_builtins)

    existing_vm_ids = {vm["vmid"] for vm in await infra.qemu_commands.list_vms()}

    await asyncio.gather(*(built_in_vm.ensure_exists(name) for name in BUILT_INS))

    all_vms = await infra.qemu_commands.list_vms()
    return Rebuild(
        built_ins=BUILT_INS,
        new_vms=[vm for vm in all_vms if vm["vmid"] not in existing_vm_ids],
    )


async def test_one_vm_per_built_in(rebuild: Rebuild) -> None:
    assert len(rebuild.new_vms) == len(rebuild.built_ins), rebuild.new_vms


async def test_ubuntu(rebuild: Rebuild) -> None:
    _check_template(rebuild, "ubuntu24.04")


async def test_debian(rebuild: Rebuild) -> None:
    _check_template(rebuild, "debian13")


@pytest.mark.skipif(
    not REBUILD_KALI, reason="set PROXMOX_TEST_REBUILD_KALI to rebuild Kali (~25 min)"
)
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
