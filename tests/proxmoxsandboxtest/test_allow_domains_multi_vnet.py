"""Unit tests for multi-vnet allow_domains support.

Tests the schema validation changes (accepting N vnets), the nftables config
generator (sandbox_nets named set), and the dnsmasq config generator (multiple
listen-address lines).  All pure/unit — no Proxmox connection needed.
"""

from ipaddress import ip_address, ip_network

import pytest
from pydantic import ValidationError

from proxmoxsandbox._impl.infra_commands import (
    _dnsmasq_allowlist_config,
    _gateway_mac,
    _nftables_config,
)
from proxmoxsandbox.schema import (
    DhcpRange,
    SdnConfig,
    SubnetConfig,
    VnetConfig,
)


def _make_vnet(third_octet: int) -> VnetConfig:
    """Helper: build a VnetConfig with 10.77.<third_octet>.0/24, DHCP .50-.100."""
    base = f"10.77.{third_octet}"
    return VnetConfig(
        alias=f"test-{third_octet}",
        subnets=(
            SubnetConfig(
                cidr=ip_network(f"{base}.0/24"),
                snat=True,
                dhcp_ranges=(
                    DhcpRange(
                        start=ip_address(f"{base}.50"),
                        end=ip_address(f"{base}.100"),
                    ),
                ),
            ),
        ),
    )


# --- Schema validation ---


class TestSdnConfigMultiVnet:
    def test_single_vnet_accepted(self) -> None:
        """Backward compat: single vnet + allow_domains still works."""
        config = SdnConfig(
            vnet_configs=(_make_vnet(10),),
            allow_domains=("example.com",),
        )
        assert len(config.vnet_configs) == 1

    def test_two_vnets_accepted(self) -> None:
        config = SdnConfig(
            vnet_configs=(_make_vnet(10), _make_vnet(11)),
            allow_domains=("example.com",),
        )
        assert len(config.vnet_configs) == 2

    def test_six_vnets_accepted(self) -> None:
        """CAST needs 6+ vnets."""
        vnets = tuple(_make_vnet(i) for i in range(10, 16))
        config = SdnConfig(
            vnet_configs=vnets,
            allow_domains=("example.com",),
        )
        assert len(config.vnet_configs) == 6

    def test_zero_vnets_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one vnet_config"):
            SdnConfig(
                vnet_configs=(),
                allow_domains=("example.com",),
            )

    def test_dhcp_range_conflict_caught_on_second_vnet(self) -> None:
        """DHCP range conflict is caught for any vnet, not just the first."""
        ok_vnet = _make_vnet(10)
        # This vnet's DHCP range includes .2 (gateway IP)
        bad_base = "10.77.11"
        bad_vnet = VnetConfig(
            alias="bad",
            subnets=(
                SubnetConfig(
                    cidr=ip_network(f"{bad_base}.0/24"),
                    snat=True,
                    dhcp_ranges=(
                        DhcpRange(
                            start=ip_address(f"{bad_base}.1"),
                            end=ip_address(f"{bad_base}.10"),
                        ),
                    ),
                ),
            ),
        )
        with pytest.raises(ValidationError, match="vnet 1"):
            SdnConfig(
                vnet_configs=(ok_vnet, bad_vnet),
                allow_domains=("example.com",),
            )

    def test_gateway_mismatch_caught_on_any_vnet(self) -> None:
        """Explicit wrong gateway is caught on any vnet."""
        base = "10.77.12"
        bad_vnet = VnetConfig(
            alias="bad-gw",
            subnets=(
                SubnetConfig(
                    cidr=ip_network(f"{base}.0/24"),
                    gateway=ip_address(f"{base}.99"),
                    snat=True,
                    dhcp_ranges=(
                        DhcpRange(
                            start=ip_address(f"{base}.50"),
                            end=ip_address(f"{base}.100"),
                        ),
                    ),
                ),
            ),
        )
        with pytest.raises(ValidationError, match="do not set the gateway"):
            SdnConfig(
                vnet_configs=(_make_vnet(10), bad_vnet),
                allow_domains=("example.com",),
            )

    def test_bare_ip_rejected_in_allow_domains(self) -> None:
        """IP addresses are not valid domain names."""
        with pytest.raises(
            ValidationError, match="looks like an IP"
        ):
            SdnConfig(
                vnet_configs=(_make_vnet(10),),
                allow_domains=("8.8.8.8",),
            )

    def test_no_allow_domains_skips_vnet_validation(self) -> None:
        """Without allow_domains, multi-vnet is always fine (no gateway constraints)."""
        config = SdnConfig(
            vnet_configs=(_make_vnet(10), _make_vnet(11)),
            allow_domains=(),
        )
        assert len(config.vnet_configs) == 2


# --- _gateway_mac ---


class TestGatewayMac:
    def test_different_vnet_index_gives_different_mac(self) -> None:
        mac0 = _gateway_mac("test123", 0)
        mac1 = _gateway_mac("test123", 1)
        assert mac0 != mac1

    def test_default_vnet_index_matches_explicit_zero(self) -> None:
        assert _gateway_mac("test123") == _gateway_mac("test123", 0)

    def test_mac_format(self) -> None:
        mac = _gateway_mac("test123", 0)
        assert mac.startswith("52:54:00:")
        assert len(mac) == 17


# --- _nftables_config ---


class TestNftablesConfig:
    def test_single_cidr_produces_sandbox_nets_set(self) -> None:
        config = _nftables_config(("10.77.10.0/24",))
        assert "set sandbox_nets" in config
        assert "elements = { 10.77.10.0/24 }" in config
        assert "@sandbox_nets" in config
        # No raw CIDR references in chain rules
        assert "ip saddr 10.77.10.0/24" not in config

    def test_two_cidrs_produces_both_elements(self) -> None:
        config = _nftables_config(("10.77.10.0/24", "10.77.11.0/24"))
        assert "10.77.10.0/24, 10.77.11.0/24" in config
        assert "set sandbox_nets" in config

    def test_allowed_ips_set_with_timeout(self) -> None:
        config = _nftables_config(("10.77.10.0/24",))
        assert "set allowed_ips" in config
        assert "@allowed_ips" in config
        assert "timeout 1h" in config

    def test_input_chain_restricts_to_port_53(self) -> None:
        config = _nftables_config(("10.77.10.0/24",))
        assert "chain input" in config
        assert "udp dport 53 accept" in config
        assert "tcp dport 53 accept" in config
        assert "policy drop" in config

    def test_forward_chain_restricts_to_tcp_udp(self) -> None:
        """Forward only allows TCP and UDP to allowed IPs."""
        config = _nftables_config(("10.77.10.0/24",))
        assert "tcp daddr @allowed_ips" in config
        assert "udp daddr @allowed_ips" in config
        # No protocol-agnostic accept rule for allowed_ips
        assert "ip daddr @allowed_ips counter accept" not in config


# --- _dnsmasq_allowlist_config ---


class TestDnsmasqAllowlistConfig:
    def test_single_ip(self) -> None:
        config = _dnsmasq_allowlist_config(("10.77.10.2",), ("example.com",))
        assert "listen-address=10.77.10.2" in config
        assert "server=/example.com/8.8.8.8" in config

    def test_nftset_lines_generated(self) -> None:
        """Each domain gets an nftset= line for dynamic IP injection."""
        config = _dnsmasq_allowlist_config(
            ("10.77.10.2",), ("example.com", "foo.org")
        )
        assert "nftset=/example.com/4#inet#gateway#allowed_ips" in config
        assert "nftset=/foo.org/4#inet#gateway#allowed_ips" in config

    def test_two_ips_produces_two_listen_addresses(self) -> None:
        config = _dnsmasq_allowlist_config(
            ("10.77.10.2", "10.77.11.2"), ("example.com",)
        )
        assert "listen-address=10.77.10.2" in config
        assert "listen-address=10.77.11.2" in config

    def test_bind_interfaces_present(self) -> None:
        config = _dnsmasq_allowlist_config(("10.77.10.2",), ("example.com",))
        assert "bind-interfaces" in config
