"""SDN zones are tracked from before they exist until they are confirmed gone."""

from ipaddress import ip_address, ip_network
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from proxmoxsandbox._impl.infra_commands import InfraCommands
from proxmoxsandbox._impl.sdn_commands import (
    STATIC_SDN_START,
    IpamMapping,
    SdnCommands,
    SdnTeardownError,
)
from proxmoxsandbox.schema import DhcpRange, SdnConfig, SubnetConfig, VnetConfig

SDN_CONFIG = SdnConfig(
    vnet_configs=(
        VnetConfig(
            subnets=(
                SubnetConfig(
                    cidr=ip_network("192.168.50.0/24"),
                    gateway=ip_address("192.168.50.1"),
                    snat=True,
                    dhcp_ranges=(
                        DhcpRange(
                            start=ip_address("192.168.50.50"),
                            end=ip_address("192.168.50.100"),
                        ),
                    ),
                ),
            ),
        ),
    )
)


def _http_error(status: int, text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("DELETE", "https://pve/api2/json/x")
    response = httpx.Response(status, text=text, request=request)
    return httpx.HTTPStatusError(text, request=request, response=response)


def _sdn_commands(request_side_effect) -> SdnCommands:
    api = MagicMock()
    api.request = AsyncMock(side_effect=request_side_effect)
    sdn = SdnCommands(api)
    sdn.check_cidrs = AsyncMock()  # type: ignore[method-assign]
    sdn.do_update_all_sdn = AsyncMock()  # type: ignore[method-assign]
    return sdn


@pytest.mark.asyncio
async def test_zone_is_tracked_when_vnet_creation_fails():
    async def request(method, path, **kwargs):
        if method == "POST" and path == "/cluster/sdn/vnets":
            raise _http_error(400, "Parameter verification failed")
        return None

    sdn = _sdn_commands(request)

    with pytest.raises(httpx.HTTPStatusError):
        await sdn.create_sdn("abc123", SDN_CONFIG)

    assert sdn._tracked_sdn_zone_ids == {"abc123z"}


@pytest.mark.asyncio
async def test_static_zone_is_never_tracked():
    sdn = _sdn_commands(lambda *a, **k: None)

    zone_id, _ = await sdn.create_sdn(STATIC_SDN_START, SDN_CONFIG)

    assert zone_id == f"{STATIC_SDN_START}z"
    assert sdn._tracked_sdn_zone_ids == set()


@pytest.mark.asyncio
async def test_teardown_reports_only_zones_that_are_gone():
    async def request(method, path, **kwargs):
        if method == "GET" and path == "/cluster/sdn/vnets":
            return []
        if method == "DELETE" and path == "/cluster/sdn/zones/stuck1z":
            raise _http_error(500, "zone is used by vnet")
        if method == "DELETE" and path == "/cluster/sdn/zones/gone12z":
            raise _http_error(
                500, "delete sdn zone object failed: sdn zone 'gone12z' does not exist"
            )
        if method == "DELETE" and path == "/cluster/sdn/zones/vnet12z":
            # a vnet vanished mid-teardown; the zone itself is still there
            raise _http_error(500, "sdn vnet 'vnet12v0' does not exist")
        return None

    sdn = _sdn_commands(request)

    deleted = await sdn.tear_down_sdn_zones_and_vnets(
        ["stuck1z", "gone12z", "fine12z", "vnet12z"], ()
    )

    assert deleted == {"gone12z", "fine12z"}


@pytest.mark.asyncio
async def test_task_cleanup_keeps_tracking_a_zone_that_survives():
    sdn = _sdn_commands(lambda *a, **k: None)
    sdn.register_sdn_zone("stuck1z")
    sdn.register_sdn_zone("fine12z")
    stuck_mapping = IpamMapping(
        vnet_id="stuck1v0", zone_id="stuck1z", mac=None, ipv4="10.0.0.5"
    )
    fine_mapping = IpamMapping(
        vnet_id="fine12v0", zone_id="fine12z", mac=None, ipv4="10.0.1.5"
    )
    sdn.register_ipam_mapping(stuck_mapping)
    sdn.register_ipam_mapping(fine_mapping)
    sdn.tear_down_sdn_zones_and_vnets = AsyncMock(  # type: ignore[method-assign]
        return_value={"fine12z"}
    )

    await sdn.task_cleanup()

    assert sdn._tracked_sdn_zone_ids == {"stuck1z"}
    assert sdn._tracked_ipam_mappings == [stuck_mapping]


@pytest.mark.asyncio
async def test_delete_sdn_and_vms_raises_when_zone_survives():
    infra = MagicMock()
    infra.qemu_commands.destroy_vm = AsyncMock()
    infra.sdn_commands.tear_down_sdn_zone_and_vnet = AsyncMock(return_value=False)

    with pytest.raises(SdnTeardownError):
        await InfraCommands.delete_sdn_and_vms(
            infra, sdn_zone_id="abc123z", ipam_mappings=(), vms={}
        )
