from ipaddress import ip_address, ip_network

import pytest
from pydantic import ValidationError
from pydantic_extra_types.mac_address import MacAddress

from proxmoxsandbox.schema import (
    DhcpRange,
    SubnetConfig,
    VmNicConfig,
    VnetConfig,
)


def _opnsense_subnet(
    *,
    cidr: str = "10.0.2.0/24",
    gateway: str = "10.0.2.1",
    domain_whitelist: tuple[str, ...] = ("example.com",),
) -> SubnetConfig:
    return SubnetConfig(
        cidr=ip_network(cidr),
        gateway=ip_address(gateway),
        dhcp_ranges=(
            DhcpRange(start=ip_address("10.0.2.100"), end=ip_address("10.0.2.200")),
        ),
        vnet_type="opnsense",
        domain_whitelist=domain_whitelist,
    )


def _proxmox_subnet(
    *,
    cidr: str = "10.0.3.0/24",
    gateway: str = "10.0.3.1",
) -> SubnetConfig:
    return SubnetConfig(
        cidr=ip_network(cidr),
        gateway=ip_address(gateway),
        dhcp_ranges=(
            DhcpRange(start=ip_address("10.0.3.100"), end=ip_address("10.0.3.200")),
        ),
        vnet_type="proxmox",
        snat=False,
    )


def test_vmnicconfig_ipv4_requires_mac():
    """IPv4 requires MAC address."""
    with pytest.raises(ValidationError, match="ipv4 address requires a mac address"):
        VmNicConfig(vnet_alias="test", ipv4=ip_address("192.168.1.10"))


def test_vmnicconfig_ipv4_with_mac():
    """IPv4 works with MAC address."""
    nic = VmNicConfig(
        vnet_alias="test",
        mac=MacAddress("52:54:00:12:34:56"),
        ipv4=ip_address("192.168.1.10"),
    )
    assert nic.ipv4 == ip_address("192.168.1.10")
    assert str(nic.mac) == "52:54:00:12:34:56"


def test_vmnicconfig_mac_only():
    """MAC without IPv4 is valid."""
    nic = VmNicConfig(vnet_alias="test", mac=MacAddress("52:54:00:12:34:56"))
    assert nic.ipv4 is None
    assert str(nic.mac) == "52:54:00:12:34:56"


def test_vmnicconfig_minimal():
    """Minimal config without MAC or IPv4."""
    nic = VmNicConfig(vnet_alias="test")
    assert nic.mac is None
    assert nic.ipv4 is None


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".",
        "*",
        "*.example.com",
        "1.2.3.4",
        "example.com:443",
        "https://example.com",
        " example.com",
        "ex ample.com",
        "foo\nbar.com",
        'foo"bar.com',
    ],
)
def test_subnet_rejects_bad_whitelist_entry(bad):
    with pytest.raises(ValidationError, match="domain_whitelist"):
        _opnsense_subnet(domain_whitelist=(bad,))


@pytest.mark.parametrize(
    "good",
    [
        "example.com",
        "sub.example.com",
        "example.com.",
        "foo-bar.example.com",
        "a.b.c.d.example.com",
    ],
)
def test_subnet_accepts_valid_whitelist_entry(good):
    subnet = _opnsense_subnet(domain_whitelist=(good,))
    assert subnet.domain_whitelist == (good,)


def test_vnet_rejects_mixed_subnet_types():
    with pytest.raises(ValidationError, match="vnet_type"):
        VnetConfig(
            alias="lan",
            subnets=(
                _proxmox_subnet(),
                _opnsense_subnet(),
            ),
        )


def test_vnet_rejects_multiple_opnsense_subnets():
    with pytest.raises(ValidationError, match="At most one"):
        VnetConfig(
            alias="lan",
            subnets=(
                _opnsense_subnet(
                    cidr="10.0.2.0/24",
                    gateway="10.0.2.1",
                    domain_whitelist=("a.com",),
                ),
                _opnsense_subnet(
                    cidr="10.0.4.0/24",
                    gateway="10.0.4.1",
                    domain_whitelist=("b.com",),
                ),
            ),
        )


def test_vnet_accepts_single_opnsense_subnet():
    vnet = VnetConfig(alias="lan", subnets=(_opnsense_subnet(),))
    assert len(vnet.subnets) == 1


def test_vnet_accepts_multiple_proxmox_subnets():
    vnet = VnetConfig(
        alias="lan",
        subnets=(
            _proxmox_subnet(cidr="10.0.3.0/24", gateway="10.0.3.1"),
            _proxmox_subnet(cidr="10.0.5.0/24", gateway="10.0.5.1"),
        ),
    )
    assert len(vnet.subnets) == 2
