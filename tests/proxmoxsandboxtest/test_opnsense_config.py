"""Unit tests for OPNsense config.xml generation (no Proxmox required)."""

import xml.etree.ElementTree as ET
from ipaddress import ip_address, ip_network

from proxmoxsandbox._impl.opnsense import generate_config_xml
from proxmoxsandbox.schema import DhcpRange, SubnetConfig


def _subnet(*, allow_internal: bool) -> SubnetConfig:
    return SubnetConfig(
        cidr=ip_network("10.0.2.0/24"),
        gateway=ip_address("10.0.2.1"),
        dhcp_ranges=(
            DhcpRange(start=ip_address("10.0.2.100"), end=ip_address("10.0.2.200")),
        ),
        vnet_type="opnsense",
        domain_whitelist=("example.com",),
        allow_internal=allow_internal,
    )


def _lan_interface_rules(root: ET.Element) -> list[ET.Element]:
    """Ordered LAN transit rules (excludes the floating lanip rules)."""
    return [
        r
        for r in root.find(".//filter").findall("rule")
        if r.findtext("interface") == "lan" and r.find("floating") is None
    ]


def test_allow_internal_false_has_no_rfc1918():
    root = ET.fromstring(generate_config_xml(_subnet(allow_internal=False)))
    assert root.find(".//*[name='rfc1918_internal']") is None
    # Still exactly: pass->whitelist, block->any.
    rules = _lan_interface_rules(root)
    assert [r.findtext("type") for r in rules] == ["pass", "block"]


def test_allow_internal_adds_alias():
    root = ET.fromstring(generate_config_xml(_subnet(allow_internal=True)))
    xpath = ".//OPNsense/Firewall/Alias/aliases/alias[name='rfc1918_internal']"
    alias = root.find(xpath)
    assert alias is not None
    assert alias.findtext("type") == "network"
    content = alias.findtext("content")
    assert "10.0.0.0/8" in content
    assert "172.16.0.0/12" in content
    assert "192.168.0.0/16" in content


def test_allow_internal_rule_precedes_block():
    """The RFC1918 pass must sit before the catch-all block (first-match)."""
    root = ET.fromstring(generate_config_xml(_subnet(allow_internal=True)))
    rules = _lan_interface_rules(root)
    # Order: pass whitelist, pass rfc1918, block any.
    assert [r.findtext("type") for r in rules] == ["pass", "pass", "block"]
    rfc_idx = next(
        i
        for i, r in enumerate(rules)
        if r.find("destination/address") is not None
        and r.findtext("destination/address") == "rfc1918_internal"
    )
    block_idx = next(
        i for i, r in enumerate(rules) if r.find("destination/any") is not None
    )
    assert rfc_idx < block_idx
