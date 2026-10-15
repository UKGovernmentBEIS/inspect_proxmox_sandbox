"""Unit tests for the ephemeral SDN zone a built-in template is built in.

Collaborators are mocked; these tests check the zone's lifecycle around the
build (created with the shared ids-start/create_sdn machinery, torn down
whatever happens) and that the finished template carries no NIC.
"""

from ipaddress import ip_address
from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxmoxsandbox._impl.built_in_vm import DEBIAN_13_URL, BuiltInVM
from proxmoxsandbox._impl.sdn_commands import (
    BUILT_IN_IDS_PREFIX,
    IpamMapping,
    is_ephemeral_zone,
)

ZONE_ID = f"{BUILT_IN_IDS_PREFIX}123z"
VNET_ID = f"{BUILT_IN_IDS_PREFIX}123v0"
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

        async def remove_existing_nics(vm_id):
            self.events.append(("remove_nics", vm_id))

        self.qemu_commands = MagicMock()
        self.qemu_commands.start = AsyncMock()
        self.qemu_commands.await_vm = AsyncMock()
        self.qemu_commands.list_vms = AsyncMock(return_value=[])
        self.qemu_commands.remove_existing_nics = AsyncMock(
            side_effect=remove_existing_nics
        )

        async def create_sdn(proxmox_ids_start, sdn_config):
            self.events.append(("create_sdn", (proxmox_ids_start, sdn_config)))
            return ZONE_ID, [(VNET_ID, None)]

        async def tear_down(sdn_zone_id, ipam_mappings):
            self.events.append(("tear_down", (sdn_zone_id, tuple(ipam_mappings))))

        self.sdn_commands = MagicMock()
        self.sdn_commands.find_proxmox_ids_start = AsyncMock(
            return_value=f"{BUILT_IN_IDS_PREFIX}123"
        )
        self.sdn_commands.create_sdn = AsyncMock(side_effect=create_sdn)
        self.sdn_commands.read_zone_ipam_mappings = AsyncMock(return_value=[LEASE])
        self.sdn_commands.tear_down_sdn_zone_and_vnet = AsyncMock(side_effect=tear_down)

        self.built_in_vm = BuiltInVM(
            async_proxmox=async_proxmox,
            node="pve",
            image_storage="local-lvm",
            qemu_commands=self.qemu_commands,
            sdn_commands=self.sdn_commands,
            storage_commands=MagicMock(),
        )
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
            await self.built_in_vm.ensure_exists_from_qcow2(
                next_available_vm_id=VM_ID,
                built_in="debian13",
                source_image_source_url=DEBIAN_13_URL,
            )

    def event_names(self) -> List[str]:
        return [name for name, _ in self.events]


def test_build_zone_name_is_ephemeral() -> None:
    """Orphan sweeps (cleanup_no_id, the sample_init pre-check) must see it."""
    assert is_ephemeral_zone(ZONE_ID)


async def test_template_built_in_ephemeral_zone_then_zone_torn_down() -> None:
    h = Harness()

    await h.build_debian()

    assert h.event_names() == ["create_sdn", "template", "remove_nics", "tear_down"]
    assert h.events[0][1] == (f"{BUILT_IN_IDS_PREFIX}123", "auto")
    h.sdn_commands.find_proxmox_ids_start.assert_awaited_once_with(
        prefix=BUILT_IN_IDS_PREFIX
    )
    h.sdn_commands.register_sdn_zone.assert_called_once_with(ZONE_ID)
    h.sdn_commands.deregister_sdn_resources.assert_called_once_with(
        sdn_zone_id=ZONE_ID, ipam_mappings=()
    )

    # the VM was built on the zone's VNET...
    assert len(h.create_requests) == 1
    assert h.create_requests[0]["net0"] == f"virtio,bridge={VNET_ID}"
    # ...and the template's NIC and the zone's leftover leases went with the zone
    assert h.events[2][1] == VM_ID
    assert h.events[3][1] == (ZONE_ID, (LEASE,))


async def test_zone_torn_down_when_cloud_init_fails() -> None:
    h = Harness(cloud_init_output="....\nstatus: error")

    with pytest.raises(ValueError, match="cloud-init failed"):
        await h.build_debian()

    assert h.event_names() == ["create_sdn", "tear_down"]
    assert h.events[1][1] == (ZONE_ID, (LEASE,))
    h.sdn_commands.deregister_sdn_resources.assert_called_once_with(
        sdn_zone_id=ZONE_ID, ipam_mappings=()
    )


async def test_zone_torn_down_when_lease_lookup_fails() -> None:
    """A failing IPAM read must not stop the zone from being removed."""
    h = Harness()
    h.sdn_commands.read_zone_ipam_mappings = AsyncMock(
        side_effect=RuntimeError("ipam down")
    )

    await h.build_debian()

    assert h.events[-1] == ("tear_down", (ZONE_ID, ()))
