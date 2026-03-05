from pathlib import Path

import pytest

from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.qemu_commands import QemuCommands, VnetAliases
from proxmoxsandbox._impl.sdn_commands import STATIC_SDN_START, SdnCommands
from proxmoxsandbox.schema import (
    SdnConfig,
    VmConfig,
    VmNicConfig,
    VmSourceConfig,
    VnetConfig,
)

CURRENT_DIR = Path(__file__).parent  # noqa: F821


async def test_simple_vm_non_sandbox(
    qemu_commands: QemuCommands,
    auto_sdn_vnet_aliases: VnetAliases,
    built_in_vm: BuiltInVM,
):
    built_in_ubuntu = VmSourceConfig(built_in="ubuntu24.04")

    await built_in_vm.ensure_exists("ubuntu24.04")

    new_vm_id = await qemu_commands.create_and_start_vm(
        sdn_vnet_aliases=auto_sdn_vnet_aliases,
        vm_config=VmConfig(
            vm_source_config=built_in_ubuntu,
            ram_mb=768,
            vcpus=3,
            is_sandbox=False,
            uefi_boot=False,
        ),
        built_in_vm_ids=await built_in_vm.known_builtins(),
    )

    new_vm = await qemu_commands.read_vm(new_vm_id)
    assert new_vm["memory"] == "768"
    assert new_vm["cores"] == 3
    assert new_vm["agent"] == "enabled=0"
    assert "net0" in new_vm
    assert "inspect" in new_vm["tags"]
    assert "ubuntu24.04" not in new_vm["tags"]

    await qemu_commands.destroy_vm(new_vm_id)


async def test_none_nic_from_template_tag(
    qemu_commands: QemuCommands,
    auto_sdn_vnet_aliases: VnetAliases,
    built_in_vm: BuiltInVM,
):
    await built_in_vm.ensure_exists("ubuntu24.04")

    new_vm_id = await qemu_commands.create_and_start_vm(
        sdn_vnet_aliases=auto_sdn_vnet_aliases,
        vm_config=VmConfig(
            vm_source_config=VmSourceConfig(
                existing_vm_template_tag="builtin-ubuntu24.04"
            ),  # coupling ourselves to the implementation of built_in_vm, naughty
            nics=None,
        ),
        built_in_vm_ids=await built_in_vm.known_builtins(),
    )

    new_vm = await qemu_commands.read_vm(new_vm_id)
    assert "net0" in new_vm
    assert STATIC_SDN_START in new_vm["net0"]
    assert "inspect" in new_vm["tags"]
    assert "builtin-ubuntu24.04" in new_vm["tags"]

    await qemu_commands.destroy_vm(new_vm_id)


async def test_empty_nic_from_template_tag(
    qemu_commands: QemuCommands,
    auto_sdn_vnet_aliases: VnetAliases,
    built_in_vm: BuiltInVM,
):
    await built_in_vm.ensure_exists("ubuntu24.04")

    new_vm_id = await qemu_commands.create_and_start_vm(
        sdn_vnet_aliases=auto_sdn_vnet_aliases,
        vm_config=VmConfig(
            vm_source_config=VmSourceConfig(
                existing_vm_template_tag="builtin-ubuntu24.04"
            ),
            nics=(),
        ),
        built_in_vm_ids=await built_in_vm.known_builtins(),
    )

    new_vm = await qemu_commands.read_vm(new_vm_id)
    assert "net0" not in new_vm

    await qemu_commands.destroy_vm(new_vm_id)


async def test_none_nic_from_built_in(
    qemu_commands: QemuCommands,
    auto_sdn_vnet_aliases: VnetAliases,
    built_in_vm: BuiltInVM,
):
    built_in_ubuntu = VmSourceConfig(built_in="ubuntu24.04")

    await built_in_vm.ensure_exists("ubuntu24.04")

    new_vm_id = await qemu_commands.create_and_start_vm(
        sdn_vnet_aliases=auto_sdn_vnet_aliases,
        vm_config=VmConfig(
            vm_source_config=built_in_ubuntu,
            nics=None,
        ),
        built_in_vm_ids=await built_in_vm.known_builtins(),
    )

    new_vm = await qemu_commands.read_vm(new_vm_id)
    assert "net0" in new_vm
    assert auto_sdn_vnet_aliases[0][0] in new_vm["net0"]

    await qemu_commands.destroy_vm(new_vm_id)


async def test_existing_alias_from_built_in(
    qemu_commands: QemuCommands,
    built_in_vm: BuiltInVM,
    sdn_commands: SdnCommands,
):
    built_in_ubuntu = VmSourceConfig(built_in="ubuntu24.04")

    await built_in_vm.ensure_exists("ubuntu24.04")

    sdn_zone_id, _ = await sdn_commands.create_sdn(
        proxmox_ids_start="tea999",
        sdn_config=SdnConfig(
            vnet_configs=(VnetConfig(alias="my-alias"),), use_pve_ipam_dnsnmasq=False
        ),
    )

    assert sdn_zone_id is not None

    new_vm_id = await qemu_commands.create_and_start_vm(
        # we didn't create any vnets in the eval, so this would be empty
        sdn_vnet_aliases=[],
        vm_config=VmConfig(
            vm_source_config=built_in_ubuntu,
            nics=(
                VmNicConfig(
                    vnet_alias="my-alias",
                ),
            ),
        ),
        built_in_vm_ids=await built_in_vm.known_builtins(),
    )

    new_vm = await qemu_commands.read_vm(new_vm_id)
    assert "net0" in new_vm
    assert "tea999" in new_vm["net0"]

    await qemu_commands.destroy_vm(new_vm_id)
    await sdn_commands.tear_down_sdn_zone_and_vnet(sdn_zone_id, ())


async def test_multiple_nic(
    qemu_commands: QemuCommands,
    built_in_vm: BuiltInVM,
    sdn_commands: SdnCommands,
    ids_start: str,
):
    built_in_ubuntu = VmSourceConfig(built_in="ubuntu24.04")

    await built_in_vm.ensure_exists("ubuntu24.04")

    sdn_zone_id, vnet_aliases = await sdn_commands.create_sdn(
        ids_start,
        sdn_config=SdnConfig(
            vnet_configs=(
                VnetConfig(alias="vnetA"),
                VnetConfig(alias="vnetB"),
            ),
            use_pve_ipam_dnsnmasq=False,
        ),
    )

    assert sdn_zone_id is not None

    new_vm_id = await qemu_commands.create_and_start_vm(
        sdn_vnet_aliases=vnet_aliases,
        vm_config=VmConfig(
            vm_source_config=built_in_ubuntu,
            nics=(VmNicConfig(vnet_alias="vnetB"), VmNicConfig(vnet_alias="vnetA")),
        ),
        built_in_vm_ids=await built_in_vm.known_builtins(),
    )

    new_vm = await qemu_commands.read_vm(new_vm_id)
    assert "net0" in new_vm
    assert vnet_aliases[1][0] in new_vm["net0"]
    assert vnet_aliases[1][1] == "vnetB"
    assert "net1" in new_vm
    assert vnet_aliases[0][0] in new_vm["net1"]
    assert vnet_aliases[0][1] == "vnetA"

    await qemu_commands.destroy_vm(new_vm_id)
    await sdn_commands.tear_down_sdn_zone_and_vnet(sdn_zone_id, ())


async def test_empty_nic_from_built_in(
    qemu_commands: QemuCommands,
    auto_sdn_vnet_aliases: VnetAliases,
    built_in_vm: BuiltInVM,
):
    built_in_ubuntu = VmSourceConfig(built_in="ubuntu24.04")

    await built_in_vm.ensure_exists("ubuntu24.04")

    new_vm_id = await qemu_commands.create_and_start_vm(
        sdn_vnet_aliases=auto_sdn_vnet_aliases,
        vm_config=VmConfig(
            vm_source_config=built_in_ubuntu,
            nics=(),
        ),
        built_in_vm_ids=await built_in_vm.known_builtins(),
    )

    new_vm = await qemu_commands.read_vm(new_vm_id)
    assert "net0" not in new_vm

    await qemu_commands.destroy_vm(new_vm_id)


async def test_from_ova_local(qemu_commands: QemuCommands):
    new_vm_id = await qemu_commands.create_and_start_vm(
        sdn_vnet_aliases=[],
        vm_config=VmConfig(
            vm_source_config=VmSourceConfig(
                # This is originally from a release in https://github.com/oVirt/ovirt-tinycore-linux
                # but converted to OVA and checked in here
                ova=CURRENT_DIR / ".." / "oVirtTinyCore64-13.11.ova"
            ),
            name="test-from-ova-local",
            nics=(),
            uefi_boot=False,
            is_sandbox=True,
        ),
        built_in_vm_ids={},
    )

    await qemu_commands.ping_qemu_agent(new_vm_id)

    new_vm = await qemu_commands.read_vm(new_vm_id)
    assert "inspect" in new_vm["tags"]

    await qemu_commands.destroy_vm(new_vm_id)


# test disabled - you need a publicly available OVA that has both:
# 1. UEFI boot enabled
# 2. qemu-guest-agent installed
# AISI has one internally which can be provided on request, but it is
# nearly 1GB in size and hence not checked in to this repo.
@pytest.mark.skip
async def test_from_ova_uefi_sandbox(qemu_commands: QemuCommands):
    new_vm_id = await qemu_commands.create_and_start_vm(
        sdn_vnet_aliases=[],
        vm_config=VmConfig(
            vm_source_config=VmSourceConfig(ova=Path("ubu.ova")),
            nics=(),
            uefi_boot=True,
            is_sandbox=True,
        ),
        built_in_vm_ids={},
    )

    await qemu_commands.ping_qemu_agent(new_vm_id)

    await qemu_commands.destroy_vm(new_vm_id)


async def test_uefi(
    qemu_commands: QemuCommands,
    auto_sdn_vnet_aliases: VnetAliases,
    built_in_vm: BuiltInVM,
):
    built_in_ubuntu = VmSourceConfig(built_in="ubuntu24.04")

    await built_in_vm.ensure_exists("ubuntu24.04")

    new_vm_id = await qemu_commands.create_and_start_vm(
        sdn_vnet_aliases=auto_sdn_vnet_aliases,
        vm_config=VmConfig(
            vm_source_config=built_in_ubuntu,
            is_sandbox=True,
            uefi_boot=True,
        ),
        built_in_vm_ids=await built_in_vm.known_builtins(),
    )

    new_vm = await qemu_commands.read_vm(new_vm_id)
    assert new_vm["agent"] == "enabled=1"
    assert new_vm["bios"] == "ovmf"

    await qemu_commands.ping_qemu_agent(new_vm_id)

    await qemu_commands.destroy_vm(new_vm_id)
