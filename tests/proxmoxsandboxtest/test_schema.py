from ipaddress import ip_address

import pytest
from pydantic import ValidationError
from pydantic_extra_types.mac_address import MacAddress

from proxmoxsandbox.schema import VmNicConfig


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
