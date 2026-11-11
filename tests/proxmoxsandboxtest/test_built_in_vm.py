"""Rebuilds the Ubuntu and Debian templates and leaves them for the rest of the suite.

Kali is rebuilt only under --runslow.
"""

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

    await built_in_vm.ensure_exists_many(built_ins)

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
