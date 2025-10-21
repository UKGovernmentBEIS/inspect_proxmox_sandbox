from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.qemu_commands import QemuCommands


async def test_ubuntu(qemu_commands: QemuCommands, built_in_vm: BuiltInVM) -> None:
    await _do_test_builtin(qemu_commands, built_in_vm, "ubuntu24.04")


async def test_debian(qemu_commands: QemuCommands, built_in_vm: BuiltInVM) -> None:
    await _do_test_builtin(qemu_commands, built_in_vm, "debian13")


async def test_kali(qemu_commands: QemuCommands, built_in_vm: BuiltInVM) -> None:
    await _do_test_builtin(qemu_commands, built_in_vm, "kali2025.3")


async def _do_test_builtin(
    qemu_commands: QemuCommands, built_in_vm: BuiltInVM, builtin_name: str
):
    await built_in_vm.clear_builtins()

    known_builtins = await built_in_vm.known_builtins()

    assert builtin_name not in known_builtins

    existing_vms = await qemu_commands.list_vms()

    await built_in_vm.ensure_exists(builtin_name)

    all_vms = await qemu_commands.list_vms()

    existing_vm_ids = [vm["vmid"] for vm in existing_vms]

    assert len(all_vms) == len(existing_vms) + 1

    new_vms = [vm for vm in all_vms if vm["vmid"] not in existing_vm_ids]
    assert len(new_vms) == 1
    assert new_vms[0]["template"] == 1
    assert new_vms[0]["tags"]
    tags = new_vms[0]["tags"].split(";")
    assert "inspect" in tags
    assert f"builtin-{builtin_name}" in tags
