import pytest

from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox._impl.sdn_commands import SdnCommands

pytestmark = pytest.mark.req_proxmox


async def test_ubuntu(
    qemu_commands: QemuCommands, sdn_commands: SdnCommands, built_in_vm: BuiltInVM
) -> None:
    await _do_test_builtin(qemu_commands, sdn_commands, built_in_vm, "ubuntu24.04")


async def test_debian(
    qemu_commands: QemuCommands, sdn_commands: SdnCommands, built_in_vm: BuiltInVM
) -> None:
    await _do_test_builtin(qemu_commands, sdn_commands, built_in_vm, "debian13")


async def test_kali(
    qemu_commands: QemuCommands, sdn_commands: SdnCommands, built_in_vm: BuiltInVM
) -> None:
    await _do_test_builtin(qemu_commands, sdn_commands, built_in_vm, "kali2025.4")


async def _do_test_builtin(
    qemu_commands: QemuCommands,
    sdn_commands: SdnCommands,
    built_in_vm: BuiltInVM,
    builtin_name: str,
):
    await built_in_vm.clear_builtins()

    known_builtins = await built_in_vm.known_builtins()

    assert builtin_name not in known_builtins

    existing_vms = await qemu_commands.list_vms()
    existing_zones = await sdn_commands.list_sdn_zones()

    await built_in_vm.ensure_exists(builtin_name)

    all_vms = await qemu_commands.list_vms()

    # The build zone is ephemeral: nothing SDN-wise is left behind
    assert await sdn_commands.list_sdn_zones() == existing_zones

    existing_vm_ids = [vm["vmid"] for vm in existing_vms]

    assert len(all_vms) == len(existing_vms) + 1

    new_vms = [vm for vm in all_vms if vm["vmid"] not in existing_vm_ids]
    assert len(new_vms) == 1
    assert new_vms[0]["template"] == 1
    assert new_vms[0]["tags"]
    tags = new_vms[0]["tags"].split(";")
    assert "inspect" in tags
    assert f"builtin-{builtin_name}" in tags

    # The build-time NIC is removed; clones are wired into their sample's SDN
    template_config = await qemu_commands.read_vm(new_vms[0]["vmid"])
    assert not any(key.startswith("net") for key in template_config)
