"""The SDN zone built-in templates are built in, and the build VM's place in it.

Collaborators are mocked; the harness records the order of the steps that
matter to the zone.
"""

from ipaddress import ip_address
from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proxmoxsandbox._impl.built_in_vm import BuiltInVM
from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._impl.sdn_commands import (
    BUILT_IN_IDS_PREFIX,
    IpamMapping,
    is_ephemeral_zone,
)

IDS_START = f"{BUILT_IN_IDS_PREFIX}123"
ZONE_ID = f"{IDS_START}z"
VNET_ID = f"{IDS_START}v0"
VM_ID = 100
CLOUD_INIT_DONE = "....\nstatus: done"
CLOUD_INIT_ERROR = "....\nstatus: error"
LEASE = IpamMapping(
    vnet_id=VNET_ID,
    zone_id=ZONE_ID,
    mac="BC:24:11:00:00:01",
    ipv4=ip_address("192.168.42.50"),
)


class Harness:
    """BuiltInVM over mocked collaborators, recording the order of key steps."""

    def __init__(self, *, cloud_init_output: str | dict[int, str] = CLOUD_INIT_DONE):
        """VM IDs are handed out from VM_ID up, in the order builds ask.

        `cloud_init_output` is one output for every build, or one per VM ID.
        """
        self.events: List[Tuple[str, object]] = []
        self.create_requests: List[dict] = []

        async def request(method, path, **kwargs):
            if method == "GET" and path.endswith("/config?current=1"):
                return {"template": 1}
            if method == "POST" and path.endswith("/template"):
                self.events.append(("template", int(path.split("/")[-2])))
            if method == "POST" and path.endswith("/qemu"):
                self.create_requests.append(kwargs["json"])
            return "UPID:test"

        async_proxmox = MagicMock()
        async_proxmox.request = AsyncMock(side_effect=request)
        async_proxmox.wait_for_task = AsyncMock()

        next_vm_id = VM_ID

        async def create_with_fresh_vm_id(create):
            nonlocal next_vm_id
            vm_id = next_vm_id
            next_vm_id += 1
            await create(vm_id)
            return vm_id

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

        async def exec_command(vm_id, command):
            return vm_id  # stands in for the PID, so the status knows the VM

        async def get_agent_exec_status(vm_id, pid):
            out_data = (
                cloud_init_output
                if isinstance(cloud_init_output, str)
                else cloud_init_output[vm_id]
            )
            return MagicMock(exited=1, out_data=out_data)

        self.agent_commands = MagicMock()
        self.agent_commands.exec_command = AsyncMock(side_effect=exec_command)
        self.agent_commands.get_agent_exec_status = AsyncMock(
            side_effect=get_agent_exec_status
        )

    async def build(self, *built_ins: str) -> None:
        with patch(
            "proxmoxsandbox._impl.built_in_vm.AgentCommands",
            return_value=self.agent_commands,
        ):
            await self.built_in_vm.ensure_exists_many(built_ins)

    async def build_debian(self) -> None:
        await self.build("debian13")

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


async def test_failed_build_keeps_its_vm_and_the_zone_tracked() -> None:
    """Nothing is torn down: task_cleanup or the next pre-clean does that."""
    h = Harness(cloud_init_output=CLOUD_INIT_ERROR)

    with pytest.raises(ValueError, match="cloud-init failed"):
        await h.build_debian()

    assert h.event_names() == ["create_sdn"]
    assert h.qemu_commands.register_vm.call_args.kwargs["vm_id"] == VM_ID
    h.qemu_commands.deregister_vm.assert_not_called()
    h.qemu_commands.destroy_vm.assert_not_called()
    h.qemu_commands.remove_existing_nics.assert_not_called()
    h.sdn_commands.deregister_sdn_resources_for_zones.assert_not_called()


async def test_template_built_beside_a_failed_build_has_no_nic_on_the_kept_zone() -> (
    None
):
    """Its IPAM entry would stop task_cleanup deleting the zone's subnet."""
    debian_vm_id, ubuntu_vm_id = VM_ID, VM_ID + 1
    h = Harness(
        cloud_init_output={
            debian_vm_id: CLOUD_INIT_DONE,
            ubuntu_vm_id: CLOUD_INIT_ERROR,
        }
    )

    with pytest.raises(ValueError, match="cloud-init failed"):
        await h.build("debian13", "ubuntu24.04")

    assert h.events == [
        ("create_sdn", (IDS_START, "auto")),
        ("template", debian_vm_id),
        ("remove_nics", debian_vm_id),
    ]
    h.qemu_commands.deregister_vm.assert_called_once_with(debian_vm_id)
    registered = [c.kwargs["vm_id"] for c in h.qemu_commands.register_vm.call_args_list]
    assert registered == [debian_vm_id, ubuntu_vm_id]


async def test_zone_is_kept_when_its_creation_fails() -> None:
    """create_sdn tracks the zone itself before its first request."""
    h = Harness()
    h.sdn_commands.create_sdn = AsyncMock(side_effect=RuntimeError("vnet rejected"))

    with pytest.raises(RuntimeError, match="vnet rejected"):
        await h.build_debian()

    assert h.events == []
    assert h.create_requests == []


async def test_leases_left_in_the_zone_are_passed_to_its_teardown() -> None:
    h = Harness()
    h.sdn_commands.read_zone_ipam_mappings = AsyncMock(return_value=[LEASE])

    await h.build_debian()

    assert h.events[-1] == ("tear_down", (ZONE_ID, (LEASE,)))


async def test_zone_torn_down_when_lease_lookup_fails() -> None:
    h = Harness()
    h.sdn_commands.read_zone_ipam_mappings = AsyncMock(
        side_effect=RuntimeError("ipam down")
    )

    await h.build_debian()

    assert h.events[-1] == ("tear_down", (ZONE_ID, ()))


@pytest.mark.parametrize(
    "tear_down",
    [
        AsyncMock(return_value=False),
        AsyncMock(side_effect=RuntimeError("SDN apply failed")),
    ],
    ids=["zone survives", "teardown raises"],
)
async def test_zone_that_is_not_removed_stays_tracked(tear_down: AsyncMock) -> None:
    """The templates are built, so this is left to task_cleanup, not raised."""
    h = Harness()
    h.sdn_commands.tear_down_sdn_zone_and_vnet = tear_down

    await h.build_debian()

    h.sdn_commands.deregister_sdn_resources_for_zones.assert_not_called()


async def test_task_cleanup_destroys_vms_before_zones() -> None:
    """A failed build's VM holds an IPAM entry in the build zone until it goes."""
    order: List[str] = []
    qemu_commands, sdn_commands = MagicMock(), MagicMock()
    qemu_commands.task_cleanup = AsyncMock(side_effect=lambda: order.append("vms"))
    sdn_commands.task_cleanup = AsyncMock(side_effect=lambda: order.append("zones"))
    infra = InfraCommands(MagicMock(), "pve", sdn_commands, qemu_commands, MagicMock())

    await infra.task_cleanup()

    assert order == ["vms", "zones"]
