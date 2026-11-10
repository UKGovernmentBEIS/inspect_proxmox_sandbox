"""The SDN zone built-in templates are built in, and the build VM's place in it.

Collaborators are mocked. The harness records the order of the steps that
matter to the zone: it cannot be removed while a build VM still has a NIC
(and so an IPAM lease) in it.
"""

from ipaddress import ip_address
from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.sdn_commands import (
    BUILT_IN_IDS_PREFIX,
    IpamMapping,
    is_ephemeral_zone,
)

IDS_START = f"{BUILT_IN_IDS_PREFIX}123"
ZONE_ID = f"{IDS_START}z"
VNET_ID = f"{IDS_START}v0"
VM_ID = 100
LEASE = IpamMapping(
    vnet_id=VNET_ID,
    zone_id=ZONE_ID,
    mac="BC:24:11:00:00:01",
    ipv4=ip_address("192.168.42.50"),
)


class Harness:
    """BuiltInVM over mocked collaborators, recording the order of key steps."""

    def __init__(self, *, cloud_init_output: str = "....\nstatus: done"):
        self.events: List[Tuple[str, object]] = []
        self.create_requests: List[dict] = []

        async def request(method, path, **kwargs):
            if method == "GET" and path.endswith("/config?current=1"):
                return {"template": 1}
            if method == "POST" and path.endswith(f"/qemu/{VM_ID}/template"):
                self.events.append(("template", VM_ID))
            if method == "POST" and path.endswith("/qemu"):
                self.create_requests.append(kwargs["json"])
            return "UPID:test"

        async_proxmox = MagicMock()
        async_proxmox.request = AsyncMock(side_effect=request)
        async_proxmox.wait_for_task = AsyncMock()

        async def create_with_fresh_vm_id(create):
            await create(VM_ID)
            return VM_ID

        async def remove_existing_nics(vm_id):
            self.events.append(("remove_nics", vm_id))

        async def destroy_vm(*, vm_id, name):
            self.events.append(("destroy_vm", vm_id))

        self.qemu_commands = MagicMock()
        self.qemu_commands._create_with_fresh_vm_id = create_with_fresh_vm_id
        self.qemu_commands.start = AsyncMock()
        self.qemu_commands.await_vm = AsyncMock()
        self.qemu_commands.list_vms = AsyncMock(return_value=[])
        self.qemu_commands.remove_existing_nics = AsyncMock(
            side_effect=remove_existing_nics
        )
        self.qemu_commands.destroy_vm = AsyncMock(side_effect=destroy_vm)

        async def create_sdn(proxmox_ids_start, sdn_config):
            self.events.append(("create_sdn", (proxmox_ids_start, sdn_config)))
            return ZONE_ID, [(VNET_ID, None)]

        async def tear_down(sdn_zone_id, ipam_mappings):
            self.events.append(("tear_down", (sdn_zone_id, tuple(ipam_mappings))))
            return True

        self.sdn_commands = MagicMock()
        self.sdn_commands.find_proxmox_ids_start = AsyncMock(return_value=IDS_START)
        self.sdn_commands.create_sdn = AsyncMock(side_effect=create_sdn)
        self.sdn_commands.read_zone_ipam_mappings = AsyncMock(return_value=[])
        self.sdn_commands.tear_down_sdn_zone_and_vnet = AsyncMock(side_effect=tear_down)

        self.built_in_vm = BuiltInVM(
            async_proxmox=async_proxmox,
            node="pve",
            image_storage="local-lvm",
            qemu_commands=self.qemu_commands,
            sdn_commands=self.sdn_commands,
            storage_commands=MagicMock(),
        )
        self.built_in_vm.ensure_version = AsyncMock()  # type: ignore[method-assign]
        self.built_in_vm.ensure_source_uploaded = AsyncMock()  # type: ignore[method-assign]
        self.built_in_vm.create_and_upload_cloudinit_iso = AsyncMock()  # type: ignore[method-assign]

        self.agent_commands = MagicMock()
        self.agent_commands.exec_command = AsyncMock(return_value=42)
        self.agent_commands.get_agent_exec_status = AsyncMock(
            return_value=MagicMock(exited=1, out_data=cloud_init_output)
        )

    async def build_debian(self) -> None:
        with patch(
            "proxmoxsandbox._impl.built_in_vm.AgentCommands",
            return_value=self.agent_commands,
        ):
            await self.built_in_vm.ensure_exists("debian13")

    def event_names(self) -> List[str]:
        return [name for name, _ in self.events]


def test_build_zone_name_is_ephemeral() -> None:
    """Orphan sweeps (cleanup_no_id, the sample_init pre-check) must see it."""
    assert is_ephemeral_zone(ZONE_ID)


async def test_template_is_built_on_the_build_zone_and_keeps_no_nic() -> None:
    h = Harness()

    await h.build_debian()

    assert h.event_names() == ["create_sdn", "template", "remove_nics", "tear_down"]
    assert h.events[0][1] == (IDS_START, "auto")
    h.sdn_commands.find_proxmox_ids_start.assert_awaited_once_with(
        prefix=BUILT_IN_IDS_PREFIX
    )
    assert len(h.create_requests) == 1
    assert h.create_requests[0]["net0"] == f"virtio,bridge={VNET_ID}"
    assert h.events[2][1] == VM_ID
    assert h.events[3][1] == (ZONE_ID, ())
    h.sdn_commands.deregister_sdn_resources_for_zones.assert_called_once_with({ZONE_ID})


async def test_build_vm_is_tagged_from_creation() -> None:
    """cleanup_no_id finds VMs by tag, whatever stage the build died at."""
    h = Harness()

    await h.build_debian()

    assert h.create_requests[0]["tags"] == "inspect,builtin-debian13"


async def test_build_vm_is_tracked_until_it_is_a_template() -> None:
    h = Harness()

    await h.build_debian()

    registered = h.qemu_commands.register_vm.call_args.kwargs
    assert registered["vm_id"] == VM_ID
    assert registered["vm_config"].name == "inspect-debian13"
    h.qemu_commands.deregister_vm.assert_called_once_with(VM_ID)
    h.qemu_commands.destroy_vm.assert_not_called()


async def test_failed_build_destroys_its_vm_before_the_zone_goes() -> None:
    h = Harness(cloud_init_output="....\nstatus: error")

    with pytest.raises(ValueError, match="cloud-init failed"):
        await h.build_debian()

    assert h.event_names() == ["create_sdn", "destroy_vm", "tear_down"]
    assert h.events[1][1] == VM_ID
    h.qemu_commands.deregister_vm.assert_called_once_with(VM_ID)
    h.sdn_commands.deregister_sdn_resources_for_zones.assert_called_once_with({ZONE_ID})


async def test_vm_that_cannot_be_destroyed_stays_tracked_and_loses_its_lease() -> None:
    """The build's own error is raised, and the zone still goes."""
    h = Harness(cloud_init_output="....\nstatus: error")
    h.qemu_commands.destroy_vm = AsyncMock(side_effect=RuntimeError("VM is locked"))
    h.sdn_commands.read_zone_ipam_mappings = AsyncMock(return_value=[LEASE])

    with pytest.raises(ValueError, match="cloud-init failed"):
        await h.build_debian()

    h.qemu_commands.deregister_vm.assert_not_called()
    assert h.events[-1] == ("tear_down", (ZONE_ID, (LEASE,)))


async def test_zone_torn_down_when_lease_lookup_fails() -> None:
    h = Harness()
    h.sdn_commands.read_zone_ipam_mappings = AsyncMock(
        side_effect=RuntimeError("ipam down")
    )

    await h.build_debian()

    assert h.events[-1] == ("tear_down", (ZONE_ID, ()))


async def test_zone_teardown_error_does_not_replace_the_build_error() -> None:
    h = Harness(cloud_init_output="....\nstatus: error")
    h.sdn_commands.tear_down_sdn_zone_and_vnet = AsyncMock(
        side_effect=RuntimeError("SDN apply failed")
    )

    with pytest.raises(ValueError, match="cloud-init failed"):
        await h.build_debian()

    h.sdn_commands.deregister_sdn_resources_for_zones.assert_not_called()


async def test_zone_that_survives_teardown_stays_tracked() -> None:
    """The templates are built, so this is left to task_cleanup, not raised."""
    h = Harness()
    h.sdn_commands.tear_down_sdn_zone_and_vnet = AsyncMock(return_value=False)

    await h.build_debian()

    h.sdn_commands.deregister_sdn_resources_for_zones.assert_not_called()


async def test_zone_torn_down_when_its_creation_fails_part_way() -> None:
    h = Harness()
    h.sdn_commands.create_sdn = AsyncMock(side_effect=RuntimeError("vnet rejected"))

    with pytest.raises(RuntimeError, match="vnet rejected"):
        await h.build_debian()

    assert h.events == [("tear_down", (ZONE_ID, ()))]
    assert h.create_requests == []
