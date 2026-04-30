from ipaddress import ip_address, ip_network

import pytest
from pydantic_extra_types.mac_address import MacAddress

from proxmoxsandbox._impl.infra_commands import (
    _collect_static_maps,
    _validate_static_ip,
)
from proxmoxsandbox.schema import (
    DhcpRange,
    SubnetConfig,
    VmConfig,
    VmNicConfig,
    VmSourceConfig,
)


def _opn_subnet() -> SubnetConfig:
    return SubnetConfig(
        cidr=ip_network("10.0.2.0/24"),
        gateway=ip_address("10.0.2.1"),
        dhcp_ranges=(
            DhcpRange(start=ip_address("10.0.2.50"), end=ip_address("10.0.2.200")),
        ),
        vnet_type="opnsense",
        domain_whitelist=("example.com",),
    )


def _vm_with_static_ip(ipv4: str, name: str = "agent") -> VmConfig:
    return VmConfig(
        vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
        name=name,
        nics=(
            VmNicConfig(
                vnet_alias="lan",
                mac=MacAddress("52:54:00:12:34:56"),
                ipv4=ip_address(ipv4),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("bad_ip", "match"),
    [
        ("10.0.99.42", "outside LAN subnet"),
        ("10.0.2.1", "gateway"),
        ("10.0.2.55", "DHCP range"),
        ("10.0.2.150", "DHCP range"),
    ],
)
def test_validate_static_ip_rejects(bad_ip: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _validate_static_ip(_opn_subnet(), "agent", ip_address(bad_ip))


def test_validate_static_ip_accepts_in_subnet_outside_pool() -> None:
    # 10.0.2.10 — in the LAN subnet, not the gateway, outside the DHCP pool.
    _validate_static_ip(_opn_subnet(), "agent", ip_address("10.0.2.10"))


def test_collect_static_maps_rejects_bad_ip() -> None:
    vms = (_vm_with_static_ip("10.0.99.42"),)
    with pytest.raises(ValueError, match="outside LAN subnet"):
        _collect_static_maps(vms, {"lan": _opn_subnet()})


def test_collect_static_maps_collects_valid_ip() -> None:
    vms = (_vm_with_static_ip("10.0.2.10", name="agent"),)
    result = _collect_static_maps(vms, {"lan": _opn_subnet()})
    assert result == {
        "lan": [("52:54:00:12:34:56", "10.0.2.10", "agent")],
    }


def test_collect_static_maps_skips_other_vnets() -> None:
    # NIC on a non-OPNsense vnet should not be validated or collected.
    vm = VmConfig(
        vm_source_config=VmSourceConfig(built_in="ubuntu24.04"),
        name="agent",
        nics=(
            VmNicConfig(
                vnet_alias="wan",
                mac=MacAddress("52:54:00:aa:bb:cc"),
                ipv4=ip_address("203.0.113.5"),
            ),
        ),
    )
    result = _collect_static_maps((vm,), {"lan": _opn_subnet()})
    assert result == {"lan": []}
