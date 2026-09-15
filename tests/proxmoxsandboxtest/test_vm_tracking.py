"""A clone is tracked for task_cleanup from the moment it exists."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxmoxsandbox._impl.qemu_commands import QemuCommands
from proxmoxsandbox.schema import VmConfig, VmSourceConfig


@pytest.mark.asyncio
async def test_clone_is_tracked_even_if_it_never_becomes_ready():
    api = AsyncMock()
    api.request = AsyncMock(return_value=4242)  # /cluster/nextid
    task_wrapper = AsyncMock()
    qemu = QemuCommands(api, "pve1", "local", task_wrapper, MagicMock())

    with patch.object(
        qemu,
        "configure_network_and_tags",
        AsyncMock(side_effect=RuntimeError("QGA never answered")),
    ):
        with pytest.raises(RuntimeError):
            await qemu.clone_vm_and_start(
                vm_config=VmConfig(
                    vm_source_config=VmSourceConfig(existing_vm_template_tag="kali")
                ),
                vm_id_to_clone=100,
                sdn_vnet_aliases={},
                preserve_tags=False,
                wait_until_ready=True,
            )

    assert 4242 in qemu._tracked_vm_ids
