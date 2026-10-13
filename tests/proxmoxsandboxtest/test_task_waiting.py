import asyncio
import unittest.mock

import pytest

import proxmoxsandbox._impl.agent_commands as agent_commands
import proxmoxsandbox._impl.async_proxmox as async_proxmox
import proxmoxsandbox._impl.built_in_vm as built_in_vm
import proxmoxsandbox._impl.storage_commands as storage_commands


@pytest.mark.parametrize("operation", ["upload", "snapshot", "rollback"])
async def test_operation_waits_for_its_task(operation, tmp_path):
    api = unittest.mock.Mock(spec=async_proxmox.AsyncProxmoxAPI)
    upid = "UPID:other-node:123:456:789:task:100:user:"
    api.request.return_value = upid
    api.upload_file.return_value = upid
    waiting = asyncio.Event()
    completed = asyncio.Event()

    async def wait_for_task(*, upid):
        waiting.set()
        await completed.wait()

    api.wait_for_task.side_effect = wait_for_task
    if operation == "upload":
        commands = storage_commands.LocalStorageCommands(async_proxmox=api, node="node")
        action = commands.upload_file_to_storage(
            file=tmp_path / "test.iso", content_type="iso"
        )
    else:
        commands = agent_commands.AgentCommands(async_proxmox=api, node="node")
        method = (
            commands.create_snapshot
            if operation == "snapshot"
            else commands.rollback_to_snapshot
        )
        action = method(vm_id=100, snapshot_name="test")

    task = asyncio.create_task(action)
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        assert not task.done()
        api.wait_for_task.assert_awaited_once_with(upid=upid)
        assert all(
            call.args[1] != "/cluster/tasks" for call in api.request.call_args_list
        )
        completed.set()
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_clear_builtins_issues_all_deletes_before_waiting():
    commands = object.__new__(built_in_vm.BuiltInVM)
    commands.node = "node"
    commands.async_proxmox = unittest.mock.Mock(spec=async_proxmox.AsyncProxmoxAPI)
    commands.async_proxmox.request.side_effect = ["first-upid", None, "last-upid"]
    commands.read_all_content = unittest.mock.AsyncMock(
        return_value=[
            {"volid": f"local:import/{built_in_vm.KALI_DISK_RENAMED}"} for _ in range(3)
        ]
    )
    commands.known_builtins = unittest.mock.AsyncMock(return_value={"kali2025.4": 100})
    commands.qemu_commands = unittest.mock.Mock()
    commands.qemu_commands.destroy_vm = unittest.mock.AsyncMock()

    async def wait_for_task(*, upid):
        assert commands.async_proxmox.request.await_count == 3
        commands.qemu_commands.destroy_vm.assert_awaited_once_with(
            vm_id=100, name="inspect-kali2025.4"
        )

    commands.async_proxmox.wait_for_task.side_effect = wait_for_task
    await commands.clear_builtins()
    assert commands.async_proxmox.wait_for_task.await_args_list == [
        unittest.mock.call(upid="first-upid"),
        unittest.mock.call(upid="last-upid"),
    ]
