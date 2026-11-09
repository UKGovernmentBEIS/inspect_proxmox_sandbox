"""Listing in-use CIDRs tolerates a vnet that vanished after it was listed."""

from unittest.mock import AsyncMock

import httpx
import pytest

from proxmoxsandbox._impl.sdn_commands import SdnCommands


def _gone(vnet: str) -> httpx.HTTPStatusError:
    text = f"sdn vnet '{vnet}' does not exist"
    request = httpx.Request("GET", "https://pve/api2/json/x")
    return httpx.HTTPStatusError(
        text, request=request, response=httpx.Response(500, text=text, request=request)
    )


@pytest.mark.asyncio
async def test_vnet_deleted_between_list_and_subnet_read_is_skipped():
    api = AsyncMock()
    api.request = AsyncMock(
        side_effect=[
            [
                {"zone": "aaa111z", "type": "simple"},
                {"zone": "bbb222z", "type": "simple"},
            ],
            [
                {"vnet": "aaa111v0", "zone": "aaa111z"},
                {"vnet": "bbb222v0", "zone": "bbb222z"},
            ],
            _gone("aaa111v0"),
            [{"cidr": "10.2.0.0/24"}],
        ]
    )
    assert await SdnCommands(api).read_all_simple_zone_cidrs() == ["10.2.0.0/24"]


@pytest.mark.asyncio
async def test_other_errors_reading_subnets_propagate():
    api = AsyncMock()
    request = httpx.Request("GET", "https://pve/api2/json/x")
    api.request = AsyncMock(
        side_effect=[
            [{"zone": "aaa111z", "type": "simple"}],
            [{"vnet": "aaa111v0", "zone": "aaa111z"}],
            httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(500, text="boom", request=request),
            ),
        ]
    )
    with pytest.raises(httpx.HTTPStatusError, match="boom"):
        await SdnCommands(api).read_all_simple_zone_cidrs()
