"""Unit tests for InfraCommands.cleanup_no_id zone-selection logic.

These tests verify that cleanup_no_id only marks provider-managed
ephemeral zones for deletion, regardless of which VNETs `inspect`-tagged
VMs happen to be plugged into. This protects pre-existing user SDN
state when samples reference it via sdn_config=None.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._impl.sdn_commands import is_ephemeral_zone


def _make_infra(
    *,
    vms=None,
    vm_configs=None,
    zones=None,
    vnets=None,
    ipam=None,
):
    """Build an InfraCommands with mocked qemu/sdn collaborators.

    `vm_configs` maps vmid -> config dict (as returned by `read_vm`),
    used to resolve the bridges each non-template inspect VM is plugged
    into. The real teardown methods are replaced with AsyncMocks so the
    test can assert on the arguments without making any API calls.
    """
    vm_configs = vm_configs or {}

    infra = MagicMock()
    infra.async_proxmox = MagicMock()
    infra.async_proxmox.base_url = "https://test"

    infra.qemu_commands = MagicMock()
    infra.qemu_commands.list_vms = AsyncMock(return_value=vms or [])
    infra.qemu_commands.read_vm = AsyncMock(
        side_effect=lambda vmid: vm_configs.get(vmid, {})
    )
    infra.qemu_commands.destroy_vm = AsyncMock()

    infra.sdn_commands = MagicMock()
    infra.sdn_commands.list_sdn_zones = AsyncMock(return_value=zones or [])
    infra.sdn_commands.read_all_vnets = AsyncMock(return_value=vnets or [])
    infra.sdn_commands.read_all_ipam_mappings = AsyncMock(return_value=ipam or [])
    infra.sdn_commands.tear_down_sdn_zones_and_vnets = AsyncMock()

    # Bind the real cleanup_no_id and find_all_zones onto the mock so the
    # mocked collaborators get exercised.
    infra.cleanup_no_id = InfraCommands.cleanup_no_id.__get__(infra)
    infra.find_all_zones = InfraCommands.find_all_zones.__get__(infra)
    return infra


@pytest.mark.asyncio
async def test_cleanup_skips_pre_existing_zone_even_with_attached_inspect_vm():
    """Pre-existing user zone is not deleted when an inspect VM is attached.

    A non-template inspect VM plugged into a pre-existing user vnet
    must NOT cause that vnet's zone to be deleted.
    """
    infra = _make_infra(
        vms=[
            {
                "vmid": 100,
                "name": "sandbox-vm",
                "tags": "inspect;sandbox",
                "template": 0,
            },
        ],
        vm_configs={
            100: {"net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=monitor"},
        },
        # User pre-existing zone — name does not match ZONE_REGEX.
        zones=[{"zone": "user_zone", "type": "simple"}],
        vnets=[{"vnet": "monitor", "zone": "user_zone"}],
    )

    await infra.cleanup_no_id(skip_confirmation=True)

    args, _ = infra.sdn_commands.tear_down_sdn_zones_and_vnets.call_args
    zones_passed = args[0]
    assert "user_zone" not in zones_passed, (
        f"pre-existing zone wrongly targeted for deletion: {zones_passed}"
    )
    assert zones_passed == set(), (
        f"no zones should have been targeted; got: {zones_passed}"
    )


@pytest.mark.asyncio
async def test_cleanup_targets_provider_zone_via_regex():
    """Ephemeral provider zones are targeted for deletion via ZONE_REGEX.

    An ephemeral zone matching ZONE_REGEX must be targeted for deletion
    even when no inspect VMs are still plugged into it.
    """
    infra = _make_infra(
        vms=[],
        zones=[
            {"zone": "tlo123z", "type": "simple"},  # matches ZONE_REGEX
            {"zone": "user_zone", "type": "simple"},  # does not match
        ],
        vnets=[],
    )

    await infra.cleanup_no_id(skip_confirmation=True)

    args, _ = infra.sdn_commands.tear_down_sdn_zones_and_vnets.call_args
    zones_passed = args[0]
    assert zones_passed == {"tlo123z"}, (
        f"only the regex-matching zone should be targeted; got: {zones_passed}"
    )


@pytest.mark.parametrize(
    ("zone_id", "ephemeral"),
    [
        ("tlo123z", True),
        ("000000z", True),
        # Near-miss user-owned names that share a 7-char prefix with a
        # provider zone — these must NOT be claimed for deletion.
        ("abc123za", False),
        ("abc123z-prod", False),
        ("abc123zz", False),
        # Conforming substring not at the start.
        ("xabc123z", False),
        # Static built-in SDN.
        ("inspvmz", False),
        # Generic user-named zones.
        ("user_zone", False),
        ("a254c5f5", False),
    ],
)
def test_is_ephemeral_zone_anchored(zone_id: str, ephemeral: bool) -> None:
    """is_ephemeral_zone matches only exact provider zone names.

    Cleanup uses this predicate as the ownership boundary for non-interactive
    SDN deletion, so a prefix or substring match would let cleanup destroy
    unrelated infrastructure on a shared Proxmox host.
    """
    assert is_ephemeral_zone(zone_id) is ephemeral


@pytest.mark.asyncio
async def test_cleanup_skips_zone_with_matching_prefix_only():
    """A user zone whose 7-char *prefix* matches ZONE_REGEX is left alone.

    The provider only ever creates zone names of exactly 7 characters
    (`{3 chars}{3 digits}z`); a longer name with that prefix is somebody
    else's zone and must not be swept.
    """
    infra = _make_infra(
        vms=[],
        zones=[
            {"zone": "abc123za", "type": "simple"},  # near-miss, NOT ours
            {"zone": "tlo123z", "type": "simple"},  # ours
        ],
        vnets=[],
    )

    await infra.cleanup_no_id(skip_confirmation=True)

    args, _ = infra.sdn_commands.tear_down_sdn_zones_and_vnets.call_args
    zones_passed = args[0]
    assert zones_passed == {"tlo123z"}, (
        "near-miss user zone with matching prefix wrongly targeted; "
        f"got: {zones_passed}"
    )


@pytest.mark.asyncio
async def test_cleanup_skips_static_inspvm_zone():
    """Static `inspvm*` SDN is not swept by cleanup_no_id.

    The static `inspvm*` SDN is intentionally permanent and must not be
    swept by cleanup_no_id, even though the provider created it.
    """
    infra = _make_infra(
        vms=[],
        zones=[{"zone": "inspvmz", "type": "simple"}],
        vnets=[{"vnet": "inspvmv0", "zone": "inspvmz"}],
    )

    await infra.cleanup_no_id(skip_confirmation=True)

    # No deletion call at all when nothing is targeted.
    assert not infra.sdn_commands.tear_down_sdn_zones_and_vnets.called, (
        "static inspvm* SDN must not be torn down"
    )


@pytest.mark.asyncio
async def test_cleanup_mixed_case_protects_pre_existing_only():
    """Orphan provider zone is deleted; pre-existing user zone is left alone.

    With both an orphan provider zone and a pre-existing user zone present,
    only the provider zone is targeted; the user zone is left alone.
    """
    infra = _make_infra(
        vms=[
            {
                "vmid": 100,
                "name": "sandbox-vm",
                "tags": "inspect;sandbox",
                "template": 0,
            },
        ],
        vm_configs={
            # Two nics: one in the orphan provider zone, one in user zone.
            100: {
                "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=tlo123v0",
                "net1": "virtio=11:22:33:44:55:66,bridge=monitor",
            },
        },
        zones=[
            {"zone": "tlo123z", "type": "simple"},  # provider, match regex
            {"zone": "user_zone", "type": "simple"},  # pre-existing
        ],
        vnets=[
            {"vnet": "tlo123v0", "zone": "tlo123z"},
            {"vnet": "monitor", "zone": "user_zone"},
        ],
    )

    await infra.cleanup_no_id(skip_confirmation=True)

    args, _ = infra.sdn_commands.tear_down_sdn_zones_and_vnets.call_args
    zones_passed = args[0]
    assert zones_passed == {"tlo123z"}
    assert "user_zone" not in zones_passed
